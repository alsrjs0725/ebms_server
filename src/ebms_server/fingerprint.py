"""차트 렌더 지문 저장소: 차트마다 렌더 특징(onset)과 Shazam식 봉우리 해시를 모아 두고, 질의 특징과 같은 곡인
차트를 찾습니다. 중복 탐색(상시 점검 render_index, 이후 업로드 판정·commit 검증·섞인 곡 분리)이 함께 쓰는 곳입니다.

모든 쌍을 렌더 일치율(render_match)로 비교할 수 없으므로, 봉우리 세 개(기준 + 뒤따르는 둘)의 대역·간격을 해시로
만들어 역색인에 넣고, 해시가 같은 시간차로 많이 겹치는 차트만 후보로 뽑아 저장한 특징으로 렌더 일치율을 잽니다.
렌더 음원은 저장하지 않습니다(특징만으로 일치율을 계산하고, 음원은 필요하면 다시 렌더).

저장소는 MySQL이 아니라 var 아래 SQLite 파일 하나(RENDER_INDEX_DIR/index.sqlite)입니다. 다시 만들 수 있는 파생
데이터라 binlog·백업에 넣지 않습니다. 같은 차트라도 곡 키음이 바뀌면 소리가 달라지므로 (곡 번호, 차트 경로)로
두고, 곡 zip(sha256)이 바뀌면 그 곡을 다시 넣습니다.
"""
import sqlite3
import time
import zlib
from collections.abc import Callable, Iterator

import numpy as np

from . import constant, render_match

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


def variants_blob(features: list[np.ndarray]) -> bytes:
    parts = [pack(f) for f in features[:MAX_STORED_VARIANTS]]
    return b"".join(len(p).to_bytes(4, "little") + p for p in parts)


def variants(blob: bytes) -> list[np.ndarray]:
    out, pos = [], 0
    while pos < len(blob):
        size = int.from_bytes(blob[pos : pos + 4], "little")
        out.append(unpack(blob[pos + 4 : pos + 4 + size]))
        pos += 4 + size
    return out


def render_score(a: list[np.ndarray], b: list[np.ndarray]) -> float | None:
    """분기 중 가장 잘 맞는 쌍의 렌더 일치율."""
    best = None
    for x in a:
        for y in b:
            value = render_match.similarity(x, y)
            if value is not None and (best is None or value > best):
                best = value
    return best


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
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def db_path():
    return constant.RENDER_INDEX_DIR / "index.sqlite"


def connect(extra_schema: str = "") -> sqlite3.Connection:
    """저장소를 엽니다. extra_schema는 같은 파일에 둘 다른 표(render_index의 finding 등)."""
    constant.RENDER_INDEX_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path(), timeout=60, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(_SCHEMA + extra_schema)
    return con


def candidates(con: sqlite3.Connection, query: list[tuple[int, int]], song_id: int) -> list[tuple[int, int]]:
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


def store(con: sqlite3.Connection, song_id: int, path: str, features: list[np.ndarray]) -> tuple[int, list[np.ndarray], list[tuple[int, int]]] | None:
    """차트 특징(분기별)을 저장하고 해시를 색인에 넣습니다. 커밋은 부르는 쪽이 합니다.
    (차트 id, 저장한 그대로(양자화된) 특징, 질의 해시) 또는 저장할 특징이 없으면 None."""
    feats = [f for f in features if len(f)][:MAX_STORED_VARIANTS]
    if not feats:
        return None
    blob = variants_blob(feats)
    feats = variants(blob)  # 저장한 그대로(양자화된 값)로 비교합니다.
    chart_id = con.execute("INSERT INTO chart(song_id, path, features) VALUES (?, ?, ?)", (song_id, path, blob)).lastrowid
    query = sorted({hv for f in feats for hv in hashes(f)})
    con.executemany("INSERT OR IGNORE INTO hash(h, chart, t) VALUES (?, ?, ?)", [(h, chart_id, t) for h, t in query])
    return chart_id, feats, query


def search(
    con: sqlite3.Connection,
    features: list[np.ndarray],
    exclude_song: int,
    query: list[tuple[int, int]] | None = None,
    skip: Callable[[int], bool] = lambda song_id: False,
) -> Iterator[tuple[int, str, float | None]]:
    """질의 특징(분기별)과 같은 시간차로 해시가 많이 겹치는 다른 곡 차트를 후보로 뽑아 렌더 일치율을 잽니다.
    (곡 번호, 차트 경로, 렌더 일치율)을 해시가 많이 겹친 순으로 하나씩 냅니다. skip(곡 번호)은 후보마다 그때 보므로,
    앞 후보 결과로 뒤 후보를 건너뛸 수 있습니다."""
    if query is None:
        query = sorted({hv for f in features if len(f) for hv in hashes(f)})
    for cand, _ in candidates(con, query, exclude_song):
        other_song, other_path, other_blob = con.execute("SELECT song_id, path, features FROM chart WHERE id = ?", (cand,)).fetchone()
        if skip(other_song):
            continue
        yield other_song, other_path, render_score(features, variants(other_blob))


def forget_song(con: sqlite3.Connection, song_id: int) -> None:
    """곡의 차트 특징과 곡 기록을 지웁니다. 해시는 바로 지우지 않고(찾을 때 거름) compact()가 지웁니다."""
    con.execute("DELETE FROM chart WHERE song_id = ?", (song_id,))
    con.execute("DELETE FROM song WHERE id = ?", (song_id,))


def mark_song(con: sqlite3.Connection, song_id: int, sha256: str, charts: int) -> None:
    """곡을 넣었다고 기록합니다(charts -1은 실패: 같은 내용으로는 다시 하지 않음)."""
    con.execute("INSERT OR REPLACE INTO song(id, sha256, charts, indexed_at) VALUES (?, ?, ?, ?)", (song_id, sha256, charts, int(time.time())))


def songs(con: sqlite3.Connection) -> dict[int, str]:
    """넣은 곡들: {곡 번호: 넣을 때의 곡 zip sha256}."""
    return dict(con.execute("SELECT id, sha256 FROM song").fetchall())


def compact(con: sqlite3.Connection) -> int:
    """지운 차트의 해시를 지웁니다."""
    n = con.execute("DELETE FROM hash WHERE chart NOT IN (SELECT id FROM chart)").rowcount
    con.commit()
    return n
