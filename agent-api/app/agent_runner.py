"""Reusable LangGraph runner that talks to AIGW (Portkey) + MCP servers.

The frontend / API keeps ONE ChatSession instance per user session (in memory).
Each session lazily loads the MCP tools once on first message, then reuses them
for the rest of that HTTP session.

MCP transport: custom stateless streamable-http client (see app/mcp_httpx.py).
We do NOT use langchain-mcp-adapters because the OSS `mcp` client insists on
stateful sessions that our supergateway-fronted MCP servers do not implement.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import uuid
import logging
from typing import Any

from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

from app.mcp_httpx import load_tools_from_servers

log = logging.getLogger("agent_runner")

# --------------------------------------------------------------------------
# Config from env
# --------------------------------------------------------------------------
AIGW_BASE_URL     = os.environ.get("AIGW_BASE_URL",     "http://aigw.local/v1")
GEMINI_API_KEY    = os.environ.get("GEMINI_API_KEY", "gemini-poc-not-validated")
PORTKEY_API_KEY   = os.environ.get("PORTKEY_API_KEY", "")
MCP_GATEWAY_BASE  = os.environ.get("MCP_GATEWAY_BASE", "http://portkey-gateway.airs-gw.svc.cluster.local:8788").rstrip("/")
if not PORTKEY_API_KEY:
    raise RuntimeError("PORTKEY_API_KEY env var is required (SCM-issued key from AIGW UI)")
GEMINI_MODEL      = os.environ.get("GEMINI_MODEL",      "gemini-2.0-flash")
AIGW_USER         = os.environ.get("AIGW_USER", "aigwuser")
AIGW_PASS         = os.environ.get("AIGW_PASS", "")

# Fixed metadata tags added to every request (used by SCM Budget / Rate Limit
# policies with Group by Key or Conditions). Extend as needed.
LLM_TEAM          = os.environ.get("LLM_METADATA_TEAM",        "np-team")
LLM_ENVIRONMENT   = os.environ.get("LLM_METADATA_ENVIRONMENT", "poc")

_basic = base64.b64encode(f"{AIGW_USER}:{AIGW_PASS}".encode()).decode()
BASIC_AUTH_HEADER = f"Basic {_basic}"


def _build_llm(
    chat_user: str,
    session_trace_id: str,
    *,
    access_token: str | None = None,
    email: str | None = None,
    groups: list[str] | None = None,
) -> ChatOpenAI:
    """Build a ChatOpenAI wired to Portkey/SCM with per-user metadata.

    Two auth modes:
      1. JWT   (access_token is not None): send the Keycloak access_token as
         `api_key=<token>` so LangChain emits `Authorization: Bearer <token>`.
         Portkey Gateway (with JWT_ENABLED=ON) validates it against Keycloak's
         JWKS and enforces the SCM policies attached to the token's scopes.
      2. API_KEY (fallback / legacy password login): use the shared
         PORTKEY_API_KEY (SCM-issued virtual key). Used only until every
         browser session is OIDC.

    Metadata keys attached to every LLM request (flat primitives — Portkey
    silently drops nested keys or array values):
        - _user        : Portkey-reserved -> SCM Observability "User ID"
        - chat_user    : same, kept for backwards compat with earlier policies
        - team         : static tag from LLM_METADATA_TEAM env
        - environment  : static tag from LLM_METADATA_ENVIRONMENT env
        - email        : OIDC email if present
        - groups       : comma-joined OIDC groups if present
    """
    metadata: dict[str, str] = {
        "_user":       chat_user,   # Portkey-reserved key -> SCM Observability "User ID"
        "chat_user":   chat_user,
        "team":        LLM_TEAM,
        "environment": LLM_ENVIRONMENT,
    }
    if email:
        metadata["email"] = email
    if groups:
        # Portkey rejects array values -> join
        metadata["groups"] = ",".join(groups)

    if access_token:
        api_key_to_use = access_token
        auth_mode = "JWT"
    else:
        api_key_to_use = PORTKEY_API_KEY
        auth_mode = "API_KEY"

    log.info(
        "LLM auth=%s user=%s email=%s groups=%s trace_id=%s",
        auth_mode, chat_user, email, metadata.get("groups"), session_trace_id,
    )

    return ChatOpenAI(
        model=GEMINI_MODEL,
        base_url=AIGW_BASE_URL,
        api_key=api_key_to_use,    # JWT (bearer) or SCM virtual key
        temperature=0.2,
        timeout=60,
        max_retries=1,
        default_headers={
            # SCM enterprise mode: use saved integration by slug (@integration-slug)
            "x-portkey-provider": "@geminiapi",
            # Per-request metadata for SCM Budget / Rate Limit / Conditional Routing
            "x-portkey-metadata": json.dumps(metadata),
            # One trace_id per browser session so SCM Traces tab groups related turns
            "x-portkey-trace-id": session_trace_id,
        },
    )


def _mcp_server_config() -> dict[str, dict[str, Any]]:
    """Hybrid routing:
    - weather -> through Portkey MCP Gateway (SCM observes + policies)
    - filesystem, github -> direct (supergateway/github-mcp-server not yet
      compatible with Portkey MCP Gateway session model)
    """
    portkey_hdrs = {"x-portkey-api-key": PORTKEY_API_KEY} if PORTKEY_API_KEY else {}
    return {
        "filesystem": {
            "url": "http://mcp-filesystem.mcp-servers.svc.cluster.local:8080/mcp",
        },
        "github": {
            "url": "http://mcp-github.mcp-servers.svc.cluster.local:8080/mcp",
        },
        "weather": {
            "url":     f"{MCP_GATEWAY_BASE}/weather/mcp",
            "headers": portkey_hdrs,
        },
    }

class ChatSession:
    """One agent + history bag per browser session."""

    def __init__(
        self,
        username: str = "anon",
        *,
        # ---- optional OIDC identity (populated by BFF after Keycloak login) ----
        email: str | None = None,
        preferred_username: str | None = None,
        groups: list[str] | None = None,
        access_token: str | None = None,
        refresh_token: str | None = None,
        access_token_exp: int | None = None,  # epoch seconds
        id_token: str | None = None,
    ) -> None:
        self.username = username
        # ---- OIDC identity (None for legacy password-auth sessions) ----
        self.email              = email
        self.preferred_username = preferred_username or username
        self.groups             = groups or []
        self.access_token       = access_token
        self.refresh_token      = refresh_token
        self.access_token_exp   = access_token_exp
        # id_token is retained ONLY so we can pass it as id_token_hint to
        # Keycloak's end_session_endpoint on logout (without it Keycloak keeps
        # the SSO cookie and the next "Sign in with Keycloak" click silently
        # replays the same identity).
        self.id_token           = id_token
        # One SCM/Portkey trace_id per browser session. Every LLM call this
        # ChatSession makes will carry the same trace_id header so the
        # SCM Traces tab groups them into one tree.
        self.trace_id = uuid.uuid4().hex
        self.llm    = _build_llm(
            chat_user=username,
            session_trace_id=self.trace_id,
            access_token=access_token,
            email=email,
            groups=self.groups,
        )
        self.agent  = None
        self.tools_meta: list[dict[str, str]] = []
        self.history: list[dict[str, str]] = []
        self._lock  = asyncio.Lock()
        log.info(
            "ChatSession created user=%s pref=%s groups=%s trace_id=%s oidc=%s",
            username, self.preferred_username, self.groups, self.trace_id,
            bool(access_token),
        )

    async def _ensure_agent(self) -> None:
        if self.agent is not None:
            return
        try:
            tools = await load_tools_from_servers(_mcp_server_config())
            log.info("loaded %d MCP tools total: %s", len(tools), [t.name for t in tools])
        except Exception as exc:
            log.exception("load_tools_from_servers failed: %s", exc)
            tools = []
        self.tools_meta = [
            {"name": t.name, "description": (getattr(t, "description", "") or "").strip()[:200]}
            for t in tools
        ]
        self.agent = create_react_agent(self.llm, tools)
        log.info("session bootstrapped with %d MCP tools", len(tools))

    async def send(self, user_message: str) -> dict[str, Any]:
        """Run the agent for one user turn.  Returns dict:
            { "reply": str, "tool_calls": [ {name, args, output_preview}, ... ] }
        """
        async with self._lock:
            await self._ensure_agent()
            self.history.append({"role": "user", "content": user_message})
            try:
                result = await self.agent.ainvoke({"messages": self.history})
            except Exception as exc:
                log.exception("agent invoke failed")
                return {"reply": f"[error] {exc}", "tool_calls": []}

            msgs = result.get("messages", [])
            reply = ""
            tool_calls: list[dict[str, Any]] = []
            for m in msgs:
                # capture assistant tool_calls if the model made any
                tc = getattr(m, "tool_calls", None)
                if tc:
                    for c in tc:
                        tool_calls.append({
                            "name": c.get("name") if isinstance(c, dict) else getattr(c, "name", ""),
                            "args": c.get("args") if isinstance(c, dict) else getattr(c, "args", {}),
                        })
                # capture tool result previews
                if getattr(m, "type", "") == "tool":
                    content = getattr(m, "content", "")
                    if isinstance(content, list):
                        content = " ".join(str(x) for x in content)
                    tool_calls.append({
                        "name":    getattr(m, "name", "tool"),
                        "output_preview": str(content)[:400],
                    })
            if msgs:
                last = msgs[-1]
                reply = getattr(last, "content", "") or ""
                if isinstance(reply, list):
                    reply = " ".join(str(x) for x in reply)

            self.history.append({"role": "assistant", "content": reply})
            # Trim history so we don't grow unbounded
            if len(self.history) > 40:
                self.history = self.history[-40:]

            return {"reply": reply, "tool_calls": tool_calls}
