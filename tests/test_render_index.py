"""모든 곡 렌더 색인(render_index): 다른 곡 번호의 같은 곡(중복)과 곡 안에 섞인 다른 곡(섞임) 찾기."""
import logging

import numpy as np

from ebms_server import fingerprint, render_index, render_match
from test_auth import client, oauth  # noqa: F401  (fixture 재사용)
from test_import import make_zip, stage, upload
from test_quota import admin_login
from test_render_check import chart, needs_ffmpeg, wait_long
from test_render_match import tone


def test_pack_roundtrip_and_hashes_shift():
    rng = np.random.default_rng(0)
    f = np.maximum(rng.normal(0, 1, (2000, render_match.BANDS)), 0).astype(np.float32)
    g = fingerprint.unpack(fingerprint.pack(f))
    assert g.shape == f.shape and np.abs(g - np.clip(f, 0, 12.75)).max() <= fingerprint.FEATURE_STEP / 2 + 1e-6
    # 앞에 무음을 붙여 늦게 시작해도 같은 해시가 같은 시간차로 나옵니다.
    a = {(h, t + 100) for h, t in fingerprint.hashes(f)}
    b = set(fingerprint.hashes(np.vstack([np.zeros((100, render_match.BANDS), np.float32), f])))
    assert len(a) > 100 and len(a & b) > 0.95 * len(a)


@needs_ffmpeg
def test_index_finds_duplicate_and_mixed_songs(client, monkeypatch, tmp_path):
    admin_login(client, monkeypatch)
    sounds = {"01.wav": tone(tmp_path, "a.wav", 200), "02.wav": tone(tmp_path, "b.wav", 900), "03.wav": tone(tmp_path, "c.wav", 3000)}
    other = {"x1.wav": tone(tmp_path, "d.wav", 3000, 1.5), "x2.wav": tone(tmp_path, "e.wav", 120, 0.05), "x3.wav": tone(tmp_path, "f.wav", 500)}
    mixed = chart(2, "B").replace(b"01.wav", b"x1.wav").replace(b"02.wav", b"x2.wav").replace(b"03.wav", b"x3.wav")
    upload(client, "a.zip", make_zip({"a.bms": chart(1, "A"), "a2.bms": chart(1, "A another"), "b.bms": mixed, **sounds, **other}))
    # 같은 곡을 다른 번호로(새 곡) 등록
    job = stage(client, "c.zip", make_zip({"c.bms": chart(1, "A copy"), **sounds}))
    unit = client.get(f"/api/admin/import/stages/{job['stage_id']}").json()["items"][0]
    client.put(f"/api/admin/import/stages/{job['stage_id']}/units/{unit['id']}", json={"target": "new"})
    assert wait_long(client, client.post(f"/api/admin/import/stages/{job['stage_id']}/commit").json())["songs"][0]["song_id"] == 2
    # 다른 곡(겹치는 소리 없음)
    upload(client, "d.zip", make_zip({"d.bms": chart(3, "D").replace(b"01.wav", b"x1.wav").replace(b"02.wav", b"x2.wav")
                                      .replace(b"03.wav", b"x3.wav"), **other}))

    assert client.get("/admin/render").status_code == 200
    assert render_index.run(5) is False
    state = render_index.status_()
    assert state["indexed_songs"] == 3 and state["indexed_charts"] == 5 and not state["running"]
    found = client.get("/api/admin/render/findings").json()
    kinds = {(f["kind"], f["song_a"], f["song_b"]) for f in found}
    assert kinds == {("dup", 1, 2), ("mixed", 1, None)}, found
    dup = next(f for f in found if f["kind"] == "dup")
    assert dup["score"] >= render_match.SAME and dup["detail"]["chart"] in ("a.bms", "a2.bms") and dup["detail"]["other_chart"] == "c.bms"
    mixed_finding = next(f for f in found if f["kind"] == "mixed")
    assert sorted(map(sorted, mixed_finding["detail"]["groups"])) == [["a.bms", "a2.bms"], ["b.bms"]]
    assert "#1" in client.get("/admin/render").text

    # 넘긴 항목은 다시 색인해도 열리지 않습니다. 바뀐 곡이 없으면 다시 색인하지 않습니다.
    assert client.put(f"/api/admin/render/findings/{dup['id']}", json={"status": "dismissed"}).status_code == 200
    assert client.put("/api/admin/render/findings/999", json={"status": "dismissed"}).status_code == 404
    assert client.put(f"/api/admin/render/findings/{dup['id']}", json={"status": "x"}).status_code == 400
    render_index.run(5)
    assert render_index.status_()["songs"] == 0
    assert [f["kind"] for f in client.get("/api/admin/render/findings").json()] == ["mixed"]
    assert [f["id"] for f in client.get("/api/admin/render/findings", params={"status": "dismissed"}).json()] == [dup["id"]]

    # 사용 중지한 곡은 색인과 열린 항목에서 빠집니다.
    assert client.request("DELETE", "/api/admin/songs/1", json={"reason": "test"}).status_code == 200
    render_index.run(5)
    state = render_index.status_()
    assert state["indexed_songs"] == 2
    assert client.get("/api/admin/render/findings").json() == []
    assert client.get("/api/admin/render/index").json()["indexed_charts"] == 2


def test_cpus(monkeypatch):
    monkeypatch.setattr(render_index.os, "sched_getaffinity", lambda pid: {0, 1, 2, 3})
    monkeypatch.setattr(render_index.constant, "RENDER_INDEX_CPUS", "")
    assert render_index._cpus() == {3}
    monkeypatch.setattr(render_index.constant, "RENDER_INDEX_CPUS", "1, 2,9")
    assert render_index._cpus() == {1, 2}
    monkeypatch.setattr(render_index.constant, "RENDER_INDEX_CPUS", "none")
    assert render_index._cpus() is None


def test_run_with_zero_minutes_leaves_work(client, monkeypatch):
    admin_login(client, monkeypatch)
    upload(client, "a.zip", make_zip({"a.bms": b"#TITLE A\n", "01.ogg": b"a"}))
    assert render_index.run(0) is True
    assert render_index.status_()["indexed_songs"] == 0 and render_index.status_()["left"] is True


def test_parse_output_streams_records_and_joins_raw_stderr():
    lines = [
        "Traceback (most recent call last):\n",  # 로그 설정 전에 죽은 경우(stderr)
        '  File "<string>", line 1\n',
        "FileNotFoundError: x\n",
        "INFO|ebms_server.render_index|start\n",
        f"ERROR|ebms_server.render_index|run failed{render_index.NEWLINE}Traceback (most recent call last):{render_index.NEWLINE}OSError: y\n",
        "INFO|result|1\n",
    ]
    out = list(render_index.parse_output(lines))
    assert out[0][0] == logging.ERROR and out[0][1] == "ebms_server.render_index.process" and out[0][2].endswith("FileNotFoundError: x")
    assert out[1] == (logging.INFO, "ebms_server.render_index", "start")
    assert out[2] == (logging.ERROR, "ebms_server.render_index", "run failed\nTraceback (most recent call last):\nOSError: y")
    assert out[3] == (logging.INFO, "result", "1")

    # 로그 줄은 다음 줄을 기다리지 않고 바로 나옵니다(색인 중에도 로그 페이지에 보이게).
    def feed():
        yield "INFO|ebms_server.render_index|first\n"
        raise AssertionError("waited for the next line")

    assert next(render_index.parse_output(feed())) == (logging.INFO, "ebms_server.render_index", "first")


def test_worker_process_logs_reach_server(monkeypatch, tmp_path, caplog):
    """색인 프로세스의 로그(오류 traceback 포함)가 웹 서버 로거로 남고, 끝나면 done이 섭니다."""
    monkeypatch.setenv("EBMS_RENDER_INDEX_DIR", str(tmp_path / "ri"))
    monkeypatch.setenv("EBMS_DB_HOST", "127.0.0.1")
    monkeypatch.setenv("EBMS_DB_PORT", "1")  # 연결 실패 → run이 오류를 남기고 끝남
    monkeypatch.setenv("EBMS_RENDER_INDEX_CPUS", "none")
    caplog.set_level(logging.INFO)
    proc = render_index._spawn(1)
    assert render_index._spawn(1) is None  # 돌고 있으면 새로 띄우지 않음
    assert proc.done.wait(120)
    assert proc.left is False and proc.proc.returncode == 0
    messages = [r.getMessage() for r in caplog.records if r.name.startswith("ebms_server.render_index")]
    assert any("render index process" in m for m in messages)
    assert any("render index run failed" in m and "\nTraceback" in m for m in messages)


def test_render_page_paginates_and_trims_groups(client, monkeypatch):
    admin_login(client, monkeypatch)
    con = render_index.connect()
    paths = [f"c{i}.bms" for i in range(8)]
    for i in range(render_index.PAGE_SIZE + 3):
        render_index._put_finding(con, "mixed", 1000 + i, 0, None, {"groups": [paths, ["z.bms"]]})
    con.commit()
    con.close()
    first = client.get("/admin/render").text
    assert "1 / 2 쪽" in first and "c4.bms" in first and "c5.bms" not in first
    second = client.get("/admin/render", params={"page": 2}).text
    assert second.count("data-id=") == 3
    assert len(client.get("/api/admin/render/findings", params={"limit": 2, "offset": render_index.PAGE_SIZE + 2}).json()) == 1


def test_index_song_keeps_renders_of_one_part_only(monkeypatch, tmp_path):
    """곡 전체의 렌더 결과를 모아 두지 않습니다(차트가 많은 곡에서 색인 프로세스가 OOM으로 죽던 문제).
    살아 있는 특징 배열 수가 한 번에 렌더하는 차트 수 × 분기 수를 넘지 않아야 합니다."""
    import io
    import weakref
    import zipfile

    from ebms_server import constant

    monkeypatch.setattr(constant, "RENDER_INDEX_DIR", tmp_path / "ri")
    paths = [f"c{i:03d}.bms" for i in range(60)]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for p in paths:
            zf.writestr(p, b"#TITLE x\n")
    zpath = tmp_path / "song.zip"
    zpath.write_bytes(buf.getvalue())

    class _File:
        def __init__(self):
            import os
            self._fd = os.open(zpath, os.O_RDONLY)

        def close(self):
            import os
            os.close(self._fd)

    monkeypatch.setattr(render_index, "_song_row", lambda song_id: ([{"path": p} for p in paths], _File()))
    monkeypatch.setattr(render_index, "RENDER_PART", 4)
    branches = 6
    alive: list = []
    peak = [0]
    rng = np.random.default_rng(1)

    def fake(names, read, part, nice=True):
        out = {}
        for p in part:
            feats = [np.maximum(rng.normal(0, 1, (400, render_match.BANDS)), 0).astype(np.float32) for _ in range(branches)]
            alive.extend(weakref.ref(f) for f in feats)
            out[p] = render_match.Fingerprint(feats, [frozenset()] * branches, False, (1, 1))
        peak[0] = max(peak[0], sum(r() is not None for r in alive))
        return out

    monkeypatch.setattr(render_index.render_match, "fingerprints", fake)
    con = render_index.connect()
    try:
        result = render_index.index_song(con, 1, "x")
    finally:
        con.close()
    assert result["charts"] == len(paths)
    # 앞 묶음들의 특징은 버려졌고(묶음 대표는 저장 분기 수만), 한 묶음 분량 + 대표들만 남습니다.
    assert peak[0] <= 4 * branches + result["groups"] * fingerprint.MAX_STORED_VARIANTS + 4 * branches


def test_runs_dying_twice_on_a_song_mark_it_failed(monkeypatch, tmp_path):
    """SIGKILL(OOM)로 죽은 회차는 실패 표시를 못 남기므로, 같은 곡에서 두 번 죽으면 다음 회차가 실패로 표시합니다."""
    import json

    from ebms_server import constant

    monkeypatch.setattr(constant, "RENDER_INDEX_DIR", tmp_path / "ri")
    monkeypatch.setattr(render_index, "_live_songs", lambda: {5: "sha5"})
    calls = []
    monkeypatch.setattr(render_index, "index_song", lambda con, song_id, sha: calls.append(song_id))

    def died_on(song_id):
        con = render_index.connect()
        con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('run', ?)",
                    (json.dumps({"started_at": 1, "finished_at": None, "song": song_id}),))
        con.commit()
        con.close()

    died_on(5)
    render_index.run(1)
    assert calls == [5]  # 한 번 죽은 것은 재시작일 수 있어 다시 해 봄
    calls.clear()
    # index_song이 호출됐지만 mark_song을 안 하는 가짜라 아직 색인 안 된 곡. 다시 같은 곡에서 죽음.
    died_on(5)
    render_index.run(1)
    assert calls == []
    con = render_index.connect()
    assert con.execute("SELECT charts FROM song WHERE id = 5").fetchone()[0] == -1
    con.close()


def test_huge_chart_is_not_rendered(monkeypatch):
    monkeypatch.setattr(render_match, "MAX_CHART_BYTES", 10)
    monkeypatch.setattr(render_match.preview, "_ffmpeg", lambda: "ffmpeg")
    out = render_match.fingerprints(["a.bms"], lambda p: b"#TITLE " + b"x" * 100, ["a.bms"])
    assert out == {}
