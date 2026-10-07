"""관리자 페이지와 API. 웹 세션 쿠키로 로그인한 admin만 쓸 수 있습니다.

- 전역 기본값(티켓 수·리필 시간·재차감 유효시간, 사전 다운로드 월 한도·초과 후 속도) 일괄 수정
- 사용자 검색, 사용자별 한도 덮어쓰기(NULL이면 전역 기본값), 티켓 즉시 충전, 정지·해제
"""
import time
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from . import accounts, auth, db, quota
from .accounts import User
from .templating import templates

router = APIRouter()

STATUSES = ("active", "banned")
SEARCH_LIMIT = 50


def web_admin(session: Annotated[tuple[User, int] | None, Depends(auth.optional_session)]) -> User:
    if session is None:
        raise HTTPException(status_code=401, detail="login required")
    if not session[0].is_admin:
        raise HTTPException(status_code=403, detail="admin only")
    return session[0]


def _like(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def search_users(q: str = "") -> list[dict]:
    """이름·이메일·계정 ID·연결된 OAuth 이름/이메일로 찾습니다(대소문자 무시). 최근 로그인 순, 최대 SEARCH_LIMIT명."""
    columns = "id, display_name, email, role, status, created_at, last_login_at, " + ", ".join(quota.USER_LIMITS)
    with db.connect() as con, con.cursor() as cur:
        if q.strip():
            # 컬럼이 _bin 콜레이션이라 대소문자를 맞춰 비교합니다.
            like = _like(q.strip().lower())
            cur.execute(
                f"""
                SELECT {columns} FROM user
                WHERE id = %s OR LOWER(display_name) LIKE %s OR LOWER(email) LIKE %s
                   OR id IN (SELECT user_id FROM user_identity WHERE LOWER(name) LIKE %s OR LOWER(email) LIKE %s)
                ORDER BY last_login_at DESC, created_at DESC LIMIT {SEARCH_LIMIT}
                """,
                (q.strip(), like, like, like, like),
            )
        else:
            cur.execute(f"SELECT {columns} FROM user ORDER BY last_login_at DESC, created_at DESC LIMIT {SEARCH_LIMIT}")
        rows = cur.fetchall()
    names = ("id", "display_name", "email", "role", "status", "created_at", "last_login_at", *quota.USER_LIMITS)
    return [_with_usage(dict(zip(names, row))) for row in rows]


def _with_usage(user: dict) -> dict:
    limits = quota.limits_for(user["id"])
    return {
        "id": user["id"],
        "display_name": user["display_name"],
        "email": user["email"],
        "role": user["role"],
        "status": user["status"],
        "created_at": user["created_at"],
        "last_login_at": user["last_login_at"],
        "oauths": [i.oauth for i in accounts.list_identities(user["id"])],
        # 사용자별 값(None이면 전역 기본값)
        "overrides": {name: user[name] for name in quota.USER_LIMITS},
        "tickets": quota.ticket_json(quota.ticket_state(user["id"], limits)),
        "pre": quota.pre_state(user["id"], limits),
    }


def get_user_detail(user_id: str) -> dict:
    columns = "id, display_name, email, role, status, created_at, last_login_at, " + ", ".join(quota.USER_LIMITS)
    with db.connect() as con, con.cursor() as cur:
        cur.execute(f"SELECT {columns} FROM user WHERE id = %s", (user_id,))
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="user not found")
    names = ("id", "display_name", "email", "role", "status", "created_at", "last_login_at", *quota.USER_LIMITS)
    return _with_usage(dict(zip(names, row)))


# ---- 페이지 ----

@router.get("/admin", response_class=HTMLResponse)
def admin_page(
    request: Request,
    session: Annotated[tuple[User, int] | None, Depends(auth.optional_session)],
    q: str = "",
):
    if session is None:
        return auth._login_redirect(request)
    user = session[0]
    if not user.is_admin:
        return auth._message(request, "권한 없음", "관리자만 볼 수 있는 페이지입니다.", 403, back="/account")
    return templates.TemplateResponse(
        request=request,
        name="pages/admin.html",
        context={
            "user": user,
            "settings": quota.get_settings(),
            "users": search_users(q),
            "q": q,
            "limit": SEARCH_LIMIT,
            "now": int(time.time()),
        },
    )


# ---- API ----

@router.get("/api/admin/settings")
def get_settings(admin: Annotated[User, Depends(web_admin)]):
    return quota.get_settings()


@router.put("/api/admin/settings")
def put_settings(values: dict, admin: Annotated[User, Depends(web_admin)]):
    """보낸 항목만 바꿉니다. 하나라도 틀리면 아무것도 바꾸지 않고 400."""
    try:
        return quota.update_settings(values)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/api/admin/users")
def list_users(admin: Annotated[User, Depends(web_admin)], q: str = ""):
    return search_users(q)


@router.get("/api/admin/users/{user_id}")
def get_user(user_id: str, admin: Annotated[User, Depends(web_admin)]):
    return get_user_detail(user_id)


class UserUpdate(BaseModel):
    status: str | None = None
    max_tickets: int | None = None
    refill_seconds: int | None = None
    pre_monthly_bytes: int | None = None
    pre_throttled_kbps: int | None = None


@router.put("/api/admin/users/{user_id}")
def put_user(user_id: str, body: UserUpdate, admin: Annotated[User, Depends(web_admin)]):
    """보낸 항목만 바꿉니다. 한도 항목에 null을 보내면 전역 기본값을 따릅니다."""
    get_user_detail(user_id)
    values = body.model_dump(exclude_unset=True)
    status = values.pop("status", None)
    if "status" in body.model_fields_set:
        if status not in STATUSES:
            raise HTTPException(status_code=400, detail=f"status must be one of {', '.join(STATUSES)}")
        if status != "active" and user_id == admin.id:
            raise HTTPException(status_code=400, detail="cannot suspend yourself")
    try:
        quota.set_overrides(user_id, values)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if status is not None:
        with db.connect() as con, con.cursor() as cur:
            cur.execute("UPDATE user SET status = %s WHERE id = %s", (status, user_id))
            con.commit()
    return get_user_detail(user_id)


@router.post("/api/admin/users/{user_id}/refill")
def refill(user_id: str, admin: Annotated[User, Depends(web_admin)]):
    """티켓을 최대치로 채웁니다."""
    get_user_detail(user_id)
    quota.refill_tickets(user_id)
    return get_user_detail(user_id)
