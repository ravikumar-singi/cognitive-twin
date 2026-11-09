"""Sign-in routes: institution SSO (OIDC), plus personas in development only."""

from __future__ import annotations

import re
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..auth.oidc import OIDCError, identity_from_claims, pkce_pair
from ..auth.provision import DEV_ISSUER, DEV_PERSONAS, upsert_user
from ..auth.tokens import TokenError, new_session, short_lived, unsign
from ..db.models import User
from ..deps import DB, SESSION_COOKIE, AppState, CommitRoute, ctx, current_user, state
from ..learning import audit

router = APIRouter(prefix="/auth", tags=["auth"], route_class=CommitRoute)
LOADTEST_PERSONA = re.compile(r"loadtest-\d{1,6}")
FLOW_COOKIE = "ct_oidc_flow"


def _set_session(resp, st: AppState, user: User) -> str:
    token, csrf = new_session(user.id, st.settings.session_secret, st.settings.session_ttl_minutes)
    resp.set_cookie(SESSION_COOKIE, token, max_age=st.settings.session_ttl_minutes * 60,
                    httponly=True, secure=not st.settings.is_dev, samesite="lax", path="/")
    return csrf


@router.get("/config")
def auth_config(request: Request) -> dict:
    st = state(request)
    return {"sso": st.oidc is not None, "dev_login": st.settings.dev_login_enabled,
            "personas": [{"id": k, "name": v["name"]} for k, v in DEV_PERSONAS.items()]
            if st.settings.dev_login_enabled else [],
            "demo": st.settings.demo_enabled}


@router.get("/session")
def session_info(request: Request, db: Session = DB) -> dict:
    """Who is signed in, if anyone. Never 401, so the app can check quietly."""
    st = state(request)
    try:
        payload = unsign(request.cookies.get(SESSION_COOKIE, ""), st.settings.session_secret)
    except TokenError:
        return {"user": None, "csrf": None}
    user = db.get(User, payload.get("uid"))
    if user is None:
        return {"user": None, "csrf": None}
    return {"user": {"id": user.id, "name": user.display_name, "role": user.role},
            "csrf": payload.get("csrf")}


@router.get("/login")
def login(request: Request) -> RedirectResponse:
    st = state(request)
    if st.oidc is None:
        raise HTTPException(404, "single sign-on is not configured")
    verifier, challenge = pkce_pair()
    flow = {"state": secrets.token_urlsafe(24), "nonce": secrets.token_urlsafe(24),
            "verifier": verifier}
    redirect_uri = f"{st.settings.public_base_url}/auth/callback"
    try:
        url = st.oidc.authorize_url(redirect_uri, flow["state"], flow["nonce"], challenge)
    except OIDCError:
        raise HTTPException(503, "the sign-in service is unavailable; try again shortly") from None
    resp = RedirectResponse(url, status_code=302)
    resp.set_cookie(FLOW_COOKIE, short_lived(flow, st.settings.session_secret), max_age=600,
                    httponly=True, secure=not st.settings.is_dev, samesite="lax",
                    path="/auth/callback")
    return resp


@router.get("/callback")
def callback(request: Request, code: str = "",
             db: Session = DB) -> RedirectResponse:
    st = state(request)
    if st.oidc is None:
        raise HTTPException(404, "single sign-on is not configured")
    returned_state = request.query_params.get("state", "")
    try:
        flow = unsign(request.cookies.get(FLOW_COOKIE, ""), st.settings.session_secret)
    except TokenError:
        raise HTTPException(400, "sign-in expired; please start again") from None
    if not code or not secrets.compare_digest(returned_state, flow["state"]):
        raise HTTPException(400, "sign-in could not be verified; please start again")
    redirect_uri = f"{st.settings.public_base_url}/auth/callback"
    try:
        tokens = st.oidc.exchange(code, redirect_uri, flow["verifier"])
        claims = st.oidc.validate(tokens["id_token"], st.settings.oidc_client_id,
                                  nonce=flow["nonce"])
        ident = identity_from_claims(claims, st.settings)
    except (OIDCError, KeyError) as e:
        audit(db, ctx(request), "auth.login", None, outcome="failed", reason=str(e)[:200])
        raise HTTPException(401, "sign-in failed") from None
    user = upsert_user(db, ident)
    request.state.user = user
    audit(db, ctx(request), "auth.login", user.id)
    resp = RedirectResponse("/", status_code=302)
    _set_session(resp, st, user)
    resp.delete_cookie(FLOW_COOKIE, path="/auth/callback")
    return resp


class DevLogin(BaseModel):
    persona: str


@router.post("/dev-login")
def dev_login(body: DevLogin, request: Request, db: Session = DB):
    st = state(request)
    if not st.settings.dev_login_enabled:
        raise HTTPException(404, "not found")
    p = DEV_PERSONAS.get(body.persona)
    if p is None and LOADTEST_PERSONA.fullmatch(body.persona):
        p = {"name": f"Load {body.persona[9:]}", "role": "learner", "ct_adult_verified": True,
             "ct_region": "IN", "ct_cohorts": ["Load test"], "ct_institution": "Load test"}
    if p is None:
        raise HTTPException(404, "unknown persona")
    claims = {"iss": DEV_ISSUER, "sub": body.persona, "email": f"{body.persona}@example.edu",
              "name": p["name"], "ct_role": p["role"], **{k: v for k, v in p.items()
                                                          if k not in ("name", "role")}}
    user = upsert_user(db, identity_from_claims(claims, st.settings))
    request.state.user = user
    audit(db, ctx(request), "auth.dev_login", user.id)
    resp = JSONResponse({"ok": True})
    csrf = _set_session(resp, st, user)
    resp.headers["X-CSRF-Token"] = csrf
    return resp


@router.post("/logout")
def logout(request: Request, user: User = Depends(current_user),
           db: Session = DB):
    audit(db, ctx(request), "auth.logout", user.id)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp
