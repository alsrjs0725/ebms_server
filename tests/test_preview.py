"""서버가 만드는 곡 미리듣기(preview.py) 테스트."""
import io
import math
import shutil
import struct
import wave
import zipfile

import pytest

from ebms_server import preview
from ebms_server.db import Database, file_kinds, zip_entries

from test_auth import client, oauth  # noqa: F401  (fixture 재사용)
from test_client_auth import client_login
from test_quota import bearer

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def times(sounds):
    return [(round(s.time, 4), s.name) for s in sounds]


def test_bms_timing():
    chart = (
        b"#BPM 120\r\n"
        b"#WAV01 kick.wav\r\n#WAV02 snare.wav\r\n#WAVZZ ln.wav\r\n"
        b"#BPM01 240\r\n#STOP01 96\r\n#LNOBJ 02\r\n"
        b"#00001:01\r\n"            # 0초
        b"#00102:0.5\r\n"           # 1마디는 2박
        b"#00111:0001\r\n"          # 2.5초(1마디 1박)
        b"#00203:00F0\r\n"          # 2마디 2박째에 BPM 240
        b"#00211:0101\r\n"          # 3초, 4초
        b"#00309:01\r\n"            # 3마디 시작에 2박 정지
        b"#00311:01\r\n"            # 정지 직전 시각에 소리
        b"#00312:02\r\n"            # LNOBJ(끝) 노트는 소리가 나지 않음
        b"#00351:ZZ00ZZ00\r\n"      # 롱노트: 시작만
        b"#00401:01\r\n"
    )
    # 0마디 0~2초, 1마디(2박) 2~3초, 2마디 3초 시작: 0~2박 120BPM(1초), 2~4박 240BPM(0.5초)
    # 3마디 시작 4.5초, 정지 2박(240BPM) 0.5초, 3마디 4박(240BPM) 1초. 롱노트 끝은 무시
    parsed = preview.bms_chart(chart)
    assert times(parsed.sounds) == [
        (0.0, "kick.wav"),
        (2.5, "kick.wav"),
        (3.0, "kick.wav"),
        (4.0, "kick.wav"),
        (4.5, "kick.wav"),
        (4.5, "ln.wav"),
        (6.0, "kick.wav"),
    ]
    # 플레이 노트는 LNOBJ 노트와 롱노트 끝(3마디 2박 = 5.5초)도 셉니다. 배경음은 세지 않습니다.
    assert [round(t, 4) for t in parsed.notes] == [2.5, 3.0, 4.0, 4.5, 4.5, 4.5, 5.5]
    assert parsed.length == pytest.approx(6.0)


def test_bms_random_picks_first_branch():
    chart = (
        b"#BPM 120\r\n#WAV01 a.wav\r\n#WAV02 b.wav\r\n"
        b"#RANDOM 2\r\n#IF 1\r\n#00001:01\r\n#ELSE\r\n#00001:02\r\n#ENDIF\r\n#ENDRANDOM\r\n"
        b"#RANDOM 2\r\n#IF 2\r\n#00101:01\r\n#ELSEIF 1\r\n#00101:02\r\n#ENDIF\r\n"
    )
    assert times(preview.bms_chart(chart).sounds) == [(0.0, "a.wav"), (2.0, "b.wav")]


def test_bmson_continue_and_cut():
    data = {
        "info": {"init_bpm": 120, "resolution": 240},
        "bpm_events": [{"y": 960, "bpm": 240}],
        "stop_events": [],
        "sound_channels": [
            {"name": "bgm.ogg", "notes": [{"x": 0, "y": 0, "l": 0, "c": False}, {"x": 1, "y": 480, "l": 0, "c": True}]},
            {"name": "hat.wav", "notes": [{"x": 2, "y": 1200, "l": 240, "c": False}]},
        ],
        "bga": {"bga_events": [{"y": 1920, "id": 1}]},
    }
    chart = preview.bmson_chart(data)
    assert [(round(s.time, 4), s.name, round(s.offset, 4), s.end) for s in chart.sounds] == [
        (0.0, "bgm.ogg", 0.0, 1.0),
        (1.0, "bgm.ogg", 1.0, None),
        (2.25, "hat.wav", 0.0, None),
    ]
    # x가 0인 노트는 배경음. 롱노트 끝(y 1440 = 2.5초)도 셉니다. 곡 길이는 BGA(y 1920 = 3초)까지
    assert [round(t, 4) for t in chart.notes] == [1.0, 2.25, 2.5]
    assert chart.length == pytest.approx(3.0)


def test_highlight_finds_density_jump():
    # 100초 곡, 10초마다 노트 하나에 50초 칸에 노트가 몰림 → 그 한 칸 앞(49초)
    notes = sorted([i * 10.0 for i in range(10)] + [50 + k * 0.1 for k in range(10)])
    assert preview.highlight(notes, 100.0) == pytest.approx(49.0)
    assert preview.highlight([], 100.0) == 0.0


def test_clip_start_fits_in_song():
    sounds = [preview.Sound(2.0, "a.wav")]
    # 하이라이트(37초)부터 30초는 곡(40초)을 넘으므로 10초로 앞당깁니다.
    notes = sorted([i * 0.4 for i in range(100)] + [37.6 + k * 0.01 for k in range(30)])
    assert preview.highlight(notes, 40.0) == pytest.approx(37.2)
    assert preview.clip_start(preview.Chart(sounds, notes, 40.0)) == pytest.approx(10.0)
    # 첫 소리보다 앞서지 않습니다.
    assert preview.clip_start(preview.Chart(sounds, [], 40.0)) == pytest.approx(2.0)


def test_mix_includes_sound_started_before_start():
    rate = preview.SAMPLE_RATE
    samples = {"bgm": np_ramp(rate * 4), "hit": np_ramp(rate // 10)}
    # bgm은 0초에 시작해 4초 길이, 1초부터 섞으면 bgm의 1초 지점부터 들어갑니다. hit은 1초 전에 끝납니다.
    sounds = [preview.Sound(0.0, "bgm"), preview.Sound(0.5, "hit")]
    buf = preview.mix(sounds, samples, 1.0)
    assert len(buf) == rate * 3
    assert buf[0] == pytest.approx(samples["bgm"][rate])


def np_ramp(n):
    import numpy as np

    return np.linspace(0.1, 0.5, n, dtype=np.float32)


def tone(seconds=0.3, freq=440.0, rate=22050) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = int(seconds * rate)
        w.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * freq * i / rate))) for i in range(frames)))
    return buf.getvalue()


def make_song(root, name="song", extra=None):
    song = root / name
    (song / "snd").mkdir(parents=True)
    # 차트는 .wav라고 쓰고 실제 파일은 .WAV·대소문자 다름도 찾습니다.
    (song / "a.bms").write_bytes(
        b"#TITLE " + name.encode() + b"\r\n#BPM 120\r\n#WAV01 snd\\Kick.wav\r\n#WAV02 snd/hat.ogg\r\n"
        b"#00101:01010101\r\n#00211:0202\r\n"
    )
    (song / "snd" / "kick.wav").write_bytes(tone(0.2, 220))
    (song / "snd" / "hat.wav").write_bytes(tone(0.1, 880))
    for path, body in (extra or {}).items():
        (song / path).write_bytes(body)
    return song


def render(song_dir):
    data = Database().create_zip(song_dir)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        kinds = file_kinds(zf)
        files = [{**e, "kind": kinds[e["path"]]} for e in zip_entries(data)]
        return preview.render(files, lambda entry: zf.read(entry["path"]))


@needs_ffmpeg
def test_render(tmp_path, client):
    path, ogg = render(make_song(tmp_path))
    assert path == preview.PREVIEW_NAME
    assert ogg.startswith(b"OggS")
    # 첫 소리(1마디 시작 = 2초)부터 키음 끝까지(곡이 30초보다 짧음). 모노 22050Hz의 짧은 음원
    assert 1_000 < len(ogg) < 60_000


@needs_ffmpeg
def test_render_skips_song_with_preview(tmp_path, client):
    assert render(make_song(tmp_path, extra={"preview.ogg": tone()})) is None


def test_preview_path_in_chart_dir():
    files = [
        {"path": "pack/a.bms", "size": 10, "kind": "pre"},
        {"path": "pack/b.bme", "size": 20, "kind": "pre"},
        {"path": "pack/x.wav", "size": 5, "kind": "play"},
    ]
    assert preview.preview_path(files) == "pack/preview_ebms.ogg"
    assert preview.preview_path([*files, {"path": "pack/PREVIEW_EBMS.ogg", "size": 1, "kind": "play"}]) is None
    assert preview.preview_path([*files, {"path": "pack/pv.ogg", "size": 1, "kind": "pre"}]) is None


@needs_ffmpeg
def test_manifest_pre_chunk_and_file(tmp_path, client, monkeypatch):
    key, _ = client_login(client, monkeypatch)
    db = Database()
    db.insert_song(make_song(tmp_path))
    db.insert_song(make_song(tmp_path, "with_preview", extra={"preview.ogg": tone()}))
    h = bearer(key)
    assert all("preview" not in s for s in client.get("/api/pre/manifest/0", headers=h).json())

    assert db.render_previews() == 1
    # 다시 돌려도 바뀐 곡이 없습니다.
    assert db.render_previews() == 0

    songs = {s["song_id"]: s for s in client.get("/api/pre/manifest/0", headers=h).json()}
    assert "preview" not in songs[2]
    entry = songs[1]["preview"]
    assert entry["path"] == "preview_ebms.ogg" and entry["size"] > 0
    assert all(f["path"] != "preview_ebms.ogg" for f in songs[1]["files"])

    asset = client.get("/api/pre/asset/0", headers=h)
    with zipfile.ZipFile(io.BytesIO(asset.content)) as zf:
        data = zf.read("1/preview_ebms.ogg")
        assert f"{zf.getinfo('1/preview_ebms.ogg').CRC:08x}" == entry["crc32"]
    assert len(data) == entry["size"]

    r = client.get("/api/pre/song/1/file", params={"path": "preview_ebms.ogg"}, headers=h)
    assert r.status_code == 200 and r.content == data
    assert client.get("/api/pre/song/2/file", params={"path": "preview_ebms.ogg"}, headers=h).status_code == 404
    etag = r.headers["etag"]
    r = client.get("/api/pre/song/1/file", params={"path": "preview_ebms.ogg"}, headers={**h, "If-None-Match": etag})
    assert r.status_code == 304

    # 판이 바뀌면 다시 만듭니다.
    monkeypatch.setattr(preview, "PREVIEW_VERSION", preview.PREVIEW_VERSION + 1)
    assert db.render_previews() == 1
