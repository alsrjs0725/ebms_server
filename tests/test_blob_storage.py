"""MySQL BLOB 저장 왕복 테스트. EBMS_DB_* 환경변수의 서버에 ebms_test DB를 만들어 사용합니다."""
import hashlib
import io
import os
import zipfile

import pymysql
import pytest
from fastapi.testclient import TestClient

from ebms_server import constant, db as db_module
from ebms_server.db import Database

TEST_DB = os.environ.get("EBMS_DB_TEST_NAME", "ebms_test")


@pytest.fixture
def database(monkeypatch):
    try:
        con = db_module.connect(database=None)
    except pymysql.err.OperationalError as e:
        pytest.skip(f"MySQL not reachable: {e}")
    with con, con.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS {TEST_DB}")
        cur.execute(f"CREATE DATABASE {TEST_DB}")
    monkeypatch.setattr(constant, "DB_NAME", TEST_DB)
    # 여러 조각으로 나눠 읽는 경로를 확인하기 위해 작게 설정
    monkeypatch.setattr(constant, "BLOB_READ_SIZE", 1000)
    Database._instance = None
    Database._initialized = False
    yield Database()
    Database._instance = None
    Database._initialized = False


@pytest.fixture
def client(database):
    from ebms_server.main import app
    return TestClient(app)


def make_song(root, name, charts):
    song = root / name
    (song / "bga").mkdir(parents=True)
    (song / "bga" / "movie.bin").write_bytes(os.urandom(50_000))
    (song / "sound.wav").write_bytes(os.urandom(5_000))
    for chart_name, body in charts.items():
        (song / chart_name).write_bytes(body)
    return song


def sha(data):
    return hashlib.sha256(data).hexdigest()


def test_insert_and_download_roundtrip(tmp_path, client):
    chart_a = b"#TITLE A\n" + os.urandom(3000)
    chart_b = b"#TITLE B\n" + os.urandom(3000)
    song = make_song(tmp_path, "song1", {"a.bms": chart_a, "b.bme": chart_b})
    Database().insert_song(song)

    res = client.get(f"/api/files/song/{sha(chart_a)}")
    assert res.status_code == 200
    assert res.headers["content-length"] == str(len(res.content))
    with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
        assert zf.read("a.bms") == chart_a
        assert zf.read("bga/movie.bin") == (song / "bga" / "movie.bin").read_bytes()
    assert client.get(f"/api/files/song/{sha(chart_b)}").content == res.content

    hashes = client.get("/api/charthash").json()
    assert list(hashes) == ["0"]
    chunk = client.get("/api/files/chart/0")
    assert chunk.status_code == 200
    assert sha(chunk.content) == hashes["0"]
    with zipfile.ZipFile(io.BytesIO(chunk.content)) as zf:
        assert zf.read("a.bms") == chart_a
        assert zf.read("b.bme") == chart_b

    # 같은 chart를 가진 곡은 기존 song에 연결되고 chunk에 중복 추가되지 않습니다.
    chart_c = b"#TITLE C\n"
    song2 = make_song(tmp_path, "song2", {"a.bms": chart_a, "c.bms": chart_c})
    Database().insert_song(song2)
    with zipfile.ZipFile(io.BytesIO(client.get("/api/files/chart/0").content)) as zf:
        assert sorted(zf.namelist()) == ["a.bms", "b.bme", "c.bms"]
    assert client.get(f"/api/files/song/{sha(chart_c)}").content == res.content


def test_chunk_rollover(tmp_path, client, monkeypatch):
    monkeypatch.setattr(constant, "BYTE_PER_CHUNK", 1000)
    Database().insert_song(make_song(tmp_path, "s1", {"a.bms": os.urandom(2000)}))
    Database().insert_song(make_song(tmp_path, "s2", {"b.bms": os.urandom(2000)}))
    assert sorted(client.get("/api/charthash").json()) == ["0", "1"]


def test_not_found(client):
    assert client.get("/api/files/song/" + "0" * 64).status_code == 404
    assert client.get("/api/files/chart/99").status_code == 404


def test_song_too_large_is_skipped(tmp_path, database, monkeypatch):
    monkeypatch.setattr(database, "max_allowed_packet", constant.PACKET_OVERHEAD + 10)
    song = make_song(tmp_path, "big", {"a.bms": b"#X"})
    database.insert_song(song, remove=True)
    assert song.exists()  # 실패 시 원본은 지우지 않습니다.
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM song")
        assert cur.fetchone()[0] == 0
