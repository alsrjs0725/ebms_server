"""곡 임포트 대기(staged). 올린 zip과 var/tmp의 곡을 바로 DB에 넣지 않고 `var/staged/`에 곡 단위로 풀어 두고,
관리자가 곡마다(또는 차트마다) 넣을 곳을 검토·지정한 뒤 commit하면 그때 등록합니다.

- 묶음(stage): zip 하나 또는 var/tmp 가져오기 한 번. `var/staged/{묶음}/stage.json`에 상태와 곡 목록을 두고,
  곡은 `var/staged/{묶음}/{곡}/{폴더명}/`에 풉니다. 디스크에 있으므로 서버를 다시 시작해도 남습니다.
- 곡(unit): 차트 파일이 바로 아래 있는 폴더 하나. 대상(target)은 "auto"(겹치는 차트로 판정, 기본),
  "new"(새 곡), 곡 번호(그 곡에 추가), "skip"(넣지 않음)입니다. 차트마다 대상을 따로 줄 수도 있습니다(None이면 곡의 대상).
- 차트를 여러 대상으로 나누면 대상마다 그 차트, 그 차트가 가리키는 파일(키음·BGA·배너 등), 어느 차트도 가리키지 않는 파일을 묶어 넣습니다.
- commit은 배치로 넣고(importer.Job의 천천히 모드 그대로), 실패하거나 충돌(내용이 다른 같은 경로 파일)이 난 곡은 묶음에 남겨 다시 검토합니다.
"""
import hashlib
import json
import logging
import os
import pathlib
import posixpath
import shutil
import threading
import time
import uuid
import zipfile

from . import constant, db, importer
from .db import Database, _chart_refs, chart_files

logger = logging.getLogger(__name__)

STAGE_FILE = "stage.json"
# 작업 중 stage.json을 저장하는 간격(초). 곡마다 저장하면 곡이 많을 때 쓰기가 너무 많아집니다.
SAVE_INTERVAL = 2.0
# 검토 화면 한 쪽의 곡 수
UNITS_PER_PAGE = 50
# 판정(suggestion.kind): new 새 곡, add 기존 곡에 추가, ambiguous 기존 곡 여러 개와 겹침, unused 사용 중지한 곡과 겹침
SUGGESTIONS = ("new", "add", "ambiguous", "unused")

_lock = threading.RLock()


class StageError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


# ---- 저장 ----

def _dir(stage_id: str) -> pathlib.Path:
    if not stage_id or not all(c in "0123456789abcdef" for c in stage_id):
        raise StageError(404, "stage not found")
    return constant.STAGED_DIR / stage_id


def _load(stage_id: str) -> dict:
    path = _dir(stage_id) / STAGE_FILE
    try:
        return json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        raise StageError(404, "stage not found") from None


def _save(stage: dict) -> None:
    path = _dir(stage["id"]) / STAGE_FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(stage, ensure_ascii=False), "utf-8")
    os.replace(tmp, path)


def _unit_dir(stage: dict, unit: dict) -> pathlib.Path:
    return _dir(stage["id"]) / unit["id"] / unit["name"]


def _new_stage(kind: str, name: str) -> dict:
    stage = {
        "id": uuid.uuid4().hex,
        "kind": kind,
        "name": name,
        "status": "staging",  # staging / ready / committing
        "created_at": int(time.time()),
        "analyzed_at": None,
        "error": None,
        "units": [],
    }
    with _lock:
        _dir(stage["id"]).mkdir(parents=True)
        _save(stage)
    return stage


def recover() -> None:
    """서버 시작 시: 이전 실행에서 작업 도중 꺼진 묶음을 검토할 수 있는 상태로 되돌리고, 목록에 없는 폴더를 지웁니다."""
    if not constant.STAGED_DIR.is_dir():
        return
    for path in constant.STAGED_DIR.iterdir():
        if not path.is_dir():
            continue
        try:
            stage = _load(path.name)
        except (StageError, ValueError):
            logger.warning(f"staging: removed broken stage[{path}]")
            shutil.rmtree(path, ignore_errors=True)
            continue
        if stage["status"] != "ready":
            stage["error"] = (
                "서버가 다시 시작돼 작업이 중단됐습니다. "
                + ("이미 등록된 곡은 다시 commit해도 바뀌지 않습니다." if stage["status"] == "committing" else "풀어 둔 곡까지만 남았습니다.")
            )
            stage["status"] = "ready"
        known = {unit["id"] for unit in stage["units"]}
        for child in path.iterdir():
            if child.is_dir() and child.name not in known:
                shutil.rmtree(child, ignore_errors=True)
        if stage["units"]:
            _save(stage)
        else:
            shutil.rmtree(path, ignore_errors=True)


# ---- 곡 분석 ----

def _chart_info(path: pathlib.Path, root: pathlib.Path) -> dict:
    content = path.read_bytes()
    return {
        "path": path.relative_to(root).as_posix(),
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "target": None,
    }


def _make_unit(unit_id: str, song_dir: pathlib.Path, folder: str) -> dict:
    files = [p for p in song_dir.rglob("*") if p.is_file() and not p.is_symlink()]
    return {
        "id": unit_id,
        "name": song_dir.name,
        "folder": folder,
        "files": len(files),
        "size": sum(p.stat().st_size for p in files),
        "charts": [_chart_info(p, song_dir) for p in chart_files(song_dir)],
        "target": "auto",
        "suggestion": None,
        "result": None,
    }


def _analyze(units: list[dict]) -> None:
    """곡마다 겹치는 기존 곡을 찾아 판정(suggestion)과 차트별 상태(songs, deleted)를 채웁니다. 읽기만 합니다."""
    Database()  # 테이블 준비
    with db.connect() as con, con.cursor() as cur:
        for unit in units:
            songs: list[int] = []
            deleted = None
            for chart in unit["charts"]:
                cur.execute("SELECT song_id FROM chart WHERE id = %s AND size = %s", (chart["sha256"], chart["size"]))
                row = cur.fetchone()
                chart["song_id"] = row[0] if row is not None else None
                chart["deleted"] = None
                if row is not None and row[0] is None:
                    cur.execute(
                        "SELECT song_id FROM deleted_song WHERE chart_list LIKE %s ORDER BY song_id DESC LIMIT 1",
                        (f'%"{chart["sha256"]}"%',),
                    )
                    d = cur.fetchone()
                    if d is not None:
                        chart["deleted"] = d[0]
                        deleted = deleted or d[0]
                if chart["song_id"] is not None and chart["song_id"] not in songs:
                    songs.append(chart["song_id"])
            songs.sort()
            if deleted is not None:
                kind = "unused"
            elif not songs:
                kind = "new"
            elif len(songs) == 1:
                kind = "add"
            else:
                kind = "ambiguous"
            unit["suggestion"] = {"kind": kind, "songs": songs, "deleted": deleted}


def analyze(stage_id: str) -> dict:
    """묶음의 판정을 지금 DB 기준으로 다시 합니다(그 사이 다른 묶음을 commit했거나 곡을 사용 중지한 경우)."""
    with _lock:
        stage = _load(stage_id)
        _require_ready(stage)
        _analyze(stage["units"])
        stage["analyzed_at"] = int(time.time())
        _save(stage)
    return summary(stage)


# ---- staged에 넣기 ----

class _Writer:
    """작업 중 곡을 묶음에 더하고 stage.json을 가끔 저장합니다."""

    def __init__(self, stage: dict, job: importer.Job):
        self.stage = stage
        self.job = job
        self._saved = time.monotonic()

    def unit_dir(self) -> tuple[str, pathlib.Path]:
        unit_id = uuid.uuid4().hex[:12]
        return unit_id, _dir(self.stage["id"]) / unit_id

    def add(self, unit_id: str, song_dir: pathlib.Path, folder: str) -> None:
        try:
            unit = _make_unit(unit_id, song_dir, folder)
        except Exception as e:
            logger.exception(f"staging failed[{song_dir}]")
            shutil.rmtree(song_dir.parent, ignore_errors=True)
            self.job.songs.append({"folder": folder, "error": str(e) or type(e).__name__})
            return
        if not unit["charts"]:
            shutil.rmtree(song_dir.parent, ignore_errors=True)
            self.job.songs.append({"folder": folder, "error": "곡에 넣을 수 있는 차트 파일이 없습니다."})
            return
        self.stage["units"].append(unit)
        self.job.songs.append({"folder": folder, "staged": True, "charts": len(unit["charts"])})
        if time.monotonic() - self._saved >= SAVE_INTERVAL:
            self.save()

    def error(self, folder: str, message: str) -> None:
        self.job.songs.append({"folder": folder, "error": message})

    def save(self) -> None:
        with _lock:
            _save(self.stage)
        self._saved = time.monotonic()

    def finish(self, error: str | None = None) -> None:
        """판정을 채우고 검토할 수 있게 합니다. 곡이 하나도 없으면 묶음을 지웁니다."""
        stage = self.stage
        if not stage["units"]:
            shutil.rmtree(_dir(stage["id"]), ignore_errors=True)
            self.job.stage_id = None
            return
        try:
            _analyze(stage["units"])
            stage["analyzed_at"] = int(time.time())
        except Exception:
            logger.exception(f"staging analyze failed[{stage['id']}]")
        stage["status"] = "ready"
        stage["error"] = error
        self.save()


def _stage_zip(writer: _Writer, path: pathlib.Path, filename: str) -> int:
    """zip의 곡을 묶음에 풉니다. 푼 곡 수를 반환합니다(실패한 곡 포함). zip이 아니면 zipfile.BadZipFile."""
    stem = pathlib.PurePath(filename.replace("\\", "/")).stem
    if stem in ("", ".", ".."):
        stem = "song"
    with zipfile.ZipFile(path) as zf:
        groups = importer._song_groups(zf)
        writer.job.total = (writer.job.total or 0) + len(groups)
        for root, members in groups.items():
            name = root[-1] if root else stem
            folder = "/".join((stem, *root))
            unit_id, work = writer.unit_dir()
            song_dir = work / name
            try:
                for info, rel in members:
                    target = song_dir.joinpath(*rel)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(info) as fin, open(target, "wb") as fout:
                        shutil.copyfileobj(fin, fout, constant.BLOB_READ_SIZE)
            except Exception as e:
                logger.exception(f"staging failed[{filename}:{folder}]")
                shutil.rmtree(work, ignore_errors=True)
                writer.error(folder, str(e) or type(e).__name__)
                continue
            writer.add(unit_id, song_dir, folder)
    return len(groups)


def stage_zip(path: pathlib.Path, filename: str, job: importer.Job) -> dict | None:
    """올린 zip 하나를 새 묶음으로 풉니다. 곡이 없으면 묶음을 만들지 않고 None."""
    stage = _new_stage("zip", filename)
    job.stage_id = stage["id"]
    writer = _Writer(stage, job)
    try:
        _stage_zip(writer, path, filename)
    except BaseException as e:
        writer.finish(f"zip을 다 풀지 못했습니다: {e}")
        raise
    writer.finish()
    return stage if stage["units"] else None


def stage_tmp(job: importer.Job) -> dict | None:
    """`var/tmp/`의 곡 폴더와 zip을 새 묶음으로 옮깁니다. 곡 폴더는 옮기고(원래 자리에서 사라짐),
    zip은 모든 곡을 풀면 지웁니다. 곡이 없으면 묶음을 만들지 않고 None."""
    root = constant.TMP_DIR
    root.mkdir(parents=True, exist_ok=True)
    stage = _new_stage("tmp", "var/tmp")
    job.stage_id = stage["id"]
    writer = _Writer(stage, job)
    try:
        song_dirs = []
        for child in sorted(root.iterdir()):
            if child.is_symlink():
                logger.warning(f"import: skipped symlink[{child}]")
            elif child.is_dir():
                song_dirs.extend(importer.find_song_dirs(child))
        job.total = len(song_dirs)
        for song_dir in song_dirs:
            folder = song_dir.relative_to(root).as_posix()
            unit_id, work = writer.unit_dir()
            work.mkdir()
            try:
                shutil.move(song_dir, work / song_dir.name)
            except OSError as e:
                logger.exception(f"staging failed[{song_dir}]")
                shutil.rmtree(work, ignore_errors=True)
                writer.error(folder, str(e) or type(e).__name__)
                continue
            writer.add(unit_id, work / song_dir.name, folder)
        for path in sorted(p for p in root.iterdir() if p.suffix.lower() == ".zip"):
            if path.is_symlink() or not path.is_file():
                if path.is_symlink():
                    logger.warning(f"import: skipped symlink[{path}]")
                continue
            start = len(job.songs)
            try:
                _stage_zip(writer, path, path.name)
            except (zipfile.BadZipFile, OSError) as e:
                logger.warning(f"stage_tmp: cannot read zip[{path}]: {e}")
                writer.error(path.name, f"zip을 읽지 못했습니다(아직 복사 중일 수 있습니다): {e}")
                continue
            if not any("error" in song for song in job.songs[start:]):
                path.unlink(missing_ok=True)
    except BaseException as e:
        writer.finish(f"var/tmp를 다 옮기지 못했습니다: {e}")
        raise
    writer.finish()
    return stage if stage["units"] else None


# ---- 검토 ----

def _require_ready(stage: dict) -> None:
    if stage["status"] != "ready":
        raise StageError(409, "묶음을 풀거나 등록하는 중입니다. 끝난 뒤 다시 시도하세요.")


def _counts(stage: dict) -> dict:
    counts = {kind: 0 for kind in SUGGESTIONS}
    counts.update(changed=0, skip=0, failed=0)
    for unit in stage["units"]:
        kind = (unit.get("suggestion") or {}).get("kind")
        if kind in counts:
            counts[kind] += 1
        if _effective_skip(unit):
            counts["skip"] += 1
        elif _changed(unit):
            counts["changed"] += 1
        if unit.get("result") and unit["result"].get("error"):
            counts["failed"] += 1
    return counts


def _changed(unit: dict) -> bool:
    return unit["target"] != "auto" or any(chart["target"] is not None for chart in unit["charts"])


def _effective_skip(unit: dict) -> bool:
    return all(_chart_target(unit, chart) == "skip" for chart in unit["charts"])


def _chart_target(unit: dict, chart: dict) -> str | int:
    return unit["target"] if chart["target"] is None else chart["target"]


def summary(stage: dict) -> dict:
    return {
        "id": stage["id"],
        "kind": stage["kind"],
        "name": stage["name"],
        "status": stage["status"],
        "created_at": stage["created_at"],
        "analyzed_at": stage["analyzed_at"],
        "error": stage["error"],
        "units": len(stage["units"]),
        "charts": sum(len(unit["charts"]) for unit in stage["units"]),
        "size": sum(unit["size"] for unit in stage["units"]),
        "counts": _counts(stage),
    }


def list_stages() -> list[dict]:
    if not constant.STAGED_DIR.is_dir():
        return []
    stages = []
    with _lock:
        for path in constant.STAGED_DIR.iterdir():
            try:
                stages.append(summary(_load(path.name)))
            except (StageError, ValueError, KeyError):
                continue
    return sorted(stages, key=lambda s: s["created_at"], reverse=True)


def _matches(unit: dict, show: str, q: str) -> bool:
    if show in SUGGESTIONS and (unit.get("suggestion") or {}).get("kind") != show:
        return False
    if show == "changed" and not _changed(unit):
        return False
    if show == "failed" and not (unit.get("result") and unit["result"].get("error")):
        return False
    if q:
        text = "\n".join([unit["folder"], *(chart["path"] for chart in unit["charts"])]).lower()
        if q.lower() not in text:
            return False
    return True


def _song_folders(song_ids: set[int]) -> dict[int, str]:
    if not song_ids:
        return {}
    with db.connect() as con, con.cursor() as cur:
        marks = ", ".join(["%s"] * len(song_ids))
        cur.execute(f"SELECT id, folder FROM song WHERE id IN ({marks})", tuple(sorted(song_ids)))
        return {row[0]: row[1] for row in cur.fetchall()}


# 검토 화면의 곡 정렬. 기본("")은 묶음에 넣은 순서입니다.
SORTS = {
    "files_desc": lambda unit: (-unit["files"], unit["folder"]),
    "files_asc": lambda unit: (unit["files"], unit["folder"]),
}


def get_stage(stage_id: str, page: int = 1, show: str = "", q: str = "", sort: str = "") -> dict:
    """묶음 요약과 곡 한 쪽. show는 판정(new/add/ambiguous/unused)이나 changed(대상을 바꾼 곡), failed(지난 commit에서 실패).
    sort는 SORTS의 이름(파일 수 많은/적은 순)이고, 없거나 모르는 값이면 넣은 순서입니다."""
    with _lock:
        stage = _load(stage_id)
    units = [unit for unit in stage["units"] if _matches(unit, show, q.strip())]
    if sort in SORTS:
        units.sort(key=SORTS[sort])
    pages = max(1, -(-len(units) // UNITS_PER_PAGE))
    page = min(max(1, page), pages)
    items = units[(page - 1) * UNITS_PER_PAGE : page * UNITS_PER_PAGE]
    # 화면에 보일 곡 번호의 폴더명
    ids = set()
    for unit in items:
        ids.update((unit.get("suggestion") or {}).get("songs", []))
        ids.update(t for t in [unit["target"], *(c["target"] for c in unit["charts"])] if isinstance(t, int))
    folders = _song_folders(ids)
    result = summary(stage)
    result.update(
        page=page,
        pages=pages,
        total=len(units),
        items=items,
        songs={str(k): v for k, v in folders.items()},
    )
    return result


def _check_target(value, allow_none: bool) -> str | int | None:
    if value is None and allow_none:
        return None
    if value in ("auto", "new", "skip"):
        return value
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    raise StageError(400, f"대상이 올바르지 않습니다: {value!r}")


def set_target(stage_id: str, unit_id: str, target, charts: dict[str, object] | None) -> dict:
    """곡의 대상과 차트별 대상을 바꿉니다. charts는 {차트 경로: 대상 또는 None(곡의 대상을 따름)}이고 빠진 차트는 그대로입니다."""
    target = _check_target(target, allow_none=False)
    chart_targets = {path: _check_target(value, allow_none=True) for path, value in (charts or {}).items()}
    song_ids = {t for t in [target, *chart_targets.values()] if isinstance(t, int)}
    missing = song_ids - set(_song_folders(song_ids))
    if missing:
        raise StageError(400, f"곡 #{', #'.join(map(str, sorted(missing)))}이 없습니다(사용 중지했거나 번호가 틀림).")
    with _lock:
        stage = _load(stage_id)
        _require_ready(stage)
        unit = next((u for u in stage["units"] if u["id"] == unit_id), None)
        if unit is None:
            raise StageError(404, "unit not found")
        by_path = {chart["path"]: chart for chart in unit["charts"]}
        unknown = set(chart_targets) - set(by_path)
        if unknown:
            raise StageError(400, f"곡에 없는 차트입니다: {', '.join(sorted(unknown))}")
        unit["target"] = target
        for path, value in chart_targets.items():
            by_path[path]["target"] = value
        _save(stage)
    return unit


def discard(stage_id: str) -> None:
    """묶음을 통째로 버립니다(풀어 둔 파일 삭제)."""
    with _lock:
        stage = _load(stage_id)
        _require_ready(stage)
        shutil.rmtree(_dir(stage_id), ignore_errors=True)


# ---- commit ----

def _referenced(unit_dir: pathlib.Path, chart_paths: list[str]) -> dict[str, set[str]]:
    """차트마다 가리키는 파일의 소문자 확장자 뺀 경로."""
    refs = {}
    for path in chart_paths:
        try:
            content = (unit_dir / path).read_bytes()
        except OSError:
            refs[path] = set()
            continue
        pre, play = _chart_refs(content, posixpath.dirname(path))
        refs[path] = {posixpath.splitext(target)[0] for target, _ in pre} | play
    return refs


def _link(src: pathlib.Path, dst: pathlib.Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def _subset(unit_dir: pathlib.Path, charts: list[str], all_charts: list[str], work: pathlib.Path) -> pathlib.Path:
    """unit_dir에서 charts와, 그 차트가 가리키는 파일, 어느 차트도 가리키지 않는 파일만 work 아래 같은 이름 폴더에 모읍니다."""
    refs = _referenced(unit_dir, all_charts)
    mine = set().union(*(refs[c] for c in charts)) if charts else set()
    anyone = set().union(*refs.values()) if refs else set()
    chart_set = {c.lower() for c in all_charts}
    keep = {c.lower() for c in charts}
    out = work / unit_dir.name
    out.mkdir(parents=True)
    for path in unit_dir.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        rel = path.relative_to(unit_dir).as_posix()
        lower = rel.lower()
        if lower in chart_set:
            if lower in keep:
                _link(path, out / rel)
            continue
        stem = posixpath.splitext(lower)[0]
        if stem in mine or stem not in anyone:
            _link(path, out / rel)
    return out


def _groups(unit: dict) -> list[tuple[str | int, list[str]]]:
    """(대상, 차트 경로 목록). skip은 뺍니다. 모든 차트가 같은 대상이면 하나."""
    groups: dict = {}
    for chart in unit["charts"]:
        target = _chart_target(unit, chart)
        if target != "skip":
            groups.setdefault(target, []).append(chart["path"])
    return list(groups.items())


def _label(unit: dict, charts: list[str], split: bool) -> str:
    return f"{unit['folder']} [{', '.join(charts)}]" if split else unit["folder"]


def commit(stage_id: str, job: importer.Job) -> None:
    """묶음의 곡을 대상대로 등록합니다. 잘 들어간 곡(대상이 skip인 곡 포함)은 묶음에서 지우고,
    실패·충돌이 있는 곡은 결과(result)를 붙여 남깁니다. 남은 곡이 없으면 묶음을 지웁니다."""
    with _lock:
        stage = _load(stage_id)
    root = _dir(stage_id)
    work_root = root / "_work"
    units = list(stage["units"])
    job.total = len(units)
    pending: list[tuple[dict, list[dict]]] = []  # 커밋을 기다리는 (곡, 그 곡의 등록 결과들)
    done: set[str] = set()
    saved = time.monotonic()

    def settle() -> None:
        """커밋된(또는 롤백된) 곡의 결과를 정리합니다."""
        nonlocal saved
        for unit, infos in pending:
            errors = [info["error"] for info in infos if info.get("error")]
            conflicts = sorted({c for info in infos for c in info.get("conflicts", [])})
            if errors or conflicts:
                unit["result"] = {"error": "; ".join(errors) or None, "conflicts": conflicts, "songs": infos}
            else:
                done.add(unit["id"])
                shutil.rmtree(root / unit["id"], ignore_errors=True)
        pending.clear()
        stage["units"] = [u for u in stage["units"] if u["id"] not in done]
        if time.monotonic() - saved >= SAVE_INTERVAL:
            with _lock:
                _save(stage)
            saved = time.monotonic()

    try:
        with Database().batch() as batch:
            for unit in units:
                groups = _groups(unit)
                infos: list[dict] = []
                if not groups:
                    job.songs.append({"folder": unit["folder"], "skipped": "admin"})
                    pending.append((unit, infos))
                    if not batch.open:
                        settle()
                    continue
                unit_dir = _unit_dir(stage, unit)
                split = len(groups) > 1 or len(groups[0][1]) != len(unit["charts"])
                all_charts = [chart["path"] for chart in unit["charts"]]
                for target, charts in groups:
                    label = _label(unit, charts, split)
                    work = work_root / uuid.uuid4().hex
                    try:
                        song_dir = _subset(unit_dir, charts, all_charts, work) if split else unit_dir
                        job.apply_limits(batch)
                        info = importer._insert(batch, song_dir, label, remove=False, target=None if target == "auto" else target)
                    except Exception as e:
                        logger.exception(f"commit failed[{label}]")
                        info = {"folder": label, "error": str(e) or type(e).__name__}
                    finally:
                        shutil.rmtree(work, ignore_errors=True)
                    infos.append(info)
                    job.songs.append(info)
                pending.append((unit, infos))
                if not batch.open:
                    settle()
                job.rest(batch)
        settle()
    finally:
        shutil.rmtree(work_root, ignore_errors=True)
        with _lock:
            stage["status"] = "ready"
            stage["error"] = None
            if stage["units"]:
                try:
                    _analyze(stage["units"])
                    stage["analyzed_at"] = int(time.time())
                except Exception:
                    logger.exception(f"staging analyze failed[{stage_id}]")
                _save(stage)
            else:
                shutil.rmtree(root, ignore_errors=True)


def start_commit(stage_id: str, slow: bool = False) -> importer.Job:
    with _lock:
        stage = _load(stage_id)
        _require_ready(stage)
        stage["status"] = "committing"
        _save(stage)
    job = importer.Job("commit", stage["name"], slow)
    job.stage_id = stage_id

    def run():
        commit(stage_id, job)

    return importer._start(job, run)
