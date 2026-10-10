"""관리자 페이지와 API. 웹 세션 쿠키로 로그인한 admin만 쓸 수 있습니다.

- 전역 기본값(티켓 수·리필 시간·재차감 유효시간, 사전 다운로드 월 한도·초과 후 속도) 일괄 수정
- 사용자 검색, 사용자별 한도 덮어쓰기(NULL이면 전역 기본값), 티켓 즉시 충전, 정지·해제
- 곡 임포트: zip 조각 업로드 후 백그라운드 등록, `var/tmp/` 가져오기
"""
import logging
import time
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from . import auth, constant, db, importer, quota
from .accounts import User
from .templating import templates

router = APIRouter()
logger = logging.getLogger(__name__)

ROLES = ("user", "admin")
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


USER_COLUMNS = ("id", "display_name", "email", "role", "status", "created_at", "last_login_at", *quota.USER_LIMITS)


def search_users(q: str = "") -> list[dict]:
    """이름·이메일·계정 ID·연결된 OAuth 이름/이메일로 찾습니다(대소문자 무시). 최근 로그인 순, 최대 SEARCH_LIMIT명."""
    columns = ", ".join(USER_COLUMNS)
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
        users = [dict(zip(USER_COLUMNS, row)) for row in cur.fetchall()]
        return _with_usage(cur, users)


def _with_usage(cur, users: list[dict]) -> list[dict]:
    """사용자 목록에 OAuth·티켓·사전 사용량을 붙입니다. 사용자 수와 상관없이 같은 연결로 쿼리 4번만 씁니다(#40)."""
    if not users:
        return []
    ids = [user["id"] for user in users]
    marks = ", ".join(["%s"] * len(ids))
    settings = quota.get_settings(cur)
    oauths: dict[str, list[str]] = {user_id: [] for user_id in ids}
    cur.execute(f"SELECT user_id, oauth FROM user_identity WHERE user_id IN ({marks}) ORDER BY id", ids)
    for user_id, oauth in cur.fetchall():
        oauths[user_id].append(oauth)
    cur.execute(f"SELECT user_id, tickets, updated_at FROM user_ticket WHERE user_id IN ({marks})", ids)
    tickets = {user_id: (stored, updated_at) for user_id, stored, updated_at in cur.fetchall()}
    month = quota.current_month()
    cur.execute(f"SELECT user_id, bytes FROM pre_usage WHERE month = %s AND user_id IN ({marks})", (month, *ids))
    used = {user_id: int(nbytes) for user_id, nbytes in cur.fetchall()}

    now = time.time()
    result = []
    for user in users:
        overrides = {name: user[name] for name in quota.USER_LIMITS}
        limits = quota.merge_limits(settings, overrides)
        result.append({
            "id": user["id"],
            "display_name": user["display_name"],
            "email": user["email"],
            "role": user["role"],
            "status": user["status"],
            "created_at": user["created_at"],
            "last_login_at": user["last_login_at"],
            "oauths": oauths[user["id"]],
            # 사용자별 값(None이면 전역 기본값)
            "overrides": overrides,
            "tickets": quota.ticket_json(quota.ticket_state_from(tickets.get(user["id"]), limits, now)),
            "pre": quota.pre_state_from(used.get(user["id"], 0), limits, month),
        })
    return result


def get_user_detail(user_id: str) -> dict:
    with db.connect() as con, con.cursor() as cur:
        cur.execute(f"SELECT {', '.join(USER_COLUMNS)} FROM user WHERE id = %s", (user_id,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="user not found")
        return _with_usage(cur, [dict(zip(USER_COLUMNS, row))])[0]


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
        return _forbidden(request)
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


@router.get("/admin/import", response_class=HTMLResponse)
def import_page(
    request: Request,
    session: Annotated[tuple[User, int] | None, Depends(auth.optional_session)],
):
    if session is None:
        return auth._login_redirect(request)
    if not session[0].is_admin:
        return _forbidden(request)
    return templates.TemplateResponse(
        request=request,
        name="pages/admin_import.html",
        context={"user": session[0], "formats": constant.BMS_FORMAT},
    )


def _forbidden(request: Request):
    return auth._message(request, "권한 없음", "관리자만 볼 수 있는 페이지입니다.", 403, back="/account")


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
    role: str | None = None
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
    role = values.pop("role", None)
    status = values.pop("status", None)
    if "role" in body.model_fields_set:
        if role not in ROLES:
            raise HTTPException(status_code=400, detail=f"role must be one of {', '.join(ROLES)}")
        if role != "admin" and user_id == admin.id:
            raise HTTPException(status_code=400, detail="cannot demote yourself")
    if "status" in body.model_fields_set:
        if status not in STATUSES:
            raise HTTPException(status_code=400, detail=f"status must be one of {', '.join(STATUSES)}")
        if status != "active" and user_id == admin.id:
            raise HTTPException(status_code=400, detail="cannot suspend yourself")
    try:
        quota.set_overrides(user_id, values)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if role is not None or status is not None:
        updates = []
        params = []
        if role is not None:
            updates.append("role = %s")
            params.append(role)
        if status is not None:
            updates.append("status = %s")
            params.append(status)
        params.append(user_id)
        with db.connect() as con, con.cursor() as cur:
            cur.execute(f"UPDATE user SET {', '.join(updates)} WHERE id = %s", tuple(params))
            con.commit()
    return get_user_detail(user_id)


@router.post("/api/admin/users/{user_id}/refill")
def refill(user_id: str, admin: Annotated[User, Depends(web_admin)]):
    """티켓을 최대치로 채웁니다."""
    get_user_detail(user_id)
    quota.refill_tickets(user_id)
    return get_user_detail(user_id)


class UploadStart(BaseModel):
    filename: str
    size: int


def _upload_call(fn, *args):
    try:
        return fn(*args)
    except importer.UploadError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)


@router.post("/api/admin/import/uploads")
def start_upload(body: UploadStart, admin: Annotated[User, Depends(web_admin)]):
    """zip 조각 업로드를 시작합니다. 디스크가 모자라면 507."""
    return _upload_call(importer.start_upload, body.filename, body.size)


@router.put("/api/admin/import/uploads/{upload_id}")
async def put_chunk(upload_id: str, offset: int, request: Request, admin: Annotated[User, Depends(web_admin)]):
    """본문(바이트)을 offset 위치에 이어 씁니다. offset이 받은 크기와 다르면 409."""
    data = await request.body()
    return await run_in_threadpool(_upload_call, importer.write_chunk, upload_id, offset, data)


@router.delete("/api/admin/import/uploads/{upload_id}")
def cancel_upload(upload_id: str, admin: Annotated[User, Depends(web_admin)]):
    importer.cancel_upload(upload_id)
    return {"ok": True}


@router.post("/api/admin/import/uploads/{upload_id}/finish")
def finish_upload(upload_id: str, admin: Annotated[User, Depends(web_admin)]):
    """다 받은 zip의 임포트 작업을 백그라운드로 시작합니다."""
    job = _upload_call(importer.finish_upload, upload_id)
    logger.info(f"import job {job.id} by {admin.id}: {job.name}")
    return job.json()


@router.post("/api/admin/import/tmp")
def import_tmp(admin: Annotated[User, Depends(web_admin)]):
    """`var/tmp/` 아래 곡을 등록하는 작업을 백그라운드로 시작합니다(등록한 곡 폴더는 지움)."""
    return importer.start_tmp_job().json()


@router.get("/api/admin/import/jobs")
def list_jobs(admin: Annotated[User, Depends(web_admin)]):
    return [job.json() for job in importer.list_jobs()]


@router.get("/api/admin/import/jobs/{job_id}")
def get_job(job_id: str, admin: Annotated[User, Depends(web_admin)]):
    job = importer.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job.json()
