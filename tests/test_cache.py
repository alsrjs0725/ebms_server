"""임포트 캐시(SSD 작업 공간): 한도 안이면 업로드·대기 묶음을 캐시에, 넘으면 var에 두는지."""
import pytest

from ebms_server import cache, constant, staging

from test_auth import client, oauth  # noqa: F401  (fixture 재사용)
from test_import import commit, make_zip, song_rows, stage, wait  # noqa: F401
from test_quota import admin_login


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
    # 업로드 파일도 다 풀 때까지 캐시에 있으므로 함께 셉니다.
    monkeypatch.setattr(constant, "CACHE_MAX_BYTES", 5000)
    admin_login(client, monkeypatch)
    first = stage(client, "one.zip", make_zip({"s/a.bms": b"#TITLE A\n", "s/k.wav": b"k" * 2000}))
    second = stage(client, "two.zip", make_zip({"t/b.bms": b"#TITLE B\n", "t/k.wav": b"j" * 2000}))
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
