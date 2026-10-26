"""관리자 테스트용 API 키. 실제 서버에서 구현 전에 API를 직접 호출해 보기 위한 키입니다.

- 웹 세션(쿠키)으로 로그인한 관리자만 만들고 폐기할 수 있습니다. API 키로는 키를 만들거나 지우지 못합니다.
- 키는 만든 관리자의 권한으로 동작합니다(`Authorization: Bearer ebms_ak_...`). 그 계정이 관리자가 아니게 되거나
  정지되면 키도 바로 막힙니다.
- 계정 조작(로그인 수단 해제·다른 기기 로그아웃·탈퇴)은 웹 세션 전용이라 키로는 할 수 없습니다(auth.web_user).
- 원문은 만들 때 한 번만 보여 주고 DB에는 sha256만 둡니다. 유효기간은 최대 MAX_DAYS일이며 연장되지 않습니다.
"""
import secrets
import time
from dataclasses import asdict, dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from . import accounts, audit, auth, constant, db
from .accounts import User
from .templating import templates

router = APIRouter()

PREFIX = constant.API_KEY_PREFIX
# 목록에서 키를 알아볼 수 있게 남기는 앞부분 길이(접두어 포함)
HINT_LENGTH = len(PREFIX) + 4
DEFAULT_DAYS = 30
MAX_DAYS = 365
NAME_MAX = 100
# last_used_at 갱신 최소 간격(초)
TOUCH_INTERVAL = 60


@dataclass
class ApiKey:
    id: int
    prefix: str
    user_id: str
    display_name: str
    name: str
    created_at: int
    last_used_at: int | None
    expires_at: int
    revoked_at: int | None


def create(user_id: str, name: str, days: int) -> tuple[int, str]:
    """키를 만들고 (id, 원문)을 반환합니다. 원문은 이때만 알 수 있습니다."""
    token = PREFIX + secrets.token_urlsafe(32)
    now = int(time.time())
    with db.connect() as con, con.cursor() as cur:
        cur.execute(
            """
            INSERT INTO api_key (token_sha256, prefix, user_id, name, created_at, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (accounts.token_hash(token), token[:HINT_LENGTH], user_id, name, now, now + days * 86400),
        )
        key_id = cur.lastrowid
        con.commit()
    return key_id, token


def authenticate(token: str) -> tuple[User, int] | None:
    """유효한 키면 (만든 관리자, 키 id). 만료·폐기됐거나 그 계정이 관리자가 아니거나 정지됐으면 None."""
    now = int(time.time())
    with db.connect() as con, con.cursor() as cur:
        cur.execute(
            """
            SELECT k.id, k.last_used_at, u.id, u.display_name, u.role, u.status, u.banned_until
            FROM api_key k JOIN user u ON u.id = k.user_id
            WHERE k.token_sha256 = %s AND k.revoked_at IS NULL AND k.expires_at > %s
            """,
            (accounts.token_hash(token), now),
        )
        row = cur.fetchone()
        if row is None:
            return None
        key_id, last_used_at, *user_row = row
        user = User(*user_row)
        if user.id in constant.ADMIN_USER_IDS and user.role != "admin":
            accounts._promote_admin(cur, user.id)
            user.role = "admin"
        if user.is_banned or not user.is_admin:
            con.commit()
            return None
        if last_used_at is None or now - last_used_at >= TOUCH_INTERVAL:
            cur.execute("UPDATE api_key SET last_used_at = %s WHERE id = %s", (now, key_id))
        con.commit()
    return user, key_id


def list_keys() -> list[ApiKey]:
    """폐기·만료되지 않은 키 전체(모든 관리자). 최근에 만든 순서입니다."""
    with db.connect() as con, con.cursor() as cur:
        cur.execute(
            """
            SELECT k.id, k.prefix, k.user_id, u.display_name, k.name, k.created_at, k.last_used_at,
                   k.expires_at, k.revoked_at
            FROM api_key k JOIN user u ON u.id = k.user_id
            WHERE k.revoked_at IS NULL AND k.expires_at > %s
            ORDER BY k.id DESC
            """,
            (int(time.time()),),
        )
        return [ApiKey(*row) for row in cur.fetchall()]


def revoke(key_id: int) -> bool:
    """키를 폐기합니다. 다른 관리자가 만든 키도 폐기할 수 있습니다. 없거나 이미 폐기됐으면 False."""
    with db.connect() as con, con.cursor() as cur:
        cur.execute(
            "UPDATE api_key SET revoked_at = %s WHERE id = %s AND revoked_at IS NULL", (int(time.time()), key_id)
        )
        changed = cur.rowcount > 0
        con.commit()
    return changed


# ---- 라우트 ----

def session_admin(session: Annotated[tuple[User, int] | None, Depends(auth.optional_session)]) -> User:
    """웹 세션 쿠키로 로그인한 관리자. API 키로는 키를 관리하지 못하게 합니다."""
    if session is None:
        raise HTTPException(status_code=401, detail="login required")
    if not session[0].is_admin:
        raise HTTPException(status_code=403, detail="admin only")
    return session[0]


@router.get("/admin/api-keys", response_class=HTMLResponse)
def keys_page(request: Request, session: Annotated[tuple[User, int] | None, Depends(auth.optional_session)]):
    if session is None:
        return auth._login_redirect(request)
    if not session[0].is_admin:
        return auth._message(request, "권한 없음", "관리자만 볼 수 있는 페이지입니다.", 403, back="/account")
    return templates.TemplateResponse(
        request=request,
        name="pages/admin_api_keys.html",
        context={
            "user": session[0],
            "keys": list_keys(),
            "default_days": DEFAULT_DAYS,
            "max_days": MAX_DAYS,
            "name_max": NAME_MAX,
            "public_url": constant.PUBLIC_URL,
        },
    )


@router.get("/api/admin/api-keys")
def get_keys(admin: Annotated[User, Depends(session_admin)]):
    return [asdict(k) for k in list_keys()]


class KeyCreate(BaseModel):
    name: str
    days: int = DEFAULT_DAYS


@router.post("/api/admin/api-keys", status_code=201)
def post_key(body: KeyCreate, request: Request, admin: Annotated[User, Depends(session_admin)]):
    """키를 만듭니다. 응답의 key가 원문이며 다시 볼 수 없습니다."""
    name = body.name.strip()
    if not name or len(name) > NAME_MAX:
        raise HTTPException(status_code=400, detail=f"이름은 1~{NAME_MAX}자로 입력하세요.")
    if not 1 <= body.days <= MAX_DAYS:
        raise HTTPException(status_code=400, detail=f"유효기간은 1~{MAX_DAYS}일로 입력하세요.")
    key_id, token = create(admin.id, name, body.days)
    audit.record(request, admin.id, "api_key.create", admin.id, key=key_id, name=name, days=body.days)
    return {"id": key_id, "key": token, "expires_at": int(time.time()) + body.days * 86400}


@router.delete("/api/admin/api-keys/{key_id}", status_code=204)
def delete_key(key_id: int, request: Request, admin: Annotated[User, Depends(session_admin)]):
    if not revoke(key_id):
        raise HTTPException(status_code=404, detail="api key not found")
    audit.record(request, admin.id, "api_key.revoke", key=key_id)
    return Response(status_code=204)
