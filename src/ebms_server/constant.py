import os
import pathlib

BMS_FORMAT = (".bms", ".bme", ".bml", ".pms")
DB_PATH = pathlib.Path(os.path.realpath("./ebms.db"))
SONG_DATA_DIR = pathlib.Path(os.path.realpath("./data/song"))
CHART_DATA_DIR = pathlib.Path(os.path.realpath("./data/chart"))
CHART_METADATA_PATH = pathlib.Path(os.path.realpath("./data/chart/metadata.json"))
TMP_DIR = pathlib.Path(os.path.realpath("./var/tmp"))
LOG_DIR = pathlib.Path(os.path.realpath("./var/log"))
BYTE_PER_CHUNK = 64 * 1024 * 1024
CHART_CHUNK_FILENAME_TEMPLATE = "chart_chunk_{:05d}.tar"

if __name__ == "__main__":
    print(DB_PATH)