"""곡 임포트 대기(staged): 올린 곡을 검토하고 대상을 바꾼 뒤 commit하는 흐름 테스트."""
import io
import json
import zipfile

import pytest

from ebms_server import constant, db as db_module, importer, staging

from test_auth import client, oauth  # noqa: F401  (fixture 재사용)
from test_import import commit, import_dir, make_zip, song_rows, stage, upload, wait  # noqa: F401
from test_quota import admin_login


def detail(client, stage_id, **params):
    r = client.get(f"/api/admin/import/stages/{stage_id}", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def units(client, stage_id):
    return {u["folder"]: u for u in detail(client, stage_id)["items"]}


def set_target(client, stage_id, unit, target, charts=None):
    return client.put(
        f"/api/admin/import/stages/{stage_id}/units/{unit['id']}", json={"target": target, "charts": charts}
    )


def commit_stage(client, stage_id):
    r = client.post(f"/api/admin/import/stages/{stage_id}/commit")
    assert r.status_code == 200, r.text
    return wait(client, r.json())


def song_files(song_id):
    with zipfile.ZipFile(io.BytesIO(db_module.Database().get_song_data(song_id))) as zf:
        return sorted(zf.namelist())


def chart_owner(body):
    import hashlib
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("SELECT song_id FROM chart WHERE id = %s", (hashlib.sha256(body).hexdigest(),))
        row = cur.fetchone()
        return row and row[0]


def test_stage_requires_admin(client, monkeypatch):
    assert client.get("/api/admin/import/stages").status_code == 401
    assert client.post("/api/admin/import/stages/ab/commit").status_code == 401


def test_upload_is_staged_until_commit(client, monkeypatch):
    admin_login(client, monkeypatch)
    upload(client, "base.zip", make_zip({"a.bms": b"#TITLE A\n", "k.wav": b"k"}))

    job = stage(client, "pack.zip", make_zip({
        "p/new/n.bms": b"#TITLE N\n",
        "p/add/a.bms": b"#TITLE A\n",
        "p/add/extra.bms": b"#TITLE A2\n",
    }))
    assert job["status"] == "done" and job["stage_id"]
    assert [s["staged"] for s in job["songs"]] == [True, True]
    # 아직 DB에는 없습니다.
    assert [tuple(r) for r in song_rows()] == [(1, "base")]

    stages = client.get("/api/admin/import/stages").json()
    assert len(stages) == 1 and stages[0]["status"] == "ready" and stages[0]["units"] == 2
    assert stages[0]["counts"]["new"] == 1 and stages[0]["counts"]["add"] == 1
    got = units(client, job["stage_id"])
    assert got["pack/p/new"]["suggestion"] == {"kind": "new", "songs": [], "deleted": None, "merge": None, "matches": {}, "sounds": {}, "complete": False}
    assert got["pack/p/add"]["suggestion"]["kind"] == "add" and got["pack/p/add"]["suggestion"]["songs"] == [1]
    assert {c["path"]: c["song_id"] for c in got["pack/p/add"]["charts"]} == {"a.bms": 1, "extra.bms": None}
    assert detail(client, job["stage_id"])["songs"] == {"1": "base"}
    # 판정으로 거르기
    assert [u["folder"] for u in detail(client, job["stage_id"], show="new")["items"]] == ["pack/p/new"]

    done = commit_stage(client, job["stage_id"])
    assert done["status"] == "done"
    assert [tuple(r) for r in song_rows()] == [(1, "base"), (2, "new")]
    assert song_files(1) == ["a.bms", "extra.bms", "k.wav"]
    assert client.get("/api/admin/import/stages").json() == []
    assert not any(constant.STAGED_DIR.iterdir())


def test_new_song_as_addition_and_addition_as_new(client, monkeypatch):
    admin_login(client, monkeypatch)
    upload(client, "base.zip", make_zip({"a.bms": b"#TITLE A\n", "k.wav": b"k"}))
    job = stage(client, "pack.zip", make_zip({
        "p/sabun/s.bms": b"#TITLE SABUN\n",
        "p/other/a.bms": b"#TITLE A\n",
        "p/other/o.bms": b"#TITLE O\n",
        "p/other/o.wav": b"o",
    }))
    sid = job["stage_id"]
    got = units(client, sid)
    # 새 곡으로 판정된 차분을 곡 1의 추가분으로, 곡 1의 추가분으로 판정된 곡을 새 곡으로
    assert set_target(client, sid, got["pack/p/sabun"], 1).status_code == 200
    r = set_target(client, sid, got["pack/p/other"], "new")
    assert r.status_code == 200 and r.json()["target"] == "new"
    assert detail(client, sid)["counts"]["changed"] == 2
    assert [u["folder"] for u in detail(client, sid, show="changed")["items"]] == ["pack/p/other", "pack/p/sabun"]

    commit_stage(client, sid)
    assert [tuple(r) for r in song_rows()] == [(1, "base"), (2, "other")]
    assert song_files(1) == ["a.bms", "k.wav", "s.bms"]
    assert song_files(2) == ["a.bms", "o.bms", "o.wav"]
    # 이미 곡 1에 있던 차트는 곡 1에 그대로 둡니다.
    assert chart_owner(b"#TITLE A\n") == 1
    assert chart_owner(b"#TITLE O\n") == 2
    assert chart_owner(b"#TITLE SABUN\n") == 1


def test_chart_level_targets(client, monkeypatch):
    """차트 단위로 대상을 나누면 차트마다 그 차트가 가리키는 파일과 공통 파일이 함께 갑니다."""
    admin_login(client, monkeypatch)
    upload(client, "base.zip", make_zip({"a.bms": b"#TITLE A\n"}))
    job = stage(client, "mix.zip", make_zip({
        "mix/x.bms": b"#TITLE X\n#WAV01 x.wav\n",
        "mix/y.bms": b"#TITLE Y\n#WAV01 y.wav\n#BANNER banner.png\n",
        "mix/z.bms": b"#TITLE Z\n",
        "mix/x.wav": b"x",
        "mix/y.wav": b"y",
        "mix/banner.png": b"b",
        "mix/readme.txt": b"r",
    }))
    sid = job["stage_id"]
    unit = units(client, sid)["mix/mix"]
    # 곡은 새 곡, y는 곡 1에 추가, z는 넣지 않음
    r = set_target(client, sid, unit, "new", {"y.bms": 1, "z.bms": "skip"})
    assert r.status_code == 200
    assert {c["path"]: c["target"] for c in r.json()["charts"]} == {"x.bms": None, "y.bms": 1, "z.bms": "skip"}

    done = commit_stage(client, sid)
    assert [s["folder"] for s in done["songs"]] == ["mix/mix [x.bms]", "mix/mix [y.bms]"]
    assert [tuple(r) for r in song_rows()] == [(1, "base"), (2, "mix")]
    assert song_files(2) == ["readme.txt", "x.bms", "x.wav"]
    assert song_files(1) == ["a.bms", "banner.png", "readme.txt", "y.bms", "y.wav"]
    assert chart_owner(b"#TITLE Z\n") is None


def test_skip_ambiguous_and_failures_stay(client, monkeypatch):
    admin_login(client, monkeypatch)
    upload(client, "s1.zip", make_zip({"a.bms": b"#TITLE A\n", "same.wav": b"1"}))
    upload(client, "s2.zip", make_zip({"b.bms": b"#TITLE B\n"}))
    job = stage(client, "pack.zip", make_zip({
        "p/both/a.bms": b"#TITLE A\n",
        "p/both/b.bms": b"#TITLE B\n",
        "p/conflict/a.bms": b"#TITLE A\n",
        "p/conflict/same.wav": b"2",
        "p/drop/d.bms": b"#TITLE D\n",
        "p/allold/b.bms": b"#TITLE B\n",
    }))
    sid = job["stage_id"]
    got = units(client, sid)
    assert got["pack/p/both"]["suggestion"]["kind"] == "ambiguous"
    assert got["pack/p/both"]["suggestion"]["songs"] == [1, 2]
    assert set_target(client, sid, got["pack/p/drop"], "skip").status_code == 200
    # 차트가 모두 이미 등록돼 있으면 새 곡을 만들 수 없습니다(commit 때 실패로 남음).
    assert set_target(client, sid, got["pack/p/allold"], "new").status_code == 200
    # 잘못된 대상
    assert set_target(client, sid, got["pack/p/drop"], 99).status_code == 400
    assert set_target(client, sid, got["pack/p/drop"], "nope").status_code == 400
    assert set_target(client, sid, got["pack/p/drop"], "skip", {"zz.bms": "new"}).status_code == 400

    done = commit_stage(client, sid)
    by_folder = {s["folder"]: s for s in done["songs"]}
    assert by_folder["pack/p/drop"] == {"folder": "pack/p/drop", "skipped": "admin"}
    assert "여러 개" in by_folder["pack/p/both"]["error"]
    assert by_folder["pack/p/allold"]["error"]
    assert by_folder["pack/p/conflict"]["conflicts"] == ["same.wav"]
    assert chart_owner(b"#TITLE D\n") is None

    # 실패·충돌한 곡만 묶음에 남아 다시 검토합니다.
    left = detail(client, sid)
    assert sorted(u["folder"] for u in left["items"]) == ["pack/p/allold", "pack/p/both", "pack/p/conflict"]
    assert left["counts"]["failed"] == 2
    both = units(client, sid)["pack/p/both"]
    assert both["result"]["error"]
    assert set_target(client, sid, both, 2).status_code == 200
    commit_stage(client, sid)
    assert song_files(2) == ["a.bms", "b.bms"]

    assert client.delete(f"/api/admin/import/stages/{sid}").status_code == 200
    assert client.get("/api/admin/import/stages").json() == []
    assert client.get(f"/api/admin/import/stages/{sid}").status_code == 404


def test_tmp_is_staged_and_recover(client, monkeypatch):
    admin_login(client, monkeypatch)
    song = constant.TMP_DIR / "g" / "s1"
    song.mkdir(parents=True)
    (song / "a.bms").write_bytes(b"#TITLE T\n")
    job = wait(client, client.post("/api/admin/import/tmp").json())
    assert job["songs"] == [{"folder": "g/s1", "staged": True, "charts": 1}]
    assert not song.exists()
    assert song_rows() == [] or list(song_rows()) == []
    # 빈 var/tmp는 묶음을 만들지 않습니다.
    assert wait(client, client.post("/api/admin/import/tmp").json())["stage_id"] is None

    # commit 도중 꺼진 묶음은 다시 시작하면 이어서 commit합니다.
    sid = job["stage_id"]
    set_status(sid, "committing")
    (constant.STAGED_DIR / sid / "stray").mkdir()
    assert client.post(f"/api/admin/import/stages/{sid}/commit").status_code == 409
    staging.recover()
    assert not (constant.STAGED_DIR / sid / "stray").exists()
    resumed = resumed_job(client, "var/tmp")
    assert resumed["kind"] == "commit" and resumed["status"] == "done"
    assert [tuple(r) for r in song_rows()] == [(1, "s1")]
    assert not (constant.STAGED_DIR / sid).exists()


def set_status(sid, status, **fields):
    path = constant.STAGED_DIR / sid / staging.STAGE_FILE
    data = json.loads(path.read_text("utf-8"))
    data.update(status=status, **fields)
    path.write_text(json.dumps(data), "utf-8")
    return data


def resumed_job(client, name):
    """서버 시작(recover) 때 이어서 시작한 작업이 끝날 때까지 기다립니다."""
    jobs = [j for j in importer.list_jobs() if j.resumed and j.name == name]
    assert jobs, "no resumed job"
    return wait(client, jobs[0].json())


def test_staging_resumes_after_restart(client, monkeypatch):
    """zip을 풀다가 꺼지면 다시 시작할 때 마지막 저장 뒤의 곡부터 이어서 풉니다(이미 푼 곡은 다시 풀지 않음)."""
    admin_login(client, monkeypatch)
    data = make_zip({"p/a/a.bms": b"#TITLE A\n", "p/b/b.bms": b"#TITLE B\n", "p/c/c.bms": b"#TITLE C\n"})
    job = stage(client, "pack.zip", data)
    sid = job["stage_id"]
    root = constant.STAGED_DIR / sid
    assert not (root / staging.SOURCE_DIR).exists()
    # 곡 c를 푼 뒤 stage.json에 저장하기 전에 꺼진 상태로 되돌립니다.
    before = json.loads((root / staging.STAGE_FILE).read_text("utf-8"))
    kept = [u for u in before["units"] if u["folder"] != "pack/p/c"]
    lost = next(u for u in before["units"] if u["folder"] == "pack/p/c")
    set_status(sid, "staging", units=kept, analyzed_at=None)
    (root / staging.SOURCE_DIR).mkdir()
    (root / staging.SOURCE_DIR / "pack.zip").write_bytes(data)
    staging.recover()
    assert not (root / lost["id"]).exists()
    resumed = resumed_job(client, "pack.zip")
    assert resumed["status"] == "done"
    assert [s["folder"] for s in resumed["songs"]] == ["pack/p/c"]
    got = detail(client, sid)
    assert got["status"] == "ready" and not got["error"]
    assert sorted(u["folder"] for u in got["items"]) == ["pack/p/a", "pack/p/b", "pack/p/c"]
    assert {u["id"] for u in kept} <= {u["id"] for u in got["items"]}
    assert not (root / staging.SOURCE_DIR).exists()


def test_tmp_move_interrupted_returns_song(client, monkeypatch):
    """var/tmp에서 곡을 옮긴 뒤 저장 전에 꺼지면 var/tmp로 되돌렸다가 이어서 다시 옮깁니다(곡을 잃지 않음)."""
    admin_login(client, monkeypatch)
    song = constant.TMP_DIR / "g" / "s1"
    song.mkdir(parents=True)
    (song / "a.bms").write_bytes(b"#TITLE T\n")
    stage_ = staging._new_stage("tmp", "var/tmp")
    unit = constant.STAGED_DIR / stage_["id"] / "abc123"
    unit.mkdir()
    staging._move_out(song, unit, "g/s1")
    assert not song.exists()
    staging.recover()
    resumed = resumed_job(client, "var/tmp")
    assert resumed["songs"] == [{"folder": "g/s1", "staged": True, "charts": 1}]
    got = detail(client, stage_["id"])
    assert [u["folder"] for u in got["items"]] == ["g/s1"]
    assert not unit.exists()


def test_resume_gives_up_after_max(client, monkeypatch):
    """이어서 해도 계속 끝나지 않으면(작업 때문에 서버가 꺼지는 경우) MAX_RESUMES번 뒤 멈추고 검토할 수 있게 둡니다."""
    admin_login(client, monkeypatch)
    job = stage(client, "one.zip", make_zip({"s/a.bms": b"#TITLE A\n"}))
    sid = job["stage_id"]
    set_status(sid, "committing", resumes=staging.MAX_RESUMES)
    staging.recover()
    got = detail(client, sid)
    assert got["status"] == "ready" and "멈췄습니다" in got["error"]
    assert not [j for j in importer.list_jobs() if j.resumed and j.name == "one.zip"]


def test_bad_stage_id(client, monkeypatch):
    admin_login(client, monkeypatch)
    assert client.get("/api/admin/import/stages/..").status_code == 404
    assert client.get("/api/admin/import/stages/abc").status_code == 404
    assert client.delete("/api/admin/import/stages/abc").status_code == 404


def test_sort_by_file_count(client, monkeypatch):
    admin_login(client, monkeypatch)
    job = stage(client, "pack.zip", make_zip({
        "p/b/b.bms": b"#B\n", "p/b/1.wav": b"1", "p/b/2.wav": b"2",
        "p/a/a.bms": b"#A\n",
        "p/c/c.bms": b"#C\n", "p/c/1.wav": b"1",
    }))
    sid = job["stage_id"]
    order = lambda sort: [u["folder"] for u in detail(client, sid, sort=sort)["items"]]
    assert order("files_desc") == ["pack/p/b", "pack/p/c", "pack/p/a"]
    assert order("files_asc") == ["pack/p/a", "pack/p/c", "pack/p/b"]
    assert sorted(order("")) == sorted(order("nope")) == ["pack/p/a", "pack/p/b", "pack/p/c"]


def upload_new(client, name, data):
    """비슷한 곡에 붙지 않게 새 곡으로 등록합니다."""
    sid = stage(client, name, data)["stage_id"]
    for unit in units(client, sid).values():
        assert set_target(client, sid, unit, "new").status_code == 200
    commit_stage(client, sid)


def test_similar_songs_by_fields(client, monkeypatch):
    """키음이 없는 채보뿐인 곡은 항목만으로 판정합니다."""
    admin_login(client, monkeypatch)
    upload_new(client, "base.zip", make_zip({"b/a.bms": b"#TITLE Song [NORMAL]\n#ARTIST Comp / obj:x\n#BPM 150.00\n#GENRE Techno\n", "b/k.wav": b"k"}))
    upload_new(client, "other.zip", make_zip({"o/o.bms": b"#TITLE Song\n#ARTIST Someone\n#BPM 150\n"}))
    upload_new(client, "third.zip", make_zip({"t/t.bms": b"#TITLE Else\n#ARTIST Nobody\n#BPM 150\n#GENRE TECHNO\n"}))
    job = stage(client, "pack.zip", make_zip({
        # 이름·작곡가·BPM·장르 다 같은 곡(1)이 가장 많이 겹침 → auto는 1에 추가
        "small/x.bms": b"#TITLE SONG [ANOTHER]\n#ARTIST comp\n#BPM 150\n#GENRE techno\n", "small/k2.wav": b"k2",
        # 이름·BPM 같은 곡(1, 2)이 둘 → 후보만, auto는 새 곡
        "tie/x.bms": b"#TITLE Song\n#ARTIST Other\n#BPM 150\n",
        # BPM·장르만 같아도 후보(1, 3)
        "genre/x.bms": b"#TITLE Zzz\n#ARTIST Yyy\n#BPM 150\n#GENRE Techno\n",
        # 이름 하나만 같으면 후보가 아님
        "one/x.bms": b"#TITLE Song\n#ARTIST X\n",
    }))
    sid = job["stage_id"]
    got = units(client, sid)
    assert got["pack/small"]["suggestion"] == {
        "kind": "similar", "songs": [1, 2, 3], "deleted": None, "merge": 1,
        "matches": {"1": ["title", "artist", "bpm", "genre"], "2": ["title", "bpm"], "3": ["bpm", "genre"]}, "sounds": {}, "complete": False,
    }
    tie = got["pack/tie"]["suggestion"]
    assert tie["kind"] == "similar" and tie["songs"] == [1, 2] and tie["merge"] is None
    genre = got["pack/genre"]["suggestion"]
    assert genre["songs"] == [1, 3] and genre["merge"] is None
    assert got["pack/one"]["suggestion"]["kind"] == "new"
    chart = got["pack/small"]["charts"][0]
    assert (chart["title"], chart["bpm"], chart["keys"]["bpm"]) == ("SONG [ANOTHER]", "150", "150")
    assert sorted(u["folder"] for u in detail(client, sid, show="similar")["items"]) == ["pack/genre", "pack/small", "pack/tie"]
    assert [u["folder"] for u in detail(client, sid, q="yyy")["items"]] == ["pack/genre"]

    # 차트 외 파일 목록
    r = client.get(f"/api/admin/import/stages/{sid}/units/{got['pack/small']['id']}/files")
    assert r.status_code == 200 and r.json() == [{"path": "k2.wav", "size": 2}]
    assert client.get(f"/api/admin/import/stages/{sid}/units/nope/files").status_code == 404

    commit_stage(client, sid)
    assert song_files(1) == ["a.bms", "k.wav", "k2.wav", "x.bms"]
    assert [r[0] for r in song_rows()] == [1, 2, 3, 4, 5, 6]


def test_similar_songs_by_sounds(client, monkeypatch):
    admin_login(client, monkeypatch)
    upload_new(client, "base.zip", make_zip({
        "b/a.bms": b"#TITLE Song\n#ARTIST Comp\n#BPM 150\n#WAV01 kick.wav\n#WAV02 snare.wav\n",
        "b/kick.ogg": b"k", "b/sound/snare.wav": b"s",
    }))
    upload_new(client, "other.zip", make_zip({
        "o/o.bms": b"#TITLE Song\n#ARTIST Comp\n#BPM 150\n#WAV01 hat.wav\n", "o/hat.wav": b"h",
    }))
    many = {f"complete/{i}.wav": b"w" for i in range(60)}
    job = stage(client, "pack.zip", make_zip({
        # 없는 키음(kick, 확장자 무관)이 1에만 있고 1과 합쳐 100%, 항목 3개 같음 → auto는 1 (2는 kick이 없어 후보 아님)
        "full/x.bms": b"#TITLE Song [HARD]\n#ARTIST Comp\n#BPM 150\n#WAV01 kick.wav\n",
        # 이름이 달라 같은 항목이 작곡가 하나뿐 → 후보만, auto는 새 곡
        "renamed/x.bms": b"#TITLE Renamed\n#ARTIST Comp\n#WAV01 kick.ogg\n#WAV02 own.wav\n", "renamed/own.wav": b"o",
        # 5개 중 곡에 3개, 1에 kick → 80% → 후보만
        "most/x.bms": b"#TITLE Song\n#ARTIST Comp\n#WAV01 kick.wav\n#WAV02 snare.wav\n#WAV03 a.wav\n#WAV04 b.wav\n#WAV05 c.wav\n",
        "most/a.wav": b"a", "most/b.wav": b"b", "most/c.wav": b"c",
        # snare는 1에 sound/snare.wav로만 있어(경로가 다름) 1과 합쳐도 50% → 후보 아님
        "short/x.bms": b"#TITLE Song\n#ARTIST Comp\n#BPM 150\n#PLAYLEVEL 3\n#WAV01 kick.wav\n#WAV02 snare.wav\n",
        # 키음이 곡 안에 다 있으면 따로 선 곡 → 항목이 같아도 새 곡(파일 수와 무관)
        "complete/x.bms": b"#TITLE Song\n#ARTIST Comp\n#BPM 150\n#WAV01 0.wav\n", **many,
        "tiny/x.bms": b"#TITLE Song\n#ARTIST Comp\n#BPM 150\n#WAV01 t.wav\n", "tiny/t.wav": b"t",
        # 키음이 다 있어도 1이 그 키음을 혼자 다 가지면 중복 의심 후보(auto는 새 곡). 2는 키음이 없어 후보 아님
        "again/x.bms": b"#TITLE Song\n#ARTIST Comp\n#BPM 150\n#PLAYLEVEL 9\n#WAV01 kick.wav\n#WAV02 sound\\snare.wav\n",
        "again/kick.wav": b"k", "again/sound/snare.wav": b"s",
    }))
    got = units(client, job["stage_id"])
    assert got["pack/full"]["suggestion"] == {
        "kind": "similar", "songs": [1], "deleted": None, "merge": 1,
        "matches": {"1": ["title", "artist", "bpm"]}, "sounds": {"1": 1.0}, "complete": False,
    }
    renamed = got["pack/renamed"]["suggestion"]
    assert (renamed["songs"], renamed["matches"], renamed["sounds"], renamed["merge"]) == ([1], {"1": ["artist"]}, {"1": 1.0}, None)
    most = got["pack/most"]["suggestion"]
    assert (most["kind"], most["songs"], most["sounds"], most["merge"]) == ("similar", [1], {"1": 0.8}, None)
    assert got["pack/again"]["suggestion"] == {
        "kind": "similar", "songs": [1], "deleted": None, "merge": None,
        "matches": {"1": ["title", "artist", "bpm"]}, "sounds": {"1": 1.0}, "complete": True,
    }
    for folder in ("pack/short", "pack/complete", "pack/tiny"):
        assert got[folder]["suggestion"]["kind"] == "new", folder

def test_analyze_fills_meta_of_old_units_and_backfill(client, monkeypatch):
    admin_login(client, monkeypatch)
    upload(client, "base.zip", make_zip({"b/a.bms": b"#TITLE Song\n#ARTIST Comp\n"}))
    # 이전 버전에서 넣은 차트: 키가 없음
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("UPDATE chart SET title_key = NULL, artist_key = NULL, bpm_key = NULL, genre_key = NULL")
        con.commit()
    assert db_module.Database().backfill_chart_meta() == 1
    assert db_module.Database().backfill_chart_meta() == 0
    job = stage(client, "pack.zip", make_zip({"s/x.bms": b"#TITLE Song [HARD]\n#ARTIST Comp\n"}))
    sid = job["stage_id"]
    # 이전 버전에서 만든 묶음: 차트에 헤더 값이 없음
    path = constant.STAGED_DIR / sid / staging.STAGE_FILE
    data = json.loads(path.read_text("utf-8"))
    for unit in data["units"]:
        for chart in unit["charts"]:
            for key in ("title", "artist", "bpm", "genre", "keys"):
                chart.pop(key)
        unit["suggestion"] = None
    path.write_text(json.dumps(data), "utf-8")
    assert client.post(f"/api/admin/import/stages/{sid}/analyze").status_code == 200
    assert units(client, sid)["pack/s"]["suggestion"]["merge"] == 1


def test_units_are_packed_into_one_file(client, monkeypatch):
    """대기 곡은 낱개 파일로 풀지 않고 곡마다 압축하지 않은 zip 하나로 둡니다(작은 파일의 할당 단위 낭비 방지)."""
    admin_login(client, monkeypatch)
    keys = {f"p/s/k{i:03d}.wav": bytes([i]) * 10 for i in range(200)}
    job = stage(client, "pack.zip", make_zip({"p/s/a.bms": b"#TITLE A\n", **keys}))
    sid = job["stage_id"]
    unit = units(client, sid)["pack/p/s"]
    assert unit["files"] == 201
    assert sorted(p.name for p in (constant.STAGED_DIR / sid / unit["id"]).iterdir()) == [staging.PACK_FILE]
    files = client.get(f"/api/admin/import/stages/{sid}/units/{unit['id']}/files").json()
    assert len(files) == 200 and files[0] == {"path": "k000.wav", "size": 10}

    # var/tmp의 곡 폴더도 묶어 옮기고, 빈 폴더도 곡에 남습니다.
    song = constant.TMP_DIR / "t"
    (song / "bga" / "empty").mkdir(parents=True)
    (song / "b.bms").write_bytes(b"#TITLE B\n")
    (song / "bga" / "x.png").write_bytes(b"png")
    tmp = wait(client, client.post("/api/admin/import/tmp").json())
    tunit = units(client, tmp["stage_id"])["t"]
    names = {p.name for p in (constant.STAGED_DIR / tmp["stage_id"] / tunit["id"]).iterdir()}
    assert names == {staging.PACK_FILE, staging.ORIGIN_FILE}
    assert not song.exists()

    commit_stage(client, sid)
    commit_stage(client, tmp["stage_id"])
    rows = {name: song_id for song_id, name in song_rows()}
    assert len(song_files(rows["s"])) == 201
    assert song_files(rows["t"]) == ["b.bms", "bga/empty/", "bga/x.png"]


def test_old_unpacked_units_still_commit(client, monkeypatch):
    """이전 버전에서 폴더로 풀어 둔 곡도 읽고 commit합니다."""
    admin_login(client, monkeypatch)
    job = stage(client, "pack.zip", make_zip({"s/a.bms": b"#TITLE A\n", "s/k.wav": b"k"}))
    sid = job["stage_id"]
    unit = units(client, sid)["pack/s"]
    unit_root = constant.STAGED_DIR / sid / unit["id"]
    with zipfile.ZipFile(unit_root / staging.PACK_FILE) as zf:
        zf.extractall(unit_root / "s")
    (unit_root / staging.PACK_FILE).unlink()
    files = client.get(f"/api/admin/import/stages/{sid}/units/{unit['id']}/files").json()
    assert files == [{"path": "k.wav", "size": 1}]
    commit_stage(client, sid)
    assert song_files(song_rows()[0][0]) == ["a.bms", "k.wav"]


def test_restore_packed_origin(tmp_path):
    """var/tmp에서 옮겨 곡 파일로 묶은 뒤 stage.json에 저장하기 전에 꺼졌으면, 곡 파일을 var/tmp에 다시 풉니다."""
    src = constant.TMP_DIR / "g" / "s"
    song = tmp_path / "s"
    (song / "bga").mkdir(parents=True)
    (song / "a.bms").write_bytes(b"#TITLE A\n")
    (song / "bga" / "x.png").write_bytes(b"png")
    child = tmp_path / "unit"
    child.mkdir()
    staging._pack(song, child / staging.PACK_FILE)
    (child / staging.ORIGIN_FILE).write_text(json.dumps({"from": str(src), "folder": "g/s", "packed": True}), "utf-8")
    (child / "s").mkdir()  # 지우다 만 폴더
    staging._restore_origin(child)
    assert (src / "a.bms").read_bytes() == b"#TITLE A\n"
    assert (src / "bga" / "x.png").read_bytes() == b"png"


def test_chart_header_encoding_is_scored():
    """헤더 줄 전체로 인코딩을 고릅니다. cp932·cp949 양쪽으로 풀리는 이름도 오판 흔적이 적은 쪽입니다."""
    assert db_module.chart_meta("#TITLE 한글\n#ARTIST 바다\n".encode("cp949"))["title"] == "한글"
    assert db_module.chart_meta("#TITLE 夜想曲\n#WAV01 音楽.wav\n".encode("cp932"))["title"] == "夜想曲"
    assert db_module.chart_meta("#TITLE 노래\n".encode("utf-8"))["title"] == "노래"
    # '#'로 시작하지 않는 줄(주석)은 보지 않습니다.
    assert db_module.chart_meta(b"\x85\x40 memo\n#TITLE A\n")["title"] == "A"
    with pytest.raises(db_module.ChartEncodingError, match="cp949"):
        db_module.chart_meta(b"#TITLE \x85\x40\n")


def test_chart_encoding_error_unit_is_held_and_downloadable(client, monkeypatch):
    """헤더 인코딩을 알 수 없는 곡은 대체하지 않고 등록 불가로 묶음에 남기며, 내려받을 수 있습니다."""
    admin_login(client, monkeypatch)
    bad = b"#TITLE \x85\x40\n"
    job = stage(client, "pack.zip", make_zip({
        "p/ok/a.bms": "#TITLE 한글\n".encode("cp949"),
        "p/bad/b.bms": bad,
        "p/bad/k.wav": b"k",
    }))
    sid = job["stage_id"]
    assert sorted(bool(s.get("error")) for s in job["songs"]) == [False, True]
    got = units(client, sid)
    assert got["pack/p/ok"]["error"] is None and got["pack/p/ok"]["charts"][0]["title"] == "한글"
    held = got["pack/p/bad"]
    assert "b.bms" in held["error"] and "인코딩" in held["charts"][0]["error"]
    assert detail(client, sid)["counts"]["failed"] == 1
    assert [u["folder"] for u in detail(client, sid, show="failed")["items"]] == ["pack/p/bad"]

    r = client.get(f"/api/admin/import/stages/{sid}/units/{held['id']}/zip")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        assert sorted(zf.namelist()) == ["b.bms", "k.wav"] and zf.read("b.bms") == bad
    assert client.get(f"/api/admin/import/stages/{sid}/units/nope/zip").status_code == 404

    done = commit_stage(client, sid)
    assert any(s.get("error") for s in done["songs"])
    assert [tuple(r) for r in song_rows()] == [(1, "ok")]
    # 등록 불가 곡은 묶음에 그대로 남습니다.
    assert list(units(client, sid)) == ["pack/p/bad"]
    # 제외로 두면 commit 때 묶음에서 빠집니다.
    assert set_target(client, sid, held, "skip").status_code == 200
    commit_stage(client, sid)
    assert client.get("/api/admin/import/stages").json() == []


def test_chart_encoding_ignores_file_name_headers():
    """비교용 헤더(#TITLE 등)만 보고 인코딩을 고릅니다. #WAV 파일명이 다른 인코딩이어도 등록됩니다(#157)."""
    chart = "#TITLE 노래\n#ARTIST 바다\n".encode("cp949") + "#WAV01 ソナタ.wav\n".encode("cp932")
    assert db_module.chart_encoding(chart) == "cp949"
    assert db_module.chart_meta(chart)["title"] == "노래"
    # 비교용 헤더끼리 인코딩이 다르면 값마다 따로 고릅니다.
    mixed = "#TITLE 노래\n".encode("cp949") + "#ARTIST ソナタ\n".encode("cp932")
    assert db_module.chart_meta(mixed)["title"] == "노래"
    assert db_module.chart_meta(mixed)["artist"] == "ソナタ"
    # 어느 인코딩으로도 안 풀리는 값이 있으면 그대로 오류입니다.
    with pytest.raises(db_module.ChartEncodingError, match="#ARTIST"):
        db_module.chart_meta("#TITLE 노래\n".encode("cp949") + b"#ARTIST \x85\x40\n")


def test_commit_unit_with_skipped_broken_chart(client, monkeypatch):
    """헤더 인코딩 오류가 난 차트만 제외(skip)로 두면 같은 곡의 나머지 차트는 등록됩니다(#157)."""
    admin_login(client, monkeypatch)
    job = stage(client, "pack.zip", make_zip({
        "p/s/a.bms": "#TITLE 한글\n".encode("cp949"),
        "p/s/b.bms": b"#TITLE \x85\x40\n",
        "p/s/k.wav": b"k",
    }))
    sid = job["stage_id"]
    held = units(client, sid)["pack/p/s"]
    assert "b.bms" in held["error"]
    # 오류가 없는 차트만 제외해도 등록할 수 없습니다.
    assert set_target(client, sid, held, "auto", {"a.bms": "skip"}).status_code == 200
    commit_stage(client, sid)
    assert not song_rows()
    assert set_target(client, sid, held, "auto", {"a.bms": None, "b.bms": "skip"}).status_code == 200
    done = commit_stage(client, sid)
    assert not any(s.get("error") for s in done["songs"]), done["songs"]
    assert len(song_rows()) == 1
    assert client.get("/api/admin/import/stages").json() == []


def test_download_old_unpacked_unit(client, monkeypatch):
    admin_login(client, monkeypatch)
    job = stage(client, "pack.zip", make_zip({"s/a.bms": b"#TITLE A\n", "s/k.wav": b"k"}))
    sid = job["stage_id"]
    unit = units(client, sid)["pack/s"]
    unit_root = constant.STAGED_DIR / sid / unit["id"]
    with zipfile.ZipFile(unit_root / staging.PACK_FILE) as zf:
        zf.extractall(unit_root / "s")
    (unit_root / staging.PACK_FILE).unlink()
    r = client.get(f"/api/admin/import/stages/{sid}/units/{unit['id']}/zip")
    assert r.status_code == 200
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        assert sorted(zf.namelist()) == ["a.bms", "k.wav"]
    # 임시로 묶은 zip은 보낸 뒤 지웁니다.
    assert not list(unit_root.glob(".download-*"))
