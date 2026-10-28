"""곡 zip·차트 청크·사전 청크 파일 저장소(constant.STORE_DIR, #152).

내용은 파일로 두고 DB에는 크기·sha256·상대 경로(path)만 둡니다. 파일 이름에 sha256 앞부분을 넣어 같은 행을 바꿀 때
새 이름으로 쓰고, 옛 파일은 DB가 더 가리키지 않게 된 뒤 gc가 지웁니다. 받는 중인 다운로드는 열어 둔 파일로 끝까지 받습니다.

쓰기 순서: 임시 파일에 쓰고 fsync → 최종 이름으로 rename → 폴더 fsync → (호출한 쪽이) DB 커밋.
커밋 전에 꺼지거나 롤백하면 아무도 가리키지 않는 파일이 남고, 이것도 gc가 지웁니다.
"""
import os
import pathlib
import time
import uuid
from typing import Iterable

from . import constant

TMP_MARK = ".tmp-"

def song_path(song_id: int, sha256: str) -> str:
    """곡 zip의 상대 경로. 폴더마다 1000곡."""
    return f"song/{song_id // 1000:04d}/{song_id}.{sha256[:16]}.zip"


def chart_chunk_path(chunk_no: int, sha256: str) -> str:
    return f"chart_chunk/{chunk_no:05d}.{sha256[:16]}.zip"


def pre_chunk_path(chunk_no: int, sha256: str) -> str:
    return f"pre_chunk/{chunk_no // 1000:04d}/{chunk_no:05d}.{sha256[:16]}.zip"


def full_path(rel: str) -> pathlib.Path:
    return constant.STORE_DIR / rel


def write(rel: str, data: bytes) -> str:
    """data를 rel에 씁니다. 같은 내용의 파일이 이미 있으면 그대로 둡니다. rel을 반환합니다."""
    path = full_path(rel)
    if path.is_file() and path.stat().st_size == len(data):
        # 이름에 sha256이 들어 있어 크기가 같으면 같은 내용입니다(예: 롤백 뒤 다시 넣기).
        return rel
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}{TMP_MARK}{uuid.uuid4().hex}")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    _fsync_dir(path.parent)
    return rel


def _fsync_dir(path: pathlib.Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def read(rel: str) -> bytes:
    return full_path(rel).read_bytes()


def read_range(rel: str, start: int, length: int) -> bytes:
    with open(full_path(rel), "rb") as f:
        return os.pread(f.fileno(), length, start)


def files(older_than: float) -> list[str]:
    """STORE_DIR 아래 파일 중 mtime이 older_than(epoch)보다 오래된 것의 상대 경로(임시 파일 포함)."""
    root = constant.STORE_DIR
    out = []
    if not root.is_dir():
        return out
    for dirpath, _, names in os.walk(root):
        for name in names:
            path = pathlib.Path(dirpath) / name
            try:
                if path.stat().st_mtime < older_than:
                    out.append(path.relative_to(root).as_posix())
            except FileNotFoundError:
                continue
    return out


def remove(rels: Iterable[str]) -> int:
    removed = 0
    for rel in rels:
        try:
            full_path(rel).unlink()
            removed += 1
        except FileNotFoundError:
            pass
    return removed


def cutoff() -> float:
    """gc가 지워도 되는 파일의 mtime 상한. 쓰고 아직 커밋하지 않은 파일을 지우지 않도록 여유를 둡니다."""
    return time.time() - constant.STORE_GC_GRACE_SECONDS
