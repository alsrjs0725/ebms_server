"""개인정보 보유기간 정리 작업 테스트 (#99)."""
from ebms_server import accounts, constant, db as db_module, quota, retention
from ebms_server.accounts import Profile

from test_auth import client  # noqa: F401  (fixture 재사용)

DAY = retention.DAY
NOW = 1_800_000_000


def rows(sql, args=()):
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall()


def execute(sql, args=()):
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute(sql, args)
        con.commit()


def new_user(puid, admin=False):
    uid = accounts.signup(Profile("google", puid), puid).id
    if admin:
        execute("UPDATE user SET role = 'admin' WHERE id = %s", (uid,))
    return uid


def test_months_before():
    assert retention._months_before("2026-10", 0) == "2026-10"
    assert retention._months_before("2026-10", 11) == "2025-11"
    assert retention._months_before("2026-01", 1) == "2025-12"


def test_purge_sessions(client, monkeypatch):
    monkeypatch.setattr(constant, "RETENTION_SESSION_DAYS", 30)
    uid = new_user("u1")
    session = "INSERT INTO session (token_sha256, user_id, kind, created_at, last_used_at, expires_at, revoked_at) VALUES (%s, %s, 'web', 0, 0, %s, %s)"
    execute(session, ("live", uid, NOW + DAY, None))
    execute(session, ("expired-recent", uid, NOW - 10 * DAY, None))
    execute(session, ("expired-old", uid, NOW - 31 * DAY, None))
    execute(session, ("revoked-recent", uid, NOW + DAY, NOW - 10 * DAY))
    execute(session, ("revoked-old", uid, NOW + DAY, NOW - 31 * DAY))

    assert retention.purge(NOW)["sessions"] == 2
    left = {r[0] for r in rows("SELECT token_sha256 FROM session")}
    assert left == {"live", "expired-recent", "revoked-recent"}


def test_purge_grants_keeps_active(client, monkeypatch):
    monkeypatch.setattr(constant, "RETENTION_GRANT_DAYS", 0)
    uid = new_user("u1")
    grant = "INSERT INTO download_grant (user_id, song_id, charged_at, expires_at) VALUES (%s, %s, 0, %s)"
    execute(grant, (uid, 1, NOW + 60))
    execute(grant, (uid, 2, NOW - 60))

    assert retention.purge(NOW)["download_grants"] == 1
    assert [r[0] for r in rows("SELECT song_id FROM download_grant")] == [1]


def test_purge_grants_disabled(client, monkeypatch):
    monkeypatch.setattr(constant, "RETENTION_GRANT_DAYS", -1)
    uid = new_user("u1")
    execute("INSERT INTO download_grant (user_id, song_id, charged_at, expires_at) VALUES (%s, 1, 0, 0)", (uid,))
    assert "download_grants" not in retention.purge(NOW)
    assert len(rows("SELECT * FROM download_grant")) == 1


def test_purge_pre_usage(client, monkeypatch):
    monkeypatch.setattr(constant, "RETENTION_PRE_USAGE_MONTHS", 12)
    uid = new_user("u1")
    current = quota.current_month(NOW)
    months = [current, retention._months_before(current, 11), retention._months_before(current, 12)]
    for month in months:
        execute("INSERT INTO pre_usage (user_id, month, bytes) VALUES (%s, %s, 1)", (uid, month))

    assert retention.purge(NOW)["pre_usage"] == 1
    assert {r[0] for r in rows("SELECT month FROM pre_usage")} == set(months[:2])


def test_inactive_users_off_by_default(client):
    assert constant.RETENTION_INACTIVE_USER_DAYS == 0
    uid = new_user("u1")
    execute("UPDATE user SET last_login_at = 0, created_at = 0 WHERE id = %s", (uid,))
    assert "inactive_users" not in retention.purge(NOW)
    assert accounts.get_user(uid) is not None


def test_purge_inactive_users(client, monkeypatch):
    monkeypatch.setattr(constant, "RETENTION_INACTIVE_USER_DAYS", 365)
    old = new_user("old")
    client_user = new_user("client")
    admin = new_user("admin", admin=True)
    recent = new_user("recent")
    for uid in (old, client_user, admin):
        execute("UPDATE user SET last_login_at = %s WHERE id = %s", (NOW - 400 * DAY, uid))
    execute("UPDATE user SET last_login_at = %s WHERE id = %s", (NOW - 10 * DAY, recent))
    # 로그인은 오래됐어도 클라이언트 세션을 최근에 썼으면 남깁니다.
    execute(
        "INSERT INTO session (token_sha256, user_id, kind, created_at, last_used_at, expires_at) VALUES ('t', %s, 'client', 0, %s, %s)",
        (client_user, NOW - DAY, NOW + 80 * DAY),
    )

    assert retention.purge(NOW)["inactive_users"] == 1
    assert accounts.get_user(old) is None
    for uid in (client_user, admin, recent):
        assert accounts.get_user(uid) is not None


def test_start_disabled(monkeypatch):
    monkeypatch.setattr(constant, "RETENTION_INTERVAL_HOURS", 0)
    assert retention.start() is None
