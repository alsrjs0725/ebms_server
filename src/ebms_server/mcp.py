"""MCP 서버(Streamable HTTP, `POST /api/mcp`). AI 클라이언트(agy, Claude 등)가 붙어 도구를 부릅니다.

권한은 사이트와 같은 두 단계입니다. `Authorization: Bearer`로 클라이언트 세션키(사용자)나 관리자 API 키(`ebms_ak_...`)를 보내고,
tools/list는 그 권한으로 쓸 수 있는 도구만 보여 주며 권한 밖의 도구는 부를 수 없습니다. 웹 세션(쿠키)으로는 쓰지 못합니다.

관리자 도구는 두 가지입니다.

1. 병합 충돌 판단: 충돌 곡을 읽고 충돌 파일마다 old(기존 유지)·new(새 파일)·unsure 추천을 남깁니다.
2. 등록된 곡 정리: 렌더 색인이 찾은 중복(dup)·섞임(mixed)을 보고 곡을 나누거나(split_song) 합칩니다(merge_songs).
   이건 추천이 아니라 바로 적용됩니다. 되돌리려면 반대 도구(나눈 곡은 합치고, 합친 차트는 나눔)를 씁니다.

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

from . import admin, audit, auth, conflicts, constant, db, importer, media_match, preview, render_check, staging
from .staging import StageError

logger = logging.getLogger(__name__)

router = APIRouter()

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "ebms", "version": "2"}
AI_CHOICES = ("old", "new", "unsure")
# get_conflict_case 한 번에 주는 충돌 수
CASE_LIMIT = 20
CASE_MAX_LIMIT = 50
# 한 번 응답에 비교를 계산하는 시간 상한(초). 넘으면 그때까지 계산한 것만 주고 next_offset으로 이어 받게 합니다
# (앞단 프록시가 90초에 끊으므로). 계산한 비교는 캐시돼 다음에는 빠릅니다.
CASE_SECONDS = 30.0
# 차트 diff를 case에 넣을 때 앞부분 줄 수(전체는 get_file_diff)
CASE_DIFF_LINES = 30
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
먼저 whoami를 불러 server가 작업하려는 서버 주소인지 확인하세요. 다르면 아무것도 바꾸지 말고 멈춥니다.

EBMS 곡 임포트에서 기존 곡에 병합하려다 같은 경로에 내용이 다른 파일이 있어 멈춘 곡(충돌)을 판단합니다.
파일마다 old(기존 곡 파일 유지)·new(새 파일로 바꿈)·unsure(사람이 봐야 함) 중 하나를 추천하고 submit_recommendation으로 남깁니다.
추천은 저장만 되고 바로 적용되지 않습니다. 관리자가 화면에서 확인한 뒤 적용합니다.

순서: list_conflict_units → get_conflict_case → (필요하면 get_file_diff·get_sound_clip) → 그 페이지의 submit_recommendation →
next_offset이 있으면 그 offset으로 다음 페이지. 페이지를 받을 때마다 바로 추천을 저장하세요(모아 두었다가 한 번에 내지 말 것).
곡 정보(existing_songs, new_charts)는 offset 0 응답에만 있습니다. 셸이나 파일 도구는 필요 없습니다.
수치로 판단하기 어려운 키음(상관계수가 애매함, 싱크·품질 차이, 리믹스 의심)은 get_sound_clip으로 기존·새 소리를 직접 들어 봅니다.
소리를 들을 수 없으면 같이 오는 스펙트로그램(세로축 주파수, 위쪽이 비어 있으면 손실 압축으로 고음이 잘린 것)을 봅니다.

판단 기준:
1. 다른 곡 의심이 먼저입니다. existing_songs와 new_charts의 제목·아티스트가 다르거나, 키음 상관계수(correlation과
   aligned_correlation)가 둘 다 낮으면(0.5 미만) 이름만 같은 다른 소재입니다. 이때 그 파일은 old로 두고 split_recommended=true,
   issues에 "다른 곡"이라고 적습니다. 새 파일로 바꾸면 기존 곡 차트가 다른 소리를 냅니다.
   길이가 같아도 상관계수가 낮으면 리믹스·다른 믹스입니다. old로 두더라도 issues에 꼭 적어 관리자가 알게 합니다.
2. 같은 소재(상관계수 높음)면 quality.better 쪽을 고릅니다. same이면 old.
3. 싱크만 다르면(offset_ms가 크고 aligned_correlation이 높음): 그 키음을 쓰는 차트(used_by_old/used_by_new)가 어느 쪽 싱크에
   맞춰졌는지가 기준입니다. 한쪽 차트만 쓰면 그쪽, 양쪽이 다 쓰면 unsure.
4. 차트 충돌: header_changes와 diff를 봅니다. #WAV 번호가 다른 파일을 가리키게 바뀌었으면(wav_id_remapped) 노래가 이상해질 수 있으니
   old, issues에 적습니다. 노트 수정·오타 수정처럼 같은 차트의 개정판이면 new. conflict_sounds_old/new는 그 차트가 쓰는
   충돌 키음입니다. 차트를 new로 하면서 그 키음을 old로 두면 소리가 어긋날 수 있으니 같이 봅니다.
5. 이미지·영상: 내용이 같고 품질만 다르면 품질 좋은 쪽, 내용이 다르면(다른 그림·다른 영상) unsure.
6. 파일을 풀지 못하거나(error) 한쪽이 깨졌으면 정상인 쪽, 둘 다 이상하면 unsure.
7. 확신이 없으면 unsure. 틀린 old/new보다 unsure가 낫습니다. confidence는 0~1, reason은 한국어 한 문장.

---
등록된 곡 정리(중복·섞임). 위 충돌 판단과 따로입니다. split_song·merge_songs는 바로 적용되니 확실할 때만 부릅니다.

순서: list_song_findings → get_song(관련 곡) → 필요하면 compare_songs → get_compare_result(끝날 때까지 몇 초 간격으로) →
split_song / merge_songs / 아니면 set_finding_status(dismissed).
- mixed(곡 하나에 렌더가 다른 차트 묶음이 여럿): groups는 렌더가 같은 차트끼리 묶은 것입니다. 차트의 제목·아티스트(get_song)가
  묶음끼리 다르면 다른 곡이 섞인 것이니, 원래 곡이 아닌 묶음(보통 작은 쪽, 제목이 폴더명과 다른 쪽)마다 split_song 합니다.
  같은 곡의 다른 버전(롱 버전·리믹스·기믹 차트)도 키음이 다르면 다른 곡으로 봅니다. 제목·아티스트가 같고 난이도 표기만 다르며
  키음 수가 비슷하면(렌더만 조금 다른 같은 곡) 나누지 말고 dismissed로 둡니다. 애매하면 아무것도 하지 말고 남깁니다.
- dup(두 곡이 같은 곡): get_song으로 두 곡 제목·아티스트가 같은 곡인지 보고, 맞으면 차트가 많은 쪽(같으면 번호가 작은 쪽)에
  다른 쪽을 merge_songs 합니다. 같은 이름에 내용이 다른 파일이 있으면 서버가 합칠 곡을 하위 폴더에 넣으므로 소리는 바뀌지 않습니다.
- 나누거나 합친 곡의 발견은 저절로 닫히고 다음 색인 때 다시 검사합니다. 처리하지 않을 발견만 set_finding_status로 닫습니다.
- 방향: 살릴 수 있는 것만 살리고 나머지는 지웁니다. 곡 상태는 사용 → 제외(delete_song level unused, 복원 가능) →
  삭제(level deleted, 복원 불가) 3단계입니다.
- split_song은 먼저 preview_split(또는 dry_run=true)으로 봅니다. shared_only=false(그 묶음만 쓰는 키음이 있음)면 살릴 수 있으니 split_song 합니다.
  shared_only=true(옮길 차트만 쓰는 키음이 하나도 없음)는 두 경우가 있습니다. 먼저 제목·아티스트를 원래 곡(폴더명·큰 묶음)과
  비교합니다.
  (a) 제목·아티스트가 같은 곡 계열(같은 제목에 난이도·버전 표기만 다름, 같은 아티스트, 피아노 솔로·arr·remix 표기 등 같은 곡의
      편곡)이면 같은 키음 세트로 만든 변형 차트라 소리가 맞습니다. 나누지도 빼지도 말고 set_finding_status(dismissed)로 닫습니다.
  (b) 제목이 다른 곡(다른 제목, 보통 다른 아티스트)이면 다른 곡 차트가 이 곡의 같은 이름 키음(01.wav 등)으로 재생되던 경우라
      소리가 틀립니다. remove_charts로 빼고 reason에 "다른 곡 키음으로 재생(원본 필요)"과 제목을 적습니다.
  제목이 달라도 같은 키음 세트로 만든 다른 곡(같은 작가의 키음 공유 곡)일 수 있습니다. stage에서 shared_only로 뺀 차트의 절반이
  이런 정상 곡이었습니다. 제목만으로 (b)가 확실하지 않으면 빼지 말고 split_song으로 새 곡으로 살리거나 발견을 열어 둡니다.
  애매하면(제목이 비었거나 판단이 서지 않음) 빼지 말고 발견을 열어 둡니다. 잘못 뺐으면 restore_charts로 되돌립니다.
- 원래 곡(폴더명과 맞는 묶음) 자체가 깨졌으면(키음이 거의 없음, 차트를 읽을 수 없음) delete_song level unused로 제외합니다.
  level deleted는 이미 제외한 곡을 지울 때만 씁니다.
- 키음 팩 폴더에 여러 사람이 각자 BMS를 넣은 곡(모든 묶음이 같은 키음을 나눠 씀, 서로 제목이 다름)은 원래 곡 하나로 정할 수 없으니
  건드리지 말고 발견을 열어 둔 채 "키음 팩"으로 보고합니다(임포트 때 정리할 예정).
- 발견 목록은 list_song_findings brief=true로 20~30개씩 훑고, 필요한 곡만 get_song으로 봅니다(전체 목록은 매우 큽니다).
- 한 번에 많이 고치지 말고 발견 하나씩 처리하고, 처리한 결과(나눈 곡 번호 등)를 요약해 남깁니다.

임포트 대기 묶음 정리(관리자):
- list_import_stages → list_import_units(show로 나눠 보기) → 필요한 곡만 get_import_unit. 대상은 set_import_target으로 정하고
  등록은 commit_import(unit_ids로 정한 곡만, slow 기본)로 합니다. commit은 되돌리기 어려우니 묶음 하나·20곡 이하씩, get_import_job이
  done이 된 뒤 다음으로 넘어갑니다. 다른 commit이 돌고 있으면 409이니 기다립니다.
- 판정(suggestion.kind)별 기준. 렌더 일치율(render) 0.7 이상은 같은 곡, 0.2 이하는 다른 곡입니다.
  new: 겹치는 곡이 없음 → auto 그대로(새 곡).
  add: 차트가 이미 한 곡에 있음(같은 차트 재업로드) → auto 그대로(그 곡에 빠진 파일만 더함).
  similar: 이름·키음이 비슷한 후보. merge가 있으면 auto가 그 곡에 넣습니다. 후보의 render가 0.7 이상이면 그 곡 번호로,
    render가 0.2 이하이거나 render_blocked면 new로 정합니다(키음이 다르면 다른 곡). render가 없고 키음 충족률(sounds)만 높으면
    제목·아티스트가 같을 때만 그 곡으로, 아니면 new.
  ambiguous: 차트가 여러 곡에 흩어져 있음 → 대부분의 차트가 있는 곡이 폴더명과 맞으면 그 곡 번호로, 아니면 정하지 말고 보고.
  unused: 사용 중지(제외)한 곡의 차트가 있음 → 건드리지 않습니다(다시 넣지 않으려고 제외한 곡).
- failed(지난 commit 실패: 헤더 인코딩·파일 오류 등)는 사람이 내려받아 고치는 곡이니 대상을 바꾸지 말고 보고만 합니다.
  충돌(conflicts>0)은 위의 충돌 판단 흐름(get_conflict_case → submit_recommendation)으로 추천만 남깁니다.
- 키음 팩 폴더(여러 사람 BMS가 한 폴더, 차트마다 제목이 다름)는 대상을 정하지 말고 "키음 팩"으로 보고합니다.
- set_import_target과 commit_import에는 reason(판단 근거)을 적습니다.
"""

# admin: True면 관리자만 보고 부를 수 있습니다.
TOOLS = [
    {
        "name": "whoami",
        "description": "이 연결의 서버 주소(server)와 사용자(닉네임, 권한: user/admin).",
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
            "충돌 파일마다 비교 수치·품질·그 파일을 쓰는 차트. offset·limit으로 나눠 받습니다. "
            "곡 정보는 offset 0에만 있고, 비교 계산이 오래 걸리면 limit보다 적게 주므로 next_offset으로 이어 받습니다."
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
    {
        "name": "list_song_findings",
        "description": (
            "렌더 색인이 찾은 등록 곡 문제. kind dup: song_a·song_b가 같은 곡(score, chart/other_chart). "
            "kind mixed: song_a 안에 렌더가 다른 차트 묶음(groups, 차트 경로 목록)이 여럿."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["", "dup", "mixed"], "description": "없으면 둘 다"},
                "status": {"type": "string", "enum": ["open", "dismissed"], "description": "기본 open"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "description": "기본 50"},
                "offset": {"type": "integer", "minimum": 0},
                "brief": {
                    "type": "boolean",
                    "description": "true면 짧게: id·kind·곡 번호·폴더와 mixed 묶음마다 [차트 수, 첫 차트 경로]만. 목록을 훑을 때 씁니다.",
                },
            },
        },
        "admin": True,
    },
    {
        "name": "get_song",
        "description": (
            "등록된 곡 정보: 폴더명, 차트마다 경로·제목·아티스트·키음 수, 파일 수와 크기. 응답을 짧게 하려고 같은 제목·아티스트는 "
            "titles 표로 묶고 차트는 그 번호(t)만 줍니다. sha256·BPM·장르·차트 아닌 파일 목록은 detail=true일 때만."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "song_id": {"type": "integer"},
                "detail": {"type": "boolean", "description": "sha256·bpm·genre·other_files까지(기본 false)"},
            },
            "required": ["song_id"],
        },
        "admin": True,
    },
    {
        "name": "compare_songs",
        "description": (
            "곡들의 차트를 서버에서 렌더해 같은 곡끼리 묶는 작업을 시작합니다(곡 50개·차트 5000개까지). "
            "job_id를 get_compare_result로 봅니다."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "songs": {"type": "array", "items": {"type": "integer"}},
                "charts": {
                    "type": "object",
                    "description": "곡 번호(문자열) → 볼 차트 경로들. 없는 곡은 모든 차트",
                    "additionalProperties": {"type": "array", "items": {"type": "string"}},
                },
            },
            "required": ["songs"],
        },
        "admin": True,
    },
    {
        "name": "get_compare_result",
        "description": "compare_songs 작업의 진행(done/total)과 결과(groups: 같은 곡 묶음, unrendered).",
        "inputSchema": {
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
        },
        "admin": True,
    },
    {
        "name": "split_song",
        "description": (
            "곡에서 차트 몇 개를 새 곡으로 나눕니다(바로 적용). 그 차트가 쓰는 키음·BGA·이미지를 같이 넣고, 남는 차트도 쓰는 파일은 "
            "양쪽에 둡니다. 모든 차트는 나눌 수 없습니다."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "song_id": {"type": "integer"},
                "charts": {"type": "array", "items": {"type": "string"}, "description": "차트 경로나 sha256"},
                "folder": {"type": "string", "description": "새 곡 폴더명(보통 '[아티스트] 제목'). 없으면 원래 곡 폴더명"},
                "dry_run": {"type": "boolean", "description": "true면 바꾸지 않고 옮길·복사할 파일과 shared_only만 봅니다"},
            },
            "required": ["song_id", "charts"],
        },
        "admin": True,
    },
    {
        "name": "preview_split",
        "description": (
            "split_song을 바꾸지 않고 미리 봅니다(split_song dry_run과 같음). 옮길·복사할 파일과 shared_only. "
            "판단만 하는 에이전트는 split_song 대신 이 도구를 쓰고 split_song은 막아 둡니다."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "song_id": {"type": "integer"},
                "charts": {"type": "array", "items": {"type": "string"}, "description": "차트 경로나 sha256"},
                "folder": {"type": "string"},
            },
            "required": ["song_id", "charts"],
        },
        "admin": True,
    },
    {
        "name": "merge_songs",
        "description": (
            "source 곡을 song_id 곡에 합치고 source를 사용 중지합니다(바로 적용). 같은 경로에 내용이 다른 파일이 있으면 "
            "source 곡 전체를 하위 폴더에 넣습니다."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "song_id": {"type": "integer", "description": "남길 곡"},
                "source": {"type": "integer", "description": "합치고 사용 중지할 곡"},
                "reason": {"type": "string"},
            },
            "required": ["song_id", "source"],
        },
        "admin": True,
    },
    {
        "name": "remove_charts",
        "description": (
            "곡에서 차트 몇 개를 뺍니다(바로 적용). 소리가 틀려 살릴 수 없는 차트(split_song dry_run의 shared_only)를 지울 때 씁니다. "
            "곡 zip에서 차트 파일을 빼고, 같은 차트를 나중에 원본으로 다시 임포트하면 새 곡으로 들어옵니다. 모든 차트는 뺄 수 없습니다."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "song_id": {"type": "integer"},
                "charts": {"type": "array", "items": {"type": "string"}, "description": "차트 경로나 sha256"},
                "reason": {"type": "string"},
            },
            "required": ["song_id", "charts", "reason"],
        },
        "admin": True,
    },
    {
        "name": "restore_charts",
        "description": (
            "remove_charts로 뺀 차트를 원본 없이 다시 붙입니다(바로 적용). 차트 저장소에 남은 내용을 원래 경로로 곡 zip에 넣습니다. "
            "키음은 그 곡에 남아 있어야 합니다. 잘못 뺀 차트를 되돌릴 때 씁니다."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "song_id": {"type": "integer"},
                "charts": {"type": "array", "items": {"type": "string"}, "description": "차트 sha256(remove_charts 결과의 detached)"},
                "reason": {"type": "string"},
            },
            "required": ["song_id", "charts", "reason"],
        },
        "admin": True,
    },
    {
        "name": "delete_song",
        "description": (
            "곡 전체를 내립니다(바로 적용). level unused(제외): 파일을 보관하고 같은 차트 재임포트를 막으며 복원할 수 있음. "
            "level deleted(삭제): 보관 파일까지 지워 복원할 수 없고 재임포트는 막지 않음."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "song_id": {"type": "integer"},
                "level": {"type": "string", "enum": ["unused", "deleted"], "description": "기본 unused"},
                "reason": {"type": "string"},
            },
            "required": ["song_id", "reason"],
        },
        "admin": True,
    },
    {
        "name": "set_finding_status",
        "description": "발견을 닫거나(dismissed: 문제 아님, 다시 띄우지 않음) 다시 엽니다(open).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "finding_id": {"type": "integer"},
                "status": {"type": "string", "enum": ["open", "dismissed"]},
            },
            "required": ["finding_id", "status"],
        },
        "admin": True,
    },
    {
        "name": "list_import_stages",
        "description": "임포트 대기 묶음 목록: id, 이름, 상태, 곡·차트 수, 판정별 곡 수(new/add/ambiguous/similar/unused/changed/failed).",
        "inputSchema": {"type": "object", "properties": {}},
        "admin": True,
    },
    {
        "name": "list_import_units",
        "description": (
            "대기 묶음의 곡을 짧게 한 쪽(50곡)씩: id, 이름, 파일·차트 수, 대상(target), 판정(kind)과 후보 곡(번호·폴더·"
            "메타 일치·키음 충족률·렌더 일치율), 지난 commit 결과·오류. show로 new/add/ambiguous/similar/unused/changed/failed만."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "stage_id": {"type": "string"},
                "show": {"type": "string", "enum": ["", "new", "add", "ambiguous", "similar", "unused", "changed", "failed"]},
                "page": {"type": "integer", "minimum": 1},
                "q": {"type": "string", "description": "폴더·차트 제목·아티스트 검색"},
            },
            "required": ["stage_id"],
        },
        "admin": True,
    },
    {
        "name": "get_import_unit",
        "description": "대기 곡 하나: 차트마다 경로·제목·아티스트·차트별 대상·이미 등록된 곡 번호, 판정 전체, 지난 commit 결과.",
        "inputSchema": {
            "type": "object",
            "properties": {"stage_id": {"type": "string"}, "unit_id": {"type": "string"}},
            "required": ["stage_id", "unit_id"],
        },
        "admin": True,
    },
    {
        "name": "set_import_target",
        "description": (
            "대기 곡의 대상을 정합니다(바로 저장, 등록은 commit_import 때). target: auto(판정대로)/new(새 곡)/skip(넣지 않음)/곡 번호(그 곡에 합침). "
            "charts로 차트별 대상({경로: 대상 또는 null=곡의 대상})도 정할 수 있습니다."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "stage_id": {"type": "string"},
                "unit_id": {"type": "string"},
                "target": {"type": ["string", "integer"]},
                "charts": {"type": "object"},
                "reason": {"type": "string", "description": "판단 근거(기록에 남음)"},
            },
            "required": ["stage_id", "unit_id", "target"],
        },
        "admin": True,
    },
    {
        "name": "commit_import",
        "description": (
            "대기 묶음을 정한 대상대로 등록하는 작업을 시작합니다(되돌리기 어려움). unit_ids를 주면 그 곡만 넣습니다. "
            "작업 id를 돌려주며 get_import_job으로 진행을 봅니다. 다른 임포트·정리 작업과 동시에 돌지 않게 하나씩 합니다."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "stage_id": {"type": "string"},
                "unit_ids": {"type": "array", "items": {"type": "string"}},
                "slow": {"type": "boolean", "description": "서비스에 디스크를 양보하며 천천히(기본 true)"},
                "reason": {"type": "string"},
            },
            "required": ["stage_id"],
        },
        "admin": True,
    },
    {
        "name": "get_import_job",
        "description": "임포트 작업 진행: 상태(running/done/failed), 전체·끝난 곡 수, 오류. job_id가 없으면 최근 작업들.",
        "inputSchema": {"type": "object", "properties": {"job_id": {"type": "string"}}},
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


def _compact(value):
    """값이 없는 항목(None, 빈 목록·사전)을 뺍니다."""
    if isinstance(value, dict):
        return {k: _compact(v) for k, v in value.items() if v is not None and v != [] and v != {}}
    return value


def get_conflict_case(stage_id: str, unit_id: str, offset: int = 0, limit: int = CASE_LIMIT) -> dict:
    started = time.monotonic()
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
        if out_items and time.monotonic() - started > CASE_SECONDS:
            break
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
        facts = _facts(stage, unit, item)
        facts.pop("min_correlation", None)
        rec["compare"] = facts
        out_items.append(_compact(rec))

    done = offset + len(out_items)
    result = {
        "stage_id": stage_id,
        "unit_id": unit_id,
        "folder": unit["folder"],
        "total": len(all_items),
        "offset": offset,
        "next_offset": done if done < len(all_items) else None,
        "items": out_items,
    }
    if offset:
        return result
    # 곡 정보는 첫 페이지에만 넣습니다(페이지마다 같은 내용이라).
    result.update({
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
        "min_correlation": media_match.SOUND_MIN_CORRELATION,
    })
    return result


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
    return {
        "server": constant.PUBLIC_URL,
        "id": user.id,
        "nickname": user.display_name,
        "role": "admin" if user.is_admin else "user",
    }


# ---- 등록된 곡 정리 ----

SONG_OTHER_FILES = 100


def _http(fn, *args):
    """관리자 API 함수의 HTTPException을 도구 오류로 바꿉니다."""
    try:
        return fn(*args)
    except HTTPException as e:
        raise ToolError(f"{e.status_code}: {e.detail}") from None


def list_song_findings(kind: str = "", status: str = "open", limit: int = 50, offset: int = 0, brief: bool = False) -> dict:
    from . import render_index

    if kind not in ("", "dup", "mixed") or status not in ("open", "dismissed"):
        raise ToolError("kind는 dup·mixed, status는 open·dismissed입니다.")
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    items = render_index.findings(status, kind, limit + 1, offset)
    more = len(items) > limit
    items = items[:limit]
    if brief:
        items = [_brief_finding(f) for f in items]
    return {
        "findings": items,
        "total_open": render_index.count_findings("open"),
        "next_offset": offset + limit if more else None,
    }


def _brief_finding(f: dict) -> dict:
    out = {"id": f["id"], "kind": f["kind"], "song_a": f["song_a"], "folder_a": f["folder_a"]}
    if f["kind"] == "dup":
        out.update(song_b=f["song_b"], folder_b=f["folder_b"], score=f["score"])
    else:
        out["groups"] = [[len(g), g[0] if g else None] for g in f["detail"].get("groups", [])]
    return out


def get_song(song_id: int, detail: bool = False) -> dict:
    song = _http(admin.get_song_detail, int(song_id))
    with db.connect() as con, con.cursor() as cur:
        cur.execute("SELECT files FROM song WHERE id = %s", (song["id"],))
        row = cur.fetchone()
        entries = {e["path"]: e for e in json.loads(row[0] or "[]")} if row else {}
        database = db.Database()
        charts = []
        for chart in song["charts"]:
            item = {"sha256": chart["sha256"], "path": chart["path"]}
            entry = entries.get(chart["path"])
            try:
                content = database._read_song_member(cur, song["id"], entry) if entry else None
            except ValueError:
                content = None
            if content is not None and hashlib.sha256(content).hexdigest() == chart["sha256"]:
                try:
                    item.update(db.chart_meta(content))
                except db.ChartEncodingError as e:
                    item["meta_error"] = str(e)
                item["sounds"] = len(db.chart_sounds(content, posixpath.dirname(chart["path"])))
            else:
                item["meta_error"] = "곡 zip에 차트 파일이 없습니다(차트 청크에만 있음)."
            charts.append(item)
    others = [f["path"] for f in song["files"] if "chart" not in f]
    out = {"id": song["id"], "folder": song["folder"], "size": song["size"], "file_count": len(song["files"])}
    if detail:
        out.update(charts=charts, other_files=others[:SONG_OTHER_FILES], other_files_more=max(0, len(others) - SONG_OTHER_FILES))
        return out
    # 짧은 응답: 제목·아티스트 표 + 차트마다 [경로, 표 번호, 키음 수]. agy 등은 긴 응답을 파일로 빼서 셸로 읽으려다 멈춥니다.
    titles: list[list[str]] = []
    index: dict[tuple[str, str], int] = {}
    rows = []
    for c in charts:
        key = (c.get("title", ""), c.get("artist", ""))
        if key not in index:
            index[key] = len(titles)
            titles.append(list(key))
        rows.append([c["path"], index[key], c.get("sounds")] + ([c["meta_error"]] if "meta_error" in c else []))
    out.update(titles=titles, charts=rows, chart_format="[path, t(titles 번호), sounds(키음 수), (meta_error)]")
    return out


def compare_songs(songs: list[int], charts: dict | None = None) -> dict:
    try:
        job = render_check.start([int(s) for s in songs], {int(k): list(v) for k, v in (charts or {}).items()})
    except LookupError as e:
        raise ToolError(str(e)) from None
    return {"job_id": job.id, "total": job.total}


def get_compare_result(job_id: str) -> dict:
    job = importer.get_job(job_id)
    if job is None or job.kind != "render":
        raise ToolError("job not found")
    j = job.json()
    return {k: j[k] for k in ("id", "status", "total", "done", "error", "result")}


def split_song(user, song_id: int, charts: list[str], folder: str = "", dry_run: bool = False) -> dict:
    return _http(admin.split_song, int(song_id), list(charts), folder, user.id, bool(dry_run))


def preview_split(song_id: int, charts: list[str], folder: str = "") -> dict:
    return _http(admin.split_song, int(song_id), list(charts), folder, "", True)


def merge_songs(user, song_id: int, source: int, reason: str = "") -> dict:
    return _http(admin.merge_songs, int(song_id), int(source), reason, user.id)


def remove_charts(user, song_id: int, charts: list[str], reason: str = "") -> dict:
    detail = _http(admin.get_song_detail, int(song_id))
    by_path = {c["path"].lower(): c["sha256"] for c in detail["charts"]}
    shas = {c["sha256"] for c in detail["charts"]}
    picked = [c if c in shas else by_path.get(str(c).lower(), str(c)) for c in charts]
    body = admin.ChartDetach(charts=picked, reason=reason)
    from . import render_index

    result = _http(admin.detach_charts, int(song_id), body, user)
    render_index.forget_songs([int(song_id)])
    return result


def restore_charts(user, song_id: int, charts: list[str], reason: str = "") -> dict:
    return _http(admin.attach_charts, int(song_id), [str(c) for c in charts], reason, user.id)


def delete_song(user, song_id: int, reason: str = "", level: str = "unused") -> dict:
    if level not in ("unused", "deleted"):
        raise ToolError("level은 unused(제외)·deleted(삭제)입니다.")
    if level == "deleted":
        return _http(admin.purge_song, int(song_id), reason, user.id)
    from . import render_index

    result = _http(admin.delete_song, int(song_id), user, admin.SongDelete(reason=reason))
    render_index.forget_songs([int(song_id)])
    return {**result, "song_id": int(song_id), "level": "unused"}


def set_finding_status(finding_id: int, status: str) -> dict:
    from . import render_index

    if not render_index.set_status(int(finding_id), status):
        raise ToolError("finding not found")
    return {"id": int(finding_id), "status": status}



# ---- 임포트 대기 묶음 정리 ----

IMPORT_UNIT_FIELDS = ("id", "name", "files", "target", "result", "error")


def _brief_suggestion(suggestion: dict | None, folders: dict) -> dict | None:
    if not suggestion:
        return None
    out = {"kind": suggestion.get("kind")}
    songs = suggestion.get("songs") or []
    if songs:
        out["candidates"] = [
            {
                "song": s,
                "folder": folders.get(s),
                "matches": (suggestion.get("matches") or {}).get(str(s)),
                "sounds": (suggestion.get("sounds") or {}).get(str(s)),
                "render": (suggestion.get("render") or {}).get(str(s)),
            }
            for s in songs[:5]
        ]
    for key in ("complete", "render_partial", "render_blocked", "deleted", "merge"):
        if suggestion.get(key):
            out[key] = suggestion[key]
    return out


def list_import_stages() -> dict:
    return {"stages": staging.list_stages()}


def list_import_units(stage_id: str, show: str = "", page: int = 1, q: str = "") -> dict:
    data = staging.get_stage(stage_id, int(page), show, q)
    folders = {int(k): v for k, v in data["songs"].items()}
    items = []
    for unit in data["items"]:
        item = {k: unit.get(k) for k in IMPORT_UNIT_FIELDS if unit.get(k) is not None}
        item["charts"] = len(unit["charts"])
        item["suggestion"] = _brief_suggestion(unit.get("suggestion"), folders)
        if isinstance(unit.get("target"), int):
            item["target_folder"] = folders.get(unit["target"])
        if any(c.get("target") is not None for c in unit["charts"]):
            item["chart_targets"] = True
        items.append(item)
    return {"stage": data["name"], "counts": data["counts"], "page": data["page"], "pages": data["pages"], "total": data["total"],
            "units": items}


def get_import_unit(stage_id: str, unit_id: str) -> dict:
    with staging._lock:
        stage = staging._load(stage_id)
    unit = next((u for u in stage["units"] if u["id"] == unit_id), None)
    if unit is None:
        raise ToolError("곡이 없습니다(이미 등록됐거나 버린 곡).")
    suggestion = unit.get("suggestion") or {}
    ids = set(suggestion.get("songs") or []) | {t for t in [unit["target"], *(c.get("target") for c in unit["charts"])] if isinstance(t, int)}
    folders = staging._song_folders(ids)
    return {
        "id": unit["id"],
        "name": unit["name"],
        "folder": unit["folder"],
        "files": unit["files"],
        "size": unit["size"],
        "target": unit["target"],
        "suggestion": _brief_suggestion(suggestion, folders),
        "charts": [
            {k: c.get(k) for k in ("path", "title", "artist", "bpm", "target", "song_id", "deleted") if c.get(k) is not None}
            for c in unit["charts"]
        ],
        "result": unit.get("result"),
        "error": unit.get("error"),
        "conflicts": len(conflicts._conflict_paths(unit)),
    }


def set_import_target(user, stage_id: str, unit_id: str, target, charts: dict | None = None, reason: str = "") -> dict:
    return staging.set_target(stage_id, unit_id, target, charts)


def commit_import(user, stage_id: str, unit_ids: list[str] | None = None, slow: bool = True, reason: str = "") -> dict:
    job = staging.start_commit(stage_id, bool(slow), list(unit_ids) if unit_ids else None)
    logger.info(f"import commit {job.id} by {user.id} (mcp): stage {stage_id} {job.name}")
    return {"job_id": job.id, "name": job.name, "status": job.status}


def get_import_job(job_id: str = "") -> dict:
    if job_id:
        job = importer.get_job(job_id)
        if job is None:
            raise ToolError("작업이 없습니다(서버가 다시 시작됐으면 묶음 상태로 확인).")
        jobs = [job]
    else:
        jobs = importer.list_jobs()[:10]
    return {"jobs": [{k: v for k, v in j.json().items() if k != "songs"} for j in jobs]}


# 관리자 도구(whoami는 사용자를 받으므로 _call에서 따로 부릅니다)
HANDLERS = {
    "list_conflict_units": list_conflict_units,
    "get_conflict_case": get_conflict_case,
    "get_file_diff": get_file_diff,
    "get_sound_clip": get_sound_clip,
    "submit_recommendation": submit_recommendation,
    "list_song_findings": list_song_findings,
    "get_song": get_song,
    "compare_songs": compare_songs,
    "get_compare_result": get_compare_result,
    "preview_split": preview_split,
    "set_finding_status": set_finding_status,
    "list_import_stages": list_import_stages,
    "list_import_units": list_import_units,
    "get_import_unit": get_import_unit,
    "get_import_job": get_import_job,
}
# 부른 사용자를 첫 인자로 받는 도구(바로 적용하는 도구, 기록에 남김)
USER_HANDLERS = {
    "split_song": split_song,
    "merge_songs": merge_songs,
    "remove_charts": remove_charts,
    "restore_charts": restore_charts,
    "delete_song": delete_song,
    "set_import_target": set_import_target,
    "commit_import": commit_import,
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
        if name == "whoami":
            value = whoami(user)
        elif name in USER_HANDLERS:
            value = USER_HANDLERS[name](user, **args)
        else:
            value = HANDLERS[name](**args)
    except TypeError as e:
        return {"content": [{"type": "text", "text": f"인자가 맞지 않습니다: {e}"}], "isError": True}
    except (ToolError, StageError, ValueError) as e:
        message = e.detail if isinstance(e, StageError) else str(e)
        return {"content": [{"type": "text", "text": str(message)}], "isError": True}
    if isinstance(value, Content):
        return {"content": list(value)}
    if name in USER_HANDLERS and not args.get("dry_run"):
        # 무엇을 왜 바꿨는지 나중에 되짚을 수 있게 결과 요약과 사유를 남깁니다.
        detail = {k: value[k] for k in ("song_id", "from", "merged_from", "level", "purged", "subfolder") if value.get(k) is not None}
        for k in ("charts", "detached", "attached", "removed_files", "added_files", "moved_files"):
            if value.get(k):
                detail[k] = ",".join(map(str, value[k]))
        if args.get("reason"):
            detail["reason"] = args["reason"]
        if name in ("set_import_target", "commit_import"):
            detail.update({("to" if k == "target" else k): str(args[k]) for k in ("target", "unit_ids", "slow") if k in args})
            if value.get("job_id"):
                detail["job"] = value["job_id"]
            target = f"{args.get('stage_id')}/{args.get('unit_id', '')}"
        else:
            target = str(args.get("song_id"))
        audit.record(request, user.id, f"mcp.{name}", target, **detail)
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
