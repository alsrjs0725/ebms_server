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


def _start_signup(client, monkeypatch, puid):
    import urllib.parse
    from ebms_server import auth
    from ebms_server.accounts import Profile
    r = client.get("/auth/google/start", follow_redirects=False)
    state = urllib.parse.parse_qs(urllib.parse.urlparse(r.headers["location"]).query)["state"][0]
    monkeypatch.setattr(auth, "fetch_profile", lambda p, code, verifier: Profile("google", puid))
    r = client.get("/auth/google/callback", params={"code": "c", "state": state}, follow_redirects=False)
    assert r.headers["location"] == "/signup"


def test_signup_requires_agreement(client, monkeypatch):
    from ebms_server import db
    admin_login(client, monkeypatch)
    terms = client.post("/api/admin/policies/terms", json={"body": "약관 첫 판", "effective_on": "2020-01-01"}).json()
    client.post("/auth/logout")

    _start_signup(client, monkeypatch, "agree1")
    page = client.get("/signup").text
    assert "약관 첫 판" in page and "ebms_session" in page and 'name="agree"' in page
    assert f'name="terms_version" value="{terms["id"]}"' in page and 'name="privacy_version" value=""' in page

    form = {"nickname": "동의", "terms_version": str(terms["id"]), "privacy_version": ""}
    assert client.post("/signup", data=form, follow_redirects=False).status_code == 400
    # 화면을 연 뒤 약관이 바뀌면 다시 동의를 받음
    assert client.post("/signup", data={**form, "agree": "1", "terms_version": ""}, follow_redirects=False).status_code == 409
    r = client.post("/signup", data={**form, "agree": "1"}, follow_redirects=False)
    assert r.status_code == 303
    user_id = client.get("/api/me").json()["id"]
    with db.connect() as con, con.cursor() as cur:
        cur.execute("SELECT terms_version, privacy_version, agreed_at FROM user WHERE id = %s", (user_id,))
        row = cur.fetchone()
    assert row[0] == terms["id"] and row[1] is None and row[2] > 0
