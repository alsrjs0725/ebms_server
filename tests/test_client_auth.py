"""클라이언트 로그인(루프백 리다이렉트 + PKCE)과 Bearer 세션키 테스트."""

import base64
import hashlib
import secrets
import urllib.parse

from ebms_server import accounts, constant, db as db_module

from test_auth import client, me, oauth  # noqa: F401  (fixture 재사용)

REDIRECT = "http://127.0.0.1:53682/callback"


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    return verifier, challenge


def authorize(client, challenge, **extra):
    params = {
        "redirect_uri": REDIRECT,
        "state": "st",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "device_name": "my pc",
        **extra,
    }
    return client.get("/auth/client/authorize", params=params, follow_redirects=False)


def code_from(r) -> str:
    assert r.status_code == 303, r.text
    location = r.headers["location"]
    assert location.startswith(REDIRECT + "?")
    query = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
    assert query["state"] == ["st"]
    return query["code"][0]


def client_login(client, monkeypatch) -> tuple[str, str]:
    """웹 로그인 → authorize → token. (세션키, user id)를 반환합니다."""
    oauth(client, monkeypatch, "google", "g1", name="Alice")
    verifier, challenge = pkce()
    code = code_from(authorize(client, challenge))
    r = client.post(
        "/api/auth/client/token", json={"code": code, "code_verifier": verifier}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user"]["display_name"] == "Alice"
    return body["session_key"], body["user"]["id"]


def test_full_flow_and_bearer(client, monkeypatch):
    key, user_id = client_login(client, monkeypatch)
    assert user_id == me(client).id
    client.cookies.clear()

    r = client.get("/api/me", headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == user_id
    assert body["oauths"] == [{"oauth": "google", "name": "Alice"}]
    assert body["session"]["kind"] == "client"
    # 클라이언트 세션은 기기 목록에 이름과 함께 나옴
    sessions = accounts.list_sessions(user_id)
    client_session = next(s for s in sessions if s.kind == "client")
    assert client_session.device_name == "my pc"
    assert (
        client_session.expires_at - client_session.created_at
        == constant.CLIENT_SESSION_SECONDS
    )

    r = client.post(
        "/api/auth/client/logout", headers={"Authorization": f"Bearer {key}"}
    )
    assert r.status_code == 204
    assert (
        client.get("/api/me", headers={"Authorization": f"Bearer {key}"}).status_code
        == 401
    )


def test_me_with_cookie_and_401(client, monkeypatch):
    r = client.get("/api/me")
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"
    oauth(client, monkeypatch, "google", "g1")
    r = client.get("/api/me")
    assert r.status_code == 200 and r.json()["session"]["kind"] == "web"
    # 틀린 Bearer가 있으면 쿠키로 대신 통과시키지 않음
    assert (
        client.get("/api/me", headers={"Authorization": "Bearer nope"}).status_code
        == 401
    )


def test_web_cookie_is_not_session_key(client, monkeypatch):
    oauth(client, monkeypatch, "google", "g1")
    web_token = client.cookies.get(constant.SESSION_COOKIE)
    client.cookies.clear()
    assert (
        client.get(
            "/api/me", headers={"Authorization": f"Bearer {web_token}"}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/auth/client/logout", headers={"Authorization": f"Bearer {web_token}"}
        ).status_code
        == 401
    )


def test_authorize_requires_login(client, monkeypatch):
    _, challenge = pkce()
    r = authorize(client, challenge)
    assert r.status_code == 303
    location = r.headers["location"]
    assert location.startswith("/login?next=")
    next_url = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)["next"][0]
    assert next_url.startswith("/auth/client/authorize?")
    # 로그인하면 next로 돌아와 코드를 받음
    r = oauth(client, monkeypatch, "google", "g1", next=next_url)
    assert r.headers["location"] == next_url
    code_from(client.get(next_url, follow_redirects=False))


def test_authorize_rejects_bad_params(client, monkeypatch):
    oauth(client, monkeypatch, "google", "g1")
    _, challenge = pkce()
    for uri in (
        "http://localhost:5000/cb",
        "https://127.0.0.1:5000/cb",
        "http://127.0.0.1/cb",
        "http://evil.example:5000/cb",
        "http://user@127.0.0.1:5000/cb",
        "http://127.0.0.1:5000/cb#x",
        "http://127.0.0.1:99999/cb",
    ):
        assert authorize(client, challenge, redirect_uri=uri).status_code == 400, uri
    assert (
        authorize(client, challenge, code_challenge_method="plain").status_code == 400
    )
    assert authorize(client, "short").status_code == 400
    assert authorize(client, challenge, state="").status_code == 400


def test_code_is_single_use_and_needs_verifier(client, monkeypatch):
    oauth(client, monkeypatch, "google", "g1")
    verifier, challenge = pkce()
    code = code_from(authorize(client, challenge))
    other_verifier, _ = pkce()
    r = client.post(
        "/api/auth/client/token", json={"code": code, "code_verifier": other_verifier}
    )
    assert r.status_code == 400
    # 틀린 verifier로 한 번 쓰인 코드는 맞는 verifier로도 못 씀
    r = client.post(
        "/api/auth/client/token", json={"code": code, "code_verifier": verifier}
    )
    assert r.status_code == 400

    code = code_from(authorize(client, challenge))
    assert (
        client.post(
            "/api/auth/client/token", json={"code": code, "code_verifier": verifier}
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/auth/client/token", json={"code": code, "code_verifier": verifier}
        ).status_code
        == 400
    )


def test_expired_code(client, monkeypatch):
    oauth(client, monkeypatch, "google", "g1")
    verifier, challenge = pkce()
    code = code_from(authorize(client, challenge))
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("UPDATE auth_code SET expires_at = 0")
        con.commit()
    assert (
        client.post(
            "/api/auth/client/token", json={"code": code, "code_verifier": verifier}
        ).status_code
        == 400
    )


def test_banned_user(client, monkeypatch):
    key, user_id = client_login(client, monkeypatch)
    verifier, challenge = pkce()
    code = code_from(authorize(client, challenge))
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("UPDATE user SET status = 'banned' WHERE id = %s", (user_id,))
        con.commit()
    assert (
        client.post(
            "/api/auth/client/token", json={"code": code, "code_verifier": verifier}
        ).status_code
        == 403
    )
    assert (
        client.get("/api/me", headers={"Authorization": f"Bearer {key}"}).status_code
        == 401
    )


def test_revoke_client_session_from_account(client, monkeypatch):
    key, user_id = client_login(client, monkeypatch)
    session_id = next(
        s.id for s in accounts.list_sessions(user_id) if s.kind == "client"
    )
    assert client.delete(f"/api/account/sessions/{session_id}").status_code == 204
    assert (
        client.get("/api/me", headers={"Authorization": f"Bearer {key}"}).status_code
        == 401
    )


def test_version_lists_oauths(client, monkeypatch):
    assert client.get("/api/version").json()["auth"] == ["google", "discord"]
    monkeypatch.setattr(constant, "DISCORD_CLIENT_ID", "")
    assert client.get("/api/version").json()["auth"] == ["google"]
