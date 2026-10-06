import io
import os
import pathlib
import hashlib
import logging
import shutil
import zipfile
import threading
from collections.abc import Iterator

import pymysql

from . import constant

SCHEMA = [
    """
        CREATE TABLE IF NOT EXISTS song(
            id INT UNSIGNED NOT NULL AUTO_INCREMENT,
            size BIGINT UNSIGNED NOT NULL,
            sha256 CHAR(64) NOT NULL,
            data LONGBLOB NOT NULL,

            PRIMARY KEY (id)
        ) ENGINE=InnoDB DEFAULT CHARSET=ascii COLLATE=ascii_bin
    """,
    """
        CREATE TABLE IF NOT EXISTS chart(
            id CHAR(64) NOT NULL,
            song_id INT UNSIGNED,
            size BIGINT UNSIGNED NOT NULL,

            PRIMARY KEY (id, size),
            FOREIGN KEY (song_id) REFERENCES song(id)
        ) ENGINE=InnoDB DEFAULT CHARSET=ascii COLLATE=ascii_bin
    """,
    """
        CREATE TABLE IF NOT EXISTS chart_chunk(
            id INT UNSIGNED NOT NULL,
            size BIGINT UNSIGNED NOT NULL,
            sha256 CHAR(64) NOT NULL,
            data LONGBLOB NOT NULL,

            PRIMARY KEY (id)
        ) ENGINE=InnoDB DEFAULT CHARSET=ascii COLLATE=ascii_bin
    """,
]


def connect(**kwargs) -> pymysql.connections.Connection:
    """constant.py에 정의된 MySQL 서버에 연결합니다."""
    params = dict(
        host=constant.DB_HOST,
        port=constant.DB_PORT,
        user=constant.DB_USER,
        password=constant.DB_PASSWORD,
        database=constant.DB_NAME,
        charset="utf8mb4",
        max_allowed_packet=constant.DB_MAX_ALLOWED_PACKET,
    )
    params.update(kwargs)
    return pymysql.connect(**params)


class Database:
    _instance = None
    _initialized = False
    _write_lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True

        self.logger = logging.getLogger(__name__)
        self.generate_database()
        with connect() as con, con.cursor() as cur:
            cur.execute("SELECT @@max_allowed_packet")
            self.max_allowed_packet = cur.fetchone()[0]
        if self.max_allowed_packet < constant.BYTE_PER_CHUNK * 2 + constant.PACKET_OVERHEAD:
            # chunk는 BYTE_PER_CHUNK를 넘긴 뒤에 다음 chunk로 넘어가므로 여유있게 2배를 요구합니다.
            self.logger.warning(
                f"max_allowed_packet({self.max_allowed_packet}) is too small. "
                f"Recommended >= {constant.BYTE_PER_CHUNK * 2 + constant.PACKET_OVERHEAD}"
            )

    def generate_database(self) -> None:
        """테이블이 없으면 생성하는 함수입니다."""
        with connect() as con, con.cursor() as cur:
            for com in SCHEMA:
                cur.execute(com)
            con.commit()

    def _fits_packet(self, size: int) -> bool:
        return size + constant.PACKET_OVERHEAD <= self.max_allowed_packet

    def _append_charts_to_chunk(self, cur, chart_files: list[pathlib.Path]) -> None:
        """mutable한 chart chunk에 chart 파일들을 추가합니다. chunk가 BYTE_PER_CHUNK를 넘었다면 새 chunk를 만듭니다."""
        cur.execute("SELECT id, size FROM chart_chunk ORDER BY id DESC LIMIT 1 FOR UPDATE")
        row = cur.fetchone()
        if row is None:
            chunk_no, data = 0, b""
        elif row[1] > constant.BYTE_PER_CHUNK:
            chunk_no, data = row[0] + 1, b""
        else:
            chunk_no = row[0]
            cur.execute("SELECT data FROM chart_chunk WHERE id = %s", (chunk_no,))
            data = cur.fetchone()[0]

        buf = io.BytesIO(data)
        with zipfile.ZipFile(buf, mode="a", compression=zipfile.ZIP_STORED) as zf:
            for chart_file_path in chart_files:
                zf.write(chart_file_path, arcname=chart_file_path.name)
        data = buf.getvalue()

        if not self._fits_packet(len(data)):
            raise RuntimeError(
                f"chart chunk {chunk_no} ({len(data)} bytes) exceeds max_allowed_packet({self.max_allowed_packet})"
            )

        cur.execute(
            """
            INSERT INTO chart_chunk (id, size, sha256, data) VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE size = VALUES(size), sha256 = VALUES(sha256), data = VALUES(data)
            """,
            (chunk_no, len(data), hashlib.sha256(data).hexdigest(), data),
        )

    def insert_song(self, song_path:os.PathLike, remove=False) -> None:
        """BMS 노래 한 곡을 DB에 추가할 수 있는 함수입니다

        Args:
            song_path (os.PathLike): bms 파일을 포함한 에셋들이 담겨있는 폴더의 경로
        """
        root = pathlib.Path(song_path)
        if (not os.path.exists(root)):
            self.logger.warning(f"insert_song failed: Path doesn't exist[{str(root)}]")
            return
        if (not os.path.isdir(root)):
            self.logger.warning(f"insert_song failed: Path isn't directory")
            return
        for file_name in os.listdir(song_path):
            full_path = root / file_name
            if (full_path.suffix.lower() in constant.BMS_FORMAT): break;
        else:
            self.logger.warning(f"insert_song failed: No valid file in folder. Suporting ext: {constant.BMS_FORMAT}")
            return

        with self._write_lock, connect() as con, con.cursor() as cur:
            bms_files = []
            song_id = None
            for file in os.listdir(root):
                file_path = root / file
                if (file_path.suffix.lower() not in constant.BMS_FORMAT): continue

                with open(file_path, "rb") as fos:
                    sha256 = hashlib.sha256(fos.read()).hexdigest()
                size = os.path.getsize(file_path)

                cur.execute(
                    """
                    SELECT song_id
                    FROM chart
                    WHERE id = %s AND size = %s
                    """,
                    (sha256, size)
                )

                row = cur.fetchone()
                if (row is not None):
                    song_id = row[0]

                bms_files.append((file_path, size, sha256))

            try:
                if (song_id is None):
                    data = self.create_zip(root)
                    if not self._fits_packet(len(data)):
                        self.logger.error(
                            f"insert_song failed: song zip ({len(data)} bytes) exceeds "
                            f"max_allowed_packet({self.max_allowed_packet})[{str(root)}]"
                        )
                        return

                    cur.execute(
                        "INSERT INTO song (size, sha256, data) VALUES (%s, %s, %s)",
                        (len(data), hashlib.sha256(data).hexdigest(), data),
                    )
                    song_id = cur.lastrowid

                    self.logger.info(f"insert_song: Inserted new song[{song_id}, {len(data)} bytes]")

                new_charts = []
                for (chart_file_path, size, sha256) in bms_files:
                    cur.execute(
                        "INSERT IGNORE INTO chart (id, song_id, size) VALUES (%s, %s, %s)",
                        (str(sha256), song_id, size),
                    )
                    if cur.rowcount == 1:
                        new_charts.append(chart_file_path)
                if new_charts:
                    self._append_charts_to_chunk(cur, new_charts)
                con.commit()
            except Exception:
                con.rollback()
                raise
            self.logger.info(f"insert_song: Inserted {len(new_charts)} charts.")

        if remove:
            shutil.rmtree(song_path)

    def insert_songs(self, directory:os.PathLike, reculsive=False, remove=False) -> None:
        root_dir = pathlib.Path(directory)
        for file_name in os.listdir(root_dir):
            cur_dir = root_dir / file_name
            if (cur_dir).is_dir() and reculsive:
                self.insert_songs(cur_dir, True, remove)
                continue
            if cur_dir.suffix.lower() in constant.BMS_FORMAT:
                self.insert_song(root_dir)
                if remove:
                    shutil.rmtree(root_dir)
                break

    def create_zip(self, source_dir: os.PathLike) -> bytes:
        """
        디렉터리를 ZIP으로 압축해 bytes로 반환하는 함수
        """

        source_dir = pathlib.Path(source_dir).resolve()

        if not source_dir.exists():
            raise FileNotFoundError(
                f"Source directory does not exist: {source_dir}"
            )

        if not source_dir.is_dir():
            raise NotADirectoryError(
                f"Source path is not a directory: {source_dir}"
            )

        def get_arcname(path: pathlib.Path) -> str:
            return path.relative_to(source_dir).as_posix()

        def make_zipinfo(
            name: str,
            is_dir: bool = False,
        ) -> zipfile.ZipInfo:
            if is_dir and not name.endswith("/"):
                name += "/"

            info = zipfile.ZipInfo(name)

            # Unix permission 정보를 사용하지 않도록 설정
            info.create_system = 0
            info.external_attr = 0

            return info

        buf = io.BytesIO()
        with zipfile.ZipFile(
            buf,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
        ) as zf:

            for path in source_dir.rglob("*"):
                arcname = get_arcname(path)

                if path.is_dir():
                    # 빈 디렉터리는 명시적으로 저장
                    try:
                        next(path.iterdir())
                    except StopIteration:
                        info = make_zipinfo(
                            arcname,
                            is_dir=True,
                        )
                        zf.writestr(info, b"")

                elif path.is_file():
                    info = make_zipinfo(arcname)
                    info.compress_type = zipfile.ZIP_DEFLATED

                    with path.open("rb") as f:
                        zf.writestr(
                            info,
                            f.read(),
                            compress_type=zipfile.ZIP_DEFLATED,
                            compresslevel=6,
                        )

        return buf.getvalue()

    def get_chart_chunk_hash(self) -> dict[int, str]:
        with connect() as con, con.cursor() as cur:
            cur.execute("SELECT id, sha256 FROM chart_chunk ORDER BY id")
            return {row[0]: row[1] for row in cur.fetchall()}

    def get_song_id(self, chart_sha256) -> int | None:
        """chart의 sha256으로 song id를 구하는 함수입니다.

        Args:
            chart_sha256 (_type_):

        Returns:
            int | None: song_id, 존재하지 않는 sha256일 경우 None 반환
        """
        with connect() as con, con.cursor() as cur:
            cur.execute("SELECT song_id FROM chart WHERE id = %s", (chart_sha256,))
            row = cur.fetchone()
            return row[0] if row else None

    def open_blob(self, table: str, row_id: int) -> tuple[int, Iterator[bytes]] | None:
        """song / chart_chunk 테이블의 BLOB을 BLOB_READ_SIZE 단위로 나눠 읽습니다.

        큰 파일을 한 번에 메모리에 올리지 않고, max_allowed_packet보다 큰 응답도 피하기 위함입니다.
        크기와 내용은 같은 스냅샷에서 읽으므로 읽는 도중 chunk가 갱신되어도 일치합니다.

        Returns:
            (BLOB 크기, bytes iterator). 존재하지 않는 id의 경우 None을 반환합니다.
        """
        if table not in ("song", "chart_chunk"):
            raise ValueError(table)
        con = connect()
        try:
            cur = con.cursor()
            cur.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT")
            cur.execute(f"SELECT LENGTH(data) FROM {table} WHERE id = %s", (row_id,))
            row = cur.fetchone()
        except Exception:
            con.close()
            raise
        if row is None:
            con.close()
            return None
        size = row[0]

        def iterator() -> Iterator[bytes]:
            try:
                step = constant.BLOB_READ_SIZE
                for pos in range(1, size + 1, step):
                    cur.execute(
                        f"SELECT SUBSTRING(data, %s, %s) FROM {table} WHERE id = %s",
                        (pos, step, row_id),
                    )
                    yield cur.fetchone()[0]
            finally:
                con.close()

        return size, iterator()
