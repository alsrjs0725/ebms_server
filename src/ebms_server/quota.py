"""다운로드 할당량: 전역 설정, 플레이 티켓, 사전 다운로드 월 사용량·감속.

- 플레이 다운로드(/api/play/*)는 곡 1개당 티켓 1개를 씁니다. 티켓은 타이머 없이 요청 때
  `min(최대, 저장값 + 경과초 / 리필초)`로 계산합니다. 차감한 곡은 grant_seconds 동안 다시 차감하지 않습니다.
- 사전 다운로드(/api/pre/*)는 실제 전송한 바이트를 월별로 더하고, 한도를 넘으면 끊지 않고 감속합니다.
  감속 상태는 프로세스 메모리에 둡니다(워커 1개 전제).

수치는 setting 테이블의 전역 기본값을 쓰고, user 테이블의 같은 이름 컬럼이 NULL이 아니면 그 값이 우선합니다.
"""
import asyncio
import datetime
import math
import threading
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass

from fastapi.concurrency import run_in_threadpool

from . import constant, db

# 사용자별로 덮어쓸 수 있는 항목(user 테이블 컬럼과 이름이 같음)
USER_LIMITS = ("max_tickets", "refill_seconds", "pre_monthly_bytes", "pre_throttled_kbps")
# 항목별 허용 최솟값
MINIMUMS = {
    "max_tickets": 0,
    "refill_seconds": 1,
    "grant_seconds": 0,
    "pre_monthly_bytes": 0,
    "pre_throttled_kbps": 64,
}
# 정수 컬럼 범위를 넘지 않도록
MAXIMUMS = {name: 2 ** 31 - 1 for name in MINIMUMS} | {"pre_monthly_bytes": 2 ** 62}
KST = datetime.timezone(datetime.timedelta(hours=9))
# 사용량을 DB에 반영하는 간격(바이트). 다운로드가 끝나거나 끊길 때도 반영합니다.
USAGE_FLUSH_BYTES = 8 * 1024 * 1024
# 감속할 때 한 번에 보내는 크기
THROTTLE_PIECE = 16 * 1024
# 사용자당 최대 동시 다운로드 수
MAX_CONCURRENT_DOWNLOADS_PER_USER = 10


class ConcurrentDownloadTracker:
    """사용자별 동시 다운로드 수 제한."""

    def __init__(self, limit: int = MAX_CONCURRENT_DOWNLOADS_PER_USER):
        self.limit = limit
        self._counts: dict[str, int] = {}
        self._lock = threading.Lock()

    def acquire(self, user_id: str) -> bool:
        """다운로드 슬롯을 얻습니다. 한도를 초과하면 False."""
        with self._lock:
            current = self._counts.get(user_id, 0)
            if current >= self.limit:
                return False
            self._counts[user_id] = current + 1
            return True

    def release(self, user_id: str) -> None:
        """다운로드 슬롯을 반납합니다."""
        with self._lock:
            current = self._counts.get(user_id, 0)
            if current <= 1:
                self._counts.pop(user_id, None)
            else:
                self._counts[user_id] = current - 1


_download_tracker = ConcurrentDownloadTracker()


def acquire_download_slot(user_id: str) -> bool:
    return _download_tracker.acquire(user_id)


def release_download_slot(user_id: str) -> None:
    _download_tracker.release(user_id)


def validate(name: str, value) -> int:
    """설정 값을 검사해 정수로 반환합니다. 틀리면 ValueError."""
    if name not in MINIMUMS:
        raise ValueError(f"unknown setting: {name}")
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if not MINIMUMS[name] <= value <= MAXIMUMS[name]:
        raise ValueError(f"{name} must be between {MINIMUMS[name]} and {MAXIMUMS[name]}")
    return value


# ---- 전역 설정 ----

def get_settings() -> dict[str, int]:
    settings = dict(constant.DEFAULT_SETTINGS)
    with db.connect() as con, con.cursor() as cur:
        cur.execute("SELECT name, value FROM setting")
        for name, value in cur.fetchall():
            if name in settings:
                settings[name] = int(value)
    return settings


def update_settings(values: dict) -> dict[str, int]:
    """전역 설정을 일괄로 바꿉니다. 하나라도 틀리면 아무것도 바꾸지 않고 ValueError."""
    checked = {name: validate(name, value) for name, value in values.items()}
    with db.connect() as con, con.cursor() as cur:
        try:
            for name, value in checked.items():
                cur.execute("UPDATE setting SET value = %s WHERE name = %s", (str(value), name))
                if cur.rowcount == 0:
                    cur.execute("INSERT IGNORE INTO setting (name, value) VALUES (%s, %s)", (name, str(value)))
            con.commit()
        except Exception:
            con.rollback()
            raise
    return get_settings()


# ---- 사용자별 한도 ----

@dataclass
class Limits:
    max_tickets: int
    refill_seconds: int
    grant_seconds: int
    pre_monthly_bytes: int
    pre_throttled_kbps: int


def get_overrides(cur, user_id: str) -> dict[str, int | None]:
    cur.execute(f"SELECT {', '.join(USER_LIMITS)} FROM user WHERE id = %s", (user_id,))
    row = cur.fetchone()
    return dict(zip(USER_LIMITS, row)) if row else {name: None for name in USER_LIMITS}


def limits_for(user_id: str) -> Limits:
    settings = get_settings()
    with db.connect() as con, con.cursor() as cur:
        overrides = get_overrides(cur, user_id)
    return Limits(**{
        name: overrides[name] if overrides.get(name) is not None else settings[name]
        for name in constant.DEFAULT_SETTINGS
    })


def set_overrides(user_id: str, values: dict) -> None:
    """사용자별 한도를 바꿉니다. 값이 None이면 전역 기본값을 따르게 합니다."""
    checked = {}
    for name, value in values.items():
        if name not in USER_LIMITS:
            raise ValueError(f"unknown limit: {name}")
        checked[name] = None if value is None else validate(name, value)
    if not checked:
        return
    with db.connect() as con, con.cursor() as cur:
        cur.execute(
            f"UPDATE user SET {', '.join(f'{name} = %s' for name in checked)} WHERE id = %s",
            (*checked.values(), user_id),
        )
        con.commit()


# ---- 플레이 티켓 ----

@dataclass
class TicketState:
    tickets: float
    max_tickets: int
    refill_seconds: int
    # 다음 티켓 1개가 차는 시각(unix 초). 가득 찼으면 None.
    next_refill_at: int | None

    @property
    def available(self) -> int:
        return int(self.tickets)

    def retry_after(self, now: float) -> int:
        """티켓 1개가 찰 때까지 남은 초. 최대 티켓이 0이면 리필 1회분을 알려줍니다."""
        if self.max_tickets < 1:
            return self.refill_seconds
        return max(1, math.ceil((1 - self.tickets) * self.refill_seconds))


def _current_tickets(row, limits: Limits, now: float) -> float:
    if row is None:
        return float(limits.max_tickets)
    stored, updated_at = row
    return min(float(limits.max_tickets), stored + max(0.0, now - updated_at) / limits.refill_seconds)


def _state(tickets: float, limits: Limits, now: float) -> TicketState:
    next_refill_at = None
    if tickets < limits.max_tickets:
        next_refill_at = math.ceil(now + (1 - (tickets % 1)) * limits.refill_seconds)
    return TicketState(tickets, limits.max_tickets, limits.refill_seconds, next_refill_at)


def ticket_state(user_id: str, limits: Limits | None = None) -> TicketState:
    limits = limits or limits_for(user_id)
    now = time.time()
    with db.connect() as con, con.cursor() as cur:
        cur.execute("SELECT tickets, updated_at FROM user_ticket WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
    return _state(_current_tickets(row, limits, now), limits, now)


def _ensure_ticket_row(con, cur, user_id: str, limits: Limits, now: int) -> None:
    """행이 없으면 가득 찬 상태로 만듭니다. 잠금 트랜잭션과 분리해야 동시 요청이 데드락에 걸리지 않습니다."""
    cur.execute(
        "INSERT IGNORE INTO user_ticket (user_id, tickets, updated_at) VALUES (%s, %s, %s)",
        (user_id, float(limits.max_tickets), now),
    )
    con.commit()


class NoTicket(Exception):
    def __init__(self, retry_after: int):
        super().__init__(retry_after)
        self.retry_after = retry_after


def charge_play(user_id: str, song_id: int) -> bool:
    """곡 플레이 다운로드에 티켓 1개를 씁니다. 유효한 grant가 있으면 차감하지 않습니다.

    반환값: 이번에 차감했으면 True, grant 안이라 차감하지 않았으면 False. 티켓이 없으면 NoTicket.
    """
    limits = limits_for(user_id)
    # updated_at이 정수 컬럼이므로 시각도 정수로 맞춥니다(소수점 버림으로 티켓이 더 차지 않게).
    now = int(time.time())
    retry_after = None
    with db.connect() as con, con.cursor() as cur:
        _ensure_ticket_row(con, cur, user_id, limits, now)
        try:
            # 동시 요청은 이 행에서 줄을 섭니다.
            cur.execute("SELECT tickets, updated_at FROM user_ticket WHERE user_id = %s FOR UPDATE", (user_id,))
            row = cur.fetchone()
            cur.execute(
                "SELECT expires_at FROM download_grant WHERE user_id = %s AND song_id = %s",
                (user_id, song_id),
            )
            grant = cur.fetchone()
            charged = grant is None or grant[0] <= now
            tickets = _current_tickets(row, limits, now)
            if charged and tickets < 1:
                retry_after = _state(tickets, limits, now).retry_after(now)
            elif charged:
                cur.execute(
                    "UPDATE user_ticket SET tickets = %s, updated_at = %s WHERE user_id = %s",
                    (tickets - 1, now, user_id),
                )
                # 위에서 행 유무를 이미 봤으므로 UPDATE/INSERT 중 하나만 합니다(없는 행 DELETE의 gap lock으로 인한 데드락 방지).
                if grant is None:
                    cur.execute(
                        "INSERT INTO download_grant (user_id, song_id, charged_at, expires_at) VALUES (%s, %s, %s, %s)",
                        (user_id, song_id, now, now + limits.grant_seconds),
                    )
                else:
                    cur.execute(
                        "UPDATE download_grant SET charged_at = %s, expires_at = %s WHERE user_id = %s AND song_id = %s",
                        (now, now + limits.grant_seconds, user_id, song_id),
                    )
            con.commit()
        except Exception:
            con.rollback()
            raise
    if retry_after is not None:
        raise NoTicket(retry_after)
    return charged


def refill_tickets(user_id: str) -> None:
    """티켓을 최대치로 채웁니다(관리자)."""
    limits = limits_for(user_id)
    now = int(time.time())
    with db.connect() as con, con.cursor() as cur:
        _ensure_ticket_row(con, cur, user_id, limits, now)
        cur.execute(
            "UPDATE user_ticket SET tickets = %s, updated_at = %s WHERE user_id = %s",
            (float(limits.max_tickets), now, user_id),
        )
        con.commit()


# ---- 사전 다운로드 사용량·감속 ----

def current_month(now: float | None = None) -> str:
    """월 경계는 KST 1일 0시입니다."""
    return datetime.datetime.fromtimestamp(time.time() if now is None else now, KST).strftime("%Y-%m")


def pre_used(user_id: str, month: str | None = None) -> int:
    with db.connect() as con, con.cursor() as cur:
        cur.execute(
            "SELECT bytes FROM pre_usage WHERE user_id = %s AND month = %s",
            (user_id, month or current_month()),
        )
        row = cur.fetchone()
    return int(row[0]) if row else 0


def add_pre_usage(user_id: str, nbytes: int) -> None:
    if nbytes <= 0:
        return
    month = current_month()
    with db.connect() as con, con.cursor() as cur:
        # 데드락을 피하려고 행 만들기와 더하기를 따로 커밋합니다.
        cur.execute(
            "INSERT IGNORE INTO pre_usage (user_id, month, bytes) VALUES (%s, %s, 0)", (user_id, month)
        )
        con.commit()
        cur.execute(
            "UPDATE pre_usage SET bytes = bytes + %s WHERE user_id = %s AND month = %s",
            (nbytes, user_id, month),
        )
        con.commit()


class Throttle:
    """사용자별 토큰 버킷. 같은 사용자의 동시 요청이 하나의 속도를 나눠 씁니다."""

    _buckets: dict[str, "Throttle"] = {}
    _lock = threading.Lock()

    def __init__(self):
        self._available_at = 0.0
        self._lock = threading.Lock()

    @classmethod
    def for_user(cls, user_id: str) -> "Throttle":
        with cls._lock:
            bucket = cls._buckets.get(user_id)
            if bucket is None:
                bucket = cls._buckets[user_id] = Throttle()
            return bucket

    def reserve(self, nbytes: int, bytes_per_second: float) -> float:
        """nbytes를 보내기 전에 기다릴 초를 반환합니다."""
        with self._lock:
            now = time.monotonic()
            start = max(now, self._available_at)
            self._available_at = start + nbytes / bytes_per_second
            return start - now


async def metered(user_id: str, chunks: Iterator[bytes], close=None) -> AsyncIterator[bytes]:
    """사전 다운로드 응답 본문. 보낸 바이트를 이번 달 사용량에 더하고, 한도를 넘으면 감속합니다.

    chunks는 동기 iterator(DB 읽기)라 스레드에서 꺼내고, 감속 대기는 스레드를 붙잡지 않습니다.
    """
    limits = await run_in_threadpool(limits_for, user_id)
    used = await run_in_threadpool(pre_used, user_id)
    rate = limits.pre_throttled_kbps * 1000 / 8
    bucket = Throttle.for_user(user_id)
    pending = 0
    try:
        while True:
            chunk = await run_in_threadpool(next, chunks, None)
            if chunk is None:
                break
            throttled = used >= limits.pre_monthly_bytes
            pieces = [chunk[i:i + THROTTLE_PIECE] for i in range(0, len(chunk), THROTTLE_PIECE)] if throttled else [chunk]
            for piece in pieces:
                if throttled:
                    delay = bucket.reserve(len(piece), rate)
                    if delay > 0:
                        await asyncio.sleep(delay)
                yield piece
                used += len(piece)
                pending += len(piece)
            if pending >= USAGE_FLUSH_BYTES:
                await run_in_threadpool(add_pre_usage, user_id, pending)
                pending = 0
    finally:
        if close is not None:
            close()
        if pending:
            # 연결이 끊겨 취소된 경우에도 반영되도록 await 없이 바로 씁니다(짧은 쿼리 2개).
            add_pre_usage(user_id, pending)


def pre_state(user_id: str, limits: Limits | None = None) -> dict:
    limits = limits or limits_for(user_id)
    month = current_month()
    used = pre_used(user_id, month)
    return {
        "month": month,
        "used_bytes": used,
        "limit_bytes": limits.pre_monthly_bytes,
        "throttled_kbps": limits.pre_throttled_kbps,
        "throttled": used >= limits.pre_monthly_bytes,
    }


def ticket_json(state: TicketState) -> dict:
    return {
        "available": state.available,
        "max": state.max_tickets,
        "refill_seconds": state.refill_seconds,
        "next_refill_at": state.next_refill_at,
    }
