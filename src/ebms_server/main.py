import importlib.metadata
import logging
import logging.handlers
import shutil
import threading
import urllib.parse
from contextlib import asynccontextmanager

from .db import Database
from . import admin, api_keys, audit, auth, constant, downloads, home, importer, mcp, notices, policies, pre_rebalance, preview, proxy, render_index, retention, staging, tables
from .templating import templates
from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
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
    configure_audit_log(formatter)
    configure_access_log()
    configure_uvicorn_log()


def configure_audit_log(formatter: logging.Formatter) -> None:
    """관리자 접속기록을 audit.log에도 따로 남깁니다(ebms.log에도 같이 들어감)."""
    audit_logger = logging.getLogger(audit.__name__)
    for handler in audit_logger.handlers[:]:
        audit_logger.removeHandler(handler)
        handler.close()
    audit_logger.addHandler(audit.file_handler(formatter))


def configure_access_log() -> None:
    """uvicorn 접근 로그(접속 IP·경로)를 stdout(docker 로그) 대신 var/log/access.log에 남기고
    constant.ACCESS_LOG_DAYS일이 지나면 지웁니다."""
    access_logger = logging.getLogger("uvicorn.access")
    for handler in access_logger.handlers[:]:
        access_logger.removeHandler(handler)
        handler.close()
    handler = logging.handlers.TimedRotatingFileHandler(
        constant.LOG_DIR / "access.log",
        when="midnight",
        backupCount=constant.ACCESS_LOG_DAYS,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    access_logger.addHandler(handler)
    access_logger.propagate = False


def configure_uvicorn_log() -> None:
    """uvicorn 자체 로그(요청 처리 중 난 예외의 트레이스백, 시작·종료)를 uvicorn의 stderr 핸들러 대신 루트로 보내
    ebms.log에도 남깁니다. 그대로 두면 500의 원인이 docker 로그에만 남고 관리자 로그 화면에는 보이지 않습니다."""
    uvicorn_logger = logging.getLogger("uvicorn")
    for handler in uvicorn_logger.handlers[:]:
        uvicorn_logger.removeHandler(handler)
        handler.close()
    uvicorn_logger.propagate = True


def background_startup() -> None:
    """시작(healthcheck)을 막지 않게 뒤에서 하는 일. 다 만들기 전에는 클라이언트가 파일별 사전 API로 받습니다."""
    # 곡은 있는데 사전 청크가 없거나 든 곡이 바뀐 사전 청크를 다시 만든다(이전 버전에서 넣은 곡, 도중에 꺼진 작업).
    try:
        Database().backfill_pre_chunks()
    except Exception:
        logging.getLogger(__name__).exception("backfill_pre_chunks failed")
    # 사전 청크에 난이도표 조합이 섞인 곡을 조합이 같은 청크로 조금씩 옮긴다(이전 버전에서 넣은 곡, 바뀐 난이도표).
    pre_rebalance.kick()
    # 미리듣기 음원이 없는 곡의 미리듣기를 만든다. 곡마다 키음을 풀어 섞으므로 오래 걸린다.
    preview.kick()


def store_gc_loop(stop: threading.Event) -> None:
    """아무 행도 가리키지 않는 저장소 파일을 시작 직후 한 번, 이후 STORE_GC_HOURS마다 지운다."""
    while not stop.is_set():
        try:
            Database().store_gc()
        except Exception:
            logging.getLogger(__name__).exception("store_gc failed")
        stop.wait(constant.STORE_GC_HOURS * 3600)


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


class TableStaticFiles(StaticFiles):
    """정적 파일에 `Access-Control-Allow-Origin: *`를 붙입니다(공유용 난이도표)."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Access-Control-Allow-Origin"] = "*"
        return response


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    logging.getLogger(__name__).info("DB is now loading...")
    Database()
    logging.getLogger(__name__).info("DB is loaded.")
    threading.Thread(target=background_startup, name="background_startup", daemon=True).start()
    store_stop = threading.Event()
    if constant.STORE_GC_HOURS > 0:
        threading.Thread(target=store_gc_loop, args=(store_stop,), name="store_gc", daemon=True).start()
    if not constant.SECRET_KEY:
        logging.getLogger(__name__).warning("EBMS_SECRET_KEY is not set. Using a random key until restart.")
    if insecure_public_url(constant.PUBLIC_URL):
        logging.getLogger(__name__).warning(
            f"EBMS_PUBLIC_URL({constant.PUBLIC_URL}) is not https. Sessions and login codes are sent in plain text. "
            "Put the server behind a TLS reverse proxy and set an https:// URL (docs/deployment.md)."
        )
    if not auth.configured_oauths():
        logging.getLogger(__name__).warning("No OAuth login is configured. Set EBMS_GOOGLE_* or EBMS_DISCORD_*.")
    # 받던 업로드는 이어 받을 수 있게 남기고, 이전 실행에서 임포트 도중 꺼져 남은 임시 폴더는 지운다
    importer.recover()
    shutil.rmtree(constant.CACHE_DIR / "work", ignore_errors=True)
    # 대기 묶음을 풀거나 commit하던 중에 꺼졌다면 그 자리부터 이어서 한다
    staging.recover()
    # var/tmp는 서버 시작 시 가져오지 않는다(관리자가 "var/tmp 가져오기"를 누를 때만). 폴더만 만들어 둔다
    constant.TMP_DIR.mkdir(parents=True, exist_ok=True)
    # 보유기간이 지난 개인정보(세션·다운로드 기록·월 사용량 등)를 주기적으로 지운다(docs/privacy.md)
    retention_stop = retention.start()
    # 등록한 난이도표를 주기적으로 다시 읽는다(새로 실린 곡)
    tables_stop = tables.start()
    # 모든 곡 차트를 조금씩 렌더 색인해 중복·섞임 의심을 관리자 목록에 쌓는다(render_index.py)
    render_index_stop = render_index.start()
    yield
    store_stop.set()
    if retention_stop is not None:
        retention_stop.set()
    if tables_stop is not None:
        tables_stop.set()
    if render_index_stop is not None:
        render_index_stop.set()


app = FastAPI(lifespan=lifespan)


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception):
    """처리하지 못한 예외(500). 관리자 API는 화면에 원인이 보이게 예외 종류와 메시지를 detail로 돌려줍니다.
    트레이스백은 그대로 로그에 남습니다."""
    if request.url.path.startswith("/api/admin/"):
        return JSONResponse({"detail": f"서버 오류({type(exc).__name__}): {exc}"}, status_code=500)
    return PlainTextResponse("Internal Server Error", status_code=500)

app.mount(
    "/static",
    StaticFiles(directory=constant.BASE_DIR / "src" / "ebms_server" / "static"),
    name="static",
)
# 공유용 난이도표 페이지(constant.TABLE_PAGE_DIR). 로그인 없이 열리고, 다른 사이트의 난이도표 뷰어가
# header·data JSON을 읽을 수 있게 CORS를 엽니다.
app.mount("/table", TableStaticFiles(directory=constant.TABLE_PAGE_DIR), name="table")
app.include_router(auth.router)
app.include_router(downloads.router)
app.include_router(proxy.router)
app.include_router(admin.router)
app.include_router(api_keys.router)
app.include_router(mcp.router)
app.include_router(notices.router)
app.include_router(tables.router)
app.include_router(render_index.router)
app.include_router(policies.router)
app.include_router(home.router)
app.middleware("http")(auth.refresh_session_cookie)
app.add_middleware(SecurityHeadersMiddleware)

@app.get("/", response_class=HTMLResponse)
def read_root(request: Request, session: Annotated[tuple | None, Depends(auth.optional_session)]):
    return templates.TemplateResponse(
        request=request,
        name="pages/root.html",
        context={"user": session[0] if session else None, "home": home.content()},
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
