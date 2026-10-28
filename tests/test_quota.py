"""사전/플레이 다운로드, 티켓, 월 사용량·감속, 관리자 설정 테스트."""
import datetime
import io
import os
import time
import zipfile

import pytest

from ebms_server import constant, db as db_module, downloads, quota
from ebms_server.db import Database, file_kinds

from test_auth import client, oauth  # noqa: F401  (fixture 재사용)
from test_client_auth import client_login

GB = 1024 ** 3


def make_song(root, name, extra=None):
    """차트 헤더가 배너·스테이지파일·프리뷰를 가리키는 곡 폴더를 만듭니다."""
    song = root / name
    (song / "bga").mkdir(parents=True)
    chart = (
        b"#TITLE " + name.encode() + b"\r\n"
        b"#BANNER banner.png\r\n"          # 실제 파일은 banner.jpg(확장자 대체)
        b"#STAGEFILE \x83X\x83e\x81[\x83W.bmp\r\n"  # Shift_JIS 'ステージ.bmp'
        b"#PREVIEW sub\\pv.wav\r\n"        # 역슬래시 경로, 대소문자 다름
        b"#BMP01 bga\\movie.mp4\r\n"
        b"#WAV01 sound.wav\r\n"
    )
    (song / "a.bms").write_bytes(chart)
    (song / "banner.jpg").write_bytes(os.urandom(2_000))
    (song / "ステージ.bmp").write_bytes(os.urandom(3_000))
    (song / "sub").mkdir()
    (song / "sub" / "PV.wav").write_bytes(os.urandom(1_000))
    (song / "preview_auto.ogg").write_bytes(os.urandom(1_500))
    (song / "banner.wav").write_bytes(os.urandom(500))  # 이름만 같은 키음은 play
    (song / "sound.wav").write_bytes(os.urandom(5_000))
    (song / "bga" / "movie.mp4").write_bytes(os.urandom(50_000))
    for path, body in (extra or {}).items():
        (song / path).write_bytes(body)
    return song


EXPECTED_KINDS = {
    "a.bms": "pre",
    "banner.jpg": "pre",
    "ステージ.bmp": "pre",
    "sub/PV.wav": "pre",
    "preview_auto.ogg": "pre",
    "banner.wav": "play",
    "sound.wav": "play",
    "bga/movie.mp4": "play",
}


def bearer(key):
    return {"Authorization": f"Bearer {key}"}


def add_songs(tmp_path, n):
    for i in range(n):
        Database().insert_song(make_song(tmp_path, f"song{i}"))


def manifest_files(client, key, song_id=1):
    entry = next(e for e in client.get("/api/pre/manifest/0", headers=bearer(key)).json() if e["song_id"] == song_id)
    return {f["path"]: f for f in entry["files"]}


def admin_login(client, monkeypatch):
    oauth(client, monkeypatch, "discord", "admin1", name="Admin", admin=True)


# ---- 사전/플레이 판정 ----

def test_file_kinds(tmp_path):
    song = make_song(tmp_path, "s")
    data = Database.create_zip(None, song)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert file_kinds(zf) == EXPECTED_KINDS


def test_file_kinds_strict_rules(tmp_path):
    """헤더 값의 확장자가 헤더 형식이 아니거나 없으면 무시하고, preview*는 차트 폴더의 오디오만,
    키음·BGA로 쓰는 파일은 사전 헤더가 가리키지 않는 한 play (#36)"""
    song = tmp_path / "s"
    (song / "sub").mkdir(parents=True)
    (song / "a.bms").write_bytes(
        b"#BANNER sound.wav\r\n"         # 이미지 헤더에 오디오 → 무시
        b"#STAGEFILE bga\\movie.mp4\r\n"  # 이미지 헤더에 동영상 → 무시
        b"#PREVIEW bgm\r\n"              # 확장자 없음 → 무시
        b"#BACKBMP back.png\r\n"
        b"#WAV01 sound.wav\r\n"
        b"#WAV02 preview_full.ogg\r\n"    # preview*지만 키음
        b"#wav03 preview_hook.wav\r\n"    # 키음이지만 #PREVIEW도 가리킴
        b"#PREVIEW preview_hook.wav\r\n"
        b"#BMP01 bga\\movie.mp4\r\n"
    )
    for name in (
        "sound.wav", "sound.png", "bgm.ogg", "bgm.wav", "back.jpg", "back.wav",
        "preview_full.ogg", "preview_full.wav", "preview_hook.wav", "preview.ogg",
        "previewbga.mp4", "preview.png", "sub/preview.ogg",
    ):
        (song / name).write_bytes(os.urandom(100))
    (song / "bga").mkdir()
    (song / "bga" / "movie.mp4").write_bytes(os.urandom(100))
    data = Database.create_zip(None, song)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert file_kinds(zf) == {
            "a.bms": "pre",
            "sound.wav": "play",
            "sound.png": "play",
            "bgm.ogg": "play",
            "bgm.wav": "play",
            "back.jpg": "pre",
            "back.wav": "play",
            "preview_full.ogg": "play",
            "preview_full.wav": "play",
            "preview_hook.wav": "pre",
            "preview.ogg": "pre",
            "previewbga.mp4": "play",
            "preview.png": "play",
            "sub/preview.ogg": "play",
            "bga/movie.mp4": "play",
        }


def test_manifest_kind(tmp_path, client, monkeypatch):
    key, _ = client_login(client, monkeypatch)
    add_songs(tmp_path, 1)
    assert {p: f["kind"] for p, f in manifest_files(client, key).items()} == EXPECTED_KINDS


# ---- 인증 ----

def test_download_api_requires_login(tmp_path, client):
    add_songs(tmp_path, 1)
    for path in (
        "/api/pre/charthash",
        "/api/pre/sizes",
        "/api/pre/chart/0",
        "/api/pre/manifest/hash",
        "/api/pre/manifest/0",
        "/api/pre/song/1/file?path=banner.jpg",
        "/api/play/song/1",
    ):
        assert client.get(path).status_code == 401, path
        assert client.get(path, headers=bearer("wrong")).status_code == 401, path


# ---- 사전 다운로드 ----

def test_pre_endpoints_and_usage(tmp_path, client, monkeypatch):
    key, user_id = client_login(client, monkeypatch)
    song = make_song(tmp_path, "song0")
    Database().insert_song(song)
    h = bearer(key)

    assert client.get("/api/pre/charthash", headers=h).json() == {
        str(k): v for k, v in Database().get_chart_chunk_hash().items()
    }
    chunk = client.get("/api/pre/chart/0", headers=h)
    assert chunk.status_code == 200
    sizes = client.get("/api/pre/sizes", headers=h).json()
    assert sizes["chart"]["0"] == {"sha256": Database().get_chart_chunk_hash()[0], "size": len(chunk.content)}
    assert {k: v["sha256"] for k, v in sizes["asset"].items()} == {
        str(k): v for k, v in Database().get_pre_chunk_hash().items()
    }
    assert client.get("/api/pre/chart/0", headers={**h, "Range": "bytes=0-9"}).content == chunk.content[:10]
    assert client.get("/api/pre/manifest/hash", headers=h).json() == {
        str(k): v for k, v in Database().get_manifest_hash().items()
    }
    manifest = client.get("/api/pre/manifest/0", headers=h)
    assert manifest.headers["content-encoding"] == "gzip"
    assert client.get("/api/pre/manifest/9", headers=h).status_code == 404

    banner = client.get("/api/pre/song/1/file", params={"path": "banner.jpg"}, headers=h)
    assert banner.status_code == 200
    assert banner.content == (song / "banner.jpg").read_bytes()
    assert banner.headers["content-type"] == "image/jpeg"
    stage = client.get("/api/pre/song/1/file", params={"path": "ステージ.bmp"}, headers=h)
    assert stage.content == (song / "ステージ.bmp").read_bytes()
    assert client.get("/api/pre/song/1/file", params={"path": "banner.jpg"},
                      headers={**h, "If-None-Match": banner.headers["etag"]}).status_code == 304

    assert client.get("/api/pre/song/1/file", params={"path": "sound.wav"}, headers=h).status_code == 403
    assert client.get("/api/pre/song/1/file", params={"path": "nope.png"}, headers=h).status_code == 404
    assert client.get("/api/pre/song/9/file", params={"path": "banner.jpg"}, headers=h).status_code == 404

    # 보낼(압축 해제한) 바이트를 요청 때 더합니다. 해시 목록과 304는 세지 않습니다.
    # 매니페스트는 gzip 그대로 보냅니다(httpx가 풀어서 보여줌).
    gz = len(Database().get_manifest_chunk(0))
    sent = len(chunk.content) + 10 + gz + len(banner.content) + len(stage.content)
    assert quota.pre_used(user_id) == sent
    me = client.get("/api/me", headers=h).json()
    assert me["pre"]["used_bytes"] == sent
    assert me["pre"]["limit_bytes"] == 10 * GB
    assert me["pre"]["throttled"] is False


def test_pre_throttled_after_monthly_limit(tmp_path, client, monkeypatch):
    key, user_id = client_login(client, monkeypatch)
    add_songs(tmp_path, 1)
    delays = []

    async def fake_sleep(seconds):
        delays.append(seconds)

    monkeypatch.setattr(downloads.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(downloads, "THROTTLE_PIECE", 100)
    url = "/api/pre/song/1/file?path=banner.jpg"

    # 한도 안에서는 감속하지 않습니다.
    full = client.get(url, headers=bearer(key)).content
    assert delays == []

    quota.set_overrides(user_id, {"pre_monthly_bytes": 0, "pre_throttled_kbps": 64})  # 8000 B/s
    assert client.get(url, headers=bearer(key)).content == full
    # 100바이트씩 나눠 보내고 조각마다 그 속도만큼 쉽니다(연결 단위).
    assert len(delays) == -(-len(full) // 100)
    assert sum(delays) == pytest.approx(len(full) / 8000)
    assert client.get("/api/me", headers=bearer(key)).json()["pre"]["throttled"] is True


def test_month_is_kst():
    # 2026-10-01 00:00 KST = 2026-09-30 15:00 UTC
    boundary = datetime.datetime(2026, 9, 30, 15, tzinfo=datetime.timezone.utc).timestamp()
    assert quota.current_month(boundary - 1) == "2026-09"
    assert quota.current_month(boundary) == "2026-10"


# ---- 플레이 다운로드·티켓 ----

def set_ticket_clock(user_id, seconds_ago):
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("UPDATE user_ticket SET updated_at = updated_at - %s WHERE user_id = %s", (seconds_ago, user_id))
        con.commit()


def test_play_tickets(tmp_path, client, monkeypatch):
    key, user_id = client_login(client, monkeypatch)
    add_songs(tmp_path, 7)
    h = bearer(key)

    me = client.get("/api/me", headers=h).json()
    assert me["tickets"] == {"available": 5, "max": 5, "refill_seconds": 60, "next_refill_at": None, "next_refill_in": None}

    first = client.get("/api/play/song/1", headers=h)
    assert first.status_code == 200
    with zipfile.ZipFile(io.BytesIO(first.content)) as zf:
        assert "bga/movie.mp4" in zf.namelist()
    # 같은 곡은 grant 안에서 다시 차감하지 않습니다(이어받기 포함).
    assert client.get("/api/play/song/1", headers={**h, "Range": "bytes=10-"}).status_code == 206
    assert client.get("/api/play/song/1", headers=h).status_code == 200
    assert client.get("/api/me", headers=h).json()["tickets"]["available"] == 4

    for song_id in (2, 3, 4, 5):
        assert client.get(f"/api/play/song/{song_id}", headers=h).status_code == 200
    me = client.get("/api/me", headers=h).json()
    assert me["tickets"]["available"] == 0
    assert me["tickets"]["next_refill_at"] is not None
    assert 1 <= me["tickets"]["next_refill_in"] <= 60

    r = client.get("/api/play/song/6", headers=h)
    assert r.status_code == 429
    assert r.json() == {"detail": "no download ticket"}
    assert 1 <= int(r.headers["retry-after"]) <= 60
    # 이미 받은 곡은 계속 받을 수 있고, 304·없는 곡은 티켓과 무관합니다.
    assert client.get("/api/play/song/2", headers=h).status_code == 200
    assert client.get("/api/play/song/99", headers=h).status_code == 404

    # 60초가 지나면 1개가 찹니다.
    set_ticket_clock(user_id, 61)
    assert client.get("/api/play/song/6", headers=h).status_code == 200
    assert client.get("/api/play/song/7", headers=h).status_code == 429

    # 최대치를 넘게 쌓이지 않습니다.
    set_ticket_clock(user_id, 3600)
    assert client.get("/api/me", headers=h).json()["tickets"]["available"] == 5


def test_play_not_modified_does_not_charge(tmp_path, client, monkeypatch):
    key, _ = client_login(client, monkeypatch)
    add_songs(tmp_path, 1)
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("SELECT sha256 FROM song WHERE id = 1")
        etag = f'"{cur.fetchone()[0]}"'
    r = client.get("/api/play/song/1", headers={**bearer(key), "If-None-Match": etag})
    assert r.status_code == 304
    assert client.get("/api/me", headers=bearer(key)).json()["tickets"]["available"] == 5


def test_grant_expires(tmp_path, client, monkeypatch):
    key, user_id = client_login(client, monkeypatch)
    add_songs(tmp_path, 1)
    h = bearer(key)
    client.get("/api/play/song/1", headers=h)
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("UPDATE download_grant SET expires_at = expires_at - 1801")
        con.commit()
    client.get("/api/play/song/1", headers=h)
    assert client.get("/api/me", headers=h).json()["tickets"]["available"] == 3


# ---- 관리자 ----

def test_admin_requires_admin(client, monkeypatch):
    assert client.get("/api/admin/settings").status_code == 401
    r = client.get("/admin", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    key, _ = client_login(client, monkeypatch)
    assert client.get("/api/admin/settings").status_code == 403
    assert client.get("/admin").status_code == 403
    # 관리자 API는 웹 세션 쿠키로만 씁니다.
    client.cookies.clear()
    assert client.get("/api/admin/settings", headers=bearer(key)).status_code == 401


def test_admin_settings(tmp_path, client, monkeypatch):
    key, user_id = client_login(client, monkeypatch)
    admin_login(client, monkeypatch)
    assert client.get("/api/admin/settings").json() == constant.DEFAULT_SETTINGS

    r = client.put("/api/admin/settings", json={"max_tickets": 2, "refill_seconds": 30})
    assert r.status_code == 200
    assert r.json()["max_tickets"] == 2 and r.json()["refill_seconds"] == 30
    assert client.get("/api/me", headers=bearer(key)).json()["tickets"]["max"] == 2

    # 하나라도 틀리면 아무것도 바꾸지 않습니다.
    for bad in ({"max_tickets": 3, "refill_seconds": 0}, {"unknown": 1}, {"max_tickets": "3"}, {"max_tickets": -1}):
        assert client.put("/api/admin/settings", json=bad).status_code == 400, bad
    assert client.get("/api/admin/settings").json()["max_tickets"] == 2

    # 재시작해도 관리자가 바꾼 값은 유지됩니다.
    Database().generate_database()
    assert quota.get_settings()["max_tickets"] == 2

    assert client.get("/admin").status_code == 200


def test_admin_user_overrides(tmp_path, client, monkeypatch):
    key, user_id = client_login(client, monkeypatch)
    add_songs(tmp_path, 3)
    admin_login(client, monkeypatch)

    [found] = client.get("/api/admin/users", params={"q": "alic"}).json()
    assert found["id"] == user_id and found["oauths"] == ["google"]
    assert found["overrides"] == {name: None for name in quota.USER_LIMITS}
    assert len(client.get("/api/admin/users").json()) == 2

    r = client.put(f"/api/admin/users/{user_id}", json={"max_tickets": 1, "pre_monthly_bytes": 5 * GB})
    assert r.status_code == 200
    assert r.json()["overrides"]["max_tickets"] == 1
    assert r.json()["tickets"]["max"] == 1
    assert r.json()["pre"]["limit_bytes"] == 5 * GB

    h = bearer(key)
    assert client.get("/api/play/song/1", headers=h).status_code == 200
    assert client.get("/api/play/song/2", headers=h).status_code == 429

    # 즉시 충전
    r = client.post(f"/api/admin/users/{user_id}/refill")
    assert r.json()["tickets"]["available"] == 1
    assert client.get("/api/play/song/2", headers=h).status_code == 200

    # null이면 전역 기본값으로 돌아갑니다.
    r = client.put(f"/api/admin/users/{user_id}", json={"max_tickets": None})
    assert r.json()["overrides"]["max_tickets"] is None
    assert r.json()["tickets"]["max"] == 5
    assert r.json()["overrides"]["pre_monthly_bytes"] == 5 * GB

    assert client.put(f"/api/admin/users/{user_id}", json={"refill_seconds": 0}).status_code == 400
    assert client.put("/api/admin/users/nope", json={"max_tickets": 1}).status_code == 404
    assert client.post("/api/admin/users/nope/refill").status_code == 404


def test_admin_user_list_batches_queries(tmp_path, client, monkeypatch):
    """관리 목록은 사용자 수와 상관없이 연결 1개·쿼리 몇 번으로 만듭니다 (#40)"""
    from ebms_server import accounts, admin
    from ebms_server.accounts import Profile

    add_songs(tmp_path, 1)
    users = [accounts.signup(Profile("google", f"g{i}"), f"user{i}") for i in range(12)]
    accounts.link(users[0].id, Profile("discord", "d0"))
    quota.charge_play(users[1].id, 1)
    quota.add_pre_usage(users[2].id, 1234)
    quota.set_overrides(users[3].id, {"max_tickets": 9})
    # 목록과 기대값을 만드는 사이에 초가 바뀌면 next_refill_in이 1 달라지므로 시각을 고정합니다.
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now)
    expected = {user.id: admin.get_user_detail(user.id) for user in users}

    connects = []
    queries = []
    original = db_module.connect

    class CountingCursor:
        def __init__(self, cur):
            self.cur = cur

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.cur.close()

        def execute(self, query, args=None):
            queries.append(query)
            return self.cur.execute(query, args)

        def __getattr__(self, name):
            return getattr(self.cur, name)

    class CountingConnection:
        def __init__(self, inner):
            self.inner = inner

        def __enter__(self):
            self.con = self.inner.__enter__()
            return self

        def __exit__(self, *exc):
            return self.inner.__exit__(*exc)

        def cursor(self):
            return CountingCursor(self.con.cursor())

        def __getattr__(self, name):
            return getattr(self.con, name)

    def counting_connect(**kwargs):
        connects.append(kwargs)
        return CountingConnection(original(**kwargs))

    monkeypatch.setattr(db_module, "connect", counting_connect)
    found = {u["id"]: u for u in admin.search_users()}
    assert len(connects) == 1
    assert len(queries) == 5  # 사용자, 설정, OAuth, 티켓, 사전 사용량
    assert len(found) == 12
    for user_id, detail in expected.items():
        assert found[user_id] == detail
    assert found[users[0].id]["oauths"] == ["google", "discord"]
    assert found[users[1].id]["tickets"]["available"] == 4
    assert found[users[2].id]["pre"]["used_bytes"] == 1234
    assert found[users[3].id]["tickets"]["max"] == 9


def test_admin_ban(client, monkeypatch):
    key, user_id = client_login(client, monkeypatch)
    admin_login(client, monkeypatch)
    admin_id = next(u["id"] for u in client.get("/api/admin/users").json() if u["role"] == "admin")

    assert client.put(f"/api/admin/users/{user_id}", json={"status": "banned"}).json()["status"] == "banned"
    assert client.get("/api/me", headers=bearer(key)).status_code == 401
    assert client.put(f"/api/admin/users/{user_id}", json={"status": "active"}).status_code == 200
    assert client.get("/api/me", headers=bearer(key)).status_code == 200

    assert client.put(f"/api/admin/users/{user_id}", json={"status": "gone"}).status_code == 400
    assert client.put(f"/api/admin/users/{admin_id}", json={"status": "banned"}).status_code == 400


def test_admin_user_update_role(client, monkeypatch):
    key, user_id = client_login(client, monkeypatch)
    admin_login(client, monkeypatch)
    admin_id = next(u["id"] for u in client.get("/api/admin/users").json() if u["role"] == "admin")

    # 일반 사용자를 관리자로 승격
    r = client.put(f"/api/admin/users/{user_id}", json={"role": "admin"})
    assert r.status_code == 200
    assert r.json()["role"] == "admin"

    # 다시 일반 사용자로 강등
    r = client.put(f"/api/admin/users/{user_id}", json={"role": "user"})
    assert r.status_code == 200
    assert r.json()["role"] == "user"

    # 잘못된 role 전달 시 400
    assert client.put(f"/api/admin/users/{user_id}", json={"role": "super"}).status_code == 400

    # 자기 자신은 강등할 수 없음
    assert client.put(f"/api/admin/users/{admin_id}", json={"role": "user"}).status_code == 400


def _serve(response, fail_at):
    """응답을 ASGI로 보내되 fail_at번째 send에서 연결이 끊긴 것처럼 OSError를 냅니다."""
    import anyio
    from starlette.requests import ClientDisconnect

    sent = []

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        if len(sent) == fail_at:
            raise OSError("client gone")
        sent.append(message)

    scope = {"type": "http", "asgi": {"spec_version": "2.4"}, "method": "GET"}
    with pytest.raises(ClientDisconnect):
        anyio.run(response, scope, receive, send)
    return sent


def test_min_throttled_kbps_validation():
    with pytest.raises(ValueError, match="pre_throttled_kbps must be between 64 and"):
        quota.validate("pre_throttled_kbps", 10)

    assert quota.validate("pre_throttled_kbps", 64) == 64


def test_disconnect_closes_file(tmp_path, client, monkeypatch):
    """본문을 보내다 끊기면 열어 둔 저장소 파일을 닫습니다."""
    from starlette.requests import Request

    from ebms_server import db as db_mod

    add_songs(tmp_path, 1)
    closed = []
    real_close = db_mod.StoreFile.close

    def close(self):
        closed.append(self._fd is not None)
        real_close(self)

    monkeypatch.setattr(db_mod.StoreFile, "close", close)
    request = Request({"type": "http", "method": "GET", "headers": [], "query_string": b""})
    _serve(downloads.file_response(request, "song", 1, "1.zip", "x"), fail_at=1)
    assert True in closed
