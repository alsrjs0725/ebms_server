"""관리자 렌더 비교(render_check): 곡들의 차트를 서버에서 렌더해 같은 곡끼리 묶습니다."""
import shutil
import time

import numpy as np
import pytest

from test_auth import client, oauth  # noqa: F401  (fixture 재사용)
from test_import import make_zip, stage, upload
from test_quota import admin_login
from test_render_match import tone

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def chart(seed: int, title: str) -> bytes:
    rng = np.random.default_rng(seed)
    rows = [f"#{m:03d}01:{''.join(rng.choice(['01', '02', '03', '00', '00']) for _ in range(16))}\n" for m in range(1, 30)]
    return f"#TITLE {title}\n#BPM 150\n#WAV01 01.wav\n#WAV02 02.wav\n#WAV03 03.wav\n{''.join(rows)}".encode()


def wait_long(client, job, seconds=120):
    deadline = time.monotonic() + seconds
    while job["status"] == "running":
        assert time.monotonic() < deadline, "job did not finish"
        time.sleep(0.1)
        job = client.get(f"/api/admin/import/jobs/{job['id']}").json()
    return job


@needs_ffmpeg
def test_render_compare_groups_same_song_across_songs(client, monkeypatch, tmp_path):
    admin_login(client, monkeypatch)
    sounds = {"01.wav": tone(tmp_path, "a.wav", 200), "02.wav": tone(tmp_path, "b.wav", 900), "03.wav": tone(tmp_path, "c.wav", 3000)}
    other = {"x1.wav": tone(tmp_path, "d.wav", 3000, 1.5), "x2.wav": tone(tmp_path, "e.wav", 120, 0.05), "x3.wav": tone(tmp_path, "f.wav", 500)}
    # 다른 곡 차트가 섞인 폴더: b.bms는 다른 키음·다른 패턴
    mixed = chart(2, "B").replace(b"01.wav", b"x1.wav").replace(b"02.wav", b"x2.wav").replace(b"03.wav", b"x3.wav")
    upload(client, "a.zip", make_zip({"a.bms": chart(1, "A"), "a2.bms": chart(1, "A another"), "b.bms": mixed, **sounds, **other}))
    job = stage(client, "c.zip", make_zip({"c.bms": chart(1, "A copy"), **sounds}))
    unit = client.get(f"/api/admin/import/stages/{job['stage_id']}").json()["items"][0]
    client.put(f"/api/admin/import/stages/{job['stage_id']}/units/{unit['id']}", json={"target": "new"})
    r = client.post(f"/api/admin/import/stages/{job['stage_id']}/commit")
    assert wait_long(client, r.json())["songs"][0]["song_id"] == 2

    assert client.post("/api/admin/render/compare", json={"songs": [99]}).status_code == 404
    assert client.post("/api/admin/render/compare", json={"songs": []}).status_code == 400
    r = client.post("/api/admin/render/compare", json={"songs": [1, 2]})
    assert r.status_code == 200, r.text
    job = wait_long(client, r.json())
    assert job["status"] == "done", job["error"]
    assert job["kind"] == "render" and job["done"] == job["total"] == 4
    groups = [{(c["song_id"], c["path"]) for c in g["charts"]} for g in job["result"]["groups"]]
    assert groups == [{(1, "a.bms"), (1, "a2.bms"), (2, "c.bms")}, {(1, "b.bms")}]
    assert job["result"]["groups"][0]["songs"] == [1, 2]

    # 차트를 골라 볼 수 있습니다.
    r = client.post("/api/admin/render/compare", json={"songs": [1], "charts": {"1": ["b.bms"]}})
    job = wait_long(client, r.json())
    assert job["total"] == 1 and [[c["path"] for c in g["charts"]] for g in job["result"]["groups"]] == [["b.bms"]]
