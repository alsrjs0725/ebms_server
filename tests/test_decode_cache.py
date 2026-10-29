"""파일 내용 기준 디코드·결과 캐시(decode_cache) 테스트."""
import shutil
import subprocess

import numpy as np
import pytest

from ebms_server import decode_cache, preview

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def test_lru_drops_oldest():
    lru = decode_cache._LRU(10)
    lru.put("a", 1, 4)
    lru.put("b", 2, 4)
    assert lru.get("a") == 1  # a를 최근으로
    lru.put("c", 3, 4)
    assert lru.get("b") is None and lru.get("a") == 1 and lru.get("c") == 3
    lru.put("huge", 4, 11)
    assert lru.get("huge") is None


@needs_ffmpeg
def test_decode_sounds_once_per_content(tmp_path, monkeypatch):
    decode_cache.clear()
    a = tmp_path / "a.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.5", str(a)], check=True)
    b = tmp_path / "sub" / "b.wav"  # 이름만 다르고 내용은 같음
    b.parent.mkdir()
    shutil.copy(a, b)
    bad = tmp_path / "bad.ogg"
    bad.write_bytes(b"not audio")
    calls = []
    real = preview._decode_sounds
    monkeypatch.setattr(preview, "_decode_sounds", lambda ffmpeg, paths, *args, **kw: calls.append(len(paths)) or real(ffmpeg, paths, *args, **kw))
    out = decode_cache.decode_sounds(preview._ffmpeg(), [a, b, bad], None, rate=8000)
    assert calls == [2]
    assert out[0] is not None and np.array_equal(out[0], out[1]) and out[2] is None
    assert not out[0].flags.writeable
    again = decode_cache.decode_sounds(preview._ffmpeg(), [b, bad], None, rate=8000)
    assert calls == [2] and np.array_equal(again[0], out[0]) and again[1] is None
    # 길이·샘플레이트가 다르면 따로 풉니다.
    decode_cache.decode_sounds(preview._ffmpeg(), [a], None, rate=11025)
    assert calls == [2, 1]
    decode_cache.clear()
