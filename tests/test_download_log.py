"""곡 플레이 다운로드 기록(download_log)과 관리자 조회 테스트."""
from ebms_server import constant, db as db_module, retention
from ebms_server.db import Database

from test_auth import client, oauth  # noqa: F401  (fixture 재사용)
from test_client_auth import client_login
from test_proxy import SECRET, authz
from test_quota import add_songs, admin_login, bearer


def logged():
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("SELECT song_id, result, resumed FROM download_log ORDER BY id")
        return [(song_id, result, bool(resumed)) for song_id, result, resumed in cur.fetchall()]


def test_direct_play_downloads_are_logged(tmp_path, client, monkeypatch):
    key, user_id = client_login(client, monkeypatch)
    add_songs(tmp_path, 7)
    h = bearer(key)

    assert client.get("/api/play/song/1", headers=h).status_code == 200
    assert client.get("/api/play/song/1", headers={**h, "Range": "bytes=10-"}).status_code == 206
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("SELECT sha256 FROM song WHERE id = 1")
        etag = f'"{cur.fetchone()[0]}"'
    # 304는 남기지 않습니다.
    assert client.get("/api/play/song/1", headers={**h, "If-None-Match": etag}).status_code == 304
    for song_id in (2, 3, 4, 5):
        client.get(f"/api/play/song/{song_id}", headers=h)
    assert client.get("/api/play/song/6", headers=h).status_code == 429
    assert client.get("/api/play/song/99", headers=h).status_code == 404
    Database().delete_song(7, "test", user_id)
    assert client.get("/api/play/song/7", headers=h).status_code == 410

    assert logged() == [
        (1, "charged", False), (1, "free", True),
        (2, "charged", False), (3, "charged", False), (4, "charged", False), (5, "charged", False),
        (6, "no_ticket", False), (99, "not_found", False), (7, "deleted", False),
    ]


def test_proxy_authz_is_logged_but_fill_is_not(tmp_path, client, monkeypatch):
    monkeypatch.setattr(constant, "PROXY_SECRET", SECRET)
    key, _ = client_login(client, monkeypatch)
    add_songs(tmp_path, 1)
    h = bearer(key)

    assert authz(client, "/api/play/song/1", h).status_code == 204
    assert authz(client, "/api/play/song/1", {**h, "Range": "bytes=5-"}).status_code == 204
    assert authz(client, "/api/play/song/99", h).headers["x-ebms-status"] == "404"
    # 캐시 채우기 요청(사용자 없음)은 남기지 않습니다.
    client.cookies.clear()
    assert client.get("/api/play/song/1", headers={"X-Ebms-Proxy-Secret": SECRET}).status_code == 200

    assert logged() == [(1, "charged", False), (1, "free", True), (99, "not_found", False)]


def test_admin_search(tmp_path, client, monkeypatch):
    key, user_id = client_login(client, monkeypatch)
    add_songs(tmp_path, 2)
    for song_id in (1, 2, 99):
        client.get(f"/api/play/song/{song_id}", headers=bearer(key))

    assert client.get("/api/admin/downloads").status_code == 403
    admin_login(client, monkeypatch)
    assert client.get("/admin/downloads").status_code == 200

    data = client.get("/api/admin/downloads").json()
    assert data["total"] == 3 and data["pages"] == 1
    newest = data["items"][0]
    assert (newest["song_id"], newest["result"], newest["user_id"], newest["display_name"]) == (99, "not_found", user_id, "Alice")
    assert data["items"][2]["folder"] == "song0"

    assert client.get("/api/admin/downloads", params={"user": "ali"}).json()["total"] == 3
    assert client.get("/api/admin/downloads", params={"user": "nobody"}).json()["total"] == 0
    assert [i["song_id"] for i in client.get("/api/admin/downloads", params={"song": 2}).json()["items"]] == [2]
    assert client.get("/api/admin/downloads", params={"result": "charged"}).json()["total"] == 2
    assert client.get("/api/admin/downloads", params={"result": "bogus"}).status_code == 422


def test_retention_default_is_90_days():
    assert constant.RETENTION_DOWNLOAD_LOG_DAYS == 90


def test_retention_purges_old_rows(client, monkeypatch):
    monkeypatch.setattr(constant, "RETENTION_DOWNLOAD_LOG_DAYS", 90)
    key, user_id = client_login(client, monkeypatch)
    now = 1_800_000_000
    with db_module.connect() as con, con.cursor() as cur:
        for song_id, at in ((1, now - 91 * retention.DAY), (2, now - 89 * retention.DAY)):
            cur.execute(
                "INSERT INTO download_log (user_id, song_id, at, result) VALUES (%s, %s, %s, 'charged')",
                (user_id, song_id, at),
            )
        con.commit()
    assert retention.purge(now)["download_log"] == 1
    assert [s for s, _, _ in logged()] == [2]
