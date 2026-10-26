"""임포트 캐시(SSD 작업 공간): 한도 안이면 업로드·대기 묶음을 캐시에, 넘으면 var에 두는지."""
import pathlib

import pytest
from fastapi.testclient import TestClient

from ebms_server import cache, constant, importer, staging

from test_auth import client, oauth  # noqa: F401  (fixture 재사용)
from test_import import commit, make_zip, song_rows, stage, wait  # noqa: F401
from test_quota import admin_login
from test_rar import send

DATA = pathlib.Path(__file__).parent / "data"


@pytest.fixture
def cache_on(monkeypatch):
    monkeypatch.setattr(constant, "CACHE_MAX_BYTES", 10 * 1024 ** 2)
    monkeypatch.setattr(cache, "DISK_MARGIN", 0)


def stages(client):
    r = client.get("/api/admin/import/stages")
    assert r.status_code == 200, r.text
    return {s["id"]: s for s in r.json()}


def test_parse_size():
    assert constant.parse_size("0") == 0
    assert constant.parse_size("1048576") == 1024 ** 2
    assert constant.parse_size("50G") == 50 * 1024 ** 3
    assert constant.parse_size("512mb") == 512 * 1024 ** 2
    assert constant.parse_size("1.5GiB") == int(1.5 * 1024 ** 3)


def test_cache_off_uses_var(client, monkeypatch):
    admin_login(client, monkeypatch)
    job = stage(client, "pack.zip", make_zip({"s/a.bms": b"#TITLE A\n"}))
    assert (constant.STAGED_DIR / job["stage_id"]).is_dir()
    assert stages(client)[job["stage_id"]]["cached"] is False
    assert client.get("/api/admin/import/cache").json() == {"enabled": False, "used": 0, "max": 0}


def test_stage_in_cache_and_commit(client, monkeypatch, cache_on):
    admin_login(client, monkeypatch)
    job = stage(client, "pack.zip", make_zip({"s/a.bms": b"#TITLE A\n", "s/k.wav": b"k" * 1000}))
    sid = job["stage_id"]
    assert (constant.CACHE_DIR / "staged" / sid).is_dir()
    assert not (constant.STAGED_DIR / sid).exists()
    assert stages(client)[sid]["cached"] is True
    status = client.get("/api/admin/import/cache").json()
    assert status["enabled"] and status["max"] == 10 * 1024 ** 2
    assert status["used"] >= 1000
    # 다 받은 업로드는 지웠고 예약도 남지 않습니다.
    assert not any((constant.CACHE_DIR / "import").iterdir())
    assert not cache._reserved

    done = commit(client, job)
    assert done["status"] == "done"
    assert len(song_rows()) == 1
    assert not (constant.CACHE_DIR / "staged" / sid).exists()
    assert client.get("/api/admin/import/cache").json()["used"] == 0


def test_over_limit_falls_back_to_var(client, monkeypatch, cache_on):
    monkeypatch.setattr(constant, "CACHE_MAX_BYTES", 500)
    admin_login(client, monkeypatch)
    job = stage(client, "big.zip", make_zip({"s/a.bms": b"#TITLE A\n", "s/k.wav": b"k" * 1000}))
    assert (constant.STAGED_DIR / job["stage_id"]).is_dir()
    assert stages(client)[job["stage_id"]]["cached"] is False


def test_staged_counts_toward_limit(client, monkeypatch, cache_on):
    # 올린 파일도 다 풀 때까지 묶음에 있으므로 함께 셉니다(곡 하나 약 100KB: 업로드 + 원본 + 풀린 크기가 한도 안, 둘째 묶음은 넘음).
    monkeypatch.setattr(constant, "CACHE_MAX_BYTES", 350_000)
    admin_login(client, monkeypatch)
    first = stage(client, "one.zip", make_zip({"s/a.bms": b"#TITLE A\n", "s/k.wav": b"k" * 100_000}), chunk=50_000)
    second = stage(client, "two.zip", make_zip({"t/b.bms": b"#TITLE B\n", "t/k.wav": b"j" * 100_000}), chunk=50_000)
    listed = stages(client)
    assert listed[first["stage_id"]]["cached"] is True
    assert listed[second["stage_id"]]["cached"] is False
    # 캐시를 꺼도 캐시에 남은 묶음을 찾습니다.
    monkeypatch.setattr(constant, "CACHE_MAX_BYTES", 0)
    assert set(stages(client)) == {first["stage_id"], second["stage_id"]}
    assert commit(client, first)["status"] == "done"


def test_tmp_import_into_cache(client, monkeypatch, cache_on):
    admin_login(client, monkeypatch)
    song = constant.TMP_DIR / "g" / "s1"
    song.mkdir(parents=True)
    (song / "a.bms").write_bytes(b"#TITLE A\n")
    r = client.post("/api/admin/import/tmp")
    job = wait(client, r.json())
    sid = job["stage_id"]
    assert (constant.CACHE_DIR / "staged" / sid).is_dir()
    assert not song.exists()
    assert commit(client, job)["status"] == "done"


def test_recover_finds_cached_stage(client, monkeypatch, cache_on):
    admin_login(client, monkeypatch)
    job = stage(client, "pack.zip", make_zip({"s/a.bms": b"#TITLE A\n"}))
    sid = job["stage_id"]
    (constant.CACHE_DIR / "staged" / sid / "stray").mkdir()
    staging.recover()
    assert not (constant.CACHE_DIR / "staged" / sid / "stray").exists()
    assert sid in stages(client)


@pytest.mark.parametrize("name", ["solo.7z", "solo.rar"])
def test_finish_cached_upload_without_var_import(client, monkeypatch, cache_on, name):
    """업로드가 캐시에만 놓여 var/import가 없어도 rar·7z를 풀기 전 남은 공간 확인이 500을 내지 않습니다."""
    admin_login(client, monkeypatch)
    upload_id = send(client, name, (DATA / name).read_bytes())
    assert (constant.CACHE_DIR / "import" / f"{upload_id}.upload").is_file()
    assert not constant.IMPORT_DIR.exists()
    r = client.post(f"/api/admin/import/uploads/{upload_id}/finish")
    assert r.status_code == 200, r.text
    wait(client, r.json())


def test_admin_api_error_shows_cause(client, monkeypatch):
    """관리자 API에서 처리하지 못한 예외는 "Internal Server Error" 대신 원인을 detail로 보여줍니다."""
    admin_login(client, monkeypatch)
    upload_id = send(client, "pack.7z", b"x")

    def broken(ids):
        raise FileNotFoundError(2, "No such file or directory", "/app/var/import")

    monkeypatch.setattr(importer, "finish_uploads", broken)
    quiet = TestClient(client.app, raise_server_exceptions=False, cookies=client.cookies)
    r = quiet.post(f"/api/admin/import/uploads/{upload_id}/finish")
    assert r.status_code == 500
    assert r.json()["detail"] == "서버 오류(FileNotFoundError): [Errno 2] No such file or directory: '/app/var/import'"
    assert quiet.get("/api/version").status_code == 200


def test_work_dir(monkeypatch, cache_on):
    assert cache.work_dir() == constant.CACHE_DIR / "work"
    assert (constant.CACHE_DIR / "work").is_dir()
    monkeypatch.setattr(constant, "CACHE_MAX_BYTES", 0)
    assert cache.work_dir() == constant.TMP_DIR


def test_unusable_cache_falls_back(monkeypatch, cache_on, tmp_path, caplog):
    """캐시 폴더를 만들 수 없으면(디스크가 빠짐 등) 경고만 남기고 var 쪽을 씁니다."""
    blocker = tmp_path / "not_a_dir"
    blocker.write_bytes(b"")
    monkeypatch.setattr(constant, "CACHE_DIR", blocker)
    assert cache.work_dir() == constant.TMP_DIR
    fallback = tmp_path / "var_staged"
    assert cache.place("staged", fallback, "s1", 10) == fallback / "s1"
    assert fallback.is_dir()
    assert not cache._reserved
    assert caplog.text.count("cache: unavailable") == 2


def test_usage_counts_reservations(cache_on):
    # 캐시 폴더가 아직 없으면 예약만 셉니다.
    path = cache.place("staged", constant.STAGED_DIR, "s1", 4096)
    try:
        assert path == constant.CACHE_DIR / "staged" / "s1"
        assert cache.usage() == 4096
        # 예약한 자리에 쓴 만큼은 예약에서 빼서 두 번 세지 않습니다.
        path.mkdir()
        (path / "f").write_bytes(b"x" * 4096)
        assert cache.usage() == cache._du(constant.CACHE_DIR) + max(0, 4096 - cache._du(path))
    finally:
        cache.release(path)
    assert not cache._reserved
    assert cache._du(path / "f", apparent=True) == 4096
    assert cache._du(path, apparent=True) == 4096
