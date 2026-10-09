"""관리자 곡 임포트(zip 조각 업로드, var/tmp 가져오기) 테스트."""
import io
import os
import time
import zipfile

import pytest

from ebms_server import constant, db as db_module, importer

from test_auth import client, oauth  # noqa: F401  (fixture 재사용)
from test_client_auth import client_login
from test_quota import admin_login


@pytest.fixture(autouse=True)
def import_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(constant, "IMPORT_DIR", tmp_path / "import")


class Cp932ZipInfo(zipfile.ZipInfo):
    """일본어 Windows에서 만든 zip처럼 UTF-8 플래그 없이 cp932 바이트로 이름을 저장합니다."""

    def _encodeFilenameFlags(self):
        return self.filename.encode("cp932"), self.flag_bits & ~0x800


def make_zip(entries: dict[str, bytes], cp932: bool = False) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, body in entries.items():
            zf.writestr((Cp932ZipInfo if cp932 else zipfile.ZipInfo)(name), body)
    data = buf.getvalue()
    if cp932:
        assert all(not i.flag_bits & 0x800 for i in zipfile.ZipFile(io.BytesIO(data)).infolist())
    return data


def wait(client, job):
    for _ in range(200):
        if job["status"] != "running":
            return job
        time.sleep(0.02)
        job = client.get(f"/api/admin/import/jobs/{job['id']}").json()
    raise AssertionError("job did not finish")


def upload(client, name, data, chunk=100):
    """조각으로 올리고 작업이 끝날 때까지 기다린 결과를 반환합니다."""
    r = client.post("/api/admin/import/uploads", json={"filename": name, "size": len(data)})
    assert r.status_code == 200, r.text
    upload_id = r.json()["id"]
    for offset in range(0, len(data), chunk):
        r = client.put(f"/api/admin/import/uploads/{upload_id}", params={"offset": offset}, content=data[offset:offset + chunk])
        assert r.status_code == 200, r.text
        assert r.json()["received"] == min(len(data), offset + chunk)
    r = client.post(f"/api/admin/import/uploads/{upload_id}/finish")
    assert r.status_code == 200, r.text
    return wait(client, r.json())


def song_rows():
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("SELECT id, folder FROM song ORDER BY id")
        return cur.fetchall()


def test_import_requires_admin(client, monkeypatch):
    start = {"filename": "a.zip", "size": 1}
    assert client.get("/admin/import", follow_redirects=False).status_code == 303
    assert client.post("/api/admin/import/uploads", json=start).status_code == 401
    client_login(client, monkeypatch)
    assert client.get("/admin/import").status_code == 403
    assert client.post("/api/admin/import/uploads", json=start).status_code == 403
    assert client.put("/api/admin/import/uploads/x", params={"offset": 0}, content=b"x").status_code == 403
    assert client.post("/api/admin/import/uploads/x/finish").status_code == 403
    assert client.post("/api/admin/import/tmp").status_code == 403
    assert client.get("/api/admin/import/jobs").status_code == 403


def test_import_zip(client, monkeypatch):
    admin_login(client, monkeypatch)
    assert client.get("/admin/import").status_code == 200

    # 최상위에 차트가 있는 zip
    flat = upload(client, "flat.zip", make_zip({"a.bms": b"#TITLE A\n", "bga/movie.bin": os.urandom(1000)}))
    assert flat["status"] == "done" and flat["error"] is None
    assert flat["total"] == 1 and flat["done"] == 1
    assert flat["songs"] == [{"folder": "flat", "song_id": 1, "new_song": True, "charts": 1, "new_charts": 1}]

    # 곡 폴더 두 개가 든 zip(cp932 이름). 곡 안의 하위 폴더 차트는 같은 곡이고, 폴더 밖 경로는 버립니다.
    multi = upload(client, "multi.zip", make_zip({
        "pack/曲1/b.bme": b"#TITLE B\n",
        "pack/曲1/音.wav": os.urandom(500),
        "pack/曲1/sub/b2.bms": b"#TITLE B2\n",
        "pack/曲2/c.bms": b"#TITLE C\n",
        "pack/曲2/c2.bms": b"#TITLE A\n",
        "pack/readme.txt": b"hi",
        "../evil.bms": b"#EVIL\n",
    }, cp932=True))
    assert multi["status"] == "done" and multi["error"] is None
    assert multi["songs"] == [
        {"folder": "multi/pack/曲1", "song_id": 2, "new_song": True, "charts": 1, "new_charts": 1},
        # 같은 차트(a.bms)가 있어 기존 곡 1에 연결되고, 곡 1의 zip에 없는 파일은 그 zip에 더합니다.
        {"folder": "multi/pack/曲2", "song_id": 1, "new_song": False, "charts": 2, "new_charts": 1,
         "added_files": ["c.bms", "c2.bms"]},
    ]

    bad = upload(client, "bad.zip", b"not a zip")
    assert bad["status"] == "failed" and bad["error"]
    empty = upload(client, "empty.zip", make_zip({"readme.txt": b"hi"}))
    assert empty["status"] == "done" and empty["total"] == 0 and empty["error"]

    assert [tuple(row) for row in song_rows()] == [(1, "flat"), (2, "曲1")]
    with zipfile.ZipFile(io.BytesIO(db_module.Database().get_song_data(2))) as zf:
        assert sorted(zf.namelist()) == ["b.bme", "sub/b2.bms", "音.wav"]
    # 받은 zip과 임시 폴더는 남기지 않습니다.
    assert not any(constant.IMPORT_DIR.iterdir())
    # 최근 작업부터 보여줍니다.
    assert [j["name"] for j in client.get("/api/admin/import/jobs").json()][:4] == ["empty.zip", "bad.zip", "multi.zip", "flat.zip"]


def test_upload_chunks(client, monkeypatch):
    admin_login(client, monkeypatch)
    upload_id = client.post("/api/admin/import/uploads", json={"filename": "a.zip", "size": 10}).json()["id"]
    url = f"/api/admin/import/uploads/{upload_id}"
    assert client.put(url, params={"offset": 0}, content=b"12345").json()["received"] == 5
    # 받은 크기와 다른 위치는 409와 현재 위치를 알려줍니다(재전송·이어 올리기).
    r = client.put(url, params={"offset": 0}, content=b"12345")
    assert r.status_code == 409 and "offset must be 5" in r.json()["detail"]
    # 선언한 크기를 넘으면 400, 덜 받았으면 finish 409
    assert client.put(url, params={"offset": 5}, content=b"123456").status_code == 400
    assert client.post(f"{url}/finish").status_code == 409
    assert client.delete(url).status_code == 200
    assert client.put(url, params={"offset": 5}, content=b"1").status_code == 404
    assert not any(constant.IMPORT_DIR.iterdir())

    # 디스크가 모자라면 시작하지 않습니다.
    monkeypatch.setattr(importer, "DISK_MARGIN", 1 << 62)
    r = client.post("/api/admin/import/uploads", json={"filename": "a.zip", "size": 10})
    assert r.status_code == 507


def test_import_tmp(client, monkeypatch):
    admin_login(client, monkeypatch)
    song = constant.TMP_DIR / "group" / "s1"
    song.mkdir(parents=True)
    (song / "a.bms").write_bytes(b"#TITLE T\n")
    (constant.TMP_DIR / "loose.bms").write_bytes(b"#LOOSE\n")  # var/tmp 바로 아래 파일은 보지 않습니다.

    r = client.post("/api/admin/import/tmp")
    assert r.status_code == 200
    job = wait(client, r.json())
    assert job["songs"] == [{"folder": "group/s1", "song_id": 1, "new_song": True, "charts": 1, "new_charts": 1}]
    assert not song.exists()
    assert (constant.TMP_DIR / "loose.bms").exists()
    assert wait(client, client.post("/api/admin/import/tmp").json())["songs"] == []


def test_import_tmp_zip(client, monkeypatch):
    """내부망으로 서버에 직접 복사한 zip도 var/tmp에서 등록합니다."""
    admin_login(client, monkeypatch)
    (constant.TMP_DIR / "pack.zip").write_bytes(make_zip({"p/s1/a.bms": b"#TITLE 1\n", "p/s2/b.bms": b"#TITLE 2\n"}))
    (constant.TMP_DIR / "copying.zip").write_bytes(b"PK\x03\x04partial")  # 아직 복사 중인 zip

    job = wait(client, client.post("/api/admin/import/tmp").json())
    assert job["status"] == "done" and job["total"] == 2
    assert [s["folder"] for s in job["songs"]] == ["copying.zip", "pack/p/s1", "pack/p/s2"]
    assert job["songs"][0]["error"]
    assert job["songs"][1]["new_song"] and job["songs"][2]["new_song"]
    # 다 등록한 zip은 지우고, 읽지 못한 zip은 남깁니다.
    assert not (constant.TMP_DIR / "pack.zip").exists()
    assert (constant.TMP_DIR / "copying.zip").exists()


def test_safe_parts():
    assert importer._safe_parts("a/b.bms") == ["a", "b.bms"]
    assert importer._safe_parts("a\\.\\b.bms") == ["a", "b.bms"]
    for bad in ("/etc/x", "../x", "a/../../x", "C:/x", "C:x"):
        assert importer._safe_parts(bad) is None, bad


def test_import_tmp_skips_symlinks(client, monkeypatch, tmp_path):
    """곡 폴더 안의 심볼릭 링크(파일·폴더)는 따라가지 않고 건너뜁니다(#56)."""
    admin_login(client, monkeypatch)
    outside = tmp_path / "outside"
    (outside / "dir").mkdir(parents=True)
    secret = b"EBMS_SECRET_KEY=leaked"
    (outside / "secret.txt").write_bytes(secret)
    (outside / "dir" / "inner.wav").write_bytes(secret)
    (outside / "song").mkdir()
    (outside / "song" / "o.bms").write_bytes(b"#TITLE OUT\n")

    song = constant.TMP_DIR / "group" / "s1"
    song.mkdir(parents=True)
    (song / "a.bms").write_bytes(b"#TITLE T\n")
    (song / "real.wav").write_bytes(b"wav")
    os.symlink(outside / "secret.txt", song / "leak.ogg")
    os.symlink(outside / "secret.txt", song / "z.bms")
    os.symlink(outside / "dir", song / "bga")
    # 차트가 링크뿐인 폴더는 곡이 아닙니다.
    only_link = constant.TMP_DIR / "group" / "s2"
    only_link.mkdir()
    os.symlink(outside / "song" / "o.bms", only_link / "o.bms")
    # var/tmp 바로 아래의 폴더 링크도 따라가지 않습니다.
    os.symlink(outside / "song", constant.TMP_DIR / "linked")
    os.symlink(outside / "secret.txt", constant.TMP_DIR / "linked.zip")

    job = wait(client, client.post("/api/admin/import/tmp").json())
    assert job["songs"] == [{"folder": "group/s1", "song_id": 1, "new_song": True, "charts": 1, "new_charts": 1}]
    with zipfile.ZipFile(io.BytesIO(db_module.Database().get_song_data(1))) as zf:
        assert sorted(zf.namelist()) == ["a.bms", "real.wav"]
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("SELECT data FROM chart_chunk")
        assert all(secret not in row[0] for row in cur.fetchall())
        cur.execute("SELECT COUNT(*) FROM chart")
        assert cur.fetchone()[0] == 1
    # 링크 대상은 지우지 않습니다.
    assert (outside / "secret.txt").read_bytes() == secret
    assert (outside / "song" / "o.bms").exists()


def test_create_zip_skips_symlinks(tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_bytes(b"secret")
    song = tmp_path / "song"
    (song / "sub").mkdir(parents=True)
    (song / "a.bms").write_bytes(b"#TITLE\n")
    (song / "sub" / "x.wav").write_bytes(b"x")
    os.symlink(outside, song / "leak.ogg")
    os.symlink(outside, song / "sub" / "leak.wav")
    os.symlink(tmp_path, song / "up")
    data = db_module.Database.create_zip(None, song)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert sorted(zf.namelist()) == ["a.bms", "sub/x.wav"]
    assert [p.name for p in db_module.chart_files(song)] == ["a.bms"]
