"""회원 탈퇴(#100) 테스트."""
import time

from ebms_server import accounts, constant, db as db_module, quota, retention

from test_auth import client, me, oauth  # noqa: F401  (fixture 재사용)
from test_client_auth import client_login


def count(table: str, column: str, value: str) -> int:
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM {table} WHERE {column} = %s", (value,))
        return cur.fetchone()[0]


def test_delete_account_removes_user_data(client, monkeypatch):
    key, user_id = client_login(client, monkeypatch)
    web_token = client.cookies.get(constant.SESSION_COOKIE)
    other_token = accounts.create_session(user_id, "web")
    accounts.create_auth_code(user_id, "c" * 43, "http://127.0.0.1:1/cb")
    assert quota.charge_play(user_id, 1)
    now = int(time.time())
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("INSERT INTO pre_usage (user_id, month, bytes) VALUES (%s, %s, %s)", (user_id, "2026-10", 1))
        cur.execute(
            """
            INSERT INTO deleted_song (song_id, size, sha256, charts, chart_list, reason, deleted_by, deleted_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (7, 1, "0" * 64, 0, "[]", "", user_id, now),
        )
        con.commit()

    r = client.delete("/api/account")
    assert r.status_code == 204
    # 쿠키를 지움
    assert any(v.startswith(f"{constant.SESSION_COOKIE}=") for v in r.headers.get_list("set-cookie"))
    assert not client.cookies.get(constant.SESSION_COOKIE)

    assert accounts.get_user(user_id) is None
    for table in accounts.USER_TABLES:
        assert count(table, "user_id", user_id) == 0, table
    # 지운 곡 기록은 남고 지운 사람만 비움
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("SELECT deleted_by FROM deleted_song WHERE song_id = 7")
        assert cur.fetchone() == (None,)

    # 웹·다른 기기·클라이언트 세션 모두 무효
    assert accounts.authenticate(web_token, "web") is None
    assert accounts.authenticate(other_token, "web") is None
    assert client.get("/api/me", headers={"Authorization": f"Bearer {key}"}).status_code == 401
    assert client.get("/account", follow_redirects=False).status_code == 303

    # 하루 동안은 같은 OAuth 계정으로 다시 가입할 수 없음
    r = oauth(client, monkeypatch, "google", "g1")
    assert r.status_code == 403
    assert "다시 가입할 수 있습니다" in r.text
    assert not client.cookies.get(constant.SESSION_COOKIE)
    # 원래 ID 대신 키만 남음
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("SELECT key_sha256, until FROM rejoin_block")
        rows = cur.fetchall()
    assert len(rows) == 1 and "g1" not in rows[0][0]
    assert now + 23 * 3600 < rows[0][1] <= int(time.time()) + 24 * 3600

    # 기간이 지나면 정리 작업이 지우고 새 계정으로 가입됨
    assert retention.purge(now=rows[0][1])["rejoin_blocks"] == 1
    oauth(client, monkeypatch, "google", "g1")
    assert me(client).id != user_id


def test_delete_account_requires_web_session(client, monkeypatch):
    assert client.delete("/api/account").status_code == 401
    key, user_id = client_login(client, monkeypatch)
    assert client.delete("/api/account", headers={"Authorization": f"Bearer {key}"}).status_code == 403
    assert accounts.get_user(user_id) is not None


def test_last_admin_cannot_delete(client, monkeypatch):
    oauth(client, monkeypatch, "google", "g1", admin=True)
    admin_id = me(client).id
    assert me(client).is_admin
    r = client.delete("/api/account")
    assert r.status_code == 409
    assert accounts.get_user(admin_id) is not None

    # 다른 관리자가 있으면 탈퇴 가능
    client.post("/auth/logout")
    oauth(client, monkeypatch, "discord", "d2", admin=True)
    assert me(client).is_admin
    client.post("/auth/logout")
    oauth(client, monkeypatch, "google", "g1")
    assert me(client).id == admin_id
    assert client.delete("/api/account").status_code == 204
    assert accounts.get_user(admin_id) is None
    # 남은 관리자는 마지막이라 거부
    oauth(client, monkeypatch, "discord", "d2")
    assert client.delete("/api/account").status_code == 409


def test_account_page_has_delete_button(client, monkeypatch):
    oauth(client, monkeypatch, "google", "g1")
    html = client.get("/account").text
    assert 'id="deleteAccountConfirm"' in html
    assert "<script>" not in html


def ban(user_id: str, until: int | None) -> None:
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("UPDATE user SET status = 'banned', banned_until = %s WHERE id = %s", (until, user_id))
        con.commit()


def test_banned_user_can_withdraw(client, monkeypatch):
    oauth(client, monkeypatch, "google", "g1")
    user_id = me(client).id
    oauth(client, monkeypatch, "discord", "d1", link=True)
    client.post("/auth/logout")
    until = int(time.time()) + 10 * 86400
    ban(user_id, until)

    # 정지 계정은 세션 없이 안내와 탈퇴 링크만 받음
    r = oauth(client, monkeypatch, "google", "g1")
    assert r.status_code == 403
    assert "/account/withdraw" in r.text
    assert not client.cookies.get(constant.SESSION_COOKIE)
    assert client.get("/account/withdraw").status_code == 200

    # 확인란 없이는 탈퇴하지 않음
    assert client.post("/account/withdraw").status_code == 400
    assert accounts.get_user(user_id) is not None
    r = client.post("/account/withdraw", data={"agree": "1"})
    assert r.status_code == 200
    assert accounts.get_user(user_id) is None

    # 두 로그인 수단 모두 정지가 끝날 때까지 재가입 불가
    for provider, puid in (("google", "g1"), ("discord", "d1")):
        r = oauth(client, monkeypatch, provider, puid)
        assert r.status_code == 403, provider
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("SELECT until FROM rejoin_block")
        assert [row[0] for row in cur.fetchall()] == [until, until]
    # 다른 계정에 연결도 불가
    oauth(client, monkeypatch, "google", "g2")
    assert oauth(client, monkeypatch, "discord", "d1", link=True).status_code == 403

    # 쿠키 없이 탈퇴 화면은 쓸 수 없음
    client.cookies.clear()
    assert client.get("/account/withdraw", follow_redirects=False).status_code == 303


def test_indefinite_ban_blocks_rejoin_forever(client, monkeypatch):
    oauth(client, monkeypatch, "google", "g1")
    user_id = me(client).id
    client.post("/auth/logout")
    ban(user_id, None)
    oauth(client, monkeypatch, "google", "g1")
    assert client.post("/account/withdraw", data={"agree": "1"}).status_code == 200
    retention.purge(now=int(time.time()) + 10 * 365 * 86400)
    r = oauth(client, monkeypatch, "google", "g1")
    assert r.status_code == 403
    assert "정지 중에 탈퇴" in r.text


def test_expired_ban(client, monkeypatch):
    oauth(client, monkeypatch, "google", "g1")
    user_id = me(client).id
    client.post("/auth/logout")
    now = int(time.time())
    ban(user_id, now - 1)
    # 끝난 정지는 정리 작업 전에도 로그인 가능
    oauth(client, monkeypatch, "google", "g1")
    assert me(client).id == user_id
    assert retention.purge(now=now)["expired_bans"] == 1
    assert accounts.get_user(user_id).status == "active"
    # 정지가 끝난 뒤 탈퇴는 일반 탈퇴(하루 제한)
    assert client.delete("/api/account").status_code == 204
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("SELECT until FROM rejoin_block")
        assert cur.fetchone()[0] >= now + 24 * 3600


def test_admin_ban_with_end(client, monkeypatch):
    oauth(client, monkeypatch, "google", "admin", admin=True)
    oauth_target = accounts.signup(accounts.Profile("discord", "t1"), "target")
    until = int(time.time()) + 86400
    url = f"/api/admin/users/{oauth_target.id}"
    r = client.put(url, json={"status": "banned", "banned_until": until})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "banned" and r.json()["banned_until"] == until
    assert accounts.get_user(oauth_target.id).is_banned
    assert client.put(url, json={"status": "banned", "banned_until": 1}).status_code == 400
    assert client.put(url, json={"banned_until": until}).status_code == 400
    r = client.put(url, json={"status": "active"})
    assert r.json()["status"] == "active" and r.json()["banned_until"] is None
    r = client.put(url, json={"status": "banned"})
    assert r.json()["banned_until"] is None and accounts.get_user(oauth_target.id).is_banned
