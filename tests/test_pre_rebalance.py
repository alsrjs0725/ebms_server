"""사전 청크 재배치 계획(pre_rebalance.plan)을 테스트합니다. DB에서 옮기는 것은 test_tables에서 봅니다."""
from ebms_server import pre_rebalance
from ebms_server.pre_rebalance import plan


def apply(members, result):
    """계획대로 옮긴 뒤의 {청크: 곡들}. 새 청크는 100번부터 붙입니다."""
    out = {chunk: list(songs) for chunk, songs in members.items()}
    next_id = 100
    for move in result.moves:
        target = move.target
        if target is None:
            target, next_id = next_id, next_id + 1
        for song, source in move.songs:
            out[source].remove(song)
            out.setdefault(target, []).append(song)
    return {chunk: sorted(songs) for chunk, songs in out.items() if songs}


def sigs_of(chunks, song_sig):
    return {chunk: {song_sig.get(s, "") for s in songs} for chunk, songs in chunks.items()}


def test_signature():
    assert pre_rebalance.signature([3, 1, 3]) == "1,3"
    assert pre_rebalance.signature([]) == ""


def test_plan_separates_table_combinations():
    # 곡 1: 표 1, 곡 2: 표 2, 곡 3: 표 1·2(두 표에 다 있음), 곡 4: 표 없음
    members = {0: [1, 2, 3, 4]}
    song_sig = {1: "1", 2: "2", 3: "1,2"}
    result = plan(members, song_sig, {0: None}, {0: 400}, budget=10 ** 9, cap=8)
    after = apply(members, result)
    # 청크마다 한 조합만 남아, 어느 표를 고르든 받는 곡은 모두 고른 표의 곡입니다.
    assert all(len(sigs) == 1 for sigs in sigs_of(after, song_sig).values())
    assert sorted(after.values()) == [[1], [2], [3], [4]]
    assert result.sources == [0]


def test_plan_keeps_stored_signature_and_fills_room():
    # 청크 0은 표 1 청크인데 곡 2가 표 2로 바뀌었습니다. 표 2 청크 1에 자리가 있어 그리로 옮깁니다.
    members = {0: [1, 2], 1: [3]}
    song_sig = {1: "1", 2: "2", 3: "2"}
    result = plan(members, song_sig, {0: "1", 1: "2"}, {0: 200, 1: 100}, budget=10 ** 9, cap=2)
    assert result.adopt == {}
    assert [(m.target, m.sig, m.songs) for m in result.moves] == [(1, "2", [(2, 0)])]
    assert apply(members, result) == {0: [1], 1: [2, 3]}


def test_plan_respects_budget_and_picks_largest_waste_first():
    members = {0: [1, 2], 1: [3, 4]}
    song_sig = {1: "1", 3: "1"}
    sizes = {0: 100, 1: 1000}
    result = plan(members, song_sig, {}, sizes, budget=500, cap=8)
    # 낭비가 큰 청크 1을 먼저 고릅니다. 한도를 넘어도 하나는 고르고, 청크 0은 다음 번에 합니다.
    assert result.sources == [1]
    result = plan(members, song_sig, {}, sizes, budget=1100, cap=8)
    assert result.sources == [1, 0]
    # 같은 조합의 곡은 한 청크로 모입니다.
    after = apply(members, result)
    assert sorted(after.values()) == [[1, 3], [2, 4]]


def test_plan_pure_chunks_untouched():
    members = {0: [1, 2], 1: [3]}
    song_sig = {1: "1", 2: "1"}
    result = plan(members, song_sig, {0: None, 1: ""}, {0: 10, 1: 10}, budget=10 ** 9, cap=8)
    assert result.moves == [] and result.sources == []
    # 저장된 조합이 없던 청크는 조합만 적습니다.
    assert result.adopt == {0: "1"}


def test_plan_splits_by_cap():
    members = {0: list(range(1, 8))}
    song_sig = {i: "1" for i in range(1, 5)}
    result = plan(members, song_sig, {0: ""}, {0: 70}, budget=10 ** 9, cap=3)
    after = apply(members, result)
    assert all(len(songs) <= 3 for songs in after.values())
    assert all(len(s) == 1 for s in sigs_of(after, song_sig).values())
    assert sorted(s for songs in after.values() for s in songs) == list(range(1, 8))
