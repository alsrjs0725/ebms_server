"""개인정보 처리방침·이용약관 페이지와 관리자 편집 테스트."""
from ebms_server import policies
from test_auth import client, oauth  # noqa: F401  (fixture 재사용)
from test_quota import admin_login


def test_policy_pages_show_template(client):
    for kind in policies.KINDS:
        res = client.get(f"/{kind}")
        assert res.status_code == 200
        assert policies.KINDS[kind] in res.text
    assert "ebms_session" in client.get("/privacy").text
    # 로그인(가입) 화면과 모든 페이지 아래에서 링크
    login = client.get("/login").text
    assert 'href="/privacy"' in login and 'href="/terms"' in login
    assert 'href="/privacy"' in client.get("/").text


def test_policy_admin_only(client, monkeypatch):
    assert client.post("/api/admin/policies/privacy", json={"body": "x"}).status_code == 401
    oauth(client, monkeypatch, "google", "u1")
    assert client.post("/api/admin/policies/privacy", json={"body": "x"}).status_code == 403
    assert client.get("/admin/policies").status_code == 403


def test_policy_save_versions(client, monkeypatch):
    admin_login(client, monkeypatch)
    page = client.get("/admin/policies")
    assert page.status_code == 200
    assert "채우지 않은 칸" in page.text
    assert client.get("/api/admin/policies/terms/template").json()["body"] == policies.template("terms")
    assert client.post("/api/admin/policies/nope", json={"body": "x"}).status_code == 422
    assert client.post("/api/admin/policies/privacy", json={"body": "x", "effective_on": "2026/01/01"}).status_code == 422

    old = client.post("/api/admin/policies/privacy", json={"body": "# 제목\n\n첫 판 <b>", "effective_on": "2020-01-01"}).json()
    res = client.get("/privacy").text
    assert "첫 판 &lt;b&gt;" in res and "시행일 2020-01-01" in res

    # 시행일이 아직 안 온 판은 예정으로 알리고, 지금 판은 그대로
    new = client.post("/api/admin/policies/privacy", json={"body": "둘째 판", "effective_on": "2999-01-01"}).json()
    res = client.get("/privacy").text
    assert "첫 판" in res and "2999-01-01부터" in res and f"/privacy/{new['id']}" in res
    assert "둘째 판" in client.get(f"/privacy/{new['id']}").text
    assert client.get(f"/terms/{new['id']}").status_code == 404
    # 편집 칸은 가장 최근에 저장한 판
    assert "둘째 판" in client.get("/admin/policies").text

    assert client.delete(f"/api/admin/policies/versions/{new['id']}").json() == {"ok": True}
    assert client.delete(f"/api/admin/policies/versions/{old['id']}").json() == {"ok": True}
    assert "ebms_session" in client.get("/privacy").text


def test_render_escapes_and_formats():
    out = str(policies.render(
        "## 제목\n\n- **굵게** [링크](https://example.com)\n- [위험](javascript:alert(1))\n\n"
        "| a | b |\n| --- | --- |\n| <x> | y |\n\n문단<script>"
    ))
    assert "<h2>제목</h2>" in out
    assert "<strong>굵게</strong>" in out and '<a href="https://example.com">링크</a>' in out
    assert "javascript:" not in out.split("<a ")[-1].split(">")[0] and 'href="javascript' not in out
    assert "<th>a</th>" in out and "<td>&lt;x&gt;</td>" in out
    assert "&lt;script&gt;" in out and "<script>" not in out
