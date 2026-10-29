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


def noise(tmp_path, name, *codec, seconds=2.0):
    """대역이 넓은 소리(분홍 잡음). 손실 압축하면 고음이 잘려 대역폭이 줄어듭니다."""
    return ffmpeg(tmp_path, name, "-f", "lavfi", "-i", f"anoisesrc=color=pink:duration={seconds}:sample_rate=44100:seed=1", *codec)


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
    # 음량만 다르면 같음
    assert media_match.same_sound(a, 0.5 * a)
    # 싱크가 어긋나면(1ms 늦게 시작) 지연을 보정하지 않으므로 다름
    delayed = np.concatenate([np.zeros(int(0.001 * rate), np.float32), a])[: len(a)]
    assert not media_match.same_sound(a, delayed)
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
    wav = noise(tmp_path, "a.wav")
    put_song(constant.TMP_DIR / "g" / "song1", {"a.bms": a, "k.wav": wav, "x.wav": wav})
    importer.import_tmp()

    # 같은 소재를 손실 압축한 새 파일은 더 작아도 기존 무손실 파일을 둡니다.
    ogg_as_wav = noise(tmp_path, "a.ogg", "-c:a", "libvorbis", "-q:a", "2")
    other = sine(tmp_path, "b.wav", 880, 2.0)
    pkg = put_song(constant.TMP_DIR / "g" / "pkg", {"a.bms": a, "k.wav": ogg_as_wav, "x.wav": other})
    songs = importer.import_tmp()
    assert songs == [{
        "folder": "g/pkg", "song_id": 1, "new_song": False, "charts": 1, "new_charts": 0,
        "conflicts": ["x.wav"], "similar_files": ["k.wav"],
    }]
    assert len(ogg_as_wav) < len(wav)
    assert song_zip(1) == {"a.bms": a, "k.wav": wav, "x.wav": wav}
    assert pkg.exists()


@needs_ffmpeg
def test_merge_replaces_with_better_quality(database, tmp_path):
    """기존이 저품질(낮은 비트레이트 ogg)이고 새 파일이 무손실이면 더 커도 새 파일로 바꿉니다."""
    a = b"#TITLE A\n"
    low = noise(tmp_path, "low.ogg", "-c:a", "libvorbis", "-q:a", "0")
    put_song(constant.TMP_DIR / "g" / "song1", {"a.bms": a, "k.ogg": low})
    importer.import_tmp()

    wav = noise(tmp_path, "a.wav")
    put_song(constant.TMP_DIR / "g" / "pkg", {"a.bms": a, "k.wav": wav})
    songs = importer.import_tmp()
    assert songs[0]["similar_files"] == ["k.wav"] and songs[0]["replaced_files"] == ["k.wav"]
    # 확장자가 달라도 같은 자리이므로 기존 k.ogg를 빼고 k.wav를 넣습니다.
    assert song_zip(1) == {"a.bms": a, "k.wav": wav}


@needs_ffmpeg
def test_merge_matches_other_extension(database, tmp_path):
    """확장자만 다른 키음(k.wav ↔ k.ogg)도 같은 파일로 보고 비교합니다."""
    a = b"#TITLE A\n"
    wav = noise(tmp_path, "a.wav")
    put_song(constant.TMP_DIR / "g" / "song1", {"a.bms": a, "k.wav": wav, "x.wav": wav})
    importer.import_tmp()

    ogg = noise(tmp_path, "a.ogg", "-c:a", "libvorbis", "-q:a", "2")
    other_ogg = sine(tmp_path, "b.ogg", 880, 2.0, "-c:a", "libvorbis")
    put_song(constant.TMP_DIR / "g" / "pkg", {"a.bms": a, "k.ogg": ogg, "x.ogg": other_ogg, "n.ogg": ogg})
    songs = importer.import_tmp()
    assert songs == [{
        "folder": "g/pkg", "song_id": 1, "new_song": False, "charts": 1, "new_charts": 0,
        "added_files": ["n.ogg"], "conflicts": ["x.ogg"], "similar_files": ["k.ogg"],
    }]
    # 무손실인 기존 k.wav를 둡니다.
    assert set(song_zip(1)) == {"a.bms", "k.wav", "x.wav", "n.ogg"}


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
    # 크기·형식이 같은 그림이면 작은 쪽(png)을 남깁니다.
    assert set(song_zip(1)) == {"a.bms", "BG.PNG", "mv.bmp"}


def test_slot():
    assert media_match.slot("Sub/K.WAV") == media_match.slot("sub/k.ogg") == ("sub/k", "sound")
    assert media_match.slot("bg.bmp") == media_match.slot("bg.mpg") == media_match.slot("bg.tga") == ("bg", "bga")
    assert media_match.slot("bg.bmp") != media_match.slot("bg.wav")
    assert media_match.slot("a.bms") is None


def _better(tmp_path, path, old: bytes, new: bytes) -> bool:
    new_file = tmp_path / "new" / path
    new_file.parent.mkdir(parents=True, exist_ok=True)
    new_file.write_bytes(new)
    return path in media_match.better_new([(path, old, new_file)])


@needs_ffmpeg
def test_better_quality(tmp_path):
    wav = noise(tmp_path, "a.wav")
    mp3_low = noise(tmp_path, "low.mp3", "-c:a", "libmp3lame", "-b:a", "64k")
    mp3_high = noise(tmp_path, "high.mp3", "-c:a", "libmp3lame", "-b:a", "320k")
    ogg_low = noise(tmp_path, "low.ogg", "-c:a", "libvorbis", "-q:a", "0")
    # 대역폭: 무손실 > 고음질 손실 > 저음질 손실
    assert not _better(tmp_path, "k1.wav", wav, mp3_high)
    assert _better(tmp_path, "k2.wav", mp3_low, wav)
    assert _better(tmp_path, "k3.mp3", mp3_low, mp3_high)
    assert not _better(tmp_path, "k4.mp3", mp3_high, mp3_low)
    assert _better(tmp_path, "k5.ogg", ogg_low, mp3_high)
    # 같은 파일이면 바꾸지 않습니다.
    assert not _better(tmp_path, "k6.wav", wav, wav)

    big = image(tmp_path, "big.png")
    small = ffmpeg(tmp_path, "small.png", "-i", str(tmp_path / "big.png"), "-vf", "scale=128:128")
    jpg = image(tmp_path, "a.jpg", "testsrc", "-q:v", "3")
    assert _better(tmp_path, "bg.png", small, big)
    assert not _better(tmp_path, "bg2.png", big, small)
    # 해상도가 같으면 무손실 형식
    assert _better(tmp_path, "bg3.png", jpg, big)

    hi = ffmpeg(tmp_path, "hi.mpg", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=4", "-c:v", "mpeg1video")
    lo = ffmpeg(tmp_path, "lo.mpg", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=25:duration=4", "-c:v", "mpeg1video")
    assert _better(tmp_path, "mv.mpg", lo, hi)
    assert not _better(tmp_path, "mv2.mpg", hi, lo)


def test_bandwidth_math():
    rate = media_match.BAND_RATE
    rng = np.random.default_rng(1)
    full = rng.standard_normal(rate)
    # 8kHz 위를 잘라 낸 잡음
    spectrum = np.fft.rfft(full)
    spectrum[int(8000 * len(full) / rate):] = 0
    cut = np.fft.irfft(spectrum, len(full))
    assert media_match.bandwidth(full) > 20000
    assert 7000 < media_match.bandwidth(cut) < 9000
    assert media_match.bandwidth(np.zeros(rate)) == 0


@needs_ffmpeg
def test_cached_verdicts_skip_ffmpeg(tmp_path, monkeypatch):
    """충돌 적용·다시 commit 때 같은 내용의 쌍은 판정·품질을 다시 풀지 않습니다(decode_cache)."""
    from ebms_server import decode_cache, preview

    decode_cache.clear()
    wav = noise(tmp_path, "a.wav")
    mp3 = noise(tmp_path, "b.mp3", "-c:a", "libmp3lame", "-b:a", "64k")
    new_file = tmp_path / "new" / "k.wav"
    new_file.parent.mkdir(parents=True)
    new_file.write_bytes(wav)
    pairs = [("k.wav", mp3, new_file)]
    first = media_match.same_files(pairs), media_match.better_new(pairs)
    assert first == ({"k.wav"}, {"k.wav"})

    def fail(*args, **kwargs):
        raise AssertionError("ffmpeg ran again")

    monkeypatch.setattr(preview, "_run", fail)
    assert (media_match.same_files(pairs), media_match.better_new(pairs)) == first
    decode_cache.clear()


@needs_ffmpeg
def test_better_quality_many(tmp_path):
    """여러 쌍을 나란히·묶어 읽어도 쌍마다 결과가 섞이지 않습니다."""
    from ebms_server import decode_cache

    decode_cache.clear()
    wav = noise(tmp_path, "a.wav")
    low = noise(tmp_path, "low.mp3", "-c:a", "libmp3lame", "-b:a", "64k")
    pairs, expected = [], set()
    for i in range(20):
        old, new = (low, wav) if i % 2 else (wav, low)
        new_file = tmp_path / "new" / f"k{i}.wav"
        new_file.parent.mkdir(parents=True, exist_ok=True)
        new_file.write_bytes(new)
        pairs.append((f"k{i}.wav", old, new_file))
        if i % 2:
            expected.add(f"k{i}.wav")
    assert media_match.better_new(pairs) == expected
    decode_cache.clear()
