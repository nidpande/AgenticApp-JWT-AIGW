"""FastAPI backend serving the AIGW Chat frontend.

Endpoints
    GET  /             - serves the SPA (static/index.html)
    GET  /health       - liveness
    POST /api/login    - {username, password} -> Set-Cookie session (legacy path)
    POST /api/logout   - clears the session cookie
    GET  /api/me       - returns {username, authenticated} + tool list if bootstrapped
    POST /api/chat     - {message} -> {reply, tool_calls}

Auth model:
    - App-level username/password lives in env CHAT_USER / CHAT_PASS (legacy).
    - OIDC BFF (auth_oidc.py) is the primary path -- Keycloak login sets the
      same signed cookie via /api/auth/callback.
    - Cookie is signed with itsdangerous using CHAT_SESSION_SECRET.
    - The AIGW Basic-Auth creds are ONLY known to the server-side agent runner;
      the browser never receives them.

Phase 4 change (Redis-backed sessions)
--------------------------------------
Sessions no longer live in a process-local dict; they are persisted in a
:class:`session_store.SessionStore` (Redis in prod, in-memory locally). Only
the PRIMITIVE state of a ChatSession is stored (see :func:`_session_to_dict`).
The LLM and LangGraph agent are rebuilt on the fly on every request from
``session.access_token`` (this already happened in Phase 3 for OIDC sessions,
we simply extend it to be the *only* source of truth). Consequences:

  * Pod restarts / rollouts no longer log every user out.
  * Horizontal scale-up of agent-api works: any replica can serve any user
    because the cookie's ``sid`` is looked up in the shared store.
  * TTL on the Redis key is set to ``access_token_exp - now()`` so expired
    OIDC sessions self-evict.
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Any

from fastapi import FastAPI, Request, Response, HTTPException, Depends
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import URLSafeSerializer, BadSignature
from pydantic import BaseModel

from .agent_runner  import ChatSession, _build_llm
from .auth_oidc     import router as oidc_router, is_enabled as oidc_enabled, refresh_if_needed
from .session_store import make_store

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("agent_api")

# --------------------------------------------------------------------------
# App-level auth config
# --------------------------------------------------------------------------
CHAT_USER           = os.environ.get("CHAT_USER", "admin")
CHAT_PASS           = os.environ.get("CHAT_PASS", "")
CHAT_SESSION_SECRET = os.environ.get("CHAT_SESSION_SECRET", "change-me-please")
COOKIE_NAME         = "aigw_chat_session"

# Fallback TTL (seconds) for cookie-only / legacy sessions with no
# access_token_exp. 8 h matches the previous max_age on the Set-Cookie header.
DEFAULT_SESSION_TTL = 60 * 60 * 8

if not CHAT_PASS:
    log.warning("CHAT_PASS is empty - legacy /api/login is effectively disabled")

_serializer = URLSafeSerializer(CHAT_SESSION_SECRET, salt="aigw-chat")

# --------------------------------------------------------------------------
# Shared session store (Redis if REDIS_URL is set, else in-memory).
# Exported as ``store`` so auth_oidc.py (which imports us lazily) can push
# newly-minted OIDC sessions into it.
# --------------------------------------------------------------------------
store = make_store()


# --------------------------------------------------------------------------
# ChatSession <-> dict serialisation
# --------------------------------------------------------------------------
def _session_to_dict(cs: ChatSession) -> dict[str, Any]:
    """Serialisable snapshot of ChatSession state.

    We deliberately drop the fields that carry live objects:

      * ``llm``    - ChatOpenAI (rebuilt every turn from access_token anyway)
      * ``agent``  - LangGraph runtime (rebuilt lazily in ``_ensure_agent``)
      * ``_lock``  - asyncio.Lock (per-process; new pod -> new lock)
      * ``tools_meta`` - regenerated on first ``_ensure_agent`` call

    Everything else is a plain str / int / list so it pickles cleanly.
    """
    return {
        "username":            cs.username,
        "email":               cs.email,
        "preferred_username":  cs.preferred_username,
        "groups":              cs.groups,
        "access_token":        cs.access_token,
        "refresh_token":       cs.refresh_token,
        "access_token_exp":    cs.access_token_exp,
        "id_token":            getattr(cs, "id_token", None),
        "trace_id":            cs.trace_id,
        "history":             cs.history,
    }


def _dict_to_session(d: dict[str, Any]) -> ChatSession:
    """Rehydrate a ChatSession from its dict snapshot.

    Restores mutable fields (``trace_id``, ``history``) that the constructor
    would otherwise reset. The LLM is rebuilt inside ``__init__`` from the
    (possibly refreshed) ``access_token``; the agent stays ``None`` and will
    be built on the first ``send()`` call.
    """
    cs = ChatSession(
        username           = d["username"],
        email              = d.get("email"),
        preferred_username = d.get("preferred_username"),
        groups             = d.get("groups") or [],
        access_token       = d.get("access_token"),
        refresh_token      = d.get("refresh_token"),
        access_token_exp   = d.get("access_token_exp"),
        id_token           = d.get("id_token"),
    )
    # Restore mutable state that __init__ resets to fresh defaults.
    cs.trace_id = d.get("trace_id", cs.trace_id)
    cs.history  = d.get("history") or []
    return cs


def _ttl_for(cs: ChatSession) -> int:
    """Compute the Redis TTL (seconds) for a session.

    For OIDC sessions we use the access-token remaining lifetime so an
    expired token can never resurrect. For legacy password sessions (no
    ``access_token_exp``) we fall back to :data:`DEFAULT_SESSION_TTL`.
    """
    exp = getattr(cs, "access_token_exp", None)
    if exp:
        remaining = int(exp) - int(time.time())
        # Give expired tokens ~30 s of grace so a mid-flight request that
        # is about to trigger refresh_if_needed() still finds the session.
        return max(remaining + 30, 60)
    return DEFAULT_SESSION_TTL


async def _persist_session(sid: str, cs: ChatSession) -> None:
    """Write the session snapshot back to the store after a mutation.

    Called after every chat turn (history + potentially-refreshed tokens
    must survive a pod restart) and whenever the OIDC BFF creates a new
    session.
    """
    await store.set(sid, _session_to_dict(cs), ttl=_ttl_for(cs))


def _sign(session_id: str, username: str) -> str:
    return _serializer.dumps({"sid": session_id, "u": username})


def _verify(token: str) -> dict[str, Any] | None:
    try:
        return _serializer.loads(token)
    except BadSignature:
        return None


async def _current_session(req: Request) -> ChatSession:
    """FastAPI dependency: resolve the signed cookie to a live ChatSession.

    Reads the ``aigw_chat_session`` cookie, validates the signature, looks
    up the primitive session snapshot in the store, and rebuilds a
    ChatSession on the fly. Raises 401 if any step fails so the SPA can
    redirect to Keycloak.
    """
    tok = req.cookies.get(COOKIE_NAME)
    if not tok:
        raise HTTPException(status_code=401, detail="not authenticated")
    data = _verify(tok)
    if not data:
        raise HTTPException(status_code=401, detail="bad session")
    sid = data.get("sid", "")
    snapshot = await store.get(sid)
    if not snapshot:
        # Session either expired, was evicted by TTL, or was never persisted
        # (e.g. cookie survived a Redis flush). Force re-login.
        raise HTTPException(status_code=401, detail="session expired")
    cs = _dict_to_session(snapshot)
    # Stash the sid on the session so downstream handlers can persist back
    # without re-parsing the cookie.
    cs._sid = sid  # type: ignore[attr-defined]
    return cs


# --------------------------------------------------------------------------
# FastAPI app
# --------------------------------------------------------------------------
app  = FastAPI(title="AIGW Chat", version="1.0.0")
HERE = os.path.dirname(__file__)
STATIC_DIR = os.path.join(HERE, "static")

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# OIDC BFF endpoints: GET /api/auth/login, GET /api/auth/callback, POST /api/auth/logout
# Returns 501 for all routes if OIDC_* env vars are not set (legacy password login still works).
app.include_router(oidc_router)
log.info("OIDC BFF enabled=%s | session store=%s",
         oidc_enabled(), type(store).__name__)


class LoginIn(BaseModel):
    username: str
    password: str


class ChatIn(BaseModel):
    message: str


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/login")
async def login(body: LoginIn, response: Response) -> dict[str, Any]:
    """Legacy username+password login. Kept for emergency/admin access;
    the OIDC BFF (/api/auth/login) is the primary flow."""
    if body.username != CHAT_USER or body.password != CHAT_PASS or not CHAT_PASS:
        raise HTTPException(status_code=401, detail="invalid credentials")
    sid = uuid.uuid4().hex
    chat_session = ChatSession(username=body.username)
    await _persist_session(sid, chat_session)
    response.set_cookie(
        COOKIE_NAME, _sign(sid, body.username),
        httponly=True, samesite="lax", max_age=DEFAULT_SESSION_TTL, path="/",
    )
    return {"authenticated": True, "username": body.username}


@app.post("/api/logout")
async def logout(request: Request, response: Response) -> dict[str, str]:
    tok = request.cookies.get(COOKIE_NAME)
    if tok:
        data = _verify(tok) or {}
        sid = data.get("sid", "")
        if sid:
            await store.delete(sid)
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"status": "logged out"}


@app.get("/api/me")
async def me(request: Request) -> dict[str, Any]:
    tok = request.cookies.get(COOKIE_NAME)
    if not tok:
        return {"authenticated": False, "oidc_available": oidc_enabled()}
    data = _verify(tok)
    if not data:
        return {"authenticated": False, "oidc_available": oidc_enabled()}
    snapshot = await store.get(data.get("sid", ""))
    if not snapshot:
        # Cookie is signed & valid but the server-side session is gone
        # (TTL, restart before we had Redis, manual FLUSHDB, ...). Signal
        # the SPA to re-authenticate.
        return {"authenticated": False, "oidc_available": oidc_enabled()}

    out: dict[str, Any] = {
        "authenticated":  True,
        "username":       data.get("u"),
        # ``tools_meta`` isn't persisted (regenerated per pod on first chat);
        # SPA can still call /api/chat which will populate it.
        "tools":          [],
        "oidc_available": oidc_enabled(),
    }
    # If this session was created via OIDC, surface the enriched identity
    # so the SPA can render "signed in as alice@example.local [gemini-users]".
    if snapshot.get("access_token"):
        out["oidc"] = {
            "preferred_username": snapshot.get("preferred_username"),
            "email":              snapshot.get("email"),
            "groups":             snapshot.get("groups") or [],
            "exp":                snapshot.get("access_token_exp"),
        }
    return out


@app.post("/api/chat")
async def chat(body: ChatIn, session: ChatSession = Depends(_current_session)) -> dict[str, Any]:
    if not body.message or not body.message.strip():
        raise HTTPException(status_code=400, detail="empty message")

    sid: str = getattr(session, "_sid", "")

    # -- OIDC token freshness: refresh the access_token silently if it is
    #    within 30s of expiry. On expiry with no refresh_token, this raises
    #    HTTPException(401) which the SPA translates into a Keycloak redirect.
    try:
        await refresh_if_needed(session)
    except HTTPException:
        # Drop the server-side session so re-login is clean, then bubble up 401.
        if sid:
            await store.delete(sid)
        raise

    # -- If this is an OIDC session, rebuild the LLM every turn so the
    #    ChatOpenAI instance ALWAYS carries the freshest access_token
    #    (refresh may have just rotated it). Cheap: ChatOpenAI ctor is
    #    lightweight; we only recreate the agent if the LLM changed.
    if getattr(session, "access_token", None):
        session.llm = _build_llm(
            chat_user=session.username,
            session_trace_id=session.trace_id,
            access_token=session.access_token,
            email=session.email,
            groups=session.groups,
        )
        # Force agent to be recreated with the new LLM on next send()
        session.agent = None

    result = await session.send(body.message.strip())

    # -- Persist back so history + any refreshed tokens survive pod restart.
    if sid:
        await _persist_session(sid, session)

    return result
