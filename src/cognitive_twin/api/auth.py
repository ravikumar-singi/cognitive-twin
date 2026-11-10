"""Sign-in routes: institution SSO (OIDC), plus personas in development only."""

from __future__ import annotations

import re
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..auth.oidc import OIDCError, identity_from_claims, pkce_pair
from ..auth.provision import DEV_ISSUER, DEV_PERSONAS, upsert_user
from ..auth.tokens import TokenError, new_session, short_lived, unsign
from ..db.models import LocalCredential, School, User
from ..deps import DB, SESSION_COOKIE, AppState, CommitRoute, ctx, current_user, state
from ..learning import audit
from ..schools import AccountError, accept_invite, authenticate, join_class, set_password

router = APIRouter(prefix="/auth", tags=["auth"], route_class=CommitRoute)
LOADTEST_PERSONA = re.compile(r"loadtest-\d{1,6}")
FLOW_COOKIE = "ct_oidc_flow"


def _set_session(resp, st: AppState, user: User, db: Session | None = None) -> str:
    cred = db.get(LocalCredential, user.id) if db is not None else None
    token, csrf = new_session(user.id, st.settings.session_secret,
                              st.settings.session_ttl_minutes,
                              epoch=cred.session_epoch if cred else 0)
    resp.set_cookie(SESSION_COOKIE, token, max_age=st.settings.session_ttl_minutes * 60,
                    httponly=True, secure=not st.settings.is_dev, samesite="lax", path="/")
    return csrf


@router.get("/config")
def auth_config(request: Request) -> dict:
    st = state(request)
    return {"sso": st.oidc is not None, "local_accounts": st.settings.local_accounts,
            "dev_login": st.settings.dev_login_enabled,
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
    cred = db.get(LocalCredential, user.id)
    if cred is not None and payload.get("se", 0) != cred.session_epoch:
        return {"user": None, "csrf": None}
    school = db.get(School, user.school_id) if user.school_id else None
    return {"user": {"id": user.id, "name": user.display_name, "role": user.role,
                     "age_band": user.age_band, "school": school.name if school else None,
                     "username": cred.username if cred else None,
                     "must_change_password": bool(cred and cred.must_change)},
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
        db.commit()  # keep the audit record (and lockout count) despite the error
        raise HTTPException(401, "sign-in failed") from None
    user = upsert_user(db, ident)
    request.state.user = user
    audit(db, ctx(request), "auth.login", user.id)
    resp = RedirectResponse("/", status_code=302)
    _set_session(resp, st, user)
    resp.delete_cookie(FLOW_COOKIE, path="/auth/callback")
    return resp


# --- class codes and usernames (schools without single sign-on) ---------------------

def _local_enabled(st: AppState) -> None:
    if not st.settings.local_accounts:
        raise HTTPException(404, "sign-in with a username is not enabled here")


def _signed_in(st: AppState, user: User, db: Session, **extra) -> JSONResponse:
    resp = JSONResponse({"ok": True, **extra})
    resp.headers["X-CSRF-Token"] = _set_session(resp, st, user, db)
    return resp


class LocalLogin(BaseModel):
    username: str = Field(min_length=1, max_length=40)
    password: str = Field(min_length=1, max_length=200)


@router.post("/local-login")
def local_login(body: LocalLogin, request: Request, db: Session = DB):
    st = state(request)
    _local_enabled(st)
    user = authenticate(db, body.username, body.password)
    if user is None:
        audit(db, ctx(request), "auth.local_login", None, outcome="failed",
              username=body.username.strip().lower()[:40])
        db.commit()  # keep the audit record (and lockout count) despite the error
        # same answer for a wrong name, a wrong password and a locked account
        raise HTTPException(401, "That username and password don't match. After 5 tries the "
                                 "account pauses for 15 minutes; your teacher can reset it.")
    request.state.user = user
    audit(db, ctx(request), "auth.local_login", user.id)
    return _signed_in(st, user, db)


class JoinBody(BaseModel):
    code: str = Field(min_length=4, max_length=32)
    first_name: str = Field(min_length=1, max_length=40)
    password: str = Field(min_length=1, max_length=200)


@router.post("/join")
def join(body: JoinBody, request: Request, db: Session = DB):
    """A student joins a class with its code. Their teacher approves them before they start."""
    st = state(request)
    _local_enabled(st)
    try:
        user, username, cohort = join_class(db, body.code, body.first_name, body.password)
    except AccountError as e:
        audit(db, ctx(request), "auth.join", None, outcome="failed", reason=str(e)[:120])
        db.commit()  # keep the audit record (and lockout count) despite the error
        raise HTTPException(400, str(e)) from None
    request.state.user = user
    audit(db, ctx(request), "auth.join", user.id, cohort=cohort.id)
    return _signed_in(st, user, db, username=username, class_name=cohort.name)


class InviteBody(BaseModel):
    code: str = Field(min_length=4, max_length=32)
    name: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=200)


@router.post("/invite")
def use_invite(body: InviteBody, request: Request, db: Session = DB):
    """A teacher or school administrator creates their account from an invitation code."""
    st = state(request)
    _local_enabled(st)
    try:
        user, username = accept_invite(db, body.code, body.name, body.password)
    except AccountError as e:
        audit(db, ctx(request), "auth.invite", None, outcome="failed", reason=str(e)[:120])
        db.commit()  # keep the audit record (and lockout count) despite the error
        raise HTTPException(400, str(e)) from None
    request.state.user = user
    audit(db, ctx(request), "auth.invite", user.id, role=user.role)
    return _signed_in(st, user, db, username=username)


class PasswordBody(BaseModel):
    current: str = Field(default="", max_length=200)
    new: str = Field(min_length=1, max_length=200)


@router.post("/password")
def change_password(body: PasswordBody, request: Request, user: User = Depends(current_user),
                    db: Session = DB):
    st = state(request)
    cred = db.get(LocalCredential, user.id)
    if cred is None:
        raise HTTPException(400, "You sign in through your school's own sign-in page.")
    if not cred.must_change and authenticate(db, cred.username, body.current) is None:
        raise HTTPException(400, "Your current password isn't right.")
    try:
        set_password(db, user, body.new, must_change=False)
    except AccountError as e:
        raise HTTPException(400, str(e)) from None
    audit(db, ctx(request), "auth.password_change", user.id)
    return _signed_in(st, user, db)


class DevLogin(BaseModel):
    persona: str


@router.post("/dev-login")
def dev_login(body: DevLogin, request: Request, db: Session = DB):
    st = state(request)
    if not st.settings.dev_login_enabled:
        raise HTTPException(404, "not found")
    p = DEV_PERSONAS.get(body.persona)
    if p is None and LOADTEST_PERSONA.fullmatch(body.persona):
        p = {"name": f"Load {body.persona[9:]}", "role": "learner", "ct_age_band": "adult",
             "ct_region": "IN", "ct_cohorts": ["Load test"], "ct_institution": "Load test"}
    if p is None:
        raise HTTPException(404, "unknown persona")
    claims = {"iss": DEV_ISSUER, "sub": body.persona, "email": f"{body.persona}@example.edu",
              "name": p["name"], "ct_role": p["role"], **{k: v for k, v in p.items()
                                                          if k not in ("name", "role")}}
    user = upsert_user(db, identity_from_claims(claims, st.settings))
    request.state.user = user
    audit(db, ctx(request), "auth.dev_login", user.id)
    return _signed_in(st, user, db)


@router.post("/logout")
def logout(request: Request, user: User = Depends(current_user),
           db: Session = DB):
    audit(db, ctx(request), "auth.logout", user.id)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp
