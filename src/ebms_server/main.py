import gzip
import importlib.metadata
import logging
import logging.handlers
import os
import re
from contextlib import asynccontextmanager

from .db import Database
from . import auth, constant
from .templating import templates
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles


def configure_logging() -> None:
    constant.LOG_DIR.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    file_handler = logging.handlers.TimedRotatingFileHandler(
        constant.LOG_DIR / "ebms.log",
        when="D",
        interval=7,
        backupCount=53,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    logging.basicConfig(
        level=logging.INFO,
        handlers=[console_handler, file_handler],
        force=True,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    logging.getLogger(__name__).info("DB is now loading...")
    Database()
    logging.getLogger(__name__).info("DB is loaded.")
    if not constant.SECRET_KEY:
        logging.getLogger(__name__).warning("EBMS_SECRET_KEY is not set. Using a random key until restart.")
    if not auth.configured_oauths():
        logging.getLogger(__name__).warning("No OAuth login is configured. Set EBMS_GOOGLE_* or EBMS_DISCORD_*.")
    for folder in os.listdir(constant.TMP_DIR):
        cur_path = constant.TMP_DIR / folder
        if cur_path.is_file(): continue
        Database().insert_songs(cur_path, True, True)
    yield


app = FastAPI(lifespan=lifespan)

app.mount(
    "/static",
    StaticFiles(directory=constant.BASE_DIR / "src" / "ebms_server" / "static"),
    name="static",
)
app.include_router(auth.router)
app.middleware("http")(auth.refresh_session_cookie)

@app.get("/", response_class=HTMLResponse)
def read_root(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="pages/root.html",
    )

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


def blob_response(request: Request, table: str, row_id: int, filename: str, detail: str) -> Response:
    """BLOB을 내려줍니다. Range(단일 범위), If-Range, If-None-Match를 지원하고 ETag는 sha256입니다."""
    blob = Database().open_blob(table, row_id)
    if blob is None:
        raise HTTPException(status_code=404, detail=detail)
    etag = f'"{blob.sha256}"'
    headers = {
        "Accept-Ranges": "bytes",
        "ETag": etag,
        "X-Content-SHA256": blob.sha256,
    }

    if_none_match = request.headers.get("if-none-match")
    if if_none_match and (if_none_match.strip() == "*" or etag in [t.strip() for t in if_none_match.split(",")]):
        blob.close()
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

    headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    if rng is None:
        start, end, status = 0, blob.size - 1, 200
    else:
        start, end = rng
        status = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{blob.size}"
    headers["Content-Length"] = str(end - start + 1)
    return StreamingResponse(
        blob.iter_range(start, end),
        status_code=status,
        media_type="application/zip",
        headers=headers,
    )

@app.get("/api/version")
def get_version():
    try:
        server = importlib.metadata.version("ebms-server")
    except importlib.metadata.PackageNotFoundError:
        server = None
    # 로그인 가능한 OAuth. 클라이언트가 로그인 필요 여부와 수단을 알 수 있게 합니다.
    auth_oauths = [p.name for p in auth.configured_oauths()]
    return {"api": constant.API_VERSION, "server": server, "auth": auth_oauths}

@app.get("/api/files/chart/{chunk_id}")
def download_chart_chunk_file(chunk_id: int, request: Request):
    return blob_response(
        request,
        "chart_chunk",
        chunk_id,
        constant.CHART_CHUNK_FILENAME_TEMPLATE.format(chunk_id),
        "File not found",
    )

@app.get("/api/charthash")
def get_chart_hash():
    return Database().get_chart_chunk_hash()

@app.get("/api/manifest/hash")
def get_manifest_hash():
    return Database().get_manifest_hash()

@app.get("/api/manifest/{chunk_id}")
def get_manifest(chunk_id: int, request: Request):
    data = Database().get_manifest_chunk(chunk_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Manifest not found")
    headers = {"Vary": "Accept-Encoding"}
    if "gzip" in request.headers.get("accept-encoding", ""):
        headers["Content-Encoding"] = "gzip"
    else:
        data = gzip.decompress(data)
    return Response(data, media_type="application/json", headers=headers)

@app.get("/api/files/song/id/{song_id}")
def download_song_by_id(song_id: int, request: Request):
    return blob_response(request, "song", song_id, f"{song_id}.zip", "song not found")

@app.get("/api/files/song/{chart_sha256}")
def download_song(chart_sha256: str, request: Request):
    song_id = Database().get_song_id(chart_sha256)
    if (song_id) is None:
        raise HTTPException(
            status_code=404,
            detail="chart file not found"
        )
    return blob_response(
        request,
        "song",
        song_id,
        f"{song_id}.zip",
        "chart file found but song file not found. report this to admin.",
    )
