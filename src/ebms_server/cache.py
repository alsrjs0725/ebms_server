"""빠른 디스크(SSD 등)를 임포트 중간 작업 공간으로 씁니다. 곡 데이터 전체는 MySQL(HDD)에 두고,
올린 파일(`import/`), 풀어 둔 대기 묶음(`staged/`), 미리듣기 임시 파일(`work/`)만 `cache/` 아래에 둡니다.

- `EBMS_CACHE_MAX_BYTES`(예: 50G)가 0이면 끕니다. 그때는 지금처럼 모두 `var/` 아래를 씁니다.
- 새 업로드·묶음을 만들 때 크기(묶음은 풀린 크기 추정)를 받아, 지금 쓰는 양 + 크기가 한도 안이고 디스크에 여유가 있으면 캐시에,
  아니면 `var/`(HDD)에 둡니다. 대기 묶음은 commit하거나 버릴 때까지 남으므로 한도에 계속 들어갑니다.
- 푸는 중인 묶음처럼 아직 다 쓰지 않은 자리는 reserve로 잡아 두고, 다 쓰면 release합니다(그 뒤로는 실제 크기로 셈).
"""
import logging
import os
import pathlib
import shutil
import threading

from . import constant

logger = logging.getLogger(__name__)

# 캐시 디스크에 남겨 둘 여유(추정보다 크게 풀리는 경우)
DISK_MARGIN = 1024 ** 3

_lock = threading.Lock()
_reserved: dict[pathlib.Path, int] = {}


def enabled() -> bool:
    return constant.CACHE_MAX_BYTES > 0


def _du(path: pathlib.Path) -> int:
    """폴더(또는 파일)의 바이트 수."""
    if path.is_file():
        return path.stat().st_size
    total = 0
    for dirpath, _, filenames in os.walk(path):
        for name in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
    return total


def usage() -> int:
    """캐시에서 쓰는 바이트(아직 다 쓰지 않은 예약 포함)."""
    if not constant.CACHE_DIR.is_dir():
        return sum(_reserved.values())
    used = _du(constant.CACHE_DIR)
    for path, size in _reserved.items():
        used += max(0, size - (_du(path) if path.exists() else 0))
    return used


def place(kind: str, fallback: pathlib.Path, name: str, size: int) -> pathlib.Path:
    """kind(import/staged) 아래 name을 둘 경로. 캐시에 size만큼 자리가 있으면 캐시에 예약하고 그 경로를,
    아니면 fallback/name을 반환합니다. 캐시 경로를 받았으면 다 쓴 뒤 release하세요."""
    if enabled():
        root = constant.CACHE_DIR / kind
        with _lock:
            try:
                root.mkdir(parents=True, exist_ok=True)
                free = shutil.disk_usage(root).free
                used = usage()
            except OSError as e:
                logger.warning(f"cache: unavailable[{root}]: {e}")
            else:
                if used + size <= constant.CACHE_MAX_BYTES and size + DISK_MARGIN <= free:
                    path = root / name
                    _reserved[path] = size
                    return path
                logger.info(
                    f"cache: {kind}/{name} ({size} bytes) does not fit "
                    f"(used {used}/{constant.CACHE_MAX_BYTES}, free {free}). Using {fallback}."
                )
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback / name


def release(path: pathlib.Path) -> None:
    """place로 잡은 예약을 풉니다(파일은 그대로, 이후 실제 크기로 셈)."""
    with _lock:
        _reserved.pop(path, None)


def work_dir() -> pathlib.Path:
    """짧게 쓰고 지우는 임시 파일(미리듣기 등) 폴더. 캐시가 꺼져 있으면 var/tmp."""
    if enabled():
        path = constant.CACHE_DIR / "work"
        try:
            path.mkdir(parents=True, exist_ok=True)
            return path
        except OSError as e:
            logger.warning(f"cache: unavailable[{path}]: {e}")
    return constant.TMP_DIR


def status() -> dict:
    """관리자 페이지용 사용량."""
    with _lock:
        used = usage() if enabled() else 0
    return {"enabled": enabled(), "used": used, "max": constant.CACHE_MAX_BYTES}
