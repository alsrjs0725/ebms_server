"""7z 곡 임포트 테스트. tests/data의 7z는 7-Zip 23.01로 만들었습니다(locked.7z는 암호 X)."""
import os
import pathlib
import shutil
import subprocess

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


def make_7z(dest: pathlib.Path, entries: dict[bytes, bytes]) -> None:
    """Linux에서 UTF-8이 아닌 이름(바이트 그대로)을 넣어 7z를 만듭니다. 7-Zip은 그 바이트를 그대로 되살려 풉니다."""
    src = dest.with_suffix(".src")
    for name, body in entries.items():
        path = os.path.join(os.fsencode(src), name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(body)
    subprocess.run([sevenzip._binary(), "a", "-bso0", "-bsp0", str(dest), "."], cwd=src, check=True,
                   env={**os.environ, "LC_ALL": "C.UTF-8"})
    shutil.rmtree(src)


@needs_7z
@pytest.mark.parametrize("encoding,names", [
    ("cp932", ["曲ー", "ソナタ"]),
    ("cp949", ["노래", "곡이름"]),
])
def test_tmp_7z_non_utf8_names(client, monkeypatch, encoding, names):
    admin_login(client, monkeypatch)
    constant.TMP_DIR.mkdir(parents=True, exist_ok=True)
    make_7z(constant.TMP_DIR / "pack.7z", {
        f"{name}/{name}.bms".encode(encoding): f"#TITLE {name}\n".encode(encoding) for name in names
    })
    job = wait(client, client.post("/api/admin/import/tmp").json())
    assert job["status"] == "done", job
    assert sorted(s["folder"] for s in job["songs"]) == sorted(f"pack/{name}" for name in names), job
    assert list(constant.TMP_DIR.iterdir()) == []


@needs_7z
def test_tmp_7z_unknown_name_encoding(client, monkeypatch):
    admin_login(client, monkeypatch)
    constant.TMP_DIR.mkdir(parents=True, exist_ok=True)
    make_7z(constant.TMP_DIR / "bad.7z", {b"\x81 x/a.bms": b"#TITLE A\n"})
    job = wait(client, client.post("/api/admin/import/tmp").json())
    # 대체 인코딩으로 풀지 않고 7z만 실패로 남깁니다.
    assert job["status"] == "done", job
    [song] = job["songs"]
    assert song["folder"] == "bad.7z" and "인코딩" in song["error"], song
    assert [p.name for p in constant.TMP_DIR.iterdir()] == ["bad.7z"]


def test_tmp_non_utf8_folder(client, monkeypatch):
    """var/tmp에 UTF-8이 아닌 이름의 곡 폴더가 있어도 묶음 저장이 깨지지 않고 그 곡만 실패합니다."""
    admin_login(client, monkeypatch)
    constant.TMP_DIR.mkdir(parents=True, exist_ok=True)
    bad = os.path.join(os.fsencode(constant.TMP_DIR), "曲ー".encode("cp932"))
    os.mkdir(bad)
    with open(os.path.join(bad, b"a.bms"), "wb") as f:
        f.write(b"#TITLE A\n")
    (constant.TMP_DIR / "ok").mkdir()
    (constant.TMP_DIR / "ok" / "b.bms").write_bytes(b"#TITLE B\n")
    job = wait(client, client.post("/api/admin/import/tmp").json())
    assert job["status"] == "done", job
    assert [s["folder"] for s in job["songs"] if s.get("staged")] == ["ok"]
    [failed] = [s for s in job["songs"] if "error" in s]
    assert failed["folder"] == "\\udc8bȁ[" and "UTF-8" in failed["error"], failed
    assert os.path.isdir(bad)


def test_tmp_failure_names_source(client, monkeypatch, caplog):
    """var/tmp 가져오기가 예상 못 한 오류로 멈추면 어느 압축 파일에서였는지 오류와 로그에 남깁니다."""
    admin_login(client, monkeypatch)
    constant.TMP_DIR.mkdir(parents=True, exist_ok=True)
    (constant.TMP_DIR / "ok").mkdir()
    (constant.TMP_DIR / "ok" / "b.bms").write_bytes(b"#TITLE B\n")
    shutil.copy(DATA / "pack.7z", constant.TMP_DIR / "pack.7z")

    def boom(path, dest):
        raise RuntimeError("boom")

    monkeypatch.setattr(sevenzip, "extract", boom)
    with caplog.at_level("INFO", logger="ebms_server"):
        job = wait(client, client.post("/api/admin/import/tmp").json())
    assert job["status"] == "failed", job
    assert "(var/tmp/pack.7z)" in job["error"], job
    assert "staging: staged[ok] 1 charts" in caplog.text
    assert "staging: end[" in caplog.text and "var/tmp folders (1): 1 staged, 0 failed" in caplog.text
    assert "import job failed[var/tmp] while processing [var/tmp/pack.7z]" in caplog.text
