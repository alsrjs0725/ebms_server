"""파일 저장소(store) 단위 테스트: 쓰기(임시 파일 → rename), 범위 읽기, gc용 목록·삭제 (#152)."""
import os
import time

import pytest

from ebms_server import constant, store


def test_paths():
    sha = "ab" * 32
    assert store.song_path(12345, sha) == f"song/0012/12345.{sha[:16]}.zip"
    assert store.chart_chunk_path(7, sha) == f"chart_chunk/00007.{sha[:16]}.zip"
    assert store.pre_chunk_path(1234, sha) == f"pre_chunk/0001/01234.{sha[:16]}.zip"


def test_write_read_and_range():
    rel = store.write("song/0000/1.aaaa.zip", b"0123456789")
    assert rel == "song/0000/1.aaaa.zip"
    assert store.read(rel) == b"0123456789"
    assert store.read_range(rel, 3, 4) == b"3456"
    # 끝을 넘는 범위는 있는 만큼만
    assert store.read_range(rel, 8, 10) == b"89"
    # 임시 파일은 남지 않습니다.
    assert os.listdir(store.full_path(rel).parent) == ["1.aaaa.zip"]


def test_write_keeps_existing_file_of_same_size():
    rel = "chart_chunk/00001.bbbb.zip"
    store.write(rel, b"first")
    before = store.full_path(rel).stat().st_mtime_ns
    # 이름에 sha256이 들어 있어 크기가 같으면 같은 내용으로 보고 다시 쓰지 않습니다.
    store.write(rel, b"other")
    assert store.read(rel) == b"first"
    assert store.full_path(rel).stat().st_mtime_ns == before
    # 크기가 다르면(잘린 파일 등) 새로 씁니다.
    store.write(rel, b"longer data")
    assert store.read(rel) == b"longer data"


def test_write_failure_removes_temp_file(monkeypatch):
    rel = "song/0000/2.cccc.zip"

    def broken_fsync(fd):
        raise OSError("disk full")

    monkeypatch.setattr(store.os, "fsync", broken_fsync)
    with pytest.raises(OSError, match="disk full"):
        store.write(rel, b"data")
    folder = store.full_path(rel).parent
    # 실패하면 최종 파일도 임시 파일도 남지 않습니다.
    assert list(folder.iterdir()) == []


def test_fsync_dir_ignores_missing_folder(tmp_path):
    store._fsync_dir(tmp_path / "missing")


def test_files_lists_old_files_including_temp():
    assert store.files(time.time()) == []  # STORE_DIR이 아직 없음
    old = store.write("song/0000/1.aaaa.zip", b"a")
    new = store.write("pre_chunk/0000/00001.bbbb.zip", b"b")
    tmp = store.full_path(f"song/0000/3.dddd.zip{store.TMP_MARK}x")
    tmp.write_bytes(b"t")
    past = time.time() - 3600
    os.utime(store.full_path(old), (past, past))
    os.utime(tmp, (past, past))

    assert sorted(store.files(time.time() - 60)) == sorted([old, tmp.relative_to(constant.STORE_DIR).as_posix()])
    assert sorted(store.files(time.time() + 60)) == sorted(
        [old, new, tmp.relative_to(constant.STORE_DIR).as_posix()]
    )


def test_remove_counts_only_existing_files():
    a = store.write("song/0000/1.aaaa.zip", b"a")
    b = store.write("song/0000/2.bbbb.zip", b"b")
    assert store.remove([a, "song/0000/missing.zip", b, a]) == 2
    assert not store.full_path(a).exists() and not store.full_path(b).exists()


def test_free_bytes_before_store_dir_exists(monkeypatch, tmp_path):
    monkeypatch.setattr(constant, "STORE_DIR", tmp_path / "not" / "yet" / "store")
    assert store.free_bytes() > 0


def test_hold_is_reentrant():
    rel = "song/0000/1.aaaa.zip"
    with store.hold(rel):
        with store.hold(rel):
            assert rel in store.held()
        # 바깥 hold가 남아 있으면 계속 잡혀 있습니다.
        assert rel in store.held()
    assert rel not in store.held()


def test_hold_releases_on_error():
    rel = "song/0000/9.eeee.zip"
    with pytest.raises(RuntimeError):
        with store.hold(rel):
            raise RuntimeError("boom")
    assert rel not in store.held()


def test_cutoff_leaves_grace_period(monkeypatch):
    monkeypatch.setattr(constant, "STORE_GC_GRACE_SECONDS", 600)
    assert store.cutoff() == pytest.approx(time.time() - 600, abs=5)
