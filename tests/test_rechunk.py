"""차트 청크에 CHARTS_PER_CHUNK개씩 묶는 것과 이전 버전의 사전 청크(song id 구간)를 곡별 청크 번호로 옮기는 것을 테스트합니다."""
import io
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


def import_pre_songs(tmp_path, n):
    """배너가 사전 파일이 되도록 #BANNER를 넣은 곡 n개를 임포트합니다."""
    path = tmp_path / "pre.zip"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for i in range(n):
            zf.writestr(f"p{i}/a.bms", f"#TITLE p{i}\n#BANNER banner.png\n".encode())
            zf.writestr(f"p{i}/banner.png", bytes([i]) * 100)
    path.write_bytes(buf.getvalue())
    assert len(importer.import_zip(path, path.name)) == n


def to_legacy_layout(size):
    """이전 버전(사전 청크 = song id // size, 곡에 청크 번호 없음)의 DB로 되돌립니다."""
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("UPDATE song SET pre_chunk = NULL")
        cur.execute("UPDATE pre_chunk SET songs = NULL, sig = NULL")
        cur.execute("DELETE FROM setting WHERE name = %s", (db_module.PRE_LAYOUT_SETTING,))
        if size is not None:
            cur.execute("INSERT INTO setting (name, value) VALUES (%s, %s)", (db_module.PRE_CHUNK_SETTING, str(size)))
        con.commit()


def test_pre_layout_migrates_legacy_chunks(database, tmp_path, monkeypatch):
    monkeypatch.setattr(constant, "SONGS_PER_PRE_CHUNK", 4)
    import_pre_songs(tmp_path, 10)
    # 곡 1~10을 4곡씩 청크 0~2에 넣었습니다(이전 버전과 같은 번호가 되도록 song id // 4 구간으로 다시 만듭니다).
    with db_module.connect() as con, con.cursor() as cur:
        for song_id in range(1, 11):
            cur.execute("UPDATE song SET pre_chunk = %s WHERE id = %s", (song_id // 4, song_id))
        for chunk_no in range(3):
            database._rebuild_pre_chunk(cur, chunk_no)
        con.commit()
    hashes = database.get_pre_chunk_hash()
    to_legacy_layout(4)

    # 청크는 그대로 두고 곡에 이전 구간의 청크 번호만 적으므로 다시 만들 청크가 없습니다.
    database.generate_database()
    assert [tuple(r) for r in rows("SELECT id, pre_chunk FROM song ORDER BY id")] == [(i, i // 4) for i in range(1, 11)]
    assert setting(db_module.PRE_LAYOUT_SETTING) == "tables"
    assert setting(db_module.PRE_CHUNK_SETTING) is None
    assert database.backfill_pre_chunks() == 0
    assert database.get_pre_chunk_hash() == hashes


def test_pre_layout_rebuilds_unknown_legacy_chunks(database, tmp_path, monkeypatch):
    import_pre_songs(tmp_path, 10)
    to_legacy_layout(None)
    # 이전 크기를 모르면 청크를 지우고 8곡 구간으로 다시 만듭니다.
    database.generate_database()
    assert rows("SELECT COUNT(*) FROM pre_chunk")[0][0] == 0
    assert database.backfill_pre_chunks() == 2
    assert [r[0] for r in rows("SELECT id FROM pre_chunk ORDER BY id")] == [0, 1]
    blob = database.open_file("pre_chunk", 1)
    data = b"".join(blob.iter_range(0, blob.size - 1))
    blob.close()
    names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    assert sorted(int(n.split("/")[0]) for n in names) == [8, 9, 10]
    assert database.backfill_pre_chunks() == 0
