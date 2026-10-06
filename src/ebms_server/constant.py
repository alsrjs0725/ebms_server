import os
import pathlib

BASE_DIR = pathlib.Path(__file__).resolve().parents[2]

BMS_FORMAT = (".bms", ".bme", ".bml", ".pms")
# 기존 SQLite DB 경로 (migrate_sqlite 마이그레이션 원본으로만 사용)
DB_PATH = BASE_DIR / "var" / "ebms.db"
DB_HOST = os.environ.get("EBMS_DB_HOST", "127.0.0.1")
DB_PORT = int(os.environ.get("EBMS_DB_PORT", "3306"))
DB_USER = os.environ.get("EBMS_DB_USER", "ebms")
DB_PASSWORD = os.environ.get("EBMS_DB_PASSWORD", "")
DB_NAME = os.environ.get("EBMS_DB_NAME", "ebms")
SONG_DATA_DIR = BASE_DIR / "var" / "media" / "song"
CHART_DATA_DIR = BASE_DIR / "var" / "media" / "chart"
TMP_DIR = BASE_DIR / "var" / "tmp"
LOG_DIR = BASE_DIR / "var" / "log"
BYTE_PER_CHUNK = 64 * 1024 * 1024
CHART_CHUNK_FILENAME_TEMPLATE = "chart_chunk_{:05d}.zip"

if __name__ == "__main__":
    print(DB_PATH)
