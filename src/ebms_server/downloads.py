"""다운로드 API. 사전(/api/pre/*)과 플레이(/api/play/*) 두 갈래로, 둘 다 로그인이 필요합니다.

- 사전: 차트 청크, 매니페스트, 곡의 사전 파일(배너·스테이지파일·프리뷰 등). 이번 달 사용량에 더하고 한도를 넘으면 감속.
- 플레이: 곡 zip 전체. 곡 1개당 티켓 1개(grant_seconds 동안 같은 곡은 재차감 없음), 티켓이 없으면 429.
"""
import gzip
import mimetypes
import re
import struct
import zipfile
import zlib
from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from . import constant, quota
from .accounts import User
from .auth import current_user
from .db import BlobReader, Database

router = APIRouter()

_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


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


def _not_modified(request: Request, etag: str) -> bool:
    if_none_match = request.headers.get("if-none-match")
    return bool(if_none_match) and (
        if_none_match.strip() == "*" or etag in [t.strip() for t in if_none_match.split(",")]
    )


def blob_response(
    request: Request,
    table: str,
    row_id: int,
    filename: str,
    detail: str,
    *,
    user_id: str | None = None,
    before_send: Callable[[], None] | None = None,
    meter_user: str | None = None,
) -> Response:
    """BLOB을 내려줍니다. Range(단일 범위), If-Range, If-None-Match를 지원하고 ETag는 sha256입니다.

    user_id: 동시 다운로드 수를 제한할 사용자 ID.
    before_send: 본문(200/206)을 보내기로 정한 뒤 호출합니다(티켓 차감). HTTPException을 내면 그대로 응답합니다.
    meter_user: 주면 보낸 바이트를 그 사용자의 사전 다운로드 사용량에 더하고 한도를 넘으면 감속합니다.
    """
    if user_id and not quota.acquire_download_slot(user_id):
        raise HTTPException(status_code=429, detail="too many concurrent downloads")

    try:
        blob = Database().open_blob(table, row_id)
        if blob is None:
            raise HTTPException(status_code=404, detail=detail)
        etag = f'"{blob.sha256}"'
        headers = {
            "Accept-Ranges": "bytes",
            "ETag": etag,
            "X-Content-SHA256": blob.sha256,
        }

        if _not_modified(request, etag):
            blob.close()
            if user_id:
                quota.release_download_slot(user_id)
            return Response(status_code=304, headers=headers)

        rng = None
        if_range = request.headers.get("if-range")
        if if_range is None or if_range.strip() == etag:
            try:
                rng = parse_range(request.headers.get("range"), blob.size)
            except ValueError:
                blob.close()
                raise HTTPException(
                    status_code=416,
                    detail="Range not satisfiable",
                    headers={**headers, "Content-Range": f"bytes */{blob.size}"},
                )

        if before_send is not None:
            try:
                before_send()
            except BaseException:
                blob.close()
                raise

        headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        if rng is None:
            start, end, status = 0, blob.size - 1, 200
        else:
            start, end = rng
            status = 206
            headers["Content-Range"] = f"bytes {start}-{end}/{blob.size}"
        headers["Content-Length"] = str(end - start + 1)
        gen = blob.iter_range(start, end)

        def cleanup():
            try:
                if hasattr(gen, "close"):
                    gen.close()
            finally:
                blob.close()
                if user_id:
                    quota.release_download_slot(user_id)

        if meter_user is not None:
            body = quota.metered(meter_user, gen, close=cleanup)
        else:
            def wrapped_body():
                try:
                    yield from gen
                finally:
                    cleanup()
            body = wrapped_body()

        return StreamingResponse(body, status_code=status, media_type="application/zip", headers=headers)
    except Exception:
        if user_id:
            quota.release_download_slot(user_id)
        raise


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
    return StreamingResponse(
        quota.metered(meter_user, iter([data])), media_type="application/json", headers=headers
    )


# ---- 사전 다운로드 ----

@router.get("/api/pre/charthash")
def pre_chart_hash(user: Annotated[User, Depends(current_user)]):
    return Database().get_chart_chunk_hash()


@router.get("/api/pre/chart/{chunk_id}")
def pre_chart_chunk(chunk_id: int, request: Request, user: Annotated[User, Depends(current_user)]):
    return blob_response(
        request,
        "chart_chunk",
        chunk_id,
        constant.CHART_CHUNK_FILENAME_TEMPLATE.format(chunk_id),
        "File not found",
        user_id=user.id,
        meter_user=user.id,
    )


@router.get("/api/pre/manifest/hash")
def pre_manifest_hash(user: Annotated[User, Depends(current_user)]):
    return Database().get_manifest_hash()


@router.get("/api/pre/manifest/{chunk_id}")
def pre_manifest(chunk_id: int, request: Request, user: Annotated[User, Depends(current_user)]):
    return manifest_response(request, chunk_id, meter_user=user.id)


def _zip_member(blob: BlobReader, entry: dict) -> tuple[int, object]:
    """곡 zip에서 항목 데이터가 시작하는 위치와 압축 해제기를 구합니다."""
    if entry["method"] == zipfile.ZIP_STORED:
        decompressor = None
    elif entry["method"] == zipfile.ZIP_DEFLATED:
        decompressor = zlib.decompressobj(-15)
    else:
        raise ValueError(f"unsupported compression: {entry['method']}")

    if "data_offset" in entry:
        return entry["data_offset"], decompressor

    head = blob.read(entry["offset"], 30)
    if len(head) != 30 or head[:4] != b"PK\x03\x04":
        raise ValueError("bad local file header")
    name_len, extra_len = struct.unpack("<HH", head[26:30])
    return entry["offset"] + 30 + name_len + extra_len, decompressor


def _iter_member(blob: BlobReader, start: int, comp_size: int, decompressor):
    try:
        pos, end = start, start + comp_size
        while pos < end:
            n = min(constant.BLOB_READ_SIZE, end - pos)
            raw = blob.read(pos, n)
            pos += n
            out = decompressor.decompress(raw) if decompressor else raw
            if out:
                yield out
        if decompressor:
            tail = decompressor.flush()
            if tail:
                yield tail
    finally:
        blob.close()


@router.get("/api/pre/song/{song_id}/file")
def pre_song_file(song_id: int, path: str, request: Request, user: Annotated[User, Depends(current_user)]):
    """곡의 사전 파일 하나를 압축을 풀어 내려줍니다. 사전 파일이 아니면 403."""
    if not quota.acquire_download_slot(user.id):
        raise HTTPException(status_code=429, detail="too many concurrent downloads")

    try:
        files = Database().get_song_files(song_id)
        if files is None:
            raise HTTPException(status_code=404, detail="song not found")
        entry = next((f for f in files if f["path"] == path), None)
        if entry is None:
            raise HTTPException(status_code=404, detail="file not found")
        if entry.get("kind") != "pre":
            raise HTTPException(status_code=403, detail="not a pre-download file")

        etag = f'"{entry["crc32"]}-{entry["size"]}"'
        headers = {"ETag": etag, "Content-Length": str(entry["size"])}
        if _not_modified(request, etag):
            del headers["Content-Length"]
            quota.release_download_slot(user.id)
            return Response(status_code=304, headers=headers)

        blob = Database().open_blob("song", song_id)
        if blob is None:
            raise HTTPException(status_code=404, detail="song not found")
        try:
            start, decompressor = _zip_member(blob, entry)
        except ValueError:
            blob.close()
            raise HTTPException(status_code=500, detail="broken song file. report this to admin.")
        body = _iter_member(blob, start, entry["comp_size"], decompressor)
        media_type = mimetypes.guess_type(path)[0] or "application/octet-stream"

        def cleanup():
            try:
                body.close()
            finally:
                blob.close()
                quota.release_download_slot(user.id)

        return StreamingResponse(
            quota.metered(user.id, body, close=cleanup),
            media_type=media_type,
            headers=headers,
        )
    except Exception:
        quota.release_download_slot(user.id)
        raise


# ---- 플레이 다운로드 ----

@router.get("/api/play/song/{song_id}")
def play_song(song_id: int, request: Request, user: Annotated[User, Depends(current_user)]):
    """곡 zip 전체. Range 이어받기를 지원하고, 본문을 보낼 때 티켓을 씁니다(304·416은 차감 없음)."""

    def charge() -> None:
        try:
            quota.charge_play(user.id, song_id)
        except quota.NoTicket as e:
            raise HTTPException(
                status_code=429, detail="no download ticket", headers={"Retry-After": str(e.retry_after)}
            )

    return blob_response(request, "song", song_id, f"{song_id}.zip", "song not found", user_id=user.id, before_send=charge)
