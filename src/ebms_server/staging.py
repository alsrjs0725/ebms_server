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
- 캐시(cache.py, SSD)를 켜 두면 풀린 크기 추정이 한도 안인 묶음은 `cache/staged/{묶음}/`에 풉니다. commit 작업 폴더(`_work`)도 묶음 안이라 같이 따라갑니다.
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

from . import cache, constant, db, importer, rar, sevenzip
from .db import META_HEADERS, Database, _chart_refs, chart_meta, meta_keys, song_dir_entries

logger = logging.getLogger(__name__)

STAGE_FILE = "stage.json"
# 곡 하나의 파일을 묶어 두는 압축하지 않은 zip
PACK_FILE = "song.zip"
# 작업 중 stage.json을 저장하는 간격(초). 곡마다 저장하면 곡이 많을 때 쓰기가 너무 많아집니다.
SAVE_INTERVAL = 2.0
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


def _unpack(stage: dict, unit: dict, work: pathlib.Path) -> pathlib.Path:
    """commit할 곡 폴더. 묶어 둔 곡은 work/{폴더명}에 풀고, 이전 버전의 폴더는 그대로 돌려줍니다."""
    pack = _pack_path(stage, unit)
    if not pack.is_file():
        return _unit_dir(stage, unit)
    out = work / unit["name"]
    out.mkdir(parents=True)
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
    return out


def _new_stage(kind: str, name: str, size: int) -> dict:
    """새 묶음. size(풀린 크기 추정)가 캐시 한도 안이면 캐시에, 아니면 var/staged에 둡니다."""
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
    path = cache.place("staged", constant.STAGED_DIR, stage["id"], size)
    with _lock:
        path.mkdir(parents=True)
        _save(stage)
    return stage


def recover() -> None:
    """서버 시작 시: 이전 실행에서 작업 도중 꺼진 묶음을 검토할 수 있는 상태로 되돌리고, 목록에 없는 폴더를 지웁니다."""
    for path in (p for root in _roots() for p in root.iterdir()):
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

    def unit_id(self) -> str:
        return uuid.uuid4().hex[:12]

    def add(self, unit_id: str, song_dir: pathlib.Path, folder: str) -> None:
        """곡 폴더(어디에 있든)를 묶음의 곡 파일로 묶어 더하고 폴더는 지웁니다. 묶지 못하면 폴더는 남깁니다."""
        work = _dir(self.stage["id"]) / unit_id
        work.mkdir(exist_ok=True)
        try:
            _pack(song_dir, work / PACK_FILE)
        except Exception as e:
            logger.exception(f"staging failed[{song_dir}]")
            shutil.rmtree(work, ignore_errors=True)
            self.job.songs.append({"folder": folder, "error": str(e) or type(e).__name__})
            return
        if self.add_pack(unit_id, song_dir.name, folder):
            shutil.rmtree(song_dir, ignore_errors=True)

    def add_pack(self, unit_id: str, name: str, folder: str) -> bool:
        """묶음의 {unit_id}/song.zip을 곡으로 더합니다. 읽지 못하면 False(곡 파일은 지움)."""
        work = _dir(self.stage["id"]) / unit_id
        try:
            unit = _make_unit(unit_id, work / PACK_FILE, name, folder)
        except Exception as e:
            logger.exception(f"staging failed[{folder}]")
            shutil.rmtree(work, ignore_errors=True)
            self.job.songs.append({"folder": folder, "error": str(e) or type(e).__name__})
            return False
        if not unit["charts"]:
            shutil.rmtree(work, ignore_errors=True)
            self.job.songs.append({"folder": folder, "error": "곡에 넣을 수 있는 차트 파일이 없습니다."})
            return True
        self.stage["units"].append(unit)
        self.job.songs.append({"folder": folder, "staged": True, "charts": len(unit["charts"])})
        if time.monotonic() - self._saved >= SAVE_INTERVAL:
            self.save()
        return True

    def error(self, folder: str, message: str) -> None:
        self.job.songs.append({"folder": folder, "error": message})

    def save(self) -> None:
        with _lock:
            _save(self.stage)
        self._saved = time.monotonic()

    def finish(self, error: str | None = None) -> None:
        """판정을 채우고 검토할 수 있게 합니다. 곡이 하나도 없으면 묶음을 지웁니다."""
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
            unit_id = writer.unit_id()
            work = _dir(writer.stage["id"]) / unit_id
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


def _move_songs(writer: _Writer, song_dirs: list[pathlib.Path], root: pathlib.Path) -> None:
    """곡 폴더들을 묶음의 곡 파일로 묶어 옮깁니다(원래 폴더는 지움). 곡 이름(folder)은 root 기준 경로입니다."""
    writer.job.total = (writer.job.total or 0) + len(song_dirs)
    for song_dir in song_dirs:
        writer.add(writer.unit_id(), song_dir, song_dir.relative_to(root).as_posix())


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
    for child in root.iterdir():
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


def stage_7z(path: pathlib.Path, filename: str, job: importer.Job) -> dict | None:
    """올린 7z 하나를 새 묶음으로 풉니다. 곡이 없으면 묶음을 만들지 않고 None."""
    stage = _new_stage("7z", filename, path.stat().st_size * EXTRACT_RATIO)
    job.stage_id = stage["id"]
    writer = _Writer(stage, job)
    try:
        _stage_7z(writer, path, filename)
    except BaseException as e:
        writer.finish(f"7z를 다 풀지 못했습니다: {e}")
        raise
    writer.finish()
    return stage if stage["units"] else None


def stage_rar(first: pathlib.Path, filename: str, job: importer.Job) -> dict | None:
    """올린 rar(분할 압축이면 첫 볼륨, 나머지는 같은 폴더) 하나를 새 묶음으로 풉니다. 곡이 없으면 묶음을 만들지 않고 None."""
    size = sum(p.stat().st_size for p in first.parent.iterdir() if rar.is_volume(p.name) and p.is_file())
    stage = _new_stage("rar", filename, size * EXTRACT_RATIO)
    job.stage_id = stage["id"]
    writer = _Writer(stage, job)
    try:
        _stage_rar(writer, first, filename)
    except BaseException as e:
        writer.finish(f"rar를 다 풀지 못했습니다: {e}")
        raise
    writer.finish()
    return stage if stage["units"] else None


def stage_zip(path: pathlib.Path, filename: str, job: importer.Job) -> dict | None:
    """올린 zip 하나를 새 묶음으로 풉니다. 곡이 없으면 묶음을 만들지 않고 None."""
    stage = _new_stage("zip", filename, _zip_size(path))
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
    """`var/tmp/`의 곡 폴더와 zip·rar·7z를 새 묶음으로 옮깁니다. 곡 폴더는 옮기고(원래 자리에서 사라짐),
    zip·7z와 rar(분할 압축이면 볼륨 전체)는 모든 곡을 풀면 지웁니다. 곡이 없으면 묶음을 만들지 않고 None."""
    root = constant.TMP_DIR
    root.mkdir(parents=True, exist_ok=True)
    stage = _new_stage("tmp", "var/tmp", _tmp_size(root))
    job.stage_id = stage["id"]
    writer = _Writer(stage, job)
    try:
        song_dirs = []
        for child in sorted(root.iterdir()):
            if child.is_symlink():
                logger.warning(f"import: skipped symlink[{child}]")
            elif child.is_dir():
                song_dirs.extend(importer.find_song_dirs(child))
        _move_songs(writer, song_dirs, root)
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
                    _analyze(stage)
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
