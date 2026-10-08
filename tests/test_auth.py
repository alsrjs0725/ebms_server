"""웹 로그인·계정 테스트. OAuth와의 통신(fetch_profile)은 가짜로 바꿉니다."""
import re
import urllib.parse

import pymysql
import pytest
from fastapi.testclient import TestClient

from ebms_server import accounts, auth, constant, db as db_module
from ebms_server.accounts import Profile
from ebms_server.db import Database

TEST_DB = "ebms_test"


@pytest.fixture
def client(monkeypatch):
    try:
        con = db_module.connect(database=None)
    except pymysql.err.OperationalError as e:
        pytest.skip(f"MySQL not reachable: {e}")
    with con, con.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS {TEST_DB}")
        cur.execute(f"CREATE DATABASE {TEST_DB}")
    monkeypatch.setattr(constant, "DB_NAME", TEST_DB)
    monkeypatch.setattr(constant, "PUBLIC_URL", "http://testserver")
    monkeypatch.setattr(constant, "SECRET_KEY", "test-secret")
    for name in ("GOOGLE", "DISCORD"):
        monkeypatch.setattr(constant, f"{name}_CLIENT_ID", f"{name.lower()}-id")
        monkeypatch.setattr(constant, f"{name}_CLIENT_SECRET", f"{name.lower()}-secret")
    monkeypatch.setattr(constant, "ADMIN_EMAILS", {"admin@example.com"})
    Database._instance = None
    Database._initialized = False
    Database()
    from ebms_server.main import app
    yield TestClient(app)
    Database._instance = None
    Database._initialized = False


def oauth(client, monkeypatch, oauth, puid, *, email=None, verified=True, name="tester", link=False, next=None):
    """start → (가짜 oauth) → callback 을 거친 callback 응답을 반환합니다."""
    params = {}
    if link:
        params["link"] = "1"
    if next:
        params["next"] = next
    r = client.get(f"/auth/{oauth}/start", params=params, follow_redirects=False)
    assert r.status_code == 303, r.text
    location = urllib.parse.urlparse(r.headers["location"])
    query = urllib.parse.parse_qs(location.query)
    assert query["redirect_uri"] == [f"http://testserver/auth/{oauth}/callback"]
    seen = {}

    def fake_fetch_profile(p, code, verifier):
        seen["verifier"] = verifier
        return Profile(oauth, puid, email, verified, name)

    monkeypatch.setattr(auth, "fetch_profile", fake_fetch_profile)
    r = client.get(
        f"/auth/{oauth}/callback",
        params={"code": "c", "state": query["state"][0]},
        follow_redirects=False,
    )
    if oauth == "google":
        # PKCE: 쿠키에 둔 verifier가 보낸 challenge와 맞아야 함
        challenge = auth._b64(__import__("hashlib").sha256(seen["verifier"].encode()).digest())
        assert query["code_challenge"] == [challenge]
    return r


def me(client) -> accounts.User:
    token = client.cookies.get(constant.SESSION_COOKIE)
    return accounts.authenticate(token, "web")[0]


def test_login_creates_user_and_session(client, monkeypatch):
    r = client.get("/account", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login?next=%2Faccount"

    r = oauth(client, monkeypatch, "google", "g1", email="a@example.com", name="Alice")
    assert r.status_code == 303
    assert r.headers["location"] == "/account"
    user = me(client)
    assert user.display_name == "Alice"
    assert user.email == "a@example.com"
    assert len(user.id) == 36

    r = client.get("/account")
    assert r.status_code == 200
    assert "Alice" in r.text and user.id in r.text

    # 같은 OAuth 계정으로 다시 로그인하면 같은 user, 이전 웹 세션은 폐기
    old_token = client.cookies.get(constant.SESSION_COOKIE)
    oauth(client, monkeypatch, "google", "g1", email="a@example.com")
    assert me(client).id == user.id
    assert accounts.authenticate(old_token, "web") is None
    assert len(accounts.list_sessions(user.id)) == 1


def test_other_provider_without_link_is_new_user(client, monkeypatch):
    oauth(client, monkeypatch, "google", "g1", email="same@example.com")
    first = me(client).id
    # 이메일이 같아도 자동으로 합치지 않음
    oauth(client, monkeypatch, "discord", "d1", email="same@example.com")
    assert me(client).id != first


def test_link_and_login_with_either_oauth(client, monkeypatch):
    oauth(client, monkeypatch, "google", "g1")
    user_id = me(client).id
    r = oauth(client, monkeypatch, "discord", "d1", link=True, name="disc")
    assert r.status_code == 303
    assert [i.oauth for i in accounts.list_identities(user_id)] == ["google", "discord"]

    client.post("/auth/logout")
    oauth(client, monkeypatch, "discord", "d1")
    assert me(client).id == user_id


def test_link_requires_login(client):
    r = client.get("/auth/discord/start", params={"link": "1"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login?next=")


def test_link_taken_by_other_user(client, monkeypatch):
    oauth(client, monkeypatch, "discord", "d1")
    client.post("/auth/logout")
    oauth(client, monkeypatch, "google", "g1")
    r = oauth(client, monkeypatch, "discord", "d1", link=True)
    assert r.status_code == 409
    assert len(accounts.list_identities(me(client).id)) == 1


def test_unlink(client, monkeypatch):
    oauth(client, monkeypatch, "google", "g1")
    user_id = me(client).id
    google = accounts.list_identities(user_id)[0]
    r = client.delete(f"/api/account/identities/{google.id}")
    assert r.status_code == 409

    oauth(client, monkeypatch, "discord", "d1", link=True)
    r = client.delete(f"/api/account/identities/{google.id}")
    assert r.status_code == 204
    assert [i.oauth for i in accounts.list_identities(user_id)] == ["discord"]

    # 다른 사람의 연결은 건드릴 수 없음
    client.post("/auth/logout")
    oauth(client, monkeypatch, "google", "g2")
    discord = accounts.list_identities(user_id)[0]
    assert client.delete(f"/api/account/identities/{discord.id}").status_code == 404


def test_sessions_list_and_revoke(client, monkeypatch):
    oauth(client, monkeypatch, "google", "g1")
    user_id = me(client).id
    other = accounts.create_session(user_id, "web", "other device")
    sessions = accounts.list_sessions(user_id)
    assert len(sessions) == 2
    other_id = next(s.id for s in sessions if s.device_name == "other device")

    assert client.delete(f"/api/account/sessions/{other_id}").status_code == 204
    assert accounts.authenticate(other, "web") is None
    assert client.delete(f"/api/account/sessions/{other_id}").status_code == 404

    current_id = accounts.list_sessions(user_id)[0].id
    assert client.delete(f"/api/account/sessions/{current_id}").status_code == 204
    assert client.get("/account", follow_redirects=False).status_code == 303


def test_api_requires_login(client):
    assert client.delete("/api/account/sessions/1").status_code == 401


def test_logout(client, monkeypatch):
    oauth(client, monkeypatch, "google", "g1")
    token = client.cookies.get(constant.SESSION_COOKIE)
    r = client.post("/auth/logout", follow_redirects=False)
    assert r.status_code == 303
    assert accounts.authenticate(token, "web") is None


def test_admin_email(client, monkeypatch):
    oauth(client, monkeypatch, "discord", "d1", email="admin@example.com", verified=False)
    assert me(client).role == "user"
    oauth(client, monkeypatch, "google", "g1", email="Admin@example.com", verified=True)
    assert me(client).is_admin


def test_a3_no_demotion_after_list_change(client, monkeypatch):
    oauth(client, monkeypatch, "google", "g1", email="admin@example.com", verified=True)
    assert me(client).is_admin

    # ADMIN_EMAILS 목록에서 제외 후 재로그인하면 user로 강등됨
    monkeypatch.setattr(constant, "ADMIN_EMAILS", set())
    oauth(client, monkeypatch, "google", "g1", email="admin@example.com", verified=True)
    assert me(client).role == "user"


def test_a3_second_account_same_email_other_provider(client, monkeypatch):
    # 첫 번째 제공자로 로그인
    oauth(client, monkeypatch, "google", "g1", email="admin@example.com", verified=True)
    user1 = me(client)
    assert user1.is_admin

    # 연결 없이 다른 제공자로 로그인하면 별도 계정이 생성됨
    client.post("/auth/logout")
    oauth(client, monkeypatch, "discord", "d1", email="admin@example.com", verified=True)
    user2 = me(client)
    assert user2.id != user1.id
    assert user2.is_admin

    # 목록에서 빠지면 해당 계정으로 로그인 시 강등됨
    monkeypatch.setattr(constant, "ADMIN_EMAILS", set())
    oauth(client, monkeypatch, "discord", "d1", email="admin@example.com", verified=True)
    assert me(client).role == "user"


def test_a3_link_promotes(client, monkeypatch):
    # 일반 사용자 계정 생성
    oauth(client, monkeypatch, "discord", "d1", email="user@example.com", verified=True)
    user_id = me(client).id
    assert me(client).role == "user"

    # 관리자 이메일 OAuth를 연결해도 자동 승격되지 않음
    oauth(client, monkeypatch, "google", "g1", email="admin@example.com", verified=True, link=True)
    assert me(client).role == "user"
    assert me(client).id == user_id


def test_banned_user_cannot_login(client, monkeypatch):
    oauth(client, monkeypatch, "google", "g1")
    user_id = me(client).id
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("UPDATE user SET status = 'banned' WHERE id = %s", (user_id,))
        con.commit()
    assert client.get("/account", follow_redirects=False).status_code == 303
    r = oauth(client, monkeypatch, "google", "g1")
    assert r.status_code == 403


def test_bad_state_and_unknown_oauth(client, monkeypatch):
    client.get("/auth/google/start", follow_redirects=False)
    r = client.get("/auth/google/callback", params={"code": "c", "state": "wrong"})
    assert r.status_code == 400
    # discord start로 만든 쿠키를 google callback에 쓸 수 없음
    r = client.get("/auth/discord/start", follow_redirects=False)
    state = urllib.parse.parse_qs(urllib.parse.urlparse(r.headers["location"]).query)["state"][0]
    assert client.get("/auth/google/callback", params={"code": "c", "state": state}).status_code == 400

    assert client.get("/auth/github/start", follow_redirects=False).status_code == 404
    monkeypatch.setattr(constant, "DISCORD_CLIENT_ID", "")
    assert client.get("/auth/discord/start", follow_redirects=False).status_code == 404
    r = client.get("/login")
    assert "Google" in r.text and "Discord" not in r.text


def test_next_redirect(client, monkeypatch):
    r = oauth(client, monkeypatch, "google", "g1", next="/somewhere?x=1")
    assert r.headers["location"] == "/somewhere?x=1"
    for bad in ("//evil.example", "https://evil.example", "/\\evil.example"):
        r = oauth(client, monkeypatch, "google", "g1", next=bad)
        assert r.headers["location"] == "/account"


def test_safe_next():
    assert auth.safe_next(None) == "/account"
    assert auth.safe_next("") == "/account"
    assert auth.safe_next("/somewhere") == "/somewhere"
    assert auth.safe_next("/somewhere?x=1") == "/somewhere?x=1"
    assert auth.safe_next("//evil.com") == "/account"
    assert auth.safe_next("https://evil.com") == "/account"
    assert auth.safe_next("evil.com/path") == "/account"
    assert auth.safe_next("/\\evil.com") == "/account"
    assert auth.safe_next(None, default="/custom") == "/custom"
    assert auth.safe_next("", default="/custom") == "/custom"
    assert auth.safe_next("//evil.com", default="/custom") == "/custom"


def test_signed_cookie():
    value = auth.sign({"a": 1, "exp": 2**40})
    assert auth.unsign(value)["a"] == 1
    body, sig = value.rsplit(".", 1)
    assert auth.unsign(body + "." + sig[::-1]) is None
    assert auth.unsign(auth.sign({"exp": 1})) is None


def test_api_requires_login(client):
    # /api/version 과 세션키를 받는 /api/auth/client/token 외의 API는 모두 로그인 필요
    from ebms_server.main import app
    public = {"/api/version", "/api/auth/client/token"}
    checked = 0
    for path, methods in app.openapi()["paths"].items():
        if not path.startswith("/api/") or path in public:
            continue
        url = re.sub(r"\{[^}]+\}", "1", path)
        for method in methods:
            r = client.request(method, url, json={}, params={"path": "a.bms"})
            assert r.status_code == 401, (method, path, r.status_code)
            checked += 1
    assert checked >= 10
    assert client.get("/api/version").status_code == 200
    assert client.get("/api/version").json()["api"] == 2
    # 기존 공개 다운로드 경로는 없어짐
    for path in ("/api/charthash", "/api/files/chart/0", "/api/manifest/hash", "/api/manifest/0",
                 "/api/files/song/id/1", "/api/files/song/" + "0" * 64):
        assert client.get(path).status_code == 404, path


def test_session_cookie_renewed(client, monkeypatch):
    oauth(client, monkeypatch, "google", "g1")
    token = client.cookies.get(constant.SESSION_COOKIE)
    # 로그인한 요청마다 쿠키 유효기간을 다시 내려줌
    r = client.get("/account")
    cookie = [v for v in r.headers.get_list("set-cookie") if v.startswith(f"{constant.SESSION_COOKIE}=")]
    assert len(cookie) == 1
    assert f"{constant.SESSION_COOKIE}={token}" in cookie[0]
    assert f"Max-Age={constant.WEB_SESSION_SECONDS}" in cookie[0]
    # 로그아웃 응답은 쿠키 삭제만 하고 다시 싣지 않음
    r = client.post("/auth/logout", follow_redirects=False)
    cookie = [v for v in r.headers.get_list("set-cookie") if v.startswith(f"{constant.SESSION_COOKIE}=")]
    assert len(cookie) == 1 and "Max-Age=0" in cookie[0]
    # 비로그인 요청에는 쿠키를 싣지 않음
    client.cookies.clear()
    assert "set-cookie" not in client.get("/login").headers
