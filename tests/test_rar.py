"""rar(분할 압축 포함) 곡 임포트 테스트. tests/data의 rar는 RARLAB rar로 만들었습니다(pack: 16KB 볼륨 3개)."""
import pathlib
import shutil

import pytest

from ebms_server import constant, rar

from test_auth import client, oauth  # noqa: F401  (fixture 재사용)
from test_import import commit, import_dir, song_rows, wait  # noqa: F401
from test_quota import admin_login

DATA = pathlib.Path(__file__).parent / "data"
PACK = ["pack.part1.rar", "pack.part2.rar", "pack.part3.rar"]

needs_unrar = pytest.mark.skipif(shutil.which(rar.UNRAR) is None, reason="unrar not installed")


def test_volume_sets():
    names = ["a.part2.rar", "A.part1.rar", "b.rar", "b.r00", "b.r01", "c.zip", "d.part01.rar"]
    assert rar.volume_sets(names) == {
        ("old", "b"): ["b.rar", "b.r00", "b.r01"],
        ("part", "a"): ["A.part1.rar", "a.part2.rar"],
        ("part", "d"): ["d.part01.rar"],
    }
    assert rar.first_volume(["a.part2.rar", "A.part1.rar"]) == "A.part1.rar"
    assert rar.first_volume(["b.r00", "b.rar"]) == "b.rar"
    with pytest.raises(rar.RarError):
        rar.first_volume(["a.part2.rar", "a.part3.rar"])
    with pytest.raises(rar.RarError):
        rar.first_volume(["b.r00", "b.r01"])
    assert rar.stem("pack.part1.rar") == "pack" and rar.stem("x.r00") == "x" and rar.stem("y.RAR") == "y"


def send(client, name, data, chunk=10000):
    r = client.post("/api/admin/import/uploads", json={"filename": name, "size": len(data)})
    assert r.status_code == 200, r.text
    upload_id = r.json()["id"]
    for offset in range(0, len(data), chunk):
        r = client.put(f"/api/admin/import/uploads/{upload_id}", params={"offset": offset}, content=data[offset:offset + chunk])
        assert r.status_code == 200, r.text
    return upload_id


@needs_unrar
def test_upload_multivolume_rar(client, monkeypatch):
    admin_login(client, monkeypatch)
    # 올리는 순서와 상관없이 첫 볼륨부터 풉니다.
    ids = [send(client, name, (DATA / name).read_bytes()) for name in reversed(PACK)]
    r = client.post("/api/admin/import/uploads/finish", json={"ids": ids})
    assert r.status_code == 200, r.text
    job = wait(client, r.json())
    assert job["status"] == "done", job
    assert job["kind"] == "rar"
    assert job["songs"] == [
        {"folder": "pack/曲A", "staged": True, "charts": 1},
        {"folder": "pack/曲B", "staged": True, "charts": 1},
    ]
    assert list(constant.IMPORT_DIR.iterdir()) == []
    commit(client, job)
    assert sorted(r[1] for r in song_rows()) == ["曲A", "曲B"]


@needs_unrar
def test_upload_single_rar(client, monkeypatch):
    admin_login(client, monkeypatch)
    upload_id = send(client, "solo.rar", (DATA / "solo.rar").read_bytes())
    job = wait(client, client.post(f"/api/admin/import/uploads/{upload_id}/finish").json())
    assert job["songs"] == [{"folder": "solo", "staged": True, "charts": 1}]
    commit(client, job)
    assert [r[1] for r in song_rows()] == ["solo"]


@needs_unrar
def test_upload_missing_volume_fails(client, monkeypatch):
    admin_login(client, monkeypatch)
    ids = [send(client, name, (DATA / name).read_bytes()) for name in PACK[:2]]
    job = wait(client, client.post("/api/admin/import/uploads/finish", json={"ids": ids}).json())
    assert job["status"] == "failed" and "rar" in job["error"]
    assert job["stage_id"] is None
    assert list(constant.IMPORT_DIR.iterdir()) == []


def test_upload_finish_rejects_mixed(client, monkeypatch):
    admin_login(client, monkeypatch)
    a = send(client, "pack.part2.rar", b"x")
    b = send(client, "pack.part3.rar", b"y")
    r = client.post("/api/admin/import/uploads/finish", json={"ids": [a, b]})
    assert r.status_code == 400 and "첫 번째 볼륨" in r.json()["detail"]
    c = send(client, "other.zip", b"z")
    r = client.post("/api/admin/import/uploads/finish", json={"ids": [a, c]})
    assert r.status_code == 400
    # 거절한 업로드는 그대로 남아 다시 끝낼 수 있습니다.
    assert client.put(f"/api/admin/import/uploads/{a}", params={"offset": 1}, content=b"").status_code == 200


@needs_unrar
def test_tmp_rar_volumes(client, monkeypatch):
    admin_login(client, monkeypatch)
    constant.TMP_DIR.mkdir(parents=True, exist_ok=True)
    # 옛 이름 방식(.rar, .r00, .r01)
    for name, new in zip(PACK, ["old.rar", "old.r00", "old.r01"]):
        shutil.copy(DATA / name, constant.TMP_DIR / new)
    # 볼륨이 빠진 묶음은 남겨 둡니다.
    shutil.copy(DATA / PACK[0], constant.TMP_DIR / "broken.part1.rar")
    job = wait(client, client.post("/api/admin/import/tmp").json())
    assert job["status"] == "done", job
    staged = sorted(s["folder"] for s in job["songs"] if s.get("staged"))
    assert staged == ["old/曲A", "old/曲B"]
    assert [s["folder"] for s in job["songs"] if "error" in s] == ["broken.part1.rar"]
    assert sorted(p.name for p in constant.TMP_DIR.iterdir()) == ["broken.part1.rar"]
    commit(client, job)
    assert sorted(r[1] for r in song_rows()) == ["曲A", "曲B"]
