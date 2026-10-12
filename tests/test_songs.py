"""관리자 곡 관리(검색·파일 구조·파일 받기·삭제) 테스트."""
import gzip
import io
import json
import os
import zipfile

from ebms_server import admin, constant, db as db_module

from test_auth import client, oauth  # noqa: F401  (fixture 재사용)
from test_client_auth import client_login
from test_import import import_dir, make_zip, upload  # noqa: F401
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

    assert [s["id"] for s in client.get("/api/admin/songs").json()] == [2, 1]
    assert [s["id"] for s in client.get("/api/admin/songs", params={"q": "alp"}).json()] == [1]
    # 차트 파일명과 곡 번호로도 찾습니다.
    assert [s["id"] for s in client.get("/api/admin/songs", params={"q": "beta song"}).json()] == [2]
    assert [s["id"] for s in client.get("/api/admin/songs", params={"q": "1"}).json()] == [1]
    assert client.get("/api/admin/songs", params={"before": 2}).json()[0]["id"] == 1
    assert client.get("/api/admin/songs", params={"q": "100%"}).json() == []

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
        cur.execute("SELECT LENGTH(data) FROM chart_chunk ORDER BY id DESC LIMIT 1")
        chunk_size = cur.fetchone()[0]

    r = client.request("DELETE", "/api/admin/songs/1", json={"reason": "  저작권 요청  "})
    assert r.json() == {"ok": True}
    assert client.delete("/api/admin/songs/1").status_code == 404
    assert client.get("/api/admin/songs/1").status_code == 404
    assert [s["song_id"] for s in manifest_songs()] == [2]
    with zipfile.ZipFile(io.BytesIO(client.get("/api/admin/songs/2/zip").content)) as zf:
        assert zf.namelist() == ["b.bms"]

    # 같은 차트를 다시 넣으면 차트 청크는 그대로 두고 새 곡에 차트를 붙입니다.
    again = upload(client, "a2.zip", make_zip({"a.bms": chart}))
    assert again["songs"][0]["new_song"] and again["songs"][0]["song_id"] == 3
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("SELECT song_id FROM chart WHERE size = %s", (len(chart),))
        assert cur.fetchone()[0] == 3
        cur.execute("SELECT COUNT(*) FROM chart_chunk")
        assert cur.fetchone()[0] == chunks
        cur.execute("SELECT LENGTH(data) FROM chart_chunk ORDER BY id DESC LIMIT 1")
        assert cur.fetchone()[0] == chunk_size
    songs = {s["song_id"]: s for s in manifest_songs()}
    assert sorted(songs) == [2, 3] and len(songs[3]["charts"]) == 1


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

    rows = client.get("/api/admin/songs/deleted").json()
    assert [r["song_id"] for r in rows] == [2, 1]
    assert rows[0]["folder"] == "b" and rows[0]["reason"] == "중복 곡" and rows[0]["charts"] == 1
    assert rows[0]["deleted_by_name"] == "Admin" and rows[0]["deleted_at"] > 0
    assert rows[1]["reason"] == ""
    assert [r["song_id"] for r in client.get("/api/admin/songs/deleted", params={"q": "중복"}).json()] == [2]
    assert [r["song_id"] for r in client.get("/api/admin/songs/deleted", params={"before": 2}).json()] == [1]

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
