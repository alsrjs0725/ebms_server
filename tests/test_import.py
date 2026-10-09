"""관리자 곡 임포트(zip 업로드, var/tmp 가져오기) 테스트."""
import io
import os
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


def upload(client, *files):
    return client.post(
        "/api/admin/import",
        files=[("files", (name, data, "application/zip")) for name, data in files],
    )


def song_rows():
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("SELECT id, folder FROM song ORDER BY id")
        return cur.fetchall()


def test_import_requires_admin(client, monkeypatch):
    assert client.get("/admin/import", follow_redirects=False).status_code == 303
    assert upload(client, ("a.zip", make_zip({"a.bms": b"#X"}))).status_code == 401
    client_login(client, monkeypatch)
    assert client.get("/admin/import").status_code == 403
    assert upload(client, ("a.zip", make_zip({"a.bms": b"#X"}))).status_code == 403
    assert client.post("/api/admin/import/tmp").status_code == 403


def test_import_zip(client, monkeypatch):
    admin_login(client, monkeypatch)
    assert client.get("/admin/import").status_code == 200

    # 최상위에 차트가 있는 zip, 곡 폴더 두 개가 든 zip(cp932 이름), 깨진 파일, 곡이 없는 zip
    flat = make_zip({"a.bms": b"#TITLE A\n", "bga/movie.bin": os.urandom(1000)})
    multi = make_zip({
        "pack/曲1/b.bme": b"#TITLE B\n",
        "pack/曲1/音.wav": os.urandom(500),
        "pack/曲2/c.bms": b"#TITLE C\n",
        "pack/曲2/c2.bms": b"#TITLE A\n",
        "../evil.bms": b"#EVIL\n",
    }, cp932=True)
    r = upload(client, ("flat.zip", flat), ("multi.zip", multi), ("bad.zip", b"not a zip"), ("empty.zip", make_zip({"readme.txt": b"hi"})))
    assert r.status_code == 200, r.text
    flat_r, multi_r, bad_r, empty_r = r.json()

    assert flat_r["error"] is None
    assert flat_r["songs"] == [{"folder": "flat", "song_id": 1, "new_song": True, "charts": 1, "new_charts": 1}]
    assert multi_r["error"] is None
    assert multi_r["songs"] == [
        {"folder": "multi/pack/曲1", "song_id": 2, "new_song": True, "charts": 1, "new_charts": 1},
        # 같은 차트(a.bms)가 있어 기존 곡 1에 연결됩니다.
        {"folder": "multi/pack/曲2", "song_id": 1, "new_song": False, "charts": 2, "new_charts": 1},
    ]
    assert bad_r["error"] and bad_r["songs"] == []
    assert empty_r["error"] and empty_r["songs"] == []

    assert [tuple(row) for row in song_rows()] == [(1, "flat"), (2, "曲1")]
    with zipfile.ZipFile(io.BytesIO(db_module.Database().get_song_data(2))) as zf:
        assert sorted(zf.namelist()) == ["b.bme", "音.wav"]
    # 임시 폴더는 남기지 않습니다.
    assert not any(constant.IMPORT_DIR.iterdir())


def test_import_tmp(client, monkeypatch):
    admin_login(client, monkeypatch)
    song = constant.TMP_DIR / "group" / "s1"
    song.mkdir(parents=True)
    (song / "a.bms").write_bytes(b"#TITLE T\n")
    (constant.TMP_DIR / "loose.bms").write_bytes(b"#LOOSE\n")  # var/tmp 바로 아래 파일은 보지 않습니다.

    r = client.post("/api/admin/import/tmp")
    assert r.status_code == 200
    assert r.json() == [{"folder": "group/s1", "song_id": 1, "new_song": True, "charts": 1, "new_charts": 1}]
    assert not song.exists()
    assert (constant.TMP_DIR / "loose.bms").exists()
    assert client.post("/api/admin/import/tmp").json() == []


def test_safe_parts():
    assert importer._safe_parts("a/b.bms") == ["a", "b.bms"]
    assert importer._safe_parts("a\\.\\b.bms") == ["a", "b.bms"]
    for bad in ("/etc/x", "../x", "a/../../x", "C:/x", "C:x"):
        assert importer._safe_parts(bad) is None, bad
