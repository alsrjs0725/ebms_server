"""곡 임포트. 폴더 트리에서 차트 파일이 있는 폴더를 곡 하나로 보고 DB에 등록합니다.

- 관리자 페이지의 "var/tmp 가져오기"는(서버 시작 시에는 하지 않음) `var/tmp/`의 곡 폴더와 zip·rar를 대기 묶음(staging.py)으로 옮깁니다.
- 관리자 페이지는 zip(또는 rar 분할 압축의 볼륨들)을 조각으로 나눠 여러 개를 동시에 `var/import/`의 제자리(offset)에 쓰고
  (프록시의 요청 크기 제한을 피하고 왕복 대기를 겹침), 다 받으면 백그라운드 작업으로 곡을 대기 묶음에 풉니다.
  관리자가 검토하고 commit하면 그때 등록합니다.
  받는 중인 업로드는 `{id}.json`에 이름·크기·받은 구간을 적어 두므로 서버를 다시 시작해도 받은 구간은 다시 받지 않습니다.
  임포트 캐시(cache.py)에 자리가 있으면 `var/import/` 대신 `cache/import/`에 받습니다.
  작업 상태는 메모리에만 두고, 풀거나 commit하던 묶음은 서버를 다시 시작하면 staging.recover가 이어서 합니다.
- import_tree·import_tmp·import_zip은 검토 없이 바로 등록합니다(스크립트·테스트용).
- 작업을 "천천히"(Job.slow)로 돌리면 작은 배치로 커밋하고 커밋마다 쉬어 서비스(다운로드·DB 응답)에 디스크를 양보합니다.
  돌고 있는 작업도 바꿀 수 있습니다.
"""
import json
import logging
import os
import pathlib
import shutil
import threading
import time
import uuid
import zipfile
from collections import OrderedDict

from . import cache, charset, constant
from .db import AmbiguousSongError, Database

logger = logging.getLogger(__name__)

# zip 항목 이름에 UTF-8 플래그가 없을 때 시도할 인코딩. 일본 BMS 패키지는 대부분 cp932입니다.
# 점수가 같으면 앞의 인코딩을 고릅니다.
ZIP_NAME_ENCODINGS = ("utf-8", "cp932", "cp949")


def find_song_dirs(root: pathlib.Path) -> list[pathlib.Path]:
    """차트 파일이 바로 아래 있는 폴더 목록. 곡 폴더 안의 하위 폴더(bga 등)는 곡의 일부로 보고 더 들어가지 않습니다."""
    songs = []
    # 심볼릭 링크는 폴더·파일 모두 따라가지 않습니다(var/tmp 밖의 파일이 곡에 들어가지 않도록).
    for dirpath, dirnames, filenames in os.walk(root):  # followlinks=False
        if any(
            pathlib.Path(f).suffix.lower() in constant.BMS_FORMAT and not os.path.islink(os.path.join(dirpath, f))
            for f in filenames
        ):
            songs.append(pathlib.Path(dirpath))
            dirnames.clear()
        else:
            for name in [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]:
                logger.warning(f"import: skipped symlink[{os.path.join(dirpath, name)}]")
                dirnames.remove(name)
            dirnames.sort()
    return songs


class Job:
    """백그라운드 임포트 작업 하나의 진행 상황."""

    def __init__(self, kind: str, name: str, slow: bool = False):
        self.id = uuid.uuid4().hex
        self.kind = kind
        self.name = name
        self.status = "running"  # running / done / failed
        # 대기 묶음(staging)을 만들거나 commit하는 작업이면 그 묶음
        self.stage_id: str | None = None
        self.total: int | None = None
        self.songs: list[dict] = []
        self.error: str | None = None
        self.started_at = int(time.time())
        self._slow = slow
        self._wake = threading.Event()
        self._work_since = time.monotonic()
        self.resting_until: float | None = None
        # 서버가 다시 시작돼 이어서 하는 작업
        self.resumed = False
        # 지금 풀거나 옮기는 원본(압축 파일·곡 폴더). 실패 로그에 남깁니다.
        self.source: str | None = None

    @property
    def slow(self) -> bool:
        return self._slow

    @slow.setter
    def slow(self, value: bool) -> None:
        self._slow = value
        if not value:
            self._wake.set()  # 쉬는 중이면 바로 이어서 진행합니다.

    def apply_limits(self, batch) -> None:
        """다음 곡을 넣기 전에 배치의 커밋 기준을 지금 속도에 맞춥니다."""
        if self._slow:
            batch.max_bytes, batch.max_songs = constant.IMPORT_SLOW_BATCH_BYTES, constant.IMPORT_SLOW_BATCH_SONGS
        else:
            batch.max_bytes, batch.max_songs = constant.IMPORT_BATCH_BYTES, constant.IMPORT_BATCH_SONGS

    def rest(self, batch) -> None:
        """천천히 모드에서 배치를 막 커밋했으면(쓰기 잠금을 놓은 상태) 일한 시간에 비례해 쉽니다."""
        if not self._slow or batch.open:
            return
        now = time.monotonic()
        seconds = min(constant.IMPORT_SLOW_REST_MAX, (now - self._work_since) * constant.IMPORT_SLOW_REST_RATIO)
        self._wake.clear()
        if seconds > 0 and self._slow:  # clear 전에 보통 속도로 바뀌었으면 쉬지 않습니다.
            self.resting_until = time.time() + seconds
            self._wake.wait(seconds)
            self.resting_until = None
        self._work_since = time.monotonic()

    def json(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "status": self.status,
            "total": self.total,
            "done": len(self.songs),
            "songs": list(self.songs),
            "error": self.error,
            "started_at": self.started_at,
            "slow": self._slow,
            "resting": self.resting_until is not None,
            "stage_id": self.stage_id,
            "resumed": self.resumed,
        }


def _add(batch, job: Job, song_dir: pathlib.Path, folder: str, remove: bool) -> None:
    job.apply_limits(batch)
    job.songs.append(_insert(batch, song_dir, folder, remove))
    job.rest(batch)


def _insert(batch, song_dir: pathlib.Path, folder: str, remove: bool, target: str | int | None = None) -> dict:
    """곡 하나를 배치에 넣고 결과를 반환합니다. 실패해도 예외를 내지 않습니다. target은 SongBatch.add와 같습니다.
    반환한 결과는 나중에 배치 커밋이 실패하면 "error"가 채워집니다."""
    try:
        info = batch.add(song_dir, remove=remove, target=target)
    except AmbiguousSongError as e:
        logger.warning(f"import skipped[{song_dir}]: {e}")
        return {"folder": folder, "error": str(e)}
    except Exception as e:
        logger.exception(f"import failed[{song_dir}]")
        return {"folder": folder, "error": str(e) or type(e).__name__}
    if info is None:
        return {"folder": folder, "error": "등록하지 못했습니다(곡 크기가 DB 패킷 한도를 넘었을 수 있습니다). 서버 로그를 보세요."}
    info["folder"] = folder
    return info


def import_tree(root: pathlib.Path, remove: bool, job: Job | None = None) -> list[dict]:
    """root의 하위 폴더에서 곡 폴더를 모두 등록합니다(root 바로 아래 파일은 보지 않습니다).
    곡마다 결과를 반환하고, 한 곡이 실패해도 나머지는 계속합니다. remove면 등록에 성공한 곡 폴더를 지웁니다.
    """
    job = job or Job("tmp", root.name)
    root.mkdir(parents=True, exist_ok=True)
    song_dirs = []
    for child in sorted(root.iterdir()):
        if child.is_symlink():
            logger.warning(f"import: skipped symlink[{child}]")
        elif child.is_dir():
            song_dirs.extend(find_song_dirs(child))
    job.total = len(song_dirs)
    with Database().batch() as batch:
        for song_dir in song_dirs:
            _add(batch, job, song_dir, song_dir.relative_to(root).as_posix(), remove)
    return job.songs


def import_tmp(job: Job | None = None) -> list[dict]:
    """`var/tmp/` 아래 곡 폴더와 zip을 등록합니다. 등록한 곡 폴더는 지우고, zip은 모든 곡을 등록하면 지웁니다.
    내부망에서 큰 zip을 웹 업로드 대신 서버에 직접 복사해 넣을 때 씁니다."""
    job = job or Job("tmp", "var/tmp")
    import_tree(constant.TMP_DIR, remove=True, job=job)
    zips = sorted(p for p in constant.TMP_DIR.iterdir() if p.suffix.lower() == ".zip")
    for path in zips:
        if path.is_symlink() or not path.is_file():
            if path.is_symlink():
                logger.warning(f"import: skipped symlink[{path}]")
            continue
        start = len(job.songs)
        try:
            import_zip(path, path.name, job)
        except ZipNameEncodingError as e:
            logger.warning(f"import_tmp: {e} [{path}]")
            job.songs.append({"folder": path.name, "error": str(e)})
            continue
        except (zipfile.BadZipFile, OSError) as e:
            logger.warning(f"import_tmp: cannot read zip[{path}]: {e}")
            job.songs.append({"folder": path.name, "error": f"zip을 읽지 못했습니다(아직 복사 중일 수 있습니다): {e}"})
            continue
        songs = job.songs[start:]
        if songs and not any("error" in song for song in songs):
            path.unlink(missing_ok=True)
    return job.songs


class ZipNameEncodingError(ValueError):
    """UTF-8 플래그가 없는 zip 항목 이름을 ZIP_NAME_ENCODINGS 중 어느 것으로도 다 풀 수 없습니다."""


def _raw_name(info: zipfile.ZipInfo) -> bytes | None:
    """UTF-8 플래그가 없는 항목 이름의 원래 바이트. zipfile은 플래그가 없으면 cp437로 읽으므로 되돌립니다."""
    if info.flag_bits & 0x800:
        return None
    return info.filename.encode("cp437")


def _zip_name_encoding(zf: zipfile.ZipFile) -> str | None:
    """UTF-8 플래그가 없는 항목 이름 전체를 오류 없이 풀 수 있는 인코딩 중 이상한 문자가 가장 적은 것.
    zip 하나는 한 인코딩으로 만들어지므로 항목마다 따로 고르지 않습니다. 풀 이름이 없으면 None,
    맞는 인코딩이 없으면 ZipNameEncodingError."""
    raws = [raw for info in zf.infolist() if (raw := _raw_name(info)) is not None]
    if not raws:
        return None
    encoding, failures = charset.guess_encoding(raws, ZIP_NAME_ENCODINGS)
    if encoding is None:
        raise ZipNameEncodingError("zip 파일 이름의 인코딩을 알 수 없습니다(" + "; ".join(failures) + ")")
    return encoding


def _entry_name(info: zipfile.ZipInfo, encoding: str | None) -> str:
    raw = _raw_name(info)
    return info.filename if raw is None else raw.decode(encoding)


def _safe_parts(name: str) -> list[str] | None:
    """zip 항목 이름을 경로 조각으로 나눕니다. 절대 경로나 상위 폴더(..)를 가리키면 None."""
    name = name.replace("\\", "/")
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        return None
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        return None
    return parts


def _song_groups(zf: zipfile.ZipFile) -> dict[tuple[str, ...], list[tuple[zipfile.ZipInfo, list[str]]]]:
    """zip 항목을 곡 폴더별로 묶습니다. 곡 폴더는 차트 파일이 바로 아래 있는 가장 바깥 폴더이고,
    그 아래 항목(bga 등)은 모두 그 곡에 속합니다. 어느 곡에도 속하지 않거나 위험한 경로의 항목은 버립니다."""
    encoding = _zip_name_encoding(zf)
    entries = []
    for info in zf.infolist():
        if info.is_dir():
            continue
        parts = _safe_parts(_entry_name(info, encoding))
        if not parts:
            logger.warning(f"import_zip: skipped unsafe entry[{info.filename!r}]")
            continue
        entries.append((info, parts))
    chart_dirs = {
        tuple(parts[:-1]) for _, parts in entries
        if pathlib.PurePath(parts[-1]).suffix.lower() in constant.BMS_FORMAT
    }
    groups: dict[tuple[str, ...], list] = {}
    for info, parts in entries:
        for i in range(len(parts)):
            if tuple(parts[:i]) in chart_dirs:
                groups.setdefault(tuple(parts[:i]), []).append((info, parts[i:]))
                break
    return dict(sorted(groups.items()))


def import_zip(path: pathlib.Path, filename: str, job: Job | None = None) -> list[dict]:
    """zip 하나에서 곡을 하나씩 풀어 등록합니다(디스크에는 zip과 곡 하나만 더 필요). 청크는 배치마다 갱신합니다.
    zip 최상위에 차트가 있으면 zip 이름을 곡 폴더명으로 씁니다. zip이 아니면 zipfile.BadZipFile."""
    job = job or Job("zip", filename)
    stem = pathlib.PurePath(filename.replace("\\", "/")).stem
    if stem in ("", ".", ".."):
        stem = "song"
    with zipfile.ZipFile(path) as zf, Database().batch() as batch:
        groups = _song_groups(zf)
        job.total = (job.total or 0) + len(groups)
        for root, members in groups.items():
            name = root[-1] if root else stem
            work = constant.IMPORT_DIR / uuid.uuid4().hex
            song_dir = work / name
            try:
                for info, rel in members:
                    target = song_dir.joinpath(*rel)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(info) as fin, open(target, "wb") as fout:
                        shutil.copyfileobj(fin, fout, constant.BLOB_READ_SIZE)
                # 곡 데이터는 배치 트랜잭션과 메모리에 들어가므로 풀어둔 폴더는 바로 지워도 됩니다.
                _add(batch, job, song_dir, "/".join((stem, *root)), remove=False)
            except Exception as e:
                logger.exception(f"import failed[{filename}:{'/'.join(root)}]")
                job.songs.append({"folder": "/".join((stem, *root)), "error": str(e) or type(e).__name__})
            finally:
                shutil.rmtree(work, ignore_errors=True)
    return job.songs


# ---- 조각 업로드와 백그라운드 작업 ----

# 끝난 작업은 최근 것만 남깁니다.
MAX_JOBS = 50
# zip 크기 외에 남겨둘 여유 디스크(곡 하나를 풀 공간)
DISK_MARGIN = 2 * 1024 ** 3
# 이 시간(초) 동안 조각이 오지 않은 업로드는 버려진 것으로 보고 지웁니다(창을 닫는 등).
UPLOAD_KEEP_SECONDS = 24 * 3600

_lock = threading.Lock()
_uploads: dict[str, dict] = {}
_jobs: "OrderedDict[str, Job]" = OrderedDict()


class UploadError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def start_upload(filename: str, size: int) -> dict:
    """조각 업로드를 시작합니다. 디스크가 모자라면 UploadError(507)."""
    if size < 0:
        raise UploadError(400, "size must be >= 0")
    _drop_stale_uploads()
    upload_id = uuid.uuid4().hex
    # 캐시(SSD)에 자리가 있으면 거기에 받습니다. 아니면 var/import.
    path = cache.place("import", constant.IMPORT_DIR, f"{upload_id}.upload", size)
    if path.parent == constant.IMPORT_DIR:
        free = shutil.disk_usage(constant.IMPORT_DIR).free
        if size + DISK_MARGIN > free:
            raise UploadError(507, f"서버 디스크 공간이 부족합니다(남은 공간 {free / 1024 ** 3:.1f}GB, 필요 {(size + DISK_MARGIN) / 1024 ** 3:.1f}GB).")
    path.touch()
    meta = path.parent / f"{upload_id}.json"
    upload = {"filename": filename, "size": size, "path": path, "meta": meta, "ranges": []}
    _save_meta(upload)
    with _lock:
        _uploads[upload_id] = upload
    return {"id": upload_id, "received": 0, "size": size}


def _save_meta(upload: dict) -> None:
    """이름·크기·받은 구간을 `{id}.json`에 씁니다(임시 파일로 써서 바꿈)."""
    meta = upload["meta"]
    tmp = meta.with_name(meta.name + ".tmp")
    tmp.write_text(
        json.dumps(
            {"filename": upload["filename"], "size": upload["size"], "ranges": upload["ranges"]}, ensure_ascii=False
        ),
        "utf-8",
    )
    os.replace(tmp, meta)


def _merge_range(ranges: list[list[int]], start: int, end: int) -> list[list[int]]:
    """정렬된 [start, end) 구간 목록에 구간을 더해 겹치거나 맞닿은 구간을 합칩니다."""
    out: list[list[int]] = []
    for a, b in sorted([*ranges, [start, end]]):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def _received(upload: dict) -> int:
    return sum(b - a for a, b in upload["ranges"])


def _upload(upload_id: str) -> dict:
    with _lock:
        upload = _uploads.get(upload_id)
    if upload is None:
        raise UploadError(404, "upload not found")
    return upload


def _drop_upload(upload_id: str) -> None:
    with _lock:
        upload = _uploads.pop(upload_id, None)
    if upload is not None:
        upload["path"].unlink(missing_ok=True)
        upload["meta"].unlink(missing_ok=True)
        cache.release(upload["path"])


def _drop_stale_uploads() -> None:
    """UPLOAD_KEEP_SECONDS 동안 조각이 오지 않은 업로드를 지웁니다."""
    now = time.time()
    with _lock:
        stale = [i for i, u in _uploads.items() if now - _mtime(u["path"]) > UPLOAD_KEEP_SECONDS]
    for upload_id in stale:
        logger.info(f"import: removed stale upload[{upload_id}]")
        _drop_upload(upload_id)


def _mtime(path: pathlib.Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def recover() -> None:
    """서버 시작 시: 받던 업로드(`{id}.upload`와 `{id}.json`)를 다시 받을 수 있게 읽고, 나머지(이전 실행의 임시 폴더 등)는 지웁니다.
    관리자 페이지는 연결이 끊기면 받은 곳부터 다시 보내므로, 서버가 다시 시작돼도 업로드가 이어집니다."""
    for root in (constant.IMPORT_DIR, constant.CACHE_DIR / "import"):
        if root.is_dir():
            _recover_uploads(root)


def _recover_uploads(root: pathlib.Path) -> None:
    keep: set[str] = set()
    for meta in root.glob("*.json"):
        upload_id = meta.stem
        path = root / f"{upload_id}.upload"
        try:
            info = json.loads(meta.read_text("utf-8"))
            filename, size = str(info["filename"]), int(info["size"])
            ranges = info.get("ranges")
            if ranges is None:
                # 구간을 적지 않던 이전 버전은 앞에서부터 이어 붙였습니다.
                ranges = [[0, path.stat().st_size]] if path.is_file() and path.stat().st_size else []
            ranges = [[int(a), int(b)] for a, b in ranges]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if not path.is_file() or path.stat().st_size > size or time.time() - _mtime(path) > UPLOAD_KEEP_SECONDS:
            continue
        with _lock:
            _uploads[upload_id] = {"filename": filename, "size": size, "path": path, "meta": meta, "ranges": ranges}
        keep.update((meta.name, path.name))
    for child in root.iterdir():
        if child.name in keep:
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)
    if keep:
        logger.info(f"import: kept {len(keep) // 2} unfinished uploads")


class ChunkWriter:
    """업로드의 offset 위치에 조각을 받는 대로 씁니다(본문을 다 모으지 않음). 다 쓰면 finish가 그 구간을 받은 것으로 적습니다.
    같은 업로드의 조각 여러 개를 동시에 써도 되고, 같은 구간을 다시 보내면 덮어씁니다. 도중에 끊기면 abort하고 그 구간은
    받지 않은 것으로 남습니다."""

    def __init__(self, upload_id: str, offset: int):
        self.upload_id = upload_id
        self.upload = _upload(upload_id)
        if offset < 0 or offset > self.upload["size"]:
            raise UploadError(400, "offset out of range")
        self.offset = self.pos = offset
        self._fd = os.open(self.upload["path"], os.O_WRONLY)

    def write(self, data: bytes) -> None:
        if self.pos + len(data) > self.upload["size"]:
            self.abort()
            raise UploadError(400, "chunk exceeds declared size")
        view = memoryview(data)
        while view:
            n = os.pwrite(self._fd, view, self.pos)
            self.pos += n
            view = view[n:]

    def abort(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def finish(self) -> dict:
        self.abort()
        upload = self.upload
        with _lock:
            if _uploads.get(self.upload_id) is not upload:
                raise UploadError(404, "upload not found")
            if self.pos > self.offset:
                upload["ranges"] = _merge_range(upload["ranges"], self.offset, self.pos)
                _save_meta(upload)
            received = _received(upload)
        return {"id": self.upload_id, "received": received, "size": upload["size"]}


def write_chunk(upload_id: str, offset: int, data: bytes) -> dict:
    """offset 위치에 조각 하나를 씁니다(ChunkWriter를 한 번에 씀)."""
    writer = ChunkWriter(upload_id, offset)
    writer.write(data)
    return writer.finish()


def _staged_free() -> int:
    """rar·7z를 풀 var/staged 디스크의 남은 공간. 업로드가 캐시(SSD)에만 놓였으면 var/import는 없을 수 있어 그 폴더는 보지 않습니다."""
    constant.STAGED_DIR.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(constant.STAGED_DIR).free


def finish_upload(upload_id: str) -> Job:
    return finish_uploads([upload_id])


def finish_uploads(upload_ids: list[str]) -> Job:
    """다 받은 파일을 대기 묶음에 푸는 작업을 백그라운드로 시작합니다. 작업이 끝나면 올린 파일을 지웁니다.
    파일 하나면 zip·rar·7z, 여럿이면 한 rar 분할 압축의 볼륨들(첫 볼륨 포함)이어야 합니다."""
    from . import rar, sevenzip
    if not upload_ids or len(set(upload_ids)) != len(upload_ids):
        raise UploadError(400, "upload ids required")
    uploads = [_upload(i) for i in upload_ids]
    for upload in uploads:
        with _lock:
            complete = upload["size"] == 0 or upload["ranges"] == [[0, upload["size"]]]
            received = _received(upload)
        if not complete:
            raise UploadError(409, f"upload incomplete ({received}/{upload['size']} bytes)")
    names = [pathlib.PurePath(u["filename"].replace("\\", "/")).name for u in uploads]
    volumes = rar.volume_sets(names)
    if len(uploads) > 1 or volumes:
        if len(volumes) != 1 or len(next(iter(volumes.values()))) != len(names):
            raise UploadError(400, "여러 파일을 함께 올리려면 한 rar 분할 압축의 볼륨들이어야 합니다.")
        if len({n.lower() for n in names}) != len(names):
            raise UploadError(400, "같은 이름의 볼륨이 있습니다.")
        try:
            first = rar.first_volume(names)
        except rar.RarError as e:
            raise UploadError(400, str(e))
        # rar는 통째로 푸므로 rar 크기만큼 더 필요합니다.
        need = sum(u["size"] for u in uploads) + DISK_MARGIN
        free = _staged_free()
        if need > free:
            raise UploadError(507, f"서버 디스크 공간이 부족합니다(남은 공간 {free / 1024 ** 3:.1f}GB, rar를 풀려면 {need / 1024 ** 3:.1f}GB 필요).")
    seven = not volumes and sevenzip.is_7z(names[0])
    if seven:
        # 7z도 통째로 푸므로 7z 크기만큼 더 필요합니다.
        need = uploads[0]["size"] + DISK_MARGIN
        free = _staged_free()
        if need > free:
            raise UploadError(507, f"서버 디스크 공간이 부족합니다(남은 공간 {free / 1024 ** 3:.1f}GB, 7z를 풀려면 {need / 1024 ** 3:.1f}GB 필요).")
    with _lock:
        for upload_id in upload_ids:
            _uploads.pop(upload_id, None)
    for upload in uploads:
        # 다 받았으므로 이제 실제 크기로 셉니다.
        cache.release(upload["path"])
    # 올린 파일은 작업 스레드에서 새 묶음으로 옮깁니다. 묶음에 있으므로 풀다가 서버가 꺼져도 다시 시작할 때 이어서 풉니다.

    def drop_meta():
        for upload in uploads:
            upload["meta"].unlink(missing_ok=True)
            upload["path"].unlink(missing_ok=True)

    if seven:
        upload = uploads[0]
        job = Job("7z", upload["filename"])

        def run_7z():
            from . import staging
            try:
                staging.stage_7z(upload["path"], upload["filename"], job)
            finally:
                drop_meta()

        return _start(job, run_7z)

    if not volumes:
        upload = uploads[0]
        job = Job("zip", upload["filename"])

        def run():
            from . import staging
            try:
                staging.stage_zip(upload["path"], upload["filename"], job)
            finally:
                drop_meta()

        return _start(job, run)

    job = Job("rar", first)

    def run_rar():
        from . import staging
        # unrar가 나머지 볼륨을 이름으로 찾으므로 묶음 안 한 폴더에 원래 이름으로 모읍니다.
        try:
            staging.stage_rar([(u["path"], name) for u, name in zip(uploads, names)], first, job)
        finally:
            drop_meta()

    return _start(job, run_rar)


def cancel_upload(upload_id: str) -> None:
    _drop_upload(upload_id)


def start_tmp_job() -> Job:
    """var/tmp를 대기 묶음으로 옮기는 작업을 시작합니다. 이미 돌고 있으면 그 작업을 반환합니다(같은 파일을 두 번 처리하지 않도록)."""
    from . import staging
    with _lock:
        running = next((j for j in _jobs.values() if j.kind == "tmp" and j.status == "running"), None)
    if running is not None:
        return running
    job = Job("tmp", "var/tmp")
    return _start(job, lambda: staging.stage_tmp(job))


def _start(job: Job, run) -> Job:
    def target():
        try:
            run()
            job.status = "done"
        except zipfile.BadZipFile:
            job.status = "failed"
            job.error = "zip 파일이 아닙니다."
        except Exception as e:
            logger.exception(f"import job failed[{job.name}]" + (f" while processing [{job.source}]" if job.source else ""))
            job.status = "failed"
            job.error = (str(e) or type(e).__name__) + (f" ({job.source})" if job.source else "")
        if job.status == "done" and job.total == 0:
            job.error = "차트 파일(" + ", ".join(constant.BMS_FORMAT) + ")이 있는 폴더가 없습니다."
        logger.info(f"import job {job.status}[{job.name}]: {len(job.songs)} songs")

    with _lock:
        _jobs[job.id] = job
        while len(_jobs) > MAX_JOBS:
            oldest = next((k for k, j in _jobs.items() if j.status != "running"), None)
            if oldest is None:
                break
            del _jobs[oldest]
    threading.Thread(target=target, name=f"import-{job.id[:8]}", daemon=True).start()
    return job


def set_slow(job_id: str, slow: bool) -> Job | None:
    """작업의 속도를 바꿉니다. 끝난 작업은 그대로 반환합니다."""
    job = get_job(job_id)
    if job is not None and job.status == "running":
        job.slow = slow
    return job


def get_job(job_id: str) -> Job | None:
    with _lock:
        return _jobs.get(job_id)


def list_jobs() -> list[Job]:
    with _lock:
        return list(reversed(_jobs.values()))
