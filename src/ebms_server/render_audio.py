"""중복·섞임 점검 화면에서 차트를 렌더한 음원을 들어 봅니다.

색인(render_index)은 소리 지문만 저장하고 렌더 음원은 버리므로, 들을 때 그 차트만 다시 렌더해 ogg로 만들고
캐시 폴더에 잠깐 둡니다(LISTEN_MAX_BYTES를 넘으면 오래된 것부터 지움). 곡 zip(sha256)·차트·방식이 같으면 다시 씁니다.
렌더는 색인과 같게 #RANDOM은 첫 분기(preview.parse_chart)이고, 처음부터 MAX_SECONDS초까지입니다.
만드는 데 수 초~수십 초가 걸려 뒤 스레드에서 만들고, 화면은 다 될 때까지 상태를 물어봅니다.
"""
import hashlib
import logging
import os
import pathlib
import posixpath
import subprocess
import tempfile
import threading
import zipfile

import numpy as np

from . import cache, constant, preview, render_index

logger = logging.getLogger(__name__)

RATE = 44100
CHANNELS = 2
MAX_SECONDS = 10 * 60
PEAK = 0.98
VORBIS_QUALITY = "4"
# 들어 볼 음원 캐시 한도(넘으면 오래 안 쓴 것부터 지움)와 동시에 만드는 수
LISTEN_MAX_BYTES = 1024 ** 3
WORKERS = 2
NICE = 10
VERSION = 1

_jobs: dict[str, dict] = {}  # 만드는 중인 음원 {키: {error}}
_lock = threading.Lock()
_slots = threading.Semaphore(WORKERS)


def _dir() -> pathlib.Path:
    path = (constant.CACHE_DIR if cache.enabled() else constant.TMP_DIR) / "listen"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _key(sha256: str, chart: str) -> str:
    return hashlib.sha256(f"{VERSION}\0{sha256}\0{chart}".encode()).hexdigest()


def _song(song_id: int, chart: str):
    """(곡 zip 파일 목록, 연 저장소 파일). 곡이나 차트가 없으면 LookupError."""
    files, file = render_index._song_row(song_id)
    if file is None:
        raise LookupError("곡이 없습니다.")
    if not any(f["path"] == chart for f in files) or not chart.lower().endswith(constant.BMS_FORMAT):
        file.close()
        raise LookupError("차트가 없습니다.")
    return files, file


def status(song_id: int, chart: str) -> dict:
    """{status: ready} / {status: running}(처음 부르면 만들기 시작) / {status: error, error}."""
    files, file = _song(song_id, chart)
    key = _key(file.sha256, chart)
    out = _dir() / f"{key}.ogg"
    if out.is_file():
        file.close()
        return {"status": "ready"}
    with _lock:
        job = _jobs.get(key)
        if job is not None and job.get("error"):
            del _jobs[key]  # 실패는 한 번 알리고, 다음에 부르면 다시 만듭니다.
            file.close()
            return {"status": "error", "error": job["error"]}
        if job is None:
            if preview._ffmpeg() is None:
                file.close()
                raise RuntimeError("ffmpeg가 없습니다.")
            job = _jobs[key] = {}
            threading.Thread(target=_job, args=(files, file, chart, out, job, key), daemon=True,
                             name=f"render-audio-{key[:8]}").start()
        else:
            file.close()
    return {"status": "running"}


def path(song_id: int, chart: str) -> pathlib.Path:
    """다 만든 음원 경로. 아직 없으면 FileNotFoundError."""
    _, file = _song(song_id, chart)
    file.close()
    out = _dir() / f"{_key(file.sha256, chart)}.ogg"
    if not out.is_file():
        raise FileNotFoundError(out.name)
    os.utime(out)  # 오래 안 쓴 것부터 지우도록
    return out


def _job(files: list[dict], file, chart: str, out: pathlib.Path, job: dict, key: str) -> None:
    try:
        with _slots:
            try:
                os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), NICE)
            except OSError:
                pass
            with os.fdopen(os.dup(file._fd), "rb") as raw, zipfile.ZipFile(raw) as zf:
                file.close()
                data = render(files, zf.read, chart)
            part = out.with_name(out.name + ".part")
            part.write_bytes(data)
            part.replace(out)
            _prune()
        with _lock:
            _jobs.pop(key, None)
    except Exception as e:
        logger.exception(f"render audio failed[{chart}]")
        job["error"] = f"렌더하지 못했습니다: {e}"
    finally:
        file.close()


def render(files: list[dict], read, chart_path: str, seconds: float = MAX_SECONDS) -> bytes:
    """곡 안 차트 하나를 처음부터 seconds초까지 렌더한 ogg. read(곡 안 경로)는 그 파일 내용."""
    ffmpeg = preview._ffmpeg()
    sounds = [s for s in preview.parse_chart(read(chart_path)).sounds if 0 <= s.time < seconds]
    resolver = preview._Resolver([f["path"] for f in files])
    base = posixpath.dirname(chart_path)
    targets: dict[str, str | None] = {}
    for s in sounds:
        if s.name not in targets:
            targets[s.name] = resolver.find(base, s.name)
    members = sorted({t for t in targets.values() if t})
    if not members:
        raise ValueError("울릴 키음이 없습니다.")
    with tempfile.TemporaryDirectory(dir=cache.work_dir()) as tmp:
        disk = []
        for i, member in enumerate(members):
            p = pathlib.Path(tmp) / f"{i}{posixpath.splitext(member)[1].lower()}"
            p.write_bytes(read(member))
            disk.append(p)
        decoded = dict(zip(members, preview._decode_sounds(ffmpeg, disk, seconds, RATE, CHANNELS)))
    limit = int(seconds * RATE)
    segments, end = [], 0
    for s in sounds:
        data = decoded.get(targets[s.name] or "")
        if data is None:
            continue
        seg = data[max(0, int(round(s.offset * RATE))) :]
        if s.end is not None:
            seg = seg[: max(0, int(round((s.end - s.time) * RATE)))]
        pos = int(round(s.time * RATE))
        n = min(len(seg), limit - pos)
        if n > 0:
            segments.append((pos, seg[:n]))
            end = max(end, pos + n)
    if not end:
        raise ValueError("울릴 키음이 없습니다.")
    buf = np.zeros((end, CHANNELS), dtype=np.float32)
    for pos, seg in segments:
        buf[pos : pos + len(seg)] += seg
    del segments, decoded
    peak = float(np.max(np.abs(buf)))
    if peak > PEAK:
        buf *= PEAK / peak
    result = subprocess.run(
        [ffmpeg, "-v", "error", "-nostdin", "-f", "f32le", "-ar", str(RATE), "-ac", str(CHANNELS), "-i", "pipe:0",
         "-c:a", "libvorbis", "-q:a", VORBIS_QUALITY, "-f", "ogg", "pipe:1"],
        input=buf.astype("<f4").tobytes(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=preview.FFMPEG_TIMEOUT * 5, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", "replace").strip()[-300:] or "ffmpeg 오류")
    return result.stdout


def _prune() -> None:
    files = []
    for p in _dir().glob("*.ogg"):
        try:
            st = p.stat()
        except OSError:
            continue
        files.append((st.st_mtime, st.st_size, p))
    total = sum(size for _, size, _ in files)
    for _, size, p in sorted(files, key=lambda f: f[0]):
        if total <= LISTEN_MAX_BYTES:
            break
        p.unlink(missing_ok=True)
        total -= size
