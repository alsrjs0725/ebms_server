"""회원 탈퇴(#100) 테스트."""
import time

from ebms_server import accounts, constant, db as db_module, quota

from test_auth import client, me, oauth  # noqa: F401  (fixture 재사용)
from test_client_auth import client_login


def count(table: str, column: str, value: str) -> int:
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM {table} WHERE {column} = %s", (value,))
        return cur.fetchone()[0]


def test_delete_account_removes_user_data(client, monkeypatch):
    key, user_id = client_login(client, monkeypatch)
    web_token = client.cookies.get(constant.SESSION_COOKIE)
    other_token = accounts.create_session(user_id, "web", "other device")
    accounts.create_auth_code(user_id, "c" * 43, "http://127.0.0.1:1/cb", "pc")
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

    # 같은 OAuth 계정으로 다시 로그인하면 새 계정
    oauth(client, monkeypatch, "google", "g1")
    assert me(client).id != user_id


def test_delete_account_requires_web_session(client, monkeypatch):
    assert client.delete("/api/account").status_code == 401
    key, user_id = client_login(client, monkeypatch)
    assert client.delete("/api/account", headers={"Authorization": f"Bearer {key}"}).status_code == 403
    assert accounts.get_user(user_id) is not None


def test_last_admin_cannot_delete(client, monkeypatch):
    oauth(client, monkeypatch, "google", "g1", email="admin@example.com")
    admin_id = me(client).id
    assert me(client).is_admin
    r = client.delete("/api/account")
    assert r.status_code == 409
    assert accounts.get_user(admin_id) is not None

    # 다른 관리자가 있으면 탈퇴 가능
    client.post("/auth/logout")
    oauth(client, monkeypatch, "discord", "d2", email="admin@example.com")
    assert me(client).is_admin
    client.post("/auth/logout")
    oauth(client, monkeypatch, "google", "g1", email="admin@example.com")
    assert me(client).id == admin_id
    assert client.delete("/api/account").status_code == 204
    assert accounts.get_user(admin_id) is None
    # 남은 관리자는 마지막이라 거부
    oauth(client, monkeypatch, "discord", "d2", email="admin@example.com")
    assert client.delete("/api/account").status_code == 409


def test_account_page_has_delete_button(client, monkeypatch):
    oauth(client, monkeypatch, "google", "g1")
    html = client.get("/account").text
    assert 'id="deleteAccountConfirm"' in html
    assert "<script>" not in html
