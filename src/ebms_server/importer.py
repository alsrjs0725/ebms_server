"""곡 임포트. 폴더 트리에서 차트 파일이 있는 폴더를 곡 하나로 보고 DB에 등록합니다.

- 서버 시작 시와 관리자 페이지의 "var/tmp 가져오기"는 `var/tmp/`의 곡 폴더와 zip을 등록하고, 등록한 것은 지웁니다.
- 관리자 페이지는 zip을 조각으로 나눠 `var/import/`에 이어 붙이고(프록시의 요청 크기 제한을 피함),
  다 받으면 백그라운드 작업으로 곡을 하나씩 풀어 등록합니다. 작업 상태는 메모리에만 둡니다.
"""
import logging
import os
import pathlib
import shutil
import threading
import time
import uuid
import zipfile
from collections import OrderedDict

from . import constant
from .db import Database

logger = logging.getLogger(__name__)

# zip 항목 이름에 UTF-8 플래그가 없을 때 시도할 인코딩. 일본 BMS 패키지는 대부분 cp932입니다.
ZIP_NAME_ENCODINGS = ("utf-8", "cp932", "cp949")


def find_song_dirs(root: pathlib.Path) -> list[pathlib.Path]:
    """차트 파일이 바로 아래 있는 폴더 목록. 곡 폴더 안의 하위 폴더(bga 등)는 곡의 일부로 보고 더 들어가지 않습니다."""
    songs = []
    for dirpath, dirnames, filenames in os.walk(root):
        if any(pathlib.Path(f).suffix.lower() in constant.BMS_FORMAT for f in filenames):
            songs.append(pathlib.Path(dirpath))
            dirnames.clear()
        else:
            dirnames.sort()
    return songs


class Job:
    """백그라운드 임포트 작업 하나의 진행 상황."""

    def __init__(self, kind: str, name: str):
        self.id = uuid.uuid4().hex
        self.kind = kind
        self.name = name
        self.status = "running"  # running / done / failed
        self.total: int | None = None
        self.songs: list[dict] = []
        self.error: str | None = None
        self.started_at = int(time.time())

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
        }


def _insert(batch, song_dir: pathlib.Path, folder: str, remove: bool) -> dict:
    """곡 하나를 배치에 넣고 결과를 반환합니다. 실패해도 예외를 내지 않습니다.
    반환한 결과는 나중에 배치 커밋이 실패하면 "error"가 채워집니다."""
    try:
        info = batch.add(song_dir, remove=remove)
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
        if child.is_dir():
            song_dirs.extend(find_song_dirs(child))
    job.total = len(song_dirs)
    with Database().batch() as batch:
        for song_dir in song_dirs:
            job.songs.append(_insert(batch, song_dir, song_dir.relative_to(root).as_posix(), remove))
    return job.songs


def import_tmp(job: Job | None = None) -> list[dict]:
    """`var/tmp/` 아래 곡 폴더와 zip을 등록합니다. 등록한 곡 폴더는 지우고, zip은 모든 곡을 등록하면 지웁니다.
    내부망에서 큰 zip을 웹 업로드 대신 서버에 직접 복사해 넣을 때 씁니다."""
    job = job or Job("tmp", "var/tmp")
    import_tree(constant.TMP_DIR, remove=True, job=job)
    for path in sorted(p for p in constant.TMP_DIR.iterdir() if p.is_file() and p.suffix.lower() == ".zip"):
        start = len(job.songs)
        try:
            import_zip(path, path.name, job)
        except (zipfile.BadZipFile, OSError) as e:
            logger.warning(f"import_tmp: cannot read zip[{path}]: {e}")
            job.songs.append({"folder": path.name, "error": f"zip을 읽지 못했습니다(아직 복사 중일 수 있습니다): {e}"})
            continue
        songs = job.songs[start:]
        if songs and not any("error" in song for song in songs):
            path.unlink(missing_ok=True)
    return job.songs


def _entry_name(info: zipfile.ZipInfo) -> str:
    if info.flag_bits & 0x800:
        return info.filename
    # UTF-8 플래그가 없으면 zipfile은 cp437로 읽으므로 원래 바이트로 되돌려 다시 해석합니다.
    try:
        raw = info.filename.encode("cp437")
    except UnicodeEncodeError:
        return info.filename
    for encoding in ZIP_NAME_ENCODINGS:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return info.filename


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
    entries = []
    for info in zf.infolist():
        if info.is_dir():
            continue
        parts = _safe_parts(_entry_name(info))
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
                job.songs.append(_insert(batch, song_dir, "/".join((stem, *root)), remove=False))
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
    constant.IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(constant.IMPORT_DIR).free
    if size + DISK_MARGIN > free:
        raise UploadError(507, f"서버 디스크 공간이 부족합니다(남은 공간 {free / 1024 ** 3:.1f}GB, 필요 {(size + DISK_MARGIN) / 1024 ** 3:.1f}GB).")
    upload_id = uuid.uuid4().hex
    path = constant.IMPORT_DIR / f"{upload_id}.zip"
    path.touch()
    with _lock:
        _uploads[upload_id] = {"filename": filename, "size": size, "path": path}
    return {"id": upload_id, "received": 0, "size": size}


def _upload(upload_id: str) -> dict:
    with _lock:
        upload = _uploads.get(upload_id)
    if upload is None:
        raise UploadError(404, "upload not found")
    return upload


def write_chunk(upload_id: str, offset: int, data: bytes) -> dict:
    """offset 위치에 조각을 씁니다. offset은 지금까지 받은 크기와 같아야 합니다(다르면 409와 현재 크기)."""
    upload = _upload(upload_id)
    with _lock:
        received = upload["path"].stat().st_size
        if offset != received:
            raise UploadError(409, f"offset must be {received}")
        if received + len(data) > upload["size"]:
            raise UploadError(400, "chunk exceeds declared size")
        with open(upload["path"], "ab") as f:
            f.write(data)
    return {"id": upload_id, "received": received + len(data), "size": upload["size"]}


def finish_upload(upload_id: str) -> Job:
    """다 받은 zip의 임포트 작업을 백그라운드로 시작합니다. 작업이 끝나면 zip을 지웁니다."""
    upload = _upload(upload_id)
    received = upload["path"].stat().st_size
    if received != upload["size"]:
        raise UploadError(409, f"upload incomplete ({received}/{upload['size']} bytes)")
    with _lock:
        _uploads.pop(upload_id, None)
    job = Job("zip", upload["filename"])

    def run():
        try:
            import_zip(upload["path"], upload["filename"], job)
        finally:
            upload["path"].unlink(missing_ok=True)

    return _start(job, run)


def cancel_upload(upload_id: str) -> None:
    with _lock:
        upload = _uploads.pop(upload_id, None)
    if upload is not None:
        upload["path"].unlink(missing_ok=True)


def start_tmp_job() -> Job:
    """var/tmp 임포트를 시작합니다. 이미 돌고 있으면 그 작업을 반환합니다(같은 파일을 두 번 처리하지 않도록)."""
    with _lock:
        running = next((j for j in _jobs.values() if j.kind == "tmp" and j.status == "running"), None)
    if running is not None:
        return running
    job = Job("tmp", "var/tmp")
    return _start(job, lambda: import_tmp(job))


def _start(job: Job, run) -> Job:
    def target():
        try:
            run()
            job.status = "done"
        except zipfile.BadZipFile:
            job.status = "failed"
            job.error = "zip 파일이 아닙니다."
        except Exception as e:
            logger.exception(f"import job failed[{job.name}]")
            job.status = "failed"
            job.error = str(e) or type(e).__name__
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


def get_job(job_id: str) -> Job | None:
    with _lock:
        return _jobs.get(job_id)


def list_jobs() -> list[Job]:
    with _lock:
        return list(reversed(_jobs.values()))
