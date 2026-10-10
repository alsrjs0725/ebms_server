"""MySQL BLOB 저장 왕복 테스트. EBMS_DB_* 환경변수의 서버에 ebms_test DB를 만들어 사용합니다."""
import gzip
import hashlib
import io
import json
import os
import zipfile

import pymysql
import pytest
from fastapi.testclient import TestClient

from ebms_server import constant, db as db_module
from ebms_server.db import Database

from test_auth import oauth

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
def client(database, monkeypatch):
    """로그인한 웹 세션 쿠키를 가진 클라이언트. 다운로드 API는 모두 로그인이 필요합니다."""
    monkeypatch.setattr(constant, "PUBLIC_URL", "http://testserver")
    monkeypatch.setattr(constant, "SECRET_KEY", "test-secret")
    monkeypatch.setattr(constant, "GOOGLE_CLIENT_ID", "google-id")
    monkeypatch.setattr(constant, "GOOGLE_CLIENT_SECRET", "google-secret")
    from ebms_server.main import app
    c = TestClient(app)
    oauth(c, monkeypatch, "google", "g1")
    return c


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

    res = client.get("/api/play/song/1")
    assert res.status_code == 200
    assert res.headers["content-length"] == str(len(res.content))
    with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
        assert zf.read("a.bms") == chart_a
        assert zf.read("bga/movie.bin") == (song / "bga" / "movie.bin").read_bytes()
    assert client.get("/api/play/song/1").content == res.content

    hashes = client.get("/api/pre/charthash").json()
    assert list(hashes) == ["0"]
    chunk = client.get("/api/pre/chart/0")
    assert chunk.status_code == 200
    assert sha(chunk.content) == hashes["0"]
    with zipfile.ZipFile(io.BytesIO(chunk.content)) as zf:
        assert zf.read(f"{sha(chart_a)}.bms") == chart_a
        assert zf.read(f"{sha(chart_b)}.bme") == chart_b

    # 같은 chart를 가진 곡은 기존 song에 연결되고 chunk에 중복 추가되지 않습니다.
    # 기존 곡 zip에 없는 파일(c.bms)은 더하고, 같은 경로에 내용이 다른 파일은 기존 것을 둡니다.
    chart_c = b"#TITLE C\n"
    song2 = make_song(tmp_path, "song2", {"a.bms": chart_a, "c.bms": chart_c})
    info = Database().insert_song(song2)
    assert info["song_id"] == 1 and not info["new_song"]
    assert info["added_files"] == ["c.bms"]
    assert info["conflicts"] == ["bga/movie.bin", "sound.wav"]
    with zipfile.ZipFile(io.BytesIO(client.get("/api/pre/chart/0").content)) as zf:
        assert sorted(zf.namelist()) == sorted(
            [f"{sha(chart_a)}.bms", f"{sha(chart_b)}.bme", f"{sha(chart_c)}.bms"]
        )
    merged = client.get("/api/play/song/1").content
    with zipfile.ZipFile(io.BytesIO(merged)) as zf, zipfile.ZipFile(io.BytesIO(res.content)) as old:
        assert sorted(zf.namelist()) == sorted(old.namelist() + ["c.bms"])
        assert zf.read("c.bms") == chart_c
        for name in old.namelist():
            assert zf.read(name) == old.read(name)


def test_chunk_rollover(tmp_path, client, monkeypatch):
    monkeypatch.setattr(constant, "BYTE_PER_CHUNK", 1000)
    Database().insert_song(make_song(tmp_path, "s1", {"a.bms": os.urandom(2000)}))
    Database().insert_song(make_song(tmp_path, "s2", {"b.bms": os.urandom(2000)}))
    assert sorted(client.get("/api/pre/charthash").json()) == ["0", "1"]


def test_not_found(client):
    assert client.get("/api/play/song/99").status_code == 404
    assert client.get("/api/pre/chart/99").status_code == 404


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
    with zipfile.ZipFile(io.BytesIO(client.get("/api/pre/chart/0").content)) as zf:
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
    chunk = client.get("/api/pre/chart/0").content
    assert client.get("/api/pre/charthash").json() == {"0": sha(chunk)}
    with zipfile.ZipFile(io.BytesIO(chunk)) as zf:
        assert zf.read(f"{sha(chart_a)}.bms") == chart_a
        assert zf.read(f"{sha(chart_b)}.bms") == chart_b
    assert database.migrate_chart_chunk_names() == 0


def test_song_hash_headers(tmp_path, client):
    Database().insert_song(make_song(tmp_path, "s1", {"a.bms": b"#TITLE\n"}))
    by_id = client.get("/api/play/song/1")
    assert by_id.status_code == 200
    assert by_id.headers["x-content-sha256"] == sha(by_id.content)
    assert by_id.headers["etag"] == f'"{sha(by_id.content)}"'
    assert by_id.headers["accept-ranges"] == "bytes"
    assert client.get("/api/play/song/99").status_code == 404
    assert client.get("/api/play/song/abc").status_code == 422


def test_range_requests(tmp_path, client):
    Database().insert_song(make_song(tmp_path, "s1", {"a.bms": b"#X"}))
    full = client.get("/api/play/song/1").content
    size = len(full)
    etag = f'"{sha(full)}"'

    res = client.get("/api/play/song/1", headers={"Range": "bytes=10-2509"})
    assert res.status_code == 206
    assert res.content == full[10:2510]
    assert res.headers["content-range"] == f"bytes 10-2509/{size}"
    assert res.headers["content-length"] == "2500"

    res = client.get("/api/play/song/1", headers={"Range": "bytes=1500-"})
    assert res.status_code == 206 and res.content == full[1500:]
    res = client.get("/api/play/song/1", headers={"Range": "bytes=-100"})
    assert res.status_code == 206 and res.content == full[-100:]
    res = client.get("/api/play/song/1", headers={"Range": f"bytes=0-{size + 100}"})
    assert res.status_code == 206 and res.content == full

    res = client.get("/api/play/song/1", headers={"Range": f"bytes={size}-"})
    assert res.status_code == 416
    assert res.headers["content-range"] == f"bytes */{size}"

    # 여러 범위나 If-Range 불일치는 전체 응답
    assert client.get("/api/play/song/1", headers={"Range": "bytes=0-1,5-6"}).status_code == 200
    res = client.get("/api/play/song/1", headers={"Range": "bytes=0-9", "If-Range": '"other"'})
    assert res.status_code == 200 and res.content == full
    res = client.get("/api/play/song/1", headers={"Range": "bytes=0-9", "If-Range": etag})
    assert res.status_code == 206 and res.content == full[:10]

    assert client.get("/api/play/song/1", headers={"If-None-Match": etag}).status_code == 304

    # chart chunk도 같은 방식
    chunk = client.get("/api/pre/chart/0").content
    res = client.get("/api/pre/chart/0", headers={"Range": "bytes=5-20"})
    assert res.status_code == 206 and res.content == chunk[5:21]


def test_manifest(tmp_path, client):
    chart_a, chart_b = b"#A" + os.urandom(10), b"#B" + os.urandom(10)
    song = make_song(tmp_path, "Artist - 제목", {"a.bms": chart_a, "b.bme": chart_b})
    Database().insert_song(song)

    hashes = client.get("/api/pre/manifest/hash").json()
    assert list(hashes) == ["0"]
    res = client.get("/api/pre/manifest/0")
    assert res.status_code == 200
    assert res.headers["content-encoding"] == "gzip"
    assert sha(res.content) == hashes["0"]
    plain = client.get("/api/pre/manifest/0", headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in plain.headers
    assert plain.content == res.content

    [entry] = res.json()
    song_zip = client.get("/api/play/song/1").content
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
    import struct
    import zlib
    f = files["bga/movie.bin"]
    head = client.get("/api/play/song/1", headers={"Range": f"bytes={f['offset']}-{f['offset'] + 29}"}).content
    name_len, extra_len = struct.unpack("<HH", head[26:30])
    data_start = f["offset"] + 30 + name_len + extra_len
    raw = client.get(
        "/api/play/song/1", headers={"Range": f"bytes={data_start}-{data_start + f['comp_size'] - 1}"}
    ).content
    assert f["method"] == zipfile.ZIP_DEFLATED
    body = zlib.decompress(raw, -15)
    assert body == (song / "bga" / "movie.bin").read_bytes()
    assert f"{zlib.crc32(body):08x}" == f["crc32"] and len(body) == f["size"]

    # 기존 곡에 chart가 추가되면 매니페스트가 갱신됩니다.
    chart_c = b"#C"
    Database().insert_song(make_song(tmp_path, "other", {"a.bms": chart_a, "c.bms": chart_c}))
    assert client.get("/api/pre/manifest/hash").json()["0"] != hashes["0"]
    [entry] = client.get("/api/pre/manifest/0").json()
    assert sha(chart_c) in entry["charts"]
    c_file = next(f for f in entry["chart_files"] if f["sha256"] == sha(chart_c))
    assert c_file == {"sha256": sha(chart_c), "path": "c.bms", "size": len(chart_c)}

    assert client.get("/api/pre/manifest/5").status_code == 404
    assert client.get("/api/version").json()["api"] == constant.API_VERSION


def test_manifest_backfill_for_old_rows(tmp_path, client, database):
    Database().insert_song(make_song(tmp_path, "s1", {"a.bms": b"#A"}))
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("UPDATE song SET folder = '', files = NULL")
        cur.execute("UPDATE chart SET filename = ''")
        cur.execute("DELETE FROM manifest_chunk")
        con.commit()
    database.backfill_manifest()
    [entry] = client.get("/api/pre/manifest/0").json()
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


def test_song_part_chunking_and_migration(tmp_path, client, database):
    song = make_song(tmp_path, "s_part", {"a.bms": b"#TITLE\n#BANNER banner.bmp\n", "banner.bmp": os.urandom(2000)})
    database.insert_song(song)

    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("SELECT data FROM song WHERE id = 1")
        song_data = cur.fetchone()[0]
        assert song_data is None  # song.data should be NULL

        cur.execute("SELECT COUNT(*) FROM song_part WHERE song_id = 1")
        part_count = cur.fetchone()[0]
        assert part_count > 0  # Parts exist in song_part

    # Test downloading pre-file using data_offset
    res = client.get("/api/pre/song/1/file?path=banner.bmp")
    assert res.status_code == 200
    assert len(res.content) == 2000

    # Test migration of old song.data row to song_part
    raw_zip = database.get_song_data(1)
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("DELETE FROM song_part WHERE song_id = 1")
        cur.execute("UPDATE song SET data = %s WHERE id = 1", (raw_zip,))
        con.commit()

    database.backfill_manifest()

    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("SELECT data FROM song WHERE id = 1")
        assert cur.fetchone()[0] is None
        cur.execute("SELECT COUNT(*) FROM song_part WHERE song_id = 1")
        assert cur.fetchone()[0] == part_count

    res_play = client.get("/api/play/song/1")
    assert res_play.status_code == 200
    assert res_play.content == raw_zip


def test_backfill_manifest_skips_broken_song(tmp_path, database, monkeypatch, caplog):
    """손상된 곡 zip 하나가 있어도 시작(backfill)이 실패하지 않고 나머지 곡은 채웁니다 (#45)"""
    monkeypatch.setattr(constant, "BACKFILL_BATCH_SONGS", 2)
    for i in range(3):
        database.insert_song(make_song(tmp_path, f"s{i}", {"a.bms": f"#TITLE {i}\n".encode()}))
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("UPDATE song SET files = NULL")
        cur.execute("UPDATE chart SET filename = ''")
        cur.execute("UPDATE song_part SET data = %s WHERE song_id = 2", (b"broken" * 10,))
        con.commit()

    Database._instance = None
    Database._initialized = False
    with caplog.at_level("ERROR"):
        Database()
    assert "Skipped song[2]" in caplog.text

    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("SELECT id, files IS NULL FROM song ORDER BY id")
        assert [tuple(row) for row in cur.fetchall()] == [(1, 0), (2, 1), (3, 0)]
        cur.execute("SELECT song_id, filename FROM chart ORDER BY song_id")
        assert [tuple(row) for row in cur.fetchall()] == [(1, "a.bms"), (2, ""), (3, "a.bms")]
    manifest = json.loads(gzip.decompress(Database().get_manifest_chunk(0)))
    assert [len(song["files"]) > 0 for song in manifest] == [True, False, True]


def test_blob_reader_does_not_hold_db_connection(tmp_path, client, database):
    """BlobReader가 스트리밍 중에 DB 연결을 계속 열어두지 않는지 확인합니다."""
    Database().insert_song(make_song(tmp_path, "s1", {"a.bms": b"#TITLE\n"}))
    blob = Database().open_blob("song", 1)
    assert blob is not None

    # iter_range 조각을 가져온 후에도 open_blob이 생성했던 연결이 남아있지 않음
    chunks = list(blob.iter_range(0, blob.size - 1))
    assert len(b"".join(chunks)) == blob.size


def test_pre_chunk(tmp_path, client, database, monkeypatch):
    monkeypatch.setattr(constant, "SONGS_PER_PRE_CHUNK", 2)
    chart = b"#TITLE\n#BANNER banner.png\n#PREVIEW pv.wav\n"
    song = make_song(tmp_path, "s1", {"a.bms": chart})
    banner, preview = os.urandom(3000), b"RIFF" * 500
    (song / "banner.png").write_bytes(banner)
    (song / "pv.ogg").write_bytes(preview)
    (song / "preview_x.ogg").write_bytes(b"pv2")
    database.insert_song(song)

    hashes = client.get("/api/pre/assethash").json()
    assert list(hashes) == ["0"]
    res = client.get("/api/pre/asset/0")
    assert res.status_code == 200
    assert sha(res.content) == hashes["0"] == res.headers["x-content-sha256"]
    with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
        # 차트와 플레이 파일(sound.wav, bga)은 빠지고, 확장자가 달라도 #PREVIEW가 가리키는 파일은 들어갑니다.
        assert sorted(zf.namelist()) == ["1/banner.png", "1/preview_x.ogg", "1/pv.ogg"]
        assert all(i.compress_type == zipfile.ZIP_STORED for i in zf.infolist())
        assert zf.read("1/banner.png") == banner
        assert zf.read("1/pv.ogg") == preview

    # 다음 구간(song 2, 3)은 새 청크이고, 같은 구간에 곡이 추가되면 그 청크만 바뀝니다.
    database.insert_song(make_song(tmp_path, "s2", {"b.bms": b"#B"}))
    after = client.get("/api/pre/assethash").json()
    assert sorted(after) == ["0", "1"] and after["0"] == hashes["0"]
    with zipfile.ZipFile(io.BytesIO(client.get("/api/pre/asset/1").content)) as zf:
        assert zf.namelist() == []
    s3 = make_song(tmp_path, "s3", {"c.bms": b"#C"})
    (s3 / "preview.ogg").write_bytes(b"p3")
    database.insert_song(s3)
    last = client.get("/api/pre/assethash").json()
    assert last["0"] == hashes["0"] and last["1"] != after["1"]
    with zipfile.ZipFile(io.BytesIO(client.get("/api/pre/asset/1").content)) as zf:
        assert zf.namelist() == ["3/preview.ogg"]

    # Range 이어받기
    part = client.get("/api/pre/asset/0", headers={"Range": "bytes=10-"})
    assert part.status_code == 206
    assert part.content == client.get("/api/pre/asset/0").content[10:]
    assert client.get("/api/pre/asset/9").status_code == 404


def test_pre_chunk_backfill(tmp_path, client, database):
    song = make_song(tmp_path, "s1", {"a.bms": b"#TITLE\n#STAGEFILE st.bmp\n"})
    (song / "st.bmp").write_bytes(os.urandom(5000))
    database.insert_song(song)
    built = client.get("/api/pre/asset/0").content
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("DELETE FROM pre_chunk_part")
        cur.execute("DELETE FROM pre_chunk")
        con.commit()
    assert client.get("/api/pre/assethash").json() == {}

    assert database.backfill_pre_chunks() == 1
    assert client.get("/api/pre/asset/0").content == built
    assert database.backfill_pre_chunks() == 0


def test_pre_chunk_is_metered(tmp_path, client, database):
    song = make_song(tmp_path, "s1", {"a.bms": b"#TITLE\n"})
    (song / "preview.ogg").write_bytes(os.urandom(4000))
    database.insert_song(song)
    before = client.get("/api/me").json()["pre"]["used_bytes"]
    body = client.get("/api/pre/asset/0").content
    assert client.get("/api/me").json()["pre"]["used_bytes"] == before + len(body)
