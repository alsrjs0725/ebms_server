import os
import pathlib

BASE_DIR = pathlib.Path(__file__).resolve().parents[2]

# 차트 파일 확장자. .bmson은 JSON 형식 차트입니다(db.chart_meta·_chart_refs가 내용으로 구분).
BMS_FORMAT = (".bms", ".bme", ".bml", ".pms", ".bmson")
# 차트 헤더가 가리키는 파일의 확장자가 실제와 달라도 같은 종류면 같은 파일로 봅니다.
IMAGE_FORMAT = (".bmp", ".png", ".jpg", ".jpeg", ".gif")
AUDIO_FORMAT = (".wav", ".ogg", ".mp3", ".flac")
# BGA 영상(media_match가 다시 인코딩만 다른지 볼 때 씁니다)
VIDEO_FORMAT = (".mpg", ".mpeg", ".m1v", ".m2v", ".mp4", ".m4v", ".avi", ".wmv", ".webm", ".mkv", ".flv", ".mov")
DB_HOST = os.environ.get("EBMS_DB_HOST", "127.0.0.1")
DB_PORT = int(os.environ.get("EBMS_DB_PORT", "3306"))
DB_USER = os.environ.get("EBMS_DB_USER", "ebms")
DB_PASSWORD = os.environ.get("EBMS_DB_PASSWORD", "")
DB_NAME = os.environ.get("EBMS_DB_NAME", "ebms")
TMP_DIR = BASE_DIR / "var" / "tmp"
# 관리자 페이지에서 올린 zip을 풀어두는 임시 폴더. 등록이 끝나면 지웁니다.
IMPORT_DIR = BASE_DIR / "var" / "import"
# 올리거나 var/tmp에서 가져온 곡을 등록 전에 검토하도록 풀어 두는 폴더(staging.py). commit하거나 버리면 지웁니다.
STAGED_DIR = BASE_DIR / "var" / "staged"
LOG_DIR = BASE_DIR / "var" / "log"


def parse_size(text: str) -> int:
    """'50G', '512M', '1073741824' 같은 크기를 바이트로. 단위는 K·M·G·T(1024배)."""
    text = text.strip().upper().removesuffix("B").removesuffix("I")
    units = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}
    if text and text[-1] in units:
        return int(float(text[:-1]) * units[text[-1]])
    return int(text or 0)


# 임포트 중간 작업 공간(cache.py). 빠른 디스크(SSD)를 이 폴더에 붙이고 EBMS_CACHE_MAX_BYTES(예: 50G)로 한도를 줍니다.
# 0이면 끄고 모두 var/ 아래를 씁니다. 한도를 넘는 업로드·묶음은 var/(HDD)에 둡니다.
CACHE_DIR = pathlib.Path(os.environ.get("EBMS_CACHE_DIR", BASE_DIR / "cache"))
CACHE_MAX_BYTES = parse_size(os.environ.get("EBMS_CACHE_MAX_BYTES", "0"))
# 곡 zip·차트 청크·사전 청크 파일을 두는 폴더(store.py, #152). DB에는 경로·크기·sha256만 둡니다.
STORE_DIR = pathlib.Path(os.environ.get("EBMS_STORE_DIR", BASE_DIR / "var" / "store"))
# 아무 행도 가리키지 않는 저장소 파일을 지우는 주기(시간)와, 지우지 않고 두는 최근 파일의 나이(초).
STORE_GC_HOURS = float(os.environ.get("EBMS_STORE_GC_HOURS", "1"))
STORE_GC_GRACE_SECONDS = 600
# HTTP 접근 로그(var/log/access.log, 접속 IP·경로)를 날마다 돌려 남기는 일수. 처리방침의 보유기간과 맞춥니다.
ACCESS_LOG_DAYS = 30
# 관리자가 지운(사용 중지한) 곡의 zip을 옮겨 두는 폴더. 복원하면 DB로 되돌리고 지웁니다.
UNUSED_DIR = BASE_DIR / "var" / "unused"
# 차트 청크 하나에 넣는 차트 수. 차트가 커서 BYTE_PER_CHUNK를 넘으면 그 전에 다음 청크로 넘어갑니다.
# 새로 만드는 청크에만 적용됩니다. 기존 청크를 다시 묶으려면 따로 스크립트를 돌려야 합니다.
CHARTS_PER_CHUNK = 100
BYTE_PER_CHUNK = 64 * 1024 * 1024
# 곡 여러 개를 한 트랜잭션으로 넣을 때 커밋하는 곡 수. 새 차트가 BYTE_PER_CHUNK에 이르러도 커밋합니다.
# 곡 zip은 add 때 바로 저장소 파일로 쓰고 트랜잭션에는 행만 들어가므로 크기 기준은 두지 않습니다.
# 커밋마다 마지막 차트 청크를 다시 쓰므로 너무 작게 잡지 않습니다.
IMPORT_BATCH_SONGS = 20
# 저속 임포트(관리자 페이지 "천천히"): 작은 배치로 자주 커밋해 쓰기 잠금과 MySQL 쓰기 폭주를 줄이고,
# 커밋마다 일한 시간 x IMPORT_SLOW_REST_RATIO 만큼(최대 IMPORT_SLOW_REST_MAX초) 쉬어 디스크를 서비스에 양보합니다.
IMPORT_SLOW_BATCH_SONGS = 5
IMPORT_SLOW_REST_RATIO = 1.0
IMPORT_SLOW_REST_MAX = 60
# 오래 걸리는 뒤 작업(사전 청크·미리듣기 만들기 등)의 진행도 로그를 남기는 간격(초)
BACKFILL_PROGRESS_SECONDS = 10
CHART_CHUNK_FILENAME_TEMPLATE = "chart_chunk_{:05d}.zip"
# 매니페스트 청크 하나에 들어가는 song id 개수. chunk_id = song_id // SONGS_PER_MANIFEST_CHUNK
# 곡 하나가 바뀌어도 청크를 통째로 다시 만들고 클라이언트가 다시 받으므로 작게 둡니다(곡당 파일 목록이 커서 100곡에 gzip 약 2MB).
# chunk_id가 song_id에서 바로 나오므로 바꾸면 기존 매니페스트 청크를 모두 다시 만들어야 합니다(따로 스크립트).
SONGS_PER_MANIFEST_CHUNK = 100
# 사전 청크(곡의 배너·프리뷰 등 사전 파일 묶음) 하나에 넣는 최대 곡 수. 곡은 속한 난이도표 조합이 같은 곡끼리 묶습니다
# (pre_rebalance.py). 곡 하나가 바뀌면 청크를 통째로 다시 받으므로 작게 둡니다.
SONGS_PER_PRE_CHUNK = 8
# 사전 청크 재배치(pre_rebalance.py) 한 번에 다시 쓰는 청크 용량의 대략적인 한도와, 일감이 남았을 때 다음 번까지 쉬는 시간(초).
# 한 번에 다 옮기지 않아 임포트·다운로드에 디스크를 양보하고, 클라이언트가 다시 받는 양도 나눕니다.
PRE_REBALANCE_BYTES = int(os.environ.get("EBMS_PRE_REBALANCE_BYTES", str(2 * 1024 ** 3)))
PRE_REBALANCE_PAUSE_SECONDS = float(os.environ.get("EBMS_PRE_REBALANCE_PAUSE_SECONDS", "600"))
# 사전 청크 재배치를 뒤에서 돌릴지. 끄면 새 곡만 제자리에 넣고 기존 곡은 옮기지 않습니다.
PRE_REBALANCE_ENABLED = os.environ.get("EBMS_PRE_REBALANCE", "1") not in ("0", "false", "no", "")
# 미리듣기 음원이 없는 곡에 서버가 미리듣기를 만들어 사전 파일로 줄지(preview.py, ffmpeg 필요).
PREVIEW_ENABLED = os.environ.get("EBMS_PREVIEW", "1") not in ("0", "false", "no", "")
PRE_CHUNK_FILENAME_TEMPLATE = "pre_chunk_{:05d}.zip"
# 난이도표(tables.py)를 읽을 때 주소 하나의 응답 시간·크기 한도
TABLE_FETCH_TIMEOUT = 30
TABLE_MAX_BYTES = 32 * 1024 * 1024
# 등록한 난이도표를 모두 다시 읽는 주기(시간). 0이면 자동으로 다시 읽지 않습니다(관리자 페이지에서 직접).
TABLE_REFRESH_HOURS = float(os.environ.get("EBMS_TABLE_REFRESH_HOURS", "24"))
# 공유용 난이도표 정적 페이지 폴더. `/table/{표 이름}.html`(과 그 페이지가 가리키는 header·data JSON)로 그대로 내보냅니다.
TABLE_PAGE_DIR = BASE_DIR / "src" / "ebms_server" / "table"
# /api/version 으로 알려주는 API 버전. 클라이언트와 호환되지 않는 변경이 있을 때 올립니다.
API_VERSION = 3
# 다운로드 본문(BLOB 조각) 읽기에 쓰는 스레드 수 한도. API·로그인이 쓰는 기본 스레드 한도(40)와 따로 셉니다.
# 큰 곡을 동시에 많이 받아도 다른 요청이 스레드를 기다리지 않게 합니다. 늘리면 MySQL 동시 연결도 그만큼 늘어납니다.
DOWNLOAD_THREADS = int(os.environ.get("EBMS_DOWNLOAD_THREADS", "16"))
# 파일을 나눠 읽거나 복사할 때 한 번에 다루는 크기
READ_SIZE = 1024 * 1024
# 다운로드 하나가 보내기 전에 미리 읽어 메모리에 쌓아 두는 조각 수 상한(조각당 READ_SIZE).
# 디스크 읽기가 전송을 기다리지 않고 연달아 이어지게 합니다. 메모리는 동시 다운로드 수 × 이 값 × 1MB까지 씁니다.
READ_AHEAD_CHUNKS = int(os.environ.get("EBMS_READ_AHEAD_CHUNKS", "64"))
# 연결 풀에 남겨 둘 유휴 MySQL 연결 수 상한
DB_POOL_SIZE = int(os.environ.get("EBMS_DB_POOL_SIZE", "32"))

# 로그인/세션. docker compose에서는 .env 값을 environment로 넘깁니다.
# 외부에서 접속하는 서버 주소. OAuth 리다이렉트 주소(<PUBLIC_URL>/auth/<oauth>/callback)를 만드는 데 씁니다.
PUBLIC_URL = os.environ.get("EBMS_PUBLIC_URL", "http://localhost:8000").rstrip("/")
# 로그인 중 state 쿠키 서명용. 비어 있으면 시작할 때마다 새로 만듭니다(진행 중이던 로그인만 실패).
SECRET_KEY = os.environ.get("EBMS_SECRET_KEY", "")
GOOGLE_CLIENT_ID = os.environ.get("EBMS_GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("EBMS_GOOGLE_CLIENT_SECRET", "")
DISCORD_CLIENT_ID = os.environ.get("EBMS_DISCORD_CLIENT_ID", "")
DISCORD_CLIENT_SECRET = os.environ.get("EBMS_DISCORD_CLIENT_SECRET", "")
# 이 계정 ID(user.id UUID, /account에 표시)는 로그인하거나 세션을 쓸 때 admin으로 지정합니다(쉼표로 구분).
ADMIN_USER_IDS = {
    u.strip().lower() for u in os.environ.get("EBMS_ADMIN_USER_IDS", "").split(",") if u.strip()
}
# 캐싱 리버스 프록시(deploy/proxy)와 나누는 비밀값. 설정하면 프록시가 /api/proxy/authz로 다운로드 권한을 묻고
# 캐시를 채울 수 있습니다. 비우면 프록시 연동을 끕니다(docs/proxy.md).
PROXY_SECRET = os.environ.get("EBMS_PROXY_SECRET", "")
SESSION_COOKIE = "ebms_session"
# 웹 세션 유효기간. 쓸 때마다 연장됩니다.
# 관리자 API 키(api_keys.py)의 접두어. 이 접두어로 시작하는 Bearer 토큰은 세션키가 아니라 API 키로 봅니다.
API_KEY_PREFIX = "ebms_ak_"
WEB_SESSION_SECONDS = 30 * 24 * 3600
# OAuth 로그인 화면에 다녀오는 동안의 state 쿠키 유효기간
OAUTH_STATE_SECONDS = 600
# 처음 로그인한 뒤 닉네임을 정해 가입을 마칠 때까지의 시간
SIGNUP_SECONDS = 1800
# 클라이언트 세션키 유효기간. 쓸 때마다 연장됩니다.
CLIENT_SESSION_SECONDS = 90 * 24 * 3600
# 클라이언트 로그인용 1회용 코드 유효기간
AUTH_CODE_SECONDS = 60

# 개인정보 보유기간(docs/privacy.md). 지난 행은 정리 작업이 RETENTION_INTERVAL_HOURS마다 지웁니다.
# 0이면 그 항목은 지우지 않습니다(RETENTION_GRANT_DAYS만 예외로 -1).
# 만료·해지된 세션(기기 이름·사용 시각)을 남겨 두는 일수
RETENTION_SESSION_DAYS = int(os.environ.get("EBMS_RETENTION_SESSION_DAYS", "30"))
# 곡별 다운로드 기록(download_grant)을 차감 면제 기간(expires_at)이 지난 뒤 남겨 두는 일수. 0이면 지나자마자, -1이면 지우지 않습니다.
RETENTION_GRANT_DAYS = int(os.environ.get("EBMS_RETENTION_GRANT_DAYS", "0"))
# 월별 사전 다운로드 사용량(pre_usage)을 이번 달 포함 몇 달 남길지
RETENTION_PRE_USAGE_MONTHS = int(os.environ.get("EBMS_RETENTION_PRE_USAGE_MONTHS", "12"))
# 곡 플레이 다운로드 기록(download_log)을 남겨 두는 일수. 0이면 지우지 않습니다.
RETENTION_DOWNLOAD_LOG_DAYS = int(os.environ.get("EBMS_RETENTION_DOWNLOAD_LOG_DAYS", "90"))
# 마지막 로그인·세션 사용 뒤 이 일수가 지난 계정을 연결된 데이터와 함께 지웁니다(관리자 제외). 기본 0(지우지 않음).
RETENTION_INACTIVE_USER_DAYS = int(os.environ.get("EBMS_RETENTION_INACTIVE_USER_DAYS", "0"))
# 정리 작업 주기(시간). 서버가 시작할 때 한 번 돌고 이후 이 간격으로 돕니다. 0이면 정리 작업을 끕니다.
RETENTION_INTERVAL_HOURS = float(os.environ.get("EBMS_RETENTION_INTERVAL_HOURS", "24"))

# 할당량 전역 기본값. 처음 시작할 때 setting 테이블에 넣고, 이후에는 관리자 페이지에서 바꿉니다.
DEFAULT_SETTINGS = {
    # 플레이 다운로드: 최대 티켓 수, 티켓 1개가 차는 시간(초), 차감 후 같은 곡을 다시 받아도 차감하지 않는 시간(초)
    "max_tickets": 5,
    "refill_seconds": 60,
    "grant_seconds": 1800,
    # 사전 다운로드: 매달 정상 속도로 받을 수 있는 바이트, 넘은 뒤 속도(Kbps)
    "pre_monthly_bytes": 10 * 1024 ** 3,
    "pre_throttled_kbps": 500,
}

if __name__ == "__main__":
    print(DB_HOST, DB_PORT, DB_NAME)
