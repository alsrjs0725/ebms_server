import importlib.metadata
import logging
import logging.handlers
import shutil
import threading
import urllib.parse
from contextlib import asynccontextmanager

from .db import Database
from . import admin, auth, constant, downloads, importer, notices, proxy
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


def insecure_public_url(url: str) -> bool:
    """EBMS_PUBLIC_URL이 https가 아니고 localhost(루프백)도 아니면 True.
    이 경우 세션 쿠키·OAuth 코드·클라이언트 세션키가 암호화 없이 오갑니다."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "https":
        return False
    return parts.hostname not in ("localhost", "127.0.0.1", "::1")


# 모든 응답에 붙이는 보안 헤더. 페이지는 static의 스크립트·스타일만 쓰고(인라인 스크립트·style 속성 없음),
# Bootstrap CSS의 아이콘은 data: SVG입니다. 다른 사이트의 iframe에 넣지 못하게 합니다(클릭재킹 방지).
# HSTS는 TLS를 끝내는 리버스 프록시에서 붙입니다(docs/deployment.md).
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; "
        "connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
}


class SecurityHeadersMiddleware:
    """응답에 SECURITY_HEADERS를 붙입니다(이미 있는 헤더는 그대로 둠). 본문은 건드리지 않아 스트리밍 응답에도 씁니다."""

    def __init__(self, app):
        self.app = app
        self.headers = [(name.lower().encode(), value.encode()) for name, value in SECURITY_HEADERS.items()]

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {name.lower() for name, _ in headers}
                headers.extend((name, value) for name, value in self.headers if name not in present)
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


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
app.include_router(proxy.router)
app.include_router(admin.router)
app.include_router(notices.router)
app.middleware("http")(auth.refresh_session_cookie)
app.add_middleware(SecurityHeadersMiddleware)

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
