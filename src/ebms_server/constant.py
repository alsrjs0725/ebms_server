import pathlib

BASE_DIR = pathlib.Path(__file__).resolve().parents[2]

BMS_FORMAT = (".bms", ".bme", ".bml", ".pms")
DB_PATH = BASE_DIR / "var" / "ebms.db"
SONG_DATA_DIR = BASE_DIR / "var" / "media" / "song"
CHART_DATA_DIR = BASE_DIR / "var" / "media" / "chart"
TMP_DIR = BASE_DIR / "var" / "tmp"
LOG_DIR = BASE_DIR / "var" / "log"
BYTE_PER_CHUNK = 64 * 1024 * 1024
CHART_CHUNK_FILENAME_TEMPLATE = "chart_chunk_{:05d}.zip"

if __name__ == "__main__":
    print(DB_PATH)
