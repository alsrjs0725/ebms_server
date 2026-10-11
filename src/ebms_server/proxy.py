"""캐싱 리버스 프록시(deploy/proxy) 연동.

프록시는 큰 파일(차트 청크·사전 청크·곡 zip)을 캐시에서 내보내므로, 요청마다 먼저 `/api/proxy/authz`에
인증을 묻습니다(nginx auth_request). 여기서 로그인 확인, 티켓 차감, 사전 다운로드 사용량 기록을 하고
캐시 키로 쓸 sha256을 알려줍니다. 캐시에 없으면 프록시가 같은 경로를 비밀값과 함께 요청해 채웁니다(채우기 요청).

둘 다 EBMS_PROXY_SECRET이 설정되고 요청의 X-Ebms-Proxy-Secret이 같을 때만 동작합니다.
비어 있으면 /api/proxy/authz는 404이고 다운로드는 예전처럼 서버가 직접 처리합니다.

nginx auth_request는 2xx면 통과, 401·403이면 거부로만 구분하므로, 거부는 모두 403으로 보내고
원래 상태 코드와 내용은 X-Ebms-Status·X-Ebms-Detail·X-Ebms-Retry-After 헤더로 알려줍니다.
"""
import hmac
import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response

from . import auth, constant, quota
from .db import Database

router = APIRouter()

SECRET_HEADER = "x-ebms-proxy-secret"
# 채우기 요청이 기대하는 내용의 sha256. authz와 채우기 사이에 내용이 바뀌면 캐시에 넣지 않게 합니다.
EXPECT_SHA256_HEADER = "x-ebms-expect-sha256"

# 프록시가 캐시하는 경로 -> (테이블, 종류). 종류 pre는 사용량을 기록하고, play는 티켓을 씁니다.
CACHED_ROUTES = (
    (re.compile(r"/api/pre/chart/(\d+)"), "chart_chunk", "pre"),
    (re.compile(r"/api/pre/asset/(\d+)"), "pre_chunk", "pre"),
    (re.compile(r"/api/play/song/(\d+)"), "song", "play"),
)


def from_proxy(request: Request) -> bool:
    """요청이 비밀값을 가진 프록시에서 왔으면 True."""
    secret = constant.PROXY_SECRET
    if not secret:
        return False
    given = request.headers.get(SECRET_HEADER, "")
    return hmac.compare_digest(given.encode(), secret.encode())


def match_route(uri: str) -> tuple[str, int, str] | None:
    """원래 요청 경로(쿼리 포함 가능)를 (테이블, id, 종류)로 바꿉니다. 캐시 대상이 아니면 None."""
    path = uri.split("?", 1)[0]
    for pattern, table, kind in CACHED_ROUTES:
        m = pattern.fullmatch(path)
        if m:
            return table, int(m.group(1)), kind
    return None


def _deny(status: int, detail: str, retry_after: int | None = None) -> Response:
    headers = {"X-Ebms-Status": str(status), "X-Ebms-Detail": detail}
    if retry_after is not None:
        headers["X-Ebms-Retry-After"] = str(retry_after)
    return Response(status_code=403, headers=headers)


def _not_modified(request: Request, etag: str) -> bool:
    value = request.headers.get("if-none-match")
    return bool(value) and (value.strip() == "*" or etag in [t.strip() for t in value.split(",")])


def _requested_bytes(request: Request, etag: str, size: int) -> int:
    """이번 요청으로 나갈 바이트(Range가 있으면 그 길이). 만족할 수 없는 범위면 0."""
    from .downloads import parse_range

    if_range = request.headers.get("if-range")
    if if_range is not None and if_range.strip() != etag:
        return size
    try:
        rng = parse_range(request.headers.get("range"), size)
    except ValueError:
        return 0
    return size if rng is None else rng[1] - rng[0] + 1


def authorize(request: Request) -> Response:
    route = match_route(request.headers.get("x-original-uri", ""))
    if route is None:
        return _deny(404, "Not Found")
    table, row_id, kind = route

    session = auth.optional_api_session(request)
    if session is None:
        return _deny(401, "login required")
    user = session[0]

    blob = Database().open_blob(table, row_id)
    if blob is None:
        return _deny(404, "song not found" if table == "song" else "File not found")
    etag = f'"{blob.sha256}"'
    headers = {"X-Ebms-Sha256": blob.sha256, "X-Ebms-Limit-Rate": "0", "X-Ebms-User": user.id}

    # 304는 프록시가 캐시의 ETag로 답합니다. 차감·사용량 기록 없음.
    if _not_modified(request, etag):
        return Response(status_code=204, headers=headers)
    nbytes = _requested_bytes(request, etag, blob.size)

    if kind == "play":
        if nbytes:
            try:
                quota.charge_play(user.id, row_id)
            except quota.NoTicket as e:
                return _deny(429, "no download ticket", e.retry_after)
    else:
        # 프록시는 보낸 바이트를 알려주지 않으므로 요청한 만큼을 미리 더합니다.
        limits = quota.limits_for(user.id)
        used = quota.pre_used(user.id)
        quota.add_pre_usage(user.id, nbytes)
        if used >= limits.pre_monthly_bytes:
            # 감속은 연결 단위(nginx limit_rate)입니다. 동시 요청이 속도를 나눠 쓰지는 않습니다.
            headers["X-Ebms-Limit-Rate"] = str(limits.pre_throttled_kbps * 1000 // 8)
    return Response(status_code=204, headers=headers)


@router.get("/api/proxy/authz", include_in_schema=False)
async def proxy_authz(request: Request):
    if not from_proxy(request):
        raise HTTPException(status_code=404, detail="Not Found")
    return await run_in_threadpool(authorize, request)
