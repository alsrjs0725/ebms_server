"""개인정보 보유기간이 지난 행을 지우는 정리 작업입니다(docs/privacy.md).

보유기간은 constant.RETENTION_*(env)로 정합니다. 대상 행을 잠그지 않는 SELECT로 먼저 찾고,
기본 키로 BATCH개씩 지우고 커밋해 다운로드·로그인 요청을 오래 막지 않습니다.
"""
import logging
import threading
import time

from . import constant, db, quota

logger = logging.getLogger(__name__)

DAY = 24 * 3600
# 한 번에 지우고 커밋하는 행 수
BATCH = 500


def _delete_rows(cur, con, select_sql: str, select_args: tuple, delete_sql: str, recheck_args: tuple = ()) -> int:
    """select_sql로 고른 행의 기본 키마다 delete_sql(기본 키 + recheck_args)을 실행합니다.
    delete_sql에 조건을 다시 넣어, 고른 뒤 그 사이에 갱신된 행(예: 다시 다운로드한 곡)은 지우지 않습니다."""
    cur.execute(select_sql, select_args)
    keys = [tuple(row) for row in cur.fetchall()]
    deleted = 0
    for i in range(0, len(keys), BATCH):
        for key in keys[i:i + BATCH]:
            cur.execute(delete_sql, (*key, *recheck_args))
            deleted += max(cur.rowcount, 0)
        con.commit()
    return deleted


def _months_before(month: str, n: int) -> str:
    """'YYYY-MM'에서 n달 앞의 'YYYY-MM'."""
    year, mon = map(int, month.split("-"))
    index = year * 12 + (mon - 1) - n
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def purge_sessions(cur, con, now: int, days: int) -> int:
    """만료되거나 해지된 지 days일이 지난 세션을 지웁니다."""
    cutoff = now - days * DAY
    return _delete_rows(
        cur, con,
        "SELECT id FROM session WHERE expires_at < %s OR revoked_at < %s",
        (cutoff, cutoff),
        "DELETE FROM session WHERE id = %s AND (expires_at < %s OR revoked_at < %s)",
        (cutoff, cutoff),
    )


def purge_api_keys(cur, con, now: int, days: int) -> int:
    """만료되거나 폐기된 지 days일이 지난 관리자 API 키를 지웁니다(세션과 같은 기간)."""
    cutoff = now - days * DAY
    return _delete_rows(
        cur, con,
        "SELECT id FROM api_key WHERE expires_at < %s OR revoked_at < %s",
        (cutoff, cutoff),
        "DELETE FROM api_key WHERE id = %s AND (expires_at < %s OR revoked_at < %s)",
        (cutoff, cutoff),
    )


def purge_grants(cur, con, now: int, days: int) -> int:
    """차감 면제 기간이 끝나고 days일이 지난 곡별 다운로드 기록을 지웁니다.
    면제 기간이 끝난 기록은 다음 다운로드 때 어차피 다시 차감하므로 지워도 할당량 동작은 같습니다."""
    cutoff = now - days * DAY
    return _delete_rows(
        cur, con,
        "SELECT user_id, song_id FROM download_grant WHERE expires_at < %s",
        (cutoff,),
        "DELETE FROM download_grant WHERE user_id = %s AND song_id = %s AND expires_at < %s",
        (cutoff,),
    )


def purge_download_log(cur, con, now: int, days: int) -> int:
    """days일보다 오래된 곡 다운로드 기록을 지웁니다."""
    cutoff = now - days * DAY
    return _delete_rows(
        cur, con,
        "SELECT id FROM download_log WHERE at < %s",
        (cutoff,),
        "DELETE FROM download_log WHERE id = %s",
    )


def purge_pre_usage(cur, con, now: int, months: int) -> int:
    """이번 달을 포함한 최근 months달보다 오래된 월별 사용량을 지웁니다."""
    oldest_kept = _months_before(quota.current_month(now), months - 1)
    return _delete_rows(
        cur, con,
        "SELECT user_id, month FROM pre_usage WHERE month < %s",
        (oldest_kept,),
        "DELETE FROM pre_usage WHERE user_id = %s AND month = %s",
    )


def purge_inactive_users(cur, con, now: int, days: int) -> int:
    """마지막 로그인과 세션 사용이 모두 days일보다 오래된 계정을 지웁니다(관리자 제외).
    클라이언트는 로그인 없이 세션키로 계속 쓰므로 세션의 last_used_at도 사용으로 봅니다.
    연결된 로그인 수단·세션·티켓·다운로드 기록은 외래 키(ON DELETE CASCADE)로 함께 지워집니다."""
    cutoff = now - days * DAY
    return _delete_rows(
        cur, con,
        """
            SELECT id FROM user u
            WHERE u.role != 'admin' AND COALESCE(u.last_login_at, u.created_at) < %s
              AND NOT EXISTS (SELECT 1 FROM session s WHERE s.user_id = u.id AND s.last_used_at >= %s)
        """,
        (cutoff, cutoff),
        "DELETE FROM user WHERE id = %s AND role != 'admin' AND COALESCE(last_login_at, created_at) < %s",
        (cutoff,),
    )


def purge_rejoin_blocks(cur, con, now: int) -> int:
    """재가입 제한 기간이 끝난 OAuth 계정 키를 지웁니다(무기한은 남김)."""
    return _delete_rows(
        cur, con,
        "SELECT key_sha256 FROM rejoin_block WHERE until IS NOT NULL AND until <= %s",
        (now,),
        "DELETE FROM rejoin_block WHERE key_sha256 = %s AND until IS NOT NULL AND until <= %s",
        (now,),
    )


def lift_expired_bans(cur, con, now: int) -> int:
    """끝나는 시각이 지난 정지를 풉니다. 로그인·세션 확인은 이 작업 전에도 지난 정지를 정지로 보지 않습니다."""
    cur.execute(
        "UPDATE user SET status = 'active', banned_until = NULL "
        "WHERE status = 'banned' AND banned_until IS NOT NULL AND banned_until <= %s",
        (now,),
    )
    lifted = max(cur.rowcount, 0)
    con.commit()
    return lifted


def purge(now: int | None = None) -> dict[str, int]:
    """보유기간이 지난 행을 지우고 항목별로 지운 행 수를 반환합니다. 기간이 0 이하인 항목은 건너뜁니다."""
    now = int(time.time()) if now is None else now
    jobs = [
        ("inactive_users", purge_inactive_users, constant.RETENTION_INACTIVE_USER_DAYS),
        ("sessions", purge_sessions, constant.RETENTION_SESSION_DAYS),
        ("api_keys", purge_api_keys, constant.RETENTION_SESSION_DAYS),
        ("download_grants", purge_grants, constant.RETENTION_GRANT_DAYS),
        ("pre_usage", purge_pre_usage, constant.RETENTION_PRE_USAGE_MONTHS),
        ("download_log", purge_download_log, constant.RETENTION_DOWNLOAD_LOG_DAYS),
    ]
    result = {}
    with db.connect() as con, con.cursor() as cur:
        for name, job, period in jobs:
            # download_grant는 0일이면 면제 기간이 지나자마자 지우는 뜻이라 0도 돌립니다.
            if period < 0 or (period == 0 and name != "download_grants"):
                continue
            try:
                result[name] = job(cur, con, now, period)
            except Exception:
                con.rollback()
                logger.exception(f"retention purge failed: {name}")
        # 기간이 정해진 항목이라 설정과 상관없이 돕니다.
        for name, job in (("rejoin_blocks", purge_rejoin_blocks), ("expired_bans", lift_expired_bans)):
            try:
                result[name] = job(cur, con, now)
            except Exception:
                con.rollback()
                logger.exception(f"retention purge failed: {name}")
    return result


def _loop(stop: threading.Event, interval: float) -> None:
    while True:
        try:
            deleted = purge()
            if any(deleted.values()):
                logger.info(f"retention purge: {deleted}")
        except Exception:
            logger.exception("retention purge failed")
        if stop.wait(interval):
            return


def start() -> threading.Event | None:
    """정리 작업 스레드를 시작합니다(시작 직후 한 번, 이후 RETENTION_INTERVAL_HOURS마다). 멈출 때 쓸 Event를 반환합니다."""
    if constant.RETENTION_INTERVAL_HOURS <= 0:
        logger.warning("EBMS_RETENTION_INTERVAL_HOURS is 0. Expired personal data is not purged.")
        return None
    stop = threading.Event()
    threading.Thread(
        target=_loop, args=(stop, constant.RETENTION_INTERVAL_HOURS * 3600), name="retention", daemon=True
    ).start()
    return stop
