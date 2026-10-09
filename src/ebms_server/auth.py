"""Google·Discord 웹 로그인, 웹 세션 쿠키, 내 계정 페이지, 클라이언트 로그인.

OAuth 진행 중에만 필요한 state·PKCE verifier는 DB 대신 서명된 단기 쿠키에 둡니다.
로그인이 끝나면 OAuth 토큰은 버리고 user 세션만 발급합니다.

클라이언트는 루프백 리다이렉트 + PKCE(RFC 8252)로 1회용 코드를 받아 세션키로 바꾸고,
이후 `Authorization: Bearer <세션키>`로 API를 호출합니다.
"""
import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
import urllib.parse
from dataclasses import dataclass
from typing import Annotated, Callable

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel

from . import accounts, constant, quota
from .accounts import Profile, User
from .templating import templates

logger = logging.getLogger(__name__)

router = APIRouter()

STATE_COOKIE = "ebms_oauth"
_fallback_secret = secrets.token_bytes(32)


@dataclass(frozen=True)
class OAuth:
    name: str
    label: str
    authorize_url: str
    token_url: str
    userinfo_url: str
    scope: str
    pkce: bool
    parse_profile: Callable[[dict], Profile]

    @property
    def client_id(self) -> str:
        return getattr(constant, f"{self.name.upper()}_CLIENT_ID")

    @property
    def client_secret(self) -> str:
        return getattr(constant, f"{self.name.upper()}_CLIENT_SECRET")

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @property
    def redirect_uri(self) -> str:
        return f"{constant.PUBLIC_URL}/auth/{self.name}/callback"


def _google_profile(data: dict) -> Profile:
    return Profile(
        oauth="google",
        oauth_user_id=str(data["sub"]),
        email=data.get("email"),
        email_verified=bool(data.get("email_verified")),
        name=data.get("name") or data.get("email") or "",
    )


def _discord_profile(data: dict) -> Profile:
    return Profile(
        oauth="discord",
        oauth_user_id=str(data["id"]),
        email=data.get("email"),
        email_verified=bool(data.get("verified")),
        name=data.get("global_name") or data.get("username") or "",
    )


OAUTHS = {
    p.name: p
    for p in (
        OAuth(
            name="google",
            label="Google",
            authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
            token_url="https://oauth2.googleapis.com/token",
            userinfo_url="https://openidconnect.googleapis.com/v1/userinfo",
            scope="openid email profile",
            pkce=True,
            parse_profile=_google_profile,
        ),
        OAuth(
            name="discord",
            label="Discord",
            authorize_url="https://discord.com/oauth2/authorize",
            token_url="https://discord.com/api/oauth2/token",
            userinfo_url="https://discord.com/api/users/@me",
            scope="identify email",
            pkce=False,
            parse_profile=_discord_profile,
        ),
    )
}


def configured_oauths() -> list[OAuth]:
    return [p for p in OAUTHS.values() if p.configured]


def get_oauth(name: str) -> OAuth:
    oauth = OAUTHS.get(name)
    if oauth is None or not oauth.configured:
        raise HTTPException(status_code=404, detail="unknown oauth")
    return oauth


def fetch_profile(oauth: OAuth, code: str, code_verifier: str) -> Profile:
    """authorization code를 access token으로 바꾸고 사용자 정보를 가져옵니다."""
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": oauth.redirect_uri,
        "client_id": oauth.client_id,
        "client_secret": oauth.client_secret,
    }
    if oauth.pkce:
        form["code_verifier"] = code_verifier
    with httpx.Client(timeout=10) as http:
        r = http.post(oauth.token_url, data=form, headers={"Accept": "application/json"})
        r.raise_for_status()
        access_token = r.json()["access_token"]
        r = http.get(oauth.userinfo_url, headers={"Authorization": f"Bearer {access_token}"})
        r.raise_for_status()
        return oauth.parse_profile(r.json())


# ---- 서명된 쿠키 ----

def _secret() -> bytes:
    return constant.SECRET_KEY.encode() if constant.SECRET_KEY else _fallback_secret


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign(payload: dict) -> str:
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64(hmac.new(_secret(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def unsign(value: str | None) -> dict | None:
    """서명이 맞고 만료되지 않았으면 payload를, 아니면 None을 반환합니다."""
    if not value or "." not in value:
        return None
    body, sig = value.rsplit(".", 1)
    expected = _b64(hmac.new(_secret(), body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        payload = json.loads(_unb64(body))
    except ValueError:
        return None
    if not isinstance(payload, dict) or payload.get("exp", 0) < time.time():
        return None
    return payload


def _secure_cookie() -> bool:
    return constant.PUBLIC_URL.startswith("https://")


def safe_next(next_url: str | None, default: str = "/account") -> str:
    """로그인 후 이동할 곳. 같은 사이트의 경로만 허용합니다(오픈 리다이렉트 방지)."""
    if next_url and next_url.startswith("/") and not next_url.startswith("//") and "\\" not in next_url:
        return next_url
    return default


# ---- 현재 사용자 ----

def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        constant.SESSION_COOKIE, token,
        max_age=constant.WEB_SESSION_SECONDS, path="/",
        httponly=True, samesite="lax", secure=_secure_cookie(),
    )


def optional_session(request: Request) -> tuple[User, int] | None:
    token = request.cookies.get(constant.SESSION_COOKIE)
    if not token:
        return None
    session = accounts.authenticate(token, "web")
    if session is not None:
        # DB의 만료 시각이 연장되므로 쿠키 유효기간도 응답에서 같이 연장합니다(refresh_session_cookie).
        request.state.web_session_token = token
        # 페이지 상단 메뉴(auth_base.html)가 로그인 상태를 알 수 있게 둡니다.
        request.state.user = session[0]
    return session


async def refresh_session_cookie(request: Request, call_next):
    """로그인한 요청의 응답에 세션 쿠키를 다시 실어 브라우저 쪽 유효기간도 연장합니다.

    응답이 이미 세션 쿠키를 바꾸는 경우(로그인, 로그아웃)는 건드리지 않습니다.
    """
    response = await call_next(request)
    token = getattr(request.state, "web_session_token", None)
    if token and not any(
        v.startswith(f"{constant.SESSION_COOKIE}=") for v in response.headers.getlist("set-cookie")
    ):
        set_session_cookie(response, token)
    return response


def bearer_token(request: Request) -> str | None:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def optional_api_session(request: Request) -> tuple[User, int, str] | None:
    """API 요청의 (user, session id, 세션 종류). 클라이언트는 Bearer 세션키, 웹은 쿠키입니다.

    Authorization 헤더가 있으면 그것만 봅니다(틀린 세션키를 쿠키로 덮지 않음).
    """
    if "authorization" in request.headers:
        token = bearer_token(request)
        session = accounts.authenticate(token, "client") if token else None
        return (*session, "client") if session else None
    session = optional_session(request)
    return (*session, "web") if session else None


def api_session(session: Annotated[tuple[User, int, str] | None, Depends(optional_api_session)]) -> tuple[User, int, str]:
    """로그인한 API 요청. 없거나 만료·폐기된 세션이면 401."""
    if session is None:
        raise HTTPException(status_code=401, detail="login required", headers={"WWW-Authenticate": "Bearer"})
    return session


def current_user(session: Annotated[tuple[User, int, str], Depends(api_session)]) -> User:
    """세션키(Bearer) 또는 웹 세션 쿠키로 로그인한 사용자. 없으면 401."""
    return session[0]


def _login_redirect(request: Request) -> RedirectResponse:
    target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse(f"/login?{urllib.parse.urlencode({'next': target})}", status_code=303)


def _message(request: Request, title: str, message: str, status_code: int, back: str = "/login") -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="pages/message.html",
        context={"title": title, "message": message, "back": back},
        status_code=status_code,
    )


# ---- 라우트 ----

@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, session: Annotated[tuple[User, int] | None, Depends(optional_session)], next: str | None = None):
    return templates.TemplateResponse(
        request=request,
        name="pages/login.html",
        context={
            "oauths": configured_oauths(),
            "next": safe_next(next),
            "user": session[0] if session else None,
        },
    )


@router.get("/auth/{oauth_name}/start")
def auth_start(
    oauth_name: str,
    request: Request,
    session: Annotated[tuple[User, int] | None, Depends(optional_session)],
    next: str | None = None,
    link: bool = False,
):
    oauth = get_oauth(oauth_name)
    if link and session is None:
        return _login_redirect(request)
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    params = {
        "client_id": oauth.client_id,
        "redirect_uri": oauth.redirect_uri,
        "response_type": "code",
        "scope": oauth.scope,
        "state": state,
    }
    if oauth.pkce:
        params["code_challenge"] = _b64(hashlib.sha256(verifier.encode()).digest())
        params["code_challenge_method"] = "S256"
    if oauth.name == "google":
        params["prompt"] = "select_account"
    response = RedirectResponse(f"{oauth.authorize_url}?{urllib.parse.urlencode(params)}", status_code=303)
    cookie = sign({
        "p": oauth.name,
        "s": state,
        "v": verifier,
        "n": safe_next(next),
        "l": session[0].id if link else None,
        "exp": int(time.time()) + constant.OAUTH_STATE_SECONDS,
    })
    response.set_cookie(
        STATE_COOKIE, cookie,
        max_age=constant.OAUTH_STATE_SECONDS, path="/auth/",
        httponly=True, samesite="lax", secure=_secure_cookie(),
    )
    return response


@router.get("/auth/{oauth_name}/callback")
def auth_callback(
    oauth_name: str,
    request: Request,
    session: Annotated[tuple[User, int] | None, Depends(optional_session)],
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    oauth = get_oauth(oauth_name)
    pending = unsign(request.cookies.get(STATE_COOKIE))
    if error:
        response = _message(request, "로그인 취소", f"{oauth.label} 로그인이 취소됐습니다.", 400)
    elif (
        pending is None or not code or not state
        or pending.get("p") != oauth.name
        or not hmac.compare_digest(str(pending.get("s")), state)
    ):
        response = _message(request, "로그인 실패", "로그인 요청이 만료됐거나 올바르지 않습니다. 다시 시도해 주세요.", 400)
    else:
        response = _finish_login(request, oauth, pending, code, session)
    response.delete_cookie(STATE_COOKIE, path="/auth/")
    return response


def _finish_login(request: Request, oauth: OAuth, pending: dict, code: str, session) -> Response:
    try:
        profile = fetch_profile(oauth, code, pending["v"])
    except (httpx.HTTPError, KeyError, ValueError):
        logger.exception("OAuth token exchange failed: %s", oauth.name)
        return _message(request, "로그인 실패", f"{oauth.label}에서 사용자 정보를 받지 못했습니다. 다시 시도해 주세요.", 502)

    link_user_id = pending.get("l")
    if link_user_id:
        # 연결은 시작할 때와 같은 계정으로 로그인돼 있을 때만 허용합니다.
        if session is None or session[0].id != link_user_id:
            return _message(request, "연결 실패", "로그인 상태가 바뀌었습니다. 다시 로그인한 뒤 연결해 주세요.", 400)
        result = accounts.link(link_user_id, profile)
        if result == "taken":
            return _message(
                request, "연결 실패",
                f"이 {oauth.label} 계정은 이미 다른 EBMS 계정에 연결돼 있습니다. "
                "그 계정에서 연결을 해제한 뒤 다시 시도해 주세요.",
                409, back="/account",
            )
        return RedirectResponse(pending.get("n") or "/account", status_code=303)

    user = accounts.login(profile)
    if user.status != "active":
        return _message(request, "로그인 불가", "정지된 계정입니다. 관리자에게 문의해 주세요.", 403)
    token = accounts.create_session(user.id, "web", request.headers.get("user-agent", ""))
    response = RedirectResponse(pending.get("n") or "/account", status_code=303)
    set_session_cookie(response, token)
    # 쿠키가 새 세션으로 바뀌므로 이전 웹 세션은 폐기합니다.
    if session is not None:
        accounts.revoke_session(session[0].id, session[1])
    return response


@router.post("/auth/logout")
def logout(session: Annotated[tuple[User, int] | None, Depends(optional_session)]):
    if session is not None:
        accounts.revoke_session(session[0].id, session[1])
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(constant.SESSION_COOKIE, path="/")
    return response


@router.get("/account", response_class=HTMLResponse)
def account_page(request: Request, session: Annotated[tuple[User, int] | None, Depends(optional_session)]):
    if session is None:
        return _login_redirect(request)
    user, session_id = session
    identities = accounts.list_identities(user.id)
    linked = {i.oauth for i in identities}
    return templates.TemplateResponse(
        request=request,
        name="pages/account.html",
        context={
            "user": user,
            "identities": identities,
            "labels": {p.name: p.label for p in OAUTHS.values()},
            "linkable": [p for p in configured_oauths() if p.name not in linked],
            "sessions": accounts.list_sessions(user.id),
            "current_session_id": session_id,
            **_quota_json(user.id),
        },
    )


@router.delete("/api/account/identities/{identity_id}", status_code=204)
def delete_identity(identity_id: int, user: Annotated[User, Depends(current_user)]):
    result = accounts.unlink(user.id, identity_id)
    if result == "not_found":
        raise HTTPException(status_code=404, detail="identity not found")
    if result == "last":
        raise HTTPException(status_code=409, detail="cannot remove the last login method")
    return Response(status_code=204)


@router.delete("/api/account/sessions/{session_id}", status_code=204)
def delete_session(session_id: int, user: Annotated[User, Depends(current_user)]):
    if not accounts.revoke_session(user.id, session_id):
        raise HTTPException(status_code=404, detail="session not found")
    return Response(status_code=204)


# ---- 클라이언트 로그인 ----

# base64url(sha256(code_verifier)), 패딩 없음
_CHALLENGE_RE = re.compile(r"[A-Za-z0-9_-]{43}")
# RFC 7636 code_verifier
_VERIFIER_RE = re.compile(r"[A-Za-z0-9._~-]{43,128}")
DEFAULT_DEVICE_NAME = "EBMS 클라이언트"


def is_loopback_redirect(uri: str) -> bool:
    """클라이언트가 로그인 결과를 받을 주소. http://127.0.0.1:<포트>/... 만 허용합니다."""
    if len(uri) > 255:
        return False
    parts = urllib.parse.urlsplit(uri)
    try:
        port = parts.port
    except ValueError:
        return False
    return (
        parts.scheme == "http"
        and port is not None
        and parts.netloc == f"127.0.0.1:{port}"
        and not parts.fragment
    )


def _with_query(uri: str, params: dict) -> str:
    parts = urllib.parse.urlsplit(uri)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True) + list(params.items())
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


def _user_json(user: User) -> dict:
    return {"id": user.id, "display_name": user.display_name, "email": user.email, "role": user.role}


@router.get("/auth/client/authorize")
def client_authorize(
    request: Request,
    session: Annotated[tuple[User, int] | None, Depends(optional_session)],
    redirect_uri: str = "",
    state: str = "",
    code_challenge: str = "",
    code_challenge_method: str = "S256",
    device_name: str = "",
):
    """클라이언트 로그인 시작. 웹에 로그인돼 있으면 1회용 코드를 붙여 클라이언트로 돌려보냅니다."""
    if (
        not is_loopback_redirect(redirect_uri)
        or not state or len(state) > 512
        or code_challenge_method != "S256"
        or not _CHALLENGE_RE.fullmatch(code_challenge)
    ):
        return _message(
            request, "로그인 실패",
            "클라이언트 로그인 요청이 올바르지 않습니다. 클라이언트에서 다시 로그인해 주세요.",
            400, back="/account",
        )
    if session is None:
        return _login_redirect(request)
    code = accounts.create_auth_code(
        session[0].id, code_challenge, redirect_uri, device_name.strip() or DEFAULT_DEVICE_NAME,
    )
    return RedirectResponse(_with_query(redirect_uri, {"code": code, "state": state}), status_code=303)


class ClientTokenRequest(BaseModel):
    code: str
    code_verifier: str


@router.post("/api/auth/client/token")
def client_token(body: ClientTokenRequest):
    """1회용 코드 + code_verifier를 세션키로 바꿉니다. 코드는 성공 여부와 상관없이 한 번만 쓸 수 있습니다."""
    found = accounts.consume_auth_code(body.code)
    if found is None:
        raise HTTPException(status_code=400, detail="invalid or expired code")
    user_id, challenge, device_name = found
    if not _VERIFIER_RE.fullmatch(body.code_verifier) or not hmac.compare_digest(
        _b64(hashlib.sha256(body.code_verifier.encode()).digest()), challenge
    ):
        raise HTTPException(status_code=400, detail="invalid code_verifier")
    user = accounts.get_user(user_id)
    if user is None or user.status != "active":
        raise HTTPException(status_code=403, detail="account suspended")
    token = accounts.create_session(user.id, "client", device_name)
    return {
        "session_key": token,
        "expires_at": int(time.time()) + constant.CLIENT_SESSION_SECONDS,
        "user": _user_json(user),
    }


@router.post("/api/auth/client/logout", status_code=204)
def client_logout(request: Request):
    """현재 클라이언트 세션키를 폐기합니다."""
    token = bearer_token(request)
    session = accounts.authenticate(token, "client") if token else None
    if session is None:
        raise HTTPException(status_code=401, detail="login required", headers={"WWW-Authenticate": "Bearer"})
    accounts.revoke_session(session[0].id, session[1])
    return Response(status_code=204)


@router.get("/api/me")
def get_me(session: Annotated[tuple[User, int, str], Depends(api_session)]):
    user, _, kind = session
    return {
        **_user_json(user),
        "oauths": [
            {"oauth": i.oauth, "name": i.name or i.email or ""}
            for i in accounts.list_identities(user.id)
        ],
        "session": {"kind": kind},
        **_quota_json(user.id),
    }


def _quota_json(user_id: str) -> dict:
    """남은 플레이 티켓과 이번 달 사전 다운로드 사용량."""
    limits = quota.limits_for(user_id)
    return {
        "tickets": quota.ticket_json(quota.ticket_state(user_id, limits)),
        "pre": quota.pre_state(user_id, limits),
    }
