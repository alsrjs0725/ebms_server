"""관리자 페이지와 API. 웹 세션 쿠키로 로그인한 admin만 쓸 수 있습니다.

- 전역 기본값(티켓 수·리필 시간·재차감 유효시간, 사전 다운로드 월 한도·초과 후 속도) 일괄 수정
- 사용자 검색, 사용자별 한도 덮어쓰기(NULL이면 전역 기본값), 티켓 즉시 충전, 정지·해제
- 곡 관리: 곡 검색·삭제(사용 중지)·복원, 곡 zip의 파일 구조 보기·파일 받기
- 곡 임포트: zip 조각 업로드 후 백그라운드 등록, `var/tmp/` 가져오기
- 서버 로그 조회: 수준·로거·검색어 필터
"""
import json
import logging
import mimetypes
import time
import urllib.parse
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel

from . import auth, constant, db, downloads, importer, logs, quota
from .accounts import User
from .templating import templates

router = APIRouter()
logger = logging.getLogger(__name__)

ROLES = ("user", "admin")
STATUSES = ("active", "banned")
SEARCH_LIMIT = 50
# 곡을 지울 때 임포트 배치가 쓰기 잠금을 놓기를 기다리는 시간(초)
DELETE_LOCK_TIMEOUT = 10
# 삭제 사유 최대 글자 수
REASON_MAX = 1000


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


def _paged(cur, select: str, source: str, where: list[str], params: list, order: str, page: int) -> tuple[list, int]:
    """(page쪽의 행들, 전체 개수). 쪽마다 SEARCH_LIMIT개, page는 1부터."""
    cond = " WHERE " + " AND ".join(where) if where else ""
    cur.execute(f"SELECT COUNT(*) FROM {source}{cond}", tuple(params))
    total = cur.fetchone()[0]
    offset = (page - 1) * SEARCH_LIMIT
    cur.execute(f"SELECT {select} FROM {source}{cond} ORDER BY {order} LIMIT {SEARCH_LIMIT} OFFSET {offset}", tuple(params))
    return list(cur.fetchall()), total


def _page_json(items: list[dict], total: int, page: int) -> dict:
    return {"items": items, "total": total, "page": page, "pages": max(1, -(-total // SEARCH_LIMIT))}


def search_songs(q: str = "", page: int = 1) -> dict:
    """곡 번호·폴더명·차트 파일명으로 찾습니다(대소문자 무시). 최근 등록 순으로 쪽마다 SEARCH_LIMIT곡."""
    where = []
    params: list = []
    q = q.strip()
    if q:
        like = _like(q.lower())
        cond = "LOWER(s.folder) LIKE %s OR s.id IN (SELECT song_id FROM chart WHERE LOWER(filename) LIKE %s)"
        params += [like, like]
        if q.isdigit():
            cond = "s.id = %s OR " + cond
            params.insert(0, int(q))
        where.append(f"({cond})")
    with db.connect() as con, con.cursor() as cur:
        rows, total = _paged(
            cur,
            "s.id, s.folder, s.size, (SELECT COUNT(*) FROM chart c WHERE c.song_id = s.id)",
            "song s", where, params, "s.id DESC", page,
        )
    items = [{"id": song_id, "folder": folder, "size": size, "charts": charts} for song_id, folder, size, charts in rows]
    return _page_json(items, total, page)


DELETED_COLUMNS = ("song_id", "folder", "size", "sha256", "charts", "reason", "deleted_by", "deleted_at")


def search_deleted_songs(q: str = "", page: int = 1) -> dict:
    """사용 중지한 곡을 곡 번호·폴더명·사유로 찾습니다. 번호 내림차순으로 쪽마다 SEARCH_LIMIT개."""
    where = []
    params: list = []
    q = q.strip()
    if q:
        like = _like(q.lower())
        cond = "LOWER(d.folder) LIKE %s OR LOWER(d.reason) LIKE %s"
        params += [like, like]
        if q.isdigit():
            cond = "d.song_id = %s OR " + cond
            params.insert(0, int(q))
        where.append(f"({cond})")
    columns = ", ".join(f"d.{name}" for name in DELETED_COLUMNS)
    with db.connect() as con, con.cursor() as cur:
        rows, total = _paged(
            cur, f"{columns}, u.display_name", "deleted_song d LEFT JOIN user u ON u.id = d.deleted_by",
            where, params, "d.song_id DESC", page,
        )
    items = [{**dict(zip(DELETED_COLUMNS, row)), "deleted_by_name": row[-1]} for row in rows]
    return _page_json(items, total, page)


def get_song_detail(song_id: int) -> dict:
    """곡 정보와 곡 zip의 파일 목록. 파일마다 차트면 chart(sha256)가 붙습니다."""
    with db.connect() as con, con.cursor() as cur:
        cur.execute("SELECT id, folder, size, sha256, files FROM song WHERE id = %s", (song_id,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="song not found")
        cur.execute("SELECT id, filename, size FROM chart WHERE song_id = %s ORDER BY filename", (song_id,))
        charts = [{"sha256": sha256, "path": path, "size": size} for sha256, path, size in cur.fetchall()]
    by_path = {chart["path"]: chart["sha256"] for chart in charts}
    files = []
    for entry in json.loads(row[4]) if row[4] else []:
        item = {"path": entry["path"], "size": entry["size"], "comp_size": entry["comp_size"], "kind": entry.get("kind")}
        if entry["path"] in by_path:
            item["chart"] = by_path[entry["path"]]
        files.append(item)
    return {"id": row[0], "folder": row[1], "size": row[2], "sha256": row[3], "charts": charts, "files": files}


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


@router.get("/admin/songs", response_class=HTMLResponse)
@router.get("/admin/import", response_class=HTMLResponse)
def songs_page(
    request: Request,
    session: Annotated[tuple[User, int] | None, Depends(auth.optional_session)],
):
    """곡 관리(검색·삭제·파일 보기)와 곡 임포트 탭. /admin/import는 임포트 탭을 엽니다."""
    if session is None:
        return auth._login_redirect(request)
    if not session[0].is_admin:
        return _forbidden(request)
    return templates.TemplateResponse(
        request=request,
        name="pages/admin_songs.html",
        context={
            "user": session[0],
            "formats": constant.BMS_FORMAT,
            "tab": "import" if request.url.path == "/admin/import" else "songs",
            "limit": SEARCH_LIMIT,
        },
    )


@router.get("/admin/logs", response_class=HTMLResponse)
def logs_page(
    request: Request,
    session: Annotated[tuple[User, int] | None, Depends(auth.optional_session)],
):
    if session is None:
        return auth._login_redirect(request)
    if not session[0].is_admin:
        return _forbidden(request)
    return templates.TemplateResponse(
        request=request,
        name="pages/admin_logs.html",
        context={"user": session[0], "levels": logs.LEVELS, "loggers": logs.loggers()},
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


@router.get("/api/admin/songs")
def list_songs(admin: Annotated[User, Depends(web_admin)], q: str = "", page: Annotated[int, Query(ge=1)] = 1):
    return search_songs(q, page)


@router.get("/api/admin/songs/deleted")
def list_deleted_songs(admin: Annotated[User, Depends(web_admin)], q: str = "", page: Annotated[int, Query(ge=1)] = 1):
    return search_deleted_songs(q, page)


@router.get("/api/admin/songs/{song_id}")
def get_song(song_id: int, admin: Annotated[User, Depends(web_admin)]):
    return get_song_detail(song_id)


class SongDelete(BaseModel):
    reason: str = ""


@router.delete("/api/admin/songs/{song_id}")
def delete_song(song_id: int, admin: Annotated[User, Depends(web_admin)], body: SongDelete | None = None):
    """곡을 사용 중지합니다. 곡 파일은 var/unused/{id}/로 옮기고 사유와 함께 기록하며, 번호는 결번으로 남아
    /restore로 되돌릴 수 있습니다. 클라이언트는 다음 동기화 때 목록에서 뺍니다.
    임포트가 쓰기 잠금을 오래 잡고 있으면 409."""
    reason = (body.reason if body else "").strip()
    if len(reason) > REASON_MAX:
        raise HTTPException(status_code=400, detail=f"reason must be at most {REASON_MAX} characters")
    try:
        deleted = db.Database().delete_song(song_id, reason, admin.id, timeout=DELETE_LOCK_TIMEOUT)
    except TimeoutError:
        raise HTTPException(status_code=409, detail="곡 임포트가 진행 중입니다. 잠시 뒤 다시 시도하세요.")
    if not deleted:
        raise HTTPException(status_code=404, detail="song not found")
    logger.info(f"song {song_id} deleted by {admin.id}: {reason}")
    return {"ok": True}


@router.post("/api/admin/songs/{song_id}/restore")
def restore_song(song_id: int, admin: Annotated[User, Depends(web_admin)]):
    """사용 중지한 곡을 같은 번호로 되돌립니다. 클라이언트는 다음 동기화 때 다시 받습니다."""
    try:
        db.Database().restore_song(song_id, timeout=DELETE_LOCK_TIMEOUT)
    except LookupError:
        raise HTTPException(status_code=404, detail="사용 중지한 곡이 아닙니다.")
    except TimeoutError:
        raise HTTPException(status_code=409, detail="곡 임포트가 진행 중입니다. 잠시 뒤 다시 시도하세요.")
    except db.ChartInUseError as e:
        raise HTTPException(
            status_code=409,
            detail=f"차트가 그 사이 다른 곡({', '.join(f'#{i}' for i in e.song_ids)})으로 등록돼 복원할 수 없습니다.",
        )
    except (FileNotFoundError, ValueError) as e:
        logger.exception(f"restore song {song_id} failed")
        raise HTTPException(status_code=500, detail=f"옮겨 둔 파일로 복원하지 못했습니다: {e}")
    logger.info(f"song {song_id} restored by {admin.id}")
    return get_song_detail(song_id)


@router.get("/api/admin/songs/{song_id}/zip")
def download_song_zip(song_id: int, request: Request, admin: Annotated[User, Depends(web_admin)]):
    """곡 zip 전체. 티켓·사용량은 쓰지 않습니다."""
    return downloads.blob_response(request, "song", song_id, f"song_{song_id}.zip", "song not found")


@router.get("/api/admin/songs/{song_id}/file")
def download_song_file(song_id: int, path: str, admin: Annotated[User, Depends(web_admin)]):
    """곡 zip의 파일 하나를 압축을 풀어 내려줍니다. 티켓·사용량은 쓰지 않습니다."""
    files = db.Database().get_song_files(song_id)
    if files is None:
        raise HTTPException(status_code=404, detail="song not found")
    entry = next((f for f in files if f["path"] == path), None)
    if entry is None:
        raise HTTPException(status_code=404, detail="file not found")
    blob = db.Database().open_blob("song", song_id)
    if blob is None:
        raise HTTPException(status_code=404, detail="song not found")
    try:
        start, decompressor = downloads._zip_member(blob, entry)
    except ValueError:
        blob.close()
        raise HTTPException(status_code=500, detail="broken song file")
    body = downloads._iter_member(blob, start, entry["comp_size"], decompressor)
    name = urllib.parse.quote(path.rsplit("/", 1)[-1])

    @downloads._once
    def cleanup():
        try:
            body.close()
        finally:
            blob.close()

    return downloads.ReleasingStreamingResponse(
        body,
        on_close=cleanup,
        media_type=mimetypes.guess_type(path)[0] or "application/octet-stream",
        headers={"Content-Length": str(entry["size"]), "Content-Disposition": f"attachment; filename*=UTF-8''{name}"},
    )


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


@router.get("/api/admin/logs")
def search_logs(
    admin: Annotated[User, Depends(web_admin)],
    level: str = "",
    logger: str = "",
    q: str = "",
    cursor: str = "",
    limit: int = 200,
):
    """서버 로그를 최신부터 거릅니다. level은 최소 수준, logger는 이름 또는 상위 이름."""
    try:
        return logs.search(level=level, logger=logger, q=q, cursor=cursor, limit=limit)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
