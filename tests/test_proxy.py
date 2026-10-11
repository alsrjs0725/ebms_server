"""캐싱 리버스 프록시 연동(/api/proxy/authz, 캐시 채우기 요청) 테스트."""
from ebms_server import constant, db as db_module, quota
from ebms_server.db import Database

from test_auth import client, oauth  # noqa: F401  (fixture 재사용)
from test_client_auth import client_login
from test_quota import add_songs, bearer

SECRET = "proxy-secret"


def authz(client, uri, headers=None, secret=SECRET):
    return client.get(
        "/api/proxy/authz",
        headers={"X-Ebms-Proxy-Secret": secret, "X-Original-URI": uri, **(headers or {})},
    )


def song_sha(song_id=1):
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("SELECT sha256, size FROM song WHERE id = %s", (song_id,))
        return cur.fetchone()


def test_authz_disabled_without_secret(client, monkeypatch):
    monkeypatch.setattr(constant, "PROXY_SECRET", "")
    assert authz(client, "/api/play/song/1").status_code == 404
    monkeypatch.setattr(constant, "PROXY_SECRET", SECRET)
    assert authz(client, "/api/play/song/1", secret="wrong").status_code == 404


def test_authz_denies(tmp_path, client, monkeypatch):
    monkeypatch.setattr(constant, "PROXY_SECRET", SECRET)
    key, _ = client_login(client, monkeypatch)
    client.cookies.clear()  # 로그인 중 받은 웹 세션 쿠키 없이
    add_songs(tmp_path, 1)

    r = authz(client, "/api/play/song/1")
    assert (r.status_code, r.headers["x-ebms-status"]) == (403, "401")
    r = authz(client, "/api/play/song/1", bearer("bad"))
    assert r.headers["x-ebms-status"] == "401"
    r = authz(client, "/api/play/song/99", bearer(key))
    assert (r.headers["x-ebms-status"], r.headers["x-ebms-detail"]) == ("404", "song not found")
    # 캐시 대상이 아닌 경로
    assert authz(client, "/api/pre/song/1/file", bearer(key)).headers["x-ebms-status"] == "404"


def test_authz_play_charges_tickets(tmp_path, client, monkeypatch):
    monkeypatch.setattr(constant, "PROXY_SECRET", SECRET)
    key, user_id = client_login(client, monkeypatch)
    add_songs(tmp_path, 7)
    h = bearer(key)

    r = authz(client, "/api/play/song/1", h)
    assert r.status_code == 204
    sha, _ = song_sha(1)
    assert r.headers["x-ebms-sha256"] == sha
    assert r.headers["x-ebms-limit-rate"] == "0"
    # grant 안이라 같은 곡은 다시 차감하지 않고, 304는 차감하지 않습니다.
    assert authz(client, "/api/play/song/1", h).status_code == 204
    assert authz(client, "/api/play/song/2", {**h, "If-None-Match": f'"{song_sha(2)[0]}"'}).status_code == 204
    for song_id in (3, 4, 5, 6):
        assert authz(client, f"/api/play/song/{song_id}", h).status_code == 204
    assert quota.ticket_state(user_id).available == 0
    r = authz(client, "/api/play/song/7", h)
    assert (r.status_code, r.headers["x-ebms-status"], r.headers["x-ebms-detail"]) == (403, "429", "no download ticket")
    assert 1 <= int(r.headers["x-ebms-retry-after"]) <= 60


def test_authz_pre_meters_requested_bytes(tmp_path, client, monkeypatch):
    monkeypatch.setattr(constant, "PROXY_SECRET", SECRET)
    key, user_id = client_login(client, monkeypatch)
    add_songs(tmp_path, 1)
    h = bearer(key)
    blob = Database().open_blob("chart_chunk", 0)

    assert authz(client, "/api/pre/chart/0", h).headers["x-ebms-sha256"] == blob.sha256
    assert quota.pre_used(user_id) == blob.size
    authz(client, "/api/pre/chart/0", {**h, "Range": "bytes=0-9"})
    assert quota.pre_used(user_id) == blob.size + 10
    authz(client, "/api/pre/chart/0", {**h, "If-None-Match": f'"{blob.sha256}"'})
    assert quota.pre_used(user_id) == blob.size + 10

    quota.set_overrides(user_id, {"pre_monthly_bytes": 0, "pre_throttled_kbps": 64})
    assert authz(client, "/api/pre/chart/0", h).headers["x-ebms-limit-rate"] == "8000"


def test_fill_request_serves_without_user(tmp_path, client, monkeypatch):
    monkeypatch.setattr(constant, "PROXY_SECRET", SECRET)
    key, user_id = client_login(client, monkeypatch)
    add_songs(tmp_path, 1)
    sha, size = song_sha(1)
    fill = {"X-Ebms-Proxy-Secret": SECRET}

    r = client.get("/api/play/song/1", headers={**fill, "X-Ebms-Expect-Sha256": sha})
    assert r.status_code == 200
    assert len(r.content) == size
    assert client.get("/api/pre/chart/0", headers=fill).status_code == 200
    # 채우기 요청은 티켓·사용량과 무관합니다.
    assert quota.ticket_state(user_id).available == 5
    assert quota.pre_used(user_id) == 0
    # authz 이후 내용이 바뀌었으면 캐시에 넣지 않도록 409
    assert client.get("/api/play/song/1", headers={**fill, "X-Ebms-Expect-Sha256": "0" * 64}).status_code == 409
    client.cookies.clear()
    # 비밀값이 틀리면 일반 요청처럼 로그인이 필요합니다.
    assert client.get("/api/play/song/1", headers={"X-Ebms-Proxy-Secret": "wrong"}).status_code == 401
    monkeypatch.setattr(constant, "PROXY_SECRET", "")
    assert client.get("/api/play/song/1", headers=fill).status_code == 401
    assert client.get("/api/play/song/1", headers=bearer(key)).status_code == 200
