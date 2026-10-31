"""업로드 판정의 렌더 검색(stage_render): commit한 곡의 앞부분 렌더를 저장소에 넣고, 다음 업로드에서 같은 곡을 찾고
키음 이름만 같은 다른 소리의 곡에는 자동으로 넣지 않습니다(빠진 키음은 후보 곡 것으로 채워 렌더)."""
from test_auth import client, oauth  # noqa: F401  (fixture 재사용)
from test_import import make_zip, stage, upload
from test_quota import admin_login
from test_render_check import chart, needs_ffmpeg
from test_render_match import tone

from ebms_server import fingerprint, render_match


def _unit(client, job):
    return client.get(f"/api/admin/import/stages/{job['stage_id']}").json()["items"][0]


@needs_ffmpeg
def test_render_search_finds_same_song_and_blocks_other_sounds(client, monkeypatch, tmp_path):
    admin_login(client, monkeypatch)
    sounds = {"01.wav": tone(tmp_path, "a.wav", 200), "02.wav": tone(tmp_path, "b.wav", 900), "03.wav": tone(tmp_path, "c.wav", 3000)}
    upload(client, "a.zip", make_zip({"a.bms": chart(1, "A"), **sounds}))
    con = fingerprint.connect()
    assert fingerprint.song_paths(con, 1) == ["a.bms"] and fingerprint.songs(con) == {1: ""}  # 상시 색인이 다시 렌더할 곡
    con.close()

    # 이름이 전혀 다른 같은 곡(키음 포함): 이름·키음 이름으로는 못 찾지만 렌더로 찾음
    renamed = _unit(client, stage(client, "r.zip", make_zip({"r.bms": chart(1, "Zzz").replace(b"#BPM 150", b"#BPM 150.0"), **sounds})))
    s = renamed["suggestion"]
    assert s["kind"] == "similar" and s["songs"] == [1] and s["matches"] == {} and s["merge"] is None
    assert s["render"]["1"] >= render_match.SAME

    assert s["render_partial"] is False and s["render_blocked"] is None

    # 같은 곡의 차분(03.wav는 올린 곡에 없음): 곡 1의 03.wav로 채워 렌더하면 같은 곡, 자동 추가 그대로
    same = _unit(client, stage(client, "b.zip", make_zip({"b.bms": chart(1, "A [ANOTHER]"), "01.wav": sounds["01.wav"], "02.wav": sounds["02.wav"]})))
    s = same["suggestion"]
    assert s["kind"] == "similar" and s["merge"] == 1 and s["render_partial"] is True and s["render_blocked"] is None
    assert s["render"]["1"] >= render_match.SAME

    # 키음 이름은 같지만 소리가 다른 차분(01·02.wav가 다른 소리, 03.wav 없음): 이름·키음 이름으로는 자동 추가지만
    # 곡 1 키음으로 채워 렌더해도 '다름'이라 자동 추가하지 않음
    other = {"01.wav": tone(tmp_path, "d.wav", 3000, 1.5), "02.wav": tone(tmp_path, "e.wav", 120, 0.05)}
    diff = _unit(client, stage(client, "c.zip", make_zip({"c.bms": chart(1, "A [HYPER]"), **other})))
    s = diff["suggestion"]
    assert s["kind"] == "similar" and s["songs"] == [1] and s["merge"] is None and s["render_blocked"] == 1
    assert s["render"]["1"] <= render_match.DIFFERENT
