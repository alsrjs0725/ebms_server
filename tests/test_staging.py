"""곡 임포트 대기(staged): 올린 곡을 검토하고 대상을 바꾼 뒤 commit하는 흐름 테스트."""
import io
import json
import zipfile

from ebms_server import constant, db as db_module, staging

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
    assert got["pack/p/new"]["suggestion"] == {"kind": "new", "songs": [], "deleted": None, "merge": None, "matches": {}}
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

    # commit 도중 꺼진 묶음은 다시 시작하면 검토할 수 있게 되돌립니다.
    sid = job["stage_id"]
    path = constant.STAGED_DIR / sid / staging.STAGE_FILE
    data = json.loads(path.read_text("utf-8"))
    data["status"] = "committing"
    path.write_text(json.dumps(data), "utf-8")
    (constant.STAGED_DIR / sid / "stray").mkdir()
    assert client.post(f"/api/admin/import/stages/{sid}/commit").status_code == 409
    staging.recover()
    got = detail(client, sid)
    assert got["status"] == "ready" and got["error"]
    assert not (constant.STAGED_DIR / sid / "stray").exists()
    commit_stage(client, sid)
    assert [tuple(r) for r in song_rows()] == [(1, "s1")]


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


def test_similar_songs_for_small_units(client, monkeypatch):
    admin_login(client, monkeypatch)
    upload_new(client, "base.zip", make_zip({"b/a.bms": b"#TITLE Song [NORMAL]\n#ARTIST Comp / obj:x\n#BPM 150.00\n#GENRE Techno\n", "b/k.wav": b"k"}))
    upload_new(client, "other.zip", make_zip({"o/o.bms": b"#TITLE Song\n#ARTIST Someone\n#BPM 150\n"}))
    upload_new(client, "third.zip", make_zip({"t/t.bms": b"#TITLE Else\n#ARTIST Nobody\n#BPM 150\n#GENRE TECHNO\n"}))
    many = {f"big/{i}.wav": b"w" for i in range(staging.SIMILAR_FILES)}
    job = stage(client, "pack.zip", make_zip({
        # 이름·작곡가·BPM·장르 다 같은 곡(1)이 가장 많이 겹침 → auto는 1에 추가
        "small/x.bms": b"#TITLE SONG [ANOTHER]\n#ARTIST comp\n#BPM 150\n#GENRE techno\n", "small/k2.wav": b"k2",
        # 파일이 많으면 겹치는 차트로만 판정
        "big/x.bms": b"#TITLE Song (HYPER)\n#ARTIST Comp\n", **many,
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
        "matches": {"1": ["title", "artist", "bpm", "genre"], "2": ["title", "bpm"], "3": ["bpm", "genre"]},
    }
    assert got["pack/big"]["suggestion"]["kind"] == "new"
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
    assert [r[0] for r in song_rows()] == [1, 2, 3, 4, 5, 6, 7]


def test_analyze_fills_meta_of_old_units_and_backfill(client, monkeypatch):
    admin_login(client, monkeypatch)
    upload(client, "base.zip", make_zip({"b/a.bms": b"#TITLE Song\n#ARTIST Comp\n"}))
    # 이전 버전에서 넣은 차트: 키가 없음
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("UPDATE chart SET title_key = NULL, artist_key = NULL, bpm_key = NULL, genre_key = NULL")
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
    assert sorted(p.name for p in (constant.STAGED_DIR / tmp["stage_id"] / tunit["id"]).iterdir()) == [staging.PACK_FILE]
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
