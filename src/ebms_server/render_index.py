"""모든 곡의 차트를 날마다 일정 시간씩 렌더해 지문 저장소(fingerprint)에 넣으며, 다른 곡 번호의 같은 곡(중복)과
한 곡 안에 섞인 다른 곡을 찾아 관리자 목록에 쌓습니다. 자동으로 고치지는 않습니다.

후보 찾기·렌더 일치율은 fingerprint.search가 합니다. 곡이 다른 차트끼리는 렌더 일치율만 봅니다(키음 이름은
곡마다 01.ogg처럼 겹치므로 순서 일치율은 쓰지 않음). 곡 zip이 바뀌면(sha256) 그 곡을 다시 색인하고, 없어진 곡은 지웁니다.
발견(finding) 표는 저장소와 같은 SQLite 파일에 둡니다.
"""
import json
import logging
import os
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
    """곡 하나를 (다시) 색인하고 그 곡이 낀 중복·섞임을 찾습니다."""
    files, file = _song_row(song_id)
    paths = [f["path"] for f in files if f["path"].lower().endswith(constant.BMS_FORMAT)]
    _forget_song(con, song_id)
    groups: list[tuple[render_match.Fingerprint, list[str]]] = []  # (대표 차트 특징, 같은 묶음 경로들)
    dups: dict[int, dict] = {}
    if paths and file is None:
        raise FileNotFoundError(f"song {song_id} file")
    stored = 0
    if file is not None and paths:
        with os.fdopen(os.dup(file._fd), "rb") as raw, zipfile.ZipFile(raw) as zf:
            file.close()
            names = [i.filename for i in zf.infolist() if not i.is_dir()]
            for start in range(0, len(paths), 16):
                part = paths[start : start + 16]
                fps = render_match.fingerprints(names, zf.read, part, nice=True)
                for path in part:
                    fp = fps.get(path)
                    put = fingerprint.store(con, song_id, path, fp.features if fp else [])
                    if put is None:
                        continue
                    _, feats, query = put
                    found = lambda other: other in dups and dups[other]["score"] >= render_match.SAME
                    for other_song, other_path, score in fingerprint.search(con, feats, song_id, query, skip=found):
                        if score is not None and score >= render_match.SAME and not found(other_song):
                            dups[other_song] = {"score": score, "chart": path, "other_chart": other_path}
                    stored += 1
                    _group(groups, path, fp)
    for other, d in dups.items():
        a, b = sorted((song_id, other))
        _put_finding(con, "dup", a, b, round(d["score"], 3), d if a == song_id else
                     {"score": d["score"], "chart": d["other_chart"], "other_chart": d["chart"]})
    if len(groups) > 1:
        _put_finding(con, "mixed", song_id, 0, None, {"groups": [g[1] for g in groups]})
    fingerprint.mark_song(con, song_id, sha256, stored)
    con.commit()
    return {"song_id": song_id, "charts": stored, "dups": sorted(dups), "groups": len(groups)}


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
        groups.append((fp, [path]))


def _live_songs() -> dict[int, str]:
    with db.connect() as con, con.cursor() as cur:
        cur.execute("SELECT id, sha256 FROM song")
        return {row[0]: row[1] for row in cur.fetchall()}


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
        _save_state(con, started_at=int(started), finished_at=None, songs=0, song=None, error=None)
        try:
            live = _live_songs()
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
            out["open_findings"] = con.execute("SELECT COUNT(*) FROM finding WHERE status = 'open'").fetchone()[0]
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


def findings(state: str = "open", kind: str = "", limit: int = 200) -> list[dict]:
    if not _path().exists():
        return []
    con = connect()
    try:
        sql = "SELECT id, kind, song_a, song_b, score, detail, created_at, status FROM finding WHERE status = ?"
        args: list = [state]
        if kind:
            sql += " AND kind = ?"
            args.append(kind)
        rows = con.execute(sql + " ORDER BY created_at DESC, id DESC LIMIT ?", (*args, limit)).fetchall()
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
):
    if session is None:
        return auth._login_redirect(request)
    if not session[0].is_admin:
        return _forbidden(request)
    if status not in ("open", "dismissed"):
        status = "open"
    return templates.TemplateResponse(
        request=request,
        name="pages/admin_render.html",
        context={"user": session[0], "state": status_(), "findings": findings(status), "status": status},
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
def api_findings(admin: Annotated[User, Depends(web_admin)], status: str = "open", kind: str = "", limit: int = 200):
    return findings(status, kind, max(1, min(limit, 1000)))


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
