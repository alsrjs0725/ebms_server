import importlib.metadata
import logging
import logging.handlers
import os
from contextlib import asynccontextmanager

from .db import Database
from . import admin, auth, constant, downloads
from .downloads import blob_response, manifest_response
from .templating import templates
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
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
app.include_router(downloads.router)
app.include_router(admin.router)
app.middleware("http")(auth.refresh_session_cookie)

@app.get("/", response_class=HTMLResponse)
def read_root(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="pages/root.html",
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
    return manifest_response(request, chunk_id)

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
