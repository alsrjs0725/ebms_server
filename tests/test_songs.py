"""관리자 곡 관리(검색·파일 구조·파일 받기·삭제) 테스트."""
import gzip
import hashlib
import io
import json
import os
import zipfile

from ebms_server import admin, constant, db as db_module

from test_auth import client, oauth  # noqa: F401  (fixture 재사용)
from test_client_auth import client_login
from test_import import import_dir, import_tmp, make_zip, upload, wait  # noqa: F401
from test_quota import admin_login


def manifest_songs():
    data = db_module.Database().get_manifest_chunk(0)
    return json.loads(gzip.decompress(data)) if data else []


def test_songs_require_admin(client, monkeypatch):
    assert client.get("/admin/songs", follow_redirects=False).status_code == 303
    assert client.get("/api/admin/songs").status_code == 401
    client_login(client, monkeypatch)
    assert client.get("/admin/songs").status_code == 403
    assert client.get("/api/admin/songs").status_code == 403
    assert client.get("/api/admin/songs/1").status_code == 403
    assert client.get("/api/admin/songs/1/zip").status_code == 403
    assert client.get("/api/admin/songs/1/file", params={"path": "a.bms"}).status_code == 403
    assert client.delete("/api/admin/songs/1").status_code == 403
    assert client.get("/api/admin/songs/deleted").status_code == 403


def test_search_and_files(client, monkeypatch):
    admin_login(client, monkeypatch)
    page = client.get("/admin/songs")
    assert page.status_code == 200 and "곡 관리" in page.text
    # 예전 주소는 임포트 탭을 엽니다.
    assert 'data-tab="import" data-url="/admin/import" aria-controls="tab-import" aria-selected="true"' in client.get("/admin/import").text

    wav = os.urandom(3000)
    upload(client, "Alpha.zip", make_zip({"a.bms": b"#TITLE A\n", "snd/kick.wav": wav}))
    upload(client, "beta.zip", make_zip({"Beta Song.bme": b"#TITLE B\n"}))

    assert [s["id"] for s in client.get("/api/admin/songs").json()["items"]] == [2, 1]
    assert [s["id"] for s in client.get("/api/admin/songs", params={"q": "alp"}).json()["items"]] == [1]
    # 차트 파일명과 곡 번호로도 찾습니다.
    assert [s["id"] for s in client.get("/api/admin/songs", params={"q": "beta song"}).json()["items"]] == [2]
    assert [s["id"] for s in client.get("/api/admin/songs", params={"q": "1"}).json()["items"]] == [1]
    # 쪽마다 SEARCH_LIMIT곡
    monkeypatch.setattr(admin, "SEARCH_LIMIT", 1)
    page2 = client.get("/api/admin/songs", params={"page": 2}).json()
    assert [s["id"] for s in page2["items"]] == [1]
    assert (page2["total"], page2["page"], page2["pages"]) == (2, 2, 2)
    assert client.get("/api/admin/songs", params={"page": 3}).json()["items"] == []
    assert client.get("/api/admin/songs", params={"page": 0}).status_code == 422
    monkeypatch.setattr(admin, "SEARCH_LIMIT", 50)
    assert client.get("/api/admin/songs", params={"q": "100%"}).json()["items"] == []

    song = client.get("/api/admin/songs/1").json()
    assert song["folder"] == "Alpha" and len(song["charts"]) == 1
    files = {f["path"]: f for f in song["files"]}
    assert set(files) == {"a.bms", "snd/kick.wav"}
    assert files["a.bms"]["chart"] == song["charts"][0]["sha256"]
    assert "chart" not in files["snd/kick.wav"] and files["snd/kick.wav"]["size"] == 3000
    assert client.get("/api/admin/songs/99").status_code == 404

    r = client.get("/api/admin/songs/1/file", params={"path": "snd/kick.wav"})
    assert r.status_code == 200 and r.content == wav
    assert "filename*=UTF-8''kick.wav" in r.headers["content-disposition"]
    assert client.get("/api/admin/songs/1/file", params={"path": "nope"}).status_code == 404

    r = client.get("/api/admin/songs/1/zip")
    assert r.status_code == 200
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        assert zf.read("snd/kick.wav") == wav


def test_delete_song(client, monkeypatch):
    admin_login(client, monkeypatch)
    chart = b"#TITLE A\n"
    upload(client, "a.zip", make_zip({"a.bms": chart}))
    upload(client, "b.zip", make_zip({"b.bms": b"#TITLE B\n"}))
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM chart_chunk")
        chunks = cur.fetchone()[0]
        cur.execute("SELECT size FROM chart_chunk ORDER BY id DESC LIMIT 1")
        chunk_size = cur.fetchone()[0]

    r = client.request("DELETE", "/api/admin/songs/1", json={"reason": "  저작권 요청  "})
    assert r.json() == {"ok": True}
    assert client.delete("/api/admin/songs/1").status_code == 404
    assert client.get("/api/admin/songs/1").status_code == 404
    assert [s["song_id"] for s in manifest_songs()] == [2]
    with zipfile.ZipFile(io.BytesIO(client.get("/api/admin/songs/2/zip").content)) as zf:
        assert zf.namelist() == ["b.bms"]

    # 같은 차트를 다시 넣으면 건너뛰고 차트 청크·차트 행도 그대로 둡니다.
    again = upload(client, "a2.zip", make_zip({"a.bms": chart}))
    assert again["songs"][0]["skipped"] == "unused" and again["songs"][0]["song_id"] == 1
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("SELECT song_id FROM chart WHERE size = %s", (len(chart),))
        assert cur.fetchone()[0] is None
        cur.execute("SELECT COUNT(*) FROM chart_chunk")
        assert cur.fetchone()[0] == chunks
        cur.execute("SELECT size FROM chart_chunk ORDER BY id DESC LIMIT 1")
        assert cur.fetchone()[0] == chunk_size
    assert [s["song_id"] for s in manifest_songs()] == [2]


def test_delete_waits_for_import(client, monkeypatch):
    admin_login(client, monkeypatch)
    upload(client, "a.zip", make_zip({"a.bms": b"#TITLE A\n"}))
    monkeypatch.setattr(admin, "DELETE_LOCK_TIMEOUT", 0.01)
    lock = db_module.Database._write_lock
    with lock:
        assert client.delete("/api/admin/songs/1").status_code == 409
    assert client.get("/api/admin/songs/1").status_code == 200



def test_deleted_log_and_gap(client, monkeypatch):
    admin_login(client, monkeypatch)
    upload(client, "a.zip", make_zip({"a.bms": b"#TITLE A\n"}))
    upload(client, "b.zip", make_zip({"b.bms": b"#TITLE B\n"}))
    assert client.request("DELETE", "/api/admin/songs/2", json={"reason": "x" * 1001}).status_code == 400
    assert client.request("DELETE", "/api/admin/songs/2", json={"reason": "중복 곡"}).status_code == 200
    assert client.delete("/api/admin/songs/1").status_code == 200  # 사유 없이도 지움

    rows = client.get("/api/admin/songs/deleted").json()["items"]
    assert [r["song_id"] for r in rows] == [2, 1]
    assert rows[0]["folder"] == "b" and rows[0]["reason"] == "중복 곡" and rows[0]["charts"] == 1
    assert rows[0]["deleted_by_name"] == "Admin" and rows[0]["deleted_at"] > 0
    assert rows[1]["reason"] == ""
    assert [r["song_id"] for r in client.get("/api/admin/songs/deleted", params={"q": "중복"}).json()["items"]] == [2]
    monkeypatch.setattr(admin, "SEARCH_LIMIT", 1)
    assert [r["song_id"] for r in client.get("/api/admin/songs/deleted", params={"page": 2}).json()["items"]] == [1]
    monkeypatch.setattr(admin, "SEARCH_LIMIT", 50)

    # AUTO_INCREMENT가 되돌아가도(오래된 MySQL 재시작·복원) 지운 번호는 다시 쓰지 않습니다.
    with db_module.connect() as con, con.cursor() as cur:
        try:
            cur.execute("UPDATE sqlite_sequence SET seq = 0 WHERE name = 'song'")
        except Exception:
            cur.execute("ALTER TABLE song AUTO_INCREMENT = 1")
        con.commit()
    again = upload(client, "c.zip", make_zip({"c.bms": b"#TITLE C\n"}))
    assert again["songs"][0]["song_id"] == 3
    assert client.get("/api/admin/songs/2").status_code == 404


def test_unuse_and_restore(client, monkeypatch):
    admin_login(client, monkeypatch)
    wav = os.urandom(2000)
    upload(client, "a.zip", make_zip({"a.bms": b"#TITLE A\n", "kick.wav": wav}))
    before = client.get("/api/admin/songs/1").json()
    assert client.request("DELETE", "/api/admin/songs/1", json={"reason": "잠시 내림"}).status_code == 200

    # 곡 zip과 차트를 별도 폴더로 옮겨 둡니다.
    folder = constant.UNUSED_DIR / "1"
    sha = before["charts"][0]["sha256"]
    assert (folder / "charts" / f"{sha}.bms").read_bytes() == b"#TITLE A\n"
    with zipfile.ZipFile(folder / "song.zip") as zf:
        assert zf.read("kick.wav") == wav
    assert manifest_songs() == []

    assert client.post("/api/admin/songs/1/restore").json()["files"] == before["files"]
    assert client.post("/api/admin/songs/1/restore").status_code == 404
    assert not folder.exists()
    assert client.get("/api/admin/songs/deleted").json()["items"] == []
    assert client.get("/api/admin/songs/1/file", params={"path": "kick.wav"}).content == wav
    songs = manifest_songs()
    assert [s["song_id"] for s in songs] == [1] and songs[0]["charts"] == [sha]
    # 다시 사용 중지할 수도 있습니다.
    assert client.delete("/api/admin/songs/1").status_code == 200


def test_unuse_chart_missing_from_song_zip(client, monkeypatch):
    """곡 zip에 없는 차트는 차트 청크에서 꺼내 옮겨 두고, 복원할 때 차트 행이 없으면 청크에 다시 넣습니다."""
    admin_login(client, monkeypatch)
    upload(client, "a.zip", make_zip({"a.bms": b"#TITLE A\n", "b.bme": b"#TITLE B\n"}))
    database = db_module.Database()
    # 곡 zip에서 a.bms를 뺍니다(차트는 차트 청크에만 있음).
    data = make_zip({"b.bme": b"#TITLE B\n"})
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute(
            "UPDATE song SET size = %s, sha256 = %s, files = %s WHERE id = 1",
            (len(data), hashlib.sha256(data).hexdigest(), json.dumps(db_module.zip_entries(data))),
        )
        database._put_song_data(cur, 1, data)
        con.commit()
    sha_a = hashlib.sha256(b"#TITLE A\n").hexdigest()

    assert client.delete("/api/admin/songs/1").status_code == 200
    charts = constant.UNUSED_DIR / "1" / "charts"
    assert (charts / f"{sha_a}.bms").read_bytes() == b"#TITLE A\n"
    assert len(list(charts.iterdir())) == 2

    # 차트 행이 없어졌어도 옮겨 둔 차트로 되살립니다.
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("DELETE FROM chart WHERE id = %s", (sha_a,))
        con.commit()
    assert client.post("/api/admin/songs/1/restore").status_code == 200
    assert sorted(c["sha256"] for c in client.get("/api/admin/songs/1").json()["charts"]) == sorted(
        [sha_a, hashlib.sha256(b"#TITLE B\n").hexdigest()]
    )


def test_restore_conflict(client, monkeypatch):
    admin_login(client, monkeypatch)
    upload(client, "a.zip", make_zip({"a.bms": b"#TITLE A\n"}))
    client.delete("/api/admin/songs/1")
    upload(client, "b.zip", make_zip({"b.bms": b"#TITLE B\n"}))
    # 임포트로는 지운 곡의 차트가 다른 곡에 붙지 않으므로 DB를 직접 바꿉니다.
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("UPDATE chart SET song_id = 2 WHERE id = %s", (hashlib.sha256(b"#TITLE A\n").hexdigest(),))
        con.commit()
    r = client.post("/api/admin/songs/1/restore")
    assert r.status_code == 409 and "#2" in r.json()["detail"]
    assert (constant.UNUSED_DIR / "1" / "song.zip").exists()
    assert [r["song_id"] for r in client.get("/api/admin/songs/deleted").json()["items"]] == [1]


def test_unused_song_not_reimported(client, monkeypatch):
    """사용 중지한 곡과 차트가 겹치면 다시 임포트하지 않고 건너뜁니다."""
    admin_login(client, monkeypatch)
    upload(client, "a.zip", make_zip({"a.bms": b"#TITLE A\n", "b.bme": b"#TITLE B\n"}))
    assert client.request("DELETE", "/api/admin/songs/1", json={"reason": "저작권"}).status_code == 200

    # 차트 하나만 겹쳐도, 새 파일이 있어도 건너뜁니다. 같은 zip의 다른 곡은 들어갑니다.
    job = upload(client, "again.zip", make_zip({"x/b.bme": b"#TITLE B\n", "x/new.wav": b"w", "y/c.bms": b"#TITLE C\n"}))
    assert job["songs"] == [
        {"folder": "again/x", "song_id": 1, "new_song": False, "skipped": "unused", "charts": 1, "new_charts": 0},
        {"folder": "again/y", "song_id": 2, "new_song": True, "charts": 1, "new_charts": 1},
    ]
    assert [s["song_id"] for s in manifest_songs()] == [2]
    assert client.get("/api/admin/songs/1").status_code == 404
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("SELECT song_id FROM chart WHERE id = %s", (hashlib.sha256(b"#TITLE B\n").hexdigest(),))
        assert cur.fetchone()[0] is None

    # var/tmp의 원본은 처리한 것으로 보고 지웁니다.
    song = constant.TMP_DIR / "group" / "a"
    song.mkdir(parents=True)
    (song / "a.bms").write_bytes(b"#TITLE A\n")
    assert import_tmp(client)["songs"][0]["skipped"] == "unused"
    assert not song.exists()

    # 복원한 뒤에는 기존 곡으로 합쳐집니다.
    assert client.post("/api/admin/songs/1/restore").status_code == 200
    job = upload(client, "again2.zip", make_zip({"b.bme": b"#TITLE B\n", "new.wav": b"w"}))
    assert job["songs"][0]["song_id"] == 1 and job["songs"][0]["added_files"] == ["new.wav"]


def test_detach_charts(client, monkeypatch):
    """다른 곡 차트가 잘못 병합된 곡에서 그 차트만 떼고, 원본 폴더를 새 곡으로 다시 넣습니다."""
    admin_login(client, monkeypatch)
    upload(client, "host.zip", make_zip({"a.bms": b"#TITLE A\n", "01.ogg": b"host"}))
    # 다른 곡(B)의 차트가 host에 병합돼 host의 01.ogg로 재생되는 상태
    job = upload(client, "other.zip", make_zip({"a.bms": b"#TITLE A\n", "b.bms": b"#TITLE B\n", "01.ogg": b"other"}))
    assert job["songs"][0]["song_id"] == 1
    sha_a, sha_b = (hashlib.sha256(c).hexdigest() for c in (b"#TITLE A\n", b"#TITLE B\n"))
    assert {c["sha256"] for c in client.get("/api/admin/songs/1").json()["charts"]} == {sha_a, sha_b}

    assert client.post("/api/admin/songs/1/charts/detach", json={"charts": ["0" * 64]}).status_code == 400
    assert client.post("/api/admin/songs/1/charts/detach", json={"charts": [sha_a, sha_b]}).status_code == 400
    assert client.post("/api/admin/songs/9/charts/detach", json={"charts": [sha_b]}).status_code == 404
    r = client.post("/api/admin/songs/1/charts/detach", json={"charts": [sha_b]})
    assert r.json() == {"detached": [sha_b], "removed_files": ["b.bms"]}
    song = client.get("/api/admin/songs/1").json()
    assert [c["sha256"] for c in song["charts"]] == [sha_a]
    assert {f["path"] for f in song["files"]} == {"a.bms", "01.ogg"}
    assert [s["charts"] for s in manifest_songs()] == [[sha_a]]

    # 원본 폴더를 다시 넣으면 뗀 차트가 새 곡에 붙습니다(사용 중지한 곡처럼 건너뛰지 않음).
    job = upload(client, "b.zip", make_zip({"b.bms": b"#TITLE B\n", "01.ogg": b"other"}))
    assert job["songs"][0]["new_song"] is True
    new_id = job["songs"][0]["song_id"]
    assert [c["sha256"] for c in client.get(f"/api/admin/songs/{new_id}").json()["charts"]] == [sha_b]
    assert client.get(f"/api/admin/songs/{new_id}/file", params={"path": "01.ogg"}).content == b"other"
