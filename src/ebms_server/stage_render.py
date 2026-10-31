"""업로드 묶음 곡의 차트를 앞부분(STAGE_RENDER_SECONDS초)만 렌더해 지문 저장소(fingerprint)에서 같은 곡을 찾습니다.

- 렌더: 묶음을 다 푼 뒤 판정 전에 한 번만 합니다. 차트 sha256이 기존 차트와 겹치는 곡(add·ambiguous)은 하지 않고,
  나머지 곡은 모든 차트를 렌더합니다(섞인 곡 분리에도 같은 결과를 씀). 웹 서버와 GIL을 나누지 않도록
  STAGE_RENDER_WORKERS개 프로세스로 곡을 나눠 렌더하고, 결과(특징)는 묶음 폴더에 곡마다 저장해 다시 판정할 때 씁니다.
- 차분: 차트가 부르는 키음 파일이 올린 곡에 다 없으면(차분은 본곡 키음을 그대로 씀) 올린 것만으로는 소리가 빠지므로,
  이름·키음 이름으로 고른 후보 곡(앞 FILL_CANDIDATES개)마다 빠진 키음을 그 곡 zip의 같은 경로 파일로 채워서도 렌더합니다.
- 판정: 렌더 결과로 저장소를 찾아 같은 곡(render_match.SAME 이상)을 후보에 더하고, 후보 곡과의 렌더 일치율을 잽니다
  (그 곡 키음으로 채운 렌더가 있으면 그것으로). 자동 추가할 곡이 렌더로 '다름'(DIFFERENT 이하)이면 자동 추가하지 않습니다.
  다만 빠진 키음을 그 곡으로 채워 렌더하지 못한 경우(partial)는 막지 않습니다.
- commit: 등록한 차트의 특징을 바로 저장소에 넣어 다음 업로드부터 찾게 하고, 곡은 다시 색인할 곡으로 표시해
  상시 색인(render_index)이 전체 길이로 다시 렌더하게 합니다.
"""
import base64
import json
import logging
import multiprocessing
import pathlib
import sqlite3
import time
import zipfile
from concurrent.futures import ProcessPoolExecutor, as_completed

import numpy as np

from . import cache, constant, fingerprint, preview, render_match

logger = logging.getLogger(__name__)


# 빠진 키음을 채워 렌더해 볼 후보 곡 수
FILL_CANDIDATES = 3


def _pack(fps: dict[str, render_match.Fingerprint]) -> dict[str, dict]:
    """{차트 경로: {"f": 특징(base64), "s": [찾은 키음 수, 쓰는 키음 수]}}, 소리가 없는 차트는 뺌."""
    out = {}
    for chart, fp in fps.items():
        feats = [f for f in fp.features if len(f)]
        if fp.sounds[0] > 0 and feats:
            out[chart] = {"f": base64.b64encode(fingerprint.variants_blob(feats)).decode(), "s": list(fp.sounds)}
    return out


def _render_source(source: str, charts: list[str], seconds: float, work: str, fills: dict[int, str] | None = None) -> dict:
    """(다른 프로세스에서) 곡 하나(곡 zip이나 폴더)의 차트들을 렌더합니다. fills는 {후보 곡 번호: 곡 zip}으로,
    곡 안에 없는 파일을 그 zip의 같은 경로 파일로 채워 한 번씩 더 렌더합니다.
    {"own": _pack 결과, "filled": {곡 번호: _pack 결과}}."""
    path = pathlib.Path(source)
    opened: list[zipfile.ZipFile] = []
    try:
        if path.is_file():
            zf = zipfile.ZipFile(path)
            opened.append(zf)
            names = [i.filename for i in zf.infolist() if not i.is_dir()]
            read = zf.read
        else:
            names = [p.relative_to(path).as_posix() for p in path.rglob("*") if p.is_file() and not p.is_symlink()]
            read = lambda rel: (path / rel).read_bytes()  # noqa: E731
        work_dir = pathlib.Path(work)
        own = render_match.fingerprints(names, read, charts, nice=False, seconds=seconds, work_dir=work_dir)
        out = {"own": _pack(own), "filled": {}}
        # 키음이 다 있는 차트는 채워도 같으므로 빠진 키음이 있는 차트만 채워 렌더합니다.
        partial = [c for c, fp in own.items() if fp.sounds[0] < fp.sounds[1]] + [c for c in charts if c not in own]
        mine = set(names)
        for song_id, song_zip in (fills or {}).items() if partial else ():
            try:
                other = zipfile.ZipFile(song_zip)
            except (OSError, zipfile.BadZipFile):
                continue
            opened.append(other)
            theirs = {i.filename for i in other.infolist() if not i.is_dir()} - mine
            both = lambda rel, other=other, theirs=theirs: other.read(rel) if rel in theirs else read(rel)  # noqa: E731
            filled = render_match.fingerprints(names + sorted(theirs), both, partial, nice=False, seconds=seconds, work_dir=work_dir)
            out["filled"][song_id] = _pack(filled)
        return out
    finally:
        for zf in opened:
            zf.close()


def render(todo: list[tuple[str, str, dict[str, str], dict[int, str]]], out_dir: pathlib.Path) -> None:
    """곡들을 렌더해 out_dir/{곡 id}.json에 둡니다. todo는 [(곡 id, 곡 zip이나 폴더, {차트 경로: sha256}, {후보 곡 번호: 곡 zip})].
    실패한 곡도 빈 결과를 두어 다시 렌더하지 않습니다(이름·키음 판정만 함)."""
    workers = constant.STAGE_RENDER_WORKERS
    if not todo or workers <= 0:
        return
    if preview._ffmpeg() is None:
        logger.warning("stage render: ffmpeg not found. Similar songs are judged without rendering.")
        return
    out_dir.mkdir(parents=True, exist_ok=True)
    seconds = constant.STAGE_RENDER_SECONDS
    work = str(cache.work_dir())
    started = time.monotonic()
    rendered = 0
    with ProcessPoolExecutor(max_workers=min(workers, len(todo)), mp_context=multiprocessing.get_context("spawn")) as pool:
        futures = {
            pool.submit(_render_source, source, list(shas), seconds, work, fills): (unit_id, shas)
            for unit_id, source, shas, fills in todo
        }
        for future in as_completed(futures):
            unit_id, shas = futures[future]
            try:
                result = future.result()
            except Exception:
                logger.exception(f"stage render failed[{unit_id}]")
                result = {"own": {}, "filled": {}}
            rendered += len(result["own"]) + sum(len(v) for v in result["filled"].values())
            data = {
                "seconds": seconds,
                "charts": {shas[p]: b for p, b in result["own"].items()},
                "filled": {str(song): {shas[p]: b for p, b in got.items()} for song, got in result["filled"].items()},
            }
            target = out_dir / f"{unit_id}.json"
            tmp = target.with_suffix(".tmp")
            tmp.write_text(json.dumps(data), "utf-8")
            tmp.replace(target)
    logger.info(f"stage render: {len(todo)} songs, {rendered} charts in {time.monotonic() - started:.1f}s")


class Rendered:
    """렌더해 둔 곡: own {차트 경로: 분기별 특징}(올린 그대로), partial(쓰는 키음 중 곡에 없는 것이 있는 차트가 있는지),
    filled {후보 곡 번호: {차트 경로: 분기별 특징}}(빠진 키음을 그 곡 것으로 채운 렌더, 키음이 다 있는 차트는 own 그대로)."""

    def __init__(self, own: dict, partial: bool, filled: dict):
        self.own, self.partial, self.filled = own, partial, filled

    def query(self, song: int) -> tuple[dict[str, list[np.ndarray]], bool]:
        """song과 비교할 렌더와, 그것이 소리가 다 찬 렌더인지."""
        if song in self.filled:
            return {**self.own, **self.filled[song]}, True
        return self.own, not self.partial


def load(out_dir: pathlib.Path, unit: dict) -> Rendered:
    """렌더해 둔 곡. 없으면 빈 Rendered."""
    try:
        data = json.loads((out_dir / f"{unit['id']}.json").read_text("utf-8"))
    except (OSError, ValueError):
        return Rendered({}, False, {})

    def feats(items: dict) -> dict[str, list[np.ndarray]]:
        return {c["path"]: fingerprint.variants(base64.b64decode(items[c["sha256"]]["f"])) for c in unit["charts"] if c["sha256"] in items}

    charts = data.get("charts") or {}
    partial = any(item["s"][0] < item["s"][1] for item in charts.values()) or any(
        c["sha256"] not in charts for c in unit["charts"]
    )
    filled = {int(song): feats(items) for song, items in (data.get("filled") or {}).items()}
    return Rendered(feats(charts), partial, filled)


def open_store() -> sqlite3.Connection | None:
    """지문 저장소. 아직 만든 적이 없으면 None."""
    if not fingerprint.db_path().exists():
        return None
    return fingerprint.connect()


def judge(con: sqlite3.Connection, rendered: Rendered, songs: list[int]) -> tuple[dict[int, float], dict[int, tuple[float, bool]]]:
    """(저장소에서 찾은 같은 곡 {곡 번호: 렌더 일치율}(SAME 이상),
    후보 곡 songs 중 저장소에 있는 곡의 {곡 번호: (렌더 일치율, 소리가 다 찬 렌더로 잰 값인지)})."""
    found: dict[int, float] = {}
    for chart in rendered.own.values():
        known = lambda song: found.get(song, 0.0) >= render_match.SAME  # noqa: E731
        for song, _, score in fingerprint.search(con, chart, exclude_song=0, skip=known, prefix=True):
            if score is not None and score >= render_match.SAME and score > found.get(song, 0.0):
                found[song] = score
    scores: dict[int, tuple[float, bool]] = {}
    for song in songs:
        query, full = rendered.query(song)
        if song in found and song not in rendered.filled:
            scores[song] = (found[song], full)
            continue
        stored = fingerprint.song_features(con, song)
        value = fingerprint.best_score(list(query.values()), stored, prefix=True) if stored else None
        if value is not None:
            scores[song] = (max(value, found.get(song, 0.0)), full)
    return found, scores


def store_committed(song_id: int, query: dict[str, list[np.ndarray]]) -> None:
    """commit한 차트들의 (앞부분) 특징을 저장소에 넣고, 곡을 상시 색인이 전체 길이로 다시 렌더할 곡으로 표시합니다."""
    if not query:
        return
    con = fingerprint.connect()
    try:
        have = set(fingerprint.song_paths(con, song_id))
        stored = sum(fingerprint.store(con, song_id, path, feats) is not None for path, feats in query.items() if path not in have)
        # sha256을 비워 두면 상시 색인이 내용이 바뀐 곡으로 보고 다시 색인합니다.
        fingerprint.mark_song(con, song_id, "", len(have) + stored)
        con.commit()
    finally:
        con.close()
