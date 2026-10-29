"""난이도표. 관리자가 `/admin/tables`에서 등록하고, 클라이언트는 `GET /api/pre/tables`로 사전 청크마다 붙은
난이도표 태그를 받아 고른 난이도표의 곡이 든 사전 청크만 받습니다.

난이도표는 beatoraja(jbmstable-parser의 DifficultyTableParser)와 같은 방식으로 읽습니다.
1. 등록한 주소가 `.json`으로 끝나면 그 주소가 헤더 JSON입니다. 아니면 HTML을 받아
   `<meta name="bmstable" content="...">`가 가리키는 헤더 JSON 주소를 찾습니다(HTML 주소 기준 상대 경로).
2. 헤더 JSON에는 `name`·`symbol`(문자열, 필수)과 `data_url`(문자열 또는 문자열 목록, 헤더 주소 기준 상대 경로)이 있습니다.
   `data_rule`(data_url마다 {레벨: 바꿀 레벨})에서 바꿀 레벨이 ''인 레벨의 차트는 뺍니다.
3. 데이터 JSON은 차트 목록이고, `level`이 있고 `md5`나 `sha256`이 25자 이상인 항목만 씁니다.
   `title`·`mode`·`md5`·`sha256`이 문자열이 아닌 항목은 건너뜁니다. 여러 data_url에 같은 차트가 있으면 처음 것만 둡니다.

곡과는 차트의 md5(chart.md5) 또는 sha256(chart.id)으로 맞춥니다. 사전 청크 하나에 난이도표의 곡이 하나라도 있으면
그 청크에 그 난이도표 태그가 붙습니다. 태그는 요청할 때 계산하므로 곡이나 난이도표가 바뀌어도 바로 맞습니다.
사전 청크는 속한 난이도표 조합이 같은 곡끼리 묶으므로(pre_rebalance.py) 표를 더하거나 다시 읽으면 뒤에서 곡을 옮깁니다.
"""
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from . import auth, constant, db, pre_rebalance
from .accounts import User
from .admin import _forbidden, web_admin
from .auth import current_user
from .templating import templates

router = APIRouter()
logger = logging.getLogger(__name__)

_META_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_HEX = re.compile(r"[0-9a-f]+")
# 표 하나를 읽는 동안 바뀌지 않도록 갱신은 한 번에 하나씩 합니다.
_refresh_lock = threading.Lock()

COLUMNS = ("id", "url", "name", "symbol", "header_url", "charts", "fetched_at", "error", "created_at")


class TableError(ValueError):
    """난이도표를 읽지 못했습니다. 메시지는 관리자 페이지에 그대로 보여줍니다."""


def http_get(url: str) -> bytes:
    """주소의 본문. 크기는 TABLE_MAX_BYTES까지만 받습니다."""
    if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
        raise TableError(f"http(s) 주소가 아닙니다: {url}")
    # User-Agent가 없으면 403을 주는 사이트가 있습니다.
    req = urllib.request.Request(url, headers={"User-Agent": "ebms-server (difficulty table)"})
    try:
        with urllib.request.urlopen(req, timeout=constant.TABLE_FETCH_TIMEOUT) as resp:
            data = resp.read(constant.TABLE_MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        raise TableError(f"{url}: HTTP {e.code}") from e
    except (urllib.error.URLError, OSError) as e:
        raise TableError(f"{url}: {getattr(e, 'reason', e)}") from e
    if len(data) > constant.TABLE_MAX_BYTES:
        raise TableError(f"{url}: {constant.TABLE_MAX_BYTES} 바이트보다 큽니다")
    return data


def _attr(tag: str, name: str) -> str | None:
    m = re.search(r"(?:^|\s)" + name + r"\s*=\s*([\"'])(.*?)\1", tag, re.IGNORECASE | re.DOTALL)
    return m.group(2) if m else None


def find_header_url(html: str, page_url: str) -> str | None:
    """HTML의 `<meta name="bmstable" content="...">`가 가리키는 헤더 JSON 주소. 여러 개면 마지막 것(beatoraja와 같음)."""
    found = None
    for tag in _META_RE.findall(html):
        name = _attr(tag, "name")
        content = _attr(tag, "content")
        if name is not None and name.lower() == "bmstable" and content:
            found = content.strip()
    return urllib.parse.urljoin(page_url, found) if found else None


def _json(data: bytes, url: str):
    try:
        return json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise TableError(f"{url}: JSON이 아닙니다 ({e})") from e


def _hash(value, length: int) -> str:
    """정해진 길이의 16진수 해시만 소문자로. 아니면 ''."""
    if not isinstance(value, str):
        return ""
    value = value.strip().lower()
    return value if len(value) == length and _HEX.fullmatch(value) else ""


def parse_table(url: str, get: Callable[[str], bytes] | None = None) -> dict:
    """난이도표를 읽어 {name, symbol, header_url, charts: [(md5, sha256, level), ...]}로 반환합니다.
    읽지 못하면 TableError."""
    get = get or http_get
    url = url.strip()
    if urllib.parse.urlsplit(url).path.lower().endswith(".json"):
        header_url = url
    else:
        html = get(url).decode("utf-8", errors="replace")
        header_url = find_header_url(html, url)
        if header_url is None:
            raise TableError(f'{url}: <meta name="bmstable"> 태그가 없습니다')

    header = _json(get(header_url), header_url)
    if not isinstance(header, dict) or not isinstance(header.get("name"), str) or not isinstance(header.get("symbol"), str):
        raise TableError(f"{header_url}: 헤더에 name·symbol이 없습니다")
    data_url = header.get("data_url")
    if isinstance(data_url, str):
        data_urls = [data_url]
    elif isinstance(data_url, list):
        data_urls = [u for u in data_url if isinstance(u, str)]
    else:
        data_urls = []
    rules = header.get("data_rule") if isinstance(header.get("data_rule"), list) else []

    charts: list[tuple[str, str, str]] = []
    seen_md5: set[str] = set()
    seen_sha256: set[str] = set()
    loaded = 0
    errors = []
    for i, data_url in enumerate(data_urls):
        if not data_url.strip():
            continue
        rule = rules[i] if i < len(rules) and isinstance(rules[i], dict) else {}
        full = urllib.parse.urljoin(header_url, data_url.strip())
        try:
            rows = _json(get(full), full)
        except TableError as e:
            # beatoraja처럼 읽지 못한 데이터는 건너뛰고, 하나도 못 읽었을 때만 실패로 봅니다.
            errors.append(str(e))
            continue
        if not isinstance(rows, list):
            errors.append(f"{full}: 데이터가 배열이 아닙니다")
            continue
        loaded += 1
        for row in rows:
            if not isinstance(row, dict) or any(
                row.get(k) is not None and not isinstance(row.get(k), str) for k in ("title", "mode", "md5", "sha256")
            ):
                continue
            md5, sha256 = row.get("md5"), row.get("sha256")
            if row.get("level") is None or not (
                (md5 is not None and len(md5) > 24) or (sha256 is not None and len(sha256) > 24)
            ):
                continue
            level = str(row["level"])
            if isinstance(rule.get(level), str):
                if not rule[level]:
                    continue
                level = rule[level]
            md5, sha256 = _hash(md5, 32), _hash(sha256, 64)
            if not md5 and not sha256:
                continue
            if (md5 and md5 in seen_md5) or (sha256 and sha256 in seen_sha256):
                continue
            seen_md5.add(md5)
            seen_sha256.add(sha256)
            charts.append((md5, sha256, level[:64]))
    if loaded == 0:
        raise TableError("; ".join(errors) or f"{header_url}: data_url이 없습니다")
    return {
        "name": header["name"].strip()[:255],
        "symbol": header["symbol"].strip()[:64],
        "header_url": header_url,
        "charts": charts,
    }


# ---- DB ----

def _row(row) -> dict:
    return dict(zip(COLUMNS, row))


def list_tables() -> list[dict]:
    """등록한 난이도표. 등록한 순. 각 표에 서버에 있는 차트 수(matched_charts)·곡 수(songs)를 붙입니다."""
    with db.connect() as con, con.cursor() as cur:
        cur.execute(f"SELECT {', '.join(COLUMNS)} FROM difficulty_table ORDER BY id")
        tables = [_row(row) for row in cur.fetchall()]
        songs, charts = _matches(cur)
    for t in tables:
        t["matched_charts"] = len(charts.get(t["id"], ()))
        t["songs"] = len(songs.get(t["id"], ()))
    return tables


def get_table(table_id: int) -> dict:
    with db.connect() as con, con.cursor() as cur:
        cur.execute(f"SELECT {', '.join(COLUMNS)} FROM difficulty_table WHERE id = %s", (table_id,))
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="table not found")
    return _row(row)


def _matches(cur) -> tuple[dict[int, set[int]], dict[int, set[tuple[str, str]]]]:
    """({표 id: 곡 id들}, {표 id: 서버에 있는 표의 차트(md5, sha256)들}). 지운 곡의 차트는 뺍니다."""
    songs: dict[int, set[int]] = defaultdict(set)
    charts: dict[int, set[tuple[str, str]]] = defaultdict(set)
    for column, key in (("md5", "md5"), ("id", "sha256")):
        cur.execute(
            f"""
            SELECT t.table_id, t.md5, t.sha256, c.song_id FROM difficulty_table_chart t
            JOIN chart c ON c.{column} = t.{key}
            WHERE t.{key} <> '' AND c.song_id IS NOT NULL
            """
        )
        for table_id, md5, sha256, song_id in cur.fetchall():
            songs[table_id].add(song_id)
            charts[table_id].add((md5, sha256))
    return songs, charts


def chunk_tags() -> dict[int, list[int]]:
    """{사전 청크 id: 그 청크에 곡이 있는 난이도표 id들}. 난이도표의 곡이 없는 청크는 빠집니다."""
    with db.connect() as con, con.cursor() as cur:
        songs, _ = _matches(cur)
        cur.execute("SELECT id, pre_chunk FROM song WHERE pre_chunk IS NOT NULL")
        chunk_of = dict(cur.fetchall())
    tags: dict[int, set[int]] = defaultdict(set)
    for table_id, song_ids in songs.items():
        for song_id in song_ids:
            if song_id in chunk_of:
                tags[chunk_of[song_id]].add(table_id)
    return {chunk: sorted(ids) for chunk, ids in sorted(tags.items())}


def _store(table_id: int, table: dict) -> None:
    with db.connect() as con, con.cursor() as cur:
        try:
            cur.execute("DELETE FROM difficulty_table_chart WHERE table_id = %s", (table_id,))
            for md5, sha256, level in table["charts"]:
                cur.execute(
                    "INSERT INTO difficulty_table_chart (table_id, md5, sha256, level) VALUES (%s, %s, %s, %s)",
                    (table_id, md5, sha256, level),
                )
            cur.execute(
                """
                UPDATE difficulty_table SET name = %s, symbol = %s, header_url = %s, charts = %s, fetched_at = %s,
                    error = NULL
                WHERE id = %s
                """,
                (table["name"], table["symbol"], table["header_url"], len(table["charts"]), int(time.time()), table_id),
            )
            con.commit()
        except Exception:
            con.rollback()
            raise


def refresh_table(table_id: int, get: Callable[[str], bytes] | None = None) -> dict:
    """난이도표를 다시 읽어 차트 목록을 바꿉니다. 읽지 못하면 이전 목록을 두고 error만 남깁니다."""
    url = get_table(table_id)["url"]
    with _refresh_lock:
        try:
            table = parse_table(url, get)
        except TableError as e:
            logger.warning(f"difficulty table {table_id}: refresh failed: {e}")
            with db.connect() as con, con.cursor() as cur:
                cur.execute("UPDATE difficulty_table SET error = %s WHERE id = %s", (str(e)[:2000], table_id))
                con.commit()
        else:
            _store(table_id, table)
            logger.info(f"difficulty table {table_id}: {table['name']} with {len(table['charts'])} charts")
            pre_rebalance.kick()
    return get_table(table_id)


def add_table(url: str, get: Callable[[str], bytes] | None = None) -> dict:
    """난이도표를 읽어 등록합니다. 같은 주소가 있거나 읽지 못하면 등록하지 않습니다."""
    url = url.strip()
    if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
        raise TableError("http(s) 주소를 넣어 주세요")
    with db.connect() as con, con.cursor() as cur:
        cur.execute("SELECT id FROM difficulty_table WHERE url = %s", (url,))
        if cur.fetchone() is not None:
            raise TableError("이미 등록한 주소입니다")
    with _refresh_lock:
        table = parse_table(url, get)
        with db.connect() as con, con.cursor() as cur:
            cur.execute(
                "INSERT INTO difficulty_table (url, created_at) VALUES (%s, %s)", (url, int(time.time()))
            )
            table_id = cur.lastrowid
            con.commit()
        _store(table_id, table)
    pre_rebalance.kick()
    return get_table(table_id)


def delete_table(table_id: int) -> None:
    get_table(table_id)
    with db.connect() as con, con.cursor() as cur:
        cur.execute("DELETE FROM difficulty_table_chart WHERE table_id = %s", (table_id,))
        cur.execute("DELETE FROM difficulty_table WHERE id = %s", (table_id,))
        con.commit()
    pre_rebalance.kick()


def refresh_all(get: Callable[[str], bytes] | None = None) -> None:
    with db.connect() as con, con.cursor() as cur:
        cur.execute("SELECT id FROM difficulty_table ORDER BY id")
        ids = [row[0] for row in cur.fetchall()]
    for table_id in ids:
        try:
            refresh_table(table_id, get)
        except Exception:
            logger.exception(f"difficulty table {table_id}: refresh failed")


def start() -> threading.Event | None:
    """TABLE_REFRESH_HOURS마다 모든 난이도표를 다시 읽는 스레드를 띄웁니다. 0이면 띄우지 않습니다."""
    if constant.TABLE_REFRESH_HOURS <= 0:
        return None
    stop = threading.Event()

    def loop():
        # 시작 직후에는 곡 임포트·백필이 돌므로 한 주기 뒤부터 읽습니다(등록할 때 이미 읽었음).
        while not stop.wait(constant.TABLE_REFRESH_HOURS * 3600):
            refresh_all()

    threading.Thread(target=loop, name="difficulty_tables", daemon=True).start()
    return stop


# ---- 클라이언트 API ----

@router.get("/api/pre/tables")
def pre_tables(user: Annotated[User, Depends(current_user)]):
    """난이도표 목록과 사전 청크별 난이도표 태그. 클라이언트는 고른 표의 태그가 붙은 사전 청크만 받습니다."""
    tables = list_tables()
    return {
        "tables": [
            {k: t[k] for k in ("id", "name", "symbol", "url", "charts", "matched_charts", "songs", "fetched_at")}
            for t in tables
        ],
        "chunks": chunk_tags(),
    }


# ---- 관리자 ----

class TableBody(BaseModel):
    url: str = Field(min_length=1, max_length=1024)


@router.get("/admin/tables", response_class=HTMLResponse)
def tables_page(
    request: Request,
    session: Annotated[tuple[User, int] | None, Depends(auth.optional_session)],
):
    if session is None:
        return auth._login_redirect(request)
    if not session[0].is_admin:
        return _forbidden(request)
    return templates.TemplateResponse(
        request=request,
        name="pages/admin_tables.html",
        context={"user": session[0], "tables": list_tables(), "refresh_hours": constant.TABLE_REFRESH_HOURS},
    )


@router.get("/api/admin/tables")
def admin_list(admin: Annotated[User, Depends(web_admin)]):
    return list_tables()


@router.post("/api/admin/tables")
async def admin_create(body: TableBody, admin: Annotated[User, Depends(web_admin)]):
    try:
        table = await run_in_threadpool(add_table, body.url)
    except TableError as e:
        raise HTTPException(status_code=400, detail=str(e))
    logger.info(f"difficulty table {table['id']}: added {table['name']} ({table['url']}) by {admin.id}")
    return table


@router.post("/api/admin/tables/{table_id}/refresh")
async def admin_refresh(table_id: int, admin: Annotated[User, Depends(web_admin)]):
    return await run_in_threadpool(refresh_table, table_id)


@router.delete("/api/admin/tables/{table_id}")
def admin_delete(table_id: int, admin: Annotated[User, Depends(web_admin)]):
    delete_table(table_id)
    logger.info(f"difficulty table {table_id}: deleted by {admin.id}")
    return {"ok": True}
