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


# ---- 겹치는 차트가 있는 폴더를 기존 곡에 병합(#50) ----

def put_song(root, files):
    root.mkdir(parents=True)
    for name, body in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(body)
    return root


def song_zip(song_id):
    data = Database().get_song_data(song_id)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return {name: zf.read(name) for name in zf.namelist()}


def manifest_entry(song_id):
    import gzip
    import json
    entries = json.loads(gzip.decompress(Database().get_manifest_chunk(0)))
    return next(e for e in entries if e["song_id"] == song_id)


def test_overlapping_chart_merges_new_files(database):
    a, a_wav, n, n_wav, pv = b"#TITLE A\n#PREVIEW pv.ogg\n", os.urandom(300), b"#TITLE N\n", os.urandom(400), os.urandom(200)
    put_song(constant.TMP_DIR / "g" / "song1", {"a.bms": a, "a.wav": a_wav})
    assert importer.import_tmp()[0]["new_song"]
    hash_before = Database().get_pre_chunk_hash()

    pkg = put_song(constant.TMP_DIR / "g" / "pkgX", {"a.bms": a, "n.bms": n, "n.wav": n_wav, "pv.ogg": pv})
    songs = importer.import_tmp()
    assert songs == [{
        "folder": "g/pkgX", "song_id": 1, "new_song": False, "charts": 2, "new_charts": 1,
        "added_files": ["n.bms", "n.wav", "pv.ogg"],
    }]
    # 새 키음·프리뷰가 기존 곡 zip에 들어가고 폴더는 지웁니다.
    assert song_zip(1) == {"a.bms": a, "a.wav": a_wav, "n.bms": n, "n.wav": n_wav, "pv.ogg": pv}
    assert not pkg.exists()
    assert rows("SELECT COUNT(*) FROM song")[0][0] == 1
    # song 행(크기·해시·files)과 매니페스트·사전 청크를 다시 만듭니다.
    entry = manifest_entry(1)
    data = Database().get_song_data(1)
    assert entry["zip_size"] == len(data)
    assert {f["path"]: f["kind"] for f in entry["files"]} == {
        "a.bms": "pre", "a.wav": "play", "n.bms": "pre", "n.wav": "play", "pv.ogg": "pre",
    }
    assert {c["path"] for c in entry["chart_files"]} == {"a.bms", "n.bms"}
    assert Database().get_pre_chunk_hash() != hash_before
    assert Database().get_song_files(1) == entry["files"]


def test_overlapping_chart_conflicts_keep_existing(database):
    a, c1, c2 = b"#TITLE A\n", b"#TITLE C1\n", b"#TITLE C2\n"
    put_song(constant.TMP_DIR / "g" / "song1", {"a.bms": a, "c.bms": c1, "k.wav": b"old"})
    importer.import_tmp()

    pkg = put_song(constant.TMP_DIR / "g" / "pkg", {"a.bms": a, "c.bms": c2, "k.wav": b"new", "m.wav": b"m"})
    songs = importer.import_tmp()
    assert songs == [{
        "folder": "g/pkg", "song_id": 1, "new_song": False, "charts": 1, "new_charts": 0,
        "added_files": ["m.wav"], "conflicts": ["c.bms", "k.wav"],
    }]
    assert song_zip(1) == {"a.bms": a, "c.bms": c1, "k.wav": b"old", "m.wav": b"m"}
    # 곡 zip에 넣지 못한 차트는 등록하지 않고, 원본 폴더는 남깁니다.
    assert rows("SELECT COUNT(*) FROM chart")[0][0] == 2
    assert pkg.exists()


def test_overlapping_multiple_songs_is_not_registered(database):
    a, b = b"#TITLE A\n", b"#TITLE B\n"
    put_song(constant.TMP_DIR / "g" / "s1", {"a.bms": a, "a.wav": b"a"})
    put_song(constant.TMP_DIR / "g" / "s2", {"b.bms": b, "b.wav": b"b"})
    importer.import_tmp()
    before = [song_zip(1), song_zip(2)]

    pkg = put_song(constant.TMP_DIR / "g" / "pkgY", {"a.bms": a, "b.bms": b, "c.bms": b"#TITLE C\n", "c.wav": b"c"})
    songs = importer.import_tmp()
    assert len(songs) == 1 and songs[0]["folder"] == "g/pkgY" and "1, 2" in songs[0]["error"]
    assert pkg.exists()
    assert [song_zip(1), song_zip(2)] == before
    assert rows("SELECT COUNT(*) FROM chart")[0][0] == 2


def test_overlapping_chart_within_same_batch(database, tmp_path):
    """같은 배치에서 앞서 넣은 곡과 겹쳐도 그 곡에 병합합니다."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("s0/a.bms", b"#TITLE A\n")
        zf.writestr("s0/x.wav", b"x")
        zf.writestr("s1/a.bms", b"#TITLE A\n")
        zf.writestr("s1/y.wav", b"y")
        zf.writestr("s2/b.bms", b"#TITLE B\n")
    path = tmp_path / "pack.zip"
    path.write_bytes(buf.getvalue())
    songs = importer.import_zip(path, "pack.zip")
    assert [(s["song_id"], s["new_song"], s.get("added_files")) for s in songs] == [
        (1, True, None), (1, False, ["y.wav"]), (2, True, None),
    ]
    assert song_zip(1) == {"a.bms": b"#TITLE A\n", "x.wav": b"x", "y.wav": b"y"}
    assert {f["path"] for f in manifest_entry(1)["files"]} == {"a.bms", "x.wav", "y.wav"}


def test_merge_too_large_is_skipped(database, monkeypatch):
    a = b"#TITLE A\n"
    put_song(constant.TMP_DIR / "g" / "s1", {"a.bms": a})
    importer.import_tmp()
    before = song_zip(1)
    monkeypatch.setattr(database, "max_allowed_packet", constant.PACKET_OVERHEAD + len(Database().get_song_data(1)) + 100)
    pkg = put_song(constant.TMP_DIR / "g" / "big", {"a.bms": a, "big.wav": os.urandom(10_000)})
    songs = importer.import_tmp()
    assert songs[0]["error"]
    assert song_zip(1) == before
    assert pkg.exists()
