"""곡 임포트 대기(staged). 올린 zip·rar·7z와 var/tmp의 곡을 바로 DB에 넣지 않고 `var/staged/`에 곡 단위로 풀어 두고,
관리자가 곡마다(또는 차트마다) 넣을 곳을 검토·지정한 뒤 commit하면 그때 등록합니다.

- 묶음(stage): zip이나 7z, rar(분할 압축이면 볼륨 전체) 하나, 또는 var/tmp 가져오기 한 번. `var/staged/{묶음}/stage.json`에 상태와 곡 목록을 두고,
  곡은 `var/staged/{묶음}/{곡}/song.zip`(압축하지 않은 zip, 곡 폴더 안 경로 그대로)에 하나의 파일로 둡니다.
  작은 파일(키음 등)을 낱개로 풀어 두면 파일마다 디스크 할당 단위만큼 자리를 차지해 실제 크기보다 몇 배로 커지기 때문입니다.
  commit할 때 곡 하나씩 작업 폴더에 풀어 넣고 지웁니다. 디스크에 있으므로 서버를 다시 시작해도 남습니다.
  (이전 버전에서 만든 묶음은 `{곡}/{폴더명}/` 폴더 그대로 두고 읽습니다.)
- 곡(unit): 차트 파일이 바로 아래 있는 폴더 하나. 대상(target)은 "auto"(겹치는 차트로 판정, 기본),
  "new"(새 곡), 곡 번호(그 곡에 추가), "skip"(넣지 않음)입니다. 차트마다 대상을 따로 줄 수도 있습니다(None이면 곡의 대상).
- 차트를 여러 대상으로 나누면 대상마다 그 차트, 그 차트가 가리키는 파일(키음·BGA·배너 등), 어느 차트도 가리키지 않는 파일을 묶어 넣습니다.
- 파일이 SIMILAR_FILES개 미만인 곡(차분·추가 채보로 보이는 곡)은 겹치는 차트가 없으면 #TITLE·#ARTIST·#BPM·#GENRE 중
  2개 이상 같은 기존 곡을 비슷한 곡(similar) 후보로 찾고, 가장 많이 겹치는 곡이 하나면 auto는 그 곡에 추가합니다.
  파일이 그보다 많은 곡은 겹치는 차트로만 판정합니다.
- commit은 배치로 넣고(importer.Job의 천천히 모드 그대로), 실패하거나 충돌(내용이 다른 같은 경로 파일)이 난 곡은 묶음에 남겨 다시 검토합니다.
- 캐시(cache.py, SSD)를 켜 두면 원본과 풀린 크기 추정이 한도 안인 묶음은 `cache/staged/{묶음}/`에 둡니다. commit 작업 폴더(`_work`)도 묶음 안이라 같이 따라갑니다.
"""
import errno
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

from . import cache, constant, db, importer, rar, sevenzip
from .db import META_HEADERS, Database, _chart_refs, chart_meta, meta_keys, song_dir_entries

logger = logging.getLogger(__name__)

STAGE_FILE = "stage.json"
# 곡 하나의 파일을 묶어 두는 압축하지 않은 zip
PACK_FILE = "song.zip"
# 작업 중 stage.json을 저장하는 간격(초). 곡마다 저장하면 곡이 많을 때 쓰기가 너무 많아집니다.
SAVE_INTERVAL = 2.0
# 올린 zip·rar·7z를 다 풀 때까지 두는 묶음 안 폴더
SOURCE_DIR = "_source"
# var/tmp에서 곡 폴더를 옮기는 동안 곡 자리에 두는 원래 위치. 옮기다 꺼지면 recover가 보고 var/tmp로 되돌립니다.
ORIGIN_FILE = ".origin.json"
# 서버가 다시 시작될 때 이어서 하는 최대 횟수(작업 때문에 서버가 꺼져 계속 다시 시작하는 것을 막음)
MAX_RESUMES = 5
# 검토 화면 한 쪽의 곡 수
UNITS_PER_PAGE = 50
# 판정(suggestion.kind): new 새 곡, add 기존 곡에 추가, ambiguous 기존 곡 여러 개와 겹침, unused 사용 중지한 곡과 겹침,
# similar 겹치는 차트는 없지만 이름·작곡가·BPM·장르 중 2개 이상 같은 기존 곡이 있음
SUGGESTIONS = ("new", "add", "ambiguous", "unused", "similar")
# 파일이 이 수 미만인 곡만 비슷한 곡을 찾습니다. 키음·BGA까지 다 든 곡은 보통 이보다 많고, 적으면 기존 곡의 추가 채보입니다.
SIMILAR_FILES = 50
# 비슷한 곡이 되려면 같아야 하는 항목(META_HEADERS) 수
SIMILAR_MIN_FIELDS = 2
# 비슷한 곡 후보 최대 수
SIMILAR_LIMIT = 10
# rar·7z는 풀린 크기를 미리 알 수 없어 압축 파일 크기의 이 배수로 추정합니다(캐시에 둘지 정할 때).
EXTRACT_RATIO = 2

_lock = threading.RLock()


class StageError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


# ---- 저장 ----

def _cache_root() -> pathlib.Path:
    return constant.CACHE_DIR / "staged"


def _roots() -> list[pathlib.Path]:
    """묶음을 두는 폴더들(캐시, var/staged). 캐시를 끈 뒤에도 캐시에 남은 묶음을 찾습니다."""
    return [root for root in (_cache_root(), constant.STAGED_DIR) if root.is_dir()]


def _dir(stage_id: str) -> pathlib.Path:
    if not stage_id or not all(c in "0123456789abcdef" for c in stage_id):
        raise StageError(404, "stage not found")
    cached = _cache_root() / stage_id
    return cached if cached.is_dir() else constant.STAGED_DIR / stage_id




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
    """이전 버전에서 풀어 둔 곡 폴더."""
    return _dir(stage["id"]) / unit["id"] / unit["name"]


def _pack_path(stage: dict, unit: dict) -> pathlib.Path:
    return _dir(stage["id"]) / unit["id"] / PACK_FILE


def _pack(song_dir: pathlib.Path, pack: pathlib.Path) -> None:
    """곡 폴더를 압축하지 않은 zip 하나로 묶습니다(빈 폴더 포함, 심볼릭 링크는 뺌)."""
    root = song_dir.resolve()
    with zipfile.ZipFile(pack, "w", zipfile.ZIP_STORED) as zf:
        for path, is_dir in song_dir_entries(root):
            rel = path.relative_to(root).as_posix()
            if is_dir:
                zf.writestr(rel + "/", b"")
            else:
                zf.write(path, rel)


def _read(stage: dict, unit: dict, rel: str) -> bytes:
    """곡 안 파일 하나의 내용."""
    pack = _pack_path(stage, unit)
    if pack.is_file():
        with zipfile.ZipFile(pack) as zf:
            return zf.read(rel)
    return (_unit_dir(stage, unit) / rel).read_bytes()


def _extract(pack: pathlib.Path, out: pathlib.Path) -> None:
    """곡 파일(pack)을 out 폴더에 풉니다."""
    out.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(pack) as zf:
        for info in zf.infolist():
            parts = [p for p in info.filename.split("/") if p]
            if not parts or any(p in (".", "..") for p in parts):
                continue
            target = out.joinpath(*parts)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as fin, open(target, "wb") as fout:
                shutil.copyfileobj(fin, fout, constant.BLOB_READ_SIZE)


def _unpack(stage: dict, unit: dict, work: pathlib.Path) -> pathlib.Path:
    """commit할 곡 폴더. 묶어 둔 곡은 work/{폴더명}에 풀고, 이전 버전의 폴더는 그대로 돌려줍니다."""
    pack = _pack_path(stage, unit)
    if not pack.is_file():
        return _unit_dir(stage, unit)
    out = work / unit["name"]
    _extract(pack, out)
    return out


def _zip_size(path: pathlib.Path) -> int:
    """zip의 풀린 크기. 읽지 못하면 파일 크기(풀 때 다시 실패합니다)."""
    try:
        with zipfile.ZipFile(path) as zf:
            return sum(info.file_size for info in zf.infolist())
    except (zipfile.BadZipFile, OSError):
        return path.stat().st_size if path.exists() else 0


def _tmp_size(root: pathlib.Path) -> int:
    """var/tmp의 곡 폴더와 압축 파일이 풀린 크기 추정."""
    total = 0
    for child in root.iterdir() if root.is_dir() else []:
        if child.is_symlink():
            continue
        if child.is_dir():
            # 곡 파일로 묶으면 할당 단위 낭비가 없어지므로 파일 크기의 합으로 셉니다.
            total += cache._du(child, apparent=True)
        elif child.suffix.lower() == ".zip":
            total += _zip_size(child)
        elif rar.is_volume(child.name) or sevenzip.is_7z(child.name):
            total += child.stat().st_size * EXTRACT_RATIO
    return total


def _stage_size(kind: str, sources) -> int:
    """묶음이 차지할 크기 추정: 다 풀 때까지 두는 원본 + 풀린 크기."""
    if kind == "tmp":
        return _tmp_size(constant.TMP_DIR)
    size = sum(path.stat().st_size for path, _ in sources)
    if kind == "zip":
        return size + sum(_zip_size(path) for path, _ in sources)
    return size * (1 + EXTRACT_RATIO)


def _new_stage(kind: str, name: str, sources: list[tuple[pathlib.Path, str]] = ()) -> dict:
    """새 묶음을 만듭니다. sources(올린 파일, 묶음 안 이름)는 묶음의 SOURCE_DIR로 옮겨 두고 다 풀면 지웁니다.
    서버가 도중에 꺼져도 다시 시작할 때 이 파일로 이어서 풉니다.
    원본과 풀린 크기 추정이 캐시 한도 안이면 캐시에, 아니면 var/staged에 둡니다."""
    stage = {
        "id": uuid.uuid4().hex,
        "kind": kind,
        "name": name,
        "status": "staging",  # staging / ready / committing
        "created_at": int(time.time()),
        "analyzed_at": None,
        "error": None,
        "source": [name for _, name in sources],
        "units": [],
    }
    path = cache.place("staged", constant.STAGED_DIR, stage["id"], _stage_size(kind, sources))
    with _lock:
        path.mkdir(parents=True)
        _save(stage)
        if sources:
            source = _dir(stage["id"]) / SOURCE_DIR
            source.mkdir()
            for src, name in sources:
                shutil.move(src, source / name)
    return stage


def _restore_origin(child: pathlib.Path) -> None:
    """var/tmp에서 옮기다 꺼진 곡 폴더(ORIGIN_FILE이 있는 곡 자리)를 var/tmp로 되돌립니다. 다음 가져오기에서 다시 옮깁니다."""
    try:
        origin = json.loads((child / ORIGIN_FILE).read_text("utf-8"))
        src = pathlib.Path(origin["from"])
    except (OSError, ValueError, KeyError, TypeError):
        return  # 옮기기 전(원본이 그대로 있음)
    pack = child / PACK_FILE
    if origin.get("packed") and pack.is_file():
        # 곡 파일로 다 묶은 뒤 옮긴 폴더를 지우던 중이었습니다. 곡 파일을 var/tmp에 풉니다.
        shutil.rmtree(src, ignore_errors=True)
        _extract(pack, src)
        logger.info(f"staging: returned song to var/tmp[{src}]")
        return
    moved = child / src.name
    if not moved.is_dir():
        return
    if origin.get("copied"):
        # 다른 디스크로 다 복사한 뒤 원본을 지우던 중이었습니다. 남은 원본은 복사본과 같으므로 지웁니다.
        shutil.rmtree(src, ignore_errors=True)
    elif src.exists():
        return  # 복사하다 꺼짐: 원본이 그대로 있습니다
    src.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(moved, src)
    logger.info(f"staging: returned song to var/tmp[{src}]")


def recover() -> None:
    """서버 시작 시: 이전 실행에서 풀거나 commit하던 중 꺼진 묶음을 그 자리부터 이어서 하는 작업을 시작하고, 목록에 없는 폴더를 지웁니다.
    마지막 저장(SAVE_INTERVAL) 뒤에 푼 곡은 다시 풀고, var/tmp에서 옮기던 곡은 var/tmp로 되돌려 다시 옮깁니다.
    MAX_RESUMES번 이어서 해도 끝나지 않으면(작업 때문에 서버가 꺼지는 경우) 멈추고 검토할 수 있게 둡니다."""
    resumes = []
    for path in (p for root in _roots() for p in root.iterdir()):
        if not path.is_dir():
            continue
        try:
            stage = _load(path.name)
        except (StageError, ValueError):
            logger.warning(f"staging: removed broken stage[{path}]")
            shutil.rmtree(path, ignore_errors=True)
            continue
        status = stage["status"]
        known = {unit["id"] for unit in stage["units"]}
        if status == "staging":
            known.add(SOURCE_DIR)
        for child in path.iterdir():
            if child.is_dir() and child.name not in known:
                _restore_origin(child)
                shutil.rmtree(child, ignore_errors=True)
        if status != "ready":
            stage["resumes"] = stage.get("resumes", 0) + 1
            if stage["resumes"] > MAX_RESUMES:
                logger.warning(f"staging: gave up resuming stage[{stage['id']}] after {MAX_RESUMES} restarts")
                stage["error"] = (
                    f"서버를 다시 시작할 때마다 이어서 했지만 {MAX_RESUMES}번 모두 끝나기 전에 꺼져 멈췄습니다. "
                    + ("이미 등록된 곡은 다시 commit해도 바뀌지 않습니다." if status == "committing" else "풀어 둔 곡까지만 남았습니다.")
                )
                stage["status"] = "ready"
                stage.pop("resumes")
                shutil.rmtree(path / SOURCE_DIR, ignore_errors=True)
            else:
                resumes.append(stage)
        if stage["units"] or stage["status"] != "ready":
            _save(stage)
        else:
            shutil.rmtree(path, ignore_errors=True)
    for stage in resumes:
        logger.info(f"staging: resuming {stage['status']} stage[{stage['id']}] {stage['name']}")
        if stage["status"] == "committing":
            job = importer.Job("commit", stage["name"], stage.get("slow", False))
            job.stage_id = stage["id"]
            job.resumed = True
            importer._start(job, lambda stage=stage, job=job: commit(stage["id"], job))
        else:
            job = importer.Job(stage["kind"], stage["name"], stage.get("slow", False))
            job.resumed = True
            importer._start(job, lambda stage=stage, job=job: _run(stage, job))


# ---- 곡 분석 ----

def _set_meta(chart: dict, content: bytes) -> None:
    meta = chart_meta(content)
    chart.update(meta)
    chart["keys"] = meta_keys(meta)


def _chart_info(rel: str, content: bytes) -> dict:
    chart = {
        "path": rel,
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "target": None,
    }
    _set_meta(chart, content)
    return chart


def _make_unit(unit_id: str, pack: pathlib.Path, name: str, folder: str) -> dict:
    """묶어 둔 곡(pack)의 정보. 차트는 곡 폴더 바로 아래의 차트 파일입니다(db.chart_files와 같음)."""
    with zipfile.ZipFile(pack) as zf:
        files = sorted((i for i in zf.infolist() if not i.is_dir()), key=lambda i: i.filename)
        charts = [
            _chart_info(i.filename, zf.read(i)) for i in files
            if "/" not in i.filename and pathlib.PurePath(i.filename).suffix.lower() in constant.BMS_FORMAT
        ]
    return {
        "id": unit_id,
        "name": name,
        "folder": folder,
        "files": len(files),
        "size": sum(i.file_size for i in files),
        "charts": charts,
        "target": "auto",
        "suggestion": None,
        "result": None,
    }


def _similar(cur, unit: dict) -> tuple[list[int], dict[str, list[str]], int | None]:
    """파일이 적고 겹치는 차트가 없는 곡과 META_HEADERS 중 SIMILAR_MIN_FIELDS개 이상 같은 기존 곡.
    (후보 곡 번호(많이 겹치는 순), {곡 번호: 같은 항목}, auto로 추가할 곡 번호(가장 많이 겹치는 곡이 하나일 때))."""
    if unit["files"] >= SIMILAR_FILES:
        return [], {}, None
    mine = {field: {chart["keys"][field] for chart in unit["charts"] if chart["keys"].get(field)} for field in META_HEADERS}
    # 후보: 이름이나 작곡가가 같거나, BPM과 장르가 같은 차트의 곡. 두 항목이 같으려면 이 중 하나는 맞아야 합니다.
    where, args = [], []
    for field in ("title", "artist"):
        if mine[field]:
            where.append(f"{field}_key IN ({', '.join(['%s'] * len(mine[field]))})")
            args.extend(sorted(mine[field]))
    for bpm in sorted(mine["bpm"]):
        for genre in sorted(mine["genre"]):
            where.append("(bpm_key = %s AND genre_key = %s)")
            args.extend((bpm, genre))
    if not where:
        return [], {}, None
    cur.execute(f"SELECT DISTINCT song_id FROM chart WHERE song_id IS NOT NULL AND ({' OR '.join(where)})", tuple(args))
    ids = sorted(row[0] for row in cur.fetchall())
    if not ids:
        return [], {}, None
    cur.execute(
        f"SELECT song_id, title_key, artist_key, bpm_key, genre_key FROM chart WHERE song_id IN ({', '.join(['%s'] * len(ids))})",
        tuple(ids),
    )
    theirs: dict[int, dict[str, set[str]]] = {}
    for song_id, *keys in cur.fetchall():
        song = theirs.setdefault(song_id, {field: set() for field in META_HEADERS})
        for field, key in zip(META_HEADERS, keys):
            if key:
                song[field].add(key)
    matches = {}
    for song_id, song in theirs.items():
        same = [field for field in META_HEADERS if mine[field] & song[field]]
        if len(same) >= SIMILAR_MIN_FIELDS:
            matches[song_id] = same
    songs = sorted(matches, key=lambda i: (-len(matches[i]), i))[:SIMILAR_LIMIT]
    top = [i for i in songs if len(matches[i]) == len(matches[songs[0]])] if songs else []
    merge = top[0] if len(top) == 1 else None
    return songs, {str(i): matches[i] for i in songs}, merge


def _fill_meta(stage: dict, unit: dict) -> None:
    """헤더를 읽기 전 버전에서 넣은 곡의 차트를 읽어 채웁니다."""
    for chart in unit["charts"]:
        if "keys" in chart:
            continue
        try:
            _set_meta(chart, _read(stage, unit, chart["path"]))
        except (OSError, KeyError, zipfile.BadZipFile):
            chart.update({field: "" for field in META_HEADERS}, keys={field: "" for field in META_HEADERS})


def _analyze(stage: dict) -> None:
    """곡마다 겹치는 기존 곡을 찾아 판정(suggestion)과 차트별 상태(songs, deleted)를 채웁니다. DB는 읽기만 합니다."""
    Database()  # 테이블 준비
    with db.connect() as con, con.cursor() as cur:
        for unit in stage["units"]:
            _fill_meta(stage, unit)
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
            merge, matches = None, {}
            if deleted is not None:
                kind = "unused"
            elif not songs:
                songs, matches, merge = _similar(cur, unit)
                kind = "similar" if songs else "new"
            elif len(songs) == 1:
                kind = "add"
            else:
                kind = "ambiguous"
            unit["suggestion"] = {"kind": kind, "songs": songs, "deleted": deleted, "merge": merge, "matches": matches}


def analyze(stage_id: str) -> dict:
    """묶음의 판정을 지금 DB 기준으로 다시 합니다(그 사이 다른 묶음을 commit했거나 곡을 사용 중지한 경우)."""
    with _lock:
        stage = _load(stage_id)
        _require_ready(stage)
        _analyze(stage)
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
        # 이어서 풀 때 이미 묶음에 있는 곡(곡 이름)은 건너뜁니다.
        self.done = {unit["folder"] for unit in stage["units"]}

    def unit_dir(self) -> tuple[str, pathlib.Path]:
        unit_id = uuid.uuid4().hex[:12]
        return unit_id, _dir(self.stage["id"]) / unit_id

    def add(self, unit_id: str, song_dir: pathlib.Path, folder: str) -> None:
        """곡 자리(unit_id)에 옮겨 둔 곡 폴더를 곡 파일로 묶어 더하고 폴더는 지웁니다."""
        work = _dir(self.stage["id"]) / unit_id
        try:
            _pack(song_dir, work / PACK_FILE)
        except Exception as e:
            logger.exception(f"staging failed[{song_dir}]")
            _restore_origin(work)
            shutil.rmtree(work, ignore_errors=True)
            self.job.songs.append({"folder": folder, "error": str(e) or type(e).__name__})
            return
        origin = work / ORIGIN_FILE
        if origin.is_file():
            # var/tmp에서 옮긴 곡: 폴더를 지우다 꺼져도 recover가 곡 파일로 되돌릴 수 있게 적어 둡니다.
            info = json.loads(origin.read_text("utf-8"))
            origin.write_text(json.dumps({**info, "packed": True}, ensure_ascii=False), "utf-8")
        shutil.rmtree(song_dir, ignore_errors=True)
        self.add_pack(unit_id, song_dir.name, folder)

    def add_pack(self, unit_id: str, name: str, folder: str) -> None:
        """곡 자리의 곡 파일(song.zip)을 곡으로 더합니다."""
        work = _dir(self.stage["id"]) / unit_id
        try:
            unit = _make_unit(unit_id, work / PACK_FILE, name, folder)
        except Exception as e:
            logger.exception(f"staging failed[{folder}]")
            shutil.rmtree(work, ignore_errors=True)
            self.job.songs.append({"folder": folder, "error": str(e) or type(e).__name__})
            return
        if not unit["charts"]:
            shutil.rmtree(work, ignore_errors=True)
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
        """판정을 채우고 검토할 수 있게 합니다. 곡이 하나도 없으면 묶음을 지웁니다. 다 푼 원본(SOURCE_DIR)은 지웁니다."""
        stage = self.stage
        cache.release(_dir(stage["id"]))
        if not stage["units"]:
            shutil.rmtree(_dir(stage["id"]), ignore_errors=True)
            self.job.stage_id = None
            return
        try:
            _analyze(stage)
            stage["analyzed_at"] = int(time.time())
        except Exception:
            logger.exception(f"staging analyze failed[{stage['id']}]")
        stage["status"] = "ready"
        stage["error"] = error
        stage.pop("resumes", None)
        self.save()
        shutil.rmtree(_dir(stage["id"]) / SOURCE_DIR, ignore_errors=True)


def _stage_zip(writer: _Writer, path: pathlib.Path, filename: str) -> int:
    """zip의 곡을 묶음에 풉니다. 푼 곡 수를 반환합니다(실패한 곡 포함, 이미 묶음에 있어 건너뛴 곡 제외).
    zip이 아니면 zipfile.BadZipFile."""
    stem = pathlib.PurePath(filename.replace("\\", "/")).stem
    if stem in ("", ".", ".."):
        stem = "song"
    with zipfile.ZipFile(path) as zf:
        groups = [
            (root, members) for root, members in importer._song_groups(zf).items()
            if "/".join((stem, *root)) not in writer.done
        ]
        writer.job.total = (writer.job.total or 0) + len(groups)
        for root, members in groups:
            name = root[-1] if root else stem
            folder = "/".join((stem, *root))
            unit_id, work = writer.unit_dir()
            # 폴더에 풀지 않고 곡 파일(압축하지 않은 zip)로 바로 옮겨 담습니다.
            try:
                work.mkdir()
                with zipfile.ZipFile(work / PACK_FILE, "w", zipfile.ZIP_STORED) as pack:
                    for info, rel in members:
                        with zf.open(info) as fin, pack.open(
                            "/".join(rel), "w", force_zip64=info.file_size >= zipfile.ZIP64_LIMIT
                        ) as fout:
                            shutil.copyfileobj(fin, fout, constant.BLOB_READ_SIZE)
            except Exception as e:
                logger.exception(f"staging failed[{filename}:{folder}]")
                shutil.rmtree(work, ignore_errors=True)
                writer.error(folder, str(e) or type(e).__name__)
                continue
            writer.add_pack(unit_id, name, folder)
    return len(groups)


def _move_out(song_dir: pathlib.Path, work: pathlib.Path, folder: str) -> None:
    """var/tmp의 곡 폴더를 곡 자리(work)로 옮깁니다. 옮기다 꺼져도 recover가 var/tmp로 되돌릴 수 있게
    먼저 원래 위치(ORIGIN_FILE)를 적어 둡니다. 다른 디스크면 다 복사한 뒤 그렇게 적고 원본을 지웁니다."""
    origin = work / ORIGIN_FILE
    origin.write_text(json.dumps({"from": str(song_dir), "folder": folder}, ensure_ascii=False), "utf-8")
    try:
        os.rename(song_dir, work / song_dir.name)
        return
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
    shutil.copytree(song_dir, work / song_dir.name, symlinks=True)
    origin.write_text(json.dumps({"from": str(song_dir), "folder": folder, "copied": True}, ensure_ascii=False), "utf-8")
    shutil.rmtree(song_dir, ignore_errors=True)
    if song_dir.exists():
        logger.warning(f"staging: could not remove moved song from var/tmp[{song_dir}]")


def _move_songs(writer: _Writer, song_dirs: list[pathlib.Path], root: pathlib.Path, origin: bool = False) -> None:
    """곡 폴더들을 묶음의 곡으로 옮깁니다. 곡 이름(folder)은 root 기준 경로입니다. 이미 묶음에 있는 곡은 건너뜁니다.
    origin이면(var/tmp의 원본) 옮기다 꺼져도 되돌릴 수 있게 옮깁니다(_move_out)."""
    song_dirs = [d for d in song_dirs if d.relative_to(root).as_posix() not in writer.done]
    writer.job.total = (writer.job.total or 0) + len(song_dirs)
    for song_dir in song_dirs:
        folder = song_dir.relative_to(root).as_posix()
        unit_id, work = writer.unit_dir()
        work.mkdir()
        try:
            if origin:
                _move_out(song_dir, work, folder)
            else:
                shutil.move(song_dir, work / song_dir.name)
        except OSError as e:
            logger.exception(f"staging failed[{song_dir}]")
            shutil.rmtree(work, ignore_errors=True)
            writer.error(folder, str(e) or type(e).__name__)
            continue
        writer.add(unit_id, work / song_dir.name, folder)


def _stage_rar(writer: _Writer, first: pathlib.Path, filename: str | None = None) -> None:
    """rar(첫 볼륨 first, 나머지 볼륨은 같은 폴더)를 묶음 안 임시 폴더에 통째로 푼 뒤 곡 폴더를 옮깁니다.
    최상위에 차트가 있으면 rar 이름을 곡 폴더명으로 씁니다. 풀지 못하면 rar.RarError."""
    name = rar.stem(pathlib.PurePath((filename or first.name).replace("\\", "/")).name)
    if name in ("", ".", ".."):
        name = "song"
    work = _dir(writer.stage["id"]) / f"rar-{uuid.uuid4().hex[:12]}"
    try:
        rar.extract(first, work / name)
        _move_songs(writer, importer.find_song_dirs(work / name), work)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _stage_7z(writer: _Writer, path: pathlib.Path, filename: str | None = None) -> None:
    """7z를 묶음 안 임시 폴더에 통째로 푼 뒤 곡 폴더를 옮깁니다(rar와 같음).
    최상위에 차트가 있으면 7z 이름을 곡 폴더명으로 씁니다. 풀지 못하면 sevenzip.SevenZipError."""
    name = pathlib.PurePath((filename or path.name).replace("\\", "/")).stem
    if name in ("", ".", ".."):
        name = "song"
    work = _dir(writer.stage["id"]) / f"7z-{uuid.uuid4().hex[:12]}"
    try:
        sevenzip.extract(path, work / name)
        _move_songs(writer, importer.find_song_dirs(work / name), work)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _stage_tmp(writer: _Writer) -> None:
    root = constant.TMP_DIR
    root.mkdir(parents=True, exist_ok=True)
    job = writer.job
    song_dirs = []
    for child in sorted(root.iterdir()):
        if child.is_symlink():
            logger.warning(f"import: skipped symlink[{child}]")
        elif child.is_dir():
            song_dirs.extend(importer.find_song_dirs(child))
    _move_songs(writer, song_dirs, root, origin=True)
    for path in sorted(p for p in root.iterdir() if p.suffix.lower() == ".zip"):
        if path.is_symlink() or not path.is_file():
            if path.is_symlink():
                logger.warning(f"import: skipped symlink[{path}]")
            continue
        start = len(job.songs)
        try:
            _stage_zip(writer, path, path.name)
        except importer.ZipNameEncodingError as e:
            logger.warning(f"stage_tmp: {e} [{path}]")
            writer.error(path.name, str(e))
            continue
        except (zipfile.BadZipFile, OSError) as e:
            logger.warning(f"stage_tmp: cannot read zip[{path}]: {e}")
            writer.error(path.name, f"zip을 읽지 못했습니다(아직 복사 중일 수 있습니다): {e}")
            continue
        if not any("error" in song for song in job.songs[start:]):
            path.unlink(missing_ok=True)
    files = [p.name for p in root.iterdir() if rar.is_volume(p.name) and p.is_file() and not p.is_symlink()]
    for names in rar.volume_sets(files).values():
        start = len(job.songs)
        try:
            _stage_rar(writer, root / rar.first_volume(names))
        except (rar.RarError, OSError) as e:
            logger.warning(f"stage_tmp: cannot extract rar[{names[0]}]: {e}")
            writer.error(names[0], f"{e} (아직 복사 중이거나 볼륨이 빠졌을 수 있습니다)")
            continue
        if not any("error" in song for song in job.songs[start:]):
            for name in names:
                (root / name).unlink(missing_ok=True)
    for path in sorted(p for p in root.iterdir() if sevenzip.is_7z(p.name)):
        if path.is_symlink() or not path.is_file():
            if path.is_symlink():
                logger.warning(f"import: skipped symlink[{path}]")
            continue
        start = len(job.songs)
        try:
            _stage_7z(writer, path)
        except (sevenzip.SevenZipError, OSError) as e:
            logger.warning(f"stage_tmp: cannot extract 7z[{path}]: {e}")
            writer.error(path.name, f"{e} (아직 복사 중일 수 있습니다)")
            continue
        if not any("error" in song for song in job.songs[start:]):
            path.unlink(missing_ok=True)


# 작업이 끝나지 못했을 때 묶음에 붙이는 오류의 앞부분
_FAILED = {
    "zip": "zip을 다 풀지 못했습니다",
    "rar": "rar를 다 풀지 못했습니다",
    "7z": "7z를 다 풀지 못했습니다",
    "tmp": "var/tmp를 다 옮기지 못했습니다",
}


def _run(stage: dict, job: importer.Job) -> dict | None:
    """묶음의 원본(올린 파일이나 var/tmp)을 풉니다. 처음 풀 때와 서버가 다시 시작돼 이어서 풀 때 모두 씁니다.
    곡이 없으면 묶음을 지우고 None."""
    job.stage_id = stage["id"]
    writer = _Writer(stage, job)
    source = _dir(stage["id"]) / SOURCE_DIR
    kind = stage["kind"]
    try:
        if kind != "tmp" and not (stage.get("source") and all((source / name).is_file() for name in stage["source"])):
            raise RuntimeError("올린 파일이 남아 있지 않아 이어서 풀 수 없습니다")
        if kind == "tmp":
            _stage_tmp(writer)
        elif kind == "zip":
            _stage_zip(writer, source / stage["source"][0], stage["name"])
        elif kind == "7z":
            _stage_7z(writer, source / stage["source"][0], stage["name"])
        else:
            _stage_rar(writer, source / rar.first_volume(stage["source"]), stage["name"])
    except BaseException as e:
        writer.finish(f"{_FAILED[kind]}: {e}")
        raise
    writer.finish()
    return stage if stage["units"] else None


def _source_name(filename: str, suffix: str) -> str:
    name = pathlib.PurePath(filename.replace("\\", "/")).name
    return name if name not in ("", ".", "..") else "source" + suffix


def stage_7z(path: pathlib.Path, filename: str, job: importer.Job) -> dict | None:
    """올린 7z 하나를 새 묶음으로 옮겨(path는 사라짐) 풉니다. 곡이 없으면 묶음을 만들지 않고 None."""
    return _run(_new_stage("7z", filename, [(path, _source_name(filename, ".7z"))]), job)


def stage_rar(volumes: list[tuple[pathlib.Path, str]], first: str, job: importer.Job) -> dict | None:
    """올린 rar 볼륨들((파일, 볼륨 이름), 분할 압축이 아니면 하나)을 새 묶음으로 옮겨 풉니다. first는 첫 볼륨 이름.
    곡이 없으면 묶음을 만들지 않고 None."""
    return _run(_new_stage("rar", first, volumes), job)


def stage_zip(path: pathlib.Path, filename: str, job: importer.Job) -> dict | None:
    """올린 zip 하나를 새 묶음으로 옮겨(path는 사라짐) 풉니다. 곡이 없으면 묶음을 만들지 않고 None."""
    return _run(_new_stage("zip", filename, [(path, _source_name(filename, ".zip"))]), job)


def stage_tmp(job: importer.Job) -> dict | None:
    """`var/tmp/`의 곡 폴더와 zip·rar·7z를 새 묶음으로 옮깁니다. 곡 폴더는 옮기고(원래 자리에서 사라짐),
    zip·7z와 rar(분할 압축이면 볼륨 전체)는 모든 곡을 풀면 지웁니다. 곡이 없으면 묶음을 만들지 않고 None."""
    return _run(_new_stage("tmp", "var/tmp"), job)


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
        "cached": _dir(stage["id"]).parent == _cache_root(),
    }


def list_stages() -> list[dict]:
    stages = []
    with _lock:
        for path in (p for root in _roots() for p in root.iterdir()):
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
        text = "\n".join(
            [unit["folder"], *(f"{c['path']}\n{c.get('title', '')}\n{c.get('artist', '')}\n{c.get('genre', '')}" for c in unit["charts"])]
        ).lower()
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
    """묶음 요약과 곡 한 쪽. show는 판정(new/add/ambiguous/unused/similar)이나 changed(대상을 바꾼 곡), failed(지난 commit에서 실패).
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
        similar_files=SIMILAR_FILES,
    )
    return result


def unit_files(stage_id: str, unit_id: str) -> list[dict]:
    """곡에서 차트가 아닌 파일 [{path, size}] (경로 순)."""
    with _lock:
        stage = _load(stage_id)
    unit = next((u for u in stage["units"] if u["id"] == unit_id), None)
    if unit is None:
        raise StageError(404, "unit not found")
    charts = {chart["path"] for chart in unit["charts"]}
    pack = _pack_path(stage, unit)
    if pack.is_file():
        with zipfile.ZipFile(pack) as zf:
            files = [
                {"path": i.filename, "size": i.file_size}
                for i in zf.infolist() if not i.is_dir() and i.filename not in charts
            ]
        return sorted(files, key=lambda f: f["path"].lower())
    root = _unit_dir(stage, unit)
    files = []
    for path in root.rglob("*") if root.is_dir() else []:
        if path.is_symlink() or not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel not in charts:
            files.append({"path": rel, "size": path.stat().st_size})
    return sorted(files, key=lambda f: f["path"].lower())


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


def _auto(unit: dict, target: str | int) -> str | int | None:
    """SongBatch.add의 target. auto는 비슷한 곡 판정의 추가할 곡이 있으면 그 곡, 없으면 None(겹치는 차트로 판정)."""
    if target != "auto":
        return target
    return (unit.get("suggestion") or {}).get("merge")


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
    removable: list[str] = []  # 등록해서 묶음에서 뺐지만 아직 stage.json에 저장하지 않은 곡(저장한 뒤 폴더를 지움)
    saved = time.monotonic()

    def save() -> None:
        nonlocal saved
        with _lock:
            _save(stage)
        saved = time.monotonic()
        for unit_id in removable:
            shutil.rmtree(root / unit_id, ignore_errors=True)
        removable.clear()

    def settle(committed: bool) -> None:
        """커밋된(또는 롤백된) 곡의 결과를 정리합니다. 배치를 커밋했으면(committed) 바로 저장해,
        서버가 꺼졌다 켜져 이어서 commit할 때 등록한 곡을 다시 넣지 않게 합니다."""
        for unit, infos in pending:
            errors = [info["error"] for info in infos if info.get("error")]
            conflicts = sorted({c for info in infos for c in info.get("conflicts", [])})
            if errors or conflicts:
                unit["result"] = {"error": "; ".join(errors) or None, "conflicts": conflicts, "songs": infos}
            else:
                done.add(unit["id"])
                removable.append(unit["id"])
        pending.clear()
        stage["units"] = [u for u in stage["units"] if u["id"] not in done]
        if committed or time.monotonic() - saved >= SAVE_INTERVAL:
            save()

    try:
        with Database().batch() as batch:
            for unit in units:
                groups = _groups(unit)
                infos: list[dict] = []
                if not groups:
                    job.songs.append({"folder": unit["folder"], "skipped": "admin"})
                    pending.append((unit, infos))
                    if not batch.open:
                        settle(False)
                    continue
                split = len(groups) > 1 or len(groups[0][1]) != len(unit["charts"])
                all_charts = [chart["path"] for chart in unit["charts"]]
                # 곡 데이터는 배치 트랜잭션과 메모리에 들어가므로 풀어 둔 폴더는 넣은 뒤 바로 지워도 됩니다.
                source = work_root / uuid.uuid4().hex
                try:
                    unit_dir = _unpack(stage, unit, source)
                except Exception as e:
                    logger.exception(f"commit failed[{unit['folder']}]")
                    unit_dir = None
                    info = {"folder": unit["folder"], "error": str(e) or type(e).__name__}
                    infos.append(info)
                    job.songs.append(info)
                for target, charts in groups if unit_dir is not None else []:
                    label = _label(unit, charts, split)
                    work = work_root / uuid.uuid4().hex
                    try:
                        song_dir = _subset(unit_dir, charts, all_charts, work) if split else unit_dir
                        job.apply_limits(batch)
                        info = importer._insert(batch, song_dir, label, remove=False, target=_auto(unit, target))
                    except Exception as e:
                        logger.exception(f"commit failed[{label}]")
                        info = {"folder": label, "error": str(e) or type(e).__name__}
                    finally:
                        shutil.rmtree(work, ignore_errors=True)
                    infos.append(info)
                    job.songs.append(info)
                shutil.rmtree(source, ignore_errors=True)
                pending.append((unit, infos))
                if not batch.open:
                    settle(True)
                job.rest(batch)
        settle(True)
    finally:
        shutil.rmtree(work_root, ignore_errors=True)
        with _lock:
            stage["status"] = "ready"
            stage["error"] = None
            stage.pop("resumes", None)
            if stage["units"]:
                try:
                    _analyze(stage)
                    stage["analyzed_at"] = int(time.time())
                except Exception:
                    logger.exception(f"staging analyze failed[{stage_id}]")
                save()
            else:
                shutil.rmtree(root, ignore_errors=True)


def start_commit(stage_id: str, slow: bool = False) -> importer.Job:
    with _lock:
        stage = _load(stage_id)
        _require_ready(stage)
        stage["status"] = "committing"
        stage["slow"] = slow  # 서버가 다시 시작돼 이어서 commit할 때도 같은 속도로
        _save(stage)
    job = importer.Job("commit", stage["name"], slow)
    job.stage_id = stage_id

    def run():
        commit(stage_id, job)

    return importer._start(job, run)
