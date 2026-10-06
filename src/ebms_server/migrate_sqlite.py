"""기존 SQLite DB(var/ebms.db)의 데이터를 MySQL로 옮기는 일회성 스크립트입니다.

사용법:
    python -m ebms_server.migrate_sqlite [sqlite_db_path]

MySQL 접속 정보는 EBMS_DB_* 환경변수(constant.py 참고)로 지정합니다.
song.id를 그대로 유지하므로 대상 MySQL 테이블은 비어 있어야 합니다.
"""
import logging
import pathlib
import sqlite3
import sys

from . import constant
from .db import SCHEMA, connect

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("migrate_sqlite")


def migrate(sqlite_path: pathlib.Path) -> None:
    if not sqlite_path.exists():
        raise FileNotFoundError(f"SQLite DB not found: {sqlite_path}")

    src = sqlite3.connect(sqlite_path)
    songs = src.execute("SELECT id, path FROM song ORDER BY id").fetchall()
    charts = src.execute("SELECT id, song_id, size FROM chart").fetchall()
    src.close()
    logger.info(f"Read {len(songs)} songs, {len(charts)} charts from {sqlite_path}")

    con = connect()
    with con, con.cursor() as cur:
        for com in SCHEMA:
            cur.execute(com)

        cur.execute("SELECT (SELECT COUNT(*) FROM song) + (SELECT COUNT(*) FROM chart)")
        if cur.fetchone()[0] > 0:
            raise RuntimeError("MySQL song/chart tables are not empty. Aborting.")

        try:
            cur.executemany("INSERT INTO song (id, path) VALUES (%s, %s)", songs)
            cur.executemany(
                "INSERT INTO chart (id, song_id, size) VALUES (%s, %s, %s)",
                charts,
            )
            con.commit()
        except Exception:
            con.rollback()
            raise

        cur.execute("SELECT COUNT(*) FROM song")
        song_cnt = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM chart")
        chart_cnt = cur.fetchone()[0]

    if (song_cnt, chart_cnt) != (len(songs), len(charts)):
        raise RuntimeError(f"Row count mismatch: song {song_cnt}, chart {chart_cnt}")
    logger.info(f"Migrated {song_cnt} songs, {chart_cnt} charts to MySQL")


if __name__ == "__main__":
    migrate(pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else constant.DB_PATH)
