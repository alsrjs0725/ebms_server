"""보안 헤더(CSP 등)와 페이지가 CSP 안에서 동작하는지(인라인 스크립트·스타일 없음, 외부 리소스 없음) 확인합니다 (#33)."""
import base64
import hashlib
import re

from ebms_server import main

from test_auth import client, oauth  # noqa: F401  (fixture 재사용)

PAGES = ("/", "/login", "/account", "/admin", "/admin/import", "/admin/notices", "/auth/client/authorize")


def assert_security_headers(response):
    for name, value in main.SECURITY_HEADERS.items():
        assert response.headers[name] == value, name
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_security_headers_on_all_responses(client):
    for path in ("/api/version", "/static/css/styles.css", "/nope"):
        assert_security_headers(client.get(path))
    # 리다이렉트(로그인 필요)에도 붙음
    r = client.get("/account", follow_redirects=False)
    assert r.status_code == 303
    assert_security_headers(r)


def test_pages_work_under_csp(client, monkeypatch):
    oauth(client, monkeypatch, "discord", "admin1", email="admin@example.com", name="Admin")
    assert client.post("/api/admin/notices", json={"title": "공지", "body": "본문"}).status_code == 200
    seen_assets = set()
    for path in PAGES:
        r = client.get(path)
        assert r.status_code in (200, 400), path  # client/authorize는 잘못된 요청 안내 페이지(400)
        assert r.headers["content-type"].startswith("text/html"), path
        assert_security_headers(r)
        html = r.text
        # script-src 'self': 인라인 스크립트·이벤트 핸들러·javascript: URL 없음
        assert not re.search(r"<script(?![^>]*\bsrc=)", html), path
        assert not re.search(r"\son[a-z]+\s*=", html), path
        assert "javascript:" not in html, path
        # style-src 'self': style 속성·<style> 없음
        assert not re.search(r"\sstyle\s*=", html), path
        assert "<style" not in html, path
        # 모든 스크립트·스타일시트는 같은 사이트의 static 파일
        for url in re.findall(r'<script[^>]*\bsrc="([^"]+)"', html) + re.findall(r'<link[^>]*\bhref="([^"]+)"', html):
            assert url.startswith("/static/") or url.startswith("http://testserver/static/"), (path, url)
            seen_assets.add(url)
    for url in seen_assets:
        assert client.get(url).status_code == 200, url


def test_vendored_bootstrap_matches_integrity(client):
    r = client.get("/")
    tag = re.search(r'<script[^>]*bootstrap\.bundle\.min\.js[^>]*>', r.text).group(0)
    src = re.search(r'src="([^"]+)"', tag).group(1)
    integrity = re.search(r'integrity="([^"]+)"', tag).group(1)
    body = client.get(src).content
    assert integrity == "sha384-" + base64.b64encode(hashlib.sha384(body).digest()).decode()
