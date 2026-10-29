"""모든 곡의 차트 렌더 특징을 모아 두고, 날마다 일정 시간씩 곡을 색인하며 다른 곡 번호의 같은 곡(중복)과
한 곡 안에 섞인 다른 곡을 찾아 관리자 목록에 쌓습니다. 자동으로 고치지는 않습니다.

차트가 수만 개라 모든 쌍을 렌더 일치율(render_match)로 비교할 수 없으므로, Shazam처럼 onset 특징의 봉우리
세 개(기준 + 뒤따르는 둘)의 대역·간격을 해시로 만들어 색인에 넣고, 해시가 같은 시간차로 많이 겹치는 차트만
후보로 뽑아 저장한 특징으로 렌더 일치율을 잽니다. 곡이 다른 차트끼리는 렌더 일치율만 봅니다(키음 이름은
곡마다 01.ogg처럼 겹치므로 순서 일치율은 쓰지 않음).

색인은 MySQL이 아니라 var 아래 SQLite 파일 하나(RENDER_INDEX_DIR/index.sqlite)에 둡니다. 다시 만들 수 있는
파생 데이터라 binlog·백업에 넣지 않습니다. 곡 zip이 바뀌면(sha256) 그 곡을 다시 색인하고, 없어진 곡은 지웁니다.
"""
import json
import logging
import os
import sqlite3
import threading
import time
import zipfile
import zlib

from typing import Annotated

import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from . import auth, constant, db, render_match
from .accounts import User
from .admin import _forbidden, web_admin
from .templating import templates

logger = logging.getLogger(__name__)

# 특징 저장: FEATURE_STEP 단위 uint8(0~12.75)로 줄여 zlib으로 압축합니다(렌더 일치율에는 차이가 거의 없음).
FEATURE_STEP = 0.05
# 차트마다 저장·색인하는 분기 수
MAX_STORED_VARIANTS = 4
# 봉우리: 주변(±PEAK_FRAMES 프레임, ±PEAK_BANDS 대역)에서 가장 크고 PEAK_MIN 이상인 점을 1초 창마다 PEAKS_PER_SECOND개까지
PEAK_FRAMES = 3
PEAK_BANDS = 2
PEAK_MIN = 0.5
PEAKS_PER_SECOND = 3
# 해시: 기준 봉우리와 그 뒤 MAX_DT 프레임 안의 봉우리 FAN개 중 두 개(대역 3개 + 간격 2개)
FAN = 3
MAX_DT = 63
# 후보: 같은 시간차(±1프레임)로 겹친 해시가 MIN_MATCH개 이상이고 질의 해시의 MIN_MATCH_RATIO 이상
MIN_MATCH = 10
MIN_MATCH_RATIO = 0.03
# 차트 하나가 렌더 일치율로 확인하는 후보 수
MAX_CANDIDATES = 8
# 너무 흔한 해시(무음에 가까운 반복 등)는 후보 찾기에서 뺍니다.
MAX_POSTING = 5000
NICE = 10


# ---- 특징 저장 ----


def pack(features: np.ndarray) -> bytes:
    q = np.clip(np.rint(features / FEATURE_STEP), 0, 255).astype(np.uint8)
    return zlib.compress(np.int32(q.shape[0]).tobytes() + q.tobytes(), 6)


def unpack(data: bytes) -> np.ndarray:
    raw = zlib.decompress(data)
    frames = int(np.frombuffer(raw[:4], dtype=np.int32)[0])
    return np.frombuffer(raw[4:], dtype=np.uint8).reshape(frames, render_match.BANDS).astype(np.float32) * FEATURE_STEP


# ---- 해시 ----


def peaks(features: np.ndarray) -> list[tuple[int, int]]:
    """(프레임, 대역) 봉우리들, 시간순."""
    if len(features) == 0:
        return []
    f = features
    padded = np.pad(f, ((PEAK_FRAMES, PEAK_FRAMES), (PEAK_BANDS, PEAK_BANDS)), constant_values=-1)
    local = np.full_like(f, -1)
    for dt in range(2 * PEAK_FRAMES + 1):
        for db_ in range(2 * PEAK_BANDS + 1):
            np.maximum(local, padded[dt : dt + len(f), db_ : db_ + f.shape[1]], out=local)
    ts, bs = np.nonzero((f >= local) & (f >= PEAK_MIN))
    if len(ts) == 0:
        return []
    values = f[ts, bs]
    # 고정된 1초 칸이 아니라 앞뒤 0.5초 안에서 더 큰 봉우리가 PEAKS_PER_SECOND개보다 적은 봉우리만 남깁니다
    # (렌더가 늦게 시작해도 같은 봉우리가 남도록).
    half = int(render_match.RATE / render_match.HOP) // 2
    order = np.argsort(ts, kind="stable")
    ts, bs, values = ts[order], bs[order], values[order]
    out = []
    lo = hi = 0
    for i in range(len(ts)):
        while ts[lo] < ts[i] - half:
            lo += 1
        while hi < len(ts) and ts[hi] <= ts[i] + half:
            hi += 1
        if int((values[lo:hi] > values[i]).sum()) < PEAKS_PER_SECOND:
            out.append((int(ts[i]), int(bs[i])))
    out.sort()
    return out


def hashes(features: np.ndarray) -> list[tuple[int, int]]:
    """(해시, 기준 프레임)들. 해시 = 대역 셋(5비트씩)과 간격 둘(6비트씩)."""
    p = peaks(features)
    out = []
    for i, (t1, b1) in enumerate(p):
        following = [(t, b) for t, b in p[i + 1 : i + 1 + FAN * 3] if 0 < t - t1 <= MAX_DT][:FAN]
        for j in range(len(following)):
            t2, b2 = following[j]
            for t3, b3 in following[j + 1 :]:
                if t3 - t1 > MAX_DT:
                    continue
                h = (b1 << 22) | (b2 << 17) | (b3 << 12) | ((t2 - t1) << 6) | (t3 - t1)
                out.append((h, t1))
    return out


def _variants_blob(features: list[np.ndarray]) -> bytes:
    parts = [pack(f) for f in features[:MAX_STORED_VARIANTS]]
    return b"".join(len(p).to_bytes(4, "little") + p for p in parts)


def _variants(blob: bytes) -> list[np.ndarray]:
    out, pos = [], 0
    while pos < len(blob):
        size = int.from_bytes(blob[pos : pos + 4], "little")
        out.append(unpack(blob[pos + 4 : pos + 4 + size]))
        pos += 4 + size
    return out


def _render_score(a: list[np.ndarray], b: list[np.ndarray]) -> float | None:
    """분기 중 가장 잘 맞는 쌍의 렌더 일치율."""
    best = None
    for x in a:
        for y in b:
            value = render_match.similarity(x, y)
            if value is not None and (best is None or value > best):
                best = value
    return best


# ---- 색인 저장소 ----

_SCHEMA = """
CREATE TABLE IF NOT EXISTS song(
    id INTEGER PRIMARY KEY,
    sha256 TEXT NOT NULL,
    charts INTEGER NOT NULL,
    indexed_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS chart(
    id INTEGER PRIMARY KEY,
    song_id INTEGER NOT NULL,
    path TEXT NOT NULL,
    features BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS chart_song ON chart(song_id);
-- 지운 차트의 해시는 바로 지우지 않고(해시로만 찾을 수 있게 둔 표라 차트로 지우려면 전체를 훑어야 함) 찾을 때 거르고,
-- 쌓이면 compact()가 한 번에 지웁니다.
CREATE TABLE IF NOT EXISTS hash(
    h INTEGER NOT NULL,
    chart INTEGER NOT NULL,
    t INTEGER NOT NULL,
    PRIMARY KEY (h, chart, t)
) WITHOUT ROWID;
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
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""



def _path():
    return constant.RENDER_INDEX_DIR / "index.sqlite"


def connect() -> sqlite3.Connection:
    constant.RENDER_INDEX_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(_path(), timeout=60, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(_SCHEMA)
    return con


def _candidates(con: sqlite3.Connection, query: list[tuple[int, int]], song_id: int) -> list[tuple[int, int]]:
    """질의 해시들과 같은 시간차로 많이 겹치는 다른 곡 차트들: [(차트 id, 겹친 수)], 많은 순."""
    if not query:
        return []
    by_hash: dict[int, list[int]] = {}
    for h, t in query:
        by_hash.setdefault(h, []).append(t)
    offsets: dict[int, dict[int, int]] = {}
    keys = list(by_hash)
    for start in range(0, len(keys), 500):
        part = keys[start : start + 500]
        rows = con.execute(
            f"SELECT h.h, h.chart, h.t FROM hash h JOIN chart c ON c.id = h.chart WHERE h.h IN ({','.join('?' * len(part))}) "
            "AND c.song_id != ?",
            (*part, song_id),
        ).fetchall()
        counts: dict[int, int] = {}
        for h, _, _ in rows:
            counts[h] = counts.get(h, 0) + 1
        for h, chart, t in rows:
            if counts[h] > MAX_POSTING:
                continue
            bins = offsets.setdefault(chart, {})
            for tq in by_hash[h]:
                d = t - tq
                bins[d] = bins.get(d, 0) + 1
    need = max(MIN_MATCH, int(len(query) * MIN_MATCH_RATIO))
    out = []
    for chart, bins in offsets.items():
        best = max(bins.get(d, 0) + bins.get(d - 1, 0) + bins.get(d + 1, 0) for d in bins)
        if best >= need:
            out.append((chart, best))
    out.sort(key=lambda x: -x[1])
    return out[:MAX_CANDIDATES]


def _put_finding(con: sqlite3.Connection, kind: str, a: int, b: int, score: float | None, detail: dict) -> None:
    con.execute(
        "INSERT INTO finding(kind, song_a, song_b, score, detail, created_at) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(kind, song_a, song_b) DO UPDATE SET score = excluded.score, detail = excluded.detail, "
        "created_at = excluded.created_at WHERE finding.status = 'open'",
        (kind, a, b, score, json.dumps(detail, ensure_ascii=False), int(time.time())),
    )


def _forget_song(con: sqlite3.Connection, song_id: int) -> None:
    """곡의 색인과 열린 발견을 지웁니다(해시는 compact가 지움). 넘긴(dismissed) 발견은 남겨 다시 띄우지 않습니다."""
    con.execute("DELETE FROM chart WHERE song_id = ?", (song_id,))
    con.execute("DELETE FROM song WHERE id = ?", (song_id,))
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
                    feats = [f for f in (fp.features if fp else []) if len(f)][:MAX_STORED_VARIANTS]
                    if not feats:
                        continue
                    blob = _variants_blob(feats)
                    feats = _variants(blob)  # 저장한 그대로(양자화된 값)로 비교합니다.
                    chart_id = con.execute(
                        "INSERT INTO chart(song_id, path, features) VALUES (?, ?, ?)", (song_id, path, blob)
                    ).lastrowid
                    query = sorted({hv for f in feats for hv in hashes(f)})
                    for cand, matched in _candidates(con, query, song_id):
                        other_song, other_path, other_blob = con.execute(
                            "SELECT song_id, path, features FROM chart WHERE id = ?", (cand,)
                        ).fetchone()
                        if other_song in dups and dups[other_song]["score"] >= render_match.SAME:
                            continue
                        score = _render_score(feats, _variants(other_blob))
                        if score is not None and score >= render_match.SAME:
                            dups[other_song] = {"score": score, "chart": path, "other_chart": other_path}
                    con.executemany("INSERT OR IGNORE INTO hash(h, chart, t) VALUES (?, ?, ?)", [(h, chart_id, t) for h, t in query])
                    stored += 1
                    _group(groups, path, fp)
    for other, d in dups.items():
        a, b = sorted((song_id, other))
        _put_finding(con, "dup", a, b, round(d["score"], 3), d if a == song_id else
                     {"score": d["score"], "chart": d["other_chart"], "other_chart": d["chart"]})
    if len(groups) > 1:
        _put_finding(con, "mixed", song_id, 0, None, {"groups": [g[1] for g in groups]})
    con.execute("INSERT INTO song(id, sha256, charts, indexed_at) VALUES (?, ?, ?, ?)", (song_id, sha256, stored, int(time.time())))
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


def compact(con: sqlite3.Connection) -> int:
    """지운 차트의 해시를 지웁니다."""
    n = con.execute("DELETE FROM hash WHERE chart NOT IN (SELECT id FROM chart)").rowcount
    con.commit()
    return n


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
            known = dict(con.execute("SELECT id, sha256 FROM song").fetchall())
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
                    con.execute("INSERT INTO song(id, sha256, charts, indexed_at) VALUES (?, ?, -1, ?)",
                                (song_id, live[song_id], int(time.time())))
                    con.commit()
                done += 1
            if gone or changed:
                compact(con)
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
_LEVELS = {name: getattr(logging, name) for name in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")}


def parse_output(lines):
    """색인 프로세스 출력 → (수준, 로거 이름, 메시지). 로그 줄은 "수준|로거|메시지"이고, 그 뒤에 붙는 줄(traceback 등)과
    로그 설정 전에 stderr로 나온 줄은 앞 기록에 붙이거나 따로 ERROR로 남깁니다."""
    current = None
    for raw in lines:
        line = raw.rstrip("\n")
        level, sep, rest = line.partition("|")
        if sep and level in _LEVELS and "|" in rest:
            if current:
                yield current
            name, _, message = rest.partition("|")
            current = (_LEVELS[level], name, message)
        elif current:
            current = (max(current[0], logging.ERROR if line.startswith("Traceback") else current[0]), current[1],
                       current[2] + "\n" + line)
        elif line.strip():
            current = (logging.ERROR, __name__ + ".process", line)
    if current:
        yield current


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
