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
    monkeypatch.setattr(constant, "ADMIN_USER_IDS", set())
    Database._instance = None
    Database._initialized = False
    Database()
    from ebms_server.main import app
    yield TestClient(app)
    Database._instance = None
    Database._initialized = False


def oauth(client, monkeypatch, oauth, puid, *, name="tester", link=False, next=None, admin=False):
    """start → (가짜 oauth) → callback 을 거친 callback 응답을 반환합니다.

    처음 보는 계정이면 name을 닉네임으로 가입까지 마치고 그 응답을 반환합니다.
    admin이면 로그인한 계정 ID를 EBMS_ADMIN_USER_IDS에 넣습니다(다음 요청부터 관리자).
    """
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
        return Profile(oauth, puid)

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
    if r.status_code == 303 and r.headers["location"] == "/signup":
        assert client.get("/signup").status_code == 200
        r = client.post("/signup", data={"nickname": name}, follow_redirects=False)
    if admin:
        monkeypatch.setattr(constant, "ADMIN_USER_IDS", constant.ADMIN_USER_IDS | {me(client).id})
    return r


def me(client) -> accounts.User:
    token = client.cookies.get(constant.SESSION_COOKIE)
    return accounts.authenticate(token, "web")[0]


def test_login_creates_user_and_session(client, monkeypatch):
    r = client.get("/account", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login?next=%2Faccount"

    r = oauth(client, monkeypatch, "google", "g1", name="Alice")
    assert r.status_code == 303
    assert r.headers["location"] == "/account"
    user = me(client)
    assert user.display_name == "Alice"
    assert len(user.id) == 36

    r = client.get("/account")
    assert r.status_code == 200
    assert "Alice" in r.text and user.id in r.text

    # 같은 OAuth 계정으로 다시 로그인하면 같은 user, 이전 웹 세션은 폐기
    old_token = client.cookies.get(constant.SESSION_COOKIE)
    oauth(client, monkeypatch, "google", "g1")
    assert me(client).id == user.id
    assert accounts.authenticate(old_token, "web") is None
    assert len(accounts.list_sessions(user.id)) == 1


def test_other_provider_without_link_is_new_user(client, monkeypatch):
    oauth(client, monkeypatch, "google", "g1")
    first = me(client).id
    # 다른 OAuth 계정은 자동으로 합치지 않음
    oauth(client, monkeypatch, "discord", "d1")
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
    current_id = accounts.list_sessions(user_id)[0].id
    other = accounts.create_session(user_id, "web")
    sessions = accounts.list_sessions(user_id)
    assert len(sessions) == 2
    other_id = next(s.id for s in sessions if s.id != current_id)

    assert client.delete(f"/api/account/sessions/{other_id}").status_code == 204
    assert accounts.authenticate(other, "web") is None
    assert client.delete(f"/api/account/sessions/{other_id}").status_code == 404

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


def test_admin_user_ids(client, monkeypatch):
    oauth(client, monkeypatch, "google", "g1")
    user_id = me(client).id
    assert me(client).role == "user"
    # env에 계정 ID를 넣으면 다시 로그인하지 않아도 다음 요청부터 관리자
    monkeypatch.setattr(constant, "ADMIN_USER_IDS", {user_id})
    assert me(client).is_admin
    assert accounts.get_user(user_id).is_admin
    # 다른 계정은 그대로
    client.post("/auth/logout")
    oauth(client, monkeypatch, "google", "g2")
    assert me(client).role == "user"


def test_admin_user_ids_promote_on_login(client, monkeypatch):
    oauth(client, monkeypatch, "google", "g1")
    user_id = me(client).id
    client.post("/auth/logout")
    monkeypatch.setattr(constant, "ADMIN_USER_IDS", {user_id})
    oauth(client, monkeypatch, "google", "g1")
    assert accounts.get_user(user_id).is_admin


def test_a3_no_demotion_after_list_change(client, monkeypatch):
    oauth(client, monkeypatch, "google", "g1", admin=True)
    assert me(client).is_admin

    # ADMIN_USER_IDS는 승격만 하므로 목록에서 빼도 재로그인으로 강등되지 않음(관리자 페이지에서 강등)
    monkeypatch.setattr(constant, "ADMIN_USER_IDS", set())
    oauth(client, monkeypatch, "google", "g1")
    assert me(client).is_admin


def test_admin_page_promotion_survives_login(client, monkeypatch):
    """관리자 페이지에서 승격한 사용자는 다시 로그인해도 admin으로 남습니다(#79)."""
    oauth(client, monkeypatch, "discord", "d1")
    user_id = me(client).id
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("UPDATE user SET role = 'admin' WHERE id = %s", (user_id,))
        con.commit()
    client.post("/auth/logout")
    oauth(client, monkeypatch, "discord", "d1")
    assert me(client).id == user_id
    assert me(client).is_admin


def test_env_admin_other_identity_keeps_admin(client, monkeypatch):
    """EBMS_ADMIN_USER_IDS 관리자가 연결된 다른 수단으로 로그인해도 관리자입니다(#79)."""
    oauth(client, monkeypatch, "google", "g1", admin=True)
    user_id = me(client).id
    assert me(client).is_admin
    oauth(client, monkeypatch, "discord", "d1", link=True)
    client.post("/auth/logout")
    oauth(client, monkeypatch, "discord", "d1")
    assert me(client).id == user_id
    assert me(client).is_admin


def test_no_email_or_device_name_stored(client, monkeypatch):
    """이메일과 기기 이름(User-Agent)은 저장하지 않습니다(#102)."""
    client.headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130.0"
    oauth(client, monkeypatch, "google", "g1", name="Alice")
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute(
            """
            SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE() AND COLUMN_NAME IN ('email', 'device_name')
            """
        )
        assert cur.fetchall() == ()
    assert "Chrome" not in client.get("/account").text
    assert "email" not in client.get("/api/me").json()
    assert {p.scope for p in auth.OAUTHS.values()} == {"openid", "identify"}


def test_signup_asks_nickname(client, monkeypatch):
    """처음 로그인하면 OAuth 계정 이름 대신 직접 정한 닉네임으로 가입합니다(#102)."""
    r = client.get("/auth/google/start", params={"next": "/admin"}, follow_redirects=False)
    state = urllib.parse.parse_qs(urllib.parse.urlparse(r.headers["location"]).query)["state"][0]
    monkeypatch.setattr(auth, "fetch_profile", lambda p, code, verifier: Profile("google", "g1"))
    r = client.get("/auth/google/callback", params={"code": "c", "state": state}, follow_redirects=False)
    assert r.headers["location"] == "/signup"
    assert constant.SESSION_COOKIE not in client.cookies
    assert 'name="nickname"' in client.get("/signup").text

    for bad in ("", "   ", "x" * 33, "a\x01b"):
        assert client.post("/signup", data={"nickname": bad}, follow_redirects=False).status_code == 400
    r = client.post("/signup", data={"nickname": "  닉네임  "}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin"
    user = me(client)
    assert user.display_name == "닉네임"
    # 가입 쿠키는 한 번 쓰면 지움
    assert client.get("/signup", follow_redirects=False).headers["location"] == "/login"
    # 다시 로그인하면 바로 같은 계정
    oauth(client, monkeypatch, "google", "g1", name="ignored")
    assert me(client).id == user.id and me(client).display_name == "닉네임"


def test_signup_without_cookie(client):
    assert client.get("/signup", follow_redirects=False).headers["location"] == "/login"
    assert client.post("/signup", data={"nickname": "a"}).status_code == 400


def test_change_nickname(client, monkeypatch):
    oauth(client, monkeypatch, "google", "g1", name="Alice")
    assert client.post("/account/nickname", data={"nickname": ""}).status_code == 400
    r = client.post("/account/nickname", data={"nickname": "Bob", "next": "//evil.com"}, follow_redirects=False)
    assert r.headers["location"] == "/account"
    assert me(client).display_name == "Bob"
    client.post("/auth/logout")
    assert client.post("/account/nickname", data={"nickname": "x"}, follow_redirects=False).status_code == 303


def test_missing_nickname_asked_after_login(client, monkeypatch):
    oauth(client, monkeypatch, "google", "g1", name="Alice")
    accounts.set_nickname(me(client).id, "")
    client.post("/auth/logout")
    r = oauth(client, monkeypatch, "google", "g1", next="/admin")
    assert r.headers["location"] == "/account/nickname?next=%2Fadmin"
    r = client.post("/account/nickname", data={"nickname": "New", "next": "/admin"}, follow_redirects=False)
    assert r.headers["location"] == "/admin"


def test_old_columns_dropped_on_start(client, monkeypatch):
    """예전 DB에 남은 이메일·기기 이름 컬럼은 서버 시작 시 값과 함께 지웁니다(#102)."""
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("ALTER TABLE user ADD COLUMN email VARCHAR(255)")
        cur.execute("ALTER TABLE user_identity ADD COLUMN email VARCHAR(255)")
        cur.execute("ALTER TABLE user_identity ADD COLUMN name VARCHAR(255) NOT NULL DEFAULT ''")
        cur.execute("INSERT INTO user (id, display_name, created_at) VALUES ('u1', 'OAuth Name', 0)")
        cur.execute("ALTER TABLE session ADD COLUMN device_name VARCHAR(255) NOT NULL DEFAULT ''")
        cur.execute("ALTER TABLE auth_code ADD COLUMN device_name VARCHAR(255) NOT NULL DEFAULT ''")
        con.commit()
    Database().generate_database()
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*) FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE() AND COLUMN_NAME IN ('email', 'device_name')
               OR (TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'user_identity' AND COLUMN_NAME = 'name')
            """
        )
        assert cur.fetchone()[0] == 0
        # OAuth에서 가져온 표시 이름도 비움(다음 로그인 때 닉네임을 정함)
        cur.execute("SELECT display_name FROM user WHERE id = 'u1'")
        assert cur.fetchone()[0] == ""
    # 이미 정리한 DB에서 다시 시작하면 닉네임을 건드리지 않음
    accounts.set_nickname("u1", "Nick")
    Database().generate_database()
    assert accounts.get_user("u1").display_name == "Nick"


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
    # 브라우저는 탭·개행을 지우고 역슬래시를 /로 바꿔 //evil.com으로 해석함 (#58)
    for bad in (
        "/\t/evil.com", "/\n/evil.com", "/\r/evil.com", "/\x00/evil.com", "/\x7f/evil.com",
        " //evil.com", "/ /evil.com", "/\u3000/evil.com", "/\\/evil.com", "/\t\\evil.com",
        "/x\ty",
    ):
        assert auth.safe_next(bad) == "/account", repr(bad)
    # 퍼센트 인코딩은 그대로 두면 브라우저가 경로로 해석하므로 허용
    assert auth.safe_next("/%09/evil.com") == "/%09/evil.com"
    assert auth.safe_next("/a/b?next=%2F%2Fx#frag") == "/a/b?next=%2F%2Fx#frag"


def test_login_page_continue_link_rejects_control_chars(client, monkeypatch):
    """로그인된 상태의 로그인 페이지 "계속하기" 링크에 탭이 섞인 next가 들어가지 않음 (#58)"""
    oauth(client, monkeypatch, "google", "g1")
    r = client.get("/login", params={"next": "/\t/evil.com"})
    assert r.status_code == 200
    assert '<a href="/account">계속하기</a>' in r.text
    assert "evil.com" not in r.text


def test_signed_cookie():
    value = auth.sign({"a": 1, "exp": 2**40})
    assert auth.unsign(value)["a"] == 1
    body, sig = value.rsplit(".", 1)
    assert auth.unsign(body + "." + sig[::-1]) is None
    assert auth.unsign(auth.sign({"exp": 1})) is None


def test_api_requires_login(client):
    # /api/version, 공지(/api/notices), 세션키를 받는 /api/auth/client/token 외의 API는 모두 로그인 필요
    from ebms_server.main import app
    public = {"/api/version", "/api/notices", "/api/auth/client/token"}
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
    assert client.get("/api/version").json()["api"] == constant.API_VERSION
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


def test_docker_healthcheck_url_is_public(client):
    # Dockerfile HEALTHCHECK가 부르는 경로는 로그인 없이 200이어야 배포가 healthy가 됩니다.
    dockerfile = (constant.BASE_DIR / "Dockerfile").read_text(encoding="utf-8")
    path = re.search(r"http://127\.0\.0\.1:8000(/[^'\"]*)", dockerfile).group(1)
    assert client.get(path).status_code == 200, path


def test_menu(client, monkeypatch):
    """상단 메뉴: 비로그인은 로그인/가입, 로그인은 설정·로그아웃, 관리자는 관리자 홈까지."""
    r = client.get("/")
    assert "로그인 / 가입" in r.text and 'href="/account"' not in r.text

    oauth(client, monkeypatch, "google", "u1", name="Bob")
    for path in ("/", "/account", "/login"):
        text = client.get(path).text
        assert 'href="/account">설정' in text and "로그아웃" in text and 'href="/admin"' not in text, path
    # user를 넘기지 않는 메시지 페이지도 로그인 상태로 보임
    text = client.get("/admin").text
    assert "권한 없음" in text and 'href="/account">설정' in text

    client.post("/auth/logout")
    oauth(client, monkeypatch, "google", "a1", name="Ad", admin=True)
    for path in ("/", "/account", "/admin"):
        assert 'href="/admin"' in client.get(path).text, path


def test_insecure_public_url():
    """https가 아니고 루프백도 아닌 EBMS_PUBLIC_URL은 시작할 때 경고합니다(#28)."""
    from ebms_server.main import insecure_public_url

    assert insecure_public_url("http://example.com")
    assert insecure_public_url("http://192.168.0.10:8000")
    assert not insecure_public_url("https://ebms.example.com")
    for url in ("http://localhost:8000", "http://127.0.0.1:1234", "http://[::1]:8000"):
        assert not insecure_public_url(url), url
