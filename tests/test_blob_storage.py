"""곡 zip·청크 저장(저장소 파일)과 다운로드 왕복 테스트. EBMS_DB_* 환경변수의 서버에 ebms_test DB를 만들어 사용합니다."""
import hashlib
import io
import os
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
    monkeypatch.setattr(constant, "READ_SIZE", 1000)
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


def count(sql):
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute(sql)
        return cur.fetchone()[0]


def test_song_is_stored_as_file(tmp_path, client, database):
    song = make_song(tmp_path, "s_part", {"a.bms": b"#TITLE\n#BANNER banner.bmp\n", "banner.bmp": os.urandom(2000)})
    database.insert_song(song)
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("SELECT path, sha256 FROM song WHERE id = 1")
        rel, sha256 = cur.fetchone()
    assert rel == store.song_path(1, sha256)
    assert hashlib.sha256(store.read(rel)).hexdigest() == sha256
    for table in db_module.STORE_TABLES:
        assert count(f"SELECT COUNT(*) FROM {table} WHERE path IS NULL") == 0

    res = client.get("/api/pre/song/1/file?path=banner.bmp")
    assert res.status_code == 200 and len(res.content) == 2000
    res = client.get("/api/play/song/1", headers={"Range": "bytes=10-19"})
    assert res.status_code == 206 and res.content == store.read(rel)[10:20]


def test_store_gc(tmp_path, database, monkeypatch):
    """아무 행도 가리키지 않는 파일(바뀌기 전 판, 지운 곡, 임시 파일)만 지웁니다. 최근 파일은 둡니다."""
    database.insert_song(make_song(tmp_path, "s1", {"a.bms": b"#TITLE A\n"}))
    database.insert_song(make_song(tmp_path, "s2", {"b.bms": b"#TITLE B\n"}))
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("SELECT path FROM song ORDER BY id")
        used = [row[0] for row in cur.fetchall()]
    old = store.write(store.song_path(1, "0" * 64), b"old")
    (store.full_path(old).parent / f"x{store.TMP_MARK}abc").write_bytes(b"tmp")

    # 방금 쓴 파일은 지우지 않습니다.
    assert database.store_gc() == 0
    monkeypatch.setattr(constant, "STORE_GC_GRACE_SECONDS", -10)
    # old, 임시 파일, 두 번째 곡을 넣을 때 바뀐 차트 청크의 옛 판
    assert database.store_gc() == 3
    assert not store.full_path(old).exists()
    assert not list(store.full_path(old).parent.glob(f"*{store.TMP_MARK}*"))
    with db_module.connect() as con, con.cursor() as cur:
        for table in ("song", "chart_chunk", "pre_chunk"):
            cur.execute(f"SELECT path FROM {table}")
            used += [row[0] for row in cur.fetchall()]
    assert all(store.full_path(rel).exists() for rel in used)
    assert database.store_gc() == 0


def test_open_file_keeps_reading_replaced_file(tmp_path, database, monkeypatch):
    """받는 중에 새 판으로 바뀌어 옛 파일이 지워져도 열어 둔 파일로 끝까지 읽습니다."""
    database.insert_song(make_song(tmp_path, "s1", {"a.bms": b"#TITLE A\n"}))
    file = database.open_file("song", 1)
    data = store.read(file.path)
    store.full_path(file.path).unlink()
    assert b"".join(file.iter_range(0, file.size - 1)) == data
    assert file.read(3, 5) == data[3:8]
    file.close()


def test_drops_blob_tables_after_store_migration(database):
    """저장소 파일로 다 옮긴 DB(#152)는 시작할 때 남은 BLOB 테이블·컬럼을 지웁니다."""
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("CREATE TABLE song_part (song_id INT UNSIGNED NOT NULL, seq INT UNSIGNED NOT NULL, data LONGBLOB)")
        cur.execute("ALTER TABLE song ADD COLUMN data LONGBLOB")
        cur.execute("ALTER TABLE chart_chunk ADD COLUMN data LONGBLOB")
        con.commit()
    database.generate_database()
    with db_module.connect() as con, con.cursor() as cur:
        for table, column in db_module.BLOB_COLUMNS:
            assert not database._has_column(cur, table, column)
        with pytest.raises(Exception):
            cur.execute("SELECT 1 FROM song_part")


def test_refuses_to_start_with_blobs_not_moved(tmp_path, database):
    """아직 DB BLOB에 남은 곡이 있으면 지우지 않고 시작하지 않습니다."""
    database.insert_song(make_song(tmp_path, "s1", {"a.bms": b"#TITLE A\n"}))
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("ALTER TABLE song ADD COLUMN data LONGBLOB")
        cur.execute("UPDATE song SET path = NULL, data = %s", (b"zip",))
        con.commit()
    with pytest.raises(RuntimeError, match="BLOB"):
        database.generate_database()
    with db_module.connect() as con, con.cursor() as cur:
        assert database._has_column(cur, "song", "data")


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

    # 청크에 자리(2곡)가 남으면 같은 청크에 들어가고, 차면 다음 곡은 새 청크에 들어갑니다. 바뀐 청크만 바뀝니다.
    database.insert_song(make_song(tmp_path, "s2", {"b.bms": b"#B"}))
    after = client.get("/api/pre/assethash").json()
    # 곡 2는 사전 파일이 없어 청크 내용이 같습니다.
    assert after == hashes
    s3 = make_song(tmp_path, "s3", {"c.bms": b"#C"})
    (s3 / "preview.ogg").write_bytes(b"p3")
    database.insert_song(s3)
    last = client.get("/api/pre/assethash").json()
    assert sorted(last) == ["0", "1"] and last["0"] == after["0"]
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


def song_member_cases(database):
    """곡 1의 매니페스트 항목과 실제 내용."""
    files = database.get_song_files(1)
    with zipfile.ZipFile(io.BytesIO(database.get_song_data(1))) as zf:
        return [(entry, zf.read(entry["path"])) for entry in files]


def test_read_song_member(tmp_path, database):
    """곡 zip 전체를 읽지 않고 항목 하나만 풀어 읽습니다."""
    database.insert_song(make_song(tmp_path, "s1", {"a.bms": b"#TITLE A\n" * 200, "b.bmp": os.urandom(3000)}))
    cases = song_member_cases(database)
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
