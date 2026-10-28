"""다운로드 API. 사전(/api/pre/*)과 플레이(/api/play/*) 두 갈래로, 둘 다 로그인이 필요합니다.

- 사전: 차트 청크, 매니페스트, 사전 청크(곡들의 배너·스테이지파일·프리뷰 등 묶음), 곡의 사전 파일 하나.
  보낼 바이트를 요청 때 이번 달 사용량에 더하고, 한도를 넘었으면 연결마다 pre_throttled_kbps로 보냅니다.
- 플레이: 곡 zip 전체. 곡 1개당 티켓 1개(grant_seconds 동안 같은 곡은 재차감 없음), 티켓이 없으면 429.

캐싱 리버스 프록시(proxy.py) 뒤에서는 차트 청크·사전 청크·곡 zip의 인증·차감을 /api/proxy/authz가 같은 규칙으로 하고,
이 경로들은 프록시의 캐시 채우기 요청(사용자 없음)에 본문만 내려줍니다.
"""
import asyncio
import gzip
import mimetypes
import re
import struct
import zipfile
import zlib
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response, StreamingResponse

from . import constant, download_log, proxy, quota
from .accounts import User
from .auth import current_user, optional_api_session
from .db import Database, StoreFile

router = APIRouter()

_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")
# 감속할 때 한 번에 보내는 크기
THROTTLE_PIECE = 16 * 1024


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """단일 bytes range만 해석해 (start, end)를 반환합니다(양끝 포함).

    헤더가 없거나 해석할 수 없는 형식(여러 범위 등)이면 None을 반환해 전체를 보냅니다.
    만족할 수 없는 범위면 ValueError를 냅니다.
    """
    if not header:
        return None
    m = _RANGE_RE.fullmatch(header.strip())
    if m is None:
        return None
    first, last = m.groups()
    if first:
        start = int(first)
        if last and int(last) < start:
            return None
        end = min(int(last), size - 1) if last else size - 1
    elif last:
        if int(last) == 0:
            raise ValueError(header)
        start, end = max(size - int(last), 0), size - 1
    else:
        return None
    if start >= size:
        raise ValueError(header)
    return start, end


def _file_chunks(file: StoreFile, start: int, end: int) -> Iterator[bytes]:
    """[start, end] 바이트(양끝 포함)를 READ_SIZE씩 읽습니다. 끝나거나 닫히면 파일을 닫습니다."""
    try:
        yield from file.iter_range(start, end)
    finally:
        file.close()


async def send(chunks: Iterator[bytes], rate: int | None = None) -> AsyncIterator[bytes]:
    """동기 iterator(파일 읽기)의 조각을 스레드에서 꺼내 보냅니다. rate(바이트/초)를 주면 그 속도로 나눠 보냅니다.
    끝나거나 연결이 끊기면 chunks를 닫습니다(본문을 시작하기 전에 끊기면 열어 둔 파일은 StoreFile이 GC 때 닫음)."""
    try:
        while (chunk := await run_in_threadpool(next, chunks, None)) is not None:
            if not rate:
                yield chunk
                continue
            for i in range(0, len(chunk), THROTTLE_PIECE):
                piece = chunk[i : i + THROTTLE_PIECE]
                yield piece
                await asyncio.sleep(len(piece) / rate)
    finally:
        close = getattr(chunks, "close", None)
        if close is not None:
            close()


def _not_modified(request: Request, etag: str) -> bool:
    if_none_match = request.headers.get("if-none-match")
    return bool(if_none_match) and (
        if_none_match.strip() == "*" or etag in [t.strip() for t in if_none_match.split(",")]
    )


def download_user(
    request: Request, session: Annotated[tuple[User, int, str] | None, Depends(optional_api_session)]
) -> User | None:
    """캐시되는 다운로드의 사용자. 프록시의 캐시 채우기 요청이면 None(인증·차감은 /api/proxy/authz에서 끝남)."""
    if proxy.from_proxy(request):
        return None
    if session is None:
        raise HTTPException(status_code=401, detail="login required", headers={"WWW-Authenticate": "Bearer"})
    return session[0]


def file_response(
    request: Request,
    table: str,
    row_id: int,
    filename: str,
    detail: str,
    *,
    charge: Callable[[], None] | None = None,
    meter_user: str | None = None,
    expect_sha256: str | None = None,
) -> Response:
    """저장소 파일을 내려줍니다. Range(단일 범위), If-Range, If-None-Match를 지원하고 ETag는 sha256입니다.

    charge: 본문(200/206)을 보내기로 정한 뒤 부릅니다(티켓 차감). HTTPException을 내면 그대로 응답합니다.
    meter_user: 주면 보낼 바이트를 그 사용자의 사전 다운로드 사용량에 더하고, 한도를 넘었으면 감속합니다.
    expect_sha256: 주면 내용의 sha256이 다를 때 409를 냅니다(프록시가 옛 키로 새 내용을 캐시하지 않도록).
    """
    file = Database().open_file(table, row_id)
    if file is None:
        status, detail = proxy.missing_song(row_id) if table == "song" else (404, detail)
        raise HTTPException(status_code=status, detail=detail)
    try:
        if expect_sha256 and expect_sha256 != file.sha256:
            raise HTTPException(status_code=409, detail="content changed. retry.")
        etag = f'"{file.sha256}"'
        headers = {"Accept-Ranges": "bytes", "ETag": etag, "X-Content-SHA256": file.sha256}
        if _not_modified(request, etag):
            file.close()
            return Response(status_code=304, headers=headers)

        rng = None
        if_range = request.headers.get("if-range")
        if if_range is None or if_range.strip() == etag:
            try:
                rng = parse_range(request.headers.get("range"), file.size)
            except ValueError:
                raise HTTPException(
                    status_code=416,
                    detail="Range not satisfiable",
                    headers={**headers, "Content-Range": f"bytes */{file.size}"},
                )
        headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        if rng is None:
            start, end, status = 0, file.size - 1, 200
        else:
            start, end = rng
            status = 206
            headers["Content-Range"] = f"bytes {start}-{end}/{file.size}"
        headers["Content-Length"] = str(end - start + 1)
        if charge is not None:
            charge()
        rate = quota.meter_pre(meter_user, end - start + 1) if meter_user is not None else None
    except BaseException:
        file.close()
        raise
    return StreamingResponse(
        send(_file_chunks(file, start, end), rate), status_code=status, media_type="application/zip", headers=headers
    )


def manifest_response(request: Request, chunk_id: int, meter_user: str | None = None) -> Response:
    data = Database().get_manifest_chunk(chunk_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Manifest not found")
    headers = {"Vary": "Accept-Encoding"}
    if "gzip" in request.headers.get("accept-encoding", ""):
        headers["Content-Encoding"] = "gzip"
    else:
        data = gzip.decompress(data)
    if meter_user is None:
        return Response(data, media_type="application/json", headers=headers)
    headers["Content-Length"] = str(len(data))
    rate = quota.meter_pre(meter_user, len(data))
    return StreamingResponse(send(iter([data]), rate), media_type="application/json", headers=headers)


# ---- 사전 다운로드 ----

@router.get("/api/pre/charthash")
def pre_chart_hash(user: Annotated[User, Depends(current_user)]):
    return Database().get_chart_chunk_hash()


def _pre_file(request: Request, table: str, chunk_id: int, filename: str, user: User | None) -> Response:
    if user is None:
        return file_response(
            request, table, chunk_id, filename, "File not found",
            expect_sha256=request.headers.get(proxy.EXPECT_SHA256_HEADER),
        )
    return file_response(request, table, chunk_id, filename, "File not found", meter_user=user.id)


@router.get("/api/pre/chart/{chunk_id}")
def pre_chart_chunk(chunk_id: int, request: Request, user: Annotated[User | None, Depends(download_user)]):
    return _pre_file(request, "chart_chunk", chunk_id, constant.CHART_CHUNK_FILENAME_TEMPLATE.format(chunk_id), user)


@router.get("/api/pre/assethash")
def pre_asset_hash(user: Annotated[User, Depends(current_user)]):
    return Database().get_pre_chunk_hash()


@router.get("/api/pre/sizes")
def pre_chunk_sizes(user: Annotated[User, Depends(current_user)]):
    """차트·사전 청크별 SHA-256과 크기. 클라이언트가 동기화 전에 받을 용량을 보여 줄 때 씁니다."""
    return Database().get_chunk_sizes()


@router.get("/api/pre/asset/{chunk_id}")
def pre_asset_chunk(chunk_id: int, request: Request, user: Annotated[User | None, Depends(download_user)]):
    return _pre_file(request, "pre_chunk", chunk_id, constant.PRE_CHUNK_FILENAME_TEMPLATE.format(chunk_id), user)


@router.get("/api/pre/manifest/hash")
def pre_manifest_hash(user: Annotated[User, Depends(current_user)]):
    return Database().get_manifest_hash()


@router.get("/api/pre/manifest/{chunk_id}")
def pre_manifest(chunk_id: int, request: Request, user: Annotated[User, Depends(current_user)]):
    return manifest_response(request, chunk_id, meter_user=user.id)


def zip_member(file: StoreFile, entry: dict) -> tuple[int, object]:
    """곡 zip에서 항목 데이터가 시작하는 위치와 압축 해제기를 구합니다."""
    if entry["method"] == zipfile.ZIP_STORED:
        decompressor = None
    elif entry["method"] == zipfile.ZIP_DEFLATED:
        decompressor = zlib.decompressobj(-15)
    else:
        raise ValueError(f"unsupported compression: {entry['method']}")

    if "data_offset" in entry:
        return entry["data_offset"], decompressor

    head = file.read(entry["offset"], 30)
    if len(head) != 30 or head[:4] != b"PK\x03\x04":
        raise ValueError("bad local file header")
    name_len, extra_len = struct.unpack("<HH", head[26:30])
    return entry["offset"] + 30 + name_len + extra_len, decompressor


def member_chunks(file: StoreFile, start: int, comp_size: int, decompressor) -> Iterator[bytes]:
    """곡 zip 항목 하나를 압축을 풀며 읽습니다. 끝나거나 닫히면 파일을 닫습니다."""
    try:
        pos, end = start, start + comp_size
        while pos < end:
            n = min(constant.READ_SIZE, end - pos)
            raw = file.read(pos, n)
            pos += n
            out = decompressor.decompress(raw) if decompressor else raw
            if out:
                yield out
        if decompressor:
            tail = decompressor.flush()
            if tail:
                yield tail
    finally:
        file.close()


@router.get("/api/pre/song/{song_id}/file")
def pre_song_file(song_id: int, path: str, request: Request, user: Annotated[User, Depends(current_user)]):
    """곡의 사전 파일 하나를 압축을 풀어 내려줍니다. 사전 파일이 아니면 403."""
    files = Database().get_song_files(song_id)
    if files is None:
        status, detail = proxy.missing_song(song_id)
        raise HTTPException(status_code=status, detail=detail)
    entry = next((f for f in files if f["path"] == path), None)
    if entry is None:
        rendered = Database().get_preview(song_id, path)
        if rendered is None:
            raise HTTPException(status_code=404, detail="file not found")
        entry, data = rendered
        media_type = "audio/ogg"
    elif entry.get("kind") != "pre":
        raise HTTPException(status_code=403, detail="not a pre-download file")
    else:
        data = None
        media_type = mimetypes.guess_type(path)[0] or "application/octet-stream"

    etag = f'"{entry["crc32"]}-{entry["size"]}"'
    if _not_modified(request, etag):
        return Response(status_code=304, headers={"ETag": etag})
    headers = {"ETag": etag, "Content-Length": str(entry["size"])}
    if data is not None:
        # 서버가 만든 미리듣기(곡 zip에 없는 사전 파일)
        rate = quota.meter_pre(user.id, len(data))
        return StreamingResponse(send(iter([data]), rate), media_type=media_type, headers=headers)

    file = Database().open_file("song", song_id)
    if file is None:
        status, detail = proxy.missing_song(song_id)
        raise HTTPException(status_code=status, detail=detail)
    try:
        start, decompressor = zip_member(file, entry)
    except ValueError:
        file.close()
        raise HTTPException(status_code=500, detail="broken song file. report this to admin.")
    rate = quota.meter_pre(user.id, entry["size"])
    return StreamingResponse(
        send(member_chunks(file, start, entry["comp_size"], decompressor), rate), media_type=media_type, headers=headers
    )


# ---- 플레이 다운로드 ----

@router.get("/api/play/song/{song_id}")
def play_song(song_id: int, request: Request, user: Annotated[User | None, Depends(download_user)]):
    """곡 zip 전체. Range 이어받기를 지원하고, 본문을 보낼 때 티켓을 씁니다(304·416은 차감 없음)."""
    if user is None:
        return file_response(
            request, "song", song_id, f"{song_id}.zip", "song not found",
            expect_sha256=request.headers.get(proxy.EXPECT_SHA256_HEADER),
        )

    resumed = download_log.is_resume(request)

    def charge():
        try:
            charged = quota.charge_play(user.id, song_id)
        except quota.NoTicket as e:
            download_log.record(user.id, song_id, "no_ticket", resumed)
            raise HTTPException(
                status_code=429, detail="no download ticket", headers={"Retry-After": str(e.retry_after)}
            )
        download_log.record(user.id, song_id, "charged" if charged else "free", resumed)

    try:
        return file_response(request, "song", song_id, f"{song_id}.zip", "song not found", charge=charge)
    except HTTPException as e:
        result = download_log.failure_result(e.status_code)
        if result:
            download_log.record(user.id, song_id, result, resumed)
        raise
