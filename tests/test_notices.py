"""공지 조회(공개)와 관리자 공지 관리 테스트."""
import time

from test_auth import client, oauth  # noqa: F401  (fixture 재사용)
from test_quota import admin_login


def test_notice_admin_only(client, monkeypatch):
    assert client.get("/api/admin/notices").status_code == 401
    assert client.post("/api/admin/notices", json={"title": "x"}).status_code == 401
    oauth(client, monkeypatch, "google", "u1", email="user@example.com")
    assert client.post("/api/admin/notices", json={"title": "x"}).status_code == 403
    assert client.get("/admin/notices").status_code == 403


def test_notices(client, monkeypatch):
    # 로그인 없이 조회
    assert client.get("/api/notices").json() == []

    admin_login(client, monkeypatch)
    assert client.get("/admin/notices").status_code == 200
    now = int(time.time())
    r = client.post("/api/admin/notices", json={"title": " 점검 ", "body": "오늘 밤 점검", "level": "warning"})
    assert r.status_code == 200
    first = r.json()
    assert first["title"] == "점검" and first["published"] is True
    client.post("/api/admin/notices", json={"title": "숨김", "published": False})
    client.post("/api/admin/notices", json={"title": "예약", "starts_at": now + 3600})
    client.post("/api/admin/notices", json={"title": "끝남", "ends_at": now - 1})
    assert len(client.get("/api/admin/notices").json()) == 4
    assert "점검" in client.get("/admin/notices").text

    client.cookies.clear()
    [shown] = client.get("/api/notices").json()
    assert shown["id"] == first["id"] and shown["body"] == "오늘 밤 점검" and shown["level"] == "warning"
    assert set(shown) == {"id", "title", "body", "level", "starts_at", "ends_at", "updated_at"}

    admin_login(client, monkeypatch)
    for bad in (
        {"title": ""},
        {"title": "   "},
        {"title": "x", "level": "nope"},
        {"title": "x", "starts_at": now, "ends_at": now},
    ):
        assert client.post("/api/admin/notices", json=bad).status_code == 422, bad

    url = f"/api/admin/notices/{first['id']}"
    r = client.put(url, json={"title": "점검", "body": "바뀜", "published": False})
    assert r.json()["body"] == "바뀜" and r.json()["published"] is False
    assert client.get("/api/notices").json() == []
    assert client.delete(url).json() == {"ok": True}
    assert client.delete(url).status_code == 404
    assert client.put(url, json={"title": "x"}).status_code == 404
    assert len(client.get("/api/admin/notices").json()) == 3
