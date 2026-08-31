import logging
import logging.handlers
import os
from contextlib import asynccontextmanager

from .db import Database
from . import constant
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates


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
    for folder in os.listdir(constant.TMP_DIR):
        cur_path = constant.TMP_DIR / folder
        if cur_path.is_file(): continue
        Database().insert_songs(cur_path, True, True)
    yield


app = FastAPI(lifespan=lifespan)

templates = Jinja2Templates(
    directory=constant.BASE_DIR / "src" / "ebms_server" / "templates"
)
app.mount(
    "/static",
    StaticFiles(directory=constant.BASE_DIR / "src" / "ebms_server" / "static"),
    name="static",
)

@app.get("/", response_class=HTMLResponse)
def read_root(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="pages/root.html",
    )

@app.get("/api/files/chart/{chunk_id}")
def download_chart_chunk_file(chunk_id: int):
    file_path = constant.CHART_DATA_DIR / constant.CHART_CHUNK_FILENAME_TEMPLATE.format(chunk_id)
    if not os.path.exists(file_path):
        raise HTTPException(
            status_code=404,
            detail="File not found"
        )
    return FileResponse(file_path)

@app.get("/api/charthash")
def get_chart_hash():
    return Database().get_chart_chunk_hash()

@app.get("/api/files/song/{chart_sha256}")
def download_song(chart_sha256:str):
    song_id = Database().get_song_id(chart_sha256)
    if (song_id) is None:
        raise HTTPException(
            status_code=404,
            detail="chart file not found"
        )
    print(song_id)
    song_path = Database().get_song_file(song_id)
    if song_path is None:
        raise HTTPException(
            status_code=404,
            detail="chart file found but song file not found. report this to admin."
        )
    return FileResponse(song_path)
