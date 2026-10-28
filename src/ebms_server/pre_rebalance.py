"""사전 청크 재배치. 곡을 속한 난이도표 조합(서명)이 같은 곡끼리 사전 청크에 묶습니다.

서명은 곡의 차트가 든 난이도표 id를 오름차순으로 쉼표로 이은 문자열입니다(표에 없는 곡은 ''). 한 청크의 곡이 모두 같은
서명이면 어떤 표를 고른 클라이언트든 그 청크를 받을 때 고른 표의 곡만 받습니다. 한 곡이 여러 표에 있으면 그 표 조합의
청크에 들어가고, 그 청크에는 표 태그가 여러 개 붙습니다(tables.chunk_tags).

새 곡은 임포트할 때 서명에 맞는 청크에 넣습니다(Database._place_song). 난이도표를 더하거나 다시 읽으면 서명이 바뀐 곡이
생기므로, 여러 서명이 섞인 청크에서 낭비가 큰 순으로 PRE_REBALANCE_BYTES만큼씩 곡을 옮깁니다(Database.rebalance_pre_chunks).
옮길 일감이 남으면 PRE_REBALANCE_PAUSE_SECONDS 쉬었다가 이어서 합니다.
"""
import logging
import threading
from collections import defaultdict
from dataclasses import dataclass, field

from . import constant

logger = logging.getLogger(__name__)


def signature(table_ids) -> str:
    """난이도표 id들의 서명."""
    return ",".join(str(i) for i in sorted(set(table_ids)))


def _ids(sig: str) -> set[str]:
    return set(sig.split(",")) if sig else set()


@dataclass
class Move:
    """target 청크(None이면 새 청크)에 서명 sig의 곡들을 넣습니다. songs는 (곡 id, 원래 청크 id)."""
    target: int | None
    sig: str
    songs: list[tuple[int, int]] = field(default_factory=list)


@dataclass
class Plan:
    # 청크에 남는 곡들의 서명이 저장된 서명과 다른 청크 {청크 id: 서명}. 다시 만들지 않고 서명만 고칩니다.
    adopt: dict[int, str]
    moves: list[Move]
    # 곡이 빠져 다시 만들 청크
    sources: list[int]


def plan(
    members: dict[int, list[int]],
    song_sig: dict[int, str],
    chunk_sig: dict[int, str | None],
    chunk_size: dict[int, int],
    budget: int,
    cap: int,
) -> Plan:
    """한 번의 재배치 계획을 세웁니다.

    members는 {청크 id: 든 곡 id들}, song_sig는 {곡 id: 지금 서명}, chunk_sig·chunk_size는 청크에 저장된 서명(None이면 아직 없음)과
    바이트 크기입니다. 청크마다 저장된 서명(없으면 가장 많은 곡의 서명)의 곡은 남기고 나머지를 옮깁니다.
    낭비는 "그 청크의 표 태그 하나를 고른 클라이언트가 받는, 그 표에 없는 곡의 바이트"를 태그마다 더한 값이고(곡 크기는 청크 크기를
    곡 수로 나눈 값), 낭비가 큰 청크부터 청크 크기의 합이 budget을 넘기 전까지 고릅니다(적어도 하나는 고름).
    곡이 빠지는 청크끼리 남는 서명이 같으면 적은 쪽의 곡도 옮겨 모읍니다. 옮기는 곡은 같은 서명의, 곡이 cap보다 적은
    청크(이번에 다시 만드는 청크 먼저)를 채우고 남으면 새 청크에 넣습니다."""
    keep: dict[int, str] = {}
    waste: dict[int, float] = {}
    for chunk, songs in members.items():
        groups: dict[str, list[int]] = defaultdict(list)
        for song in songs:
            groups[song_sig.get(song, "")].append(song)
        stored = chunk_sig.get(chunk)
        keep[chunk] = stored if stored in groups else max(groups, key=lambda g: (len(groups[g]), g))
        if len(groups) > 1:
            per_song = chunk_size.get(chunk, 0) / len(songs)
            tags = set().union(*(_ids(g) for g in groups))
            waste[chunk] = sum(
                per_song * len(group) for g, group in groups.items() for tag in tags if tag not in _ids(g)
            )
    adopt = {chunk: sig for chunk, sig in keep.items() if chunk_sig.get(chunk) != sig}

    sources: list[int] = []
    spent = 0
    for chunk in sorted(waste, key=lambda c: (-waste[c], c)):
        cost = chunk_size.get(chunk, 0)
        if sources and spent + cost > budget:
            continue
        sources.append(chunk)
        spent += cost

    outgoing: dict[str, list[tuple[int, int]]] = defaultdict(list)
    kept: dict[str, list[tuple[int, list[int]]]] = defaultdict(list)
    for chunk in sources:
        stay = []
        for song in members[chunk]:
            sig = song_sig.get(song, "")
            if sig != keep[chunk]:
                outgoing[sig].append((song, chunk))
            else:
                stay.append(song)
        kept[keep[chunk]].append((chunk, stay))
    # 곡이 빠진 청크끼리 같은 서명이면 적은 청크의 곡을 많은 청크로 모아, 덜 찬 청크가 늘지 않게 합니다.
    emptied: set[int] = set()
    for sig, stays in kept.items():
        stays.sort(key=lambda item: (-len(item[1]), item[0]))
        need = -(-sum(len(stay) for _, stay in stays) // cap)
        for chunk, stay in stays[need:]:
            outgoing[sig].extend((song, chunk) for song in stay)
            emptied.add(chunk)

    # 같은 서명의 빈자리. 다시 만드는 청크(곡이 빠지는 청크)를 먼저 채우고, 그다음 곡이 많은 청크부터 채웁니다.
    rebuilt = set(sources)
    room: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for chunk, songs in members.items():
        if chunk in emptied:
            continue
        if chunk in rebuilt:
            left = sum(1 for s in songs if song_sig.get(s, "") == keep[chunk])
        elif all(song_sig.get(s, "") == keep[chunk] for s in songs):
            left = len(songs)
        else:
            continue
        if left < cap:
            room[keep[chunk]].append((chunk, left))
    for slots in room.values():
        slots.sort(key=lambda slot: (slot[0] not in rebuilt, -slot[1], slot[0]))

    moves: list[Move] = []
    for sig in sorted(outgoing):
        songs = sorted(outgoing[sig])
        for chunk, left in room.get(sig, []):
            if not songs:
                break
            take, songs = songs[: cap - left], songs[cap - left :]
            moves.append(Move(chunk, sig, take))
        while songs:
            take, songs = songs[:cap], songs[cap:]
            moves.append(Move(None, sig, take))
    return Plan(adopt, moves, sources)


_worker_lock = threading.Lock()
_worker: threading.Thread | None = None
_again = threading.Event()


def kick() -> None:
    """사전 청크 재배치를 뒤에서 돌립니다. 이미 도는 중이면 끝난 뒤 한 번 더 돕니다."""
    global _worker
    if not constant.PRE_REBALANCE_ENABLED:
        return
    with _worker_lock:
        if _worker is not None and _worker.is_alive():
            _again.set()
            return
        _again.clear()
        _worker = threading.Thread(target=_work, name="rebalance_pre_chunks", daemon=True)
        _worker.start()


def _work() -> None:
    from .db import Database

    while True:
        try:
            moved = Database().rebalance_pre_chunks()
        except Exception:
            logger.exception("rebalance_pre_chunks failed")
            moved = 0
        with _worker_lock:
            if not moved and not _again.is_set():
                return
            _again.clear()
        if moved:
            # 옮길 곡이 남았을 수 있으므로 쉬었다가 다시 봅니다. 남은 게 없으면 다음 번에 0을 돌려주고 끝납니다.
            threading.Event().wait(constant.PRE_REBALANCE_PAUSE_SECONDS)
