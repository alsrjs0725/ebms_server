"""차트를 그 곡의 키음으로 끝까지 렌더링해 비교합니다(같은 곡인지, 다른 곡 키음으로 울리는지).

키음 파일 이름(01.ogg 등)은 곡마다 겹치므로, 이름 대신 실제로 날 소리를 비교합니다.

- 렌더: preview.py의 차트 해석·키음 찾기·풀기를 그대로 쓰고, 전체 길이를 RATE Hz 모노로 섞습니다.
  볼륨·패닝·BGA는 보지 않습니다(비교하는 양쪽이 같은 규칙이므로).
- 분기(#RANDOM): 미리듣기처럼 항상 1이 나온 것으로 보고 차트당 한 번만 렌더합니다. 분기마다 내용이 다른 차트는
  다른 곡으로 나올 수 있고, 그건 받아들입니다(2026-11-09 결정, 분기 수만큼 늘던 렌더 시간·메모리를 없앰).
  예전에 분기마다 렌더해 저장한 특징(여러 개)도 그대로 비교합니다. 둘 중 하나라도 SAME 이상이면 같은 곡입니다.
- 특징: 로그 간격 BANDS개 대역 에너지의 로그 값을 시간으로 미분해 양수만 남깁니다(소리가 시작하는 타이밍, onset).
  재인코딩·음질 차이에는 둔하고, 다른 곡 키음이 끼면 타이밍·음색이 달라져 크게 떨어집니다.
- 순서 일치율: 시간을 빼고 키음이 울리는 순서(찾은 파일 경로, 확장자 제외)만 ORDER_GRAM개씩 이어 겹치는 비율입니다.
  BPM·STOP으로 곡을 늘린 차트(1000초여왕 등)는 렌더 타이밍이 달라 렌더 일치율이 낮으므로 이것으로 봅니다.
  키음을 다르게 나눈 차분은 순서가 달라지므로 렌더 일치율로 봅니다. 둘 중 하나라도 SAME 이상이면 같은 곡입니다.
- 렌더 일치율: 두 렌더의 onset 곡선 상호상관에서 ±MAX_LAG_SECONDS 안의 봉우리 LAG_CANDIDATES개를 지연 후보로 두고,
  SEGMENT_SECONDS 구간마다 가장 잘 맞는 후보의 상관이 SEGMENT_CORR 이상인 구간 비율입니다. 양방향 중 작은 값.
  STOP·BPM 기믹으로 중간부터 어긋나는 차트도 후보 지연이 여러 개라 맞춰집니다.
- 끝나지 않는 차트(아주 느린 BPM, 거대한 마디 길이·STOP 등): MAX_SECONDS 뒤의 소리는 버리고, 소리 수는
  MAX_SOUNDS, 한 곡 렌더 시간은 deadline까지만 씁니다. 잘린 경우 결과에 표시합니다.

2026-10-29 release 실험(곡 zip, gingerrush 원본): 같은 곡 0.78~1.00, 다른 곡 0.00~0.10.
"""
import logging
import math
import pathlib
import posixpath
import tempfile
from collections.abc import Callable

import numpy as np

from . import cache, constant, decode_cache, preview

logger = logging.getLogger(__name__)

RATE = 11025
# 특징 프레임 간격(샘플)과 FFT 길이. 약 43프레임/초
HOP = 256
WINDOW = 2048
# 60Hz~5kHz를 로그 간격으로 나눈 대역 수
BANDS = 24
BAND_LOW, BAND_HIGH = 60.0, 5000.0
# 비교 구간 길이(초). 반씩 겹쳐 셉니다.
SEGMENT_SECONDS = 9.3
# 구간이 맞았다고 보는 상관계수
SEGMENT_CORR = 0.5
# 이 값보다 onset이 약한 구간은 무음으로 보고 세지 않습니다.
FLOOR = 3.5  # 약 30dB(로그 에너지)
SILENCE = 0.3
# 지연 후보를 찾는 범위(초)와 후보 수, 후보 사이 최소 간격(초)
MAX_LAG_SECONDS = 30.0
LAG_CANDIDATES = 8
LAG_GAP_SECONDS = 0.25
# 같은 곡 / 다른 곡 판정 기준(일치율). 그 사이는 판정 보류
SAME = 0.7
DIFFERENT = 0.2
# 순서 일치율의 묶음 길이와, 판정하려면 필요한 최소 묶음 수
ORDER_GRAM = 4
ORDER_MIN = 50
# 끝나지 않는 차트 대비 한도
MAX_SECONDS = 1800.0
MAX_SOUNDS = 200_000
# 이보다 큰 차트 파일은 렌더하지 않습니다. 보통 차트는 1MB 안쪽이고, 수백 MB짜리 장난 차트(CRAZYBUS 등)는 파싱·분기 복사만으로
# 메모리를 10GB 넘게 써서 색인 프로세스가 OOM으로 죽습니다.
MAX_CHART_BYTES = 8 * 1024 * 1024
# 분기 쌍 비교 한도(차트 둘을 비교할 때)
MAX_PAIRS = 512


class Fingerprint:
    """차트 하나의 렌더 특징. features는 렌더마다 하나(지금은 하나, 예전 색인은 분기마다 하나), orders는 그 키음 순서 묶음 집합,
    truncated는 한도 때문에 잘렸는지, sounds는 (찾은 키음 수, 쓰는 키음 수)."""

    __slots__ = ("features", "orders", "truncated", "sounds")

    def __init__(
        self,
        features: list[np.ndarray],
        orders: list[frozenset] | None = None,
        truncated: bool = False,
        sounds: tuple[int, int] = (0, 0),
    ):
        self.features = features
        self.orders = orders if orders is not None else []
        self.truncated = truncated
        self.sounds = sounds

    def __repr__(self) -> str:
        return f"Fingerprint({len(self.features)} branches, truncated={self.truncated}, sounds={self.sounds})"


# ---- 렌더 ----


def _sounds(chart: bytes, seconds: float = MAX_SECONDS) -> tuple[list[preview.Sound], bool]:
    """차트의 소리 중 렌더할 것(seconds 안, MAX_SOUNDS개까지)과 잘렸는지(앞부분만 렌더할 때는 잘렸다고 하지 않음)."""
    sounds = preview.parse_chart(chart).sounds
    kept = [s for s in sounds if math.isfinite(s.time) and 0 <= s.time < seconds and math.isfinite(s.offset)]
    if seconds < MAX_SECONDS:
        return kept[:MAX_SOUNDS], len(kept) > MAX_SOUNDS
    truncated = len(kept) < len(sounds) or len(kept) > MAX_SOUNDS
    return kept[:MAX_SOUNDS], truncated


def mix(sounds: list[preview.Sound], samples: dict[str, np.ndarray], seconds: float = MAX_SECONDS) -> np.ndarray:
    """소리를 처음부터 끝까지(seconds까지) 섞습니다."""
    limit = int(seconds * RATE)
    segments = []
    end = 0
    for s in sounds:
        data = samples.get(s.name)
        if data is None:
            continue
        seg = data[max(0, int(round(s.offset * RATE))) :]
        if s.end is not None and math.isfinite(s.end):
            seg = seg[: max(0, int(round((s.end - s.time) * RATE)))]
        pos = int(round(s.time * RATE))
        n = min(len(seg), limit - pos)
        if n > 0:
            segments.append((pos, seg[:n]))
            end = max(end, pos + n)
    buf = np.zeros(end, dtype=np.float32)
    for pos, seg in segments:
        buf[pos : pos + len(seg)] += seg
    return buf


def features(buf: np.ndarray) -> np.ndarray:
    """(프레임, BANDS) onset 특징."""
    frames = (len(buf) - WINDOW) // HOP + 1
    if frames <= 0:
        return np.zeros((0, BANDS), dtype=np.float32)
    freqs = np.fft.rfftfreq(WINDOW, 1 / RATE)
    edges = np.geomspace(BAND_LOW, BAND_HIGH, BANDS + 1)
    band = np.searchsorted(edges, freqs, side="right") - 1
    keep = (band >= 0) & (band < BANDS)
    window = np.hanning(WINDOW).astype(np.float32)
    out = np.empty((frames, BANDS), dtype=np.float32)
    # 긴 곡에서 메모리를 아끼려고 나눠 계산합니다.
    step = 4096
    for start in range(0, frames, step):
        count = min(step, frames - start)
        index = (start + np.arange(count))[:, None] * HOP + np.arange(WINDOW)[None, :]
        spec = np.abs(np.fft.rfft(buf[index] * window, axis=1))[:, keep]
        energy = np.zeros((count, BANDS), dtype=np.float64)
        np.add.at(energy.T, band[keep], spec.T)
        level = np.log1p(energy * 10)
        # 프레임에서 가장 센 밴드보다 FLOOR 넘게 약한 밴드(창 누설 등)는 0으로 둡니다. 안 그러면 소리가 시작할 때
        # 모든 밴드가 함께 튀어 박자만 같고 음색이 다른 소리도 같다고 봅니다.
        out[start : start + count] = np.maximum(level - (level.max(1, keepdims=True) - FLOOR), 0)
    return np.maximum(np.diff(out, axis=0, prepend=out[:1]), 0)


def fingerprints(
    names: list[str], read: Callable[[str], bytes], charts: list[str], nice: bool = True, seconds: float = MAX_SECONDS, work_dir: pathlib.Path | None = None,
) -> dict[str, Fingerprint]:
    """곡 안 차트들의 렌더 특징. names는 곡 안 모든 파일 경로, read(경로)는 그 내용, charts는 렌더할 차트 경로입니다.
    키음은 내용(sha256) 기준으로 한 번만 풉니다(decode_cache). #RANDOM은 1로 보고 차트당 한 번 렌더합니다. nice는 ffmpeg 우선순위를 낮출지(commit 중에는 False). seconds는 앞에서부터 렌더할
    길이(업로드 검색용은 앞부분만), work_dir은 키음을 잠깐 풀어 둘 폴더(기본 cache.work_dir()). ffmpeg가 없으면 RuntimeError."""
    ffmpeg = preview._ffmpeg()
    if ffmpeg is None:
        raise RuntimeError("ffmpeg not found")
    resolver = preview._Resolver(names)
    parsed: dict[str, tuple[list[preview.Sound], bool]] = {}
    for path in charts:
        try:
            content = read(path)
            if len(content) > MAX_CHART_BYTES:
                logger.warning(f"render_match: skipped huge chart[{path}] {len(content)} bytes")
                continue
            parsed[path] = _sounds(content, seconds)
            del content
        except Exception as e:  # 깨진 차트는 렌더하지 않습니다.
            logger.info(f"render_match: parse failed[{path}]: {e}")
    targets: dict[str, dict[str, str | None]] = {}
    for path, (sounds, _) in parsed.items():
        base = posixpath.dirname(path)
        found = targets.setdefault(path, {})
        for s in sounds:
            if s.name not in found:
                found[s.name] = resolver.find(base, s.name)
    members = sorted({t for found in targets.values() for t in found.values() if t})
    decoded: dict[str, np.ndarray] = {}
    if members:
        with tempfile.TemporaryDirectory(dir=work_dir or cache.work_dir()) as tmp:
            disk, hashes = [], []
            for i, member in enumerate(members):
                target = pathlib.Path(tmp) / f"{i}{posixpath.splitext(member)[1].lower()}"
                data = read(member)
                target.write_bytes(data)
                disk.append(target)
                hashes.append(decode_cache.sha256_bytes(data))
            found_all = decode_cache.decode_sounds(ffmpeg, disk, seconds, rate=RATE, nice=nice, hashes=hashes)
            for member, data in zip(members, found_all):
                if data is not None:
                    decoded[member] = data
    out = {}
    for path, (sounds, truncated) in parsed.items():
        found = targets[path]
        samples = {name: decoded[t] for name, t in found.items() if t in decoded}
        # 확장자만 다른 키음(.wav/.ogg)은 같은 자리로 봅니다.
        heard = sorted(
            (round(s.time, 4), posixpath.splitext(found[s.name].lower())[0])
            for s in sounds
            if found.get(s.name) in decoded
        )
        feats = [features(mix(sounds, samples, seconds))]
        orders = [order_grams([target for _, target in heard])]
        out[path] = Fingerprint(feats, orders, truncated, (len(samples), len(found)))
    return out


# ---- 비교 ----


def order_grams(sequence: list[str]) -> frozenset:
    """키음 순서에서 ORDER_GRAM개씩 이은 묶음들. 같은 시각에 울리는 소리는 이름순으로 둡니다(차트마다 적은 순서가 달라도 같게)."""
    return frozenset(tuple(sequence[i : i + ORDER_GRAM]) for i in range(len(sequence) - ORDER_GRAM + 1))


def order_similarity(a: frozenset, b: frozenset) -> float | None:
    """순서 묶음이 겹치는 비율(작은 쪽 기준). 어느 쪽이든 ORDER_MIN개보다 적으면 None."""
    if len(a) < ORDER_MIN or len(b) < ORDER_MIN:
        return None
    return len(a & b) / min(len(a), len(b))


def _lags(a: np.ndarray, b: np.ndarray) -> list[int]:
    """a[t] ≈ b[t - 지연]이 되는 지연 후보(프레임). onset 합 곡선 상호상관의 봉우리 순."""
    ea = a.sum(1) - a.sum(1).mean()
    eb = b.sum(1) - b.sum(1).mean()
    n = 1 << int(math.ceil(math.log2(len(a) + len(b))))
    xc = np.fft.irfft(np.fft.rfft(ea, n) * np.conj(np.fft.rfft(eb, n)), n)
    span = int(MAX_LAG_SECONDS * RATE / HOP)
    lags = np.r_[np.arange(0, min(span, len(a)) + 1), -np.arange(1, min(span, len(b)) + 1)]
    gap = max(1, int(LAG_GAP_SECONDS * RATE / HOP))
    out: list[int] = []
    for lag in lags[np.argsort(xc[lags % n])[::-1]]:
        if all(abs(int(lag) - o) >= gap for o in out):
            out.append(int(lag))
            if len(out) == LAG_CANDIDATES:
                break
    return out


def _rate(a: np.ndarray, b: np.ndarray) -> float | None:
    """a의 구간 중 b와 맞는 비율. a가 무음이면 None."""
    seg = min(int(SEGMENT_SECONDS * RATE / HOP), len(a), len(b))
    if seg < 2:
        return None
    lags = _lags(a, b)
    hits = total = 0
    for start in list(range(0, len(a) - seg + 1, max(1, seg // 2))):
        x = a[start : start + seg].reshape(-1)
        if np.abs(x).max() < SILENCE:
            continue
        x = x - x.mean()
        nx = np.linalg.norm(x) + 1e-6
        best = 0.0
        for lag in lags:
            other = start - lag
            if other < 0 or other + seg > len(b):
                continue
            y = b[other : other + seg].reshape(-1)
            y = y - y.mean()
            best = max(best, float(x @ y) / (nx * (np.linalg.norm(y) + 1e-6)))
        total += 1
        hits += best >= SEGMENT_CORR
    return hits / total if total else None


def similarity(a: np.ndarray, b: np.ndarray) -> float | None:
    """렌더 특징 둘의 일치율(0~1, 양방향 중 작은 값). 어느 쪽이 무음이면 None."""
    ab, ba = _rate(a, b), _rate(b, a)
    if ab is None or ba is None:
        return None
    return min(ab, ba)


def score(a: Fingerprint, b: Fingerprint, paired: bool = False) -> float | None:
    """두 차트의 렌더 일치율. 분기 중 가장 잘 맞는 쌍의 값입니다(SAME 이상이 나오면 더 보지 않음).
    paired는 같은 차트를 두 파일 묶음으로 렌더한 경우(등록 전후 비교)로, 같은 분기끼리만 비교합니다."""
    if paired:
        pairs = list(zip(a.features, b.features))
    else:
        pairs = [(x, y) for x in a.features for y in b.features]
    best = None
    for x, y in pairs[:MAX_PAIRS]:
        value = similarity(x, y)
        if value is not None and (best is None or value > best):
            best = value
            if best >= SAME:
                break
    return best


def order_score(a: Fingerprint, b: Fingerprint) -> float | None:
    """두 차트의 순서 일치율. 분기 중 가장 잘 맞는 쌍의 값입니다."""
    values = [v for x in a.orders for y in b.orders if (v := order_similarity(x, y)) is not None]
    return max(values, default=None)


def same_song(a: Fingerprint, b: Fingerprint) -> tuple[str, float | None, float | None]:
    """다른 차트 둘이 같은 곡인지: (판정, 렌더 일치율, 순서 일치율). 둘 중 하나라도 SAME 이상이면 "same".
    한도 때문에 잘린 차트는 "different"로 판정하지 않고 "unsure"로 둡니다(길이 때문에 잘못 나누지 않게)."""
    order = order_score(a, b)
    if order is not None and order >= SAME:
        return "same", None, order
    render = score(a, b)
    best = max((v for v in (render, order) if v is not None), default=None)
    result = verdict(best)
    if result == "different" and (a.truncated or b.truncated):
        result = "unsure"
    return result, render, order


def verdict(value: float | None) -> str:
    """일치율 → "same" | "different" | "unsure"(중간이거나 소리가 없음)."""
    if value is None:
        return "unsure"
    return "same" if value >= SAME else "different" if value <= DIFFERENT else "unsure"
