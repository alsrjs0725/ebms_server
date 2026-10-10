import importlib.metadata
import logging
import logging.handlers
import shutil
import threading
import urllib.parse
from contextlib import asynccontextmanager

from .db import Database
from . import admin, auth, constant, downloads, importer, notices, s3cache
from .templating import templates
from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from typing import Annotated


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


def backfill_pre_chunks() -> None:
    try:
        Database().backfill_pre_chunks()
    except Exception:
        logging.getLogger(__name__).exception("backfill_pre_chunks failed")
    # S3 캐시가 켜져 있으면 차트·사전 청크를 버킷에 미리 올려 둔다(모든 사용자가 동기화할 때 받음).
    cache = s3cache.get()
    if cache is not None:
        try:
            cache.prewarm()
        except Exception:
            logging.getLogger(__name__).exception("s3 prewarm failed")


def insecure_public_url(url: str) -> bool:
    """EBMS_PUBLIC_URL이 https가 아니고 localhost(루프백)도 아니면 True.
    이 경우 세션 쿠키·OAuth 코드·클라이언트 세션키가 암호화 없이 오갑니다."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "https":
        return False
    return parts.hostname not in ("localhost", "127.0.0.1", "::1")


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    logging.getLogger(__name__).info("DB is now loading...")
    Database()
    logging.getLogger(__name__).info("DB is loaded.")
    # 이전 버전에서 넣은 곡의 사전 청크는 시간이 걸리므로 뒤에서 만든다. 다 만들기 전에는 클라이언트가 파일별 사전 API로 받는다.
    threading.Thread(target=backfill_pre_chunks, name="backfill_pre_chunks", daemon=True).start()
    if not constant.SECRET_KEY:
        logging.getLogger(__name__).warning("EBMS_SECRET_KEY is not set. Using a random key until restart.")
    if insecure_public_url(constant.PUBLIC_URL):
        logging.getLogger(__name__).warning(
            f"EBMS_PUBLIC_URL({constant.PUBLIC_URL}) is not https. Sessions and login codes are sent in plain text. "
            "Put the server behind a TLS reverse proxy and set an https:// URL (docs/deployment.md)."
        )
    if s3cache.enabled():
        logging.getLogger(__name__).info(
            f"S3 cache enabled: bucket={constant.S3_BUCKET} cache_bytes={constant.S3_CACHE_BYTES or 'unlimited'}"
        )
    if not auth.configured_oauths():
        logging.getLogger(__name__).warning("No OAuth login is configured. Set EBMS_GOOGLE_* or EBMS_DISCORD_*.")
    # 이전 실행에서 임포트 도중 꺼졌다면 남은 임시 폴더를 지운다
    shutil.rmtree(constant.IMPORT_DIR, ignore_errors=True)
    # var/tmp 임포트는 큰 zip이 있으면 오래 걸리므로 백그라운드로 돌려 시작(헬스체크)을 막지 않는다.
    # var를 빈 호스트 폴더로 마운트하면 tmp가 없으므로 import_tmp가 만들어 둔다
    importer.start_tmp_job()
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
app.include_router(notices.router)
app.middleware("http")(auth.refresh_session_cookie)

@app.get("/", response_class=HTMLResponse)
def read_root(request: Request, session: Annotated[tuple | None, Depends(auth.optional_session)]):
    return templates.TemplateResponse(
        request=request,
        name="pages/root.html",
        context={"user": session[0] if session else None},
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
