"""병합 충돌 해결: 충돌 파일 비교와 파일마다 기존 유지·새 파일로 바꿈을 골라 그 곡만 다시 넣는 흐름 테스트."""
import io
import time
import shutil
import subprocess
import zipfile

import numpy as np
import pytest

from ebms_server import conflicts

from test_auth import client, oauth  # noqa: F401  (fixture 재사용)
from test_import import make_zip, stage, upload, wait  # noqa: F401
from test_quota import admin_login
from test_staging import commit_stage, song_files, units


def song_zip(song_id):
    from ebms_server import db as db_module
    with zipfile.ZipFile(io.BytesIO(db_module.Database().get_song_data(song_id))) as zf:
        return {name: zf.read(name) for name in zf.namelist()}


def conflicted(client, monkeypatch, new_files):
    """기존 곡(#1)과 겹치는 곡을 올려 commit하고, 충돌로 남은 (묶음 id, 곡) 을 돌려줍니다."""
    admin_login(client, monkeypatch)
    upload(client, "base.zip", make_zip({"a.bms": b"#TITLE A\n", "k.wav": b"old", "readme.txt": b"one\ntwo\n"}))
    job = stage(client, "pack.zip", make_zip({f"p/s/{k}": v for k, v in {"a.bms": b"#TITLE A\n", **new_files}.items()}))
    sid = job["stage_id"]
    commit_stage(client, sid)
    return sid, units(client, sid)["pack/p/s"]


def base(sid, unit):
    return f"/api/admin/import/stages/{sid}/units/{unit['id']}/conflicts"


def test_list_choose_and_apply(client, monkeypatch):
    sid, unit = conflicted(client, monkeypatch, {"k.wav": b"new", "readme.txt": b"one\nTWO\n", "m.wav": b"m"})
    assert unit["result"]["conflicts"] == ["k.wav", "readme.txt"]

    r = client.get(base(sid, unit))
    assert r.status_code == 200, r.text
    items = {i["path"]: i for i in r.json()["items"]}
    assert items["k.wav"] == {
        "path": "k.wav", "song_id": 1, "kind": "sound", "new_size": 3,
        "old_path": "k.wav", "old_kind": "sound", "old_size": 3, "choice": None, "ai": None,
    }
    assert items["readme.txt"]["kind"] == "other"

    # 원본 내려받기와 텍스트 diff
    r = client.get(base(sid, unit) + "/file", params={"path": "k.wav", "side": "old", "raw": True})
    assert r.status_code == 200 and r.content == b"old"
    r = client.get(base(sid, unit) + "/file", params={"path": "k.wav", "side": "new"})
    assert r.content == b"new" and r.headers["content-type"] == "audio/wav"
    # 재생 중 위치를 옮길 수 있게 Range 요청을 받습니다.
    r = client.get(base(sid, unit) + "/file", params={"path": "k.wav", "side": "new"}, headers={"Range": "bytes=1-2"})
    assert r.status_code == 206 and r.content == b"ew"
    diff = client.get(base(sid, unit) + "/compare", params={"path": "readme.txt"}).json()
    assert "-two" in diff["diff"] and "+TWO" in diff["diff"]
    assert client.get(base(sid, unit) + "/file", params={"path": "a.bms", "side": "old"}).status_code == 404

    # 다 고르기 전에는 적용할 수 없습니다.
    assert client.put(base(sid, unit), json={"choices": {"k.wav": "new"}}).status_code == 200
    assert client.post(base(sid, unit) + "/apply").status_code == 409
    assert client.put(base(sid, unit), json={"choices": {"readme.txt": "maybe"}}).status_code == 400
    assert client.put(base(sid, unit), json={"choices": {"zz.txt": "old"}}).status_code == 400
    assert client.put(base(sid, unit), json={"choices": {"readme.txt": "old"}}).json() == {
        "resolutions": {"k.wav": "new", "readme.txt": "old"},
    }

    r = client.post(base(sid, unit) + "/apply")
    assert r.status_code == 200, r.text
    done = wait(client, r.json())
    assert done["songs"] == [{
        "folder": "pack/p/s", "song_id": 1, "new_song": False, "charts": 1, "new_charts": 0,
        "resolved_files": {"k.wav": "new", "readme.txt": "old"},
    }]
    files = song_zip(1)
    assert files["k.wav"] == b"new" and files["readme.txt"] == b"one\ntwo\n" and files["m.wav"] == b"m"
    # 충돌이 풀린 곡은 묶음에서 빠지고, 남은 곡이 없으면 묶음도 지웁니다.
    assert client.get(f"/api/admin/import/stages/{sid}").status_code == 404


def test_apply_commits_only_that_unit(client, monkeypatch):
    admin_login(client, monkeypatch)
    upload(client, "base.zip", make_zip({"a.bms": b"#TITLE A\n", "k.wav": b"old"}))
    job = stage(client, "pack.zip", make_zip({"p/s/a.bms": b"#TITLE A\n", "p/s/k.wav": b"new"}))
    sid = job["stage_id"]
    commit_stage(client, sid)
    # 충돌 곡이 남은 묶음에 다른 곡이 있어도 적용은 그 곡만 넣습니다.
    from ebms_server import staging
    with staging._lock:
        data = staging._load(sid)
        other = dict(data["units"][0], id="other", folder="pack/p/other", result=None)
        data["units"].append(other)
        staging._save(data)
    unit = units(client, sid)["pack/p/s"]
    client.put(base(sid, unit), json={"choices": {"k.wav": "old"}})
    done = wait(client, client.post(base(sid, unit) + "/apply").json())
    assert [s["folder"] for s in done["songs"]] == ["pack/p/s"]
    assert [u["folder"] for u in client.get(f"/api/admin/import/stages/{sid}").json()["items"]] == ["pack/p/other"]
    assert song_files(1) == ["a.bms", "k.wav"]


def test_slot_conflict_replaces_existing_entry(client, monkeypatch):
    # 확장자만 다른 같은 자리(k.wav ↔ k.ogg)를 새 파일로 바꾸면 기존 항목을 뺍니다.
    admin_login(client, monkeypatch)
    upload(client, "base.zip", make_zip({"a.bms": b"#TITLE A\n", "k.wav": b"old"}))
    monkeypatch.setattr("ebms_server.media_match.same_files", lambda pairs: set())
    job = stage(client, "pack.zip", make_zip({"p/s/a.bms": b"#TITLE A\n", "p/s/k.ogg": b"new"}))
    sid = job["stage_id"]
    commit_stage(client, sid)
    unit = units(client, sid)["pack/p/s"]
    items = client.get(base(sid, unit)).json()["items"]
    assert [(i["path"], i["old_path"]) for i in items] == [("k.ogg", "k.wav")]
    client.put(base(sid, unit), json={"choices": {"k.ogg": "new"}})
    wait(client, client.post(base(sid, unit) + "/apply").json())
    assert song_zip(1) == {"a.bms": b"#TITLE A\n", "k.ogg": b"new"}


def test_chart_conflict_text_diff_and_apply(client, monkeypatch):
    # 싱크를 고치며 차트도 바뀌는 경우가 있어 같은 이름 차트도 비교해 고릅니다.
    admin_login(client, monkeypatch)
    upload(client, "base.zip", make_zip({"a.bms": b"#TITLE A\n", "c.bms": b"#TITLE C\n#00111:01\n"}))
    job = stage(client, "pack.zip", make_zip({"p/s/a.bms": b"#TITLE A\n", "p/s/c.bms": b"#TITLE C\n#00111:0001\n"}))
    sid = job["stage_id"]
    commit_stage(client, sid)
    unit = units(client, sid)["pack/p/s"]
    items = client.get(base(sid, unit)).json()["items"]
    assert [(i["path"], i["kind"]) for i in items] == [("c.bms", "chart")]
    diff = client.get(base(sid, unit) + "/compare", params={"path": "c.bms"}).json()["diff"]
    assert "-#00111:01" in diff and "+#00111:0001" in diff
    # 채보로 들어보기는 그 차트의 기존·새 쪽을 고릅니다.
    charts = client.get(base(sid, unit) + "/charts", params={"path": "c.bms"}).json()["items"]
    assert [(c["side"], c["path"], c["title"]) for c in charts] == [("old", "c.bms", "C"), ("new", "c.bms", "C")]
    client.put(base(sid, unit), json={"choices": {"c.bms": "new"}})
    done = wait(client, client.post(base(sid, unit) + "/apply").json())
    assert done["songs"][0]["new_charts"] == 1
    assert song_zip(1)["c.bms"] == b"#TITLE C\n#00111:0001\n"


needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


def _media(tmp_path, name, args):
    out = tmp_path / name
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args, str(out)], check=True)
    return out.read_bytes()


@needs_ffmpeg
def test_video_timeline_and_webm(client, monkeypatch, tmp_path):
    admin_login(client, monkeypatch)
    old = _media(tmp_path, "old.mpg", ["-f", "lavfi", "-i", "color=c=black:s=64x64:d=6:r=30", "-c:v", "mpeg1video"])
    # 2초부터 흰 화면
    new = _media(tmp_path, "new.mpg", [
        "-f", "lavfi", "-i", "color=c=black:s=64x64:d=2:r=30", "-f", "lavfi", "-i", "color=c=white:s=64x64:d=4:r=30",
        "-filter_complex", "[0][1]concat=n=2:v=1", "-c:v", "mpeg1video",
    ])
    upload(client, "base.zip", make_zip({"a.bms": b"#TITLE A\n", "bg.mpg": old}))
    job = stage(client, "pack.zip", make_zip({"p/s/a.bms": b"#TITLE A\n", "p/s/bg.mpg": new}))
    sid = job["stage_id"]
    commit_stage(client, sid)
    unit = units(client, sid)["pack/p/s"]

    r = client.get(base(sid, unit) + "/compare", params={"path": "bg.mpg"})
    assert r.status_code == 200, r.text
    timeline = r.json()
    assert timeline["fps"] == conflicts.VIDEO_FPS
    assert len(timeline["segments"]) == 1
    start, end = timeline["segments"][0]
    assert abs(start - 60) <= 2 and end >= 175
    r = client.get(base(sid, unit) + "/file", params={"path": "bg.mpg", "side": "new"})
    assert r.status_code == 200 and r.headers["content-type"] == "video/webm" and r.content[:4] == b"\x1a\x45\xdf\xa3"


@needs_ffmpeg
def test_sound_compare(client, monkeypatch, tmp_path):
    admin_login(client, monkeypatch)
    old = _media(tmp_path, "old.wav", ["-f", "lavfi", "-i", "sine=f=440:d=0.5"])
    new = _media(tmp_path, "new.wav", ["-f", "lavfi", "-i", "sine=f=440:d=1.5"])
    upload(client, "base.zip", make_zip({"a.bms": b"#TITLE A\n", "k.wav": old}))
    job = stage(client, "pack.zip", make_zip({"p/s/a.bms": b"#TITLE A\n", "p/s/k.wav": new}))
    sid = job["stage_id"]
    commit_stage(client, sid)
    unit = units(client, sid)["pack/p/s"]
    result = client.get(base(sid, unit) + "/compare", params={"path": "k.wav"}).json()
    assert result["old"]["seconds"] == pytest.approx(0.5, abs=0.05)
    assert result["new"]["seconds"] == pytest.approx(1.5, abs=0.05)
    assert result["same"] is False and len(result["new"]["peaks"]) == conflicts.WAVE_BARS
    assert np.max(result["old"]["peaks"]) > 0.1
    assert {"correlation", "aligned_correlation", "offset_ms"} <= result.keys()


@needs_ffmpeg
def test_sound_compare_reports_sync_offset(client, monkeypatch, tmp_path):
    # 판정은 싱크를 맞추지 않고, 싱크 맞춘 상관계수와 싱크 오차는 따로 보여 줍니다.
    admin_login(client, monkeypatch)
    old = _media(tmp_path, "old.wav", ["-f", "lavfi", "-i", "anoisesrc=d=1:seed=3", "-ac", "1"])
    new = _media(tmp_path, "new.wav", ["-f", "lavfi", "-i", "anoisesrc=d=1:seed=3", "-ac", "1", "-af", "adelay=200"])
    upload(client, "base.zip", make_zip({"a.bms": b"#TITLE A\n", "k.wav": old}))
    job = stage(client, "pack.zip", make_zip({"p/s/a.bms": b"#TITLE A\n", "p/s/k.wav": new}))
    sid = job["stage_id"]
    commit_stage(client, sid)
    unit = units(client, sid)["pack/p/s"]
    result = client.get(base(sid, unit) + "/compare", params={"path": "k.wav"}).json()
    assert result["offset_ms"] == pytest.approx(-200, abs=5)
    assert result["aligned_correlation"] > 0.9 > result["correlation"]


def _wait_render(client, url, params):
    for _ in range(300):
        r = client.get(url, params=params)
        assert r.status_code == 200, r.text
        if r.json()["status"] != "running":
            return r.json()
        time.sleep(0.05)
    raise AssertionError("render did not finish")


@needs_ffmpeg
def test_render_chart_with_sound_conflict(client, monkeypatch, tmp_path):
    # 채보로 들어보기: 충돌 키음(lead.wav)은 바탕에서 빼고 시각표로, 나머지(kick.wav)는 바탕 FLAC에 섞습니다.
    admin_login(client, monkeypatch)
    kick = _media(tmp_path, "kick.wav", ["-f", "lavfi", "-i", "sine=f=60:d=0.3"])
    lead = _media(tmp_path, "lead.wav", ["-f", "lavfi", "-i", "sine=f=523:d=0.5"])
    lead2 = _media(tmp_path, "lead2.wav", ["-f", "lavfi", "-i", "sine=f=330:d=0.5"])
    chart = b"#TITLE R\n#PLAYLEVEL 5\n#BPM 120\n#WAV01 kick.wav\n#WAV02 lead.wav\n#00111:01010101\n#00112:0002\n#00201:02\n"
    upload(client, "base.zip", make_zip({"a.bms": chart, "kick.wav": kick, "lead.wav": lead}))
    job = stage(client, "pack.zip", make_zip({"p/s/a.bms": chart, "p/s/kick.wav": kick, "p/s/lead.wav": lead2}))
    sid = job["stage_id"]
    commit_stage(client, sid)
    unit = units(client, sid)["pack/p/s"]

    charts = client.get(base(sid, unit) + "/charts", params={"path": "lead.wav"}).json()
    assert charts["song_id"] == 1
    assert [(c["side"], c["path"], c["uses"]) for c in charts["items"]] == [("old", "a.bms", 2), ("new", "a.bms", 2)]
    assert charts["items"][0]["title"] == "R (Lv.5)"

    params = {"song": 1, "side": "new", "chart": "a.bms"}
    data = _wait_render(client, base(sid, unit) + "/render", params)
    assert data["status"] == "ready", data
    # 120BPM에서 1마디는 2초: lead는 1번 마디 가운데(3초)와 2번 마디 처음(4초)
    assert [(round(e["t"], 3), e["key"]) for e in data["events"]] == [(3.0, "lead.wav"), (4.0, "lead.wav")]
    assert data["rate"] == conflicts.RENDER_RATE and 0 < data["gain"] <= 1
    r = client.get(base(sid, unit) + "/render/bed", params=params)
    assert r.status_code == 200 and r.content[:4] == b"fLaC"
    bed = tmp_path / "bed.flac"
    bed.write_bytes(r.content)
    pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", str(bed), "-ac", "1", "-f", "f32le", "-"], capture_output=True, check=True).stdout
    samples = np.frombuffer(pcm, "<f4")
    rate = conflicts.RENDER_RATE
    assert np.abs(samples[int(2.05 * rate):int(2.25 * rate)]).max() > 0.1  # kick (2초)
    assert np.abs(samples[int(4.05 * rate):int(4.4 * rate)]).max() < 0.01  # lead는 바탕에 없음
    assert client.get(base(sid, unit) + "/charts", params={"path": "zz.wav"}).status_code == 404
