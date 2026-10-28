"""검토 대기 곡의 병합 충돌 해결. 기존 곡에 더하려다 내용이 달라 넣지 못한 파일(unit.result.songs[].conflicts)을
관리자가 기존 파일과 새 파일로 나란히 비교하고, 파일마다 "old"(기존 유지)나 "new"(새 파일로 바꿈)를 골라 그 곡만 다시 넣습니다.

- 고른 것은 stage.json의 unit.resolutions({새 파일 경로: "old" | "new"})에 두고, commit 때 SongBatch.add로 넘깁니다.
- 비교용 파일: 브라우저가 못 여는 형식은 ffmpeg로 바꿉니다. 이미지는 png, 키음은 ogg, 영상은 작은 webm
  (VIDEO_FPS 고정 프레임레이트, 키프레임 간격 VIDEO_GOP)이라 화면에서 프레임 번호 ÷ fps로 정확히 옮겨 갈 수 있습니다.
- 영상은 두 파일을 VIDEO_FPS로 풀어 프레임마다 차이를 재고, 다른 구간을 타임라인에 표시합니다.
- 바꾼 파일·계산 결과는 곡 폴더 아래 `_diff/`에 둡니다(곡이 묶음에서 빠지면 같이 지워짐).
"""
import difflib
import hashlib
import json
import logging
import pathlib
import subprocess
import tempfile

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
    """곡의 충돌 파일 목록. items: [{path, song_id, kind, new_size, old_path, old_kind, old_size, choice}]"""
    stage, unit = _unit(stage_id, unit_id)
    new_sizes = dict(staging._unit_entries(stage, unit))
    choices = unit.get("resolutions") or {}
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
        })
    folders = staging._song_folders({item["song_id"] for item in items})
    return {
        "unit_id": unit_id,
        "folder": unit["folder"],
        "items": items,
        "songs": {str(k): v for k, v in folders.items()},
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
