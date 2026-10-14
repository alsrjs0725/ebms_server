"""메인화면 소개 글과 관리자 편집 테스트."""
from ebms_server import home
from test_auth import client, oauth  # noqa: F401  (fixture 재사용)
from test_quota import admin_login


def test_home_shows_default(client):
    res = client.get("/").text
    assert "https://github.com/alsrjs0725/ebms_client/releases" in res
    # 채우지 않은 칸은 메인화면에 보이지 않음
    assert "[[" not in res
    # 로그인 화면에도 보임
    assert "https://github.com/alsrjs0725/ebms_client/releases" in client.get("/login").text


def test_home_admin_only(client, monkeypatch):
    assert client.put("/api/admin/home", json={"body": "x"}).status_code == 401
    oauth(client, monkeypatch, "google", "u1")
    assert client.put("/api/admin/home", json={"body": "x"}).status_code == 403
    assert client.get("/admin/home").status_code == 403


def test_home_save_and_reset(client, monkeypatch):
    admin_login(client, monkeypatch)
    page = client.get("/admin/home")
    assert page.status_code == 200 and "채우지 않은 칸" in page.text and "기본 글을 보여주고" in page.text
    assert 'href="/admin/home"' in client.get("/admin").text

    body = "소개 <b>\n\n- [클라이언트](https://github.com/example/client)\n- 문의: [메일](mailto:a@example.com)"
    assert client.put("/api/admin/home", json={"body": body}).json() == {"body": body, "saved": True}
    res = client.get("/").text
    assert "소개 &lt;b&gt;" in res
    assert '<a href="https://github.com/example/client">클라이언트</a>' in res
    assert '<a href="mailto:a@example.com">메일</a>' in res
    assert "ebms_client/releases" not in res
    assert client.get("/api/admin/home").json()["saved"] is True
    assert "기본 글을 보여주고" not in client.get("/admin/home").text

    client.post("/auth/logout")
    assert '<a href="https://github.com/example/client">클라이언트</a>' in client.get("/login").text
    admin_login(client, monkeypatch)

    # 비우면 소개 글을 보여주지 않음
    client.put("/api/admin/home", json={"body": ""})
    assert "home-content" not in client.get("/").text
    assert "home-content" not in client.get("/login").text

    assert client.delete("/api/admin/home").json() == {"body": home.DEFAULT, "saved": False}
    assert "ebms_client/releases" in client.get("/").text
