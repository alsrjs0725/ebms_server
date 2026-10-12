"""관리자 로그 조회. `var/log/ebms.log`와 주 단위로 돌린 이전 파일(`ebms.log.YYYY-MM-DD`)을
최신 항목부터 거꾸로 읽어 수준·로거·검색어로 거릅니다.

한 번에 읽는 양을 제한하고, 더 볼 곳은 `next` 커서("파일명:오프셋")로 돌려줍니다.
줄 머리(`시각 수준 로거: 메시지`)가 없는 줄(트레이스백 등)은 앞 항목의 detail로 붙입니다.
"""
import logging
import re
from typing import Iterator

from . import constant

LOG_NAME = "ebms.log"
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
BLOCK = 1024 * 1024
SCAN_LIMIT = 64 * 1024 * 1024  # 요청 하나가 읽는 최대 바이트
MAX_LIMIT = 1000

HEADER = re.compile(rb"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d{3} (\w+) (\S+): ?(.*)$")


def log_files() -> list[str]:
    """최신 파일부터. 돌린 파일 접미사는 날짜라 이름 역순이 곧 시간 역순입니다."""
    if not constant.LOG_DIR.is_dir():
        return []
    rotated = sorted(
        (p.name for p in constant.LOG_DIR.iterdir() if p.is_file() and p.name.startswith(LOG_NAME + ".")),
        reverse=True,
    )
    current = [LOG_NAME] if (constant.LOG_DIR / LOG_NAME).is_file() else []
    return current + rotated


def loggers() -> list[str]:
    """지금 프로세스에 만들어진 로거 이름(필터 선택지)."""
    names = [name for name, obj in logging.root.manager.loggerDict.items() if isinstance(obj, logging.Logger)]
    return sorted(names)


def _reverse_lines(f, end: int) -> Iterator[tuple[int, bytes]]:
    """end 앞에서부터 (줄 시작 오프셋, 줄) 을 뒤에서 앞으로."""
    pos = end
    tail = b""
    while pos > 0:
        size = min(BLOCK, pos)
        pos -= size
        f.seek(pos)
        lines = (f.read(size) + tail).split(b"\n")
        starts = []
        off = pos
        for line in lines:
            starts.append(off)
            off += len(line) + 1
        tail = lines[0]
        for i in range(len(lines) - 1, 0, -1):
            yield starts[i], lines[i]
    yield 0, tail


def _entries(f, end: int) -> Iterator[tuple[int, dict | None]]:
    """(항목 시작 오프셋, 항목) 을 최신부터. 줄 머리 없는 파일 첫머리 조각은 항목 None."""
    extra: list[bytes] = []
    for start, line in _reverse_lines(f, end):
        line = line.rstrip(b"\r")
        m = HEADER.match(line)
        if not m:
            if line or extra:
                extra.append(line)
            continue
        extra.reverse()
        detail = b"\n".join(extra).rstrip(b"\n")
        extra = []
        yield start, {
            "time": m[1].decode(),
            "level": m[2].decode(),
            "logger": m[3].decode(errors="replace"),
            "message": m[4].decode(errors="replace"),
            "detail": detail.decode(errors="replace"),
        }
    if extra:
        yield 0, None


def _matches(entry: dict, min_level: int, logger: str, q: str) -> bool:
    if min_level and logging.getLevelName(entry["level"]) < min_level:
        return False
    if logger and entry["logger"] != logger and not entry["logger"].startswith(logger + "."):
        return False
    if q:
        text = f"{entry['time']} {entry['level']} {entry['logger']}: {entry['message']}\n{entry['detail']}"
        if q not in text.lower():
            return False
    return True


def parse_cursor(cursor: str, files: list[str]) -> tuple[int, int | None]:
    """커서를 (파일 순번, 끝 오프셋)으로. 없거나 알 수 없는 파일이면 ValueError."""
    if not cursor:
        return 0, None
    name, _, offset = cursor.rpartition(":")
    if name not in files or not offset.isdigit():
        raise ValueError("bad cursor")
    return files.index(name), int(offset)


def search(level: str = "", logger: str = "", q: str = "", cursor: str = "", limit: int = 200) -> dict:
    """조건에 맞는 항목을 최신부터 limit개. 더 있을 수 있으면 next에 이어 읽을 커서."""
    files = log_files()
    index, end = parse_cursor(cursor, files)
    min_level = logging.getLevelName(level.upper()) if level else 0
    if not isinstance(min_level, int):
        raise ValueError("bad level")
    limit = max(1, min(limit, MAX_LIMIT))
    q = q.strip().lower()
    logger = logger.strip()

    found: list[dict] = []
    budget = SCAN_LIMIT
    for i in range(index, len(files)):
        path = constant.LOG_DIR / files[i]
        try:
            f = open(path, "rb")
        except FileNotFoundError:  # 그 사이 돌았으면 건너뜀
            continue
        with f:
            size = f.seek(0, 2)
            file_end = size if end is None or i != index else min(end, size)
            for start, entry in _entries(f, file_end):
                if entry is not None and _matches(entry, min_level, logger, q):
                    entry["file"] = files[i]
                    found.append(entry)
                if len(found) >= limit or file_end - start >= budget:
                    more = start > 0 or i + 1 < len(files)
                    nxt = f"{files[i]}:{start}" if start > 0 else (f"{files[i + 1]}:{2**63 - 1}" if more else None)
                    return {"entries": found, "next": nxt, "scanned_to": entry and entry["time"]}
            budget -= file_end
        end = None
    return {"entries": found, "next": None, "scanned_to": None}
