import os
import pathlib

BASE_DIR = pathlib.Path(__file__).resolve().parents[2]

BMS_FORMAT = (".bms", ".bme", ".bml", ".pms")
DB_HOST = os.environ.get("EBMS_DB_HOST", "127.0.0.1")
DB_PORT = int(os.environ.get("EBMS_DB_PORT", "3306"))
DB_USER = os.environ.get("EBMS_DB_USER", "ebms")
DB_PASSWORD = os.environ.get("EBMS_DB_PASSWORD", "")
DB_NAME = os.environ.get("EBMS_DB_NAME", "ebms")
TMP_DIR = BASE_DIR / "var" / "tmp"
LOG_DIR = BASE_DIR / "var" / "log"
BYTE_PER_CHUNK = 64 * 1024 * 1024
CHART_CHUNK_FILENAME_TEMPLATE = "chart_chunk_{:05d}.zip"
# 매니페스트 청크 하나에 들어가는 song id 개수. chunk_id = song_id // SONGS_PER_MANIFEST_CHUNK
SONGS_PER_MANIFEST_CHUNK = 1000
# /api/version 으로 알려주는 API 버전. 클라이언트와 호환되지 않는 변경이 있을 때 올립니다.
API_VERSION = 1
# BLOB 다운로드 시 한 번에 읽어오는 크기
BLOB_READ_SIZE = 1024 * 1024
# INSERT/UPDATE 쿼리에서 BLOB 외에 필요한 여유 바이트
PACKET_OVERHEAD = 1024 * 1024
# 클라이언트가 보낼 수 있는 최대 패킷 크기. 서버의 max_allowed_packet과 맞춥니다.
DB_MAX_ALLOWED_PACKET = 1024 * 1024 * 1024

# 로그인/세션. docker compose에서는 .env 값을 environment로 넘깁니다.
# 외부에서 접속하는 서버 주소. OAuth 리다이렉트 주소(<PUBLIC_URL>/auth/<provider>/callback)를 만드는 데 씁니다.
PUBLIC_URL = os.environ.get("EBMS_PUBLIC_URL", "http://localhost:8000").rstrip("/")
# 로그인 중 state 쿠키 서명용. 비어 있으면 시작할 때마다 새로 만듭니다(진행 중이던 로그인만 실패).
SECRET_KEY = os.environ.get("EBMS_SECRET_KEY", "")
GOOGLE_CLIENT_ID = os.environ.get("EBMS_GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("EBMS_GOOGLE_CLIENT_SECRET", "")
DISCORD_CLIENT_ID = os.environ.get("EBMS_DISCORD_CLIENT_ID", "")
DISCORD_CLIENT_SECRET = os.environ.get("EBMS_DISCORD_CLIENT_SECRET", "")
# 로그인할 때 provider가 확인한 이메일이 이 목록에 있으면 admin으로 지정합니다(쉼표로 구분).
ADMIN_EMAILS = {
    e.strip().lower() for e in os.environ.get("EBMS_ADMIN_EMAILS", "").split(",") if e.strip()
}
SESSION_COOKIE = "ebms_session"
# 웹 세션 유효기간. 쓸 때마다 연장됩니다.
WEB_SESSION_SECONDS = 30 * 24 * 3600
# provider 로그인 화면에 다녀오는 동안의 state 쿠키 유효기간
OAUTH_STATE_SECONDS = 600

if __name__ == "__main__":
    print(DB_HOST, DB_PORT, DB_NAME)
