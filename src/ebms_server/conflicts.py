"""검토 대기 곡의 병합 충돌 해결. 기존 곡에 더하려다 내용이 달라 넣지 못한 파일(unit.result.songs[].conflicts)을
관리자가 기존 파일과 새 파일로 나란히 비교하고, 파일마다 "old"(기존 유지)나 "new"(새 파일로 바꿈)를 골라 그 곡만 다시 넣습니다.

- 고른 것은 stage.json의 unit.resolutions({새 파일 경로: "old" | "new"})에 두고, commit 때 SongBatch.add로 넘깁니다.
- 비교용 파일: 브라우저가 못 여는 형식은 ffmpeg로 바꿉니다. 이미지는 png, 키음은 ogg, 영상은 작은 webm
  (VIDEO_FPS 고정 프레임레이트, 키프레임 간격 VIDEO_GOP)이라 화면에서 프레임 번호 ÷ fps로 정확히 옮겨 갈 수 있습니다.
- 영상은 두 파일을 VIDEO_FPS로 풀어 프레임마다 차이를 재고, 다른 구간을 타임라인에 표시합니다.
- 채보로 들어보기: 차트를 해석해(preview.parse_chart) 충돌 키음을 뺀 소리로 바탕 음원(FLAC)을 만들고,
  충돌 키음은 시각표(events)로 줘서 브라우저가 기존·새 키음 중 고른 쪽을 겹쳐 재생합니다.
- 바꾼 파일·계산 결과는 곡 폴더 아래 `_diff/`에 둡니다(곡이 묶음에서 빠지면 같이 지워짐).
"""
import difflib
import hashlib
import json
import logging
import pathlib
import posixpath
import subprocess
import tempfile
import threading
import zipfile

import numpy as np

from . import db, media_match, preview, staging
from .db import Database
from .staging import StageError

logger = logging.getLogger(__name__)

CHOICES = ("old", "new")
DIFF_DIR = "_diff"
# 비교 화면에서 브라우저가 그대로 여는 형식
WEB_IMAGE = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
WEB_SOUND = (".wav", ".ogg", ".mp3", ".flac")
# 영상 비교: 이 프레임레이트로 바꾸고 프레임마다 비교합니다.
VIDEO_FPS = 30
VIDEO_WIDTH = 256
VIDEO_GOP = 10
VIDEO_SECONDS = media_match.VIDEO_SECONDS
VIDEO_CMP_SIZE = 32
# 프레임 평균 차이(0~1)가 이 값을 넘으면 다른 프레임
VIDEO_FRAME_DIFF = media_match.VIDEO_MAX_MEAN_DIFF
# 키음 파형 막대 수
WAVE_BARS = 600
# 줄 단위 비교를 하는 파일 크기 한도
TEXT_LIMIT = 2 * 1024 * 1024
TEXT_ENCODINGS = ("utf-8", "cp932", "cp949")
CONVERT_TIMEOUT = 600
# 채보로 들어보기: 바탕 음원(무손실 FLAC) 샘플레이트, 최대 길이(초), 넘으면 줄이는 최고 음량, 방식을 바꾸면 올림
RENDER_RATE = 44100
RENDER_SECONDS = 20 * 60
RENDER_PEAK = 0.98
RENDER_VERSION = 1

MEDIA_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
    ".bmp": "image/bmp", ".wav": "audio/wav", ".ogg": "audio/ogg", ".mp3": "audio/mpeg", ".flac": "audio/flac",
    ".webm": "video/webm", ".txt": "text/plain; charset=utf-8",
}


def _unit(stage_id: str, unit_id: str) -> tuple[dict, dict]:
    with staging._lock:
        stage = staging._load(stage_id)
    unit = next((u for u in stage["units"] if u["id"] == unit_id), None)
    if unit is None:
        raise StageError(404, "unit not found")
    return stage, unit


def _conflict_paths(unit: dict) -> dict[str, int]:
    """{충돌 경로: 겨룬 곡 번호}. 차트를 나눠 여러 곡에 넣었으면 경로마다 처음 나온 곡."""
    paths: dict[str, int] = {}
    for info in (unit.get("result") or {}).get("songs", []):
        song_id = info.get("song_id")
        if not song_id:
            continue
        for path in info.get("conflicts", []):
            paths.setdefault(path, song_id)
    return paths


def _match(files: list[dict], path: str) -> dict | None:
    """기존 곡 zip에서 path와 겨룬 항목(_merge_into_song과 같은 기준: 같은 경로, 없으면 확장자만 다른 같은 자리)."""
    lower = path.lower()
    for entry in files:
        if entry["path"].rstrip("/").lower() == lower:
            return entry
    key = media_match.slot(path)
    if key is None:
        return None
    for entry in sorted(files, key=lambda e: e["path"].lower()):
        if not entry["path"].endswith("/") and media_match.slot(entry["path"].lower()) == key:
            return entry
    return None


def _kind(path: str) -> str:
    """sound, image, video, chart(줄 단위 비교), other"""
    return media_match.kind(path) or ("chart" if db._is_chart(path) else "other")


def list_conflicts(stage_id: str, unit_id: str) -> dict:
    """곡의 충돌 파일 목록. items: [{path, song_id, kind, new_size, old_path, old_kind, old_size, choice, ai}]"""
    stage, unit = _unit(stage_id, unit_id)
    new_sizes = dict(staging._unit_entries(stage, unit))
    choices = unit.get("resolutions") or {}
    ai = unit.get("ai") or {}
    ai_items = ai.get("items") or {}
    files_by_song: dict[int, list[dict]] = {}
    items = []
    for path, song_id in sorted(_conflict_paths(unit).items(), key=lambda kv: kv[0].lower()):
        if song_id not in files_by_song:
            files_by_song[song_id] = Database().get_song_files(song_id) or []
        old = _match(files_by_song[song_id], path)
        items.append({
            "path": path,
            "song_id": song_id,
            "kind": _kind(path),
            "new_size": new_sizes.get(path),
            "old_path": old["path"] if old else None,
            "old_kind": _kind(old["path"]) if old else None,
            "old_size": old["size"] if old else None,
            "choice": choices.get(path),
            "ai": ai_items.get(path),
        })
    folders = staging._song_folders({item["song_id"] for item in items})
    return {
        "unit_id": unit_id,
        "folder": unit["folder"],
        "items": items,
        "songs": {str(k): v for k, v in folders.items()},
        # MCP(mcp.py)로 AI가 남긴 곡 전체 판단(summary, split_recommended, issues, model). 파일별 추천은 items[].ai
        "ai": {k: v for k, v in ai.items() if k != "items"} or None,
    }


def _old_bytes(song_id: int, path: str) -> tuple[str, bytes]:
    """(기존 항목 이름, 내용)"""
    files = Database().get_song_files(song_id)
    if files is None:
        raise StageError(404, f"곡 #{song_id}이 없습니다.")
    entry = _match(files, path)
    if entry is None or entry["path"].endswith("/"):
        raise StageError(404, "기존 곡에 겨룬 파일이 없습니다.")
    with db.connect() as con, con.cursor() as cur:
        return entry["path"], Database()._read_song_member(cur, song_id, entry)


def _side(stage: dict, unit: dict, path: str, side: str) -> tuple[str, bytes]:
    """(파일 이름, 내용). side는 old(기존 곡 zip) 또는 new(묶음의 곡)."""
    paths = _conflict_paths(unit)
    if path not in paths:
        raise StageError(404, "충돌 파일이 아닙니다.")
    if side == "old":
        return _old_bytes(paths[path], path)
    if side == "new":
        try:
            return path, staging._read(stage, unit, path)
        except (KeyError, FileNotFoundError):
            raise StageError(404, "묶음에 파일이 없습니다.") from None
    raise StageError(400, "side는 old 또는 new입니다.")


def _cache_dir(stage: dict, unit: dict) -> pathlib.Path:
    out = staging._dir(stage["id"]) / unit["id"] / DIFF_DIR
    out.mkdir(parents=True, exist_ok=True)
    return out


def _cache_name(name: str, content: bytes, variant: str) -> str:
    return hashlib.sha1(f"{variant}\0{name}\0".encode() + hashlib.sha256(content).digest()).hexdigest()


def _ffmpeg() -> str:
    ffmpeg = preview._ffmpeg()
    if ffmpeg is None:
        raise StageError(503, "서버에 ffmpeg가 없어 비교용 파일을 만들 수 없습니다.")
    return ffmpeg


def _convert(src: bytes, name: str, out: pathlib.Path, args: list[str]) -> None:
    """src(이름 name)를 ffmpeg args로 바꿔 out에 씁니다. 실패하면 StageError."""
    ffmpeg = _ffmpeg()
    with tempfile.TemporaryDirectory(prefix="ebms-diff-") as tmp:
        source = pathlib.Path(tmp) / f"in{pathlib.PurePosixPath(name).suffix.lower()}"
        source.write_bytes(src)
        part = out.with_name(out.name + ".part" + out.suffix)
        cmd = [ffmpeg, "-v", "error", "-nostdin", "-y", "-i", str(source), *args, str(part)]
        try:
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=CONVERT_TIMEOUT, check=False)
        except subprocess.TimeoutExpired:
            part.unlink(missing_ok=True)
            raise StageError(500, "비교용 파일을 만드는 데 너무 오래 걸립니다.") from None
        if result.returncode != 0:
            part.unlink(missing_ok=True)
            message = result.stderr.decode("utf-8", "replace").strip()[-300:]
            raise StageError(422, f"브라우저용으로 바꾸지 못했습니다: {message or 'ffmpeg 오류'}")
        part.replace(out)


def web_file(stage_id: str, unit_id: str, path: str, side: str, raw: bool = False) -> tuple[pathlib.Path | bytes, str, str]:
    """비교 화면에 띄울 파일 (경로 또는 내용, media type, 내려받을 이름). raw면 원본 그대로."""
    stage, unit = _unit(stage_id, unit_id)
    name, content = _side(stage, unit, path, side)
    suffix = pathlib.PurePosixPath(name).suffix.lower()
    filename = pathlib.PurePosixPath(name).name
    kind = _kind(name)
    if raw or kind in ("chart", "other"):
        return content, MEDIA_TYPES.get(suffix, "application/octet-stream"), filename
    if (kind == "image" and suffix in WEB_IMAGE) or (kind == "sound" and suffix in WEB_SOUND):
        # 파일로 돌려줘야 Range 요청을 받아 재생 중 위치를 옮길 수 있습니다.
        out = _cache_dir(stage, unit) / f"{_cache_name(name, content, 'raw')}{suffix}"
        if not out.is_file():
            part = out.with_name(out.name + ".part")
            part.write_bytes(content)
            part.replace(out)
        return out, MEDIA_TYPES[suffix], filename
    if kind == "image":
        ext, args = ".png", ["-frames:v", "1", "-f", "image2", "-c:v", "png"]
    elif kind == "sound":
        ext, args = ".ogg", ["-vn", "-c:a", "libvorbis", "-q:a", "4"]
    else:
        ext, args = ".webm", [
            "-t", f"{VIDEO_SECONDS:.3f}", "-an",
            "-vf", f"fps={VIDEO_FPS},scale={VIDEO_WIDTH}:-2:flags=area,format=yuv420p",
            "-c:v", "libvpx", "-g", str(VIDEO_GOP), "-deadline", "realtime", "-cpu-used", "8",
            "-b:v", "1M", "-crf", "20",
        ]
    out = _cache_dir(stage, unit) / f"{_cache_name(name, content, 'web')}{ext}"
    if not out.is_file():
        _convert(content, name, out, args)
    return out, MEDIA_TYPES[ext], pathlib.PurePosixPath(filename).with_suffix(ext).name


def _decode(content: bytes, name: str, args: list[str], stream: str) -> bytes | None:
    ffmpeg = _ffmpeg()
    with tempfile.TemporaryDirectory(prefix="ebms-diff-") as tmp:
        source = pathlib.Path(tmp) / f"in{pathlib.PurePosixPath(name).suffix.lower()}"
        source.write_bytes(content)
        decoded = media_match._decode(ffmpeg, [source], pathlib.Path(tmp), args, stream)[0]
    return decoded


def _cached_json(stage: dict, unit: dict, key: str, make) -> dict:
    out = _cache_dir(stage, unit) / f"{key}.json"
    if out.is_file():
        return json.loads(out.read_text("utf-8"))
    value = make()
    out.write_text(json.dumps(value), "utf-8")
    return value


def _peaks(samples: np.ndarray) -> list[float]:
    if not len(samples):
        return []
    bars = min(WAVE_BARS, len(samples))
    edges = np.linspace(0, len(samples), bars + 1, dtype=int)
    return [round(float(np.abs(samples[a:b]).max()), 3) if b > a else 0.0 for a, b in zip(edges[:-1], edges[1:])]


def sound_compare(stage_id: str, unit_id: str, path: str) -> dict:
    """키음 둘의 길이·파형·상관계수(media_match.same_sound와 같은 방식, 지연 보정 없음)."""
    stage, unit = _unit(stage_id, unit_id)
    old_name, old = _side(stage, unit, path, "old")
    new_name, new = _side(stage, unit, path, "new")
    rate = media_match.SOUND_RATE

    def make() -> dict:
        args = ["-ac", "1", "-ar", str(rate), "-t", f"{media_match.SOUND_SECONDS:.3f}", "-f", "f32le"]
        raw_a, raw_b = _decode(old, old_name, args, "a"), _decode(new, new_name, args, "a")
        if raw_a is None or raw_b is None:
            return {"error": "키음을 풀지 못했습니다."}
        a = np.frombuffer(raw_a, dtype="<f4").astype(np.float64)
        b = np.frombuffer(raw_b, dtype="<f4").astype(np.float64)
        # 판정(correlation)은 싱크를 맞추지 않습니다(싱크도 같아야 같은 소재).
        # 참고로 싱크 오차(offset_ms)와 그 오차를 맞췄을 때의 상관계수(aligned_correlation)도 보여 줍니다.
        value = media_match.correlation(a, b)
        offset = media_match._best_lag(a, b, int(media_match.SOUND_OFFSET_SEARCH * rate))
        aligned = media_match.correlation(a[offset:], b) if offset >= 0 else media_match.correlation(a, b[-offset:])
        return {
            "old": {"seconds": round(len(a) / rate, 3), "peaks": _peaks(a)},
            "new": {"seconds": round(len(b) / rate, 3), "peaks": _peaks(b)},
            "offset_ms": round(offset / rate * 1000, 1),
            "correlation": None if value is None else round(value, 4),
            "aligned_correlation": None if aligned is None else round(aligned, 4),
            "min_correlation": media_match.SOUND_MIN_CORRELATION,
            "same": bool(media_match.same_sound(a.astype("<f4"), b.astype("<f4"))),
        }

    key = _cache_name(path, hashlib.sha256(old + b"\0" + new).digest(), "sound-sync")
    return _cached_json(stage, unit, key, make)


def video_compare(stage_id: str, unit_id: str, path: str) -> dict:
    """영상 둘을 VIDEO_FPS로 풀어 프레임마다 평균 차이(0~1)를 잽니다. 짧은 쪽 뒤의 프레임은 1(다름)입니다.
    segments는 차이가 VIDEO_FRAME_DIFF를 넘는 [시작 프레임, 끝 프레임] 구간(끝 포함)."""
    stage, unit = _unit(stage_id, unit_id)
    old_name, old = _side(stage, unit, path, "old")
    new_name, new = _side(stage, unit, path, "new")
    size = VIDEO_CMP_SIZE

    def frames(content: bytes, name: str) -> np.ndarray | None:
        args = [
            "-t", f"{VIDEO_SECONDS:.3f}", "-an",
            "-vf", f"fps={VIDEO_FPS},scale={size}:{size}:flags=area", "-pix_fmt", "gray", "-f", "rawvideo",
        ]
        raw = _decode(content, name, args, "v")
        if raw is None:
            return None
        frame = size * size
        return np.frombuffer(raw, np.uint8)[: len(raw) // frame * frame].reshape(-1, frame)

    def make() -> dict:
        a, b = frames(old, old_name), frames(new, new_name)
        if a is None or b is None:
            return {"error": "영상을 풀지 못했습니다."}
        n, total = min(len(a), len(b)), max(len(a), len(b))
        diff = np.ones(total, dtype=np.float32)
        if n:
            diff[:n] = (np.abs(a[:n].astype(np.float32) - b[:n].astype(np.float32)) / 255).mean(axis=1)
        segments = []
        for i in np.flatnonzero(diff > VIDEO_FRAME_DIFF).tolist():
            if segments and segments[-1][1] == i - 1:
                segments[-1][1] = i
            else:
                segments.append([i, i])
        return {
            "fps": VIDEO_FPS,
            "old_frames": int(len(a)),
            "new_frames": int(len(b)),
            "diff": [round(float(d), 3) for d in diff],
            "threshold": VIDEO_FRAME_DIFF,
            "segments": segments,
        }

    key = _cache_name(path, hashlib.sha256(old + b"\0" + new).digest(), "video")
    return _cached_json(stage, unit, key, make)


def _text(content: bytes) -> str | None:
    if len(content) > TEXT_LIMIT or b"\0" in content[:8192]:
        return None
    for encoding in TEXT_ENCODINGS:
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def text_diff(stage_id: str, unit_id: str, path: str) -> dict:
    """텍스트 파일의 줄 단위 차이(unified diff). 텍스트가 아니면 binary."""
    stage, unit = _unit(stage_id, unit_id)
    old_name, old = _side(stage, unit, path, "old")
    new_name, new = _side(stage, unit, path, "new")
    a, b = _text(old), _text(new)
    result = {
        "old": {"size": len(old), "sha256": hashlib.sha256(old).hexdigest()},
        "new": {"size": len(new), "sha256": hashlib.sha256(new).hexdigest()},
    }
    if a is None or b is None:
        result["binary"] = True
        return result
    lines = difflib.unified_diff(a.splitlines(), b.splitlines(), f"기존 {old_name}", f"새 {new_name}", n=3, lineterm="")
    result["diff"] = "\n".join(lines)
    return result


# ---- 채보로 들어보기: 충돌 없는 소리로 바탕 음원을 만들고, 충돌 키음은 브라우저가 시각에 맞춰 겹칩니다 ----


class _Files:
    """곡 하나의 기존(old, 곡 zip)·새(new, 묶음) 파일을 읽습니다. 기존 파일은 DB 연결 하나로 읽습니다."""

    def __init__(self, stage: dict, unit: dict, song_id: int):
        self.stage, self.unit, self.song_id = stage, unit, song_id
        self.old = {e["path"]: e for e in Database().get_song_files(song_id) or [] if not e["path"].endswith("/")}
        self.new = {path for path, _ in staging._unit_entries(stage, unit)}
        self._con = self._cur = self._zip = None

    def __enter__(self) -> "_Files":
        return self

    def __exit__(self, *exc) -> None:
        if self._cur is not None:
            self._cur.close()
            self._con.close()
        if self._zip is not None:
            self._zip.close()

    def read(self, side: str, path: str) -> bytes:
        if side == "old":
            if self._con is None:
                self._con = db.connect()
                self._cur = self._con.cursor()
            return Database()._read_song_member(self._cur, self.song_id, self.old[path])
        if path not in self.new:
            raise KeyError(path)
        pack = staging._pack_path(self.stage, self.unit)
        if pack.is_file():
            if self._zip is None:
                self._zip = zipfile.ZipFile(pack)
            return self._zip.read(path)
        return (staging._unit_dir(self.stage, self.unit) / path).read_bytes()

    def merged(self) -> dict[str, tuple[str, str]]:
        """병합한 뒤의 파일 {경로: (쪽, 그 쪽의 경로)}. 같은 경로(대소문자 무시)는 기존 쪽입니다."""
        out = {path: ("new", path) for path in self.new}
        lower = {path.lower(): path for path in self.new}
        for path in self.old:
            out.pop(lower.get(path.lower()), None)
            out[path] = ("old", path)
        return out


def _sound_keys(items: list[dict], song_id: int) -> dict[str, str]:
    """키음 충돌 {기존·새 경로(소문자): 충돌 경로}. 차트가 어느 쪽 이름을 쓰든 같은 충돌로 봅니다."""
    keys = {}
    for item in items:
        if item["song_id"] == song_id and item["kind"] == "sound":
            keys[item["path"].lower()] = item["path"]
            if item["old_path"]:
                keys[item["old_path"].lower()] = item["path"]
    return keys


def _chart_sounds(files: _Files, side: str, chart_path: str) -> tuple[bytes, preview.Chart, list[str | None]]:
    """(차트 내용, 해석한 차트, 소리마다 찾은 병합 후 파일 경로(없으면 None))."""
    content = files.read(side, chart_path)
    chart = preview.parse_chart(content)
    merged = files.merged()
    resolver = preview._Resolver(list(merged))
    base = posixpath.dirname(chart_path)
    found: dict[str, str | None] = {}
    targets = []
    for s in chart.sounds:
        if s.name not in found:
            found[s.name] = resolver.find(base, s.name)
        targets.append(found[s.name])
    return content, chart, targets


def _chart_title(content: bytes) -> str:
    from .db import _bmson

    data = _bmson(content)
    if data is not None:
        info = data.get("info") if isinstance(data.get("info"), dict) else {}
        return " ".join(str(info.get(k) or "") for k in ("title", "subtitle", "chart_name")).strip()
    lines = preview._bms_lines(content)
    values = {}
    for line in lines:
        parts = line.split(None, 1)
        name = parts[0][1:].upper()
        if name in (b"TITLE", b"SUBTITLE", b"PLAYLEVEL") and len(parts) > 1:
            values.setdefault(name, preview._decode(parts[1].strip()))
    title = " ".join(v for v in (values.get(b"TITLE"), values.get(b"SUBTITLE")) if v)
    return f"{title} (Lv.{values[b'PLAYLEVEL']})" if b"PLAYLEVEL" in values else title


def render_charts(stage_id: str, unit_id: str, path: str) -> dict:
    """충돌 파일 path를 들어볼 차트 목록 items: [{side, path, title, uses}]. 차트 충돌이면 그 차트의 기존·새 쪽,
    키음 충돌이면 그 키음을 쓰는 차트(양쪽)이고, uses는 그 키음이 울리는 횟수입니다."""
    stage, unit = _unit(stage_id, unit_id)
    items = list_conflicts(stage_id, unit_id)["items"]
    item = next((i for i in items if i["path"] == path), None)
    if item is None:
        raise StageError(404, "충돌 파일이 아닙니다.")
    out = []
    with _Files(stage, unit, item["song_id"]) as files:
        if item["kind"] == "chart":
            pairs = [("old", item["old_path"]), ("new", item["path"])]
        else:
            pairs = [("old", p) for p in sorted(files.old) if db._is_chart(p)]
            pairs += [("new", p) for p in sorted(files.new) if db._is_chart(p)]
        keys = _sound_keys(items, item["song_id"])
        for side, chart_path in pairs:
            if not chart_path:
                continue
            try:
                content, chart, targets = _chart_sounds(files, side, chart_path)
            except Exception:
                logger.exception(f"conflicts: could not read chart {side}:{chart_path}")
                continue
            uses = sum(1 for t in targets if t and keys.get(t.lower()) == path)
            if item["kind"] != "chart" and not uses:
                continue
            out.append({"side": side, "path": chart_path, "title": _chart_title(content), "uses": uses,
                        "sounds": len(chart.sounds)})
    return {"song_id": item["song_id"], "items": out}


_renders: dict[str, dict] = {}  # 만드는 중인 바탕 음원 {캐시 이름: {done, total, error}}
_renders_lock = threading.Lock()


def _render_key(stage: dict, unit: dict, song_id: int, side: str, chart_path: str) -> str:
    with _Files(stage, unit, song_id) as files:
        content = files.read(side, chart_path)
    return _cache_name(f"{song_id}\0{side}\0{chart_path}", content, f"render{RENDER_VERSION}")


def render(stage_id: str, unit_id: str, song_id: int, side: str, chart_path: str) -> dict:
    """차트 하나의 바탕 음원과 겹칠 소리. 만드는 중이면 {status: running, done, total}(처음 부르면 만들기 시작),
    다 됐으면 {status: ready, seconds, gain, rate, events}. events는 충돌 키음 [{t, key, o, e}]
    (t 시각, key 충돌 경로, o 키음 안에서 시작할 위치, e 끊을 시각, 모두 초)이고, 바탕에는 빠져 있습니다.
    바탕은 gain을 곱해 둔 것이라 겹치는 소리에도 같은 gain을 곱합니다."""
    if side not in CHOICES:
        raise StageError(400, "side는 old 또는 new입니다.")
    stage, unit = _unit(stage_id, unit_id)
    if not any(p == song_id for p in _conflict_paths(unit).values()):
        raise StageError(404, "이 곡의 충돌이 아닙니다.")
    try:
        key = _render_key(stage, unit, song_id, side, chart_path)
    except (KeyError, FileNotFoundError):
        raise StageError(404, "차트가 없습니다.") from None
    out = _cache_dir(stage, unit) / f"{key}.json"
    if out.is_file() and (out.with_suffix(".flac")).is_file():
        return {"status": "ready", **json.loads(out.read_text("utf-8"))}
    with _renders_lock:
        state = _renders.get(key)
        if state is not None and state.get("error"):
            # 실패는 한 번 알리고, 다음에 부르면 다시 만듭니다.
            del _renders[key]
            return {"status": "error", "error": state["error"]}
        if state is None:
            _ffmpeg()
            state = _renders[key] = {"done": 0, "total": 0}
            threading.Thread(
                target=_render_job, args=(stage, unit, song_id, side, chart_path, out, state), daemon=True,
                name=f"conflict-render-{key[:8]}",
            ).start()
    return {"status": "running", "done": state["done"], "total": state["total"]}


def render_bed(stage_id: str, unit_id: str, song_id: int, side: str, chart_path: str) -> pathlib.Path:
    stage, unit = _unit(stage_id, unit_id)
    try:
        key = _render_key(stage, unit, song_id, side, chart_path)
    except (KeyError, FileNotFoundError):
        raise StageError(404, "차트가 없습니다.") from None
    path = _cache_dir(stage, unit) / f"{key}.flac"
    if not path.is_file():
        raise StageError(404, "바탕 음원을 아직 만들지 않았습니다.")
    return path


def _render_job(stage: dict, unit: dict, song_id: int, side: str, chart_path: str, out: pathlib.Path, state: dict) -> None:
    try:
        result = _render_bed(stage, unit, song_id, side, chart_path, out.with_suffix(".flac"), state)
        out.write_text(json.dumps(result), "utf-8")
        with _renders_lock:
            _renders.pop(out.stem, None)
    except Exception as e:
        logger.exception(f"conflicts: render failed {side}:{chart_path}")
        state["error"] = f"채보 음원을 만들지 못했습니다: {e}"


def _render_bed(
    stage: dict, unit: dict, song_id: int, side: str, chart_path: str, flac: pathlib.Path, state: dict,
) -> dict:
    ffmpeg = _ffmpeg()
    rate, channels = RENDER_RATE, 2
    items = list_conflicts(stage["id"], unit["id"])["items"]
    keys = _sound_keys(items, song_id)
    with _Files(stage, unit, song_id) as files:
        _, chart, targets = _chart_sounds(files, side, chart_path)
        merged = files.merged()
        events = []
        bed: dict[str, list[preview.Sound]] = {}  # 병합 후 경로 → 바탕에 섞을 소리
        for s, target in zip(chart.sounds, targets):
            if target is None or s.time >= RENDER_SECONDS:
                continue
            key = keys.get(target.lower())
            if key is not None:
                events.append({"t": round(s.time, 5), "key": key, "o": round(s.offset, 5),
                               "e": None if s.end is None else round(s.end, 5)})
            else:
                bed.setdefault(target, []).append(s)
        members = sorted(bed)
        state["total"] = len(members)
        buf = np.zeros((int((chart.length + 5) * rate), channels), dtype=np.float32)
        last = 0
        with tempfile.TemporaryDirectory(prefix="ebms-render-") as tmp:
            for start in range(0, len(members), preview.DECODE_BATCH):
                batch = members[start : start + preview.DECODE_BATCH]
                paths = []
                for n, member in enumerate(batch):
                    p = pathlib.Path(tmp) / f"{n}{posixpath.splitext(member)[1].lower()}"
                    p.write_bytes(files.read(*merged[member]))
                    paths.append(p)
                decoded = preview._decode_sounds(ffmpeg, paths, RENDER_SECONDS, rate, channels)
                for p in paths:
                    p.unlink(missing_ok=True)
                for member, data in zip(batch, decoded):
                    if data is None:
                        continue
                    for s in bed[member]:
                        seg = data[int(round(s.offset * rate)) :]
                        if s.end is not None:
                            seg = seg[: max(0, int(round((s.end - s.time) * rate)))]
                        pos = int(round(s.time * rate))
                        end = min(pos + len(seg), int(RENDER_SECONDS * rate))
                        if end <= pos:
                            continue
                        if end > len(buf):
                            buf = np.concatenate([buf, np.zeros((end - len(buf) + 10 * rate, channels), np.float32)])
                        # 같은 키음이 끝나기 전에 또 나와도 앞 소리를 끊지 않고 겹칩니다.
                        buf[pos:end] += seg[: end - pos]
                        last = max(last, end)
                state["done"] = min(len(members), start + len(batch))
    seconds = max(last / rate, max((e["t"] for e in events), default=0.0) + 1.0, chart.length)
    buf = buf[: int(seconds * rate)]
    if len(buf) < int(seconds * rate):
        buf = np.concatenate([buf, np.zeros((int(seconds * rate) - len(buf), channels), np.float32)])
    peak = float(np.max(np.abs(buf))) if len(buf) else 0.0
    gain = 1.0 if peak <= RENDER_PEAK else RENDER_PEAK / peak
    if gain != 1.0:
        buf *= gain
    part = flac.with_name(flac.name + ".part")
    result = subprocess.run(
        [ffmpeg, "-v", "error", "-nostdin", "-y", "-f", "f32le", "-ar", str(rate), "-ac", str(channels), "-i", "pipe:0",
         "-c:a", "flac", "-sample_fmt", "s16", "-f", "flac", str(part)],
        input=buf.astype("<f4").tobytes(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=CONVERT_TIMEOUT, check=False,
    )
    if result.returncode != 0:
        part.unlink(missing_ok=True)
        raise RuntimeError(result.stderr.decode("utf-8", "replace").strip()[-300:] or "ffmpeg 오류")
    part.replace(flac)
    events.sort(key=lambda e: e["t"])
    return {"seconds": round(seconds, 3), "gain": round(gain, 6), "rate": rate, "events": events}


def set_choices(stage_id: str, unit_id: str, choices: dict[str, str | None]) -> dict:
    """충돌 파일마다 고른 것을 저장합니다. None이면 고른 것을 지웁니다. 빠진 파일은 그대로."""
    for value in choices.values():
        if value is not None and value not in CHOICES:
            raise StageError(400, f"고를 수 있는 값은 old, new입니다: {value!r}")
    with staging._lock:
        stage = staging._load(stage_id)
        staging._require_ready(stage)
        unit = next((u for u in stage["units"] if u["id"] == unit_id), None)
        if unit is None:
            raise StageError(404, "unit not found")
        known = _conflict_paths(unit)
        unknown = set(choices) - set(known)
        if unknown:
            raise StageError(400, f"충돌 파일이 아닙니다: {', '.join(sorted(unknown))}")
        resolutions = dict(unit.get("resolutions") or {})
        for path, value in choices.items():
            if value is None:
                resolutions.pop(path, None)
            else:
                resolutions[path] = value
        unit["resolutions"] = resolutions
        staging._save(stage)
    return {"resolutions": resolutions}


def apply(stage_id: str, unit_id: str, slow: bool = False):
    """고른 대로 그 곡만 다시 넣는 commit을 시작합니다. 충돌 파일을 모두 골라야 합니다."""
    _, unit = _unit(stage_id, unit_id)
    paths = _conflict_paths(unit)
    if not paths:
        raise StageError(409, "충돌 파일이 없습니다.")
    missing = [p for p in paths if (unit.get("resolutions") or {}).get(p) not in CHOICES]
    if missing:
        raise StageError(409, f"고르지 않은 파일이 있습니다: {', '.join(sorted(missing))}")
    return staging.start_commit(stage_id, slow=slow, unit_ids=[unit_id])
