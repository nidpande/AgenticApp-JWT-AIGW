"""OIDC BFF (Backend-For-Frontend) integration with Keycloak.

Flow (per RFC 6749 auth-code + PKCE-S256 + OIDC nonce):

    1. Browser hits GET  /api/auth/login
       - Server generates state, nonce, code_verifier + code_challenge (S256).
       - Stores state -> {nonce, code_verifier, return_to} in a short-lived
         signed cookie (`oidc_state`).
       - 302 to Keycloak /authorize with client_id, redirect_uri,
         scope=openid aigw-api, response_type=code, state, nonce,
         code_challenge, code_challenge_method=S256.

    2. Keycloak authenticates the user, 302 to redirect_uri with ?code&state.

    3. Server hits GET /api/auth/callback
       - Verifies the state matches the `oidc_state` cookie.
       - POSTs to Keycloak /token: grant_type=authorization_code,
         code, client_id, client_secret, redirect_uri, code_verifier.
       - Validates the returned id_token (iss, aud, nonce, sig, exp).
       - Extracts sub/email/preferred_username/groups from access_token
         (or id_token) claims.
       - Creates a ChatSession, stores in `main._sessions[sid]`, sets the
         same signed `aigw_chat_session` cookie the existing password
         login already uses (so /api/chat, /api/me work unchanged).
       - Deletes the transient `oidc_state` cookie, 302 to `/`.

    4. POST /api/auth/logout
       - Removes ChatSession + `aigw_chat_session` cookie.
       - 302 to Keycloak end_session_endpoint (with post_logout_redirect_uri
         and id_token_hint) so Keycloak clears its own SSO cookie.

Design notes:
    * PKCE is mandatory (Keycloak client `aigw-chat` is configured with
      pkce.code.challenge.method=S256).
    * Nonce validation defends against ID-token replay across sessions.
    * JWT signature validation uses authlib's JsonWebKey against the JWKS
      fetched from the OIDC discovery document. JWKS is cached with a soft
      TTL (5 min) - we do NOT re-fetch on every request.
    * The BFF NEVER sends tokens to the browser. Only a signed opaque
      cookie (session-id) leaves the server. Tokens sit in
      `main._sessions[sid].access_token`.
    * If OIDC_ISSUER env is unset, all /api/auth/* routes return 501
      "OIDC not configured" so legacy /api/login (password) keeps working.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
import time
import uuid
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from itsdangerous import URLSafeSerializer, BadSignature

log = logging.getLogger("agent_api.oidc")

# --------------------------------------------------------------------------
# Config from env
# --------------------------------------------------------------------------
OIDC_ISSUER        = os.environ.get("OIDC_ISSUER", "").rstrip("/")
OIDC_CLIENT_ID     = os.environ.get("OIDC_CLIENT_ID", "")
OIDC_CLIENT_SECRET = os.environ.get("OIDC_CLIENT_SECRET", "")
OIDC_REDIRECT_URI  = os.environ.get("OIDC_REDIRECT_URI", "")
OIDC_SCOPES        = os.environ.get("OIDC_SCOPES", "openid aigw-api")
OIDC_POST_LOGOUT   = os.environ.get("OIDC_POST_LOGOUT_REDIRECT_URI", "")

# Optional overrides so we can hit Keycloak via cluster-internal DNS while
# `iss` and the browser-facing endpoints stay on the public hostname
# (see Phase-0 dual-hostname strategy). If OIDC_DISCOVERY_URL is set, we skip
# the .well-known fetch entirely; otherwise we fetch discovery from OIDC_ISSUER.
OIDC_DISCOVERY_URL     = os.environ.get("OIDC_DISCOVERY_URL", "")
OIDC_JWKS_URL_OVERRIDE = os.environ.get("OIDC_JWKS_URL", "")

# Signed cookie for the transient OAuth `state` bundle (5-min lifetime).
_STATE_COOKIE_NAME = "oidc_state"
_STATE_COOKIE_TTL  = 300  # seconds

# Reuse the same secret as the main session cookie for simplicity; we salt
# differently so signatures don't collide.
_STATE_SECRET = os.environ.get("CHAT_SESSION_SECRET", "change-me-please")
_state_ser    = URLSafeSerializer(_STATE_SECRET, salt="aigw-oidc-state")


def is_enabled() -> bool:
    """True iff all required OIDC env vars are set."""
    return bool(
        OIDC_ISSUER and OIDC_CLIENT_ID and OIDC_CLIENT_SECRET and OIDC_REDIRECT_URI
    )


# --------------------------------------------------------------------------
# OIDC discovery + JWKS cache (soft TTL)
# --------------------------------------------------------------------------
_disc_cache: dict[str, Any] = {}   # {'doc': {...}, 'fetched_at': float}
_jwks_cache: dict[str, Any] = {}   # {'keys': [...], 'fetched_at': float}
_CACHE_TTL = 300.0                  # 5 minutes


async def _get_discovery() -> dict[str, Any]:
    """Fetch and cache the OIDC discovery document.

    Uses OIDC_DISCOVERY_URL if set (typically cluster-internal svc DNS so the
    backend doesn't need to route through an ingress). Falls back to
    <OIDC_ISSUER>/.well-known/openid-configuration.
    """
    now = time.time()
    if _disc_cache and now - _disc_cache.get("fetched_at", 0) < _CACHE_TTL:
        return _disc_cache["doc"]
    url = OIDC_DISCOVERY_URL or f"{OIDC_ISSUER}/.well-known/openid-configuration"
    async with httpx.AsyncClient(timeout=5.0) as cli:
        r = await cli.get(url)
        r.raise_for_status()
        doc = r.json()
    _disc_cache["doc"]        = doc
    _disc_cache["fetched_at"] = now
    log.info("OIDC discovery loaded from %s (issuer=%s)", url, doc.get("issuer"))
    return doc


async def _get_jwks() -> dict[str, Any]:
    """Fetch and cache the JWKS (list of JWK dicts)."""
    now = time.time()
    if _jwks_cache and now - _jwks_cache.get("fetched_at", 0) < _CACHE_TTL:
        return _jwks_cache["keys"]
    if OIDC_JWKS_URL_OVERRIDE:
        jwks_url = OIDC_JWKS_URL_OVERRIDE
    else:
        disc = await _get_discovery()
        jwks_url = disc["jwks_uri"]
    async with httpx.AsyncClient(timeout=5.0) as cli:
        r = await cli.get(jwks_url)
        r.raise_for_status()
        keys = r.json()
    _jwks_cache["keys"]       = keys
    _jwks_cache["fetched_at"] = now
    log.info("JWKS loaded from %s (%d keys)", jwks_url, len(keys.get("keys", [])))
    return keys


# --------------------------------------------------------------------------
# PKCE helpers
# --------------------------------------------------------------------------
def _pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) using RFC 7636 S256."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode("ascii")
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    return verifier, challenge


# --------------------------------------------------------------------------
# JWT validation (authlib does the crypto lifting; we do the claim checks)
# --------------------------------------------------------------------------
def _validate_id_token(id_token: str, nonce: str, jwks: dict[str, Any]) -> dict[str, Any]:
    """Verify signature (via JWKS) and validate iss/aud/nonce/exp claims."""
    from authlib.jose import jwt as jose_jwt
    from authlib.jose.errors import JoseError

    # authlib picks the right JWK from the JWKS using the token's kid.
    try:
        claims = jose_jwt.decode(id_token, jwks)
        # Enforce standard OIDC claims manually so we control the error msg.
        claims_options = {
            "iss": {"essential": True, "value": OIDC_ISSUER},
            "aud": {"essential": True, "value": OIDC_CLIENT_ID},
            "exp": {"essential": True},
            "iat": {"essential": True},
        }
        claims.options = claims_options
        claims.validate(now=int(time.time()), leeway=30)
    except JoseError as e:
        log.warning("id_token validation failed: %s", e)
        raise HTTPException(status_code=401, detail=f"invalid id_token: {e}")

    if claims.get("nonce") != nonce:
        log.warning("nonce mismatch: expected=%s got=%s", nonce, claims.get("nonce"))
        raise HTTPException(status_code=401, detail="nonce mismatch")

    return dict(claims)


def _decode_access_token_claims(access_token: str, jwks: dict[str, Any]) -> dict[str, Any]:
    """Decode the access_token (JWT). Keycloak issues access tokens as
    JWTs with aud=aigw-api. We validate iss+sig+exp but NOT aud (that's
    AIGW's job when it receives the token)."""
    from authlib.jose import jwt as jose_jwt
    from authlib.jose.errors import JoseError

    try:
        claims = jose_jwt.decode(access_token, jwks)
        claims.options = {
            "iss": {"essential": True, "value": OIDC_ISSUER},
            "exp": {"essential": True},
        }
        claims.validate(now=int(time.time()), leeway=30)
    except JoseError as e:
        log.warning("access_token validation failed: %s", e)
        raise HTTPException(status_code=401, detail=f"invalid access_token: {e}")
    return dict(claims)


# --------------------------------------------------------------------------
# State cookie helpers
# --------------------------------------------------------------------------
def _pack_state(payload: dict[str, Any]) -> str:
    return _state_ser.dumps(payload)


def _unpack_state(token: str) -> dict[str, Any] | None:
    try:
        return _state_ser.loads(token)
    except BadSignature:
        return None


# --------------------------------------------------------------------------
# FastAPI router
# --------------------------------------------------------------------------
router = APIRouter(prefix="/api/auth", tags=["oidc"])


@router.get("/login")
async def auth_login(request: Request) -> RedirectResponse:
    """Kick off the OIDC authorization code flow.

    Query params:
        return_to: (optional) relative URL to redirect to after login. Defaults to '/'.
    """
    if not is_enabled():
        raise HTTPException(status_code=501, detail="OIDC not configured on this server")

    return_to = request.query_params.get("return_to", "/")
    if not return_to.startswith("/"):  # only allow same-origin redirects
        return_to = "/"

    disc = await _get_discovery()

    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    verifier, challenge = _pkce_pair()

    # Bundle everything needed by the callback into a short-lived signed cookie.
    state_bundle = {
        "state":         state,
        "nonce":         nonce,
        "code_verifier": verifier,
        "return_to":     return_to,
        "ts":            int(time.time()),
    }
    state_cookie = _pack_state(state_bundle)

    params = {
        "client_id":             OIDC_CLIENT_ID,
        "redirect_uri":          OIDC_REDIRECT_URI,
        "response_type":         "code",
        "scope":                 OIDC_SCOPES,
        "state":                 state,
        "nonce":                 nonce,
        "code_challenge":        challenge,
        "code_challenge_method": "S256",
    }
    authz_url = f"{disc['authorization_endpoint']}?{urlencode(params)}"

    resp = RedirectResponse(url=authz_url, status_code=302)
    resp.set_cookie(
        _STATE_COOKIE_NAME, state_cookie,
        max_age=_STATE_COOKIE_TTL, httponly=True, samesite="lax", path="/",
    )
    log.info("OIDC login initiated state=%s return_to=%s", state, return_to)
    return resp


@router.get("/callback")
async def auth_callback(request: Request) -> RedirectResponse:
    """Handle Keycloak's redirect back with ?code&state."""
    if not is_enabled():
        raise HTTPException(status_code=501, detail="OIDC not configured on this server")

    err = request.query_params.get("error")
    if err:
        desc = request.query_params.get("error_description", "")
        log.warning("OIDC callback error: %s %s", err, desc)
        raise HTTPException(status_code=400, detail=f"OIDC error: {err} {desc}")

    code  = request.query_params.get("code")
    state = request.query_params.get("state")
    if not code or not state:
        raise HTTPException(status_code=400, detail="missing code or state")

    # 1. Verify state matches the cookie we set at login-start.
    state_cookie = request.cookies.get(_STATE_COOKIE_NAME)
    if not state_cookie:
        raise HTTPException(status_code=400, detail="missing state cookie (session expired?)")
    bundle = _unpack_state(state_cookie)
    if not bundle or bundle.get("state") != state:
        log.warning("state mismatch: cookie=%s query=%s", bundle, state)
        raise HTTPException(status_code=400, detail="state mismatch")
    if int(time.time()) - bundle.get("ts", 0) > _STATE_COOKIE_TTL:
        raise HTTPException(status_code=400, detail="state cookie expired")

    disc = await _get_discovery()

    # 2. Exchange the code for tokens.
    async with httpx.AsyncClient(timeout=8.0) as cli:
        r = await cli.post(
            disc["token_endpoint"],
            data={
                "grant_type":    "authorization_code",
                "code":          code,
                "redirect_uri":  OIDC_REDIRECT_URI,
                "client_id":     OIDC_CLIENT_ID,
                "client_secret": OIDC_CLIENT_SECRET,
                "code_verifier": bundle["code_verifier"],
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    if r.status_code != 200:
        log.warning("token endpoint returned %d: %s", r.status_code, r.text[:400])
        raise HTTPException(status_code=401, detail=f"token exchange failed: {r.status_code}")

    tokens = r.json()
    id_token      = tokens.get("id_token")
    access_token  = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")
    expires_in    = int(tokens.get("expires_in", 0))
    if not id_token or not access_token:
        raise HTTPException(status_code=401, detail="token response missing id_token or access_token")

    # 3. Validate ID token (iss+aud+nonce+sig+exp) and derive claims.
    jwks       = await _get_jwks()
    id_claims  = _validate_id_token(id_token, bundle["nonce"], jwks)
    at_claims  = _decode_access_token_claims(access_token, jwks)

    # Prefer claims from access_token (it carries `groups` and `aud=aigw-api`);
    # fall back to id_token for user profile fields Keycloak only puts in ID.
    sub                = at_claims.get("sub") or id_claims.get("sub", "")
    email              = at_claims.get("email") or id_claims.get("email")
    preferred_username = (
        at_claims.get("preferred_username")
        or id_claims.get("preferred_username")
        or sub
    )
    groups             = at_claims.get("groups", []) or id_claims.get("groups", [])

    # 4. Create the ChatSession + set the *same* signed cookie the legacy
    # password flow uses, so /api/chat, /api/me keep working unchanged.
    # We import lazily to avoid a circular import at module-load time.
    from . import main as main_mod
    from .agent_runner import ChatSession

    sid = uuid.uuid4().hex
    chat_session = ChatSession(
        username           = preferred_username,
        email              = email,
        preferred_username = preferred_username,
        groups             = list(groups) if isinstance(groups, list) else [str(groups)],
        access_token       = access_token,
        refresh_token      = refresh_token,
        access_token_exp   = int(time.time()) + expires_in if expires_in else None,
        # Retain id_token for use as id_token_hint on logout -> Keycloak
        # end_session_endpoint. Without this, Keycloak's SSO cookie survives
        # and clicking "Sign in with Keycloak" silently replays the identity.
        id_token           = id_token,
    )
    # Phase 4: persist the session in the shared store (Redis in prod, in-memory
    # locally). Store the primitive snapshot only; LLM/agent are rebuilt on
    # every /api/chat call from access_token.
    await main_mod._persist_session(sid, chat_session)

    return_to = bundle.get("return_to", "/")
    resp = RedirectResponse(url=return_to, status_code=302)
    resp.set_cookie(
        main_mod.COOKIE_NAME,
        main_mod._sign(sid, preferred_username),
        httponly=True, samesite="lax", max_age=60 * 60 * 8, path="/",
    )
    resp.delete_cookie(_STATE_COOKIE_NAME, path="/")

    log.info(
        "OIDC login SUCCESS sub=%s user=%s email=%s groups=%s -> %s",
        sub, preferred_username, email, groups, return_to,
    )
    return resp


@router.api_route("/logout", methods=["GET", "POST"])
@router.api_route("/logout-redirect", methods=["GET"])
async def auth_logout(request: Request) -> RedirectResponse:
    """Clear the local session and 302 to Keycloak's end-session endpoint.

    Exposed on both GET and POST because the SPA needs GET for full-page
    ``window.location.href`` navigation (so the browser actually follows
    the 302 to Keycloak; a fetch() would follow it silently and the
    Set-Cookie clearing KEYCLOAK_IDENTITY would never reach the browser).
    ``/logout-redirect`` is an alias kept for clarity in the SPA code.
    """
    if not is_enabled():
        raise HTTPException(status_code=501, detail="OIDC not configured on this server")

    from . import main as main_mod

    tok  = request.cookies.get(main_mod.COOKIE_NAME)
    data = main_mod._verify(tok) if tok else None
    session_user = "?"
    id_token_hint: str | None = None
    if data:
        sid = data.get("sid", "")
        if sid:
            # Peek at the snapshot to grab the id_token_hint + log the user,
            # then delete the session from the store.
            snap = await main_mod.store.get(sid)
            if snap:
                session_user = snap.get("preferred_username") or snap.get("username") or "?"
                id_token_hint = snap.get("id_token")
            await main_mod.store.delete(sid)

    disc = await _get_discovery()
    logout_url = disc.get("end_session_endpoint", "")
    if logout_url:
        params: dict[str, str] = {}
        # ``id_token_hint`` is what actually kills Keycloak's SSO cookie
        # silently (no confirmation page, no cookie survives). Without it
        # Keycloak keeps the KEYCLOAK_IDENTITY cookie alive and the next
        # click on "Sign in with Keycloak" replays the same identity.
        if id_token_hint:
            params["id_token_hint"] = id_token_hint
        if OIDC_POST_LOGOUT:
            params["post_logout_redirect_uri"] = OIDC_POST_LOGOUT
            # client_id is only required when id_token_hint is absent, but
            # Keycloak accepts it either way, so we send it for robustness.
            params["client_id"] = OIDC_CLIENT_ID
        if params:
            logout_url = f"{logout_url}?{urlencode(params)}"

    target = logout_url or (OIDC_POST_LOGOUT or "/")
    resp = RedirectResponse(url=target, status_code=302)
    resp.delete_cookie(main_mod.COOKIE_NAME, path="/")
    log.info(
        "OIDC logout user=%s id_token_hint=%s -> %s",
        session_user, "yes" if id_token_hint else "no", target,
    )
    return resp


# --------------------------------------------------------------------------
# Silent refresh helper (used by /api/chat before every LLM call in Phase 3)
# --------------------------------------------------------------------------
async def refresh_if_needed(session: Any) -> None:
    """If the access_token is < 30 s from expiry, POST refresh_token to KC.

    Mutates session.access_token, .refresh_token, .access_token_exp in-place.
    No-op if session has no refresh_token or OIDC is disabled.
    """
    if not is_enabled():
        return
    exp = getattr(session, "access_token_exp", None)
    rt  = getattr(session, "refresh_token", None)
    if not exp or not rt:
        return
    if exp - int(time.time()) > 30:
        return  # still fresh

    disc = await _get_discovery()
    async with httpx.AsyncClient(timeout=8.0) as cli:
        r = await cli.post(
            disc["token_endpoint"],
            data={
                "grant_type":    "refresh_token",
                "refresh_token": rt,
                "client_id":     OIDC_CLIENT_ID,
                "client_secret": OIDC_CLIENT_SECRET,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    if r.status_code != 200:
        log.warning("token refresh failed %d: %s", r.status_code, r.text[:300])
        # Do NOT raise here - let the downstream Bearer call return 401,
        # SPA will then redirect to /api/auth/login.
        return

    tk = r.json()
    session.access_token     = tk.get("access_token", session.access_token)
    session.refresh_token    = tk.get("refresh_token", session.refresh_token)
    session.access_token_exp = int(time.time()) + int(tk.get("expires_in", 0))
    log.info("access_token silently refreshed for user=%s", getattr(session, "preferred_username", "?"))
