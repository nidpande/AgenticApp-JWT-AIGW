"""Session store abstraction: in-memory for local dev, Redis for prod.

Selected by REDIS_URL env var:
  - REDIS_URL unset -> InMemorySessionStore (dict backed, POC / unit tests)
  - REDIS_URL set   -> RedisSessionStore (redis.asyncio + pickle)

Stored value is a dict of the ChatSession's PRIMITIVE state (no LangChain
objects, no _lock, no agent). The LLM + agent are lazily rebuilt on each
retrieval by main._current_session(). This matches Phase 3 behaviour where
we already rebuild the LLM before every chat turn from session.access_token.

TTL is derived from access_token_exp so expired sessions naturally evict
from Redis (and never come back as ghosts after the token is dead).

Notes
-----
- We use ``decode_responses=False`` on the redis client so ``get()`` returns
  raw ``bytes`` suitable for ``pickle.loads`` -- pickle payloads are binary
  and must not be UTF-8 decoded.
- Pickle is safe here because we only ever unpickle data we ourselves wrote
  to a cluster-local Redis (no untrusted input). If Redis is ever exposed
  we'd switch to JSON or msgpack.
"""
from __future__ import annotations

import logging
import os
import pickle
from typing import Any, Protocol

log = logging.getLogger("session_store")


# --------------------------------------------------------------------------
# Interface
# --------------------------------------------------------------------------
class SessionStore(Protocol):
    async def get(self, sid: str) -> dict[str, Any] | None: ...
    async def set(self, sid: str, data: dict[str, Any], ttl: int | None = None) -> None: ...
    async def delete(self, sid: str) -> None: ...


# --------------------------------------------------------------------------
# In-memory backend (local dev / fallback)
# --------------------------------------------------------------------------
class InMemorySessionStore:
    """Process-local dict backed store. Sessions are lost on pod restart."""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, Any]] = {}

    async def get(self, sid: str) -> dict[str, Any] | None:
        return self._data.get(sid)

    async def set(self, sid: str, data: dict[str, Any], ttl: int | None = None) -> None:
        # ttl is ignored for the in-memory backend; callers already prune on logout.
        self._data[sid] = data

    async def delete(self, sid: str) -> None:
        self._data.pop(sid, None)


# --------------------------------------------------------------------------
# Redis backend (production / multi-pod)
# --------------------------------------------------------------------------
class RedisSessionStore:
    """Redis-backed session store using ``redis.asyncio`` + pickle.

    Keys are namespaced under ``aigw-chat:sid:<sid>`` so multiple apps can
    safely share a Redis instance.
    """

    def __init__(self, url: str) -> None:
        # Import lazily so environments without the redis package can still
        # boot with the in-memory backend.
        import redis.asyncio as redis  # noqa: WPS433 (intentional lazy import)

        self._r = redis.from_url(url, decode_responses=False)
        self._prefix = "aigw-chat:sid:"
        # Redact credentials in logs (redis://user:pass@host/db -> host/db)
        safe = url.split("@")[-1] if "@" in url else url
        log.info("RedisSessionStore initialised url=%s", safe)

    def _key(self, sid: str) -> str:
        return self._prefix + sid

    async def get(self, sid: str) -> dict[str, Any] | None:
        raw = await self._r.get(self._key(sid))
        if raw is None:
            return None
        try:
            return pickle.loads(raw)
        except Exception as e:  # noqa: BLE001 - we log + treat as cache miss
            log.warning("session pickle decode failed sid=%s: %s", sid, e)
            return None

    async def set(self, sid: str, data: dict[str, Any], ttl: int | None = None) -> None:
        payload = pickle.dumps(data)
        if ttl and ttl > 0:
            await self._r.setex(self._key(sid), ttl, payload)
        else:
            await self._r.set(self._key(sid), payload)

    async def delete(self, sid: str) -> None:
        await self._r.delete(self._key(sid))


# --------------------------------------------------------------------------
# Factory
# --------------------------------------------------------------------------
def make_store() -> SessionStore:
    """Return a Redis-backed store if REDIS_URL is set, else an in-memory one."""
    url = os.environ.get("REDIS_URL")
    if url:
        return RedisSessionStore(url)
    log.info(
        "REDIS_URL not set -> using InMemorySessionStore "
        "(sessions will be lost on pod restart / scale-up)",
    )
    return InMemorySessionStore()
