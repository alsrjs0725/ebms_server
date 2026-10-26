"""관리자 곡 임포트(zip 조각 업로드, var/tmp 가져오기) 테스트."""
import io
import json
import os
import threading
import time
import zipfile
from collections import OrderedDict

import pytest

from ebms_server import constant, db as db_module, importer

from test_auth import client, oauth  # noqa: F401  (fixture 재사용)
from test_client_auth import client_login
from test_quota import admin_login


@pytest.fixture(autouse=True)
def import_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(constant, "IMPORT_DIR", tmp_path / "import")
    # 작업 목록은 모듈 전역이라 다른 테스트의 작업이 섞이지 않도록 비웁니다.
    monkeypatch.setattr(importer, "_jobs", OrderedDict())


class Cp932ZipInfo(zipfile.ZipInfo):
    """일본어 Windows에서 만든 zip처럼 UTF-8 플래그 없이 cp932 바이트로 이름을 저장합니다."""

    def _encodeFilenameFlags(self):
        return self.filename.encode("cp932"), self.flag_bits & ~0x800


class Cp949ZipInfo(zipfile.ZipInfo):
    """한국어 Windows에서 만든 zip처럼 UTF-8 플래그 없이 cp949 바이트로 이름을 저장합니다."""

    def _encodeFilenameFlags(self):
        return self.filename.encode("cp949"), self.flag_bits & ~0x800


def make_zip(entries: dict[str, bytes], cp932: bool = False) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, body in entries.items():
            zf.writestr((Cp932ZipInfo if cp932 else zipfile.ZipInfo)(name), body)
    data = buf.getvalue()
    if cp932:
        assert all(not i.flag_bits & 0x800 for i in zipfile.ZipFile(io.BytesIO(data)).infolist())
    return data


def wait(client, job):
    for _ in range(200):
        if job["status"] != "running":
            return job
        time.sleep(0.02)
        job = client.get(f"/api/admin/import/jobs/{job['id']}").json()
    raise AssertionError("job did not finish")


def commit(client, job, slow=False):
    """대기 묶음에 넣은 작업(job)의 묶음을 그대로(자동 판정) commit하고 결과를 반환합니다. 묶음이 없으면 job 그대로."""
    if job["status"] != "done" or not job["stage_id"]:
        return job
    r = client.post(f"/api/admin/import/stages/{job['stage_id']}/commit", params={"slow": slow})
    assert r.status_code == 200, r.text
    return wait(client, r.json())


def import_tmp(client, slow=False):
    """var/tmp를 대기 묶음으로 옮기고 그대로 commit한 결과."""
    r = client.post("/api/admin/import/tmp")
    assert r.status_code == 200, r.text
    return commit(client, wait(client, r.json()), slow)


def upload(client, name, data, chunk=100):
    """조각으로 올려 대기 묶음에 넣고, 그대로 commit한 결과를 반환합니다."""
    return commit(client, stage(client, name, data, chunk))


def stage(client, name, data, chunk=100):
    """조각으로 올리고 대기 묶음에 넣는 작업이 끝날 때까지 기다린 결과를 반환합니다."""
    r = client.post("/api/admin/import/uploads", json={"filename": name, "size": len(data)})
    assert r.status_code == 200, r.text
    upload_id = r.json()["id"]
    for offset in range(0, len(data), chunk):
        r = client.put(f"/api/admin/import/uploads/{upload_id}", params={"offset": offset}, content=data[offset:offset + chunk])
        assert r.status_code == 200, r.text
        assert r.json()["received"] == min(len(data), offset + chunk)
    r = client.post(f"/api/admin/import/uploads/{upload_id}/finish")
    assert r.status_code == 200, r.text
    return wait(client, r.json())


def song_rows():
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("SELECT id, folder FROM song ORDER BY id")
        return cur.fetchall()


def test_import_requires_admin(client, monkeypatch):
    start = {"filename": "a.zip", "size": 1}
    assert client.get("/admin/import", follow_redirects=False).status_code == 303
    assert client.post("/api/admin/import/uploads", json=start).status_code == 401
    client_login(client, monkeypatch)
    assert client.get("/admin/import").status_code == 403
    assert client.post("/api/admin/import/uploads", json=start).status_code == 403
    assert client.put("/api/admin/import/uploads/x", params={"offset": 0}, content=b"x").status_code == 403
    assert client.post("/api/admin/import/uploads/x/finish").status_code == 403
    assert client.post("/api/admin/import/tmp").status_code == 403
    assert client.get("/api/admin/import/jobs").status_code == 403


def test_import_zip(client, monkeypatch):
    admin_login(client, monkeypatch)
    assert client.get("/admin/import").status_code == 200

    # 최상위에 차트가 있는 zip
    flat = upload(client, "flat.zip", make_zip({"a.bms": b"#TITLE A\n", "bga/movie.bin": os.urandom(1000)}))
    assert flat["status"] == "done" and flat["error"] is None
    assert flat["total"] == 1 and flat["done"] == 1
    assert flat["songs"] == [{"folder": "flat", "song_id": 1, "new_song": True, "charts": 1, "new_charts": 1}]

    # 곡 폴더 두 개가 든 zip(cp932 이름). 곡 안의 하위 폴더 차트는 같은 곡이고, 폴더 밖 경로는 버립니다.
    multi = upload(client, "multi.zip", make_zip({
        "pack/曲1/b.bme": b"#TITLE B\n",
        "pack/曲1/音.wav": os.urandom(500),
        "pack/曲1/sub/b2.bms": b"#TITLE B2\n",
        "pack/曲2/c.bms": b"#TITLE C\n",
        "pack/曲2/c2.bms": b"#TITLE A\n",
        "pack/readme.txt": b"hi",
        "../evil.bms": b"#EVIL\n",
    }, cp932=True))
    assert multi["status"] == "done" and multi["error"] is None
    assert multi["songs"] == [
        {"folder": "multi/pack/曲1", "song_id": 2, "new_song": True, "charts": 1, "new_charts": 1},
        # 같은 차트(a.bms)가 있어 기존 곡 1에 연결되고, 곡 1의 zip에 없는 파일은 그 zip에 더합니다.
        {"folder": "multi/pack/曲2", "song_id": 1, "new_song": False, "charts": 2, "new_charts": 1,
         "added_files": ["c.bms", "c2.bms"]},
    ]

    bad = upload(client, "bad.zip", b"not a zip")
    assert bad["status"] == "failed" and bad["error"]
    empty = upload(client, "empty.zip", make_zip({"readme.txt": b"hi"}))
    assert empty["status"] == "done" and empty["total"] == 0 and empty["error"]

    assert [tuple(row) for row in song_rows()] == [(1, "flat"), (2, "曲1")]
    with zipfile.ZipFile(io.BytesIO(db_module.Database().get_song_data(2))) as zf:
        assert sorted(zf.namelist()) == ["b.bme", "sub/b2.bms", "音.wav"]
    # 받은 zip과 임시 폴더는 남기지 않습니다.
    assert not any(constant.IMPORT_DIR.iterdir())
    # 최근 작업부터 보여줍니다.
    # commit한 묶음은 지웁니다.
    assert client.get("/api/admin/import/stages").json() == []
    # 최근 작업부터 보여줍니다(올린 zip을 푸는 작업과 commit 작업).
    jobs = client.get("/api/admin/import/jobs").json()
    assert [j["name"] for j in jobs if j["kind"] == "zip"] == ["empty.zip", "bad.zip", "multi.zip", "flat.zip"]
    assert [j["name"] for j in jobs if j["kind"] == "commit"] == ["multi.zip", "flat.zip"]


def test_upload_chunks(client, monkeypatch):
    admin_login(client, monkeypatch)
    upload_id = client.post("/api/admin/import/uploads", json={"filename": "a.zip", "size": 10}).json()["id"]
    url = f"/api/admin/import/uploads/{upload_id}"
    # 조각은 순서와 상관없이 제자리에 쓰고, 같은 구간을 다시 보내면 덮어씁니다.
    assert client.put(url, params={"offset": 5}, content=b"67890").json()["received"] == 5
    assert client.put(url, params={"offset": 5}, content=b"67890").json()["received"] == 5
    # 선언한 크기를 넘으면 400, 덜 받았으면 finish 409
    assert client.put(url, params={"offset": 5}, content=b"123456").status_code == 400
    assert client.put(url, params={"offset": 11}, content=b"").status_code == 400
    assert client.post(f"{url}/finish").status_code == 409
    assert client.put(url, params={"offset": 0}, content=b"12345").json()["received"] == 10
    path = next(p for p in constant.IMPORT_DIR.iterdir() if p.suffix == ".upload")
    assert path.read_bytes() == b"1234567890"
    assert client.delete(url).status_code == 200
    assert client.put(url, params={"offset": 5}, content=b"1").status_code == 404
    assert not any(constant.IMPORT_DIR.iterdir())

    # 디스크가 모자라면 시작하지 않습니다.
    monkeypatch.setattr(importer, "DISK_MARGIN", 1 << 62)
    r = client.post("/api/admin/import/uploads", json={"filename": "a.zip", "size": 10})
    assert r.status_code == 507


def test_import_tmp(client, monkeypatch):
    admin_login(client, monkeypatch)
    song = constant.TMP_DIR / "group" / "s1"
    song.mkdir(parents=True)
    (song / "a.bms").write_bytes(b"#TITLE T\n")
    (constant.TMP_DIR / "loose.bms").write_bytes(b"#LOOSE\n")  # var/tmp 바로 아래 파일은 보지 않습니다.

    job = import_tmp(client)
    assert job["songs"] == [{"folder": "group/s1", "song_id": 1, "new_song": True, "charts": 1, "new_charts": 1}]
    assert not song.exists()
    assert (constant.TMP_DIR / "loose.bms").exists()
    assert import_tmp(client)["songs"] == []


def test_import_tmp_zip(client, monkeypatch):
    """내부망으로 서버에 직접 복사한 zip도 var/tmp에서 등록합니다."""
    admin_login(client, monkeypatch)
    (constant.TMP_DIR / "pack.zip").write_bytes(make_zip({"p/s1/a.bms": b"#TITLE 1\n", "p/s2/b.bms": b"#TITLE 2\n"}))
    (constant.TMP_DIR / "copying.zip").write_bytes(b"PK\x03\x04partial")  # 아직 복사 중인 zip

    staged = wait(client, client.post("/api/admin/import/tmp").json())
    assert staged["status"] == "done" and staged["total"] == 2
    assert [s["folder"] for s in staged["songs"]] == ["copying.zip", "pack/p/s1", "pack/p/s2"]
    assert staged["songs"][0]["error"]
    job = commit(client, staged)
    assert [s["new_song"] for s in job["songs"]] == [True, True]
    # 다 등록한 zip은 지우고, 읽지 못한 zip은 남깁니다.
    assert not (constant.TMP_DIR / "pack.zip").exists()
    assert (constant.TMP_DIR / "copying.zip").exists()


def test_safe_parts():
    assert importer._safe_parts("a/b.bms") == ["a", "b.bms"]
    assert importer._safe_parts("a\\.\\b.bms") == ["a", "b.bms"]
    for bad in ("/etc/x", "../x", "a/../../x", "C:/x", "C:x"):
        assert importer._safe_parts(bad) is None, bad


def test_import_tmp_skips_symlinks(client, monkeypatch, tmp_path):
    """곡 폴더 안의 심볼릭 링크(파일·폴더)는 따라가지 않고 건너뜁니다(#56)."""
    admin_login(client, monkeypatch)
    outside = tmp_path / "outside"
    (outside / "dir").mkdir(parents=True)
    secret = b"EBMS_SECRET_KEY=leaked"
    (outside / "secret.txt").write_bytes(secret)
    (outside / "dir" / "inner.wav").write_bytes(secret)
    (outside / "song").mkdir()
    (outside / "song" / "o.bms").write_bytes(b"#TITLE OUT\n")

    song = constant.TMP_DIR / "group" / "s1"
    song.mkdir(parents=True)
    (song / "a.bms").write_bytes(b"#TITLE T\n")
    (song / "real.wav").write_bytes(b"wav")
    os.symlink(outside / "secret.txt", song / "leak.ogg")
    os.symlink(outside / "secret.txt", song / "z.bms")
    os.symlink(outside / "dir", song / "bga")
    # 차트가 링크뿐인 폴더는 곡이 아닙니다.
    only_link = constant.TMP_DIR / "group" / "s2"
    only_link.mkdir()
    os.symlink(outside / "song" / "o.bms", only_link / "o.bms")
    # var/tmp 바로 아래의 폴더 링크도 따라가지 않습니다.
    os.symlink(outside / "song", constant.TMP_DIR / "linked")
    os.symlink(outside / "secret.txt", constant.TMP_DIR / "linked.zip")

    job = import_tmp(client)
    assert job["songs"] == [{"folder": "group/s1", "song_id": 1, "new_song": True, "charts": 1, "new_charts": 1}]
    with zipfile.ZipFile(io.BytesIO(db_module.Database().get_song_data(1))) as zf:
        assert sorted(zf.namelist()) == ["a.bms", "real.wav"]
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("SELECT id FROM chart_chunk")
        assert all(secret not in db_module.Database().get_chart_chunk_data(row[0]) for row in cur.fetchall())
        cur.execute("SELECT COUNT(*) FROM chart")
        assert cur.fetchone()[0] == 1
    # 링크 대상은 지우지 않습니다.
    assert (outside / "secret.txt").read_bytes() == secret
    assert (outside / "song" / "o.bms").exists()


def test_create_zip_skips_symlinks(tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_bytes(b"secret")
    song = tmp_path / "song"
    (song / "sub").mkdir(parents=True)
    (song / "a.bms").write_bytes(b"#TITLE\n")
    (song / "sub" / "x.wav").write_bytes(b"x")
    os.symlink(outside, song / "leak.ogg")
    os.symlink(outside, song / "sub" / "leak.wav")
    os.symlink(tmp_path, song / "up")
    data = db_module.Database.create_zip(None, song)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert sorted(zf.namelist()) == ["a.bms", "sub/x.wav"]
    assert [p.name for p in db_module.chart_files(song)] == ["a.bms"]


def test_import_tmp_slow(client, monkeypatch):
    """천천히 모드는 작은 배치로 커밋하고 커밋마다 쉽니다."""
    admin_login(client, monkeypatch)
    monkeypatch.setattr(constant, "IMPORT_SLOW_BATCH_SONGS", 1)
    rests = []
    monkeypatch.setattr(importer.Job, "rest", lambda self, batch: rests.append(batch.open))
    for i in range(3):
        song = constant.TMP_DIR / "g" / f"s{i}"
        song.mkdir(parents=True)
        (song / "a.bms").write_bytes(f"#TITLE {i}\n".encode())

    job = import_tmp(client, slow=True)
    assert job["status"] == "done" and job["slow"]
    assert [s["new_song"] for s in job["songs"]] == [True, True, True]
    # 곡마다 커밋해 쓰기 잠금을 놓은 뒤 쉽니다.
    assert rests == [False, False, False]


def test_job_rest_and_speed_change(client, monkeypatch):
    monkeypatch.setattr(constant, "IMPORT_SLOW_REST_MAX", 5)
    batch = db_module.Database().batch()
    job = importer.Job("tmp", "var/tmp", slow=True)
    job.apply_limits(batch)
    assert batch.max_songs == constant.IMPORT_SLOW_BATCH_SONGS

    # 쉬는 중에 보통 속도로 바꾸면 바로 이어서 진행합니다.
    job._work_since -= 100
    started = time.monotonic()
    threading.Timer(0.1, lambda: setattr(job, "slow", False)).start()
    job.rest(batch)
    assert time.monotonic() - started < 2
    assert not job.slow and not job.json()["resting"]
    job.apply_limits(batch)
    assert batch.max_songs == constant.IMPORT_BATCH_SONGS


def test_set_job_speed(client, monkeypatch):
    admin_login(client, monkeypatch)
    job = importer.Job("zip", "a.zip")
    with importer._lock:
        importer._jobs[job.id] = job
    r = client.put(f"/api/admin/import/jobs/{job.id}/speed", json={"slow": True})
    assert r.status_code == 200 and r.json()["slow"]
    assert job.slow
    assert client.put("/api/admin/import/jobs/nope/speed", json={"slow": True}).status_code == 404


def _name_zip(info_cls, names: list[str]) -> zipfile.ZipFile:
    return _mixed_name_zip([(info_cls, name) for name in names])


def _mixed_name_zip(entries: list[tuple[type, str]]) -> zipfile.ZipFile:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for info_cls, name in entries:
            zf.writestr(info_cls(name), b"")
    return zipfile.ZipFile(buf)


def test_zip_name_encoding():
    # cp932로도 오류 없이 풀리는 한국어 이름(반각 가타카나로 깨짐)이 섞여 있어도 zip 전체로 보고 cp949를 고릅니다.
    zf = _name_zip(Cp949ZipInfo, ["곡/효과음.wav", "곡/베이스.wav", "곡/노래.bms"])
    assert importer._zip_name_encoding(zf) == "cp949"
    assert sorted(parts for g in importer._song_groups(zf).values() for _, parts in g) == [
        ["노래.bms"], ["베이스.wav"], ["효과음.wav"]]
    # cp949로도 풀리는 일본어 이름(확장 한글로 깨짐)은 cp932입니다.
    assert importer._zip_name_encoding(_name_zip(Cp932ZipInfo, ["夜想曲/音.wav", "夜想曲/a.bms"])) == "cp932"
    # 반각 가타카나뿐인 일본어 이름은 cp949로 풀리지 않으므로 cp932입니다.
    assert importer._zip_name_encoding(_name_zip(Cp932ZipInfo, ["ｱｲｳ/ﾄﾞﾗﾑ.wav"])) == "cp932"
    # ASCII 이름은 utf-8, UTF-8 플래그가 있으면 고를 필요가 없습니다.
    assert importer._zip_name_encoding(_name_zip(Cp932ZipInfo, ["song/a.bms"])) == "utf-8"
    assert importer._zip_name_encoding(_name_zip(zipfile.ZipInfo, ["곡/a.bms"])) is None


def test_zip_name_encoding_unknown():
    # 한 인코딩으로 다 풀리지 않으면 항목별로 대신 풀지 않고 오류를 냅니다.
    zf = _mixed_name_zip([(Cp932ZipInfo, "曲/ドラム.wav"), (Cp949ZipInfo, "曲/노래.bms")])
    with pytest.raises(importer.ZipNameEncodingError, match="cp932"):
        importer._song_groups(zf)


def test_upload_resumes_after_restart(client, monkeypatch):
    """받던 업로드는 서버를 다시 시작해도 받은 곳부터 이어 받습니다."""
    admin_login(client, monkeypatch)
    data = make_zip({"s/a.bms": b"#TITLE A\n"})
    r = client.post("/api/admin/import/uploads", json={"filename": "r.zip", "size": len(data)})
    upload_id = r.json()["id"]
    half = len(data) // 2
    assert client.put(f"/api/admin/import/uploads/{upload_id}", params={"offset": 0}, content=data[:half]).status_code == 200
    stray = constant.IMPORT_DIR / "leftover"
    stray.mkdir()
    # 다시 시작: 메모리의 업로드 목록이 비고, recover가 디스크에서 다시 읽습니다.
    importer._uploads.clear()
    importer.recover()
    assert not stray.exists()
    # 받은 구간은 다시 시작해도 남아 있어 나머지만 보내면 됩니다.
    assert client.post(f"/api/admin/import/uploads/{upload_id}/finish").status_code == 409
    assert client.put(f"/api/admin/import/uploads/{upload_id}", params={"offset": half}, content=data[half:]).status_code == 200
    job = wait(client, client.post(f"/api/admin/import/uploads/{upload_id}/finish").json())
    assert job["status"] == "done" and job["stage_id"]
    assert list(constant.IMPORT_DIR.iterdir()) == []


def test_upload_parallel_chunks_out_of_order(client, monkeypatch):
    """조각을 거꾸로(동시에) 보내도 제자리에 써서 원래 파일이 됩니다. 이전 버전의 받던 업로드(구간 없음)도 이어 받습니다."""
    admin_login(client, monkeypatch)
    data = make_zip({"s/a.bms": b"#TITLE A\n", "s/b.wav": os.urandom(5000)})
    upload_id = client.post("/api/admin/import/uploads", json={"filename": "p.zip", "size": len(data)}).json()["id"]
    url = f"/api/admin/import/uploads/{upload_id}"
    step = 1000
    for offset in reversed(range(0, len(data), step)):
        assert client.put(url, params={"offset": offset}, content=data[offset:offset + step]).status_code == 200
    job = wait(client, client.post(f"{url}/finish").json())
    assert job["status"] == "done" and job["stage_id"]

    upload_id = client.post("/api/admin/import/uploads", json={"filename": "q.zip", "size": len(data)}).json()["id"]
    url = f"/api/admin/import/uploads/{upload_id}"
    path = constant.IMPORT_DIR / f"{upload_id}.upload"
    path.write_bytes(data[:step])
    (constant.IMPORT_DIR / f"{upload_id}.json").write_text(json.dumps({"filename": "q.zip", "size": len(data)}))
    importer._uploads.clear()
    importer.recover()
    assert client.put(url, params={"offset": step}, content=data[step:]).json()["received"] == len(data)
    job = wait(client, client.post(f"{url}/finish").json())
    assert job["status"] == "done"

