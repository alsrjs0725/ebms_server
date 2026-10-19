"""곡 미리듣기 음원(PREVIEW_NAME) 렌더링.

Qwilight는 곡 고르는 화면에서 미리듣기할 때 키음·BGA를 모두 읽습니다(플레이 파일이라 티켓을 씁니다).
곡에 preview 음원이 있으면 그것만 재생하므로, preview 음원이 없는 곡은 서버가 차트의 키음을 섞어
짧은 미리듣기 음원을 만들어 사전 파일로 줍니다. 클라이언트는 이 파일을 곡 폴더에 보여 주고,
그 직후 Qwilight가 읽는 플레이 파일은 받지 않습니다(ebms_client).

- 저작권·용량 때문에 일부러 음질을 낮춥니다: PREVIEW_SECONDS초, 모노 SAMPLE_RATE Hz,
  Vorbis 품질 VORBIS_QUALITY(가장 낮음).
- Qwilight의 하이라이트 위치(highlight)부터 자릅니다. Qwilight는 preview 음원을 처음부터 재생하므로
  "하이라이트부터 미리듣기" 설정과 상관없이 하이라이트가 들립니다.
- 곡에 이미 preview 음원(사전 파일인 오디오)이 있으면 만들지 않습니다.
- 차트는 곡에서 가장 큰 차트 하나를 씁니다(난이도가 달라도 배경음은 같습니다).
- 결과는 song_preview 테이블에 두고, 매니페스트의 "preview"와 사전 청크에 넣습니다(db.py).
- 키음을 풀고 다시 묶는 데 ffmpeg를 씁니다. 없으면 만들지 않고 로그만 남깁니다.
"""
import bisect
import logging
import math
import os
import pathlib
import posixpath
import re
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Callable

import numpy as np

from . import constant

logger = logging.getLogger(__name__)

# 곡 zip의 차트 폴더에 이 이름으로 보입니다. Qwilight는 이름에 PREVIEW가 든 오디오를, beatoraja는 preview로
# 시작하는 오디오를 미리듣기로 씁니다.
PREVIEW_NAME = "preview_ebms.ogg"
# 렌더링 방식(길이·음질 등)을 바꾸면 올립니다. 서버 시작 뒤 다른 판의 미리듣기를 모두 다시 만듭니다.
PREVIEW_VERSION = 2
PREVIEW_SECONDS = 30.0
# 끝부분을 줄여 가는 길이(초)
FADE_SECONDS = 2.0
SAMPLE_RATE = 22050
# ffmpeg libvorbis -q:a (-1 ~ 10). 모노 22050Hz에서 약 30kbps입니다.
VORBIS_QUALITY = "-1"
# ffmpeg 한 번에 푸는 키음 수
DECODE_BATCH = 64
# 렌더링 ffmpeg 하나의 시간 한도(초)
FFMPEG_TIMEOUT = 120

CHART_ENCODINGS = ("utf-8", "shift_jis", "cp949")
_CHANNEL = re.compile(rb"#(\d{3})([0-9A-Za-z]{2}):(.*)")


class Sound:
    """미리듣기에 섞을 소리 하나. time은 차트 시작부터 초, offset은 키음 파일 안에서 시작할 위치(초),
    end는 끊을 시각(초, 없으면 끝까지)."""

    __slots__ = ("time", "name", "offset", "end")

    def __init__(self, time: float, name: str, offset: float = 0.0, end: float | None = None):
        self.time = time
        self.name = name
        self.offset = offset
        self.end = end

    def __repr__(self) -> str:
        return f"Sound({self.time:.3f}, {self.name!r}, {self.offset:.3f}, {self.end})"


class Chart:
    """차트에서 미리듣기에 쓰는 것. sounds는 시각 순 소리, notes는 플레이 노트 시각(롱노트는 끝도 하나),
    length는 마지막 노트·배경음·BGA 시각(초)."""

    __slots__ = ("sounds", "notes", "length")

    def __init__(self, sounds: list[Sound], notes: list[float], length: float):
        self.sounds = sounds
        self.notes = notes
        self.length = length


def highlight(notes: list[float], length: float) -> float:
    """Qwilight의 하이라이트 위치(초). 곡을 100칸으로 나눠 플레이 노트 밀도가 앞 칸보다 가장 크게 오르는 칸을 찾되
    곡 가운데일수록 가중치를 줍니다(반원). 그 칸의 한 칸 앞이 하이라이트입니다. 노트가 없으면 0.
    Qwilight BaseCompiler.PostCompile의 "건너뛰기와 하이라이트 계산"과 같습니다."""
    if not notes or length <= 0:
        return 0.0
    width = length / 100
    counts = [0] * 100
    for t in notes:
        i = int(t // width)
        # 곡 끝(length)에 걸친 노트는 Qwilight도 세지 않습니다.
        if 0 <= i < 100:
            counts[i] += 1
    best = -math.inf
    position = 0.0
    for i in range(99, 0, -1):
        value = (counts[i] - counts[i - 1]) * math.sqrt(50 * 50 - (50 - i) ** 2)
        if value >= best:
            best = value
            position = length * (i - 1) / 100
    return position


def _decode(value: bytes) -> str:
    for encoding in CHART_ENCODINGS:
        try:
            return value.decode(encoding)
        except UnicodeDecodeError:
            continue
    return value.decode("utf-8", "replace")


def _float(value: bytes | str | None, default: float | None = None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class _Timeline:
    """박자(4분음표 단위) → 초. BPM 변경과 정지를 박자 순으로 둡니다."""

    def __init__(self, bpm: float, changes: list[tuple[float, float]], stops: list[tuple[float, float]]):
        self.bpm = bpm if bpm and bpm > 0 else 130.0
        # (박자, 순서, 종류, 값). 같은 박자에서는 BPM 변경 → 소리 → 정지 순입니다.
        self.events = sorted(
            [(beat, 0, "bpm", value) for beat, value in changes if value and value > 0]
            + [(beat, 2, "stop", value) for beat, value in stops if value and value > 0]
        )

    def times(self, beats: list[float]) -> list[float]:
        """오름차순으로 정렬된 박자들의 시각(초)."""
        out = []
        time = last = 0.0
        bpm = self.bpm
        i = 0
        for beat in beats:
            while i < len(self.events) and self.events[i][0] <= beat and (
                self.events[i][0] < beat or self.events[i][1] < 1
            ):
                ebeat, _, kind, value = self.events[i]
                time += (ebeat - last) * 60.0 / bpm
                last = ebeat
                if kind == "bpm":
                    bpm = value
                else:
                    time += value * 60.0 / bpm
                i += 1
            out.append(time + (beat - last) * 60.0 / bpm)
        return out


def _bms_lines(chart: bytes) -> list[bytes]:
    """#RANDOM 블록은 항상 1이 나온 것으로 보고 고른 줄만 남깁니다."""
    out = []
    randoms: list[int] = []
    ifs: list[list[bool]] = []  # [바깥이 켜짐, 이미 고름, 지금 켜짐]
    for raw in chart.splitlines():
        line = raw.strip()
        if not line.startswith(b"#"):
            continue
        word = line.split(None, 1)
        command = word[0].upper()
        arg = word[1].strip() if len(word) > 1 else b""
        active = ifs[-1][2] if ifs else True
        if command in (b"#RANDOM", b"#RONDAM"):
            randoms.append(1)
        elif command == b"#SETRANDOM":
            randoms.append(int(_float(arg, 1) or 1))
        elif command == b"#ENDRANDOM":
            if randoms:
                randoms.pop()
        elif command == b"#IF":
            on = active and bool(randoms) and _float(arg) == randoms[-1]
            ifs.append([active, on, on])
        elif command == b"#ELSEIF" and ifs:
            outer, taken, _ = ifs[-1]
            on = outer and not taken and bool(randoms) and _float(arg) == randoms[-1]
            ifs[-1] = [outer, taken or on, on]
        elif command == b"#ELSE" and ifs:
            outer, taken, _ = ifs[-1]
            ifs[-1] = [outer, True, outer and not taken]
        elif command in (b"#ENDIF", b"#END") and ifs:
            ifs.pop()
        elif active:
            out.append(line)
    return out


def bms_chart(chart: bytes) -> Chart:
    """BMS 차트. 소리는 배경음 01, 보이는 노트 1x·2x, 롱노트 시작 5x·6x입니다. 플레이 노트는 1x·2x(LNOBJ 포함)와
    5x·6x의 시작·끝, 곡 길이에는 BGA(04·06·07)도 셉니다."""
    lines = _bms_lines(chart)
    base62 = any(line.upper().startswith(b"#BASE") and line.split()[-1] == b"62" for line in lines)

    def key(value: bytes) -> str:
        text = value.decode("ascii", "replace")
        return text if base62 else text.upper()

    bpm = 130.0
    wavs: dict[str, str] = {}
    ext_bpms: dict[str, float] = {}
    stop_values: dict[str, float] = {}
    lnobj = None
    lengths: dict[int, float] = {}
    notes: list[tuple[int, str, bytes]] = []
    for line in lines:
        m = _CHANNEL.fullmatch(line)
        if m:
            measure, channel, data = int(m.group(1)), m.group(2).decode().upper(), m.group(3).strip()
            if channel == "02":
                lengths[measure] = _float(data, 1.0) or 1.0
            else:
                notes.append((measure, channel, data))
            continue
        parts = line.split(None, 1)
        head = parts[0][1:]
        name = head.upper()
        value = parts[1].strip() if len(parts) > 1 else b""
        if name == b"BPM":
            bpm = _float(value, bpm)
        elif name == b"LNOBJ" and value:
            lnobj = key(value[:2])
        elif len(name) == 5 and name.startswith(b"WAV") and value:
            wavs[key(head[3:5])] = _decode(value)
        elif len(name) == 5 and name.startswith(b"BPM"):
            ext_bpms[key(head[3:5])] = _float(value, 0.0)
        elif len(name) == 7 and name.startswith(b"EXBPM"):
            ext_bpms[key(head[5:7])] = _float(value, 0.0)
        elif len(name) == 6 and name.startswith(b"STOP"):
            stop_values[key(head[4:6])] = _float(value, 0.0)

    starts = {}
    total = 0.0
    last_measure = max((n[0] for n in notes), default=0)
    for m in range(last_measure + 2):
        starts[m] = total
        total += lengths.get(m, 1.0) * 4

    changes: list[tuple[float, float]] = []
    stops: list[tuple[float, float]] = []
    sounds: list[tuple[float, str]] = []
    long_notes: dict[str, list[tuple[float, str]]] = {}
    plays: list[float] = []
    others: list[float] = []
    for measure, channel, data in notes:
        count = len(data) // 2
        if count == 0:
            continue
        span = lengths.get(measure, 1.0) * 4
        for i in range(count):
            pair = data[i * 2 : i * 2 + 2]
            if pair == b"00":
                continue
            beat = starts[measure] + span * i / count
            if channel == "03":
                try:
                    changes.append((beat, float(int(pair, 16))))
                except ValueError:
                    pass
            elif channel == "08":
                changes.append((beat, ext_bpms.get(key(pair), 0.0)))
            elif channel == "09":
                # #STOPxx는 4/4 한 마디를 192로 나눈 단위입니다.
                stops.append((beat, stop_values.get(key(pair), 0.0) / 48))
            elif channel == "01" or (channel[0] in "12" and channel[1] in "123456789"):
                if channel != "01":
                    plays.append(beat)
                    if lnobj is not None and key(pair) == lnobj:
                        continue
                sounds.append((beat, key(pair)))
            elif channel[0] in "56" and channel[1] in "123456789":
                long_notes.setdefault(channel, []).append((beat, key(pair)))
                plays.append(beat)
            elif channel in ("04", "06", "07"):
                others.append(beat)
    for events in long_notes.values():
        events.sort()
        # 롱노트 채널은 시작·끝이 번갈아 나옵니다. 시작만 소리가 납니다.
        sounds.extend(events[::2])

    sounds.sort()
    beats = sorted({beat for beat, _ in sounds} | set(plays) | set(others))
    at = dict(zip(beats, _Timeline(bpm, changes, stops).times(beats)))
    return Chart(
        [Sound(at[b], wavs[k]) for b, k in sounds if k in wavs],
        sorted(at[b] for b in plays),
        max(at.values(), default=0.0),
    )


def bmson_chart(data: dict) -> Chart:
    """bmson 차트. 이어 재생(c) 노트는 같은 채널의 앞 노트에 이어지는 위치부터, 다음 노트가 나오면 끊습니다
    (bmson은 한 키음 파일을 잘라 씁니다). 플레이 노트는 x가 있는 노트(롱노트는 끝 y+l도)입니다."""
    info = data.get("info") if isinstance(data.get("info"), dict) else {}
    resolution = _float(info.get("resolution"), 240.0) or 240.0
    if resolution <= 0:
        resolution = 240.0
    bpm = _float(info.get("init_bpm"), 130.0) or 130.0

    def beat(y) -> float:
        return (_float(y, 0.0) or 0.0) / resolution

    def items(name: str) -> list[dict]:
        value = data.get(name)
        return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []

    changes = [(beat(e.get("y")), _float(e.get("bpm"), 0.0)) for e in items("bpm_events")]
    stops = [(beat(e.get("y")), beat(e.get("duration"))) for e in items("stop_events")]
    channels = []
    beats = []
    plays = []
    for channel in items("sound_channels"):
        raw = channel.get("notes")
        raw = [n for n in (raw if isinstance(raw, list) else []) if isinstance(n, dict)]
        for n in raw:
            if _float(n.get("x"), 0.0):
                plays.append(beat(n.get("y")))
                if (_float(n.get("l"), 0.0) or 0.0) > 0:
                    plays.append(beat(n.get("y")) + beat(n.get("l")))
        name = channel.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        notes = sorted((beat(n.get("y")), bool(n.get("c"))) for n in raw)
        channels.append((name.strip(), notes))
        beats.extend(b for b, _ in notes)
    bga = data.get("bga") if isinstance(data.get("bga"), dict) else {}
    for key in ("bga_events", "layer_events", "poor_events"):
        events = bga.get(key)
        beats.extend(beat(e.get("y")) for e in (events if isinstance(events, list) else []) if isinstance(e, dict))
    beats = sorted(set(beats) | set(plays))
    at = dict(zip(beats, _Timeline(bpm, changes, stops).times(beats)))

    sounds = []
    for name, notes in channels:
        restart = None
        for i, (b, cont) in enumerate(notes):
            time = at[b]
            if not cont or restart is None:
                restart = time
            end = at[notes[i + 1][0]] if i + 1 < len(notes) else None
            sounds.append(Sound(time, name, time - restart, end))
    sounds.sort(key=lambda s: s.time)
    return Chart(sounds, sorted(at[b] for b in plays), max(at.values(), default=0.0))


def parse_chart(chart: bytes) -> Chart:
    from .db import _bmson

    data = _bmson(chart)
    return bmson_chart(data) if data is not None else bms_chart(chart)


def clip_start(chart: Chart) -> float:
    """미리듣기를 시작할 시각(초). 하이라이트부터 PREVIEW_SECONDS초가 곡 안에 들도록 앞당기되 첫 소리보다 앞서지 않습니다."""
    first = chart.sounds[0].time
    return max(first, min(highlight(chart.notes, chart.length), chart.length - PREVIEW_SECONDS))


def _is_audio(path: str) -> bool:
    return posixpath.splitext(path)[1].lower() in constant.AUDIO_FORMAT


def preview_path(files: list[dict]) -> str | None:
    """곡이 이미 preview 음원(사전 파일인 오디오)을 가졌으면 None. 아니면 미리듣기를 둘 경로(가장 큰 차트의 폴더)."""
    if any(f.get("kind") == "pre" and _is_audio(f["path"]) for f in files):
        return None
    charts = [f for f in files if posixpath.splitext(f["path"])[1].lower() in constant.BMS_FORMAT]
    if not charts:
        return None
    chart = max(charts, key=lambda f: (f["size"], f["path"]))
    path = posixpath.join(posixpath.dirname(chart["path"]), PREVIEW_NAME)
    if any(f["path"].lower() == path.lower() for f in files):
        return None
    return path


class _Resolver:
    """차트에 쓴 키음 이름 → 곡 zip 안 경로. 대소문자를 무시하고, 확장자가 달라도(.wav로 쓰고 .ogg가 있음) 찾습니다."""

    def __init__(self, names: list[str]):
        self.exact = {}
        self.stems: dict[str, list[str]] = {}
        for name in names:
            if not _is_audio(name):
                continue
            lower = name.lower()
            self.exact.setdefault(lower, name)
            self.stems.setdefault(posixpath.splitext(lower)[0], []).append(name)

    def find(self, base: str, name: str) -> str | None:
        path = posixpath.normpath(posixpath.join(base, name.strip().replace("\\", "/"))).lower()
        if path in self.exact:
            return self.exact[path]
        found = self.stems.get(posixpath.splitext(path)[0])
        return sorted(found)[0] if found else None


def _ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def _run(cmd: list[str], data: bytes | None = None) -> bytes:
    kwargs = {}
    if os.name == "posix":
        # 미리듣기는 뒤에서 천천히 만들어도 되므로 다운로드·임포트에 CPU를 양보합니다.
        kwargs["preexec_fn"] = lambda: os.nice(10)
    result = subprocess.run(
        cmd, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=FFMPEG_TIMEOUT, check=False, **kwargs
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", "replace").strip()[-500:] or f"ffmpeg exit {result.returncode}")
    return result.stdout


def _decode_sounds(ffmpeg: str, paths: list[pathlib.Path], seconds: float) -> list[np.ndarray | None]:
    """키음 파일들을 모노 float32 샘플로 풉니다. 풀지 못한 파일은 None."""
    out: list[np.ndarray | None] = [None] * len(paths)

    def args(i: int, src: int, dst: pathlib.Path) -> list[str]:
        return ["-map", f"{src}:a:0", "-ac", "1", "-ar", str(SAMPLE_RATE), "-t", f"{seconds:.3f}", "-f", "f32le", str(dst)]

    for start in range(0, len(paths), DECODE_BATCH):
        batch = list(range(start, min(start + DECODE_BATCH, len(paths))))
        raws = {i: paths[i].with_suffix(".raw") for i in batch}
        cmd = [ffmpeg, "-v", "error", "-nostdin", "-y"]
        for i in batch:
            cmd += ["-i", str(paths[i])]
        for n, i in enumerate(batch):
            cmd += args(i, n, raws[i])
        try:
            _run(cmd)
            todo = []
        except (RuntimeError, subprocess.TimeoutExpired):
            # 하나라도 못 풀면 전체가 실패하므로 하나씩 다시 풉니다.
            todo = batch
        for i in todo:
            try:
                _run([ffmpeg, "-v", "error", "-nostdin", "-y", "-i", str(paths[i]), *args(i, 0, raws[i])])
            except (RuntimeError, subprocess.TimeoutExpired):
                raws[i].unlink(missing_ok=True)
        for i in batch:
            if raws[i].exists():
                out[i] = np.fromfile(raws[i], dtype="<f4")
                raws[i].unlink()
    return out


def mix(sounds: list[Sound], samples: dict[str, np.ndarray], start: float) -> np.ndarray:
    """start초부터 PREVIEW_SECONDS초를 섞습니다. 소리가 없으면 빈 배열."""
    length = int(PREVIEW_SECONDS * SAMPLE_RATE)
    buf = np.zeros(length, dtype=np.float32)
    last = 0
    for s in sounds:
        data = samples.get(s.name)
        if data is None:
            continue
        pos = int(round((s.time - start) * SAMPLE_RATE))
        if pos >= length:
            continue
        seg = data[int(round(s.offset * SAMPLE_RATE)) :]
        if s.end is not None:
            seg = seg[: max(0, int(round((s.end - s.time) * SAMPLE_RATE)))]
        if pos < 0:
            # start 앞에서 울리기 시작한 소리는 start에 걸친 부분부터 섞습니다.
            seg = seg[-pos:]
            pos = 0
        n = min(len(seg), length - pos)
        if n <= 0:
            continue
        buf[pos : pos + n] += seg[:n]
        last = max(last, pos + n)
    buf = buf[:last]
    if not len(buf):
        return buf
    peak = float(np.max(np.abs(buf)))
    if peak < 1e-4:
        return buf[:0]
    if peak > 0.98:
        buf *= 0.98 / peak
    fade = min(len(buf), int(FADE_SECONDS * SAMPLE_RATE) if last >= length else int(0.05 * SAMPLE_RATE))
    if fade > 0:
        buf[-fade:] *= np.linspace(1.0, 0.0, fade, dtype=np.float32)
    return buf


def render(files: list[dict], read: Callable[[dict], bytes]) -> tuple[str, bytes] | None:
    """곡의 미리듣기를 만듭니다. files는 매니페스트 항목, read(항목)는 그 파일의 내용입니다(곡 zip 전체를 읽지 않도록
    차트와 쓰는 키음만 읽습니다). (곡 안 경로, ogg 데이터)를 반환하고, 이미 preview가 있거나 소리가 없으면 None.
    ffmpeg가 없으면 RuntimeError."""
    path = preview_path(files)
    if path is None:
        return None
    ffmpeg = _ffmpeg()
    if ffmpeg is None:
        raise RuntimeError("ffmpeg not found")
    charts = [f for f in files if posixpath.splitext(f["path"])[1].lower() in constant.BMS_FORMAT]
    chart_entry = max(charts, key=lambda f: (f["size"], f["path"]))
    by_path = {f["path"]: f for f in files}
    chart = parse_chart(read(chart_entry))
    if not chart.sounds:
        return None
    start = clip_start(chart)
    # start 앞에서 시작한 소리도 길면 start까지 울리므로 섞습니다(끊는 시각이 start 전인 것만 뺍니다).
    sounds = chart.sounds[: bisect.bisect_left([s.time for s in chart.sounds], start + PREVIEW_SECONDS)]
    sounds = [s for s in sounds if s.end is None or s.end > start]
    resolver = _Resolver(list(by_path))
    base = posixpath.dirname(chart_entry["path"])
    targets: dict[str, str | None] = {}
    for s in sounds:
        if s.name not in targets:
            targets[s.name] = resolver.find(base, s.name)
    members = sorted({t for t in targets.values() if t})
    if not members:
        return None
    with tempfile.TemporaryDirectory(dir=constant.TMP_DIR) as tmp:
        files_on_disk = []
        for i, member in enumerate(members):
            target = pathlib.Path(tmp) / f"{i}{posixpath.splitext(member)[1].lower()}"
            target.write_bytes(read(by_path[member]))
            files_on_disk.append(target)
        decoded = _decode_sounds(
            ffmpeg, files_on_disk, PREVIEW_SECONDS + max(s.offset + max(0.0, start - s.time) for s in sounds)
        )
    by_member = dict(zip(members, decoded))
    samples = {name: by_member[t] for name, t in targets.items() if t and by_member.get(t) is not None}
    buf = mix(sounds, samples, start)
    if not len(buf):
        return None
    data = _run(
        [
            ffmpeg, "-v", "error", "-nostdin",
            "-f", "f32le", "-ar", str(SAMPLE_RATE), "-ac", "1", "-i", "pipe:0",
            "-c:a", "libvorbis", "-q:a", VORBIS_QUALITY,
            "-map_metadata", "-1", "-fflags", "+bitexact", "-flags:a", "+bitexact",
            "-f", "ogg", "pipe:1",
        ],
        buf.astype("<f4").tobytes(),
    )
    return path, data


_worker_lock = threading.Lock()
_worker: threading.Thread | None = None
_again = threading.Event()


def kick() -> None:
    """미리듣기가 없는(또는 판이 다른) 곡의 미리듣기를 뒤에서 만듭니다. 이미 도는 중이면 끝난 뒤 한 번 더 돕니다."""
    global _worker
    if not constant.PREVIEW_ENABLED:
        return
    with _worker_lock:
        if _worker is not None and _worker.is_alive():
            _again.set()
            return
        _again.clear()
        _worker = threading.Thread(target=_work, name="render_previews", daemon=True)
        _worker.start()


def _work() -> None:
    from .db import Database

    while True:
        try:
            Database().render_previews()
        except Exception:
            logger.exception("render_previews failed")
        with _worker_lock:
            if not _again.is_set():
                return
            _again.clear()
