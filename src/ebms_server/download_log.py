"""곡 플레이 다운로드 기록. 누가·어떤 곡을·언제 받으려 했고 결과가 어땠는지 `download_log` 테이블에 남기고,
관리자 페이지(/admin/downloads)에서 사용자·곡·결과로 찾아봅니다.

로그인한 사용자의 요청만 남깁니다(프록시의 캐시 채우기 요청 제외). 프록시 뒤에서는 /api/proxy/authz가,
직접 받을 때는 /api/play/song이 남깁니다. 304(바뀐 것 없음)와 416(범위 오류)은 남기지 않습니다.
보유기간은 constant.RETENTION_DOWNLOAD_LOG_DAYS(기본 30일)이고 정리 작업(retention)이 지웁니다.
"""
import logging
import time

from fastapi import Request

from . import db

logger = logging.getLogger(__name__)

# 결과 -> 관리자 페이지 표시 이름
RESULTS = {
    "charged": "받음(티켓 차감)",
    "free": "받음(재차감 면제)",
    "no_ticket": "티켓 없음",
    "busy": "동시 다운로드 초과",
    "not_found": "없는 곡",
    "deleted": "지운 곡",
}
PAGE_SIZE = 50


def is_resume(request: Request) -> bool:
    """Range 시작이 0보다 큰 이어받기 요청이면 True."""
    value = (request.headers.get("range") or "").strip()
    if not value.startswith("bytes="):
        return False
    first = value[len("bytes="):].split("-", 1)[0].strip()
    return first.isdigit() and int(first) > 0


def failure_result(status: int, detail: str) -> str | None:
    """다운로드를 거절한 HTTP 상태를 기록할 결과로 바꿉니다. 기록하지 않을 거절이면 None."""
    if status == 404:
        return "not_found"
    if status == 410:
        return "deleted"
    if status == 429 and detail == "too many concurrent downloads":
        return "busy"
    return None


def record(user_id: str, song_id: int, result: str, resumed: bool = False) -> None:
    """한 줄을 남깁니다. 기록에 실패해도 다운로드는 막지 않습니다."""
    try:
        with db.connect() as con, con.cursor() as cur:
            cur.execute(
                "INSERT INTO download_log (user_id, song_id, at, result, resumed) VALUES (%s, %s, %s, %s, %s)",
                (user_id, song_id, int(time.time()), result, int(resumed)),
            )
            con.commit()
    except Exception:
        logger.exception(f"download log failed: user={user_id} song={song_id} result={result}")


def search(user: str = "", song_id: int | None = None, result: str = "", page: int = 1) -> dict:
    """최신순으로 쪽마다 PAGE_SIZE개. user는 계정 ID(정확히) 또는 닉네임 일부(대소문자 무시)."""
    where: list[str] = []
    params: list = []
    user = user.strip()
    if user:
        like = "%" + user.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where.append("(l.user_id = %s OR LOWER(u.display_name) LIKE %s)")
        params += [user, like]
    if song_id is not None:
        where.append("l.song_id = %s")
        params.append(song_id)
    if result:
        where.append("l.result = %s")
        params.append(result)
    cond = " WHERE " + " AND ".join(where) if where else ""
    source = "download_log l JOIN user u ON u.id = l.user_id"
    offset = (page - 1) * PAGE_SIZE
    with db.connect() as con, con.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM {source}{cond}", tuple(params))
        total = cur.fetchone()[0]
        cur.execute(
            f"""
            SELECT l.id, l.at, l.user_id, u.display_name, l.song_id,
                   COALESCE(s.folder, d.folder), d.song_id IS NOT NULL, l.result, l.resumed
            FROM {source}
            LEFT JOIN song s ON s.id = l.song_id
            LEFT JOIN deleted_song d ON d.song_id = l.song_id
            {cond}
            ORDER BY l.id DESC LIMIT {PAGE_SIZE} OFFSET {offset}
            """,
            tuple(params),
        )
        rows = cur.fetchall()
    items = [
        {
            "id": row_id, "at": at, "user_id": user_id, "display_name": name, "song_id": sid,
            "folder": folder, "song_deleted": bool(deleted), "result": res, "resumed": bool(resumed),
        }
        for row_id, at, user_id, name, sid, folder, deleted, res, resumed in rows
    ]
    return {"items": items, "total": total, "page": page, "pages": max(1, -(-total // PAGE_SIZE))}
