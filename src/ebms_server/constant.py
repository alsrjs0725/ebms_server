import os
import pathlib

BASE_DIR = pathlib.Path(__file__).resolve().parents[2]

BMS_FORMAT = (".bms", ".bme", ".bml", ".pms")
# 차트 헤더가 가리키는 파일의 확장자가 실제와 달라도 같은 종류면 같은 파일로 봅니다.
IMAGE_FORMAT = (".bmp", ".png", ".jpg", ".jpeg", ".gif")
AUDIO_FORMAT = (".wav", ".ogg", ".mp3", ".flac")
DB_HOST = os.environ.get("EBMS_DB_HOST", "127.0.0.1")
DB_PORT = int(os.environ.get("EBMS_DB_PORT", "3306"))
DB_USER = os.environ.get("EBMS_DB_USER", "ebms")
DB_PASSWORD = os.environ.get("EBMS_DB_PASSWORD", "")
DB_NAME = os.environ.get("EBMS_DB_NAME", "ebms")
TMP_DIR = BASE_DIR / "var" / "tmp"
# 관리자 페이지에서 올린 zip을 풀어두는 임시 폴더. 등록이 끝나면 지웁니다.
IMPORT_DIR = BASE_DIR / "var" / "import"
LOG_DIR = BASE_DIR / "var" / "log"
BYTE_PER_CHUNK = 64 * 1024 * 1024
# 곡 여러 개를 한 트랜잭션으로 넣을 때 커밋하는 기준(곡 zip 크기 합, 곡 수). 새 차트가 BYTE_PER_CHUNK에 이르러도 커밋합니다.
IMPORT_BATCH_BYTES = 1024 * 1024 * 1024
IMPORT_BATCH_SONGS = 500
# 서버 시작 시 기존 곡을 다시 처리(매니페스트·pre/play 판정 채우기)할 때 커밋하는 곡 수
BACKFILL_BATCH_SONGS = 100
CHART_CHUNK_FILENAME_TEMPLATE = "chart_chunk_{:05d}.zip"
# 매니페스트 청크 하나에 들어가는 song id 개수. chunk_id = song_id // SONGS_PER_MANIFEST_CHUNK
SONGS_PER_MANIFEST_CHUNK = 1000
# 사전 청크(곡의 배너·프리뷰 등 사전 파일 묶음) 하나에 들어가는 song id 개수. chunk_id = song_id // SONGS_PER_PRE_CHUNK
SONGS_PER_PRE_CHUNK = 32
PRE_CHUNK_FILENAME_TEMPLATE = "pre_chunk_{:05d}.zip"
# /api/version 으로 알려주는 API 버전. 클라이언트와 호환되지 않는 변경이 있을 때 올립니다.
API_VERSION = 3
# 다운로드 본문(BLOB 조각) 읽기에 쓰는 스레드 수 한도. API·로그인이 쓰는 기본 스레드 한도(40)와 따로 셉니다.
# 큰 곡을 동시에 많이 받아도 다른 요청이 스레드를 기다리지 않게 합니다. 늘리면 MySQL 동시 연결도 그만큼 늘어납니다.
DOWNLOAD_THREADS = int(os.environ.get("EBMS_DOWNLOAD_THREADS", "16"))
# BLOB 다운로드 시 한 번에 읽어오는 크기
BLOB_READ_SIZE = 1024 * 1024
# INSERT/UPDATE 쿼리에서 BLOB 외에 필요한 여유 바이트
PACKET_OVERHEAD = 1024 * 1024
# 연결 풀에 남겨 둘 유휴 MySQL 연결 수 상한
DB_POOL_SIZE = int(os.environ.get("EBMS_DB_POOL_SIZE", "32"))
# 클라이언트가 보낼 수 있는 최대 패킷 크기. 서버의 max_allowed_packet과 맞춥니다.
DB_MAX_ALLOWED_PACKET = 1024 * 1024 * 1024

# S3 호환 스토리지(R2·MinIO·B2·AWS 등) 앞단 캐시. EBMS_S3_BUCKET을 비우면 끄고 지금처럼 서버가 직접 보냅니다.
# 켜면 원본은 MySQL에 그대로 두고, 곡 zip·차트 청크·사전 청크를 버킷에 올린 뒤 presigned URL로 302 리다이렉트합니다.
S3_ENDPOINT = os.environ.get("EBMS_S3_ENDPOINT", "").rstrip("/")
S3_BUCKET = os.environ.get("EBMS_S3_BUCKET", "")
S3_ACCESS_KEY_ID = os.environ.get("EBMS_S3_ACCESS_KEY_ID", "")
S3_SECRET_ACCESS_KEY = os.environ.get("EBMS_S3_SECRET_ACCESS_KEY", "")
# R2는 auto
S3_REGION = os.environ.get("EBMS_S3_REGION", "auto")
# 버킷에 둘 바이트 상한. 넘으면 곡 zip을 오래 안 쓴 것부터 지웁니다(차트·사전 청크는 지우지 않음). 0이면 지우지 않습니다(전체 저장).
S3_CACHE_BYTES = int(os.environ.get("EBMS_S3_CACHE_BYTES", "0"))
# presigned URL 유효시간(초). 이 시간 안에 받은 객체는 지우지 않습니다.
S3_URL_SECONDS = int(os.environ.get("EBMS_S3_URL_SECONDS", "600"))
# 버킷에 없을 때 올리기를 기다리는 시간(초). 넘으면 503 + Retry-After로 다시 오게 합니다(클라이언트 읽기 제한 30초보다 짧게).
S3_UPLOAD_WAIT_SECONDS = float(os.environ.get("EBMS_S3_UPLOAD_WAIT_SECONDS", "20"))
# 동시에 올리는 수
S3_UPLOAD_THREADS = int(os.environ.get("EBMS_S3_UPLOAD_THREADS", "2"))

# 로그인/세션. docker compose에서는 .env 값을 environment로 넘깁니다.
# 외부에서 접속하는 서버 주소. OAuth 리다이렉트 주소(<PUBLIC_URL>/auth/<oauth>/callback)를 만드는 데 씁니다.
PUBLIC_URL = os.environ.get("EBMS_PUBLIC_URL", "http://localhost:8000").rstrip("/")
# 로그인 중 state 쿠키 서명용. 비어 있으면 시작할 때마다 새로 만듭니다(진행 중이던 로그인만 실패).
SECRET_KEY = os.environ.get("EBMS_SECRET_KEY", "")
GOOGLE_CLIENT_ID = os.environ.get("EBMS_GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("EBMS_GOOGLE_CLIENT_SECRET", "")
DISCORD_CLIENT_ID = os.environ.get("EBMS_DISCORD_CLIENT_ID", "")
DISCORD_CLIENT_SECRET = os.environ.get("EBMS_DISCORD_CLIENT_SECRET", "")
# 로그인할 때 OAuth가 확인한 이메일이 이 목록에 있으면 admin으로 지정합니다(쉼표로 구분).
ADMIN_EMAILS = {
    e.strip().lower() for e in os.environ.get("EBMS_ADMIN_EMAILS", "").split(",") if e.strip()
}
SESSION_COOKIE = "ebms_session"
# 웹 세션 유효기간. 쓸 때마다 연장됩니다.
WEB_SESSION_SECONDS = 30 * 24 * 3600
# OAuth 로그인 화면에 다녀오는 동안의 state 쿠키 유효기간
OAUTH_STATE_SECONDS = 600
# 클라이언트 세션키 유효기간. 쓸 때마다 연장됩니다.
CLIENT_SESSION_SECONDS = 90 * 24 * 3600
# 클라이언트 로그인용 1회용 코드 유효기간
AUTH_CODE_SECONDS = 60

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
