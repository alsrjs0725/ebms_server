"""관리자 테스트용 API 키 테스트."""
import time

from fastapi.testclient import TestClient

from ebms_server import constant, db as db_module, retention
from test_auth import client, me, oauth  # noqa: F401  (fixture 재사용)
from test_quota import admin_login


def bearer(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def create_key(client, name="test", days=30) -> str:
    r = client.post("/api/admin/api-keys", json={"name": name, "days": days})
    assert r.status_code == 201, r.text
    key = r.json()["key"]
    assert key.startswith(constant.API_KEY_PREFIX)
    return key


def test_only_admin_creates_keys(client, monkeypatch):
    assert client.post("/api/admin/api-keys", json={"name": "x"}).status_code == 401
    oauth(client, monkeypatch, "google", "u1")
    assert client.post("/api/admin/api-keys", json={"name": "x"}).status_code == 403
    assert client.get("/admin/api-keys").status_code == 403


def test_key_has_admin_rights(client, monkeypatch):
    admin_login(client, monkeypatch)
    admin_id = me(client).id
    assert client.get("/admin/api-keys").status_code == 200
    key = create_key(client)

    # 쿠키 없는 다른 클라이언트에서 키만으로 관리자 API와 일반 API를 부릅니다.
    api = TestClient(client.app)
    assert api.get("/api/admin/settings").status_code == 401
    assert api.get("/api/admin/settings", headers=bearer(key)).status_code == 200
    r = api.get("/api/me", headers=bearer(key))
    assert r.status_code == 200
    assert r.json()["id"] == admin_id and r.json()["session"]["kind"] == "api_key"

    # 키로는 키 관리·계정 조작을 못 합니다.
    assert api.post("/api/admin/api-keys", json={"name": "x"}, headers=bearer(key)).status_code == 401
    assert api.get("/api/admin/api-keys", headers=bearer(key)).status_code == 401
    assert api.delete("/api/account", headers=bearer(key)).status_code == 403

    # 틀린 키·접두어 없는 토큰은 거부
    assert api.get("/api/admin/settings", headers=bearer(key + "x")).status_code == 401
    assert api.get("/api/admin/settings", headers=bearer("abc")).status_code == 401


def test_original_key_is_not_stored_and_listed(client, monkeypatch):
    admin_login(client, monkeypatch)
    key = create_key(client, name="부하 시험")
    keys = client.get("/api/admin/api-keys").json()
    assert len(keys) == 1
    assert keys[0]["name"] == "부하 시험" and key.startswith(keys[0]["prefix"])
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("SELECT token_sha256 FROM api_key")
        assert cur.fetchone()[0] != key


def test_revoke(client, monkeypatch):
    admin_login(client, monkeypatch)
    key = create_key(client)
    key_id = client.get("/api/admin/api-keys").json()[0]["id"]
    assert client.delete(f"/api/admin/api-keys/{key_id}").status_code == 204
    assert client.delete(f"/api/admin/api-keys/{key_id}").status_code == 404
    assert client.get("/api/admin/api-keys").json() == []
    assert TestClient(client.app).get("/api/admin/settings", headers=bearer(key)).status_code == 401


def test_key_blocked_when_owner_not_admin(client, monkeypatch):
    admin_login(client, monkeypatch)
    key = create_key(client)
    admin_id = me(client).id
    monkeypatch.setattr(constant, "ADMIN_USER_IDS", set())
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("UPDATE user SET role = 'user' WHERE id = %s", (admin_id,))
        con.commit()
    assert TestClient(client.app).get("/api/admin/settings", headers=bearer(key)).status_code == 401


def test_key_expiry_and_validation(client, monkeypatch):
    admin_login(client, monkeypatch)
    assert client.post("/api/admin/api-keys", json={"name": " ", "days": 1}).status_code == 400
    assert client.post("/api/admin/api-keys", json={"name": "x", "days": 0}).status_code == 400
    assert client.post("/api/admin/api-keys", json={"name": "x", "days": 366}).status_code == 400
    key = create_key(client, days=1)
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("UPDATE api_key SET expires_at = %s", (int(time.time()) - 1,))
        con.commit()
    assert TestClient(client.app).get("/api/admin/settings", headers=bearer(key)).status_code == 401
    # 만료되고 보유기간이 지나면 지웁니다.
    now = int(time.time()) + (constant.RETENTION_SESSION_DAYS + 1) * 86400
    assert retention.purge(now)["api_keys"] == 1


def test_audit_records_key(client, monkeypatch, caplog):
    admin_login(client, monkeypatch)
    key = create_key(client)
    key_id = client.get("/api/admin/api-keys").json()[0]["id"]
    with caplog.at_level("INFO", logger="ebms_server.audit"):
        TestClient(client.app).get("/api/admin/users", headers=bearer(key))
    assert any(f"api_key={key_id}" in r.getMessage() for r in caplog.records)
