"""LangGraph agentic app for the AIGW hybrid POC (Gemini upstream).

Every LLM call is routed through the Portkey AI Gateway (aigw.local:80 → 8787).
Every MCP tool call is routed through the Portkey MCP Gateway (mcp-aigw.local:80 → 8788).

Both gateways sit behind an ingress-nginx that enforces **HTTP Basic Auth**.
The agent therefore attaches an `Authorization: Basic <base64>` header on
every LLM and MCP request.

The upstream provider selected by AIGW is **Google Gemini** (AI Studio).
Portkey exposes an OpenAI-compatible surface at `/v1/chat/completions`, so
the agent uses `langchain-openai` unchanged - AIGW handles the translation
from the OpenAI request schema to Gemini's `generateContent` schema.

Requires:
  * .env with AIGW_USER, AIGW_PASS, GEMINI_API_KEY (used by the gateway upstream)
  * deploy.sh already ran

Usage:
    python agent.py              # interactive REPL
    python agent.py "your query" # one-shot
"""
from __future__ import annotations

import asyncio
import base64
import os
import sys
import textwrap

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.prebuilt import create_react_agent

load_dotenv()

AIGW_BASE_URL     = os.environ.get("AIGW_BASE_URL",     "http://aigw.local/v1")
MCP_GATEWAY_BASE  = os.environ.get("MCP_GATEWAY_BASE",  "http://mcp-aigw.local")

# The GEMINI_API_KEY value is what the AIGW actually forwards to Google.
# We also send it as the OpenAI-style "api_key" so Portkey's OpenAI-compat
# surface accepts the request; Portkey then swaps in the real Gemini key
# resolved from its config (`api_key: "$GEMINI_API_KEY"`).
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "gemini-poc-not-validated")
GEMINI_MODEL   = os.environ.get("GEMINI_MODEL",   "gemini-2.0-flash")

# --- Basic Auth (ingress-nginx) ---
AIGW_USER = os.environ.get("AIGW_USER", "aigwuser")
AIGW_PASS = os.environ.get("AIGW_PASS", "")
if not AIGW_PASS:
    print("[agent] WARNING: AIGW_PASS is empty. Requests will fail 401 at ingress.", file=sys.stderr)

_basic = base64.b64encode(f"{AIGW_USER}:{AIGW_PASS}".encode()).decode()
BASIC_AUTH_HEADER = f"Basic {_basic}"


def build_llm() -> ChatOpenAI:
    """LLM that routes through the AI Gateway (OpenAI-compatible surface).

    - `model` is the Gemini model name; Portkey maps it upstream.
    - `default_headers` carries the Basic Auth to ingress-nginx.
    """
    return ChatOpenAI(
        model=GEMINI_MODEL,
        base_url=AIGW_BASE_URL,
        api_key=GEMINI_API_KEY,
        temperature=0.2,
        timeout=60,
        max_retries=1,
        default_headers={"Authorization": BASIC_AUTH_HEADER},
    )


def build_mcp_client() -> MultiServerMCPClient:
    """MCP client where every server URL points at the MCP Gateway.

    Each per-server entry carries the Basic Auth header so ingress-nginx
    accepts the request.  The gateway then forwards the JSON-RPC call to
    the real upstream registered in portkey-config.json.
    """
    common_headers = {"Authorization": BASIC_AUTH_HEADER}
    return MultiServerMCPClient({
        "filesystem": {
            "transport": "streamable_http",
            "url": f"{MCP_GATEWAY_BASE}/filesystem/mcp",
            "headers": common_headers,
        },
        "github": {
            "transport": "streamable_http",
            "url": f"{MCP_GATEWAY_BASE}/github/mcp",
            "headers": common_headers,
        },
        "weather": {
            "transport": "streamable_http",
            "url": f"{MCP_GATEWAY_BASE}/weather/mcp",
            "headers": common_headers,
        },
    })


async def build_agent():
    llm     = build_llm()
    mcp     = build_mcp_client()
    # get_tools is sync in older langchain-mcp-adapters (<=0.1.x) and async in newer
    # versions - handle both without pinning.
    result = mcp.get_tools()
    import inspect
    if inspect.isawaitable(result):
        tools = await result
    else:
        tools = result
    print(f"[agent] loaded {len(tools)} MCP tools via MCP Gateway:")
    for t in tools:
        print(f"        - {t.name}")
    return create_react_agent(llm, tools)


async def run_once(query: str) -> None:
    agent = await build_agent()
    print(f"\n[agent] >>> {query}\n")
    try:
        result = await agent.ainvoke({"messages": [{"role": "user", "content": query}]})
        final  = result["messages"][-1].content
        print(f"[agent] <<< {final}\n")
    except Exception as exc:
        print(f"[agent] !!! error: {exc}\n")


async def repl() -> None:
    agent = await build_agent()
    print(textwrap.dedent(f"""
        AIGW hybrid POC - interactive agent
        LLM  : Gemini via AIGW  (model={GEMINI_MODEL})
        Auth : Basic (user={AIGW_USER})
        Type your query and hit Enter.  Ctrl-C to exit.
    """))
    while True:
        try:
            q = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not q:
            continue
        try:
            result = await agent.ainvoke({"messages": [{"role": "user", "content": q}]})
            print(f"bot> {result['messages'][-1].content}\n")
        except Exception as exc:
            print(f"bot> [error] {exc}\n")


def main() -> None:
    if len(sys.argv) > 1:
        asyncio.run(run_once(" ".join(sys.argv[1:])))
    else:
        asyncio.run(repl())


if __name__ == "__main__":
    main()
