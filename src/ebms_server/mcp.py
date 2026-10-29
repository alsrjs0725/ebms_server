"""MCP 서버(Streamable HTTP, `POST /api/mcp`). AI 클라이언트(agy, Claude 등)가 붙어 도구를 부릅니다.

권한은 사이트와 같은 두 단계입니다. `Authorization: Bearer`로 클라이언트 세션키(사용자)나 관리자 API 키(`ebms_ak_...`)를 보내고,
tools/list는 그 권한으로 쓸 수 있는 도구만 보여 주며 권한 밖의 도구는 부를 수 없습니다. 웹 세션(쿠키)으로는 쓰지 못합니다.

관리자 도구는 병합 충돌 판단입니다. 충돌 곡을 읽고 충돌 파일마다 old(기존 유지)·new(새 파일)·unsure 추천을 남깁니다.

- 서버는 판단하지 않습니다. 비교 수치(키음 상관계수·싱크 오차, 영상 프레임 차이, 품질, 차트 diff, 그 파일을 쓰는 차트)만 주고
  판단 기준은 initialize의 instructions(INSTRUCTIONS)로 알려 줍니다.
- 추천은 stage.json의 unit.ai에 따로 두고 unit.resolutions(관리자가 고른 것)는 건드리지 않습니다.
  관리자가 충돌 화면에서 추천을 보고 "AI 추천대로 고르기"로 옮긴 뒤 적용합니다.
- 응답은 요청마다 JSON 하나이고, 서버가 먼저 보내는 스트림(GET SSE)은 없습니다.
"""
import base64
import hashlib
import json
import logging
import pathlib
import posixpath
import subprocess
import tempfile
import time
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from . import audit, auth, conflicts, db, media_match, preview, staging
from .staging import StageError

logger = logging.getLogger(__name__)

router = APIRouter()

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "ebms-conflicts", "version": "1"}
AI_CHOICES = ("old", "new", "unsure")
# get_conflict_case 한 번에 주는 충돌 수
CASE_LIMIT = 50
CASE_MAX_LIMIT = 200
# 차트 diff를 case에 넣을 때 앞부분 줄 수(전체는 get_file_diff)
CASE_DIFF_LINES = 60
DIFF_MAX_LINES = 2000
# 그 파일을 쓰는 차트가 이보다 많으면 개수만 줍니다.
USED_BY_LIST = 5
REASON_MAX = 500
TEXT_MAX = 2000
# get_sound_clip: 한 번에 자르는 길이(초), 소리는 mp3(원본 채널·44.1kHz), 스펙트로그램은 png
CLIP_SECONDS = 10.0
CLIP_MAX_SECONDS = 30.0
CLIP_BITRATE = "192k"
SPECTRUM_SIZE = "640x240"
CLIP_TIMEOUT = 120

INSTRUCTIONS = """\
EBMS 곡 임포트에서 기존 곡에 병합하려다 같은 경로에 내용이 다른 파일이 있어 멈춘 곡(충돌)을 판단합니다.
파일마다 old(기존 곡 파일 유지)·new(새 파일로 바꿈)·unsure(사람이 봐야 함) 중 하나를 추천하고 submit_recommendation으로 남깁니다.
추천은 저장만 되고 바로 적용되지 않습니다. 관리자가 화면에서 확인한 뒤 적용합니다.

순서: list_conflict_units → get_conflict_case(offset을 늘려 가며 모든 충돌) → 필요하면 get_file_diff·get_sound_clip → submit_recommendation.
수치로 판단하기 어려운 키음(상관계수가 애매함, 싱크·품질 차이, 리믹스 의심)은 get_sound_clip으로 기존·새 소리를 직접 들어 봅니다.
소리를 들을 수 없으면 같이 오는 스펙트로그램(세로축 주파수, 위쪽이 비어 있으면 손실 압축으로 고음이 잘린 것)을 봅니다.

판단 기준:
1. 다른 곡 의심이 먼저입니다. existing_songs와 new_charts의 제목·아티스트가 다르거나, 키음 상관계수(correlation과
   aligned_correlation)가 둘 다 낮으면(0.5 미만) 이름만 같은 다른 소재입니다. 이때 그 파일은 old로 두고 split_recommended=true,
   issues에 "다른 곡"이라고 적습니다. 새 파일로 바꾸면 기존 곡 차트가 다른 소리를 냅니다.
2. 같은 소재(상관계수 높음)면 quality.better 쪽을 고릅니다. same이면 old.
3. 싱크만 다르면(offset_ms가 크고 aligned_correlation이 높음): 그 키음을 쓰는 차트(used_by_old/used_by_new)가 어느 쪽 싱크에
   맞춰졌는지가 기준입니다. 한쪽 차트만 쓰면 그쪽, 양쪽이 다 쓰면 unsure.
4. 차트 충돌: header_changes와 diff를 봅니다. #WAV 번호가 다른 파일을 가리키게 바뀌었으면(wav_id_remapped) 노래가 이상해질 수 있으니
   old, issues에 적습니다. 노트 수정·오타 수정처럼 같은 차트의 개정판이면 new. conflict_sounds_old/new는 그 차트가 쓰는
   충돌 키음입니다. 차트를 new로 하면서 그 키음을 old로 두면 소리가 어긋날 수 있으니 같이 봅니다.
5. 이미지·영상: 내용이 같고 품질만 다르면 품질 좋은 쪽, 내용이 다르면(다른 그림·다른 영상) unsure.
6. 파일을 풀지 못하거나(error) 한쪽이 깨졌으면 정상인 쪽, 둘 다 이상하면 unsure.
7. 확신이 없으면 unsure. 틀린 old/new보다 unsure가 낫습니다. confidence는 0~1, reason은 한국어 한 문장.
"""

# admin: True면 관리자만 보고 부를 수 있습니다.
TOOLS = [
    {
        "name": "whoami",
        "description": "이 연결의 사용자(닉네임, 권한: user/admin).",
        "inputSchema": {"type": "object", "properties": {}},
        "admin": False,
    },
    {
        "name": "list_conflict_units",
        "description": "검토 대기 묶음에서 병합 충돌이 남은 곡 목록. 곡마다 충돌 수, 관리자가 고른 수, AI 추천 수.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "stage_id": {"type": "string", "description": "이 묶음만 봅니다. 없으면 모든 묶음."},
                "pending_only": {"type": "boolean", "description": "AI 추천이 덜 된 곡만(기본 true)."},
            },
        },
        "admin": True,
    },
    {
        "name": "get_conflict_case",
        "description": (
            "곡 하나의 판단 자료: 겨룬 기존 곡과 차트, 새 차트(same_as_registered/conflict/new_chart), "
            "충돌 파일마다 비교 수치·품질·그 파일을 쓰는 차트. offset·limit으로 나눠 받습니다."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "stage_id": {"type": "string"},
                "unit_id": {"type": "string"},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": CASE_MAX_LIMIT},
            },
            "required": ["stage_id", "unit_id"],
        },
        "admin": True,
    },
    {
        "name": "get_file_diff",
        "description": "텍스트 충돌 파일(차트 등)의 전체 줄 단위 diff.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "stage_id": {"type": "string"},
                "unit_id": {"type": "string"},
                "path": {"type": "string"},
                "max_lines": {"type": "integer", "minimum": 1, "maximum": DIFF_MAX_LINES},
            },
            "required": ["stage_id", "unit_id", "path"],
        },
        "admin": True,
    },
    {
        "name": "get_sound_clip",
        "description": (
            "키음 충돌의 기존(old)·새(new) 소리를 같은 구간으로 잘라 mp3 오디오와 스펙트로그램(png)으로 줍니다. "
            "수치로 애매한 충돌을 귀나 눈으로 확인할 때 씁니다."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "stage_id": {"type": "string"},
                "unit_id": {"type": "string"},
                "path": {"type": "string"},
                "start": {"type": "number", "minimum": 0, "description": "자르기 시작(초, 기본 0)"},
                "seconds": {"type": "number", "minimum": 0.5, "maximum": CLIP_MAX_SECONDS, "description": f"길이(초, 기본 {CLIP_SECONDS:g})"},
                "audio": {"type": "boolean", "description": "오디오를 넣을지(기본 true)"},
                "spectrogram": {"type": "boolean", "description": "스펙트로그램을 넣을지(기본 true)"},
            },
            "required": ["stage_id", "unit_id", "path"],
        },
        "admin": True,
    },
    {
        "name": "submit_recommendation",
        "description": (
            "충돌 파일마다 추천(old/new/unsure)을 저장합니다. 관리자가 고른 것은 바꾸지 않습니다. "
            "나눠 받은 경우 여러 번 불러도 되고, 같은 파일은 나중 추천으로 바뀝니다."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "stage_id": {"type": "string"},
                "unit_id": {"type": "string"},
                "model": {"type": "string", "description": "추천한 모델 이름"},
                "summary": {"type": "string", "description": "곡 전체 판단 한두 문장"},
                "split_recommended": {"type": "boolean", "description": "다른 곡이 섞여 새 곡으로 나눠야 하면 true"},
                "issues": {"type": "array", "items": {"type": "string"}},
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "choice": {"type": "string", "enum": list(AI_CHOICES)},
                            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                            "reason": {"type": "string"},
                        },
                        "required": ["path", "choice"],
                    },
                },
            },
            "required": ["stage_id", "unit_id", "items"],
        },
        "admin": True,
    },
]


class ToolError(Exception):
    pass


# ---- 도구 ----


def list_conflict_units(stage_id: str | None = None, pending_only: bool = True) -> dict:
    stage_ids = [stage_id] if stage_id else [s["id"] for s in staging.list_stages()]
    units = []
    for sid in stage_ids:
        with staging._lock:
            stage = staging._load(sid)
        for unit in stage["units"]:
            paths = conflicts._conflict_paths(unit)
            if not paths:
                continue
            ai = (unit.get("ai") or {}).get("items") or {}
            recommended = sum(1 for p in paths if p in ai)
            if pending_only and recommended == len(paths):
                continue
            units.append({
                "stage_id": sid,
                "stage_name": stage["name"],
                "unit_id": unit["id"],
                "folder": unit["folder"],
                "songs": sorted(set(paths.values())),
                "conflicts": len(paths),
                "chosen": sum(1 for p in paths if (unit.get("resolutions") or {}).get(p)),
                "recommended": recommended,
            })
    return {"units": units}


def _stems(content: bytes, base: str) -> set[str]:
    """차트가 쓰는 키음(소문자, 확장자 뺀 경로)."""
    try:
        return {stem for stems in db.chart_sounds(content, base) for stem in stems}
    except Exception:
        return set()


def _charts(files: conflicts._Files, side: str) -> dict[str, dict]:
    """한쪽(old/new) 곡 폴더 바로 아래 차트 {경로: {title, artist, sha256, stems}}."""
    paths = files.old if side == "old" else files.new
    out = {}
    for path in sorted(paths):
        if "/" in path or not db._is_chart(path):
            continue
        content = files.read(side, path)
        try:
            meta = db.chart_meta(content)
        except Exception:
            meta = {}
        out[path] = {
            "title": meta.get("title", ""),
            "artist": meta.get("artist", ""),
            "sha256": hashlib.sha256(content).hexdigest(),
            "stems": _stems(content, posixpath.dirname(path)),
        }
    return out


def _used_by(charts: dict[str, dict], stem: str) -> list[str] | str:
    used = [path for path, info in charts.items() if stem in info["stems"]]
    return used if len(used) <= USED_BY_LIST else f"{len(used)} charts"


def _quality(stage: dict, unit: dict, item: dict, old: bytes, new: bytes) -> dict | None:
    kind = item["kind"]
    probe = {
        "sound": (lambda p, w: media_match._sound_quality(preview._ffmpeg(), p, w), media_match._sound_better),
        "image": (lambda p, w: media_match._image_quality(p), media_match._image_better),
        "video": (lambda p, w: media_match._video_quality(p), media_match._video_better),
    }.get(kind)
    if probe is None or preview._ffmpeg() is None:
        return None
    read, better = probe

    def make() -> dict:
        with tempfile.TemporaryDirectory(prefix="ebms-mcp-") as tmp:
            work = pathlib.Path(tmp)
            a = work / f"old{posixpath.splitext(item['old_path'])[1].lower()}"
            b = work / f"new{posixpath.splitext(item['path'])[1].lower()}"
            a.write_bytes(old)
            b.write_bytes(new)
            qa, qb = read(a, work), read(b, work)
        if qa is None or qb is None:
            return {"error": "품질을 읽지 못했습니다."}
        value = better(qa, qb)
        return {"old": qa, "new": qb, "better": "new" if value > 0 else "old" if value < 0 else "same"}

    key = conflicts._cache_name(item["path"], hashlib.sha256(old + b"\0" + new).digest(), "quality")
    return conflicts._cached_json(stage, unit, key, make)


def _chart_facts(stage_id: str, unit_id: str, path: str) -> dict:
    diff = conflicts.text_diff(stage_id, unit_id, path)
    if diff.get("binary"):
        return {"binary": True}
    body = [l for l in diff["diff"].splitlines() if l[:1] in "+-" and not l.startswith(("+++", "---"))]
    header = [l for l in body if l[1:2] == "#" and not l[2:3].isdigit()]
    wav = {"-": {}, "+": {}}
    for line in header:
        if line[1:5].upper() == "#WAV":
            parts = line[5:].split(None, 1)
            if len(parts) == 2:
                wav[line[0]][parts[0].upper()] = posixpath.splitext(parts[1].strip().replace("\\", "/").lower())[0]
    remapped = sorted(k for k in set(wav["-"]) & set(wav["+"]) if wav["-"][k] != wav["+"][k])
    return {
        "changed_lines": len(body),
        "note_lines": sum(1 for l in body if l[1:2] == "#" and l[2:3].isdigit()),
        "header_changes": header[:20],
        "wav_id_remapped": [f"#WAV{k}: {wav['-'][k]} -> {wav['+'][k]}" for k in remapped[:10]],
        "wav_id_remapped_count": len(remapped),
        "diff_head": "\n".join(diff["diff"].splitlines()[:CASE_DIFF_LINES]),
    }


def _facts(stage: dict, unit: dict, item: dict) -> dict:
    """충돌 파일 하나의 비교 수치. 실패해도 error만 적고 넘어갑니다."""
    stage_id, unit_id, path = stage["id"], unit["id"], item["path"]
    if item["old_path"] is None:
        return {"error": "기존 곡에 겨룬 파일이 없습니다."}
    kind = item["kind"]
    try:
        if kind == "sound":
            facts = conflicts.sound_compare(stage_id, unit_id, path)
            facts = {
                k: v for k, v in facts.items() if k not in ("old", "new")
            } | {"old_seconds": (facts.get("old") or {}).get("seconds"), "new_seconds": (facts.get("new") or {}).get("seconds")}
        elif kind == "video":
            v = conflicts.video_compare(stage_id, unit_id, path)
            if "error" in v:
                return v
            diff = v["diff"]
            facts = {
                "fps": v["fps"], "old_frames": v["old_frames"], "new_frames": v["new_frames"],
                "mean_diff": round(sum(diff) / len(diff), 4) if diff else None,
                "changed_segments": len(v["segments"]), "segments": v["segments"][:10],
            }
        elif kind == "chart":
            facts = _chart_facts(stage_id, unit_id, path)
        elif kind == "image":
            facts = {}
        else:
            d = conflicts.text_diff(stage_id, unit_id, path)
            facts = {"binary": True} if d.get("binary") else {"diff_head": "\n".join(d["diff"].splitlines()[:CASE_DIFF_LINES])}
        if kind in ("sound", "image", "video"):
            _, old = conflicts._side(stage, unit, path, "old")
            _, new = conflicts._side(stage, unit, path, "new")
            if kind == "image":
                facts.update(_image_facts(old, item["old_path"], new, path))
            quality = _quality(stage, unit, item, old, new)
            if quality:
                facts["quality"] = quality
        return facts
    except StageError as e:
        return {"error": e.detail}
    except Exception as e:
        logger.warning(f"mcp facts {stage_id}/{unit_id} {path}: {e!r}")
        return {"error": str(e)[:200]}


def _image_facts(old: bytes, old_name: str, new: bytes, new_name: str) -> dict:
    """그림 둘을 IMAGE_SIZE로 줄여 평균 차이와 많이 바뀐 픽셀 비율(media_match.same_image와 같은 방식)."""
    import numpy as np

    size = media_match.IMAGE_SIZE
    args = ["-frames:v", "1", "-vf", f"scale={size}:{size}:flags=area", "-pix_fmt", "rgba", "-f", "rawvideo"]
    a, b = conflicts._decode(old, old_name, args, "v"), conflicts._decode(new, new_name, args, "v")
    if not a or not b or len(a) != len(b):
        return {"error": "그림을 풀지 못했습니다."}
    diff = (np.abs(np.frombuffer(a, np.uint8).astype(np.float32) - np.frombuffer(b, np.uint8).astype(np.float32)) / 255).reshape(-1, 4)
    return {
        "mean_diff": round(float(diff.mean()), 4),
        "changed_pixels": round(float((diff.max(axis=1) > media_match.IMAGE_PIXEL_DIFF).mean()), 4),
        "same": bool(media_match.same_image(np.frombuffer(a, np.uint8), np.frombuffer(b, np.uint8))),
    }


def get_conflict_case(stage_id: str, unit_id: str, offset: int = 0, limit: int = CASE_LIMIT) -> dict:
    limit = max(1, min(int(limit), CASE_MAX_LIMIT))
    offset = max(0, int(offset))
    listing = conflicts.list_conflicts(stage_id, unit_id)
    stage, unit = conflicts._unit(stage_id, unit_id)
    all_items = listing["items"]
    page = all_items[offset : offset + limit]
    ai = (unit.get("ai") or {}).get("items") or {}

    song_ids = sorted({item["song_id"] for item in all_items})
    old_charts: dict[int, dict] = {}
    new_charts: dict[str, dict] = {}
    for song_id in song_ids:
        with conflicts._Files(stage, unit, song_id) as files:
            old_charts[song_id] = _charts(files, "old")
            if not new_charts:
                new_charts = _charts(files, "new")

    registered = {info["sha256"] for charts in old_charts.values() for info in charts.values()}
    old_paths = {path.lower() for charts in old_charts.values() for path in charts}
    sound_paths = {i["path"]: i for i in all_items if i["kind"] == "sound"}

    out_items = []
    for item in page:
        song_id = item["song_id"]
        rec = {
            "path": item["path"],
            "kind": item["kind"],
            "song_id": song_id,
            "old_path": item["old_path"],
            "old_size": item["old_size"],
            "new_size": item["new_size"],
            "choice": item["choice"],
            "ai": ai.get(item["path"]),
        }
        if item["kind"] in ("sound", "image", "video"):
            stem = posixpath.splitext(item["path"].lower())[0]
            old_stem = posixpath.splitext((item["old_path"] or item["path"]).lower())[0]
            rec["used_by_old"] = _used_by(old_charts[song_id], old_stem)
            rec["used_by_new"] = _used_by(new_charts, stem)
        elif item["kind"] == "chart":
            stems = lambda charts, p: next((v["stems"] for k, v in charts.items() if k.lower() == p.lower()), set())
            conflict_stems = {posixpath.splitext(p.lower())[0]: p for p in sound_paths}
            rec["conflict_sounds_old"] = sorted(conflict_stems[s] for s in stems(old_charts[song_id], item["old_path"] or item["path"]) if s in conflict_stems)
            rec["conflict_sounds_new"] = sorted(conflict_stems[s] for s in stems(new_charts, item["path"]) if s in conflict_stems)
        rec["compare"] = _facts(stage, unit, item)
        out_items.append(rec)

    return {
        "stage_id": stage_id,
        "unit_id": unit_id,
        "folder": unit["folder"],
        "existing_songs": {
            str(song_id): {
                "folder": listing["songs"].get(str(song_id)),
                "charts": [{"path": p, "title": v["title"], "artist": v["artist"]} for p, v in old_charts[song_id].items()],
            }
            for song_id in song_ids
        },
        "new_charts": [
            {
                "path": p, "title": v["title"], "artist": v["artist"],
                "status": "same_as_registered" if v["sha256"] in registered else "conflict" if p.lower() in old_paths else "new_chart",
            }
            for p, v in new_charts.items()
        ],
        "ai": {k: v for k, v in (unit.get("ai") or {}).items() if k != "items"} or None,
        "total": len(all_items),
        "offset": offset,
        "next_offset": offset + len(page) if offset + len(page) < len(all_items) else None,
        "items": out_items,
    }


def get_file_diff(stage_id: str, unit_id: str, path: str, max_lines: int = DIFF_MAX_LINES) -> dict:
    result = conflicts.text_diff(stage_id, unit_id, path)
    if "diff" in result:
        lines = result["diff"].splitlines()
        max_lines = max(1, min(int(max_lines), DIFF_MAX_LINES))
        result["diff"] = "\n".join(lines[:max_lines])
        result["truncated"] = len(lines) > max_lines
    return result


class Content(list):
    """도구가 MCP content(텍스트·오디오·이미지 항목)를 그대로 돌려줄 때 씁니다."""


def _clip(content: bytes, name: str, start: float, seconds: float, out_args: list[str], suffix: str) -> bytes | None:
    """content(이름 name)의 start초부터 seconds초를 ffmpeg out_args로 바꾼 결과. 실패하면 None."""
    ffmpeg = conflicts._ffmpeg()
    with tempfile.TemporaryDirectory(prefix="ebms-mcp-") as tmp:
        source = pathlib.Path(tmp) / f"in{posixpath.splitext(name)[1].lower()}"
        source.write_bytes(content)
        out = pathlib.Path(tmp) / f"out{suffix}"
        cmd = [ffmpeg, "-v", "error", "-nostdin", "-y", "-ss", f"{start:.3f}", "-t", f"{seconds:.3f}", "-i", str(source), *out_args, str(out)]
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=CLIP_TIMEOUT, check=False)
        except subprocess.TimeoutExpired:
            return None
        return out.read_bytes() if result.returncode == 0 and out.is_file() and out.stat().st_size else None


def get_sound_clip(
    stage_id: str, unit_id: str, path: str, start: float = 0.0, seconds: float = CLIP_SECONDS,
    audio: bool = True, spectrogram: bool = True,
) -> Content:
    stage, unit = conflicts._unit(stage_id, unit_id)
    if conflicts._kind(path) != "sound":
        raise ToolError("키음(소리) 충돌만 자를 수 있습니다.")
    start = max(0.0, float(start))
    seconds = max(0.5, min(float(seconds), CLIP_MAX_SECONDS))
    content = Content()
    info = {"path": path, "start": start, "seconds": seconds}
    for side in ("old", "new"):
        name, data = conflicts._side(stage, unit, path, side)
        info[f"{side}_path"] = name
        parts = []
        if audio:
            clip = _clip(data, name, start, seconds, ["-vn", "-ar", "44100", "-c:a", "libmp3lame", "-b:a", CLIP_BITRATE], ".mp3")
            if clip:
                parts.append({"type": "audio", "data": base64.b64encode(clip).decode(), "mimeType": "audio/mpeg"})
        if spectrogram:
            image = _clip(data, name, start, seconds, ["-lavfi", f"showspectrumpic=s={SPECTRUM_SIZE}:legend=1", "-frames:v", "1"], ".png")
            if image:
                parts.append({"type": "image", "data": base64.b64encode(image).decode(), "mimeType": "image/png"})
        label = "기존(old)" if side == "old" else "새(new)"
        if not parts:
            content.append({"type": "text", "text": f"{label} {name}: 이 구간을 풀지 못했습니다(구간이 소리 길이를 넘었거나 깨진 파일)."})
            continue
        content.append({"type": "text", "text": f"{label} {name} ({start:g}~{start + seconds:g}초)"})
        content.extend(parts)
    content.insert(0, {"type": "text", "text": json.dumps(info, ensure_ascii=False)})
    return content


def _text(value, limit: int = TEXT_MAX) -> str:
    return str(value or "").strip()[:limit]


def submit_recommendation(
    stage_id: str,
    unit_id: str,
    items: list[dict],
    model: str = "",
    summary: str = "",
    split_recommended: bool | None = None,
    issues: list[str] | None = None,
) -> dict:
    if not isinstance(items, list):
        raise ToolError("items는 배열이어야 합니다.")
    now = int(time.time())
    clean = {}
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise ToolError("items의 각 항목에는 path가 있어야 합니다.")
        if item.get("choice") not in AI_CHOICES:
            raise ToolError(f"choice는 old, new, unsure 중 하나입니다: {item.get('path')}")
        confidence = item.get("confidence")
        if confidence is not None:
            try:
                confidence = min(1.0, max(0.0, float(confidence)))
            except (TypeError, ValueError):
                raise ToolError(f"confidence는 0~1 숫자입니다: {item['path']}") from None
        clean[item["path"]] = {
            "choice": item["choice"],
            "confidence": confidence,
            "reason": _text(item.get("reason"), REASON_MAX),
            "model": _text(model, 100),
            "at": now,
        }
    with staging._lock:
        stage = staging._load(stage_id)
        staging._require_ready(stage)
        unit = next((u for u in stage["units"] if u["id"] == unit_id), None)
        if unit is None:
            raise StageError(404, "unit not found")
        known = conflicts._conflict_paths(unit)
        unknown = sorted(set(clean) - set(known))
        if unknown:
            raise ToolError(f"충돌 파일이 아닙니다: {', '.join(unknown[:10])}")
        ai = dict(unit.get("ai") or {})
        saved = dict(ai.get("items") or {})
        saved.update(clean)
        ai["items"] = saved
        if model:
            ai["model"] = _text(model, 100)
        if summary:
            ai["summary"] = _text(summary)
        if split_recommended is not None:
            ai["split_recommended"] = bool(split_recommended)
        if issues is not None:
            ai["issues"] = [_text(i, REASON_MAX) for i in issues if _text(i)][:50]
        ai["updated_at"] = now
        unit["ai"] = ai
        staging._save(stage)
    return {
        "saved": len(clean),
        "recommended": sum(1 for p in known if p in saved),
        "conflicts": len(known),
        "missing": sorted(p for p in known if p not in saved)[:20],
    }


def whoami(user) -> dict:
    return {"id": user.id, "nickname": user.display_name, "role": "admin" if user.is_admin else "user"}


# 관리자 도구(whoami는 사용자를 받으므로 _call에서 따로 부릅니다)
HANDLERS = {
    "list_conflict_units": list_conflict_units,
    "get_conflict_case": get_conflict_case,
    "get_file_diff": get_file_diff,
    "get_sound_clip": get_sound_clip,
    "submit_recommendation": submit_recommendation,
}


# ---- JSON-RPC ----


def _user(request: Request):
    """Bearer로 보낸 클라이언트 세션키나 관리자 API 키의 사용자. 쿠키는 보지 않습니다(다른 사이트가 대신 부르지 못하게)."""
    session = auth.optional_api_session(request) if auth.bearer_token(request) else None
    if session is None:
        raise HTTPException(status_code=401, detail="login required", headers={"WWW-Authenticate": "Bearer"})
    return session[0]


def _tools(user) -> list[dict]:
    return [{k: v for k, v in tool.items() if k != "admin"} for tool in TOOLS if user.is_admin or not tool["admin"]]


def _error(msg_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _call(request: Request, user, params: dict) -> dict:
    name = params.get("name")
    args = params.get("arguments") or {}
    tool = next((t for t in TOOLS if t["name"] == name), None)
    if tool is None or not isinstance(args, dict):
        raise KeyError(name)
    if tool["admin"] and not user.is_admin:
        raise PermissionError(name)
    try:
        value = whoami(user) if name == "whoami" else HANDLERS[name](**args)
    except TypeError as e:
        return {"content": [{"type": "text", "text": f"인자가 맞지 않습니다: {e}"}], "isError": True}
    except (ToolError, StageError, ValueError) as e:
        message = e.detail if isinstance(e, StageError) else str(e)
        return {"content": [{"type": "text", "text": str(message)}], "isError": True}
    if isinstance(value, Content):
        return {"content": list(value)}
    if name == "submit_recommendation":
        # 접속기록은 ebms.log에도 같은 줄이 남습니다.
        audit.record(request, user.id, "mcp.recommend", f"{args.get('stage_id')}/{args.get('unit_id')}", saved=value["saved"])
    return {
        "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, default=list)}],
        "structuredContent": json.loads(json.dumps(value, default=list)),
    }


def _handle(request: Request, user, message) -> dict | None:
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        return _error(message.get("id") if isinstance(message, dict) else None, -32600, "Invalid Request")
    msg_id, method, params = message.get("id"), message["method"], message.get("params") or {}
    if "id" not in message:
        return None  # 알림(notifications/initialized 등)은 답하지 않습니다.
    if method == "initialize":
        asked = params.get("protocolVersion")
        return {"jsonrpc": "2.0", "id": msg_id, "result": {
            "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": INSTRUCTIONS,
        }}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": _tools(user)}}
    if method == "tools/call":
        try:
            return {"jsonrpc": "2.0", "id": msg_id, "result": _call(request, user, params)}
        except PermissionError:
            return _error(msg_id, -32003, f"관리자만 쓸 수 있는 도구입니다: {params.get('name')}")
        except KeyError:
            return _error(msg_id, -32602, f"알 수 없는 도구: {params.get('name')}")
    return _error(msg_id, -32601, f"Method not found: {method}")


@router.post("/api/mcp")
def mcp(request: Request, payload: Annotated[Any, Body()]):
    user = _user(request)
    if isinstance(payload, list):
        if not payload:
            return JSONResponse(_error(None, -32600, "Invalid Request"))
        replies = [r for r in (_handle(request, user, m) for m in payload) if r is not None]
        return JSONResponse(replies) if replies else Response(status_code=202)
    reply = _handle(request, user, payload)
    return JSONResponse(reply) if reply is not None else Response(status_code=202)


@router.get("/api/mcp")
def mcp_stream(request: Request):
    """서버가 먼저 보낼 알림이 없어 SSE 스트림은 열지 않습니다(MCP 규격: 405)."""
    _user(request)
    return Response(status_code=405, headers={"Allow": "POST"})
