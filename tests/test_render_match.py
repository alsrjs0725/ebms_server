"""차트 렌더 비교(render_match) 테스트: 분기 고정, 끝나지 않는 차트, 일치율."""
import shutil
import subprocess
import time

import numpy as np
import pytest

from ebms_server import preview, render_match

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def test_random_uses_first_branch_only():
    # 분기는 따로 렌더하지 않고 미리듣기처럼 #RANDOM을 1로 고정합니다(#IF 1 본문만 울림).
    chart = (
        b"#BPM 120\n#WAV01 a.wav\n#WAV02 b.wav\n"
        b"#RANDOM 2\n#IF 1\n#00101:01\n#ELSE\n#00101:02\n#ENDIF\n#ENDRANDOM\n"
    )
    sounds, truncated = render_match._sounds(chart)
    assert not truncated and [s.name for s in sounds] == ["a.wav"]


def test_endless_chart_is_clipped():
    # BPM이 아주 느려 수백 시간짜리 차트: MAX_SECONDS 뒤 소리는 버립니다.
    chart = b"#BPM 0.0001\n#WAV01 a.wav\n#00001:01\n#00101:01\n#99901:01\n"
    started = time.monotonic()
    sounds, truncated = render_match._sounds(chart)
    assert truncated and [s.time for s in sounds] == [0.0]
    buf = render_match.mix(sounds, {"a.wav": np.ones(100, dtype=np.float32)})
    assert len(buf) == 100
    assert time.monotonic() - started < 5


def test_mix_stays_within_limit():
    sounds = [preview.Sound(render_match.MAX_SECONDS - 0.5, "a.wav")]
    buf = render_match.mix(sounds, {"a.wav": np.ones(render_match.RATE * 10, dtype=np.float32)})
    assert len(buf) == int(render_match.MAX_SECONDS * render_match.RATE)


def pattern(seed: int, seconds: float = 60.0) -> np.ndarray:
    """박자마다 다른 소리(대역이 다른 잡음 덩어리)가 울리는 렌더."""
    rng = np.random.default_rng(seed)
    rate = render_match.RATE
    buf = np.zeros(int(seconds * rate), dtype=np.float32)
    t = 0.0
    while t < seconds - 1:
        n = int(0.15 * rate)
        burst = rng.standard_normal(n).astype(np.float32) * np.exp(-np.arange(n) / (0.03 * rate)).astype(np.float32)
        burst = np.convolve(burst, np.ones(rng.integers(1, 30)) / 10, mode="same").astype(np.float32)
        pos = int(t * rate)
        buf[pos : pos + n] += burst
        t += rng.choice([0.125, 0.25, 0.375, 0.5])
    return buf


def test_similarity_same_and_different():
    a = render_match.features(pattern(1))
    assert render_match.similarity(a, a) == 1.0
    # 소리가 조금 커지거나 뒤로 밀려도 같은 곡
    shifted = render_match.features(np.r_[np.zeros(render_match.RATE * 2, dtype=np.float32), pattern(1) * 0.5])
    assert render_match.similarity(a, shifted) >= render_match.SAME
    other = render_match.features(pattern(2))
    assert render_match.similarity(a, other) <= render_match.DIFFERENT
    assert render_match.similarity(a, render_match.features(np.zeros(render_match.RATE * 20, dtype=np.float32))) is None


def test_similarity_with_stop_in_middle():
    # 중간에 STOP처럼 3초 쉬고 이어지는 차트도 같은 곡
    base = pattern(3)
    half = len(base) // 2
    stopped = np.r_[base[:half], np.zeros(render_match.RATE * 3, dtype=np.float32), base[half:]]
    assert render_match.similarity(render_match.features(base), render_match.features(stopped)) >= render_match.SAME


def test_order_similarity():
    seq = [f"k{i * 7919 % 101}" for i in range(400)]
    a = render_match.order_grams(seq)
    assert render_match.order_similarity(a, a) == 1.0
    assert render_match.order_similarity(a, render_match.order_grams(seq[:200])) == 1.0
    assert render_match.order_similarity(a, render_match.order_grams([f"x{i}" for i in range(400)])) == 0.0
    assert render_match.order_similarity(a, render_match.order_grams(seq[:10])) is None


def test_same_song_uses_order_or_render_and_never_splits_truncated():
    one = render_match.features(pattern(1))
    two = render_match.features(pattern(2))
    seq = render_match.order_grams([f"k{i * 7919 % 101}" for i in range(400)])
    other = render_match.order_grams([f"x{i}" for i in range(400)])
    fp = render_match.Fingerprint
    # 렌더는 달라도 순서가 같으면 같은 곡(BPM으로 늘린 차트)
    assert render_match.same_song(fp([one], [seq]), fp([two], [seq]))[0] == "same"
    # 분기 중 하나라도 맞으면 같은 곡
    assert render_match.same_song(fp([two, one], [other]), fp([one], [seq]))[0] == "same"
    assert render_match.same_song(fp([one], [seq]), fp([two], [other]))[0] == "different"
    # 한도로 잘린 차트는 다른 곡으로 나누지 않음
    assert render_match.same_song(fp([one], [seq], truncated=True), fp([two], [other]))[0] == "unsure"


def tone(tmp_path, name, freq, seconds=0.3) -> bytes:
    out = tmp_path / name
    subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}", str(out)],
        check=True,
    )
    return out.read_bytes()


@needs_ffmpeg
def test_fingerprints_detect_other_song_sounds(tmp_path):
    """같은 차트를 올린 키음과 다른 곡 키음(같은 이름)으로 렌더하면 다르게 나옵니다(이번 오염 유형)."""
    rng = np.random.default_rng(0)
    rows = []
    for measure in range(1, 40):
        data = "".join(rng.choice(["01", "02", "03", "00", "00"]) for _ in range(16))
        rows.append(f"#{measure:03d}01:{data}\n".encode())
    chart = b"#BPM 150\n#WAV01 01.wav\n#WAV02 02.wav\n#WAV03 03.wav\n" + b"".join(rows)
    mine = {"x.bms": chart, "01.wav": tone(tmp_path, "a.wav", 200), "02.wav": tone(tmp_path, "b.wav", 900), "03.wav": tone(tmp_path, "c.wav", 3000)}
    theirs = {**mine, "01.wav": tone(tmp_path, "d.wav", 3000, 1.5), "02.wav": tone(tmp_path, "e.wav", 120, 0.05)}
    a = render_match.fingerprints(list(mine), mine.__getitem__, ["x.bms"])["x.bms"]
    b = render_match.fingerprints(list(theirs), theirs.__getitem__, ["x.bms"])["x.bms"]
    assert a.sounds == (3, 3) and not a.truncated
    assert render_match.score(a, a, paired=True) == 1.0
    assert render_match.score(a, b, paired=True) < render_match.SAME
