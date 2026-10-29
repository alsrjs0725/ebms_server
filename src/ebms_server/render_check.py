"""관리자가 고른 곡들의 차트를 서버에서 렌더해 같은 곡끼리 묶습니다(render_match). 곡 섞임 점검·분리·합치기용.

차트를 하나씩 렌더해 지금까지 만든 묶음의 대표 차트들과 비교하고(same_song), "same"이 나온 묶음 중 가장 높은 쪽에
넣습니다. 없으면 새 묶음의 대표가 됩니다. 곡이 달라도 같은 묶음에 들어가면 같은 곡입니다.
특징은 대표 차트 것만 들고 있어 차트가 많은 곡(합본 등)도 메모리가 차트 수만큼 늘지 않습니다.
서비스에 양보하도록 작업 스레드와 ffmpeg 우선순위를 낮춥니다.
"""
import logging
import os
import threading
import zipfile

from . import constant, db, importer, render_match

logger = logging.getLogger(__name__)

MAX_SONGS = 50
MAX_CHARTS = 5000
# 한 번에 렌더하는 차트 수(같은 곡 차트는 키음을 같이 쓰므로 묶어서 렌더하면 키음을 한 번만 풉니다).
BATCH = 16
NICE = 10


class _Group:
    __slots__ = ("rep", "fp", "charts")

    def __init__(self, rep: dict, fp: render_match.Fingerprint):
        self.rep = rep
        self.fp = fp
        self.charts = [rep]


def _song_charts(database: db.Database, song_id: int, only: list[str] | None) -> list[str]:
    detail = database.get_song_files(song_id)
    if detail is None:
        raise LookupError(f"song {song_id} not found")
    paths = [f["path"] for f in detail if f["path"].lower().endswith(constant.BMS_FORMAT)]
    if only is not None:
        wanted = {p.lower() for p in only}
        paths = [p for p in paths if p.lower() in wanted]
    return paths


def run(todo: dict[int, list[str]], job: importer.Job) -> dict:
    """todo(곡 id → 차트 경로들)의 차트를 묶습니다. 결과는 job.result에도 둡니다."""
    try:
        os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), NICE)
    except OSError:
        pass
    database = db.Database()
    groups: list[_Group] = []
    alone: list[dict] = []  # 렌더할 소리가 없거나 차트를 못 읽은 차트
    for song_id, paths in todo.items():
        file = database.open_file("song", song_id)
        if file is None:
            raise LookupError(f"song {song_id} not found")
        with os.fdopen(os.dup(file._fd), "rb") as raw, zipfile.ZipFile(raw) as zf:
            file.close()
            names = [i.filename for i in zf.infolist() if not i.is_dir()]
            for start in range(0, len(paths), BATCH):
                part = paths[start : start + BATCH]
                job.source = f"#{song_id} {part[0]}"
                fps = render_match.fingerprints(names, zf.read, part, nice=True)
                for path in part:
                    entry = {"song_id": song_id, "path": path}
                    fp = fps.get(path)
                    if fp is None or not any(len(f) for f in fp.features):
                        entry["error"] = "렌더 못 함"
                        alone.append(entry)
                    else:
                        entry["sounds"] = list(fp.sounds)
                        if fp.truncated:
                            entry["truncated"] = True
                        _place(groups, entry, fp)
                    job.songs.append({"song_id": song_id, "path": path})
    job.source = None
    result = {
        "groups": [
            {"songs": sorted({c["song_id"] for c in g.charts}), "charts": g.charts}
            for g in sorted(groups, key=lambda g: -len(g.charts))
        ],
        "unrendered": alone,
    }
    job.result = result
    return result


def _place(groups: list[_Group], entry: dict, fp: render_match.Fingerprint) -> None:
    best, best_value, near = None, None, None
    for g in groups:
        verdict, render, order = render_match.same_song(g.fp, fp)
        value = max((v for v in (render, order) if v is not None), default=None)
        if verdict == "same" and (best_value is None or value > best_value):
            best, best_value = g, value
        elif value is not None and (near is None or value > near[1]):
            near = (g, value)
    if best is not None:
        entry["score"] = round(best_value, 3)
        best.charts.append(entry)
        return
    if near is not None:
        # 같은 곡으로 보지 않은 것 중 가장 가까운 대표(애매한 값이면 사람이 볼 수 있게)
        entry["nearest"] = {"song_id": near[0].rep["song_id"], "path": near[0].rep["path"], "score": round(near[1], 3)}
    groups.append(_Group(entry, fp))


def start(songs: list[int], charts: dict[int, list[str]]) -> importer.Job:
    """songs의 차트(charts에 곡이 있으면 그 경로만)를 묶는 작업을 시작합니다. 없는 곡이면 LookupError, 개수가 한도를 넘으면 ValueError."""
    songs = list(dict.fromkeys(songs))
    if not songs or len(songs) > MAX_SONGS:
        raise ValueError(f"곡은 1~{MAX_SONGS}개여야 합니다.")
    database = db.Database()
    todo = {song_id: _song_charts(database, song_id, charts.get(song_id)) for song_id in songs}
    total = sum(len(v) for v in todo.values())
    if total > MAX_CHARTS:
        raise ValueError(f"차트가 {total}개로 한 번에 볼 수 있는 {MAX_CHARTS}개를 넘습니다. 차트를 골라 주세요.")
    job = importer.Job("render", "렌더 비교 " + ", ".join(f"#{s}" for s in songs[:5]) + (" …" if len(songs) > 5 else ""))
    job.total = total
    return importer._start(job, lambda: run(todo, job))
