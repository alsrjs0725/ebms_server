"""관리자 페이지와 API. 웹 세션 쿠키로 로그인한 admin만 쓸 수 있습니다.
API(`/api/admin/...`)는 관리자 API 키(api_keys.py)로도 부를 수 있습니다.

- 전역 기본값(티켓 수·리필 시간·재차감 유효시간, 사전 다운로드 월 한도·초과 후 속도) 일괄 수정
- 사용자 검색, 사용자별 한도 덮어쓰기(NULL이면 전역 기본값), 티켓 즉시 충전, 정지·해제
- 곡 관리: 곡 검색·삭제(사용 중지)·복원, 곡 zip의 파일 구조 보기·파일 받기
- 곡 임포트: zip·rar·7z 조각 업로드 후 백그라운드 등록, `var/tmp/` 가져오기
- 서버 로그 조회: 수준·로거·검색어 필터
- 곡 다운로드 기록 조회: 사용자·곡·결과 필터

사용자 정보를 보거나 바꾸면 audit.record로 관리자 접속기록을 남깁니다.
"""
import asyncio
import json
import logging
import mimetypes
import time
import urllib.parse
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from . import audit, auth, cache, conflicts, constant, db, download_log, downloads, importer, logs, media_match, quota, staging
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


def web_admin(request: Request) -> User:
    """웹 세션 쿠키로 로그인한 관리자, 또는 관리자 API 키(Bearer ebms_ak_...).

    Authorization 헤더가 있으면 API 키만 봅니다. 클라이언트 세션키로는 관리자 API를 쓰지 못합니다.
    """
    if "authorization" in request.headers:
        session = auth.api_key_session(request)
    else:
        session = auth.optional_session(request)
    if session is None:
        raise HTTPException(status_code=401, detail="login required")
    if not session[0].is_admin:
        raise HTTPException(status_code=403, detail="admin only")
    return session[0]


def _like(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


USER_COLUMNS = ("id", "display_name", "role", "status", "banned_until", "created_at", "last_login_at", *quota.USER_LIMITS)


def search_users(q: str = "") -> list[dict]:
    """닉네임·계정 ID로 찾습니다(대소문자 무시). 최근 로그인 순, 최대 SEARCH_LIMIT명."""
    columns = ", ".join(USER_COLUMNS)
    with db.connect() as con, con.cursor() as cur:
        if q.strip():
            # 컬럼이 _bin 콜레이션이라 대소문자를 맞춰 비교합니다.
            like = _like(q.strip().lower())
            cur.execute(
                f"""
                SELECT {columns} FROM user
                WHERE id = %s OR LOWER(display_name) LIKE %s
                ORDER BY last_login_at DESC, created_at DESC LIMIT {SEARCH_LIMIT}
                """,
                (q.strip(), like),
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
            "role": user["role"],
            # 끝나는 시각이 지난 정지는 정리 작업이 되돌리기 전이라도 정지가 아닌 것으로 보여 줍니다.
            "status": "banned" if User(user["id"], user["display_name"], user["role"], user["status"],
                                       user["banned_until"]).is_banned else "active",
            "banned_until": user["banned_until"] if user["status"] == "banned" else None,
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
    users = search_users(q)
    audit.record(request, user.id, "user.search", [u["id"] for u in users], q=q)
    return templates.TemplateResponse(
        request=request,
        name="pages/admin.html",
        context={
            "user": user,
            "settings": quota.get_settings(),
            "users": users,
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


@router.get("/admin/downloads", response_class=HTMLResponse)
def downloads_page(
    request: Request,
    session: Annotated[tuple[User, int] | None, Depends(auth.optional_session)],
):
    if session is None:
        return auth._login_redirect(request)
    if not session[0].is_admin:
        return _forbidden(request)
    return templates.TemplateResponse(
        request=request,
        name="pages/admin_downloads.html",
        context={
            "user": session[0],
            "results": download_log.RESULTS,
            "retention_days": constant.RETENTION_DOWNLOAD_LOG_DAYS,
        },
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
        settings = quota.update_settings(values)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    logger.info(f"settings changed by {admin.id}: {values}")
    return settings


@router.get("/api/admin/users")
def list_users(request: Request, admin: Annotated[User, Depends(web_admin)], q: str = ""):
    users = search_users(q)
    audit.record(request, admin.id, "user.search", [u["id"] for u in users], q=q)
    return users


@router.get("/api/admin/users/{user_id}")
def get_user(user_id: str, request: Request, admin: Annotated[User, Depends(web_admin)]):
    detail = get_user_detail(user_id)
    audit.record(request, admin.id, "user.view", user_id)
    return detail


class UserUpdate(BaseModel):
    role: str | None = None
    status: str | None = None
    # status를 "banned"로 바꿀 때 정지가 끝나는 시각(unix 초). 없거나 null이면 무기한
    banned_until: int | None = None
    max_tickets: int | None = None
    refill_seconds: int | None = None
    pre_monthly_bytes: int | None = None
    pre_throttled_kbps: int | None = None


@router.put("/api/admin/users/{user_id}")
def put_user(user_id: str, body: UserUpdate, request: Request, admin: Annotated[User, Depends(web_admin)]):
    """보낸 항목만 바꿉니다. 한도 항목에 null을 보내면 전역 기본값을 따릅니다."""
    get_user_detail(user_id)
    values = body.model_dump(exclude_unset=True)
    role = values.pop("role", None)
    status = values.pop("status", None)
    banned_until = values.pop("banned_until", None)
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
        if status != "banned":
            banned_until = None
        elif banned_until is not None and banned_until <= time.time():
            raise HTTPException(status_code=400, detail="banned_until must be in the future")
    elif banned_until is not None:
        raise HTTPException(status_code=400, detail="banned_until needs status")
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
            updates.append("banned_until = %s")
            params.append(banned_until)
        params.append(user_id)
        with db.connect() as con, con.cursor() as cur:
            cur.execute(f"UPDATE user SET {', '.join(updates)} WHERE id = %s", tuple(params))
            con.commit()
    changes = body.model_dump(exclude_unset=True)
    audit.record(request, admin.id, "user.update", user_id, **{k: "null" if v is None else v for k, v in changes.items()})
    return get_user_detail(user_id)


@router.post("/api/admin/users/{user_id}/refill")
def refill(user_id: str, request: Request, admin: Annotated[User, Depends(web_admin)]):
    """티켓을 최대치로 채웁니다."""
    get_user_detail(user_id)
    quota.refill_tickets(user_id)
    audit.record(request, admin.id, "user.refill", user_id)
    return get_user_detail(user_id)


@router.get("/api/admin/downloads")
def list_downloads(
    request: Request,
    admin: Annotated[User, Depends(web_admin)],
    user: str = "",
    song: Annotated[int | None, Query(ge=0)] = None,
    result: str = "",
    page: Annotated[int, Query(ge=1)] = 1,
):
    if result and result not in download_log.RESULTS:
        raise HTTPException(status_code=422, detail="unknown result")
    data = download_log.search(user, song, result, page)
    audit.record(
        request, admin.id, "download.search", sorted({item["user_id"] for item in data["items"]}),
        user=user, song=song if song is not None else "", result=result, page=page,
    )
    return data


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


class ChartDetach(BaseModel):
    charts: list[str]


@router.post("/api/admin/songs/{song_id}/charts/detach")
def detach_charts(song_id: int, body: ChartDetach, admin: Annotated[User, Depends(web_admin)]):
    """곡에서 차트 몇 개(sha256)만 뗍니다. 다른 곡 차트가 잘못 병합된 경우, 떼고 나서 그 차트의 원본 폴더를
    임포트하면 거기에 다시 붙습니다. 곡 zip에 든 그 차트 파일도 뺍니다."""
    try:
        result = db.Database().detach_charts(song_id, body.charts, timeout=DELETE_LOCK_TIMEOUT)
    except TimeoutError:
        raise HTTPException(status_code=409, detail="곡 임포트가 진행 중입니다. 잠시 뒤 다시 시도하세요.")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if result is None:
        raise HTTPException(status_code=404, detail="song not found")
    logger.info(f"song {song_id} charts detached by {admin.id}: {result}")
    return result


@router.get("/api/admin/songs/{song_id}/zip")
def download_song_zip(song_id: int, request: Request, admin: Annotated[User, Depends(web_admin)]):
    """곡 zip 전체. 티켓·사용량은 쓰지 않습니다."""
    return downloads.file_response(request, "song", song_id, f"song_{song_id}.zip", "song not found")


@router.get("/api/admin/songs/{song_id}/file")
def download_song_file(song_id: int, path: str, admin: Annotated[User, Depends(web_admin)]):
    """곡 zip의 파일 하나를 압축을 풀어 내려줍니다. 티켓·사용량은 쓰지 않습니다."""
    files = db.Database().get_song_files(song_id)
    if files is None:
        raise HTTPException(status_code=404, detail="song not found")
    entry = next((f for f in files if f["path"] == path), None)
    if entry is None:
        raise HTTPException(status_code=404, detail="file not found")
    file = db.Database().open_file("song", song_id)
    if file is None:
        raise HTTPException(status_code=404, detail="song not found")
    try:
        start, decompressor = downloads.zip_member(file, entry)
    except ValueError:
        file.close()
        raise HTTPException(status_code=500, detail="broken song file")
    name = urllib.parse.quote(path.rsplit("/", 1)[-1])
    return StreamingResponse(
        downloads.send(downloads.member_chunks(file, start, entry["comp_size"], decompressor)),
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
    """zip·rar·7z 조각 업로드를 시작합니다. 디스크가 모자라면 507."""
    return _upload_call(importer.start_upload, body.filename, body.size)


# 업로드 조각을 받는 대로 디스크에 쓰는 단위. 쓰는 동안 다음 단위를 받습니다.
UPLOAD_WRITE_BYTES = 4 * 1024 * 1024


@router.put("/api/admin/import/uploads/{upload_id}")
async def put_chunk(upload_id: str, offset: int, request: Request, admin: Annotated[User, Depends(web_admin)]):
    """본문(바이트)을 offset 위치에 씁니다. 본문을 다 모으지 않고 받는 대로 쓰고, 한 단위를 쓰는 동안 다음 단위를 받습니다.
    조각은 순서와 상관없이 여러 개를 동시에 보내도 됩니다. 응답의 received는 지금까지 받은 바이트 수입니다."""
    writer = await run_in_threadpool(_upload_call, importer.ChunkWriter, upload_id, offset)
    pending = None
    try:
        buf = bytearray()
        async for part in request.stream():
            buf += part
            if len(buf) >= UPLOAD_WRITE_BYTES:
                if pending is not None:
                    await pending
                pending = asyncio.ensure_future(run_in_threadpool(_upload_call, writer.write, bytes(buf)))
                buf.clear()
        if pending is not None:
            await pending
            pending = None
        if buf:
            await run_in_threadpool(_upload_call, writer.write, bytes(buf))
        return await run_in_threadpool(_upload_call, writer.finish)
    except BaseException:
        if pending is not None:
            # 쓰던 단위가 끝난 뒤 닫습니다(쓰는 중에 닫지 않도록).
            await asyncio.gather(pending, return_exceptions=True)
        writer.abort()
        raise


@router.delete("/api/admin/import/uploads/{upload_id}")
def cancel_upload(upload_id: str, admin: Annotated[User, Depends(web_admin)]):
    importer.cancel_upload(upload_id)
    return {"ok": True}


@router.post("/api/admin/import/uploads/{upload_id}/finish")
def finish_upload(upload_id: str, admin: Annotated[User, Depends(web_admin)]):
    """다 받은 zip(또는 rar·7z)을 대기 묶음에 푸는 작업을 백그라운드로 시작합니다. 등록은 검토 뒤 commit할 때 합니다."""
    job = _upload_call(importer.finish_upload, upload_id)
    logger.info(f"import job {job.id} by {admin.id}: {job.name}")
    return job.json()


class UploadFinish(BaseModel):
    ids: list[str]


@router.post("/api/admin/import/uploads/finish")
def finish_uploads(body: UploadFinish, admin: Annotated[User, Depends(web_admin)]):
    """다 받은 파일 여럿(rar 분할 압축의 볼륨들)을 한 묶음으로 푸는 작업을 시작합니다."""
    job = _upload_call(importer.finish_uploads, body.ids)
    logger.info(f"import job {job.id} by {admin.id}: {job.name}")
    return job.json()


@router.post("/api/admin/import/tmp")
def import_tmp(admin: Annotated[User, Depends(web_admin)]):
    """`var/tmp/` 아래 곡을 대기 묶음으로 옮기는 작업을 백그라운드로 시작합니다."""
    job = importer.start_tmp_job()
    logger.info(f"import job {job.id} by {admin.id}: {job.name}")
    return job.json()


def _stage_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except staging.StageError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)


@router.get("/api/admin/import/stages")
def list_stages(admin: Annotated[User, Depends(web_admin)]):
    """대기 묶음 목록(최근 순)."""
    return staging.list_stages()


@router.get("/api/admin/import/cache")
def cache_status(admin: Annotated[User, Depends(web_admin)]):
    """임포트 캐시(SSD) 사용량. 꺼져 있으면 enabled false."""
    return cache.status()


@router.get("/api/admin/import/stages/{stage_id}")
def get_stage(
    stage_id: str,
    admin: Annotated[User, Depends(web_admin)],
    page: Annotated[int, Query(ge=1)] = 1,
    show: str = "",
    q: str = "",
    sort: str = "",
):
    """대기 묶음의 곡 한 쪽. show로 판정(new/add/ambiguous/unused/similar)이나 changed·failed만 보고,
    sort=files_desc/files_asc면 파일 수로 정렬합니다."""
    return _stage_call(staging.get_stage, stage_id, page, show, q, sort)


class UnitTarget(BaseModel):
    target: str | int
    charts: dict[str, str | int | None] | None = None


@router.put("/api/admin/import/stages/{stage_id}/units/{unit_id}")
def set_unit_target(stage_id: str, unit_id: str, body: UnitTarget, admin: Annotated[User, Depends(web_admin)]):
    """곡의 대상(auto/new/skip/곡 번호)과 차트별 대상(null이면 곡의 대상)을 바꿉니다."""
    return _stage_call(staging.set_target, stage_id, unit_id, body.target, body.charts)


@router.get("/api/admin/import/stages/{stage_id}/units/{unit_id}/files")
def unit_files(stage_id: str, unit_id: str, admin: Annotated[User, Depends(web_admin)]):
    """곡에서 차트가 아닌 파일 목록 [{path, size}]."""
    return _stage_call(staging.unit_files, stage_id, unit_id)


@router.get("/api/admin/import/stages/{stage_id}/units/{unit_id}/zip")
def download_unit(stage_id: str, unit_id: str, admin: Annotated[User, Depends(web_admin)]):
    """등록 전 곡 파일(zip). 검토하거나 인코딩 오류 곡을 직접 고치려고 내려받습니다."""
    path, name, temporary = _stage_call(staging.unit_download, stage_id, unit_id)
    logger.info(f"import stage {stage_id} unit {unit_id} downloaded by {admin.id}")
    return FileResponse(
        path,
        media_type="application/zip",
        filename=name,
        background=BackgroundTask(path.unlink, missing_ok=True) if temporary else None,
    )


@router.get("/api/admin/import/stages/{stage_id}/units/{unit_id}/conflicts")
def unit_conflicts(stage_id: str, unit_id: str, admin: Annotated[User, Depends(web_admin)]):
    """곡의 병합 충돌 파일 목록과 고른 것."""
    return _stage_call(conflicts.list_conflicts, stage_id, unit_id)


@router.get("/api/admin/import/stages/{stage_id}/units/{unit_id}/conflicts/file")
def conflict_file(
    stage_id: str, unit_id: str, path: str, side: str, admin: Annotated[User, Depends(web_admin)], raw: bool = False,
):
    """충돌 파일의 기존(old)·새(new) 쪽. 브라우저가 못 여는 형식은 바꿔서 줍니다(raw면 원본, 내려받기)."""
    body, media_type, name = _stage_call(conflicts.web_file, stage_id, unit_id, path, side, raw)
    disposition = "attachment" if raw else "inline"
    headers = {"Content-Disposition": f"{disposition}; filename*=UTF-8''{urllib.parse.quote(name)}"}
    if isinstance(body, bytes):
        return Response(body, media_type=media_type, headers=headers)
    return FileResponse(body, media_type=media_type, headers=headers)


@router.get("/api/admin/import/stages/{stage_id}/units/{unit_id}/conflicts/compare")
def conflict_compare(stage_id: str, unit_id: str, path: str, admin: Annotated[User, Depends(web_admin)]):
    """충돌 파일 비교 결과. 키음은 파형·상관계수, 영상은 프레임별 차이, 그 밖은 줄 단위 diff."""
    kind = media_match.kind(path)
    fn = {"sound": conflicts.sound_compare, "video": conflicts.video_compare}.get(kind, conflicts.text_diff)
    return _stage_call(fn, stage_id, unit_id, path)


@router.get("/api/admin/import/stages/{stage_id}/units/{unit_id}/conflicts/charts")
def conflict_charts(stage_id: str, unit_id: str, path: str, admin: Annotated[User, Depends(web_admin)]):
    """충돌 파일을 채보로 들어볼 때 고를 차트(기존·새 쪽)."""
    return _stage_call(conflicts.render_charts, stage_id, unit_id, path)


@router.get("/api/admin/import/stages/{stage_id}/units/{unit_id}/conflicts/render")
def conflict_render(
    stage_id: str, unit_id: str, song: int, side: str, chart: str, admin: Annotated[User, Depends(web_admin)],
):
    """차트의 바탕 음원과 충돌 키음 시각표. 만드는 중이면 진행률을 돌려주므로 다 될 때까지 다시 부릅니다."""
    return _stage_call(conflicts.render, stage_id, unit_id, song, side, chart)


@router.get("/api/admin/import/stages/{stage_id}/units/{unit_id}/conflicts/render/bed")
def conflict_render_bed(
    stage_id: str, unit_id: str, song: int, side: str, chart: str, admin: Annotated[User, Depends(web_admin)],
):
    """차트의 바탕 음원(충돌 키음을 뺀 소리, FLAC)."""
    return FileResponse(_stage_call(conflicts.render_bed, stage_id, unit_id, song, side, chart), media_type="audio/flac")


class ConflictChoices(BaseModel):
    choices: dict[str, str | None]


@router.put("/api/admin/import/stages/{stage_id}/units/{unit_id}/conflicts")
def set_conflict_choices(
    stage_id: str, unit_id: str, body: ConflictChoices, admin: Annotated[User, Depends(web_admin)],
):
    """충돌 파일마다 old(기존 유지)·new(새 파일로 바꿈)를 고릅니다. null이면 고른 것을 지웁니다."""
    return _stage_call(conflicts.set_choices, stage_id, unit_id, body.choices)


@router.post("/api/admin/import/stages/{stage_id}/units/{unit_id}/conflicts/apply")
def apply_conflicts(stage_id: str, unit_id: str, admin: Annotated[User, Depends(web_admin)], slow: bool = False):
    """고른 대로 그 곡만 다시 넣는 작업을 시작합니다."""
    job = _stage_call(conflicts.apply, stage_id, unit_id, slow)
    logger.info(f"import conflicts applied {job.id} by {admin.id}: stage {stage_id} unit {unit_id}")
    return job.json()


@router.post("/api/admin/import/stages/{stage_id}/analyze")
def analyze_stage(stage_id: str, admin: Annotated[User, Depends(web_admin)]):
    """지금 DB 기준으로 판정을 다시 합니다."""
    return _stage_call(staging.analyze, stage_id)


@router.post("/api/admin/import/stages/{stage_id}/commit")
def commit_stage(stage_id: str, admin: Annotated[User, Depends(web_admin)], slow: bool = False):
    """대기 묶음을 지정한 대상대로 등록하는 작업을 시작합니다. slow면 천천히(서비스에 디스크를 양보하며) 진행합니다."""
    job = _stage_call(staging.start_commit, stage_id, slow)
    logger.info(f"import commit {job.id} by {admin.id}: stage {stage_id} {job.name}")
    return job.json()


@router.delete("/api/admin/import/stages/{stage_id}")
def discard_stage(stage_id: str, admin: Annotated[User, Depends(web_admin)]):
    """대기 묶음을 버립니다(풀어 둔 파일 삭제)."""
    _stage_call(staging.discard, stage_id)
    logger.info(f"import stage {stage_id} discarded by {admin.id}")
    return {"ok": True}


@router.get("/api/admin/import/jobs")
def list_jobs(admin: Annotated[User, Depends(web_admin)]):
    return [job.json() for job in importer.list_jobs()]


class JobSpeed(BaseModel):
    slow: bool


@router.put("/api/admin/import/jobs/{job_id}/speed")
def set_job_speed(job_id: str, body: JobSpeed, admin: Annotated[User, Depends(web_admin)]):
    """돌고 있는 작업을 천천히/보통 속도로 바꿉니다."""
    job = importer.set_slow(job_id, body.slow)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    logger.info(f"import job {job.id} speed by {admin.id}: {'slow' if body.slow else 'normal'}")
    return job.json()


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
