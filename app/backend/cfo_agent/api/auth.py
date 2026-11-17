"""Sign-in endpoints: who am I, local email+password, OIDC single sign-on, sign out."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from cfo_agent.audit import record_event
from cfo_agent.auth import (
    SESSION_COOKIE,
    User,
    cookie_kwargs,
    current_user,
    load_local_users,
    make_session_cookie,
    role_from_claims,
    throttle,
)
from cfo_agent.security import verify_password
from cfo_agent.settings import get_settings

router = APIRouter()
logger = logging.getLogger(__name__)

_oauth = None


def _oidc_client():
    global _oauth
    s = get_settings()
    if s.auth_mode != "oidc":
        raise HTTPException(404, detail="Single sign-on is not enabled")
    if _oauth is None:
        from authlib.integrations.starlette_client import OAuth

        _oauth = OAuth()
        _oauth.register(
            "oidc",
            client_id=s.oidc_client_id,
            client_secret=s.oidc_client_secret,
            server_metadata_url=s.oidc_issuer.rstrip("/") + "/.well-known/openid-configuration",
            client_kwargs={"scope": s.oidc_scopes, "code_challenge_method": "S256"},
        )
    return _oauth.oidc


def _signed_in(user: User, response) -> None:
    response.set_cookie(SESSION_COOKIE, make_session_cookie(user), **cookie_kwargs())


@router.get("/auth/me")
def me(request: Request):
    s = get_settings()
    user = current_user(request)
    return {
        "auth_mode": s.auth_mode,
        "demo_mode": s.demo_mode,
        "user": None if user is None else {"email": user.email, "name": user.name, "role": user.role},
    }


class LoginBody(BaseModel):
    email: str = Field(min_length=3, max_length=256)
    password: str = Field(min_length=1, max_length=256)


@router.post("/auth/login")
def login(body: LoginBody, request: Request):
    if get_settings().auth_mode != "local":
        raise HTTPException(404, detail="Password sign-in is not enabled")
    email = body.email.strip().lower()
    ip = request.client.host if request.client else "?"
    key = f"{email}|{ip}"
    if throttle.blocked(key):
        raise HTTPException(429, detail="Too many failed attempts. Try again in 15 minutes.")
    u = load_local_users().get(email)
    if not u or not verify_password(body.password, u["password_hash"]):
        throttle.fail(key)
        record_event(request, action="sign_in_failed", status=401, user=User(email, email, "-"))
        raise HTTPException(401, detail="Email or password is incorrect")
    throttle.clear(key)
    user = User(email=email, name=u.get("name") or email, role=u["role"])
    resp = JSONResponse({"user": {"email": user.email, "name": user.name, "role": user.role}})
    _signed_in(user, resp)
    record_event(request, action="sign_in", status=200, user=user)
    return resp


@router.post("/auth/logout")
def logout(request: Request):
    user = current_user(request)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    if user:
        record_event(request, action="sign_out", status=200, user=user)
    return resp


@router.get("/auth/oidc/login")
async def oidc_login(request: Request):
    client = _oidc_client()
    redirect_uri = get_settings().public_base_url.rstrip("/") + "/api/auth/oidc/callback"
    return await client.authorize_redirect(request, redirect_uri)


@router.get("/auth/oidc/callback")
async def oidc_callback(request: Request):
    client = _oidc_client()
    try:
        token = await client.authorize_access_token(request)
    except Exception as e:  # noqa: BLE001 — state mismatch, denied consent, bad token…
        logger.warning("OIDC callback failed: %s", e)
        return RedirectResponse("/?signin=failed", status_code=302)
    claims = dict(token.get("userinfo") or {})
    email = str(claims.get("email") or claims.get("preferred_username") or claims.get("sub") or "").lower()
    if not email:
        return RedirectResponse("/?signin=failed", status_code=302)
    user = User(email=email, name=str(claims.get("name") or email), role=role_from_claims(claims))
    resp = RedirectResponse("/", status_code=302)
    _signed_in(user, resp)
    record_event(request, action="sign_in", status=200, user=user)
    return resp
