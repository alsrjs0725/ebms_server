"""같은 경로에 내용이 다른 키음·이미지·영상을 같은 소재로 볼지(media_match) 테스트합니다."""
import shutil
import subprocess

import numpy as np
import pytest

from ebms_server import constant, importer, media_match

from test_batch import put_song, song_zip
from test_blob_storage import database  # noqa: F401  (fixture 재사용)

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def ffmpeg(tmp_path, name, *args) -> bytes:
    out = tmp_path / name
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", *args, str(out)], check=True)
    return out.read_bytes()


def sine(tmp_path, name, freq, seconds=1.0, *codec):
    return ffmpeg(tmp_path, name, "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}", *codec)


def image(tmp_path, name, pattern="testsrc", *codec):
    return ffmpeg(tmp_path, name, "-f", "lavfi", "-i", f"{pattern}=size=256x256:duration=1", "-frames:v", "1", *codec)


def video(tmp_path, name, pattern="testsrc", seconds=6, *codec):
    return ffmpeg(tmp_path, name, "-f", "lavfi", "-i", f"{pattern}=size=160x120:rate=25:duration={seconds}", *codec)


def judge(tmp_path, path, old: bytes, new: bytes) -> bool:
    new_file = tmp_path / "new" / path
    new_file.parent.mkdir(parents=True, exist_ok=True)
    new_file.write_bytes(new)
    return path in media_match.same_files([(path, old, new_file)])


def test_same_sound_math():
    rate = media_match.SOUND_RATE
    t = np.arange(rate) / rate
    a = np.sin(2 * np.pi * 440 * t).astype(np.float32)
    # 음량만 다르고 인코더 지연(20ms)이 있어도 같음
    delayed = np.concatenate([np.zeros(int(0.02 * rate), np.float32), 0.5 * a])
    assert media_match.same_sound(a, delayed)
    assert not media_match.same_sound(a, np.sin(2 * np.pi * 523 * t).astype(np.float32))
    # 길이가 많이 다르면 다름
    assert not media_match.same_sound(a, np.concatenate([a, a]))
    # 둘 다 무음이면 같음, 하나만 무음이면 다름
    assert media_match.same_sound(np.zeros(rate, np.float32), np.zeros(rate, np.float32))
    assert not media_match.same_sound(a, np.zeros(rate, np.float32))


def test_not_media_is_not_checked(tmp_path):
    assert media_match.kind("a.bms") is None and media_match.kind("readme.txt") is None
    assert media_match.kind("k.WAV") == "sound" and media_match.kind("b/x.Png") == "image" and media_match.kind("m.mpg") == "video"
    assert not judge(tmp_path, "readme.txt", b"a", b"b")


@needs_ffmpeg
def test_reencoded_sound_is_same(tmp_path):
    wav = sine(tmp_path, "a.wav", 440)
    ogg = sine(tmp_path, "a.ogg", 440, 1.0, "-c:a", "libvorbis", "-q:a", "2")
    mp3 = sine(tmp_path, "a.mp3", 440, 1.0)
    other = sine(tmp_path, "b.wav", 660)
    assert judge(tmp_path, "k.wav", wav, ogg)
    assert judge(tmp_path, "k2.wav", wav, mp3)
    assert not judge(tmp_path, "k3.wav", wav, other)
    # 풀 수 없는 파일은 다른 것(충돌)으로 봅니다.
    assert not judge(tmp_path, "k4.wav", wav, b"not audio")


@needs_ffmpeg
def test_recompressed_image_is_same(tmp_path):
    png = image(tmp_path, "a.png")
    jpg = image(tmp_path, "a.jpg", "testsrc", "-q:v", "3")
    bmp = image(tmp_path, "a.bmp")
    other = image(tmp_path, "b.png", "smptebars")
    # 일부만 고친 그림(256×256 중 16×16 칸)은 다름
    edited = ffmpeg(tmp_path, "c.png", "-i", str(tmp_path / "a.png"), "-vf", "drawbox=x=10:y=10:w=16:h=16:color=red:t=fill")
    assert judge(tmp_path, "bg.png", png, jpg)
    assert judge(tmp_path, "bg.bmp", png, bmp)
    # 크기만 줄인 그림(256→128)도 같음
    small = ffmpeg(tmp_path, "s.png", "-i", str(tmp_path / "a.png"), "-vf", "scale=128:128")
    assert judge(tmp_path, "bg4.png", png, small)
    assert not judge(tmp_path, "bg2.png", png, other)
    assert not judge(tmp_path, "bg3.png", png, edited)


@needs_ffmpeg
def test_reencoded_video_is_same(tmp_path):
    mpg = video(tmp_path, "a.mpg", "testsrc", 6, "-c:v", "mpeg1video", "-q:v", "2")
    mp4 = video(tmp_path, "a.mp4", "testsrc", 6, "-c:v", "mpeg4", "-q:v", "8")
    other = video(tmp_path, "b.mpg", "smptebars", 6, "-c:v", "mpeg1video")
    longer = video(tmp_path, "c.mpg", "testsrc", 12, "-c:v", "mpeg1video")
    assert judge(tmp_path, "m.mpg", mpg, mp4)
    assert not judge(tmp_path, "m2.mpg", mpg, other)
    assert not judge(tmp_path, "m3.mpg", mpg, longer)


@needs_ffmpeg
def test_merge_keeps_same_media_without_conflict(database, tmp_path):
    a = b"#TITLE A\n"
    wav = sine(tmp_path, "a.wav", 440)
    put_song(constant.TMP_DIR / "g" / "song1", {"a.bms": a, "k.wav": wav, "x.wav": wav})
    importer.import_tmp()

    ogg_as_wav = sine(tmp_path, "a.ogg", 440, 1.0, "-c:a", "libvorbis", "-q:a", "2")
    other = sine(tmp_path, "b.wav", 880)
    pkg = put_song(constant.TMP_DIR / "g" / "pkg", {"a.bms": a, "k.wav": ogg_as_wav, "x.wav": other})
    songs = importer.import_tmp()
    assert songs == [{
        "folder": "g/pkg", "song_id": 1, "new_song": False, "charts": 1, "new_charts": 0,
        "conflicts": ["x.wav"], "similar_files": ["k.wav"],
    }]
    assert song_zip(1) == {"a.bms": a, "k.wav": wav, "x.wav": wav}
    assert pkg.exists()


@needs_ffmpeg
def test_merge_matches_other_extension(database, tmp_path):
    """확장자만 다른 키음(k.wav ↔ k.ogg)도 같은 파일로 보고 비교합니다."""
    a = b"#TITLE A\n"
    wav = sine(tmp_path, "a.wav", 440)
    other = sine(tmp_path, "b.wav", 880)
    put_song(constant.TMP_DIR / "g" / "song1", {"a.bms": a, "k.wav": wav, "x.wav": wav})
    importer.import_tmp()

    ogg = sine(tmp_path, "a.ogg", 440, 1.0, "-c:a", "libvorbis", "-q:a", "2")
    other_ogg = ffmpeg(tmp_path, "b.ogg", "-i", str(tmp_path / "b.wav"), "-c:a", "libvorbis")
    put_song(constant.TMP_DIR / "g" / "pkg", {"a.bms": a, "k.ogg": ogg, "x.ogg": other_ogg, "n.ogg": ogg})
    songs = importer.import_tmp()
    assert songs == [{
        "folder": "g/pkg", "song_id": 1, "new_song": False, "charts": 1, "new_charts": 0,
        "added_files": ["n.ogg"], "conflicts": ["x.ogg"], "similar_files": ["k.ogg"],
    }]
    assert set(song_zip(1)) == {"a.bms", "k.wav", "x.wav", "n.ogg"}
    assert other  # b.wav는 b.ogg를 만드는 데 씁니다.


@needs_ffmpeg
def test_merge_matches_bga_by_name(database, tmp_path):
    """BGA는 이미지·영상이 한 자리입니다: 확장자만 다른 같은 그림은 같고, 이미지 자리에 영상은 충돌입니다."""
    a = b"#TITLE A\n"
    bmp = image(tmp_path, "a.bmp")
    put_song(constant.TMP_DIR / "g" / "song1", {"a.bms": a, "bg.bmp": bmp, "mv.bmp": bmp})
    importer.import_tmp()

    png = image(tmp_path, "a.png")
    mpg = video(tmp_path, "a.mpg", "testsrc", 2, "-c:v", "mpeg1video")
    put_song(constant.TMP_DIR / "g" / "pkg", {"a.bms": a, "BG.PNG": png, "mv.mpg": mpg})
    songs = importer.import_tmp()
    assert songs[0]["similar_files"] == ["BG.PNG"] and songs[0]["conflicts"] == ["mv.mpg"]
    assert set(song_zip(1)) == {"a.bms", "bg.bmp", "mv.bmp"}


def test_slot():
    assert media_match.slot("Sub/K.WAV") == media_match.slot("sub/k.ogg") == ("sub/k", "sound")
    assert media_match.slot("bg.bmp") == media_match.slot("bg.mpg") == media_match.slot("bg.tga") == ("bg", "bga")
    assert media_match.slot("bg.bmp") != media_match.slot("bg.wav")
    assert media_match.slot("a.bms") is None
