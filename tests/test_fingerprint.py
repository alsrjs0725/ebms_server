"""지문 저장소(fingerprint): 특징을 넣고, 다른 곡의 같은 차트를 해시 후보 + 렌더 일치율로 찾기."""
import numpy as np

from ebms_server import constant, fingerprint, render_match


def _features(seed: int, frames: int = 3000) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return np.maximum(rng.normal(0, 1.5, (frames, render_match.BANDS)), 0).astype(np.float32)


def test_store_search_forget(monkeypatch, tmp_path):
    monkeypatch.setattr(constant, "RENDER_INDEX_DIR", tmp_path)
    con = fingerprint.connect()
    a, b = _features(1), _features(2)
    assert fingerprint.store(con, 1, "a.bms", [a]) is not None
    assert fingerprint.store(con, 2, "b.bms", [b]) is not None
    assert fingerprint.store(con, 3, "empty.bms", [np.zeros((0, render_match.BANDS), np.float32)]) is None
    fingerprint.mark_song(con, 1, "x", 1)
    con.commit()
    # 늦게 시작한 같은 소리는 곡 1로 찾고, 자기 곡은 빼고, 다른 소리는 일치율이 낮습니다.
    late = np.vstack([np.zeros((200, render_match.BANDS), np.float32), a])
    found = list(fingerprint.search(con, [late], exclude_song=9))
    assert found and found[0][:2] == (1, "a.bms") and found[0][2] >= render_match.SAME
    assert all(s != 2 or (v or 0) < render_match.SAME for s, _, v in found)
    assert not [r for r in fingerprint.search(con, [late], exclude_song=1) if r[0] == 1]
    assert not list(fingerprint.search(con, [late], exclude_song=9, skip=lambda song: song == 1))
    assert fingerprint.songs(con) == {1: "x"}
    fingerprint.forget_song(con, 1)
    assert fingerprint.compact(con) > 0
    assert not [r for r in fingerprint.search(con, [late], exclude_song=9) if r[0] == 1]
    con.close()
