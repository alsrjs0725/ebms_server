import io
import os
import gzip
import json
import pathlib
import posixpath
import hashlib
import logging
import shutil
import zipfile
import threading
from collections import defaultdict
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
            -- 매니페스트용: 원래 곡 폴더명과 zip 항목 목록(JSON)
            folder VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL DEFAULT '',
            files MEDIUMTEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin,

            PRIMARY KEY (id)
        ) ENGINE=InnoDB DEFAULT CHARSET=ascii COLLATE=ascii_bin
    """,
    """
        CREATE TABLE IF NOT EXISTS chart(
            id CHAR(64) NOT NULL,
            song_id INT UNSIGNED,
            size BIGINT UNSIGNED NOT NULL,
            filename VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL DEFAULT '',

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
    """
        CREATE TABLE IF NOT EXISTS manifest_chunk(
            id INT UNSIGNED NOT NULL,
            -- 압축을 푼 JSON의 sha256
            sha256 CHAR(64) NOT NULL,
            -- gzip으로 압축한 JSON
            data LONGBLOB NOT NULL,

            PRIMARY KEY (id)
        ) ENGINE=InnoDB DEFAULT CHARSET=ascii COLLATE=ascii_bin
    """,
    # 계정. OAuth와 무관하게 내부 UUID로만 구분합니다. 시각은 모두 unix 초(UTC)입니다.
    """
        CREATE TABLE IF NOT EXISTS user(
            id CHAR(36) NOT NULL,
            display_name VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL DEFAULT '',
            email VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin,
            role VARCHAR(16) NOT NULL DEFAULT 'user',
            status VARCHAR(16) NOT NULL DEFAULT 'active',
            created_at BIGINT UNSIGNED NOT NULL,
            last_login_at BIGINT UNSIGNED,
            -- 사용자별 할당량. NULL이면 setting의 전역 기본값을 씁니다.
            max_tickets INT UNSIGNED,
            refill_seconds INT UNSIGNED,
            pre_monthly_bytes BIGINT UNSIGNED,
            pre_throttled_kbps INT UNSIGNED,

            PRIMARY KEY (id)
        ) ENGINE=InnoDB DEFAULT CHARSET=ascii COLLATE=ascii_bin
    """,
    # 계정에 연결된 로그인 수단(1 user : N identity)
    """
        CREATE TABLE IF NOT EXISTS user_identity(
            id INT UNSIGNED NOT NULL AUTO_INCREMENT,
            user_id CHAR(36) NOT NULL,
            oauth VARCHAR(16) NOT NULL,
            oauth_user_id VARCHAR(255) NOT NULL,
            email VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin,
            name VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL DEFAULT '',
            linked_at BIGINT UNSIGNED NOT NULL,

            PRIMARY KEY (id),
            UNIQUE (oauth, oauth_user_id),
            FOREIGN KEY (user_id) REFERENCES user(id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=ascii COLLATE=ascii_bin
    """,
    # 웹 쿠키와 클라이언트 세션키. 원문 대신 sha256만 저장합니다.
    """
        CREATE TABLE IF NOT EXISTS session(
            id INT UNSIGNED NOT NULL AUTO_INCREMENT,
            token_sha256 CHAR(64) NOT NULL,
            user_id CHAR(36) NOT NULL,
            kind VARCHAR(16) NOT NULL,
            device_name VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL DEFAULT '',
            created_at BIGINT UNSIGNED NOT NULL,
            last_used_at BIGINT UNSIGNED NOT NULL,
            expires_at BIGINT UNSIGNED NOT NULL,
            revoked_at BIGINT UNSIGNED,

            PRIMARY KEY (id),
            UNIQUE (token_sha256),
            FOREIGN KEY (user_id) REFERENCES user(id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=ascii COLLATE=ascii_bin
    """,
    # 클라이언트 로그인용 1회용 코드. 세션키로 바꿀 때 PKCE(code_verifier)가 맞아야 합니다.
    """
        CREATE TABLE IF NOT EXISTS auth_code(
            id INT UNSIGNED NOT NULL AUTO_INCREMENT,
            code_sha256 CHAR(64) NOT NULL,
            user_id CHAR(36) NOT NULL,
            code_challenge VARCHAR(128) NOT NULL,
            redirect_uri VARCHAR(255) NOT NULL,
            device_name VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL DEFAULT '',
            created_at BIGINT UNSIGNED NOT NULL,
            expires_at BIGINT UNSIGNED NOT NULL,
            used_at BIGINT UNSIGNED,

            PRIMARY KEY (id),
            UNIQUE (code_sha256),
            FOREIGN KEY (user_id) REFERENCES user(id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=ascii COLLATE=ascii_bin
    """,
    # 할당량 전역 기본값. 관리자 페이지에서 수정합니다. 값은 정수 문자열입니다.
    """
        CREATE TABLE IF NOT EXISTS setting(
            name VARCHAR(64) NOT NULL,
            value VARCHAR(255) NOT NULL,

            PRIMARY KEY (name)
        ) ENGINE=InnoDB DEFAULT CHARSET=ascii COLLATE=ascii_bin
    """,
    # 남은 플레이 티켓. 요청 때 경과 시간만큼 채워 계산합니다(타이머 없음). 행이 없으면 가득 찬 상태입니다.
    """
        CREATE TABLE IF NOT EXISTS user_ticket(
            user_id CHAR(36) NOT NULL,
            tickets DOUBLE NOT NULL,
            updated_at BIGINT UNSIGNED NOT NULL,

            PRIMARY KEY (user_id),
            FOREIGN KEY (user_id) REFERENCES user(id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=ascii COLLATE=ascii_bin
    """,
    # 티켓을 쓴 곡. expires_at 전에는 같은 곡을 다시 받아도 차감하지 않습니다.
    """
        CREATE TABLE IF NOT EXISTS download_grant(
            user_id CHAR(36) NOT NULL,
            song_id INT UNSIGNED NOT NULL,
            charged_at BIGINT UNSIGNED NOT NULL,
            expires_at BIGINT UNSIGNED NOT NULL,

            PRIMARY KEY (user_id, song_id),
            FOREIGN KEY (user_id) REFERENCES user(id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=ascii COLLATE=ascii_bin
    """,
    # 월별 사전 다운로드 사용량(실제 전송한 바이트). month는 KST 기준 YYYY-MM입니다.
    """
        CREATE TABLE IF NOT EXISTS pre_usage(
            user_id CHAR(36) NOT NULL,
            month CHAR(7) NOT NULL,
            bytes BIGINT UNSIGNED NOT NULL DEFAULT 0,

            PRIMARY KEY (user_id, month),
            FOREIGN KEY (user_id) REFERENCES user(id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=ascii COLLATE=ascii_bin
    """,
]

# 이전 버전에서 만든 테이블에 없는 컬럼
MISSING_COLUMNS = [
    ("song", "folder", "VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL DEFAULT ''"),
    ("song", "files", "MEDIUMTEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin"),
    ("chart", "filename", "VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL DEFAULT ''"),
    ("user", "max_tickets", "INT UNSIGNED"),
    ("user", "refill_seconds", "INT UNSIGNED"),
    ("user", "pre_monthly_bytes", "BIGINT UNSIGNED"),
    ("user", "pre_throttled_kbps", "INT UNSIGNED"),
]


# 사전 파일을 가리키는 차트 헤더와, 파일 확장자가 달라도 같은 파일로 보는 종류(BMS 플레이어의 확장자 대체)
PRE_HEADERS = {
    b"#BANNER": constant.IMAGE_FORMAT,
    b"#STAGEFILE": constant.IMAGE_FORMAT,
    b"#BACKBMP": constant.IMAGE_FORMAT,
    b"#PREVIEW": constant.AUDIO_FORMAT,
}
# 차트 헤더 값(파일명)의 인코딩 후보
CHART_ENCODINGS = ("utf-8", "cp932", "cp949")


def _header_targets(chart: bytes, base: str) -> list[tuple[str, tuple[str, ...]]]:
    """차트 헤더가 가리키는 파일의 (소문자 경로, 대체 가능한 확장자)를 반환합니다. base는 차트가 있는 폴더입니다."""
    targets = []
    for line in chart.splitlines():
        line = line.strip()
        for header, formats in PRE_HEADERS.items():
            if line[:len(header)].upper() != header or line[len(header):len(header) + 1] not in (b" ", b"\t"):
                continue
            value = line[len(header):].strip()
            for encoding in CHART_ENCODINGS:
                try:
                    name = value.decode(encoding)
                except UnicodeDecodeError:
                    continue
                path = posixpath.normpath(posixpath.join(base, name.replace("\\", "/"))).lower()
                targets.append((path, formats))
    return targets


def file_kinds(zf: zipfile.ZipFile) -> dict[str, str]:
    """곡 zip 항목별 다운로드 구분. pre(사전): 차트, 차트 헤더 #BANNER·#STAGEFILE·#BACKBMP·#PREVIEW가
    가리키는 파일, preview*로 시작하는 파일. play(플레이): 나머지(키음, BGA 등)."""
    infos = [info for info in zf.infolist() if not info.is_dir()]
    exact: set[str] = set()
    by_stem: dict[str, set[str]] = defaultdict(set)
    for info in infos:
        path = pathlib.PurePosixPath(info.filename)
        if path.suffix.lower() not in constant.BMS_FORMAT:
            continue
        for target, formats in _header_targets(zf.read(info), path.parent.as_posix()):
            exact.add(target)
            by_stem[posixpath.splitext(target)[0]].update(formats)

    kinds = {}
    for info in infos:
        lower = info.filename.lower()
        stem, ext = posixpath.splitext(lower)
        pre = (
            ext in constant.BMS_FORMAT
            or posixpath.basename(lower).startswith("preview")
            or lower in exact
            or ext in by_stem.get(stem, ())
        )
        kinds[info.filename] = "pre" if pre else "play"
    return kinds


def zip_entries(data: bytes) -> list[dict]:
    """zip 안의 파일 항목을 매니페스트 형식으로 반환합니다. offset은 local file header의 위치입니다."""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        kinds = file_kinds(zf)
        return [
            {
                "path": info.filename,
                "size": info.file_size,
                "offset": info.header_offset,
                "comp_size": info.compress_size,
                "crc32": f"{info.CRC:08x}",
                "method": info.compress_type,
                "kind": kinds[info.filename],
            }
            for info in zf.infolist()
            if not info.is_dir()
        ]


def chart_arcname(sha256: str, path: pathlib.PurePath) -> str:
    """chart chunk 안의 항목 이름. 곡마다 같은 파일명이 있을 수 있어 sha256을 이름으로 씁니다."""
    return f"{sha256}{path.suffix.lower()}"


class BlobReader:
    """한 스냅샷에서 BLOB을 나눠 읽습니다. iter_range를 끝까지 돌거나 close를 호출하면 연결을 닫습니다."""

    def __init__(self, con, cur, table: str, row_id: int, size: int, sha256: str):
        self._con = con
        self._cur = cur
        self.table = table
        self.row_id = row_id
        self.size = size
        self.sha256 = sha256

    def read(self, start: int, length: int) -> bytes:
        """start부터 length 바이트를 읽습니다. 연결은 닫지 않습니다."""
        if self.table == "song":
            self._cur.execute("SELECT SUBSTRING(data, %s, %s) FROM song WHERE id = %s", (start + 1, length, self.row_id))
        else:
            raise ValueError(self.table)
        return self._cur.fetchone()[0]

    def iter_range(self, start: int, end: int) -> Iterator[bytes]:
        """[start, end] 바이트(양끝 포함)를 BLOB_READ_SIZE 단위로 읽습니다."""
        try:
            step = constant.BLOB_READ_SIZE
            for pos in range(start + 1, end + 2, step):
                if self.table == "song":
                    self._cur.execute(
                        "SELECT SUBSTRING(data, %s, %s) FROM song WHERE id = %s",
                        (pos, min(step, end + 2 - pos), self.row_id),
                    )
                elif self.table == "chart_chunk":
                    self._cur.execute(
                        "SELECT SUBSTRING(data, %s, %s) FROM chart_chunk WHERE id = %s",
                        (pos, min(step, end + 2 - pos), self.row_id),
                    )
                else:
                    raise ValueError(self.table)
                yield self._cur.fetchone()[0]
        finally:
            self.close()

    def close(self) -> None:
        if self._con.open:
            self._con.close()


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
        self.backfill_manifest()
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
            for table, column, definition in MISSING_COLUMNS:
                cur.execute(
                    """
                    SELECT COUNT(*) FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s
                    """,
                    (table, column),
                )
                if cur.fetchone()[0] == 0:
                    cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                    self.logger.info(f"generate_database: Added column {table}.{column}")
            # 할당량 전역 기본값. 이미 있는 값(관리자가 바꾼 값)은 그대로 둡니다.
            for name, value in constant.DEFAULT_SETTINGS.items():
                cur.execute("INSERT IGNORE INTO setting (name, value) VALUES (%s, %s)", (name, str(value)))
            con.commit()

    def _fits_packet(self, size: int) -> bool:
        return size + constant.PACKET_OVERHEAD <= self.max_allowed_packet

    def _append_charts_to_chunk(self, cur, chart_files: list[tuple[pathlib.Path, str]]) -> None:
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
            for chart_file_path, sha256 in chart_files:
                zf.write(chart_file_path, arcname=chart_arcname(sha256, chart_file_path))
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

    def _rebuild_manifest_chunk(self, cur, chunk_no: int) -> None:
        """chunk_no에 속한 곡들의 매니페스트를 다시 만들어 저장합니다."""
        lo = chunk_no * constant.SONGS_PER_MANIFEST_CHUNK
        hi = lo + constant.SONGS_PER_MANIFEST_CHUNK
        cur.execute(
            "SELECT song_id, id, filename, size FROM chart WHERE song_id >= %s AND song_id < %s ORDER BY song_id, id",
            (lo, hi),
        )
        charts = defaultdict(list)
        chart_files = defaultdict(list)
        for song_id, chart_id, filename, size in cur.fetchall():
            charts[song_id].append(chart_id)
            path = filename if filename else f"{chart_id}.bms"
            chart_files[song_id].append(
                {
                    "sha256": chart_id,
                    "path": path,
                    "size": size,
                }
            )

        cur.execute(
            "SELECT id, folder, size, sha256, files FROM song WHERE id >= %s AND id < %s ORDER BY id",
            (lo, hi),
        )
        songs = [
            {
                "song_id": song_id,
                "folder": folder,
                "zip_size": size,
                "zip_sha256": sha256,
                "charts": charts[song_id],
                "chart_files": chart_files[song_id],
                "files": json.loads(files) if files else [],
            }
            for song_id, folder, size, sha256, files in cur.fetchall()
        ]
        body = json.dumps(songs, ensure_ascii=False, separators=(",", ":")).encode()
        data = gzip.compress(body, mtime=0)
        cur.execute(
            """
            INSERT INTO manifest_chunk (id, sha256, data) VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE sha256 = VALUES(sha256), data = VALUES(data)
            """,
            (chunk_no, hashlib.sha256(body).hexdigest(), data),
        )

    def backfill_manifest(self) -> None:
        """매니페스트 정보가 없는 곡(이전 버전에서 넣은 곡)의 항목 목록을 song zip에서 채웁니다.
        원래 폴더명은 알 수 없으므로 song id를 폴더명으로 씁니다. 항목에 kind(pre/play)가 없는 곡도 다시 채웁니다.
        또한 chart 테이블에 filename이 채워지지 않은 항목의 파일명을 song zip에서 추출해 채웁니다."""
        with self._write_lock, connect() as con, con.cursor() as cur:
            cur.execute("SELECT id FROM song WHERE files IS NULL OR files NOT LIKE %s ORDER BY id", ('%"kind"%',))
            song_ids_no_files = set(row[0] for row in cur.fetchall())

            cur.execute("SELECT DISTINCT song_id FROM chart WHERE filename = '' AND song_id IS NOT NULL")
            song_ids_no_filenames = set(row[0] for row in cur.fetchall())

            affected_song_ids = sorted(song_ids_no_files | song_ids_no_filenames)
            if not affected_song_ids:
                return
            try:
                for song_id in affected_song_ids:
                    cur.execute("SELECT data FROM song WHERE id = %s FOR UPDATE", (song_id,))
                    row = cur.fetchone()
                    if not row:
                        continue
                    song_data = row[0]
                    if song_id in song_ids_no_files:
                        files = zip_entries(song_data)
                        cur.execute(
                            "UPDATE song SET folder = IF(folder = '', %s, folder), files = %s WHERE id = %s",
                            (str(song_id), json.dumps(files), song_id),
                        )
                    if song_id in song_ids_no_filenames:
                        with zipfile.ZipFile(io.BytesIO(song_data)) as zf:
                            for info in zf.infolist():
                                if not info.is_dir() and pathlib.PurePosixPath(info.filename).suffix.lower() in constant.BMS_FORMAT:
                                    content = zf.read(info)
                                    sha256 = hashlib.sha256(content).hexdigest()
                                    cur.execute(
                                        "UPDATE chart SET filename = %s WHERE song_id = %s AND id = %s AND filename = ''",
                                        (info.filename, song_id, sha256),
                                    )
                for chunk_no in sorted({i // constant.SONGS_PER_MANIFEST_CHUNK for i in affected_song_ids}):
                    self._rebuild_manifest_chunk(cur, chunk_no)
                con.commit()
            except Exception:
                con.rollback()
                raise
            self.logger.info(f"backfill_manifest: Updated manifest of {len(affected_song_ids)} songs.")

    def migrate_chart_chunk_names(self) -> int:
        """chart chunk 안의 항목 이름을 원래 파일명에서 {sha256}{ext}로 바꿉니다. 바뀐 chunk 수를 반환합니다.

        같은 이름의 항목이 여러 개 있어도 항목 순서대로 내용을 읽어 각각의 해시로 이름을 붙입니다.
        """
        changed = 0
        with self._write_lock, connect() as con, con.cursor() as cur:
            cur.execute("SELECT id FROM chart_chunk ORDER BY id")
            chunk_ids = [row[0] for row in cur.fetchall()]
            try:
                for chunk_no in chunk_ids:
                    cur.execute("SELECT data FROM chart_chunk WHERE id = %s FOR UPDATE", (chunk_no,))
                    old = cur.fetchone()[0]
                    buf = io.BytesIO()
                    renamed = False
                    with zipfile.ZipFile(io.BytesIO(old)) as src, \
                            zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_STORED) as dst:
                        for info in src.infolist():
                            content = src.read(info)
                            name = chart_arcname(
                                hashlib.sha256(content).hexdigest(), pathlib.PurePosixPath(info.filename)
                            )
                            renamed |= name != info.filename
                            dst.writestr(zipfile.ZipInfo(name, date_time=info.date_time), content)
                    if not renamed:
                        continue
                    data = buf.getvalue()
                    cur.execute(
                        "UPDATE chart_chunk SET size = %s, sha256 = %s, data = %s WHERE id = %s",
                        (len(data), hashlib.sha256(data).hexdigest(), data, chunk_no),
                    )
                    changed += 1
                con.commit()
            except Exception:
                con.rollback()
                raise
        self.logger.info(f"migrate_chart_chunk_names: Rewrote {changed} of {len(chunk_ids)} chunks.")
        return changed

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
            self.logger.warning("insert_song failed: Path isn't directory")
            return
        for file_name in os.listdir(song_path):
            full_path = root / file_name
            if (full_path.suffix.lower() in constant.BMS_FORMAT): break
        else:
            self.logger.warning(f"insert_song failed: No valid file in folder. Suporting ext: {constant.BMS_FORMAT}")
            return

        with self._write_lock, connect() as con, con.cursor() as cur:
            bms_files, song_id = self._find_bms_files_and_existing_song(root, cur)

            new_song = False
            try:
                if (song_id is None):
                    song_id = self._insert_new_song(root, cur)
                    if song_id is None:
                        return
                    new_song = True

                new_charts = self._insert_or_update_charts(root, bms_files, song_id, cur)

                if new_charts:
                    self._append_charts_to_chunk(cur, new_charts)
                if new_song or new_charts:
                    self._rebuild_manifest_chunk(cur, song_id // constant.SONGS_PER_MANIFEST_CHUNK)
                con.commit()
            except Exception:
                con.rollback()
                raise
            self.logger.info(f"insert_song: Inserted {len(new_charts)} charts.")

        if remove:
            shutil.rmtree(song_path)

    def _find_bms_files_and_existing_song(self, root: pathlib.Path, cur) -> tuple[list, int | None]:
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
        return bms_files, song_id

    def _insert_new_song(self, root: pathlib.Path, cur) -> int | None:
        data = self.create_zip(root)
        if not self._fits_packet(len(data)):
            self.logger.error(
                f"insert_song failed: song zip ({len(data)} bytes) exceeds "
                f"max_allowed_packet({self.max_allowed_packet})[{str(root)}]"
            )
            return None

        cur.execute(
            "INSERT INTO song (size, sha256, data, folder, files) VALUES (%s, %s, %s, %s, %s)",
            (
                len(data),
                hashlib.sha256(data).hexdigest(),
                data,
                root.resolve().name,
                json.dumps(zip_entries(data)),
            ),
        )
        song_id = cur.lastrowid
        self.logger.info(f"insert_song: Inserted new song[{song_id}, {len(data)} bytes]")
        return song_id

    def _insert_or_update_charts(self, root: pathlib.Path, bms_files: list, song_id: int, cur) -> list:
        new_charts = []
        for (chart_file_path, size, sha256) in bms_files:
            filename = chart_file_path.relative_to(root).as_posix()
            cur.execute(
                "INSERT IGNORE INTO chart (id, song_id, size, filename) VALUES (%s, %s, %s, %s)",
                (str(sha256), song_id, size, filename),
            )
            if cur.rowcount == 1:
                new_charts.append((chart_file_path, sha256))
            elif song_id is not None:
                cur.execute(
                    "UPDATE chart SET filename = %s WHERE id = %s AND size = %s AND filename = ''",
                    (filename, str(sha256), size),
                )
        return new_charts

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

        def _add_entry(path: pathlib.Path, zf: zipfile.ZipFile) -> None:
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

        buf = io.BytesIO()
        with zipfile.ZipFile(
            buf,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
        ) as zf:

            for path in source_dir.rglob("*"):
                _add_entry(path, zf)

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

    def get_song_files(self, song_id: int) -> list[dict] | None:
        """곡 zip의 항목 목록(매니페스트의 files). 없는 곡이면 None."""
        with connect() as con, con.cursor() as cur:
            cur.execute("SELECT files FROM song WHERE id = %s", (song_id,))
            row = cur.fetchone()
        if row is None:
            return None
        return json.loads(row[0]) if row[0] else []

    def get_manifest_hash(self) -> dict[int, str]:
        with connect() as con, con.cursor() as cur:
            cur.execute("SELECT id, sha256 FROM manifest_chunk ORDER BY id")
            return {row[0]: row[1] for row in cur.fetchall()}

    def get_manifest_chunk(self, chunk_no: int) -> bytes | None:
        """gzip으로 압축된 매니페스트 JSON을 반환합니다. 없는 chunk면 None."""
        with connect() as con, con.cursor() as cur:
            cur.execute("SELECT data FROM manifest_chunk WHERE id = %s", (chunk_no,))
            row = cur.fetchone()
            return row[0] if row else None

    def open_blob(self, table: str, row_id: int) -> BlobReader | None:
        """song / chart_chunk 테이블의 BLOB을 BLOB_READ_SIZE 단위로 나눠 읽을 준비를 합니다.

        큰 파일을 한 번에 메모리에 올리지 않고, max_allowed_packet보다 큰 응답도 피하기 위함입니다.
        크기, 해시, 내용은 같은 스냅샷에서 읽으므로 읽는 도중 chunk가 갱신되어도 일치합니다.

        Returns:
            BlobReader. 존재하지 않는 id의 경우 None을 반환합니다.
        """
        if table not in ("song", "chart_chunk"):
            raise ValueError(table)
        con = connect()
        try:
            cur = con.cursor()
            cur.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT")
            if table == "song":
                cur.execute("SELECT LENGTH(data), sha256 FROM song WHERE id = %s", (row_id,))
            elif table == "chart_chunk":
                cur.execute("SELECT LENGTH(data), sha256 FROM chart_chunk WHERE id = %s", (row_id,))
            row = cur.fetchone()
        except Exception:
            con.close()
            raise
        if row is None:
            con.close()
            return None
        return BlobReader(con, cur, table, row_id, row[0], row[1])
