"""여러 곡을 한 트랜잭션(배치)으로 넣고 청크는 배치마다 한 번만 갱신하는지 테스트합니다."""
import io
import os
import zipfile

import pytest

from ebms_server import constant, db as db_module, importer
from ebms_server.db import Database

from test_blob_storage import database, make_song  # noqa: F401  (fixture 재사용)


@pytest.fixture
def mysql(database):
    """롤백을 확인하는 테스트는 실제 MySQL에서만 돌립니다(SQLite 대체 연결은 commit/rollback이 없음)."""
    if db_module.connect.__name__ == "fake_connect":
        pytest.skip("needs MySQL for transaction rollback")
    return database


@pytest.fixture(autouse=True)
def import_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(constant, "IMPORT_DIR", tmp_path / "import")


def spy(monkeypatch, name):
    calls = []
    original = getattr(Database, name)

    def wrapper(self, cur, arg):
        calls.append(arg if isinstance(arg, int) else len(arg))
        return original(self, cur, arg)

    monkeypatch.setattr(Database, name, wrapper)
    return calls


def pack(n, prefix="s"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for i in range(n):
            zf.writestr(f"{prefix}{i}/a.bms", f"#TITLE {prefix}{i}\n".encode())
            zf.writestr(f"{prefix}{i}/banner.png", os.urandom(100))
    return buf.getvalue()


def rows(sql):
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute(sql)
        return cur.fetchall()


def chunk_names():
    names = []
    for (data,) in rows("SELECT data FROM chart_chunk ORDER BY id"):
        names += zipfile.ZipFile(io.BytesIO(data)).namelist()
    return names


def test_chunks_once_per_batch(database, tmp_path, monkeypatch):
    appends = spy(monkeypatch, "_append_charts_to_chunk")
    manifests = spy(monkeypatch, "_rebuild_manifest_chunk")
    pres = spy(monkeypatch, "_rebuild_pre_chunk")
    path = tmp_path / "pack.zip"
    path.write_bytes(pack(40))

    songs = importer.import_zip(path, "pack.zip")
    assert len(songs) == 40 and not any("error" in s for s in songs)
    # 차트 청크는 한 번만 읽고 쓰고, 매니페스트(1000곡)·사전(32곡) 청크는 영향받은 것만 한 번씩
    assert appends == [40]
    assert manifests == [0]
    assert pres == [0, 1]
    assert len(chunk_names()) == 40
    assert rows("SELECT COUNT(*) FROM song")[0][0] == 40


def test_batch_limits(database, tmp_path, monkeypatch):
    monkeypatch.setattr(constant, "IMPORT_BATCH_SONGS", 3)
    appends = spy(monkeypatch, "_append_charts_to_chunk")
    path = tmp_path / "pack.zip"
    path.write_bytes(pack(7))
    importer.import_zip(path, "pack.zip")
    assert appends == [3, 3, 1]
    assert len(chunk_names()) == 7

    # 새 차트 크기가 청크 크기에 이르러도 커밋합니다.
    monkeypatch.setattr(constant, "IMPORT_BATCH_SONGS", 500)
    monkeypatch.setattr(constant, "BYTE_PER_CHUNK", 20)  # 차트 하나가 10바이트
    appends.clear()
    path.write_bytes(pack(4, "t"))
    importer.import_zip(path, "pack2.zip")
    assert appends == [2, 2]


def test_failed_song_rolls_back_only_itself(database, tmp_path, monkeypatch):
    original = Database._insert_or_update_charts

    def flaky(self, root, bms_files, song_id, cur):
        if root.name == "s1":
            raise RuntimeError("boom")
        return original(self, root, bms_files, song_id, cur)

    monkeypatch.setattr(Database, "_insert_or_update_charts", flaky)
    path = tmp_path / "pack.zip"
    path.write_bytes(pack(3))
    songs = importer.import_zip(path, "pack.zip")
    assert [("error" in s) for s in songs] == [False, True, False]
    # 실패한 곡은 song 행까지 되돌립니다.
    assert [r[0] for r in rows("SELECT folder FROM song ORDER BY id")] == ["s0", "s2"]
    assert len(chunk_names()) == 2


def test_failed_commit_rolls_back_batch(mysql, tmp_path, monkeypatch):
    monkeypatch.setattr(constant, "IMPORT_BATCH_SONGS", 2)
    original = Database._rebuild_pre_chunk
    calls = []

    def fail_second_batch(self, cur, chunk_no):
        calls.append(chunk_no)
        if len(calls) == 2:
            raise RuntimeError("disk full")
        return original(self, cur, chunk_no)

    monkeypatch.setattr(Database, "_rebuild_pre_chunk", fail_second_batch)
    path = tmp_path / "pack.zip"
    path.write_bytes(pack(5))
    songs = importer.import_zip(path, "pack.zip")
    # 두 번째 배치(s2, s3)는 커밋하지 못해 곡·차트·청크가 모두 되돌아가고 결과에 오류가 남습니다.
    assert [("error" in s) for s in songs] == [False, False, True, True, False]
    assert sorted(r[0] for r in rows("SELECT folder FROM song")) == ["s0", "s1", "s4"]
    assert rows("SELECT COUNT(*) FROM chart")[0][0] == 3
    assert len(chunk_names()) == 3


def test_tmp_folders_removed_after_commit(mysql, monkeypatch):
    original = Database._rebuild_manifest_chunk

    def fail(self, cur, chunk_no):
        raise RuntimeError("fail")

    for i in range(2):
        make_song(constant.TMP_DIR / "g", f"s{i}", {"a.bms": f"#T{i}".encode()})
    monkeypatch.setattr(Database, "_rebuild_manifest_chunk", fail)
    with pytest.raises(RuntimeError):
        importer.import_tmp()
    # 커밋하지 못하면 원본 폴더를 지우지 않습니다.
    assert (constant.TMP_DIR / "g" / "s0").exists()
    monkeypatch.setattr(Database, "_rebuild_manifest_chunk", original)
    songs = importer.import_tmp()
    assert len(songs) == 2 and not any("error" in s for s in songs)
    assert not (constant.TMP_DIR / "g" / "s0").exists()
