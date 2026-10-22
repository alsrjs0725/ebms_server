"""7z 곡 임포트 테스트. tests/data의 7z는 7-Zip 23.01로 만들었습니다(locked.7z는 암호 X)."""
import pathlib
import shutil

import pytest

from ebms_server import constant, sevenzip

from test_auth import client, oauth  # noqa: F401  (fixture 재사용)
from test_import import commit, import_dir, song_rows, wait  # noqa: F401
from test_quota import admin_login
from test_rar import send

DATA = pathlib.Path(__file__).parent / "data"

needs_7z = pytest.mark.skipif(sevenzip._binary() is None, reason="7-Zip not installed")


@needs_7z
def test_upload_7z(client, monkeypatch):
    admin_login(client, monkeypatch)
    upload_id = send(client, "pack.7z", (DATA / "pack.7z").read_bytes())
    job = wait(client, client.post(f"/api/admin/import/uploads/{upload_id}/finish").json())
    assert job["status"] == "done", job
    assert job["kind"] == "7z"
    assert job["songs"] == [
        {"folder": "pack/曲A", "staged": True, "charts": 1},
        {"folder": "pack/曲B", "staged": True, "charts": 1},
    ]
    assert list(constant.IMPORT_DIR.iterdir()) == []
    commit(client, job)
    assert sorted(r[1] for r in song_rows()) == ["曲A", "曲B"]


@needs_7z
def test_upload_7z_top_level_chart(client, monkeypatch):
    admin_login(client, monkeypatch)
    upload_id = send(client, "solo.7z", (DATA / "solo.7z").read_bytes())
    job = wait(client, client.post(f"/api/admin/import/uploads/{upload_id}/finish").json())
    assert job["songs"] == [{"folder": "solo", "staged": True, "charts": 1}]
    commit(client, job)
    assert [r[1] for r in song_rows()] == ["solo"]


@needs_7z
@pytest.mark.parametrize("name,data", [
    ("locked.7z", (DATA / "locked.7z").read_bytes()),
    # 다른 형식을 확장자만 바꾼 파일도 받지 않습니다.
    ("fake.7z", (DATA / "solo.rar").read_bytes()),
])
def test_upload_bad_7z_fails(client, monkeypatch, name, data):
    admin_login(client, monkeypatch)
    upload_id = send(client, name, data)
    job = wait(client, client.post(f"/api/admin/import/uploads/{upload_id}/finish").json())
    assert job["status"] == "failed" and "7z" in job["error"], job
    assert job["stage_id"] is None
    assert list(constant.IMPORT_DIR.iterdir()) == []


@needs_7z
def test_tmp_7z(client, monkeypatch):
    admin_login(client, monkeypatch)
    constant.TMP_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy(DATA / "pack.7z", constant.TMP_DIR / "pack.7z")
    # 풀지 못한 7z는 남겨 둡니다.
    shutil.copy(DATA / "locked.7z", constant.TMP_DIR / "locked.7z")
    job = wait(client, client.post("/api/admin/import/tmp").json())
    assert job["status"] == "done", job
    assert sorted(s["folder"] for s in job["songs"] if s.get("staged")) == ["pack/曲A", "pack/曲B"]
    assert [s["folder"] for s in job["songs"] if "error" in s] == ["locked.7z"]
    assert sorted(p.name for p in constant.TMP_DIR.iterdir()) == ["locked.7z"]
    commit(client, job)
    assert sorted(r[1] for r in song_rows()) == ["曲A", "曲B"]
