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
        assert zf.read(f"{sha(chart_a)}.bms") == chart_a
        assert zf.read(f"{sha(chart_b)}.bme") == chart_b

    # 같은 chart를 가진 곡은 기존 song에 연결되고 chunk에 중복 추가되지 않습니다.
    chart_c = b"#TITLE C\n"
    song2 = make_song(tmp_path, "song2", {"a.bms": chart_a, "c.bms": chart_c})
    Database().insert_song(song2)
    with zipfile.ZipFile(io.BytesIO(client.get("/api/files/chart/0").content)) as zf:
        assert sorted(zf.namelist()) == sorted(
            [f"{sha(chart_a)}.bms", f"{sha(chart_b)}.bme", f"{sha(chart_c)}.bms"]
        )
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


def test_same_chart_name_in_different_songs(tmp_path, client):
    chart_a = b"#TITLE A\n" + os.urandom(100)
    chart_b = b"#TITLE B\n" + os.urandom(100)
    Database().insert_song(make_song(tmp_path, "s1", {"normal.bms": chart_a}))
    Database().insert_song(make_song(tmp_path, "s2", {"NORMAL.BMS": chart_b}))
    with zipfile.ZipFile(io.BytesIO(client.get("/api/files/chart/0").content)) as zf:
        assert sorted(zf.namelist()) == sorted([f"{sha(chart_a)}.bms", f"{sha(chart_b)}.bms"])


def test_migrate_chart_chunk_names(tmp_path, client, database):
    # 이전 버전처럼 원래 파일명으로 저장된(이름이 겹치는) chunk
    chart_a, chart_b = b"#A" + os.urandom(100), b"#B" + os.urandom(100)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("normal.bms", chart_a)
        zf.writestr("normal.bms", chart_b)
    old = buf.getvalue()
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute(
            "INSERT INTO chart_chunk (id, size, sha256, data) VALUES (0, %s, %s, %s)", (len(old), sha(old), old)
        )
        con.commit()

    assert database.migrate_chart_chunk_names() == 1
    chunk = client.get("/api/files/chart/0").content
    assert client.get("/api/charthash").json() == {"0": sha(chunk)}
    with zipfile.ZipFile(io.BytesIO(chunk)) as zf:
        assert zf.read(f"{sha(chart_a)}.bms") == chart_a
        assert zf.read(f"{sha(chart_b)}.bms") == chart_b
    assert database.migrate_chart_chunk_names() == 0


def test_song_by_id_and_hash_headers(tmp_path, client):
    chart = b"#TITLE\n"
    Database().insert_song(make_song(tmp_path, "s1", {"a.bms": chart}))
    by_chart = client.get(f"/api/files/song/{sha(chart)}")
    by_id = client.get("/api/files/song/id/1")
    assert by_id.status_code == 200
    assert by_id.content == by_chart.content
    assert by_id.headers["x-content-sha256"] == sha(by_id.content)
    assert by_id.headers["etag"] == f'"{sha(by_id.content)}"'
    assert by_id.headers["accept-ranges"] == "bytes"
    assert client.get("/api/files/song/id/99").status_code == 404
    assert client.get("/api/files/song/id/abc").status_code == 422


def test_range_requests(tmp_path, client):
    Database().insert_song(make_song(tmp_path, "s1", {"a.bms": b"#X"}))
    full = client.get("/api/files/song/id/1").content
    size = len(full)
    etag = f'"{sha(full)}"'

    res = client.get("/api/files/song/id/1", headers={"Range": "bytes=10-2509"})
    assert res.status_code == 206
    assert res.content == full[10:2510]
    assert res.headers["content-range"] == f"bytes 10-2509/{size}"
    assert res.headers["content-length"] == "2500"

    res = client.get("/api/files/song/id/1", headers={"Range": "bytes=1500-"})
    assert res.status_code == 206 and res.content == full[1500:]
    res = client.get("/api/files/song/id/1", headers={"Range": "bytes=-100"})
    assert res.status_code == 206 and res.content == full[-100:]
    res = client.get("/api/files/song/id/1", headers={"Range": f"bytes=0-{size + 100}"})
    assert res.status_code == 206 and res.content == full

    res = client.get("/api/files/song/id/1", headers={"Range": f"bytes={size}-"})
    assert res.status_code == 416
    assert res.headers["content-range"] == f"bytes */{size}"

    # 여러 범위나 If-Range 불일치는 전체 응답
    assert client.get("/api/files/song/id/1", headers={"Range": "bytes=0-1,5-6"}).status_code == 200
    res = client.get("/api/files/song/id/1", headers={"Range": "bytes=0-9", "If-Range": '"other"'})
    assert res.status_code == 200 and res.content == full
    res = client.get("/api/files/song/id/1", headers={"Range": "bytes=0-9", "If-Range": etag})
    assert res.status_code == 206 and res.content == full[:10]

    assert client.get("/api/files/song/id/1", headers={"If-None-Match": etag}).status_code == 304

    # chart chunk도 같은 방식
    chunk = client.get("/api/files/chart/0").content
    res = client.get("/api/files/chart/0", headers={"Range": "bytes=5-20"})
    assert res.status_code == 206 and res.content == chunk[5:21]


def test_manifest(tmp_path, client):
    chart_a, chart_b = b"#A" + os.urandom(10), b"#B" + os.urandom(10)
    song = make_song(tmp_path, "Artist - 제목", {"a.bms": chart_a, "b.bme": chart_b})
    Database().insert_song(song)

    hashes = client.get("/api/manifest/hash").json()
    assert list(hashes) == ["0"]
    res = client.get("/api/manifest/0")
    assert res.status_code == 200
    assert res.headers["content-encoding"] == "gzip"
    assert sha(res.content) == hashes["0"]
    plain = client.get("/api/manifest/0", headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in plain.headers
    assert plain.content == res.content

    [entry] = res.json()
    song_zip = client.get("/api/files/song/id/1").content
    assert entry["song_id"] == 1
    assert entry["folder"] == "Artist - 제목"
    assert entry["zip_size"] == len(song_zip)
    assert entry["zip_sha256"] == sha(song_zip)
    assert sorted(entry["charts"]) == sorted([sha(chart_a), sha(chart_b)])
    chart_files = {f["sha256"]: f for f in entry["chart_files"]}
    assert chart_files[sha(chart_a)] == {"sha256": sha(chart_a), "path": "a.bms", "size": len(chart_a)}
    assert chart_files[sha(chart_b)] == {"sha256": sha(chart_b), "path": "b.bme", "size": len(chart_b)}
    files = {f["path"]: f for f in entry["files"]}
    assert sorted(files) == ["a.bms", "b.bme", "bga/movie.bin", "sound.wav"]

    # offset으로 Range 요청해 파일 하나만 꺼낼 수 있어야 합니다.
    import struct, zlib
    f = files["bga/movie.bin"]
    head = client.get("/api/files/song/id/1", headers={"Range": f"bytes={f['offset']}-{f['offset'] + 29}"}).content
    name_len, extra_len = struct.unpack("<HH", head[26:30])
    data_start = f["offset"] + 30 + name_len + extra_len
    raw = client.get(
        "/api/files/song/id/1", headers={"Range": f"bytes={data_start}-{data_start + f['comp_size'] - 1}"}
    ).content
    assert f["method"] == zipfile.ZIP_DEFLATED
    body = zlib.decompress(raw, -15)
    assert body == (song / "bga" / "movie.bin").read_bytes()
    assert f"{zlib.crc32(body):08x}" == f["crc32"] and len(body) == f["size"]

    # 기존 곡에 chart가 추가되면 매니페스트가 갱신됩니다.
    chart_c = b"#C"
    Database().insert_song(make_song(tmp_path, "other", {"a.bms": chart_a, "c.bms": chart_c}))
    assert client.get("/api/manifest/hash").json()["0"] != hashes["0"]
    [entry] = client.get("/api/manifest/0").json()
    assert sha(chart_c) in entry["charts"]
    c_file = next(f for f in entry["chart_files"] if f["sha256"] == sha(chart_c))
    assert c_file == {"sha256": sha(chart_c), "path": "c.bms", "size": len(chart_c)}

    assert client.get("/api/manifest/5").status_code == 404
    assert client.get("/api/version").json()["api"] == constant.API_VERSION


def test_manifest_backfill_for_old_rows(tmp_path, client, database):
    Database().insert_song(make_song(tmp_path, "s1", {"a.bms": b"#A"}))
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("UPDATE song SET folder = '', files = NULL")
        cur.execute("UPDATE chart SET filename = ''")
        cur.execute("DELETE FROM manifest_chunk")
        con.commit()
    database.backfill_manifest()
    [entry] = client.get("/api/manifest/0").json()
    assert entry["folder"] == "1"
    assert "sound.wav" in [f["path"] for f in entry["files"]]
    assert entry["chart_files"][0]["path"] == "a.bms"


def test_add_missing_columns(database):
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("ALTER TABLE song DROP COLUMN folder, DROP COLUMN files")
        con.commit()
    database.generate_database()
    database.backfill_manifest()
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("SELECT folder, files FROM song")
        assert cur.fetchall() == ()

def test_parse_range():
    from ebms_server.main import parse_range

    # Happy paths
    assert parse_range("bytes=10-20", 100) == (10, 20)
    assert parse_range("bytes=10-", 100) == (10, 99)
    assert parse_range("bytes=-20", 100) == (80, 99)
    assert parse_range("bytes=0-0", 100) == (0, 0)
    assert parse_range("bytes=99-99", 100) == (99, 99)

    # Truncated ranges
    assert parse_range("bytes=10-200", 100) == (10, 99)
    assert parse_range("bytes=-200", 100) == (0, 99)

    # Invalid ranges that fall back to full file
    assert parse_range(None, 100) is None
    assert parse_range("", 100) is None
    assert parse_range("invalid", 100) is None
    assert parse_range("bytes=a-b", 100) is None
    assert parse_range("bytes=10-20,30-40", 100) is None
    assert parse_range("bytes=-", 100) is None

    # Error paths (unsatisfiable ranges)
    with pytest.raises(ValueError):
        parse_range("bytes=-0", 100)

    with pytest.raises(ValueError):
        parse_range("bytes=100-", 100)

    with pytest.raises(ValueError):
        parse_range("bytes=101-110", 100)

    with pytest.raises(ValueError):
        parse_range("bytes=20-10", 100)
