"""업로드 묶음 곡의 차트를 앞부분(STAGE_RENDER_SECONDS초)만 렌더해 지문 저장소(fingerprint)에서 같은 곡을 찾습니다.

- 렌더: 묶음을 다 푼 뒤 판정 전에 한 번만 합니다. 차트 sha256이 기존 차트와 겹치는 곡(add·ambiguous)은 하지 않고,
  나머지 곡은 모든 차트를 렌더합니다(섞인 곡 분리에도 같은 결과를 씀). 웹 서버와 GIL을 나누지 않도록
  STAGE_RENDER_WORKERS개 프로세스로 곡을 나눠 렌더하고, 결과(특징)는 묶음 폴더에 곡마다 저장해 다시 판정할 때 씁니다.
- 판정: 렌더 결과로 저장소를 찾아 같은 곡(render_match.SAME 이상)을 후보에 더하고, 이름·키음 이름으로 고른
  후보 곡과의 렌더 일치율도 잽니다. 키음을 일부만 담은 차분은 올린 것만으로 렌더하면 덜 맞으므로(partial) 렌더 값으로
  자동 추가를 막지는 않습니다. 다른 곡 키음으로 울리게 되는 병합은 commit 직전 검증(staging._render_check)이 막습니다.
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


def _render_source(source: str, charts: list[str], seconds: float, work: str) -> dict[str, dict]:
    """(다른 프로세스에서) 곡 하나(곡 zip이나 폴더)의 차트들을 렌더합니다.
    {차트 경로: {"f": 특징(base64), "s": [찾은 키음 수, 쓰는 키음 수]}}, 소리가 없는 차트는 뺌."""
    path = pathlib.Path(source)
    zf = None
    try:
        if path.is_file():
            zf = zipfile.ZipFile(path)
            names = [i.filename for i in zf.infolist() if not i.is_dir()]
            read = zf.read
        else:
            names = [p.relative_to(path).as_posix() for p in path.rglob("*") if p.is_file() and not p.is_symlink()]
            read = lambda rel: (path / rel).read_bytes()  # noqa: E731
        fps = render_match.fingerprints(names, read, charts, nice=False, seconds=seconds, work_dir=pathlib.Path(work))
    finally:
        if zf is not None:
            zf.close()
    out = {}
    for chart, fp in fps.items():
        feats = [f for f in fp.features if len(f)]
        if fp.sounds[0] > 0 and feats:
            out[chart] = {"f": base64.b64encode(fingerprint.variants_blob(feats)).decode(), "s": list(fp.sounds)}
    return out


def render(todo: list[tuple[str, str, dict[str, str]]], out_dir: pathlib.Path) -> None:
    """곡들을 렌더해 out_dir/{곡 id}.json에 둡니다. todo는 [(곡 id, 곡 zip이나 폴더, {차트 경로: sha256})].
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
            pool.submit(_render_source, source, list(shas), seconds, work): (unit_id, shas)
            for unit_id, source, shas in todo
        }
        for future in as_completed(futures):
            unit_id, shas = futures[future]
            try:
                blobs = future.result()
            except Exception:
                logger.exception(f"stage render failed[{unit_id}]")
                blobs = {}
            rendered += len(blobs)
            data = {"seconds": seconds, "charts": {shas[p]: b for p, b in blobs.items()}}
            target = out_dir / f"{unit_id}.json"
            tmp = target.with_suffix(".tmp")
            tmp.write_text(json.dumps(data), "utf-8")
            tmp.replace(target)
    logger.info(f"stage render: {len(todo)} songs, {rendered} charts in {time.monotonic() - started:.1f}s")


def load(out_dir: pathlib.Path, unit: dict) -> tuple[dict[str, list[np.ndarray]], bool]:
    """렌더해 둔 곡의 ({차트 경로: 분기별 특징}, 쓰는 키음 중 곡에 없는 것이 있는 차트가 있는지). 없으면 ({}, False)."""
    try:
        data = json.loads((out_dir / f"{unit['id']}.json").read_text("utf-8"))
    except (OSError, ValueError):
        return {}, False
    charts = data.get("charts") or {}
    found = [(c["path"], charts[c["sha256"]]) for c in unit["charts"] if c["sha256"] in charts]
    feats = {path: fingerprint.variants(base64.b64decode(item["f"])) for path, item in found}
    return feats, any(item["s"][0] < item["s"][1] for _, item in found)


def open_store() -> sqlite3.Connection | None:
    """지문 저장소. 아직 만든 적이 없으면 None."""
    if not fingerprint.db_path().exists():
        return None
    return fingerprint.connect()


def judge(con: sqlite3.Connection, query: dict[str, list[np.ndarray]], songs: list[int]) -> tuple[dict[int, float], dict[int, float]]:
    """(저장소에서 찾은 같은 곡 {곡 번호: 렌더 일치율}(SAME 이상), 후보 곡 songs 중 저장소에 있는 곡의 {곡 번호: 렌더 일치율})."""
    found: dict[int, float] = {}
    feats = list(query.values())
    for chart in feats:
        known = lambda song: found.get(song, 0.0) >= render_match.SAME  # noqa: E731
        for song, _, score in fingerprint.search(con, chart, exclude_song=0, skip=known, prefix=True):
            if score is not None and score >= render_match.SAME and score > found.get(song, 0.0):
                found[song] = score
    scores: dict[int, float] = {}
    for song in songs:
        if song in found:
            scores[song] = found[song]
            continue
        stored = fingerprint.song_features(con, song)
        value = fingerprint.best_score(feats, stored, prefix=True) if stored else None
        if value is not None:
            scores[song] = value
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
