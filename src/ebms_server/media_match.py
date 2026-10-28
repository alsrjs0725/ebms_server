"""같은 경로에 내용(바이트)이 다른 키음·이미지·영상이 같은 소재인지 판정합니다.

곡을 기존 곡에 더할 때(db._merge_into_song) 같은 경로의 파일이 바이트 단위로 다르면 충돌로 남기는데,
배포판마다 다시 인코딩(wav↔ogg 품질, 이미지 재압축, 영상 재인코딩)만 다른 경우가 많습니다.
이런 파일은 풀어서 비교해 같은 소재로 보이면 기존 파일을 두고 충돌로 보지 않습니다.
ffmpeg로 풀고, 풀지 못하거나(ffmpeg 없음, 깨진 파일) 다르다고 보이면 지금처럼 충돌입니다.

- 키음: 모노 SOUND_RATE Hz로 풀어(최대 SOUND_SECONDS초) 길이가 SOUND_LENGTH_TOLERANCE 안이고,
  시작을 맞춘 그대로(지연을 보정하지 않음, 싱크가 어긋나면 다른 소재) 정규화 상관계수가 SOUND_MIN_CORRELATION 이상이면 같습니다.
  둘 다 거의 무음이면 길이만 봅니다. 음량만 다른 것은 같은 것으로 봅니다(상관계수는 크기에 무관).
- 이미지: IMAGE_SIZE×IMAGE_SIZE RGBA로 (면적 평균으로) 줄여 픽셀 차이 평균이 IMAGE_MAX_MEAN_DIFF 이하이고
  어느 채널이든 IMAGE_PIXEL_DIFF보다 많이 다른 픽셀이 IMAGE_MAX_CHANGED 이하이면 같습니다(움직이는 gif는 첫 프레임).
- 영상: VIDEO_FPS 간격으로 뽑은 프레임(VIDEO_SIZE×VIDEO_SIZE 흑백, 최대 VIDEO_SECONDS초) 수 차이가
  VIDEO_FRAME_TOLERANCE 안이고, 프레임 차이 평균이 VIDEO_MAX_MEAN_DIFF 이하이면 같습니다.

같은 소재면 품질이 좋은 쪽을 남깁니다(better_new). ffprobe로 형식을 읽고,
- 키음: 실제로 소리가 있는 대역폭(손실 압축은 고음을 잘라 냄)이 BAND_MARGIN Hz 넘게 넓은 쪽, 무손실(pcm·flac 등),
  샘플레이트·채널·비트 깊이, 초당 비트레이트(QUALITY_MARGIN 넘게 차이) 순으로 봅니다.
- 이미지: 해상도, 무손실 형식(jpg가 아닌 것) 순.
- 영상: 해상도, fps, 초당 비트레이트 순.
모두 비슷하면 용량이 작은 쪽입니다. 형식을 읽지 못하면 기존 파일을 둡니다.
"""
import json
import logging
import pathlib
import shutil
import subprocess
import tempfile

import numpy as np

from . import constant, preview

logger = logging.getLogger(__name__)

SOUND_FORMAT = constant.AUDIO_FORMAT
# beatoraja BGImageProcessor.pic_extension에 있는 .tga까지
IMAGE_FORMAT = (*constant.IMAGE_FORMAT, ".tga")
VIDEO_FORMAT = constant.VIDEO_FORMAT

SOUND_RATE = 8000
SOUND_SECONDS = 600.0
# 길이 차이 허용: 이 비율과 SOUND_LENGTH_SECONDS 중 큰 값
SOUND_LENGTH_TOLERANCE = 0.02
SOUND_LENGTH_SECONDS = 0.1
# 비교 화면에 참고로 보여 주는 시작 위치 차이를 찾는 범위(초). 판정에는 쓰지 않습니다.
SOUND_OFFSET_SEARCH = 0.1
SOUND_MIN_CORRELATION = 0.75
# 이 RMS 미만이면 무음으로 봅니다.
SOUND_SILENCE_RMS = 1e-4

IMAGE_SIZE = 64
# 0~1(255 단계를 1로) 기준
IMAGE_MAX_MEAN_DIFF = 0.02
IMAGE_PIXEL_DIFF = 0.25
IMAGE_MAX_CHANGED = 0.002

VIDEO_FPS = 0.5
VIDEO_SIZE = 64
VIDEO_SECONDS = 600.0
VIDEO_FRAME_TOLERANCE = 1
VIDEO_MAX_MEAN_DIFF = 0.04

# 품질 비교: 대역폭은 BAND_RATE Hz로 풀어(최대 BAND_SECONDS초) 평균 스펙트럼이 가장 센 곳보다 BAND_FLOOR_DB 아래로
# 떨어지기 전의 가장 높은 주파수로 잽니다.
BAND_RATE = 48000
BAND_SECONDS = 30.0
BAND_FFT = 2048
BAND_FLOOR_DB = 70.0
BAND_MARGIN = 1000.0
# 비트레이트·fps는 이 비율 넘게 차이 나야 다르다고 봅니다.
QUALITY_MARGIN = 0.1
LOSSLESS_AUDIO = ("pcm_", "flac", "alac", "wavpack", "ape", "tta")
LOSSY_IMAGE = ("mjpeg", "jpeg")

# ffmpeg 한 번에 푸는 파일 수
DECODE_BATCH = 64


def kind(path: str) -> str | None:
    suffix = pathlib.PurePosixPath(path).suffix.lower()
    if suffix in SOUND_FORMAT:
        return "sound"
    if suffix in IMAGE_FORMAT:
        return "image"
    if suffix in VIDEO_FORMAT:
        return "video"
    return None


def slot(path: str) -> tuple[str, str] | None:
    """플레이어가 같은 파일 자리로 읽는 (확장자 뺀 소문자 경로, 묶음). 키음은 키음끼리, BGA는 이미지·영상이 한 묶음입니다.
    beatoraja는 #WAV가 가리키는 파일이 없거나 못 풀면 .wav·.flac·.ogg·.mp3를 차례로 찾고(AudioDriver.getResources),
    #BMP는 이미지↔이미지·영상↔영상으로 확장자를 바꿔 찾고 없으면 영상·이미지 확장자를 다 찾습니다(BGAProcessor)."""
    k = kind(path)
    if k is None:
        return None
    return pathlib.PurePosixPath(path.lower()).with_suffix("").as_posix(), "sound" if k == "sound" else "bga"


def same_files(pairs: list[tuple[str, bytes, pathlib.Path]]) -> set[str]:
    """(경로, 기존 파일 내용, 새 파일)들 중 같은 소재로 보이는 것의 경로. 키음·이미지·영상이 아니면 보지 않습니다."""
    pairs = [p for p in pairs if kind(p[0]) is not None]
    if not pairs:
        return set()
    ffmpeg = preview._ffmpeg()
    if ffmpeg is None:
        logger.warning("media_match: ffmpeg not found. Different media files are kept as conflicts.")
        return set()
    same = set()
    with tempfile.TemporaryDirectory(prefix="ebms-match-") as tmp:
        work = pathlib.Path(tmp)
        groups: dict[str, list[tuple[str, pathlib.Path, pathlib.Path]]] = {}
        for i, (path, old, new) in enumerate(pairs):
            old_path = work / f"{i}{pathlib.PurePosixPath(path).suffix.lower()}"
            old_path.write_bytes(old)
            groups.setdefault(kind(path), []).append((path, old_path, new))
        for k, items in groups.items():
            compare = {"sound": _same_sounds, "image": _same_images, "video": _same_videos}[k]
            try:
                same |= compare(ffmpeg, items, work)
            except Exception:
                logger.exception(f"media_match: {k} comparison failed")
    return same


def _decode(ffmpeg: str, paths: list[pathlib.Path], work: pathlib.Path, args: list[str], stream: str) -> list[bytes | None]:
    """파일들을 각각 args(출력 옵션)로 풀어 원시 바이트로. 풀지 못한 파일은 None.
    여러 파일을 ffmpeg 한 번에 풀고, 실패하면 하나씩 다시 풉니다(preview._decode_sounds와 같은 방식)."""
    out: list[bytes | None] = [None] * len(paths)
    for start in range(0, len(paths), DECODE_BATCH):
        batch = list(range(start, min(start + DECODE_BATCH, len(paths))))
        raws = {i: work / f"decoded-{i}.raw" for i in batch}
        cmd = [ffmpeg, "-v", "error", "-nostdin", "-y"]
        for i in batch:
            cmd += ["-i", str(paths[i])]
        for n, i in enumerate(batch):
            cmd += ["-map", f"{n}:{stream}:0", *args, str(raws[i])]
        try:
            preview._run(cmd)
            todo = []
        except (RuntimeError, subprocess.TimeoutExpired):
            todo = batch
        for i in todo:
            try:
                preview._run([ffmpeg, "-v", "error", "-nostdin", "-y", "-i", str(paths[i]), "-map", f"0:{stream}:0", *args, str(raws[i])])
            except (RuntimeError, subprocess.TimeoutExpired):
                raws[i].unlink(missing_ok=True)
        for i in batch:
            if raws[i].exists():
                out[i] = raws[i].read_bytes()
                raws[i].unlink()
    return out


def _pairs_decoded(ffmpeg, items, work, args, stream):
    paths = [p for _, old, new in items for p in (old, new)]
    raw = _decode(ffmpeg, paths, work, args, stream)
    for n, (path, _, _) in enumerate(items):
        a, b = raw[2 * n], raw[2 * n + 1]
        if a is not None and b is not None:
            yield path, a, b


def _same_sounds(ffmpeg, items, work) -> set[str]:
    args = ["-ac", "1", "-ar", str(SOUND_RATE), "-t", f"{SOUND_SECONDS:.3f}", "-f", "f32le"]
    return {
        path for path, a, b in _pairs_decoded(ffmpeg, items, work, args, "a")
        if same_sound(np.frombuffer(a, dtype="<f4"), np.frombuffer(b, dtype="<f4"))
    }


def same_sound(a: np.ndarray, b: np.ndarray, rate: int = SOUND_RATE) -> bool:
    if abs(len(a) - len(b)) > max(SOUND_LENGTH_TOLERANCE * max(len(a), len(b)), SOUND_LENGTH_SECONDS * rate):
        return False
    a = a.astype(np.float64)
    b = b.astype(np.float64)
    quiet_a = len(a) == 0 or np.sqrt(np.mean(a * a)) < SOUND_SILENCE_RMS
    quiet_b = len(b) == 0 or np.sqrt(np.mean(b * b)) < SOUND_SILENCE_RMS
    if quiet_a or quiet_b:
        return quiet_a and quiet_b
    value = correlation(a, b)
    return value is not None and value >= SOUND_MIN_CORRELATION


def correlation(a: np.ndarray, b: np.ndarray) -> float | None:
    """시작을 맞춘 그대로의 정규화 상관계수(짧은 쪽 길이까지). 무음이면 None."""
    n = min(len(a), len(b))
    a, b = a[:n].astype(np.float64), b[:n].astype(np.float64)
    denom = np.sqrt(np.dot(a, a) * np.dot(b, b)) if n else 0
    return float(np.dot(a, b) / denom) if denom > 0 else None


def _best_lag(a: np.ndarray, b: np.ndarray, max_lag: int) -> int:
    """a[lag:]가 b와 가장 잘 맞는 lag(음수면 b[-lag:]가 a와). 앞부분(최대 5초)의 상호상관으로 찾습니다."""
    n = min(len(a), len(b), 5 * SOUND_RATE + max_lag)
    if n <= 1 or max_lag <= 0:
        return 0
    size = 1 << (2 * n - 1).bit_length()
    corr = np.fft.irfft(np.fft.rfft(a[:n], size) * np.conj(np.fft.rfft(b[:n], size)), size)
    lags = np.concatenate([np.arange(0, min(max_lag, n - 1) + 1), np.arange(-min(max_lag, n - 1), 0)])
    values = corr[lags % size]
    return int(lags[int(np.argmax(values))])


def _same_images(ffmpeg, items, work) -> set[str]:
    # 면적 평균으로 줄여, 크기만 바꾼 그림(512→256 등)의 가장자리 차이가 남지 않게 합니다.
    args = ["-frames:v", "1", "-vf", f"scale={IMAGE_SIZE}:{IMAGE_SIZE}:flags=area", "-pix_fmt", "rgba", "-f", "rawvideo"]
    size = IMAGE_SIZE * IMAGE_SIZE * 4
    return {
        path for path, a, b in _pairs_decoded(ffmpeg, items, work, args, "v")
        if len(a) == len(b) == size and same_image(np.frombuffer(a, np.uint8), np.frombuffer(b, np.uint8))
    }


def same_image(a: np.ndarray, b: np.ndarray) -> bool:
    """a, b: RGBA 픽셀을 늘어놓은 배열. 픽셀마다 가장 많이 다른 채널로 셉니다."""
    diff = (np.abs(a.astype(np.float32) - b.astype(np.float32)) / 255).reshape(-1, 4)
    changed = diff.max(axis=1) > IMAGE_PIXEL_DIFF
    return float(diff.mean()) <= IMAGE_MAX_MEAN_DIFF and float(changed.mean()) <= IMAGE_MAX_CHANGED


def _same_videos(ffmpeg, items, work) -> set[str]:
    args = [
        "-t", f"{VIDEO_SECONDS:.3f}", "-an",
        "-vf", f"fps={VIDEO_FPS},scale={VIDEO_SIZE}:{VIDEO_SIZE}:flags=area", "-pix_fmt", "gray", "-f", "rawvideo",
    ]
    frame = VIDEO_SIZE * VIDEO_SIZE
    same = set()
    for path, a, b in _pairs_decoded(ffmpeg, items, work, args, "v"):
        fa = np.frombuffer(a, np.uint8)[: len(a) // frame * frame].reshape(-1, frame)
        fb = np.frombuffer(b, np.uint8)[: len(b) // frame * frame].reshape(-1, frame)
        if same_video(fa, fb):
            same.add(path)
    return same


def same_video(a: np.ndarray, b: np.ndarray) -> bool:
    """a, b: (프레임 수, 픽셀) 흑백 프레임."""
    if not len(a) or not len(b) or abs(len(a) - len(b)) > VIDEO_FRAME_TOLERANCE:
        return False
    n = min(len(a), len(b))
    diff = np.abs(a[:n].astype(np.float32) - b[:n].astype(np.float32)) / 255
    return float(diff.mean()) <= VIDEO_MAX_MEAN_DIFF


# ---- 품질 비교(같은 소재일 때 어느 쪽을 남길지) ----

def _ffprobe() -> str | None:
    return shutil.which("ffprobe")


def _probe(path: pathlib.Path) -> dict | None:
    """ffprobe로 읽은 첫 스트림(kind에 맞는 것)과 길이. 읽지 못하면 None."""
    ffprobe = _ffprobe()
    if ffprobe is None:
        return None
    try:
        out = preview._run([
            ffprobe, "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path),
        ])
        return json.loads(out)
    except (RuntimeError, subprocess.TimeoutExpired, ValueError):
        return None


def _stream(info: dict, codec_type: str) -> dict | None:
    return next((s for s in info.get("streams", []) if s.get("codec_type") == codec_type), None)


def _duration(info: dict, stream: dict) -> float:
    for value in (stream.get("duration"), info.get("format", {}).get("duration")):
        try:
            if value is not None and float(value) > 0:
                return float(value)
        except ValueError:
            continue
    return 0.0


def _fps(stream: dict) -> float:
    for key in ("avg_frame_rate", "r_frame_rate"):
        num, _, den = str(stream.get(key) or "0/0").partition("/")
        try:
            if float(den or 1):
                value = float(num) / float(den or 1)
                if value > 0:
                    return value
        except ValueError:
            continue
    return 0.0


def bandwidth(samples: np.ndarray, rate: int = BAND_RATE) -> float:
    """소리가 있는 가장 높은 주파수(Hz). 평균 파워 스펙트럼이 가장 센 곳보다 BAND_FLOOR_DB 아래로 떨어지기 전까지."""
    n = BAND_FFT
    if len(samples) < n:
        samples = np.pad(samples, (0, n - len(samples)))
    frames = len(samples) // n
    windowed = samples[: frames * n].reshape(frames, n) * np.hanning(n)
    power = (np.abs(np.fft.rfft(windowed, axis=1)) ** 2).mean(axis=0)
    peak = float(power.max())
    if peak <= 0:
        return 0.0
    loud = np.flatnonzero(power > peak * 10 ** (-BAND_FLOOR_DB / 10))
    return float(loud[-1]) * rate / n if len(loud) else 0.0


def _bitrate(path: pathlib.Path, seconds: float) -> float:
    return path.stat().st_size * 8 / seconds if seconds > 0 else 0.0


def _differ(a: float, b: float, margin: float = QUALITY_MARGIN) -> int:
    """b가 a보다 margin 비율 넘게 크면 1, 작으면 -1, 비슷하면 0."""
    if a <= 0 and b <= 0:
        return 0
    if b > a * (1 + margin):
        return 1
    if a > b * (1 + margin):
        return -1
    return 0


def _sound_quality(ffmpeg: str, path: pathlib.Path, work: pathlib.Path) -> dict | None:
    info = _probe(path)
    stream = info and _stream(info, "audio")
    if not stream:
        return None
    raw = _decode(ffmpeg, [path], work, ["-ac", "1", "-ar", str(BAND_RATE), "-t", f"{BAND_SECONDS:.3f}", "-f", "f32le"], "a")[0]
    if raw is None:
        return None
    codec = stream.get("codec_name") or ""
    bits = int(stream.get("bits_per_raw_sample") or stream.get("bits_per_sample") or 0)
    return {
        "band": bandwidth(np.frombuffer(raw, dtype="<f4").astype(np.float64)),
        "lossless": codec.startswith(LOSSLESS_AUDIO),
        "format": (int(stream.get("sample_rate") or 0), int(stream.get("channels") or 0), bits),
        "bitrate": _bitrate(path, _duration(info, stream)),
    }


def _sound_better(old: dict, new: dict) -> int:
    if abs(new["band"] - old["band"]) > BAND_MARGIN:
        return 1 if new["band"] > old["band"] else -1
    if new["lossless"] != old["lossless"]:
        return 1 if new["lossless"] else -1
    if new["format"] != old["format"]:
        return 1 if new["format"] > old["format"] else -1
    return _differ(old["bitrate"], new["bitrate"]) if not new["lossless"] else 0


def _image_quality(path: pathlib.Path) -> dict | None:
    info = _probe(path)
    stream = info and _stream(info, "video")
    if not stream:
        return None
    return {
        "pixels": int(stream.get("width") or 0) * int(stream.get("height") or 0),
        "lossless": (stream.get("codec_name") or "") not in LOSSY_IMAGE,
    }


def _image_better(old: dict, new: dict) -> int:
    if new["pixels"] != old["pixels"]:
        return 1 if new["pixels"] > old["pixels"] else -1
    if new["lossless"] != old["lossless"]:
        return 1 if new["lossless"] else -1
    return 0


def _video_quality(path: pathlib.Path) -> dict | None:
    info = _probe(path)
    stream = info and _stream(info, "video")
    if not stream:
        return None
    return {
        "pixels": int(stream.get("width") or 0) * int(stream.get("height") or 0),
        "fps": _fps(stream),
        "bitrate": _bitrate(path, _duration(info, stream)),
    }


def _video_better(old: dict, new: dict) -> int:
    if new["pixels"] != old["pixels"]:
        return 1 if new["pixels"] > old["pixels"] else -1
    return _differ(old["fps"], new["fps"]) or _differ(old["bitrate"], new["bitrate"])


def better_new(pairs: list[tuple[str, bytes, pathlib.Path]]) -> set[str]:
    """같은 소재로 본 (경로, 기존 파일 내용, 새 파일)들 중 새 파일을 남길 것의 경로.
    품질이 좋은 쪽을 남기고, 비슷하면 용량이 작은 쪽입니다. 품질을 읽지 못하면 기존 파일을 둡니다."""
    pairs = [p for p in pairs if kind(p[0]) is not None]
    if not pairs:
        return set()
    ffmpeg = preview._ffmpeg()
    if ffmpeg is None or _ffprobe() is None:
        logger.warning("media_match: ffmpeg/ffprobe not found. Keeping existing files for the same media.")
        return set()
    out = set()
    with tempfile.TemporaryDirectory(prefix="ebms-quality-") as tmp:
        work = pathlib.Path(tmp)
        for i, (path, old, new) in enumerate(pairs):
            k = kind(path)
            old_path = work / f"old{i}{pathlib.PurePosixPath(path).suffix.lower()}"
            old_path.write_bytes(old)
            try:
                if k == "sound":
                    a, b = _sound_quality(ffmpeg, old_path, work), _sound_quality(ffmpeg, new, work)
                    better = _sound_better
                elif k == "image":
                    a, b = _image_quality(old_path), _image_quality(new)
                    better = _image_better
                else:
                    a, b = _video_quality(old_path), _video_quality(new)
                    better = _video_better
                if a is None or b is None:
                    continue
                verdict = better(a, b)
                if verdict > 0 or (verdict == 0 and new.stat().st_size < len(old)):
                    out.add(path)
                logger.info(f"media_match: quality {path} old={a} new={b} -> {'new' if path in out else 'old'}")
            except Exception:
                logger.exception(f"media_match: quality comparison failed[{path}]")
            finally:
                old_path.unlink(missing_ok=True)
    return out
