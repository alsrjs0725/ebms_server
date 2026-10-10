import io
import os
import gzip
import json
import pathlib
import posixpath
import re
import hashlib
import logging
import shutil
import stat
import struct
import zipfile
import threading
import zlib
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
            data LONGBLOB,
            -- 매니페스트용: 원래 곡 폴더명과 zip 항목 목록(JSON)
            folder VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL DEFAULT '',
            files MEDIUMTEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin,

            PRIMARY KEY (id)
        ) ENGINE=InnoDB DEFAULT CHARSET=ascii COLLATE=ascii_bin
    """,
    """
        CREATE TABLE IF NOT EXISTS song_part(
            song_id INT UNSIGNED NOT NULL,
            seq INT UNSIGNED NOT NULL,
            data LONGBLOB NOT NULL,

            PRIMARY KEY (song_id, seq),
            FOREIGN KEY (song_id) REFERENCES song(id) ON DELETE CASCADE
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
    # 사전 청크. song id 구간(SONGS_PER_PRE_CHUNK)마다 곡들의 사전 파일(차트 제외)을 무압축 zip으로 묶습니다.
    # 항목 이름은 "{song_id}/{곡 zip 안 경로}"이고, 데이터는 pre_chunk_part에 BLOB_READ_SIZE씩 나눠 둡니다.
    """
        CREATE TABLE IF NOT EXISTS pre_chunk(
            id INT UNSIGNED NOT NULL,
            size BIGINT UNSIGNED NOT NULL,
            sha256 CHAR(64) NOT NULL,

            PRIMARY KEY (id)
        ) ENGINE=InnoDB DEFAULT CHARSET=ascii COLLATE=ascii_bin
    """,
    """
        CREATE TABLE IF NOT EXISTS pre_chunk_part(
            chunk_id INT UNSIGNED NOT NULL,
            seq INT UNSIGNED NOT NULL,
            data LONGBLOB NOT NULL,

            PRIMARY KEY (chunk_id, seq)
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
    # 서버 내부 값(file_kinds_version)도 여기 둡니다.
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
    # 클라이언트에 띄울 공지. starts_at·ends_at이 NULL이면 제한 없음입니다.
    """
        CREATE TABLE IF NOT EXISTS notice(
            id INT UNSIGNED NOT NULL AUTO_INCREMENT,
            title VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            body MEDIUMTEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            level VARCHAR(16) NOT NULL DEFAULT 'info',
            published TINYINT NOT NULL DEFAULT 1,
            starts_at BIGINT UNSIGNED,
            ends_at BIGINT UNSIGNED,
            created_at BIGINT UNSIGNED NOT NULL,
            updated_at BIGINT UNSIGNED NOT NULL,

            PRIMARY KEY (id)
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


# 사전 파일을 가리키는 차트 헤더와 그 헤더가 받는 형식. 헤더 값의 확장자가 이 형식일 때만 인정하고,
# 실제 파일 확장자가 달라도 같은 형식 안이면 같은 파일로 봅니다(BMS 플레이어의 확장자 대체).
PRE_HEADERS = {
    b"#BANNER": constant.IMAGE_FORMAT,
    b"#STAGEFILE": constant.IMAGE_FORMAT,
    b"#BACKBMP": constant.IMAGE_FORMAT,
    b"#PREVIEW": constant.AUDIO_FORMAT,
}
# 키음(#WAVxx)·BGA(#BMPxx) 정의. 여기서 가리키는 파일은 사전 헤더가 함께 가리키지 않는 한 pre가 아닙니다.
PLAY_HEADER = re.compile(rb"#(?:WAV|BMP)[0-9A-Z]{2}[ \t]", re.IGNORECASE)
# 차트 헤더 값(파일명)의 인코딩 후보
CHART_ENCODINGS = ("utf-8", "cp932", "cp949")
# pre/play 판정 규칙 버전. 규칙을 바꾸면 올리고, 서버 시작 시 이전 규칙으로 판정한 곡을 다시 판정합니다.
FILE_KINDS_VERSION = 2
# FILE_KINDS_VERSION을 적용한 버전을 저장하는 setting 이름
FILE_KINDS_SETTING = "file_kinds_version"


def _header_paths(value: bytes, base: str) -> list[str]:
    """차트 헤더 값을 인코딩 후보마다 풀어 base 기준 소문자 경로로 만듭니다."""
    paths = []
    for encoding in CHART_ENCODINGS:
        try:
            name = value.decode(encoding)
        except UnicodeDecodeError:
            continue
        paths.append(posixpath.normpath(posixpath.join(base, name.replace("\\", "/"))).lower())
    return paths


def _chart_refs(chart: bytes, base: str) -> tuple[list[tuple[str, tuple[str, ...]]], set[str]]:
    """차트가 가리키는 파일. (사전 헤더의 (소문자 경로, 대체 가능한 확장자) 목록, 키음·BGA의 소문자 확장자 뺀 경로)를
    반환합니다. 사전 헤더 값의 확장자가 그 헤더의 형식이 아니면(확장자 없음 포함) 무시합니다. base는 차트가 있는 폴더입니다."""
    pre_targets = []
    play_stems = set()
    for line in chart.splitlines():
        line = line.strip()
        if PLAY_HEADER.match(line):
            for path in _header_paths(line[6:].strip(), base):
                play_stems.add(posixpath.splitext(path)[0])
            continue
        for header, formats in PRE_HEADERS.items():
            if line[:len(header)].upper() != header or line[len(header):len(header) + 1] not in (b" ", b"\t"):
                continue
            for path in _header_paths(line[len(header):].strip(), base):
                if posixpath.splitext(path)[1] in formats:
                    pre_targets.append((path, formats))
    return pre_targets, play_stems


def kinds_from_charts(paths: list[str], charts: dict[str, bytes]) -> dict[str, str]:
    """곡 zip 항목별 다운로드 구분. paths는 파일 경로, charts는 그중 차트 파일의 {경로: 내용}입니다.

    pre(사전): 차트, 차트 헤더 #BANNER·#STAGEFILE·#BACKBMP(이미지)·#PREVIEW(오디오)가 가리키는 파일,
    차트와 같은 폴더의 preview*로 시작하는 오디오 파일(키음·BGA로 쓰는 파일 제외).
    play(플레이): 나머지(키음, BGA 등).
    """
    by_stem: dict[str, set[str]] = defaultdict(set)
    play_stems: set[str] = set()
    chart_dirs: set[str] = set()
    for path, content in charts.items():
        base = posixpath.dirname(path)
        chart_dirs.add(base.lower())
        targets, stems = _chart_refs(content, base)
        for target, formats in targets:
            by_stem[posixpath.splitext(target)[0]].update(formats)
        play_stems |= stems

    kinds = {}
    for path in paths:
        lower = path.lower()
        stem, ext = posixpath.splitext(lower)
        preview = (
            posixpath.basename(lower).startswith("preview")
            and ext in constant.AUDIO_FORMAT
            and posixpath.dirname(lower) in chart_dirs
            and stem not in play_stems
        )
        pre = ext in constant.BMS_FORMAT or ext in by_stem.get(stem, ()) or preview
        kinds[path] = "pre" if pre else "play"
    return kinds


def file_kinds(zf: zipfile.ZipFile) -> dict[str, str]:
    """곡 zip 항목별 다운로드 구분(kinds_from_charts 참고)."""
    paths = [info.filename for info in zf.infolist() if not info.is_dir()]
    charts = {
        path: zf.read(path) for path in paths if pathlib.PurePosixPath(path).suffix.lower() in constant.BMS_FORMAT
    }
    return kinds_from_charts(paths, charts)


def zip_entries(data: bytes) -> list[dict]:
    """zip 안의 파일 항목을 매니페스트 형식으로 반환합니다. offset은 local file header의 위치입니다."""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        kinds = file_kinds(zf)
        entries = []
        for info in zf.infolist():
            if info.is_dir():
                continue
            head = data[info.header_offset : info.header_offset + 30]
            data_offset = info.header_offset + 30
            if len(head) == 30 and head[:4] == b"PK\x03\x04":
                name_len, extra_len = struct.unpack("<HH", head[26:30])
                data_offset = info.header_offset + 30 + name_len + extra_len
            entries.append(
                {
                    "path": info.filename,
                    "size": info.file_size,
                    "offset": info.header_offset,
                    "data_offset": data_offset,
                    "comp_size": info.compress_size,
                    "crc32": f"{info.CRC:08x}",
                    "method": info.compress_type,
                    "kind": kinds[info.filename],
                }
            )
        return entries


def pre_arcname(song_id: int, path: str) -> str:
    """사전 청크 안의 항목 이름."""
    return f"{song_id}/{path}"


def is_pre_chunk_entry(entry: dict) -> bool:
    """사전 청크에 넣을 항목인지. 차트는 차트 청크로 받으므로 뺍니다."""
    return (
        entry.get("kind") == "pre"
        and pathlib.PurePosixPath(entry["path"]).suffix.lower() not in constant.BMS_FORMAT
    )


def chart_arcname(sha256: str, path: pathlib.PurePath) -> str:
    """chart chunk 안의 항목 이름. 곡마다 같은 파일명이 있을 수 있어 sha256을 이름으로 씁니다."""
    return f"{sha256}{path.suffix.lower()}"


def _safe_entry(path: pathlib.Path, root: pathlib.Path, is_dir: bool) -> bool:
    """곡 폴더(root, resolve된 경로) 안의 항목을 곡에 넣어도 되는지 봅니다.

    심볼릭 링크는 따라가지 않고 건너뜁니다. 링크가 서버의 다른 파일(/proc/self/environ 등)을 가리키면
    그 내용이 곡 zip·차트 청크에 들어가 배포되기 때문입니다. 일반 파일·폴더가 아니거나(FIFO 등)
    실제 경로가 곡 폴더 밖이면 역시 건너뜁니다. 건너뛴 항목은 경고 로그를 남깁니다.
    """
    logger = logging.getLogger(__name__)
    try:
        mode = os.lstat(path).st_mode
    except OSError as e:
        logger.warning(f"import: skipped unreadable entry[{path}]: {e}")
        return False
    if stat.S_ISLNK(mode):
        logger.warning(f"import: skipped symlink[{path}]")
        return False
    if not (stat.S_ISDIR(mode) if is_dir else stat.S_ISREG(mode)):
        logger.warning(f"import: skipped non-regular entry[{path}]")
        return False
    if not path.resolve().is_relative_to(root):
        logger.warning(f"import: skipped entry outside song folder[{path}]")
        return False
    return True


def song_dir_entries(source_dir: os.PathLike) -> list[tuple[pathlib.Path, bool]]:
    """곡 폴더 안의 (경로, 폴더 여부) 목록. 심볼릭 링크는 파일·폴더 모두 따라가지 않고 건너뜁니다."""
    root = pathlib.Path(source_dir).resolve()
    entries = []
    for dirpath, dirnames, filenames in os.walk(root):  # followlinks=False
        base = pathlib.Path(dirpath)
        keep = []
        for name in sorted(dirnames):
            if _safe_entry(base / name, root, is_dir=True):
                keep.append(name)
                entries.append((base / name, True))
        dirnames[:] = keep
        for name in sorted(filenames):
            if _safe_entry(base / name, root, is_dir=False):
                entries.append((base / name, False))
    return entries


def chart_files(source_dir: os.PathLike) -> list[pathlib.Path]:
    """곡 폴더 바로 아래의 차트 파일. 심볼릭 링크 등 곡에 넣지 않는 항목은 뺍니다."""
    root = pathlib.Path(source_dir)
    resolved = root.resolve()
    return [
        root / name
        for name in sorted(os.listdir(root))
        if pathlib.Path(name).suffix.lower() in constant.BMS_FORMAT
        and _safe_entry(root / name, resolved, is_dir=False)
    ]


def _song_zipinfo(name: str) -> zipfile.ZipInfo:
    """곡 zip 파일 항목. create_zip과 같은 형식(권한 정보 없음, 날짜 고정)입니다."""
    info = zipfile.ZipInfo(name)
    info.create_system = 0
    info.external_attr = 0
    info.compress_type = zipfile.ZIP_DEFLATED
    return info


class AmbiguousSongError(ValueError):
    """새 폴더의 차트가 기존 곡 여러 개와 겹쳐 어느 곡에 넣을지 정할 수 없습니다. 등록하지 않고 폴더는 남깁니다."""


class BlobReader:
    """BLOB을 나눠 읽습니다. 전송 중에 연결을 유지하지 않고 매 chunk 읽기마다 단기 연결을 씁니다."""

    def __init__(self, table: str, row_id: int, size: int, sha256: str):
        self.table = table
        self.row_id = row_id
        self.size = size
        self.sha256 = sha256

    def read(self, start: int, length: int) -> bytes:
        """start부터 length 바이트를 읽습니다."""
        if self.table == "song":
            start_seq = start // constant.BLOB_READ_SIZE
            end_seq = (start + length - 1) // constant.BLOB_READ_SIZE
            with connect() as con, con.cursor() as cur:
                cur.execute(
                    "SELECT seq, data FROM song_part WHERE song_id = %s AND seq >= %s AND seq <= %s ORDER BY seq",
                    (self.row_id, start_seq, end_seq),
                )
                rows = cur.fetchall()
                if rows:
                    buf = b"".join(r[1] for r in rows)
                    offset_in_buf = start - start_seq * constant.BLOB_READ_SIZE
                    return buf[offset_in_buf : offset_in_buf + length]
                # song_part에 없는 경우(마이그레이션 전) song.data fallback
                cur.execute("SELECT SUBSTRING(data, %s, %s) FROM song WHERE id = %s", (start + 1, length, self.row_id))
                return cur.fetchone()[0]
        else:
            raise ValueError(self.table)

    def iter_range(self, start: int, end: int) -> Iterator[bytes]:
        """[start, end] 바이트(양끝 포함)를 BLOB_READ_SIZE 단위로 읽습니다."""
        step = constant.BLOB_READ_SIZE
        if self.table == "song":
            start_seq = start // step
            end_seq = end // step
            with connect() as con, con.cursor() as cur:
                cur.execute(
                    "SELECT seq FROM song_part WHERE song_id = %s AND seq >= %s AND seq <= %s LIMIT 1",
                    (self.row_id, start_seq, end_seq),
                )
                has_parts = cur.fetchone() is not None

            if has_parts:
                for seq in range(start_seq, end_seq + 1):
                    with connect() as con, con.cursor() as cur:
                        cur.execute(
                            "SELECT data FROM song_part WHERE song_id = %s AND seq = %s",
                            (self.row_id, seq),
                        )
                        row = cur.fetchone()
                    if row is not None:
                        part_data = row[0]
                        chunk_start = seq * step
                        s = max(0, start - chunk_start)
                        e = min(len(part_data), end + 1 - chunk_start)
                        yield part_data[s:e]
            else:
                for pos in range(start + 1, end + 2, step):
                    with connect() as con, con.cursor() as cur:
                        cur.execute(
                            "SELECT SUBSTRING(data, %s, %s) FROM song WHERE id = %s",
                            (pos, min(step, end + 2 - pos), self.row_id),
                        )
                        row = cur.fetchone()
                    if row is not None:
                        yield row[0]
        elif self.table == "pre_chunk":
            for seq in range(start // step, end // step + 1):
                with connect() as con, con.cursor() as cur:
                    cur.execute(
                        "SELECT data FROM pre_chunk_part WHERE chunk_id = %s AND seq = %s",
                        (self.row_id, seq),
                    )
                    row = cur.fetchone()
                if row is not None:
                    chunk_start = seq * step
                    yield row[0][max(0, start - chunk_start) : end + 1 - chunk_start]
        elif self.table == "chart_chunk":
            for pos in range(start + 1, end + 2, step):
                with connect() as con, con.cursor() as cur:
                    cur.execute(
                        "SELECT SUBSTRING(data, %s, %s) FROM chart_chunk WHERE id = %s",
                        (pos, min(step, end + 2 - pos), self.row_id),
                    )
                    row = cur.fetchone()
                if row is not None:
                    yield row[0]
        else:
            raise ValueError(self.table)

    def close(self) -> None:
        pass


class ConnectionPool:
    """스레드 안전한 간단한 연결 풀. 접속 정보별로 다 쓴 연결을 최대 size개까지 남겨 두고 다시 씁니다.

    연결마다 TLS·인증에 수십 ms가 들어 쿼리마다 새로 맺으면 요청 하나에 수백 ms가 걸리므로 재사용합니다(#40).
    - 꺼낼 때 ping으로 끊긴 연결(서버 재시작, wait_timeout)을 걸러 내고 새로 맺습니다.
    - 돌려받을 때 rollback해 커밋하지 않은 작업·트랜잭션 스냅숏을 버립니다(연결을 닫을 때와 같은 결과).
      rollback이 실패하거나 남겨 둔 연결이 size개면 닫습니다.
    - 동시에 쓰는 연결 수는 막지 않습니다(스레드 한도가 상한). size는 놀고 있는 연결 수의 상한입니다.
    """

    def __init__(self, size: int):
        self.size = size
        self._idle: dict[tuple, list[pymysql.connections.Connection]] = defaultdict(list)
        self._lock = threading.Lock()

    def connect(self, params: dict) -> "ConnectionWrapper":
        key = tuple(sorted(params.items()))
        while True:
            with self._lock:
                idle = self._idle.get(key)
                con = idle.pop() if idle else None
            if con is None:
                return ConnectionWrapper(pymysql.connect(**params), self, key)
            try:
                con.ping(reconnect=False)
            except Exception:
                self._discard(con)
                continue
            return ConnectionWrapper(con, self, key)

    def release(self, key: tuple, con: pymysql.connections.Connection) -> None:
        if not con.open:
            return
        try:
            con.rollback()
        except Exception:
            self._discard(con)
            return
        with self._lock:
            idle = self._idle[key]
            if len(idle) < self.size:
                idle.append(con)
                return
        self._discard(con)

    def clear(self) -> None:
        """남겨 둔 연결을 모두 닫습니다."""
        with self._lock:
            cons = [con for idle in self._idle.values() for con in idle]
            self._idle.clear()
        for con in cons:
            self._discard(con)

    @staticmethod
    def _discard(con: pymysql.connections.Connection) -> None:
        try:
            con.close()
        except Exception:
            pass


_pool = ConnectionPool(constant.DB_POOL_SIZE)


def connect(**kwargs) -> "ConnectionWrapper":
    """constant.py에 정의된 MySQL 서버에 연결합니다. 연결 풀(ConnectionPool)에서 꺼내고 다 쓰면 돌려줍니다.

    with connect() as con 은 pymysql.Connection을 주고, with를 빠져나가거나 close()를 부르면
    커밋하지 않은 작업을 rollback한 뒤 풀에 돌려줍니다.
    """
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
    return _pool.connect(params)


class ConnectionWrapper:
    """pymysql.Connection의 래퍼. with 문을 빠져나가거나 close()를 부르면 연결을 풀에 돌려줍니다(한 번만)."""

    def __init__(self, con: pymysql.connections.Connection, pool: ConnectionPool | None = None, key: tuple = ()):
        self._con = con
        self._pool = pool
        self._key = key
        self._released = False

    def __enter__(self):
        return self._con

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def close(self) -> None:
        if self._released:
            return
        self._released = True
        if self._pool is None:
            if self._con.open:
                self._con.close()
        else:
            self._pool.release(self._key, self._con)

    def __getattr__(self, name):
        return getattr(self._con, name)


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
        self.backfill_file_kinds()
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
            cur.execute(
                """
                SELECT IS_NULLABLE FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s
                """,
                ("song", "data"),
            )
            row = cur.fetchone()
            if row and row[0] == "NO":
                try:
                    cur.execute("ALTER TABLE song MODIFY COLUMN data LONGBLOB NULL")
                except Exception:
                    pass
                self.logger.info("generate_database: Made song.data column NULLable")
            # 할당량 전역 기본값. 이미 있는 값(관리자가 바꾼 값)은 그대로 둡니다.
            for name, value in constant.DEFAULT_SETTINGS.items():
                cur.execute("INSERT IGNORE INTO setting (name, value) VALUES (%s, %s)", (name, str(value)))
            con.commit()

    def _fits_packet(self, size: int) -> bool:
        return size + constant.PACKET_OVERHEAD <= self.max_allowed_packet

    def _append_charts_to_chunk(self, cur, charts: list[tuple[str, bytes]]) -> None:
        """mutable한 chart chunk에 chart (항목 이름, 내용)들을 추가합니다. chunk가 BYTE_PER_CHUNK를 넘었다면 새 chunk를 만듭니다."""
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
            for arcname, content in charts:
                zf.writestr(zipfile.ZipInfo(arcname, date_time=(1980, 1, 1, 0, 0, 0)), content)
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

    def _read_song_range(self, cur, song_id: int, start: int, length: int) -> bytes:
        """곡 zip의 start부터 length 바이트. 같은 트랜잭션에서 넣은 곡도 읽도록 cur를 씁니다."""
        if length <= 0:
            return b""
        step = constant.BLOB_READ_SIZE
        start_seq, end_seq = start // step, (start + length - 1) // step
        cur.execute(
            "SELECT data FROM song_part WHERE song_id = %s AND seq >= %s AND seq <= %s ORDER BY seq",
            (song_id, start_seq, end_seq),
        )
        rows = cur.fetchall()
        if rows:
            buf = b"".join(r[0] for r in rows)
            offset = start - start_seq * step
            return buf[offset : offset + length]
        cur.execute("SELECT SUBSTRING(data, %s, %s) FROM song WHERE id = %s", (start + 1, length, song_id))
        row = cur.fetchone()
        return row[0] if row and row[0] else b""

    def _read_song_member(self, cur, song_id: int, entry: dict) -> bytes:
        """곡 zip에서 항목 하나를 압축을 풀어 읽습니다. 곡 전체를 읽지 않습니다."""
        if "data_offset" in entry:
            start = entry["data_offset"]
        else:
            head = self._read_song_range(cur, song_id, entry["offset"], 30)
            if len(head) != 30 or head[:4] != b"PK\x03\x04":
                raise ValueError(f"song {song_id}: bad local file header for {entry['path']}")
            name_len, extra_len = struct.unpack("<HH", head[26:30])
            start = entry["offset"] + 30 + name_len + extra_len
        raw = self._read_song_range(cur, song_id, start, entry["comp_size"])
        if entry["method"] == zipfile.ZIP_STORED:
            data = raw
        elif entry["method"] == zipfile.ZIP_DEFLATED:
            data = zlib.decompress(raw, -15)
        else:
            raise ValueError(f"song {song_id}: unsupported compression {entry['method']} for {entry['path']}")
        if len(data) != entry["size"] or f"{zlib.crc32(data):08x}" != entry["crc32"]:
            raise ValueError(f"song {song_id}: {entry['path']} does not match its manifest entry")
        return data

    def _rebuild_pre_chunk(self, cur, chunk_no: int) -> None:
        """chunk_no에 속한 곡들의 사전 파일을 다시 묶어 저장합니다. 곡이 사전 파일이 없어도 빈 zip을 둡니다."""
        lo = chunk_no * constant.SONGS_PER_PRE_CHUNK
        hi = lo + constant.SONGS_PER_PRE_CHUNK
        cur.execute("SELECT id, files FROM song WHERE id >= %s AND id < %s ORDER BY id", (lo, hi))
        songs = cur.fetchall()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_STORED) as zf:
            for song_id, files in songs:
                entries = sorted(
                    (e for e in (json.loads(files) if files else []) if is_pre_chunk_entry(e)),
                    key=lambda e: e["path"],
                )
                for entry in entries:
                    try:
                        data = self._read_song_member(cur, song_id, entry)
                    except (ValueError, zlib.error) as e:
                        # 깨진 항목은 빼고, 클라이언트는 파일별 사전 API로 받다가 오류를 봅니다.
                        self.logger.warning(f"pre chunk {chunk_no}: skipped broken entry: {e}")
                        continue
                    # 날짜를 고정해 같은 내용이면 같은 해시가 나오게 합니다.
                    info = zipfile.ZipInfo(pre_arcname(song_id, entry["path"]), date_time=(1980, 1, 1, 0, 0, 0))
                    zf.writestr(info, data)
        data = buf.getvalue()

        cur.execute("DELETE FROM pre_chunk_part WHERE chunk_id = %s", (chunk_no,))
        step = constant.BLOB_READ_SIZE
        for seq, pos in enumerate(range(0, len(data), step)):
            cur.execute(
                "INSERT INTO pre_chunk_part (chunk_id, seq, data) VALUES (%s, %s, %s)",
                (chunk_no, seq, data[pos : pos + step]),
            )
        cur.execute(
            """
            INSERT INTO pre_chunk (id, size, sha256) VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE size = VALUES(size), sha256 = VALUES(sha256)
            """,
            (chunk_no, len(data), hashlib.sha256(data).hexdigest()),
        )

    def backfill_pre_chunks(self) -> int:
        """곡은 있는데 사전 청크가 없는 구간(이전 버전에서 넣은 곡)의 사전 청크를 만듭니다. 만든 청크 수를 반환합니다.
        청크마다 따로 커밋하므로 도중에 꺼져도 다음 시작 때 남은 청크부터 이어서 만듭니다."""
        with connect() as con, con.cursor() as cur:
            cur.execute("SELECT id FROM song")
            wanted = {row[0] // constant.SONGS_PER_PRE_CHUNK for row in cur.fetchall()}
            cur.execute("SELECT id FROM pre_chunk")
            missing = sorted(wanted - {row[0] for row in cur.fetchall()})
        if missing:
            self.logger.info(f"backfill_pre_chunks: Building {len(missing)} pre chunks.")
        for i, chunk_no in enumerate(missing, 1):
            with self._write_lock, connect() as con, con.cursor() as cur:
                try:
                    cur.execute("SELECT id FROM pre_chunk WHERE id = %s", (chunk_no,))
                    if cur.fetchone() is None:
                        self._rebuild_pre_chunk(cur, chunk_no)
                    con.commit()
                except Exception:
                    con.rollback()
                    raise
            if i % 100 == 0:
                self.logger.info(f"backfill_pre_chunks: {i}/{len(missing)}")
        if missing:
            self.logger.info(f"backfill_pre_chunks: Built {len(missing)} pre chunks.")
        return len(missing)

    def get_song_data(self, song_id: int) -> bytes | None:
        """곡 id의 전체 zip data를 반환합니다. song_part 또는 song.data에서 읽어옵니다."""
        with connect() as con, con.cursor() as cur:
            return self._get_song_data_with_cur(cur, song_id)

    def _get_song_data_with_cur(self, cur, song_id: int) -> bytes | None:
        cur.execute("SELECT data FROM song_part WHERE song_id = %s ORDER BY seq", (song_id,))
        rows = cur.fetchall()
        if rows:
            return b"".join(r[0] for r in rows)
        cur.execute("SELECT data FROM song WHERE id = %s AND data IS NOT NULL", (song_id,))
        row = cur.fetchone()
        return row[0] if row else None

    def backfill_manifest(self) -> None:
        """매니페스트 정보가 없는 곡(이전 버전에서 넣은 곡)의 항목 목록을 song zip에서 채웁니다.
        원래 폴더명은 알 수 없으므로 song id를 폴더명으로 씁니다. 항목에 kind(pre/play)가 없는 곡도 다시 채웁니다.
        또한 chart 테이블에 filename이 채워지지 않은 항목의 파일명을 song zip에서 추출해 채웁니다."""
        with self._write_lock, connect() as con, con.cursor() as cur:
            try:
                # 1) 기존 song.data BLOB을 song_part로 마이그레이션
                cur.execute("SELECT id, data FROM song WHERE data IS NOT NULL")
                rows = cur.fetchall()
                for song_id, song_data in rows:
                    if song_data is not None:
                        step = constant.BLOB_READ_SIZE
                        for seq, pos in enumerate(range(0, len(song_data), step)):
                            part = song_data[pos : pos + step]
                            cur.execute(
                                "INSERT IGNORE INTO song_part (song_id, seq, data) VALUES (%s, %s, %s)",
                                (song_id, seq, part),
                            )
                        cur.execute("UPDATE song SET data = NULL WHERE id = %s", (song_id,))

                cur.execute(
                    "SELECT id FROM song WHERE files IS NULL OR files NOT LIKE %s OR files NOT LIKE %s ORDER BY id",
                    ('%"kind"%', '%"data_offset"%'),
                )
                song_ids_no_files = set(row[0] for row in cur.fetchall())

                cur.execute("SELECT DISTINCT song_id FROM chart WHERE filename = '' AND song_id IS NOT NULL")
                song_ids_no_filenames = set(row[0] for row in cur.fetchall())

                affected_song_ids = sorted(song_ids_no_files | song_ids_no_filenames)
                if affected_song_ids:
                    for song_id in affected_song_ids:
                        song_data = self._get_song_data_with_cur(cur, song_id)
                        if not song_data:
                            continue
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
            if affected_song_ids:
                self.logger.info(f"backfill_manifest: Updated manifest of {len(affected_song_ids)} songs.")

    def backfill_file_kinds(self) -> int:
        """pre/play 판정 규칙(FILE_KINDS_VERSION)이 바뀌었으면 기존 곡의 kind를 다시 판정합니다. kind가 바뀐 곡 수를 반환합니다.

        곡 zip 전체를 읽지 않고 매니페스트의 항목 목록과 차트 파일만 읽어 판정합니다. kind가 바뀐 곡은 같은 트랜잭션에서
        매니페스트 청크를 다시 만들고, 사전 청크는 지워 backfill_pre_chunks가 새 규칙으로 다시 만들게 합니다
        (그동안 클라이언트는 파일별 사전 API로 받고, 이 API도 새 kind를 봅니다).
        BACKFILL_BATCH_SONGS곡마다 커밋합니다. 판정하지 못한 곡은 로그를 남기고 건너뛰며, 그때는 버전을 올리지 않아
        다음 시작 때 다시 시도합니다.
        """
        with connect() as con, con.cursor() as cur:
            cur.execute("SELECT value FROM setting WHERE name = %s", (FILE_KINDS_SETTING,))
            row = cur.fetchone()
            if row is not None and int(row[0]) >= FILE_KINDS_VERSION:
                return 0
            cur.execute("SELECT id FROM song ORDER BY id")
            song_ids = [r[0] for r in cur.fetchall()]

        changed = failed = 0
        step = constant.BACKFILL_BATCH_SONGS
        for i in range(0, len(song_ids), step):
            with self._write_lock, connect() as con, con.cursor() as cur:
                try:
                    batch_changed = []
                    for song_id in song_ids[i : i + step]:
                        try:
                            files = self._recompute_kinds(cur, song_id)
                        except Exception:
                            self.logger.exception(f"backfill_file_kinds: Skipped song[{song_id}]")
                            failed += 1
                            continue
                        if files is not None:
                            cur.execute("UPDATE song SET files = %s WHERE id = %s", (json.dumps(files), song_id))
                            batch_changed.append(song_id)
                    for chunk_no in sorted({s // constant.SONGS_PER_MANIFEST_CHUNK for s in batch_changed}):
                        self._rebuild_manifest_chunk(cur, chunk_no)
                    for chunk_no in sorted({s // constant.SONGS_PER_PRE_CHUNK for s in batch_changed}):
                        cur.execute("DELETE FROM pre_chunk_part WHERE chunk_id = %s", (chunk_no,))
                        cur.execute("DELETE FROM pre_chunk WHERE id = %s", (chunk_no,))
                    con.commit()
                except Exception:
                    con.rollback()
                    raise
            changed += len(batch_changed)

        if failed:
            self.logger.warning(f"backfill_file_kinds: {failed} songs failed. Retrying on next start.")
        else:
            with self._write_lock, connect() as con, con.cursor() as cur:
                cur.execute("UPDATE setting SET value = %s WHERE name = %s", (str(FILE_KINDS_VERSION), FILE_KINDS_SETTING))
                if cur.rowcount == 0:
                    cur.execute(
                        "INSERT IGNORE INTO setting (name, value) VALUES (%s, %s)",
                        (FILE_KINDS_SETTING, str(FILE_KINDS_VERSION)),
                    )
                con.commit()
        if changed:
            self.logger.info(f"backfill_file_kinds: Updated pre/play kinds of {changed} songs.")
        return changed

    def _recompute_kinds(self, cur, song_id: int) -> list[dict] | None:
        """곡의 매니페스트 항목 kind를 현재 규칙으로 다시 판정합니다. 바뀌었으면 새 항목 목록, 그대로면 None."""
        cur.execute("SELECT files FROM song WHERE id = %s", (song_id,))
        row = cur.fetchone()
        files = json.loads(row[0]) if row and row[0] else []
        if not files:
            return None
        charts = {
            e["path"]: self._read_song_member(cur, song_id, e)
            for e in files
            if pathlib.PurePosixPath(e["path"]).suffix.lower() in constant.BMS_FORMAT
        }
        kinds = kinds_from_charts([e["path"] for e in files], charts)
        new = [{**e, "kind": kinds[e["path"]]} for e in files]
        return new if new != files else None

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

    def insert_song(self, song_path:os.PathLike, remove=False) -> dict | None:
        """BMS 노래 한 곡을 DB에 추가할 수 있는 함수입니다

        Args:
            song_path (os.PathLike): bms 파일을 포함한 에셋들이 담겨있는 폴더의 경로

        Returns:
            등록한 곡 정보 {"song_id", "new_song", "charts", "new_charts"}. 기존 곡에 파일을 더했으면 "added_files",
            내용이 달라 넣지 못한 파일이 있으면 "conflicts"(경로 목록)가 붙습니다. 등록하지 못하면 None.
            차트가 기존 곡 여러 개와 겹치면 AmbiguousSongError.
        """
        with self.batch() as batch:
            info = batch.add(song_path, remove=remove)
        return info

    def batch(self) -> "SongBatch":
        """곡 여러 개를 묶어 넣는 배치. 여러 곡을 넣을 때는 insert_song을 반복하지 말고 이것을 씁니다."""
        return SongBatch(self)

    def _find_bms_files_and_existing_songs(self, root: pathlib.Path, cur) -> tuple[list, list[int]]:
        """폴더의 차트 (경로, 크기, sha256) 목록과, 같은 차트(sha256+크기)가 이미 있는 곡 id 목록(정렬)을 반환합니다.
        같은 트랜잭션(배치)에서 앞서 넣은 곡도 보도록 cur를 씁니다."""
        bms_files = []
        song_ids = set()
        for file_path in chart_files(root):
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
            if row is not None and row[0] is not None:
                song_ids.add(row[0])

            bms_files.append((file_path, size, sha256))
        return bms_files, sorted(song_ids)

    def _insert_new_song(self, root: pathlib.Path, cur) -> tuple[int | None, int]:
        """곡 zip을 넣고 (song_id, zip 크기)를 반환합니다. 패킷 한도를 넘으면 (None, 0)."""
        data = self.create_zip(root)
        if not self._fits_packet(len(data)):
            self.logger.error(
                f"insert_song failed: song zip ({len(data)} bytes) exceeds "
                f"max_allowed_packet({self.max_allowed_packet})[{str(root)}]"
            )
            return None, 0

        cur.execute(
            "INSERT INTO song (size, sha256, folder, files) VALUES (%s, %s, %s, %s)",
            (
                len(data),
                hashlib.sha256(data).hexdigest(),
                root.resolve().name,
                json.dumps(zip_entries(data)),
            ),
        )
        song_id = cur.lastrowid
        self._insert_song_parts(cur, song_id, data)
        self.logger.info(f"insert_song: Inserted new song[{song_id}, {len(data)} bytes]")
        return song_id, len(data)

    def _insert_song_parts(self, cur, song_id: int, data: bytes) -> None:
        step = constant.BLOB_READ_SIZE
        for seq, pos in enumerate(range(0, len(data), step)):
            part = data[pos : pos + step]
            cur.execute(
                "INSERT INTO song_part (song_id, seq, data) VALUES (%s, %s, %s)",
                (song_id, seq, part),
            )

    def _merge_into_song(self, root: pathlib.Path, song_id: int, cur) -> tuple[int, list[str], list[str]] | None:
        """새 폴더에서 기존 곡 zip에 없는 파일을 그 곡 zip에 더합니다(겹치는 차트로 같은 곡이라고 본 경우).

        같은 경로(대소문자 무시)에 내용이 다른 파일이 있으면 기존 것을 두고 충돌로 돌려줍니다.
        (새 zip 크기, 더한 경로, 충돌 경로)를 반환합니다. 더할 파일이 없으면 크기는 0이고 곡은 그대로입니다.
        새 zip이 패킷 한도를 넘으면 None.
        """
        data = self._get_song_data_with_cur(cur, song_id)
        if data is None:
            raise ValueError(f"song {song_id}: zip data not found")
        resolved = root.resolve()
        new_files: list[tuple[str, bytes]] = []
        conflicts: list[str] = []
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            existing = {info.filename.rstrip("/").lower(): info for info in zf.infolist()}
            for path, is_dir in song_dir_entries(root):
                if is_dir:
                    continue
                arcname = path.relative_to(resolved).as_posix()
                content = path.read_bytes()
                info = existing.get(arcname.lower())
                if info is None:
                    new_files.append((arcname, content))
                elif info.is_dir() or info.file_size != len(content) or zf.read(info) != content:
                    conflicts.append(arcname)
        conflicts.sort()
        if conflicts:
            self.logger.warning(
                f"insert_song: Song[{song_id}] kept existing files; skipped different ones {conflicts}[{root}]"
            )
        if not new_files:
            return 0, [], conflicts

        buf = io.BytesIO(data)
        # 기존 항목은 그대로 두고 뒤에 붙입니다(기존 항목의 위치·압축 데이터가 바뀌지 않음).
        with zipfile.ZipFile(buf, mode="a") as zf:
            for arcname, content in new_files:
                zf.writestr(_song_zipinfo(arcname), content, compress_type=zipfile.ZIP_DEFLATED, compresslevel=6)
        data = buf.getvalue()
        if not self._fits_packet(len(data)):
            self.logger.error(
                f"insert_song failed: merged song zip ({len(data)} bytes) exceeds "
                f"max_allowed_packet({self.max_allowed_packet})[{str(root)}]"
            )
            return None

        cur.execute(
            "UPDATE song SET size = %s, sha256 = %s, files = %s, data = NULL WHERE id = %s",
            (len(data), hashlib.sha256(data).hexdigest(), json.dumps(zip_entries(data)), song_id),
        )
        cur.execute("DELETE FROM song_part WHERE song_id = %s", (song_id,))
        self._insert_song_parts(cur, song_id, data)
        added = sorted(arcname for arcname, _ in new_files)
        self.logger.info(f"insert_song: Merged {len(added)} files into song[{song_id}, {len(data)} bytes]")
        return len(data), added, conflicts

    def _insert_or_update_charts(self, root: pathlib.Path, bms_files: list, song_id: int, cur) -> list:
        new_charts = []
        for (chart_file_path, size, sha256) in bms_files:
            filename = chart_file_path.relative_to(root).as_posix()
            cur.execute(
                "INSERT IGNORE INTO chart (id, song_id, size, filename) VALUES (%s, %s, %s, %s)",
                (str(sha256), song_id, size, filename),
            )
            if cur.rowcount == 1:
                new_charts.append((chart_arcname(sha256, chart_file_path), chart_file_path.read_bytes()))
            elif song_id is not None:
                cur.execute(
                    "UPDATE chart SET filename = %s WHERE id = %s AND size = %s AND filename = ''",
                    (filename, str(sha256), size),
                )
        return new_charts

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

        def _add_entry(path: pathlib.Path, is_dir: bool, zf: zipfile.ZipFile) -> None:
            arcname = get_arcname(path)

            if is_dir:
                # 빈 디렉터리는 명시적으로 저장
                try:
                    next(path.iterdir())
                except StopIteration:
                    info = make_zipinfo(
                        arcname,
                        is_dir=True,
                    )
                    zf.writestr(info, b"")

            else:
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

            # 심볼릭 링크는 따라가지 않습니다(곡 폴더 밖의 파일이 곡에 들어가지 않도록).
            for path, is_dir in song_dir_entries(source_dir):
                _add_entry(path, is_dir, zf)

        return buf.getvalue()

    def get_chart_chunk_hash(self) -> dict[int, str]:
        with connect() as con, con.cursor() as cur:
            cur.execute("SELECT id, sha256 FROM chart_chunk ORDER BY id")
            return {row[0]: row[1] for row in cur.fetchall()}

    def get_song_files(self, song_id: int) -> list[dict] | None:
        """곡 zip의 항목 목록(매니페스트의 files). 없는 곡이면 None."""
        with connect() as con, con.cursor() as cur:
            cur.execute("SELECT files FROM song WHERE id = %s", (song_id,))
            row = cur.fetchone()
        if row is None:
            return None
        return json.loads(row[0]) if row[0] else []

    def get_pre_chunk_hash(self) -> dict[int, str]:
        with connect() as con, con.cursor() as cur:
            cur.execute("SELECT id, sha256 FROM pre_chunk ORDER BY id")
            return {row[0]: row[1] for row in cur.fetchall()}

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
        """song / chart_chunk / pre_chunk 테이블의 BLOB 메타데이터를 단기 연결로 조회해 BlobReader를 반환합니다.

        전송 중 DB 연결과 트랜잭션을 잡지 않도록 단기 연결을 사용합니다.

        Returns:
            BlobReader. 존재하지 않는 id의 경우 None을 반환합니다.
        """
        if table not in ("song", "chart_chunk", "pre_chunk"):
            raise ValueError(table)
        with connect() as con, con.cursor() as cur:
            if table == "song":
                cur.execute("SELECT size, sha256 FROM song WHERE id = %s", (row_id,))
            elif table == "chart_chunk":
                cur.execute("SELECT LENGTH(data), sha256 FROM chart_chunk WHERE id = %s", (row_id,))
            elif table == "pre_chunk":
                cur.execute("SELECT size, sha256 FROM pre_chunk WHERE id = %s", (row_id,))
            row = cur.fetchone()
        if row is None:
            return None
        return BlobReader(table, row_id, row[0], row[1])


class SongBatch:
    """곡 여러 개를 한 트랜잭션으로 넣고, 청크(차트·매니페스트·사전)는 커밋 직전에 배치마다 한 번씩만 갱신합니다.

    곡마다 청크를 읽고 다시 쓰면 대량 임포트에서 같은 청크를 수천 번 읽고 쓰게 되므로 묶어서 처리합니다.
    곡과 청크 갱신이 같은 트랜잭션이라 도중에 꺼지면 배치 전체가 롤백되고, 다시 임포트하면 처음부터 들어갑니다.
    곡 하나가 실패하면 SAVEPOINT로 그 곡만 되돌리고 배치는 이어갑니다.
    새 차트가 BYTE_PER_CHUNK, 곡 데이터가 IMPORT_BATCH_BYTES, 곡 수가 IMPORT_BATCH_SONGS에 이르면 커밋합니다.
    배치가 열려 있는 동안(커밋 전까지)만 쓰기 잠금을 잡습니다.

    with db.batch() as batch:
        info = batch.add(song_dir)          # 등록 정보 또는 None. 실패하면 예외(그 곡만 되돌림)
    # with를 빠져나갈 때 남은 곡을 커밋합니다. 커밋 전에 예외가 나면 배치 전체를 롤백합니다.
    """

    def __init__(self, db: "Database"):
        self.db = db
        self.logger = db.logger
        self._con = None
        self._cur = None
        self._reset()

    def _reset(self) -> None:
        self._charts: list[tuple[str, bytes]] = []
        self._chart_bytes = 0
        self._song_bytes = 0
        self._songs = 0
        self._manifest_chunks: set[int] = set()
        self._pre_chunks: set[int] = set()
        self._remove: list[pathlib.Path] = []
        self._infos: list[dict] = []

    def __enter__(self) -> "SongBatch":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if exc_type is None:
            self.flush()
        else:
            self._abort()

    def _begin(self) -> None:
        if self._con is not None:
            return
        self.db._write_lock.acquire()
        try:
            self._con = connect()
            self._cur = self._con.cursor()
        except Exception:
            self._con = None
            self.db._write_lock.release()
            raise

    def _close(self) -> None:
        try:
            if self._cur is not None:
                self._cur.close()
            if self._con is not None:
                self._con.close()
        finally:
            self._con = self._cur = None
            self.db._write_lock.release()
            self._reset()

    def _abort(self) -> None:
        """커밋하지 않은 곡을 모두 되돌립니다. 이 배치에서 돌려준 등록 정보에는 error를 채웁니다."""
        if self._con is None:
            return
        for info in self._infos:
            info["error"] = "배치를 커밋하지 못해 등록이 취소됐습니다. 다시 임포트하세요."
        try:
            self._con.rollback()
        finally:
            self._close()

    def add(self, song_path: os.PathLike, remove: bool = False) -> dict | None:
        """곡 하나를 배치에 넣습니다. 등록 정보를 반환하고, 넣지 못하면 None.
        반환한 dict는 배치 커밋이 실패하면 "error" 키가 채워집니다."""
        root = pathlib.Path(song_path)
        if not root.is_dir():
            self.logger.warning(f"insert_song failed: Path isn't directory[{root}]")
            return None
        if not chart_files(root):
            self.logger.warning(f"insert_song failed: No valid file in folder. Suporting ext: {constant.BMS_FORMAT}")
            return None

        self._begin()
        cur = self._cur
        cur.execute("SAVEPOINT song")
        try:
            bms_files, existing = self.db._find_bms_files_and_existing_songs(root, cur)
            if len(existing) > 1:
                raise AmbiguousSongError(
                    f"차트가 기존 곡 여러 개({', '.join(map(str, existing))})와 겹쳐 어느 곡에 넣을지 정할 수 없어 "
                    "등록하지 않았습니다. 폴더를 정리한 뒤 다시 임포트하세요."
                )
            new_song = False
            song_bytes = 0
            added: list[str] = []
            conflicts: list[str] = []
            if not existing:
                song_id, song_bytes = self.db._insert_new_song(root, cur)
                if song_id is None:
                    cur.execute("ROLLBACK TO SAVEPOINT song")
                    return None
                new_song = True
            else:
                # 겹치는 차트가 있는 기존 곡에 새 파일(키음·BGA 등)을 더합니다.
                song_id = existing[0]
                merged = self.db._merge_into_song(root, song_id, cur)
                if merged is None:
                    cur.execute("ROLLBACK TO SAVEPOINT song")
                    return None
                song_bytes, added, conflicts = merged
                # 내용이 달라 곡 zip에 넣지 못한 차트는 등록하지 않습니다(zip의 같은 경로 파일과 어긋나므로).
                bms_files = [f for f in bms_files if f[0].relative_to(root).as_posix() not in conflicts]
            new_charts = self.db._insert_or_update_charts(root, bms_files, song_id, cur)
            cur.execute("RELEASE SAVEPOINT song")
        except Exception:
            cur.execute("ROLLBACK TO SAVEPOINT song")
            raise

        self._charts.extend(new_charts)
        self._chart_bytes += sum(len(content) for _, content in new_charts)
        self._song_bytes += song_bytes
        self._songs += 1
        if new_song or new_charts or added:
            self._manifest_chunks.add(song_id // constant.SONGS_PER_MANIFEST_CHUNK)
        if new_song or added:
            self._pre_chunks.add(song_id // constant.SONGS_PER_PRE_CHUNK)
        if remove and not conflicts:
            self._remove.append(root)
        elif remove:
            # 곡에 넣지 못한 파일이 있으므로 원본 폴더는 지우지 않습니다.
            self.logger.warning(f"insert_song: Kept folder with conflicting files[{root}]")
        self.logger.info(f"insert_song: Song[{song_id}] with {len(new_charts)} new charts.")
        info = {
            "song_id": song_id,
            "new_song": new_song,
            "charts": len(bms_files),
            "new_charts": len(new_charts),
        }
        if added:
            info["added_files"] = added
        if conflicts:
            info["conflicts"] = conflicts
        self._infos.append(info)

        if (
            self._chart_bytes >= constant.BYTE_PER_CHUNK
            or self._song_bytes >= constant.IMPORT_BATCH_BYTES
            or self._songs >= constant.IMPORT_BATCH_SONGS
        ):
            self.flush()
        return info

    def flush(self) -> None:
        """모은 차트를 청크에 붙이고, 바뀐 매니페스트·사전 청크를 한 번씩 다시 만든 뒤 커밋합니다."""
        if self._con is None:
            return
        cur = self._cur
        try:
            if self._charts:
                self.db._append_charts_to_chunk(cur, self._charts)
            for chunk_no in sorted(self._manifest_chunks):
                self.db._rebuild_manifest_chunk(cur, chunk_no)
            for chunk_no in sorted(self._pre_chunks):
                self.db._rebuild_pre_chunk(cur, chunk_no)
            self._con.commit()
        except Exception:
            self.logger.exception("song batch commit failed")
            self._abort()
            raise
        self.logger.info(
            f"song batch: Committed {self._songs} songs, {len(self._charts)} charts, "
            f"{self._song_bytes} song bytes."
        )
        remove = self._remove
        self._close()
        for path in remove:
            shutil.rmtree(path, ignore_errors=True)
