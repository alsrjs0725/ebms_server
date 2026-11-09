"""모든 곡의 차트를 날마다 일정 시간씩 렌더해 지문 저장소(fingerprint)에 넣으며, 다른 곡 번호의 같은 곡(중복)과
한 곡 안에 섞인 다른 곡을 찾아 관리자 목록에 쌓습니다. 자동으로 고치지는 않습니다.

후보 찾기·렌더 일치율은 fingerprint.search가 합니다. 곡이 다른 차트끼리는 렌더 일치율만 봅니다(키음 이름은
곡마다 01.ogg처럼 겹치므로 순서 일치율은 쓰지 않음). 곡 zip이 바뀌면(sha256) 그 곡을 다시 색인하고, 없어진 곡은 지웁니다.
발견(finding) 표는 저장소와 같은 SQLite 파일에 둡니다.
"""
import json
import logging
import os
import resource
import sqlite3
import threading
import time
import zipfile

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from . import auth, constant, db, fingerprint, render_match
from .accounts import User
from .admin import _forbidden, web_admin
from .templating import templates

logger = logging.getLogger(__name__)

NICE = 10
# 한 번에 렌더하는 차트 수(같은 키음을 한 번만 풂). 결과는 바로 줄여 저장할 형태로 바꿉니다.
RENDER_PART = 16
# 최대 메모리가 이만큼 늘 때마다 로그에 곡을 남깁니다.
MEMORY_LOG_STEP_KB = 512 * 1024
_peak_kb = 0
# 같은 곡에서 색인 회차가 이만큼 거듭 죽으면 그 곡을 실패로 표시합니다.
CRASH_STRIKES = 2


# ---- 발견 ----

# 지문 저장소(fingerprint)와 같은 파일에 둡니다.
_SCHEMA = """
-- kind: dup(곡 a·b가 같은 곡) / mixed(곡 a 안에 다른 곡 차트가 섞임, b = 0). status: open / dismissed
CREATE TABLE IF NOT EXISTS finding(
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    song_a INTEGER NOT NULL,
    song_b INTEGER NOT NULL,
    score REAL,
    detail TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    UNIQUE (kind, song_a, song_b)
);
CREATE INDEX IF NOT EXISTS finding_status ON finding(status, created_at);
"""


def _path():
    return fingerprint.db_path()


def connect() -> sqlite3.Connection:
    return fingerprint.connect(_SCHEMA)


def _put_finding(con: sqlite3.Connection, kind: str, a: int, b: int, score: float | None, detail: dict) -> None:
    con.execute(
        "INSERT INTO finding(kind, song_a, song_b, score, detail, created_at) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(kind, song_a, song_b) DO UPDATE SET score = excluded.score, detail = excluded.detail, "
        "created_at = excluded.created_at WHERE finding.status = 'open'",
        (kind, a, b, score, json.dumps(detail, ensure_ascii=False), int(time.time())),
    )


def _forget_song(con: sqlite3.Connection, song_id: int) -> None:
    """곡의 색인과 열린 발견을 지웁니다(해시는 compact가 지움). 넘긴(dismissed) 발견은 남겨 다시 띄우지 않습니다."""
    fingerprint.forget_song(con, song_id)
    con.execute("DELETE FROM finding WHERE status = 'open' AND (song_a = ? OR song_b = ?)", (song_id, song_id))


def forget_songs(song_ids: list[int]) -> None:
    """곡을 나누거나 합친 뒤 그 곡들의 색인과 열린 발견을 지웁니다. 다음 색인 회차에 새 곡처럼 먼저 다시 색인합니다."""
    if not _path().exists():
        return
    con = connect()
    # 색인이 쓰는 중이면 오래 기다리지 않습니다. 실패해도 바뀐 곡의 발견은 감춰지고(_skip_stale) 다음 회차가 다시 색인합니다.
    con.execute("PRAGMA busy_timeout = 5000")
    try:
        for song_id in song_ids:
            _forget_song(con, song_id)
        con.commit()
    except sqlite3.Error:
        logger.warning(f"render index forget skipped (index busy)[songs {song_ids}]")
    finally:
        con.close()

def _song_row(song_id: int) -> tuple[list[dict], db.StoreFile | None]:
    """곡 zip의 파일 목록과 연 저장소 파일. 색인 프로세스에서 db.Database()(시작 작업이 무거움)를 만들지 않으려고 직접 읽습니다."""
    with db.connect() as con, con.cursor() as cur:
        cur.execute("SELECT files, size, sha256, path FROM song WHERE id = %s", (song_id,))
        row = cur.fetchone()
    if row is None:
        return [], None
    files = json.loads(row[0]) if row[0] else []
    try:
        return files, db.StoreFile(row[1], row[2], row[3])
    except FileNotFoundError:
        return files, None


def index_song(con: sqlite3.Connection, song_id: int, sha256: str) -> dict:
    """곡 하나를 (다시) 색인하고 그 곡이 낀 중복·섞임을 찾습니다.
    렌더(곡 하나에 수 분)는 쓰기 전에 끝내고, 다른 곡 찾기도 쓰기 전에 하고, 색인 파일 쓰기 잠금은 저장하는 짧은 동안만 잡습니다.
    (렌더 중 잠금을 잡으면 나누기·합치기 뒤 forget_songs, 임포트 지문 저장이 60초 기다리다 실패합니다.)"""
    files, file = _song_row(song_id)
    paths = [f["path"] for f in files if f["path"].lower().endswith(constant.BMS_FORMAT)]
    if paths and file is None:
        raise FileNotFoundError(f"song {song_id} file")
    # 렌더한 특징은 차트마다 바로 저장할 형태(압축 blob + 해시)로 줄이고, 다른 곡 찾기·곡 안 묶기도 그 자리에서 한 뒤 버립니다.
    # 곡 전체의 렌더 결과(분기마다 전체 길이 특징)를 모아 두면 차트가 많은 곡에서 메모리가 10GB를 넘어 OOM으로 죽습니다.
    # 다른 곡 찾기는 읽기만 하므로(흔한 해시를 많이 읽어 느림) 쓰기 전에 합니다. 같은 곡 차트는 search가 빼므로 순서와 무관.
    groups: list[tuple[render_match.Fingerprint, list[str]]] = []  # (대표 차트 특징, 같은 묶음 경로들)
    dups: dict[int, dict] = {}
    prepared: dict[str, tuple[bytes, None, list[tuple[int, int]]]] = {}  # 차트 경로 → 저장할 (blob, _, 해시)
    if file is not None and paths:
        with os.fdopen(os.dup(file._fd), "rb") as raw, zipfile.ZipFile(raw) as zf:
            file.close()
            names = [i.filename for i in zf.infolist() if not i.is_dir()]
            sizes = {i.filename: i.file_size for i in zf.infolist()}
            huge = [p for p in paths if sizes.get(p, 0) > render_match.MAX_CHART_BYTES]
            if huge:
                # 읽지도 않고 건너뜁니다(render_match.MAX_CHART_BYTES).
                logger.warning(f"render index: skipped huge charts[song {song_id}]: {huge}")
                paths = [p for p in paths if p not in huge]
            for start in range(0, len(paths), RENDER_PART):
                part = paths[start : start + RENDER_PART]
                fps = render_match.fingerprints(names, zf.read, part, nice=True)
                for path in part:
                    fp = fps.pop(path, None)
                    ready = fingerprint.prepare(fp.features if fp else [])
                    if ready is None:
                        continue
                    blob, feats, query = ready
                    prepared[path] = (blob, None, query)
                    found = lambda other: other in dups and dups[other]["score"] >= render_match.SAME
                    for other_song, other_path, score in fingerprint.search(con, feats, song_id, query, skip=found):
                        if score is not None and score >= render_match.SAME and not found(other_song):
                            dups[other_song] = {"score": score, "chart": path, "other_chart": other_path}
                    _group(groups, path, fp)
                del fps
    _log_memory(song_id, len(paths))
    con.commit()  # 읽기 트랜잭션을 닫고 아래 쓰기만 짧게
    _forget_song(con, song_id)
    for path, ready in prepared.items():
        fingerprint.store_prepared(con, song_id, path, ready)
    stored = len(prepared)
    for other, d in dups.items():
        a, b = sorted((song_id, other))
        _put_finding(con, "dup", a, b, round(d["score"], 3), d if a == song_id else
                     {"score": d["score"], "chart": d["other_chart"], "other_chart": d["chart"]})
    if len(groups) > 1:
        _put_finding(con, "mixed", song_id, 0, None, {"groups": [g[1] for g in groups]})
    fingerprint.mark_song(con, song_id, sha256, stored)
    con.commit()
    return {"song_id": song_id, "charts": stored, "dups": sorted(dups), "groups": len(groups)}


def _log_memory(song_id: int, charts: int) -> None:
    """색인 프로세스의 최대 메모리가 MEMORY_LOG_STEP 넘게 늘면 그 곡을 남깁니다(OOM 원인 곡 찾기)."""
    global _peak_kb
    try:
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss  # Linux: KB
    except (AttributeError, OSError):
        return
    if peak - _peak_kb >= MEMORY_LOG_STEP_KB:
        logger.warning(f"render index memory peak {peak // 1024} MB[song {song_id}, {charts} charts]")
        _peak_kb = peak


def _group(groups: list, path: str, fp: render_match.Fingerprint) -> None:
    """곡 안 묶기: 대표와 '다름'이 아니면 같은 묶음에 넣습니다(애매하면 섞임으로 띄우지 않음).
    같은 곡 안이라 키음 파일이 같으므로 순서 일치율도 봅니다."""
    best, best_value = None, None
    for g in groups:
        verdict, render, order = render_match.same_song(g[0], fp)
        if verdict == "different":
            continue
        value = max((v for v in (render, order) if v is not None), default=0.0)
        if best is None or value > best_value:
            best, best_value = g, value
    if best is not None:
        best[1].append(path)
    else:
        # 대표는 색인에 저장하는 분기 수만 들고 있습니다(분기가 많은 차트의 전체 길이 특징을 묶음마다 쥐지 않게).
        keep = fingerprint.MAX_STORED_VARIANTS
        rep = render_match.Fingerprint(fp.features[:keep], fp.orders[:keep], fp.truncated, fp.sounds)
        groups.append((rep, [path]))


def _live_songs() -> dict[int, str]:
    with db.connect() as con, con.cursor() as cur:
        cur.execute("SELECT id, sha256 FROM song")
        return {row[0]: row[1] for row in cur.fetchall()}


def _crashed_song(con: sqlite3.Connection) -> int | None:
    """지난 회차가 끝나지 않은 채(finished_at 없음) 색인하던 곡. 정상으로 끝났거나 기록이 없으면 None."""
    row = con.execute("SELECT value FROM meta WHERE key = 'run'").fetchone()
    state = json.loads(row[0]) if row else {}
    if state.get("started_at") and not state.get("finished_at") and isinstance(state.get("song"), int):
        return state["song"]
    return None


def _strike(con: sqlite3.Connection, song_id: int) -> int:
    """song_id에서 회차가 죽은 횟수를 하나 늘려 돌려줍니다(다른 곡이면 1부터)."""
    row = con.execute("SELECT value FROM meta WHERE key = 'crash'").fetchone()
    crash = json.loads(row[0]) if row else {}
    count = crash.get("count", 0) + 1 if crash.get("song") == song_id else 1
    con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('crash', ?)", (json.dumps({"song": song_id, "count": count}),))
    con.commit()
    return count


def _save_state(con: sqlite3.Connection, **values) -> None:
    """진행 상황은 색인 파일(meta)에 둡니다. 색인은 별도 프로세스에서 돌고 웹 서버가 읽습니다."""
    row = con.execute("SELECT value FROM meta WHERE key = 'run'").fetchone()
    state = json.loads(row[0]) if row else {}
    state.update(values)
    con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('run', ?)", (json.dumps(state),))
    con.commit()


def run(minutes: float) -> bool:
    """색인 안 된 곡(번호 순), 그다음 내용이 바뀐 곡을 minutes 동안 색인합니다. 할 일이 남았으면 True."""
    started = time.time()
    deadline = time.monotonic() + minutes * 60
    con = connect()
    left = False
    try:
        crashed = _crashed_song(con)
        _save_state(con, started_at=int(started), finished_at=None, songs=0, song=None, error=None)
        try:
            live = _live_songs()
            known = fingerprint.songs(con)
            if crashed is not None and crashed in live and _strike(con, crashed) >= CRASH_STRIKES:
                # 이 곡을 색인하다 끝내지 못하고 죽은 회차가 CRASH_STRIKES번입니다(OOM 등 SIGKILL이라 실패 표시도 못 남김).
                # 한 번은 재배포·재시작일 수 있어 다시 해 보고, 거듭 죽으면 같은 내용으로는 다시 하지 않도록 실패로 표시합니다.
                logger.error(f"render index: runs died on song {crashed} {CRASH_STRIKES} times; marked as failed")
                _forget_song(con, crashed)
                fingerprint.mark_song(con, crashed, live[crashed], -1)
                con.commit()
                known = fingerprint.songs(con)
            gone = set(known) - set(live)
            for song_id in gone:
                _forget_song(con, song_id)
            con.commit()
            changed = sorted(s for s in live if s in known and known[s] != live[s])
            todo = sorted(s for s in live if s not in known) + changed
            done = 0
            for song_id in todo:
                if time.monotonic() > deadline:
                    left = True
                    break
                _save_state(con, song=song_id, songs=done)
                try:
                    index_song(con, song_id, live[song_id])
                except Exception:
                    con.rollback()
                    logger.exception(f"render index failed[song {song_id}]")
                    # 같은 내용으로는 다시 하지 않습니다(곡이 바뀌면 다시 색인).
                    _forget_song(con, song_id)
                    fingerprint.mark_song(con, song_id, live[song_id], -1)
                    con.commit()
                done += 1
            if gone or changed:
                fingerprint.compact(con)
            _save_state(con, songs=done, song=None, finished_at=int(time.time()), left=left)
            logger.info(f"render index: {done} songs in {int(time.time() - started)}s, left={left}")
        except Exception as e:
            logger.exception("render index run failed")
            _save_state(con, song=None, finished_at=int(time.time()), error=str(e) or type(e).__name__)
    finally:
        con.close()
    return left


def _cpus() -> set[int] | None:
    """색인 프로세스를 붙일 코어. RENDER_INDEX_CPUS("3", "2,3", "none")가 비면 마지막 코어 하나."""
    value = constant.RENDER_INDEX_CPUS.strip().lower()
    if value == "none":
        return None
    try:
        available = os.sched_getaffinity(0)
    except (AttributeError, OSError):
        return None
    if not value:
        return {max(available)} if len(available) > 1 else None
    wanted = {int(x) for x in value.split(",") if x.strip()}
    return (wanted & available) or None


class _Run:
    """띄운 색인 프로세스 하나(python -m ebms_server.render_index_worker). 프로세스의 stdout·stderr를 모두 받아
    웹 서버 로거로 다시 남기므로(ebms.log → 관리자 로그 화면) 로그 설정 전에 죽어도 traceback이 남습니다.
    done이 서면 끝났고, left는 할 일이 남았는지."""

    def __init__(self, minutes: float):
        import subprocess
        import sys

        env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "ebms_server.render_index_worker", str(minutes)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=env, text=True, encoding="utf-8", errors="replace",
        )
        self.left = False
        self.done = threading.Event()
        threading.Thread(target=self._pump, name="render_index_pump", daemon=True).start()

    def _pump(self) -> None:
        for level, name, message in parse_output(self.proc.stdout):
            if name == RESULT:
                self.left = message == "1"
            else:
                logging.getLogger(name).log(level, message)
        code = self.proc.wait()
        if code != 0:
            logger.error(f"render index process exited with {code}")
        self.done.set()


RESULT = "result"
NEWLINE = "\x1f"  # 색인 프로세스 로그 한 줄 안의 줄바꿈
_LEVELS = {name: getattr(logging, name) for name in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")}


def parse_output(lines):
    """색인 프로세스 출력 → (수준, 로거 이름, 메시지). 로그 줄("수준|로거|메시지")은 받는 대로 바로 내고,
    로그 설정 전에 stderr로 나온 줄(traceback 등)은 다음 로그 줄이나 끝까지 모아 ERROR 하나로 냅니다."""
    raw_lines: list[str] = []
    for raw in lines:
        line = raw.rstrip("\n")
        level, sep, rest = line.partition("|")
        if sep and level in _LEVELS and "|" in rest:
            if raw_lines:
                yield logging.ERROR, __name__ + ".process", "\n".join(raw_lines)
                raw_lines = []
            name, _, message = rest.partition("|")
            yield _LEVELS[level], name, message.replace(NEWLINE, "\n")
        elif line.strip() or raw_lines:
            raw_lines.append(line)
    if raw_lines:
        yield logging.ERROR, __name__ + ".process", "\n".join(raw_lines)


_proc_lock = threading.Lock()
_current: _Run | None = None  # 돌고 있는 색인 프로세스


def _spawn(minutes: float) -> _Run | None:
    """색인 프로세스를 띄웁니다. 이미 돌고 있으면 None."""
    global _current
    with _proc_lock:
        if _current is not None and not _current.done.is_set():
            return None
        _current = _Run(minutes)
        return _current


def running() -> bool:
    return _current is not None and not _current.done.is_set()


def status_() -> dict:
    out = {"running": running()}
    if _path().exists():
        con = connect()
        try:
            row = con.execute("SELECT value FROM meta WHERE key = 'run'").fetchone()
            out.update(json.loads(row[0]) if row else {})
            out["indexed_songs"] = con.execute("SELECT COUNT(*) FROM song").fetchone()[0]
            out["indexed_charts"] = con.execute("SELECT COUNT(*) FROM chart").fetchone()[0]
            sql, args = _skip_stale(con, "SELECT COUNT(*) FROM finding WHERE status = 'open'", [])
            out["open_findings"] = con.execute(sql, args).fetchone()[0]
        finally:
            con.close()
    if not out["running"]:
        out["song"] = None
    out["interval_hours"] = constant.RENDER_INDEX_HOURS
    out["minutes"] = constant.RENDER_INDEX_MINUTES
    return out


def start_now(minutes: float) -> dict:
    _spawn(minutes)
    return status_()


def start() -> threading.Event | None:
    """상시 색인: 할 일이 남아 있으면 RENDER_INDEX_MINUTES분씩 쉬지 않고 이어 돌고, 다 보면 RENDER_INDEX_HOURS 뒤에
    새 곡·바뀐 곡을 다시 봅니다. 둘 중 하나가 0이면 띄우지 않습니다(관리자 "지금 색인"만).
    서버가 막 켜졌을 때는 백필·복구가 돌므로 RENDER_INDEX_START_DELAY 뒤에 처음 돕니다."""
    if constant.RENDER_INDEX_HOURS <= 0 or constant.RENDER_INDEX_MINUTES <= 0:
        return None
    stop = threading.Event()

    def loop():
        if stop.wait(constant.RENDER_INDEX_START_DELAY):
            return
        while not stop.is_set():
            spawned = _spawn(constant.RENDER_INDEX_MINUTES)
            if spawned is None:  # 관리자가 띄운 것이 돌고 있음
                if stop.wait(60):
                    return
                continue
            while not spawned.done.wait(5):
                if stop.is_set():
                    spawned.proc.terminate()
                    return
            left = spawned.left
            if not left and stop.wait(constant.RENDER_INDEX_HOURS * 3600):
                return

    threading.Thread(target=loop, name="render_index_loop", daemon=True).start()
    return stop


PAGE_SIZE = 50
# 화면에는 묶음마다 앞 몇 개 경로만 보입니다(나머지는 개수만). 섞임 곡은 묶음·경로가 많아 페이지가 커집니다.
SHOWN_PATHS = 5


def findings(state: str = "open", kind: str = "", limit: int = 200, offset: int = 0) -> list[dict]:
    if not _path().exists():
        return []
    con = connect()
    try:
        sql = "SELECT id, kind, song_a, song_b, score, detail, created_at, status FROM finding WHERE status = ?"
        args: list = [state]
        if kind:
            sql += " AND kind = ?"
            args.append(kind)
        if state == "open":
            sql, args = _skip_stale(con, sql, args)
        rows = con.execute(sql + " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()
    finally:
        con.close()
    out = [
        {"id": r[0], "kind": r[1], "song_a": r[2], "song_b": r[3] or None, "score": r[4], "detail": json.loads(r[5]),
         "created_at": r[6], "status": r[7]}
        for r in rows
    ]
    folders = _folders({s for f in out for s in (f["song_a"], f["song_b"]) if s})
    for f in out:
        f["folder_a"] = folders.get(f["song_a"])
        f["folder_b"] = folders.get(f["song_b"]) if f["song_b"] else None
    return out


def count_findings(state: str = "open") -> int:
    if not _path().exists():
        return 0
    con = connect()
    try:
        sql, args = "SELECT COUNT(*) FROM finding WHERE status = ?", [state]
        if state == "open":
            sql, args = _skip_stale(con, sql, args)
        return con.execute(sql, args).fetchone()[0]
    finally:
        con.close()


def _stale_songs(con: sqlite3.Connection) -> list[int]:
    """색인한 뒤 내용이 바뀌었거나 없어진 곡. 나누기·합치기 직후 forget_songs가 색인 잠금으로 실패해도
    그 곡의 옛 발견을 감추고, 다음 색인 회차(changed)에서 다시 검사합니다."""
    known = fingerprint.songs(con)
    if not known:
        return []
    live = _live_songs()
    return sorted(s for s, sha in known.items() if live.get(s) != sha)


def _skip_stale(con: sqlite3.Connection, sql: str, args: list) -> tuple[str, list]:
    stale = _stale_songs(con)
    if not stale or len(stale) > 10000:  # SQLite 인자 수 한도
        return sql, args
    marks = ", ".join("?" * len(stale))
    sql += f" AND song_a NOT IN ({marks}) AND song_b NOT IN ({marks})"
    return sql, [*args, *stale, *stale]


def _folders(song_ids: set[int]) -> dict[int, str]:
    if not song_ids:
        return {}
    ids = sorted(song_ids)
    with db.connect() as con, con.cursor() as cur:
        cur.execute(f"SELECT id, folder FROM song WHERE id IN ({', '.join(['%s'] * len(ids))})", ids)
        return {row[0]: row[1] for row in cur.fetchall()}


def set_status(finding_id: int, value: str) -> bool:
    if value not in ("open", "dismissed"):
        raise ValueError("status must be open or dismissed")
    con = connect()
    try:
        n = con.execute("UPDATE finding SET status = ? WHERE id = ?", (value, finding_id)).rowcount
        con.commit()
        return n > 0
    finally:
        con.close()


# ---- 관리자 화면·API ----

router = APIRouter()


@router.get("/admin/render", response_class=HTMLResponse)
def render_page(
    request: Request,
    session: Annotated[tuple[User, int] | None, Depends(auth.optional_session)],
    status: str = "open",
    page: int = 1,
):
    if session is None:
        return auth._login_redirect(request)
    if not session[0].is_admin:
        return _forbidden(request)
    if status not in ("open", "dismissed"):
        status = "open"
    state = status_()
    total = (state.get("open_findings") or 0) if status == "open" else count_findings(status)
    pages = max(1, -(-total // PAGE_SIZE))
    page = max(1, min(page, pages))
    rows = findings(status, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE)
    for f in rows:  # 화면에 안 보일 경로는 보내지 않음
        if f["kind"] == "mixed":
            f["groups"] = [{"size": len(g), "shown": g[:SHOWN_PATHS]} for g in f["detail"].get("groups", [])]
    return templates.TemplateResponse(
        request=request,
        name="pages/admin_render.html",
        context={"user": session[0], "state": state, "findings": rows, "status": status, "page": page, "pages": pages,
                 "total": total},
    )


@router.get("/api/admin/render/index")
def api_status(admin: Annotated[User, Depends(web_admin)]):
    return status_()


class RunBody(BaseModel):
    minutes: float = Field(default=0, ge=0, le=24 * 60)


@router.post("/api/admin/render/index/run")
def api_run(body: RunBody, admin: Annotated[User, Depends(web_admin)]):
    """지금 색인을 시작합니다(minutes가 0이면 RENDER_INDEX_MINUTES). 돌고 있으면 그 상태를 돌려줍니다."""
    logger.info(f"render index run by {admin.id}: {body.minutes} min")
    return start_now(body.minutes or constant.RENDER_INDEX_MINUTES)


@router.get("/api/admin/render/findings")
def api_findings(admin: Annotated[User, Depends(web_admin)], status: str = "open", kind: str = "", limit: int = 200,
                 offset: int = 0):
    return findings(status, kind, max(1, min(limit, 1000)), max(0, offset))


class FindingBody(BaseModel):
    status: str


@router.put("/api/admin/render/findings/{finding_id}")
def api_set_finding(finding_id: int, body: FindingBody, admin: Annotated[User, Depends(web_admin)]):
    try:
        found = set_status(finding_id, body.status)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not found:
        raise HTTPException(status_code=404, detail="finding not found")
    logger.info(f"render finding {finding_id} {body.status} by {admin.id}")
    return {"ok": True}
