"""파일 내용(sha256) 기준으로 미디어 계산 결과를 재사용하는 메모리 캐시.

같은 파일을 여러 단계에서 다시 푸는 일을 줄입니다. 예를 들어 충돌 해결을 적용하거나 남은 곡을 다시 commit하면
병합(db._merge_into_song)이 이미 비교한 키음·이미지·영상을 처음부터 다시 비교하고,
렌더 비교(render_match)는 원본과 등록본에 같은 키음이 대부분 겹칩니다.

- memo: 작은 결과(같은 소재 판정, 품질 등). 키는 (용도, sha256, ...) 튜플로 호출하는 쪽이 정합니다. MEMO_ENTRIES개까지.
- decode_sounds: preview._decode_sounds와 같지만 (sha256, 길이, 샘플레이트, 채널)이 같은 키음은 한 번만 풉니다.
  풀린 샘플(float32)은 SOUND_CACHE_BYTES까지 두고, 오래 안 쓴 것부터 버립니다. 돌려주는 배열은 읽기 전용입니다.

판정 기준(임계값 등)은 코드 상수라 바뀌면 서버가 다시 시작되므로, 캐시는 프로세스 안에만 둡니다.
"""
import collections
import hashlib
import os
import pathlib
import threading

import numpy as np

from . import constant, preview

MEMO_ENTRIES = 200_000
SOUND_CACHE_BYTES = 256 << 20

_MISSING = object()


class _LRU:
    """크기(max_size) 안에서 오래 안 쓴 것부터 버리는 사전. 스레드 안전."""

    def __init__(self, max_size: int):
        self.max_size = max_size
        self._items: collections.OrderedDict = collections.OrderedDict()
        self._size = 0
        self._lock = threading.Lock()

    def get(self, key, default=None):
        with self._lock:
            item = self._items.get(key, _MISSING)
            if item is _MISSING:
                return default
            self._items.move_to_end(key)
            return item[0]

    def put(self, key, value, size: int = 1) -> None:
        if size > self.max_size:
            return
        with self._lock:
            old = self._items.pop(key, None)
            if old is not None:
                self._size -= old[1]
            self._items[key] = (value, size)
            self._size += size
            while self._size > self.max_size:
                _, (_, dropped) = self._items.popitem(last=False)
                self._size -= dropped

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._size = 0


_memo = _LRU(MEMO_ENTRIES)
_sounds = _LRU(SOUND_CACHE_BYTES)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: os.PathLike) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(constant.READ_SIZE):
            h.update(chunk)
    return h.hexdigest()


def memo_get(key, default=None):
    return _memo.get(key, default)


def memo_put(key, value) -> None:
    _memo.put(key, value)


def decode_sounds(
    ffmpeg: str, paths: list[pathlib.Path], seconds: float | None, rate: int = preview.SAMPLE_RATE,
    channels: int = 1, nice: bool = True, hashes: list[str] | None = None,
) -> list[np.ndarray | None]:
    """preview._decode_sounds와 같은 결과를 내되 내용이 같은 파일은 캐시에서 꺼냅니다(못 푼 파일도 None으로 기억).
    hashes는 paths의 sha256(이미 알면 다시 읽지 않음)."""
    hashes = hashes or [sha256_file(p) for p in paths]
    keys = [("sound", h, seconds, rate, channels) for h in hashes]
    out: list[np.ndarray | None] = [None] * len(paths)
    todo: dict[tuple, int] = {}  # 키 → 풀 파일 번호(같은 내용은 한 번만)
    for i, key in enumerate(keys):
        hit = _sounds.get(key)
        if hit is not None:
            out[i] = None if hit is _MISSING else hit
        elif key not in todo:
            todo[key] = i
    if todo:
        order = list(todo.items())
        decoded = preview._decode_sounds(ffmpeg, [paths[i] for _, i in order], seconds, rate, channels, nice=nice)
        for (key, _), data in zip(order, decoded):
            if data is None:
                _sounds.put(key, _MISSING, 1)
                continue
            data.flags.writeable = False
            _sounds.put(key, data, data.nbytes)
            todo[key] = data
        for i, key in enumerate(keys):
            if key in todo:
                value = todo[key]
                out[i] = value if isinstance(value, np.ndarray) else None
    return out


def clear() -> None:
    _memo.clear()
    _sounds.clear()
