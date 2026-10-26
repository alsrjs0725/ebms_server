"""MySQL BLOB 저장 왕복 테스트. EBMS_DB_* 환경변수의 서버에 ebms_test DB를 만들어 사용합니다."""
import gzip
import hashlib
import io
import json
import os
import threading
import zipfile

import pymysql
import pytest
from fastapi.testclient import TestClient

from ebms_server import constant, db as db_module, preview, store
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
    chart_a = b"#TITLE A\n" + os.urandom(3000).hex().encode()
    chart_b = b"#TITLE B\n" + os.urandom(3000).hex().encode()
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
    Database().insert_song(make_song(tmp_path, "s1", {"a.bms": os.urandom(2000).hex().encode()}))
    Database().insert_song(make_song(tmp_path, "s2", {"b.bms": os.urandom(2000).hex().encode()}))
    assert sorted(client.get("/api/pre/charthash").json()) == ["0", "1"]


def test_not_found(client):
    assert client.get("/api/play/song/99").status_code == 404
    assert client.get("/api/pre/chart/99").status_code == 404


def test_same_chart_name_in_different_songs(tmp_path, client):
    chart_a = b"#TITLE A\n" + os.urandom(100).hex().encode()
    chart_b = b"#TITLE B\n" + os.urandom(100).hex().encode()
    Database().insert_song(make_song(tmp_path, "s1", {"normal.bms": chart_a}))
    Database().insert_song(make_song(tmp_path, "s2", {"NORMAL.BMS": chart_b}))
    with zipfile.ZipFile(io.BytesIO(client.get("/api/pre/chart/0").content)) as zf:
        assert sorted(zf.namelist()) == sorted([f"{sha(chart_a)}.bms", f"{sha(chart_b)}.bms"])


def test_migrate_chart_chunk_names(tmp_path, client, database):
    # 이전 버전처럼 원래 파일명으로 저장된(이름이 겹치는) chunk
    chart_a, chart_b = b"#A" + os.urandom(100).hex().encode(), b"#B" + os.urandom(100).hex().encode()
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
    chart_a, chart_b = b"#A" + os.urandom(10).hex().encode(), b"#B" + os.urandom(10).hex().encode()
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


def to_legacy(table, row_id):
    """저장소로 옮기기 전(DB BLOB) 행으로 되돌립니다. 저장소 파일은 지웁니다."""
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute(f"SELECT path FROM {table} WHERE id = %s", (row_id,))
        rel = cur.fetchone()[0]
        data = store.read(rel)
        step = constant.BLOB_READ_SIZE
        if table == "song":
            for seq, pos in enumerate(range(0, len(data), step)):
                cur.execute("INSERT INTO song_part (song_id, seq, data) VALUES (%s, %s, %s)", (row_id, seq, data[pos : pos + step]))
        elif table == "chart_chunk":
            cur.execute("UPDATE chart_chunk SET data = %s WHERE id = %s", (data, row_id))
        else:
            for seq, pos in enumerate(range(0, len(data), step)):
                cur.execute("INSERT INTO pre_chunk_part (chunk_id, seq, data) VALUES (%s, %s, %s)", (row_id, seq, data[pos : pos + step]))
        cur.execute(f"UPDATE {table} SET path = NULL WHERE id = %s", (row_id,))
        con.commit()
    store.full_path(rel).unlink()
    return data


def count(sql):
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute(sql)
        return cur.fetchone()[0]


def test_song_is_stored_as_file(tmp_path, client, database):
    song = make_song(tmp_path, "s_part", {"a.bms": b"#TITLE\n#BANNER banner.bmp\n", "banner.bmp": os.urandom(2000)})
    database.insert_song(song)
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("SELECT data, path, sha256 FROM song WHERE id = 1")
        data, rel, sha256 = cur.fetchone()
    assert data is None
    assert rel == store.song_path(1, sha256)
    assert hashlib.sha256(store.read(rel)).hexdigest() == sha256
    assert count("SELECT COUNT(*) FROM song_part") == 0
    assert count("SELECT COUNT(*) FROM pre_chunk_part") == 0
    assert count("SELECT COUNT(*) FROM chart_chunk WHERE path IS NULL") == 0

    res = client.get("/api/pre/song/1/file?path=banner.bmp")
    assert res.status_code == 200 and len(res.content) == 2000
    res = client.get("/api/play/song/1", headers={"Range": "bytes=10-19"})
    assert res.status_code == 206 and res.content == store.read(rel)[10:20]


def test_migrate_to_store(tmp_path, client, database, monkeypatch):
    """DB BLOB에 남은 곡·청크를 저장소 파일로 옮기고, 다 옮긴 테이블의 BLOB을 비웁니다."""
    monkeypatch.setattr(constant, "STORE_MIGRATE_REST_MIN", 0)
    monkeypatch.setattr(constant, "STORE_MIN_FREE_BYTES", 0)
    database.insert_song(make_song(tmp_path, "s1", {"a.bms": b"#TITLE A\n#BANNER b.bmp\n", "b.bmp": os.urandom(3000)}))
    database.insert_song(make_song(tmp_path, "s2", {"b.bms": b"#TITLE B\n"}))
    raw = {1: to_legacy("song", 1), 2: to_legacy("song", 2)}
    chart = to_legacy("chart_chunk", 0)
    pre = to_legacy("pre_chunk", 0)
    # 아주 오래된 song.data 행
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("DELETE FROM song_part WHERE song_id = 2")
        cur.execute("UPDATE song SET data = %s WHERE id = 2", (raw[2],))
        con.commit()

    # 옮기기 전에도 DB BLOB에서 받습니다.
    assert client.get("/api/play/song/1").content == raw[1]
    assert client.get("/api/play/song/2").content == raw[2]
    assert client.get("/api/pre/chart/0").content == chart
    assert client.get("/api/pre/asset/0").content == pre
    assert database.get_chart_chunk_data(0) == chart

    assert database.migrate_to_store() == 4
    for table in ("song", "chart_chunk", "pre_chunk"):
        assert count(f"SELECT COUNT(*) FROM {table} WHERE path IS NULL") == 0
    assert count("SELECT COUNT(*) FROM song_part") == 0
    assert count("SELECT COUNT(*) FROM pre_chunk_part") == 0
    assert count("SELECT COUNT(*) FROM song WHERE data IS NOT NULL") == 0
    assert count("SELECT LENGTH(data) FROM chart_chunk WHERE id = 0") == 0
    assert setting(db_module.STORE_RECLAIMED_SETTING) == "chart_chunk,pre_chunk,song"
    assert client.get("/api/play/song/1").content == raw[1]
    assert client.get("/api/play/song/2").content == raw[2]
    assert client.get("/api/pre/chart/0").content == chart
    assert client.get("/api/pre/asset/0").content == pre
    # 다시 돌려도 할 일이 없습니다.
    assert database.migrate_to_store() == 0


def setting(name):
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("SELECT value FROM setting WHERE name = %s", (name,))
        row = cur.fetchone()
    return row[0] if row else None


def test_migrate_to_store_waits_for_disk_space(tmp_path, database, monkeypatch):
    """저장소 디스크가 모자라면 옮기지 않고 기다립니다(stop으로 멈춤)."""
    database.insert_song(make_song(tmp_path, "s1", {"a.bms": b"#TITLE A\n"}))
    to_legacy("song", 1)
    monkeypatch.setattr(constant, "STORE_MIN_FREE_BYTES", 10 ** 18)
    stop = threading.Event()
    monkeypatch.setattr(constant, "STORE_MIGRATE_WAIT", 0)
    calls = []

    def free_bytes():
        calls.append(1)
        if len(calls) >= 3:
            stop.set()
        return 0

    monkeypatch.setattr(store, "free_bytes", free_bytes)
    assert database.migrate_to_store(stop) == 0
    assert count("SELECT COUNT(*) FROM song WHERE path IS NULL") == 1
    assert count("SELECT COUNT(*) FROM song_part") > 0


def test_store_gc(tmp_path, database, monkeypatch):
    """아무 행도 가리키지 않는 파일(바뀌기 전 판, 지운 곡, 임시 파일)만 지웁니다. 최근 파일·hold한 파일은 둡니다."""
    database.insert_song(make_song(tmp_path, "s1", {"a.bms": b"#TITLE A\n"}))
    database.insert_song(make_song(tmp_path, "s2", {"b.bms": b"#TITLE B\n"}))
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("SELECT path FROM song ORDER BY id")
        used = [row[0] for row in cur.fetchall()]
    old = store.write(store.song_path(1, "0" * 64), b"old")
    held = store.write(store.song_path(2, "1" * 64), b"held")
    (store.full_path(old).parent / f"x{store.TMP_MARK}abc").write_bytes(b"tmp")

    # 방금 쓴 파일은 지우지 않습니다.
    assert database.store_gc() == 0
    monkeypatch.setattr(constant, "STORE_GC_GRACE_SECONDS", -10)
    with store.hold(held):
        # old, 임시 파일, 두 번째 곡을 넣을 때 바뀐 차트 청크의 옛 판
        assert database.store_gc() == 3
        assert store.full_path(held).exists()
    assert not store.full_path(old).exists()
    assert not list(store.full_path(old).parent.glob(f"*{store.TMP_MARK}*"))
    with db_module.connect() as con, con.cursor() as cur:
        for table in ("song", "chart_chunk", "pre_chunk"):
            cur.execute(f"SELECT path FROM {table}")
            used += [row[0] for row in cur.fetchall()]
    assert all(store.full_path(rel).exists() for rel in used)
    assert database.store_gc() == 1
    assert not store.full_path(held).exists()


def test_move_to_store_rewrites_file_removed_by_gc(tmp_path, database, monkeypatch):
    """이전 실행이 남긴 같은 이름의 파일을 보고 쓰기를 건너뛴 사이 store_gc가 그 파일을 지워도, 커밋 전에 다시 씁니다(#158)."""
    database.insert_song(make_song(tmp_path, "s1", {"a.bms": b"#TITLE A\n", "b.bmp": os.urandom(3000)}))
    data = to_legacy("song", 1)
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("SELECT sha256 FROM song WHERE id = 1")
        rel = store.song_path(1, cur.fetchone()[0])
    store.write(rel, data)  # 커밋 전에 꺼진 이전 실행이 남긴 파일
    write = store.write

    def write_then_gc(path, content):
        result = write(path, content)
        # hold하기 전에 held를 본 store_gc가 이 파일을 지운 경우
        store.full_path(path).unlink()
        monkeypatch.setattr(store, "write", write)
        return result

    monkeypatch.setattr(store, "write", write_then_gc)
    assert database._move_to_store("song", 1)
    assert store.read(rel) == data
    assert count("SELECT COUNT(*) FROM song_part") == 0


def test_open_blob_keeps_reading_replaced_file(tmp_path, database, monkeypatch):
    """받는 중에 새 판으로 바뀌어 옛 파일이 지워져도 열어 둔 파일로 끝까지 읽습니다."""
    database.insert_song(make_song(tmp_path, "s1", {"a.bms": b"#TITLE A\n"}))
    blob = database.open_blob("song", 1)
    data = store.read(blob.path)
    store.full_path(blob.path).unlink()
    assert b"".join(blob.iter_range(0, blob.size - 1)) == data
    blob.close()


@pytest.mark.parametrize("table, row_id", [("song", 1), ("song", "legacy"), ("pre_chunk", 0), ("chart_chunk", 0)])
def test_blob_reader_continues_after_migration(tmp_path, database, monkeypatch, table, row_id):
    """옮기기 전 BLOB을 받는 도중 마이그레이션이 그 행을 저장소 파일로 옮겨도 파일에서 이어 끝까지 읽습니다."""
    monkeypatch.setattr(constant, "BLOB_READ_SIZE", 1000)
    monkeypatch.setattr(constant, "STORE_MIGRATE_REST_MIN", 0)
    monkeypatch.setattr(constant, "STORE_MIN_FREE_BYTES", 0)
    chart = b"#TITLE A\n#BANNER b.bmp\n#COMMENT " + os.urandom(3000).hex().encode() + b"\n"
    database.insert_song(make_song(tmp_path, "s1", {"a.bms": chart, "b.bmp": os.urandom(5000)}))
    if row_id == "legacy":
        # 아주 오래된 song.data 행
        row_id = 1
        data = to_legacy("song", 1)
        with db_module.connect() as con, con.cursor() as cur:
            cur.execute("DELETE FROM song_part WHERE song_id = 1")
            cur.execute("UPDATE song SET data = %s WHERE id = 1", (data,))
            con.commit()
    else:
        data = to_legacy(table, row_id)
    blob = database.open_blob(table, row_id)
    assert blob.path is None
    pieces = blob.iter_range(0, blob.size - 1)
    if table == "chart_chunk":
        # 차트 청크는 첫 조각에서 범위를 한 번에 읽으므로 읽기 전에 옮겨진 경우만 있습니다.
        assert database.migrate_to_store() >= 1
        first = next(pieces)
    else:
        first = next(pieces)
        assert database.migrate_to_store() >= 1
    assert first + b"".join(pieces) == data
    assert blob.path is not None
    blob.close()


def test_blob_reader_fails_when_content_changed(tmp_path, database, monkeypatch):
    """읽던 BLOB이 사라지고 다른 내용으로 바뀌었으면 이어 붙이지 않고 실패합니다."""
    monkeypatch.setattr(constant, "BLOB_READ_SIZE", 1000)
    database.insert_song(make_song(tmp_path, "s1", {"a.bms": b"#TITLE A\n", "b.bmp": os.urandom(5000)}))
    to_legacy("song", 1)
    blob = database.open_blob("song", 1)
    pieces = blob.iter_range(0, blob.size - 1)
    next(pieces)
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("DELETE FROM song_part WHERE song_id = 1")
        cur.execute("UPDATE song SET sha256 = %s, path = %s WHERE id = 1", ("0" * 64, "x"))
        con.commit()
    with pytest.raises(RuntimeError):
        b"".join(pieces)


def test_blob_reader_read_after_migration(tmp_path, database, monkeypatch):
    """read()도 옮겨진 뒤에는 저장소 파일에서 읽습니다."""
    monkeypatch.setattr(constant, "STORE_MIGRATE_REST_MIN", 0)
    monkeypatch.setattr(constant, "STORE_MIN_FREE_BYTES", 0)
    database.insert_song(make_song(tmp_path, "s1", {"a.bms": b"#TITLE A\n", "b.bmp": os.urandom(5000)}))
    data = to_legacy("song", 1)
    blob = database.open_blob("song", 1)
    assert blob.read(0, 10) == data[:10]
    database.migrate_to_store()
    assert blob.read(100, 50) == data[100:150]
    blob.close()


def test_backfill_manifest_skips_broken_song(tmp_path, database, monkeypatch, caplog):
    """손상된 곡 zip 하나가 있어도 시작(backfill)이 실패하지 않고 나머지 곡은 채웁니다 (#45)"""
    monkeypatch.setattr(constant, "BACKFILL_BATCH_SONGS", 2)
    for i in range(3):
        database.insert_song(make_song(tmp_path, f"s{i}", {"a.bms": f"#TITLE {i}\n".encode()}))
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("UPDATE song SET files = NULL")
        cur.execute("UPDATE chart SET filename = ''")
        cur.execute("SELECT path FROM song WHERE id = 2")
        store.full_path(cur.fetchone()[0]).write_bytes(b"broken" * 10)
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



def test_chart_chunk_range_reads_blob_once(tmp_path, client, database, monkeypatch):
    """옮기기 전 차트 청크는 범위를 한 번에 읽어 BLOB_READ_SIZE 조각으로 나눠 보냅니다."""
    monkeypatch.setattr(constant, "BLOB_READ_SIZE", 7)
    Database().insert_song(make_song(tmp_path, "s1", {"a.bms": b"#TITLE " + os.urandom(100).hex().encode()}))
    data = to_legacy("chart_chunk", 0)
    blob = Database().open_blob("chart_chunk", 0)
    assert blob.size == len(data)

    queries = []
    real_connect = db_module.connect

    def counting_connect(*args, **kwargs):
        queries.append(1)
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(db_module, "connect", counting_connect)
    parts = list(blob.iter_range(3, 60))
    assert b"".join(parts) == data[3:61]
    assert [len(p) for p in parts] == [7] * 8 + [2]
    assert len(queries) == 1

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


def test_read_ahead_reads_before_consumer_asks():
    """ReadAhead는 소비자가 꺼내기 전에 상한까지 미리 읽고, 순서대로 내려 줍니다."""
    import threading
    import time

    from ebms_server.downloads import ReadAhead

    read = []
    third = threading.Event()

    def source():
        for i in range(5):
            read.append(i)
            if i == 2:
                third.set()
            yield bytes([i])

    ahead = ReadAhead(source(), max_chunks=3)
    assert third.wait(2)
    time.sleep(0.05)
    # 큐 3개 + 넣으려고 기다리는 1개까지만 읽습니다.
    assert len(read) == 4
    assert list(ahead) == [bytes([i]) for i in range(5)]


def test_read_ahead_raises_source_error():
    from ebms_server.downloads import ReadAhead

    def source():
        yield b"a"
        raise RuntimeError("db down")

    ahead = ReadAhead(source())
    assert next(ahead) == b"a"
    with pytest.raises(RuntimeError):
        next(ahead)


def test_read_ahead_close_stops_reading_and_closes_source():
    import threading

    from ebms_server.downloads import ReadAhead

    closed = threading.Event()

    def source():
        try:
            for i in range(1000):
                yield bytes([i % 256])
        finally:
            closed.set()

    ahead = ReadAhead(source(), max_chunks=2)
    assert next(ahead) == b"\x00"
    ahead.close()
    assert closed.wait(2)
    with pytest.raises(StopIteration):
        next(ahead)


def song_member_cases(database):
    """곡 1의 매니페스트 항목과 실제 내용."""
    files = database.get_song_files(1)
    with zipfile.ZipFile(io.BytesIO(database.get_song_data(1))) as zf:
        return [(entry, zf.read(entry["path"])) for entry in files]


@pytest.mark.parametrize("storage", ["store", "song_part", "song.data"])
def test_read_song_member_from_each_storage(tmp_path, database, storage):
    """곡 zip 전체를 읽지 않고 항목 하나만 풀어 읽습니다. 저장소 파일, 조각(song_part), 옛 song.data 모두."""
    database.insert_song(make_song(tmp_path, "s1", {"a.bms": b"#TITLE A\n" * 200, "b.bmp": os.urandom(3000)}))
    cases = song_member_cases(database)
    if storage != "store":
        raw = to_legacy("song", 1)
        if storage == "song.data":
            with db_module.connect() as con, con.cursor() as cur:
                cur.execute("DELETE FROM song_part WHERE song_id = 1")
                cur.execute("UPDATE song SET data = %s WHERE id = 1", (raw,))
                con.commit()
    methods = {entry["method"] for entry, _ in cases}
    assert zipfile.ZIP_DEFLATED in methods
    with db_module.connect() as con, con.cursor() as cur:
        for entry, data in cases:
            assert database._read_song_member(cur, 1, entry) == data, entry["path"]
            # data_offset이 없는 옛 매니페스트는 local file header를 읽어 자리를 찾습니다.
            old = {k: v for k, v in entry.items() if k != "data_offset"}
            assert database._read_song_member(cur, 1, old) == data, entry["path"]
        assert database._read_song_range(cur, 1, 0, 0) == b""


def test_read_song_member_rejects_mismatch(tmp_path, database):
    database.insert_song(make_song(tmp_path, "s1", {"a.bms": b"#TITLE A\n"}))
    entry, _ = next(c for c in song_member_cases(database) if c[0]["path"] == "a.bms")
    with db_module.connect() as con, con.cursor() as cur:
        # 매니페스트와 내용이 다르면(곡 zip이 바뀜) 엉뚱한 바이트를 돌려주지 않습니다.
        with pytest.raises(ValueError, match="does not match"):
            database._read_song_member(cur, 1, {**entry, "crc32": "00000000"})
        with pytest.raises(ValueError, match="does not match"):
            database._read_song_member(cur, 1, {**entry, "size": entry["size"] + 1})
        with pytest.raises(ValueError, match="unsupported compression"):
            database._read_song_member(cur, 1, {**entry, "method": zipfile.ZIP_LZMA})
        old = {k: v for k, v in entry.items() if k != "data_offset"}
        with pytest.raises(ValueError, match="bad local file header"):
            database._read_song_member(cur, 1, {**old, "offset": old["offset"] + 1})


def preview_rows():
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("SELECT song_id, path, note FROM song_preview ORDER BY song_id")
        return cur.fetchall()


def test_render_previews_records_failures_per_song(tmp_path, database, monkeypatch, caplog):
    for name in ("a", "b", "c"):
        database.insert_song(make_song(tmp_path, name, {f"{name}.bms": f"#TITLE {name}\n".encode()}))

    def fake_render(files, read):
        paths = {f["path"] for f in files}
        if "a.bms" in paths:
            raise RuntimeError("bad chart")
        if "b.bms" in paths:
            raise KeyError("x")
        assert read(next(f for f in files if f["path"] == "c.bms")) == b"#TITLE c\n"
        return "preview_ebms.ogg", b"OggS fake"

    monkeypatch.setattr(preview, "render", fake_render)
    # 한 곡이 실패해도 다른 곡은 만들고, 실패한 곡은 이유를 남겨 시작할 때마다 다시 만들지 않습니다.
    assert database.render_previews() == 1
    assert preview_rows() == ((1, None, "error: bad chart"), (2, None, "error: KeyError"), (3, "preview_ebms.ogg", ""))
    assert database.get_preview(3, "preview_ebms.ogg")[1] == b"OggS fake"
    assert "Song[2] failed" in caplog.text
    assert database.render_previews() == 0


def test_render_previews_stops_without_ffmpeg(tmp_path, database, monkeypatch, caplog):
    for name in ("a", "b"):
        database.insert_song(make_song(tmp_path, name, {f"{name}.bms": b"#TITLE\n"}))
    calls = []

    def no_ffmpeg(files, read):
        calls.append(1)
        raise RuntimeError("ffmpeg not found")

    monkeypatch.setattr(preview, "render", no_ffmpeg)
    # ffmpeg가 없으면 곡마다 시도하지 않고 멈추며, 아무것도 남기지 않아 ffmpeg를 깐 뒤 다시 만듭니다.
    assert database.render_previews() == 0
    assert calls == [1]
    assert preview_rows() == ()
    assert "ffmpeg not found. Previews are not rendered." in caplog.text


def test_render_previews_skips_song_changed_while_rendering(tmp_path, database, monkeypatch):
    database.insert_song(make_song(tmp_path, "a", {"a.bms": b"#TITLE\n"}))

    def render_while_replaced(files, read):
        with db_module.connect() as con, con.cursor() as cur:
            cur.execute("UPDATE song SET sha256 = %s WHERE id = 1", ("f" * 64,))
            con.commit()
        return "preview_ebms.ogg", b"OggS stale"

    monkeypatch.setattr(preview, "render", render_while_replaced)
    # 렌더링하는 동안 곡이 바뀌었으면 넣지 않고 다음에 다시 만듭니다.
    assert database.render_previews() == 0
    assert preview_rows() == ()


@pytest.mark.parametrize("backfill, column", [("backfill_chart_md5", "md5"), ("backfill_chart_meta", "title_key")])
def test_chart_backfill_retries_after_broken_chunk(tmp_path, database, monkeypatch, backfill, column):
    database.insert_song(make_song(tmp_path, "s1", {"a.bms": b"#TITLE A\n"}))
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute(f"UPDATE chart SET {column} = NULL")
        con.commit()
    real = database.get_chart_chunk_data
    monkeypatch.setattr(database, "get_chart_chunk_data", lambda chunk_no: b"not a zip")
    # 깨진 청크가 있으면 못 찾은 차트를 ''로 덮지 않아 다음 시작 때 다시 채웁니다.
    assert getattr(database, backfill)() == 0
    assert count(f"SELECT COUNT(*) FROM chart WHERE {column} IS NULL") == 1
    monkeypatch.setattr(database, "get_chart_chunk_data", real)
    assert getattr(database, backfill)() == 1
    assert count(f"SELECT COUNT(*) FROM chart WHERE {column} IS NULL") == 0
