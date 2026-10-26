"""청크 크기(차트 CHARTS_PER_CHUNK개, 사전 SONGS_PER_PRE_CHUNK곡, 매니페스트 SONGS_PER_MANIFEST_CHUNK곡)를 바꿨을 때 기존 청크를 다시 묶는 것을 테스트합니다."""
import gzip
import hashlib
import io
import json
import zipfile

import pytest

from ebms_server import constant, db as db_module, importer

from test_batch import pack, rows, chunk_names
from test_blob_storage import database  # noqa: F401  (fixture 재사용)


@pytest.fixture(autouse=True)
def import_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(constant, "IMPORT_DIR", tmp_path / "import")


def import_pack(tmp_path, n, prefix):
    path = tmp_path / f"{prefix}.zip"
    path.write_bytes(pack(n, prefix))
    songs = importer.import_zip(path, path.name)
    assert len(songs) == n and not any("error" in s for s in songs)


def chunk_sizes():
    return {
        chunk_id: len(zipfile.ZipFile(io.BytesIO(db_module.Database().get_chart_chunk_data(chunk_id))).namelist())
        for (chunk_id,) in rows("SELECT id FROM chart_chunk ORDER BY id")
    }


def setting(name):
    found = rows(f"SELECT value FROM setting WHERE name = '{name}'")
    return found[0][0] if found else None


def test_chart_chunks_hold_fixed_number_of_charts(database, tmp_path, monkeypatch):
    monkeypatch.setattr(constant, "CHARTS_PER_CHUNK", 3)
    import_pack(tmp_path, 7, "a")
    assert chunk_sizes() == {0: 3, 1: 3, 2: 1}
    # 마지막 청크를 채운 뒤 다음 청크로 넘어갑니다.
    import_pack(tmp_path, 3, "b")
    assert chunk_sizes() == {0: 3, 1: 3, 2: 3, 3: 1}
    assert len(set(chunk_names())) == 10


def test_rechunk_charts(database, tmp_path, monkeypatch):
    import_pack(tmp_path, 7, "a")
    names = chunk_names()
    assert chunk_sizes() == {0: 7}
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("UPDATE chart SET md5 = NULL")
        con.commit()

    monkeypatch.setattr(constant, "CHARTS_PER_CHUNK", 3)
    assert database.rechunk_charts() == 3
    # 새 청크는 옛 청크 뒤 번호에 같은 순서로 들어가고, 옛 청크는 지웁니다.
    assert chunk_sizes() == {1: 3, 2: 3, 3: 1}
    assert chunk_names() == names
    assert setting(db_module.CHART_CHUNK_SETTING) == "3"
    assert setting(db_module.CHART_RECHUNK_SETTING) is None
    # md5도 함께 채웁니다.
    for (chunk_id,) in rows("SELECT id FROM chart_chunk"):
        with zipfile.ZipFile(io.BytesIO(db_module.Database().get_chart_chunk_data(chunk_id))) as zf:
            for info in zf.infolist():
                sha256 = info.filename.rsplit(".", 1)[0]
                [(md5,)] = rows(f"SELECT md5 FROM chart WHERE id = '{sha256}'")
                assert md5 == hashlib.md5(zf.read(info)).hexdigest()
    assert database.rechunk_charts() == 0

    # 이후 임포트는 새 크기로 이어 붙입니다.
    import_pack(tmp_path, 2, "b")
    assert chunk_sizes() == {1: 3, 2: 3, 3: 3}


def test_rechunk_charts_resumes(database, tmp_path, monkeypatch):
    import_pack(tmp_path, 5, "a")
    names = chunk_names()
    monkeypatch.setattr(constant, "CHARTS_PER_CHUNK", 2)
    # 새 청크를 만들다 꺼진 상태: 만들다 만 청크는 지우고 처음부터 다시 만듭니다.
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("INSERT INTO setting (name, value) VALUES (%s, '0')", (db_module.CHART_RECHUNK_SETTING,))
        cur.execute("INSERT INTO setting (name, value) VALUES (%s, 'copy')", (db_module.CHART_RECHUNK_PHASE_SETTING,))
        con.commit()
    with db_module.connect() as con, con.cursor() as cur:
        database._write_chart_chunk(cur, 1, b"partial")
        con.commit()
    assert database.rechunk_charts() == 3
    assert chunk_sizes() == {1: 2, 2: 2, 3: 1}
    assert chunk_names() == names

    # 옛 청크를 지우다 꺼진 상태: 새 청크는 그대로 두고 옛 청크만 마저 지웁니다.
    monkeypatch.setattr(constant, "CHARTS_PER_CHUNK", 5)
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("DELETE FROM setting WHERE name = %s", (db_module.CHART_CHUNK_SETTING,))
        cur.execute("INSERT INTO setting (name, value) VALUES (%s, '2')", (db_module.CHART_RECHUNK_SETTING,))
        cur.execute("INSERT INTO setting (name, value) VALUES (%s, 'delete')", (db_module.CHART_RECHUNK_PHASE_SETTING,))
        con.commit()
    assert database.rechunk_charts() == 0
    assert chunk_sizes() == {3: 1}
    assert setting(db_module.CHART_CHUNK_SETTING) == "5"


def test_pre_chunks_rebuilt_when_size_changes(database, tmp_path, monkeypatch):
    monkeypatch.setattr(constant, "SONGS_PER_PRE_CHUNK", 4)
    database.backfill_pre_chunks()
    assert setting(db_module.PRE_CHUNK_SETTING) == "4"
    # 배너가 사전 파일이 되도록 #BANNER를 넣은 곡
    path = tmp_path / "pre.zip"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for i in range(10):
            zf.writestr(f"p{i}/a.bms", f"#TITLE p{i}\n#BANNER banner.png\n".encode())
            zf.writestr(f"p{i}/banner.png", bytes([i]) * 100)
    path.write_bytes(buf.getvalue())
    assert len(importer.import_zip(path, path.name)) == 10
    # 곡 1~10 → 4곡 단위 청크 0~2
    assert [r[0] for r in rows("SELECT id FROM pre_chunk ORDER BY id")] == [0, 1, 2]

    monkeypatch.setattr(constant, "SONGS_PER_PRE_CHUNK", 32)
    assert database.backfill_pre_chunks() == 1
    assert [r[0] for r in rows("SELECT id FROM pre_chunk ORDER BY id")] == [0]
    assert setting(db_module.PRE_CHUNK_SETTING) == "32"
    blob = database.open_blob("pre_chunk", 0)
    data = b"".join(blob.iter_range(0, blob.size - 1))
    blob.close()
    names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    assert sorted(int(n.split("/")[0]) for n in names) == list(range(1, 11))
    assert database.backfill_pre_chunks() == 0


def manifest_song_ids():
    return {
        chunk_id: [s["song_id"] for s in json.loads(gzip.decompress(db_module.Database().get_manifest_chunk(chunk_id)))]
        for chunk_id in db_module.Database().get_manifest_hash()
    }


def test_manifest_rechunked_when_size_changes(database, tmp_path, monkeypatch):
    monkeypatch.setattr(constant, "SONGS_PER_MANIFEST_CHUNK", 1000)
    assert database.rechunk_manifest() == 0
    assert setting(db_module.MANIFEST_CHUNK_SETTING) == "1000"
    import_pack(tmp_path, 7, "a")
    assert manifest_song_ids() == {0: [1, 2, 3, 4, 5, 6, 7]}

    monkeypatch.setattr(constant, "SONGS_PER_MANIFEST_CHUNK", 3)
    assert database.rechunk_manifest() == 3
    assert manifest_song_ids() == {0: [1, 2], 1: [3, 4, 5], 2: [6, 7]}
    assert setting(db_module.MANIFEST_CHUNK_SETTING) == "3"
    assert database.rechunk_manifest() == 0

    # 다시 키우면 남는 번호의 청크는 지웁니다.
    monkeypatch.setattr(constant, "SONGS_PER_MANIFEST_CHUNK", 5)
    assert database.rechunk_manifest() == 2
    assert manifest_song_ids() == {0: [1, 2, 3, 4], 1: [5, 6, 7]}
