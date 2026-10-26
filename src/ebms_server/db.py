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
import time
import zlib
import unicodedata
from collections import defaultdict
from collections.abc import Iterator

import pymysql

from . import charset, constant, preview, store

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
            role VARCHAR(16) NOT NULL DEFAULT 'user',
            status VARCHAR(16) NOT NULL DEFAULT 'active',
            created_at BIGINT UNSIGNED NOT NULL,
            last_login_at BIGINT UNSIGNED,
            -- 사용자별 할당량. NULL이면 setting의 전역 기본값을 씁니다.
            max_tickets INT UNSIGNED,
            refill_seconds INT UNSIGNED,
            pre_monthly_bytes BIGINT UNSIGNED,
            pre_throttled_kbps INT UNSIGNED,
            -- 정지(status='banned')가 끝나는 시각. NULL이면 무기한입니다.
            banned_until BIGINT UNSIGNED,
            -- 가입할 때 동의한 이용약관·개인정보 처리방침의 판(policy.id, 템플릿이었으면 NULL)과 동의 시각
            terms_version INT UNSIGNED,
            privacy_version INT UNSIGNED,
            agreed_at BIGINT UNSIGNED,

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
            created_at BIGINT UNSIGNED NOT NULL,
            last_used_at BIGINT UNSIGNED NOT NULL,
            expires_at BIGINT UNSIGNED NOT NULL,
            revoked_at BIGINT UNSIGNED,

            PRIMARY KEY (id),
            UNIQUE (token_sha256),
            FOREIGN KEY (user_id) REFERENCES user(id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=ascii COLLATE=ascii_bin
    """,
    # 관리자 테스트용 API 키(api_keys.py). 원문 대신 sha256만 저장하고, 만든 관리자의 권한으로 동작합니다.
    """
        CREATE TABLE IF NOT EXISTS api_key(
            id INT UNSIGNED NOT NULL AUTO_INCREMENT,
            token_sha256 CHAR(64) NOT NULL,
            prefix VARCHAR(16) NOT NULL,
            user_id CHAR(36) NOT NULL,
            name VARCHAR(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            created_at BIGINT UNSIGNED NOT NULL,
            last_used_at BIGINT UNSIGNED,
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
    # 곡 플레이 다운로드 기록(관리자 조회용, download_log.py). result는 download_log.RESULTS 중 하나,
    # resumed는 이어받기(Range 시작이 0보다 큼) 요청입니다. 보유기간이 지나면 정리 작업(retention)이 지웁니다.
    """
        CREATE TABLE IF NOT EXISTS download_log(
            id INT UNSIGNED NOT NULL AUTO_INCREMENT,
            user_id CHAR(36) NOT NULL,
            song_id INT UNSIGNED NOT NULL,
            at BIGINT UNSIGNED NOT NULL,
            result VARCHAR(16) NOT NULL,
            resumed TINYINT NOT NULL DEFAULT 0,

            PRIMARY KEY (id),
            INDEX download_log_user (user_id, at),
            INDEX download_log_song (song_id, at),
            INDEX download_log_at (at),
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
    # 지운(사용 중지한) 곡. 곡 zip과 차트는 UNUSED_DIR/{song_id}/로 옮겨 두고 같은 번호로 복원할 수 있습니다.
    # 번호는 결번으로 남고 다른 곡에 다시 쓰지 않습니다. chart_list는 복원할 때 다시 붙일 차트 [[sha256, size, 경로], ...].
    """
        CREATE TABLE IF NOT EXISTS deleted_song(
            song_id INT UNSIGNED NOT NULL,
            folder VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL DEFAULT '',
            size BIGINT UNSIGNED NOT NULL,
            sha256 CHAR(64) NOT NULL,
            charts INT UNSIGNED NOT NULL,
            chart_list MEDIUMTEXT NOT NULL,
            reason TEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            deleted_by CHAR(36),
            deleted_at BIGINT UNSIGNED NOT NULL,

            PRIMARY KEY (song_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=ascii COLLATE=ascii_bin
    """,
    # 탈퇴한 OAuth 계정의 재가입 제한(accounts.delete_user). OAuth 계정 ID 원문 대신 HMAC(setting의 rejoin_salt)만 두고,
    # until이 지나면 정리 작업(retention)이 지웁니다. until이 NULL이면 무기한 정지 중에 탈퇴한 경우입니다.
    """
        CREATE TABLE IF NOT EXISTS rejoin_block(
            key_sha256 CHAR(64) NOT NULL,
            until BIGINT UNSIGNED,

            PRIMARY KEY (key_sha256)
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
    # 개인정보 처리방침(kind=privacy)·이용약관(terms)의 판. 저장할 때마다 행을 더해 변경 이력으로 남깁니다.
    # effective_on은 시행일(KST, YYYY-MM-DD)입니다. 행이 없으면 src/ebms_server/policy_templates/의 템플릿을 씁니다.
    """
        CREATE TABLE IF NOT EXISTS policy(
            id INT UNSIGNED NOT NULL AUTO_INCREMENT,
            kind VARCHAR(16) NOT NULL,
            body MEDIUMTEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            effective_on CHAR(10) NOT NULL,
            created_at BIGINT UNSIGNED NOT NULL,
            created_by CHAR(36),

            PRIMARY KEY (id)
        ) ENGINE=InnoDB DEFAULT CHARSET=ascii COLLATE=ascii_bin
    """,
    # 난이도표(tables.py). url은 관리자가 등록한 주소(HTML 또는 헤더 JSON), header_url은 실제로 읽은 헤더 JSON 주소입니다.
    # 마지막으로 읽은 결과(fetched_at, charts)와 실패했으면 그 오류(error)를 둡니다.
    """
        CREATE TABLE IF NOT EXISTS difficulty_table(
            id INT UNSIGNED NOT NULL AUTO_INCREMENT,
            url VARCHAR(1024) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            name VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL DEFAULT '',
            symbol VARCHAR(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL DEFAULT '',
            header_url VARCHAR(1024) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL DEFAULT '',
            charts INT UNSIGNED NOT NULL DEFAULT 0,
            fetched_at BIGINT UNSIGNED,
            error TEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin,
            created_at BIGINT UNSIGNED NOT NULL,

            PRIMARY KEY (id)
        ) ENGINE=InnoDB DEFAULT CHARSET=ascii COLLATE=ascii_bin
    """,
    # 난이도표에 실린 차트. 표가 md5나 sha256 중 하나만 줄 수 있어 없는 쪽은 ''입니다. chart.md5·chart.id와 맞춰 곡을 찾습니다.
    """
        CREATE TABLE IF NOT EXISTS difficulty_table_chart(
            table_id INT UNSIGNED NOT NULL,
            md5 CHAR(32) NOT NULL DEFAULT '',
            sha256 CHAR(64) NOT NULL DEFAULT '',
            level VARCHAR(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL DEFAULT '',

            PRIMARY KEY (table_id, md5, sha256),
            INDEX difficulty_table_chart_md5 (md5),
            INDEX difficulty_table_chart_sha256 (sha256),
            FOREIGN KEY (table_id) REFERENCES difficulty_table(id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=ascii COLLATE=ascii_bin
    """,
    # 관리자가 고치는 웹 페이지 글(name=home: 메인화면 소개 글). 행이 없으면 코드의 기본값을 씁니다.
    """
        CREATE TABLE IF NOT EXISTS site_text(
            name VARCHAR(32) NOT NULL,
            body MEDIUMTEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
            updated_at BIGINT UNSIGNED NOT NULL,
            updated_by CHAR(36),

            PRIMARY KEY (name)
        ) ENGINE=InnoDB DEFAULT CHARSET=ascii COLLATE=ascii_bin
    """,
    # 서버가 만든 곡 미리듣기(preview.py). 곡 zip이 바뀌면(song_sha256이 다르면) 다시 만듭니다.
    # path가 NULL이면 만들지 않은 곡(이미 preview가 있음, 소리 없음, 실패)이고 note에 이유를 둡니다.
    """
        CREATE TABLE IF NOT EXISTS song_preview(
            song_id INT UNSIGNED NOT NULL,
            song_sha256 CHAR(64) NOT NULL,
            version INT UNSIGNED NOT NULL,
            path VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin,
            size BIGINT UNSIGNED NOT NULL DEFAULT 0,
            crc32 CHAR(8),
            data MEDIUMBLOB,
            note VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL DEFAULT '',

            PRIMARY KEY (song_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=ascii COLLATE=ascii_bin
    """,
]

# 이전 버전에서 만든 테이블에 없는 컬럼
MISSING_COLUMNS = [
    ("song", "folder", "VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL DEFAULT ''"),
    ("song", "files", "MEDIUMTEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin"),
    ("chart", "filename", "VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL DEFAULT ''"),
    # 난이도표는 차트를 md5로 가리킵니다. 이전 버전에서 넣은 차트는 backfill_chart_md5가 채웁니다.
    ("chart", "md5", "CHAR(32)"),
    # 검토 대기 곡과 비슷한 곡(이름·작곡가·BPM·장르)을 찾는 키(chart_meta_keys). 이전 버전에서 넣은 차트는 backfill_chart_meta가 채웁니다.
    ("chart", "title_key", "VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin"),
    ("chart", "artist_key", "VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin"),
    ("chart", "bpm_key", "VARCHAR(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin"),
    ("chart", "genre_key", "VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin"),
    ("user", "max_tickets", "INT UNSIGNED"),
    ("user", "refill_seconds", "INT UNSIGNED"),
    ("user", "pre_monthly_bytes", "BIGINT UNSIGNED"),
    ("user", "pre_throttled_kbps", "INT UNSIGNED"),
    ("user", "banned_until", "BIGINT UNSIGNED"),
    ("user", "terms_version", "INT UNSIGNED"),
    ("user", "privacy_version", "INT UNSIGNED"),
    ("user", "agreed_at", "BIGINT UNSIGNED"),
    # 저장소 파일의 상대 경로(store.py, #152). NULL이면 아직 DB BLOB(song_part, chart_chunk.data, pre_chunk_part)에 있습니다.
    ("song", "path", "VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin"),
    ("chart_chunk", "path", "VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin"),
    ("pre_chunk", "path", "VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin"),
]
# 이전 버전에서 만든 테이블에 없는 인덱스 (테이블, 인덱스 이름, 컬럼)
MISSING_INDEXES = [
    ("chart", "chart_md5", "md5"),
    ("chart", "chart_title_key", "title_key"),
    ("chart", "chart_artist_key", "artist_key"),
    ("chart", "chart_bpm_genre_key", "bpm_key, genre_key"),
]
# 더 이상 수집하지 않는 개인정보 컬럼(#102). 서버 시작 시 있으면 값과 함께 지웁니다.
DROPPED_COLUMNS = [
    ("user", "email"),
    ("user_identity", "email"),
    ("user_identity", "name"),
    ("session", "device_name"),
    ("auth_code", "device_name"),
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
FILE_KINDS_VERSION = 3
# FILE_KINDS_VERSION을 적용한 버전을 저장하는 setting 이름
FILE_KINDS_SETTING = "file_kinds_version"
# backfill_file_kinds가 어디까지 판정했는지("버전:마지막 곡 번호"). 도중에 꺼지면 다음 시작 때 그 뒤부터 이어서 합니다.
FILE_KINDS_PROGRESS_SETTING = "file_kinds_progress"
# 지금 차트 청크·사전 청크를 만든 크기(CHARTS_PER_CHUNK, SONGS_PER_PRE_CHUNK)를 저장하는 setting 이름.
# 상수와 다르면 서버 시작 뒤 다시 묶습니다. 없으면(이전 버전) 다른 것으로 봅니다.
CHART_CHUNK_SETTING = "chart_chunk_charts"
PRE_CHUNK_SETTING = "pre_chunk_songs"
# DB BLOB을 저장소 파일로 다 옮기고 BLOB 테이블의 공간을 돌려받은 테이블(쉼표로 이음). migrate_to_store가 다시 비우지 않게 합니다.
STORE_RECLAIMED_SETTING = "store_reclaimed"
# migrate_to_store가 옮기는 순서. 작은 테이블부터 옮기고 비워, 큰 테이블(곡)을 옮길 때 쓸 공간을 먼저 확보합니다.
STORE_TABLES = ("pre_chunk", "chart_chunk", "song")
# 차트 청크를 다시 묶는 중일 때 옛 청크의 마지막 번호와 단계(copy: 새 청크를 만드는 중, delete: 옛 청크를 지우는 중)
CHART_RECHUNK_SETTING = "chart_rechunk_old_max"
CHART_RECHUNK_PHASE_SETTING = "chart_rechunk_phase"
# 기존 곡의 차트를 찾을 때 쓴 차트 확장자(BMS_FORMAT, 쉼표로 이음). 없으면 .bmson을 넣기 전 확장자로 봅니다.
# BMS_FORMAT에 확장자를 더하면 서버 시작 뒤 backfill_chart_formats가 기존 곡에서 그 확장자 파일을 차트로 등록합니다.
CHART_FORMATS_SETTING = "chart_formats"
LEGACY_CHART_FORMATS = (".bms", ".bme", ".bml", ".pms")
# bmson 차트 info의 이미지·오디오 항목. PRE_HEADERS와 같은 사전 파일입니다.
BMSON_PRE_FIELDS = {
    "banner_image": constant.IMAGE_FORMAT,
    "eyecatch_image": constant.IMAGE_FORMAT,
    "back_image": constant.IMAGE_FORMAT,
    "title_image": constant.IMAGE_FORMAT,
    "preview_music": constant.AUDIO_FORMAT,
}


# 곡 이름 끝의 난이도·모드 표기([ANOTHER], (7KEYS), -HYPER-, 【SP】 등). 같은 곡의 차트마다 붙는 것이라 이름 비교에서 뺍니다.
_TITLE_SUFFIX = re.compile(r"\s*(\[[^\[\]]*\]|\([^()]*\)|【[^【】]*】|<[^<>]*>|-[^-]+-|~[^~]+~)\s*$")
META_KEY_LENGTH = 255


# 비교에 쓰는 차트 헤더. 순서가 chart_meta_keys와 chart의 *_key 컬럼 순서입니다.
META_HEADERS = {"title": b"#TITLE", "artist": b"#ARTIST", "bpm": b"#BPM", "genre": b"#GENRE"}


class ChartEncodingError(ValueError):
    """BMS 차트의 헤더를 CHART_ENCODINGS 중 어느 것으로도 다 풀 수 없습니다."""


def chart_encoding(chart: bytes) -> str:
    """BMS 차트의 헤더 줄('#'로 시작) 전체를 오류 없이 풀 수 있는 인코딩 중 이상한 문자가 가장 적은 것(charset.guess_encoding).
    차트 하나는 한 인코딩으로 쓰므로 헤더마다 따로 고르지 않습니다. 풀 수 없으면 대체하지 않고 ChartEncodingError."""
    lines = [line for line in chart.splitlines() if line.lstrip().startswith(b"#") and not line.isascii()]
    if not lines:
        return "utf-8"
    encoding, failures = charset.guess_encoding(lines, CHART_ENCODINGS)
    if encoding is None:
        raise ChartEncodingError("차트 헤더의 인코딩을 알 수 없습니다(" + "; ".join(failures) + ")")
    return encoding


def _bmson(chart: bytes) -> dict | None:
    """bmson(JSON) 차트면 최상위 객체, 아니면 None. BMS 차트는 '{'로 시작하지 않습니다."""
    if not chart.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"{"):
        return None
    for encoding in ("utf-8-sig", *CHART_ENCODINGS[1:]):
        try:
            data = json.loads(chart.decode(encoding))
        except (UnicodeDecodeError, ValueError):
            continue
        return data if isinstance(data, dict) else None
    return None


def _bmson_info(data: dict) -> dict:
    info = data.get("info")
    return info if isinstance(info, dict) else {}


def _bmson_text(value) -> str:
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, (int, float)):
        return f"{value:g}" if isinstance(value, float) else str(value)
    return value.strip() if isinstance(value, str) else ""


def chart_meta(chart: bytes) -> dict[str, str]:
    """차트의 {title, artist, bpm, genre} 헤더 값(처음 나온 것). 없으면 ''. 헤더 인코딩을 알 수 없으면 ChartEncodingError.
    bmson은 info의 title·artist·init_bpm·genre입니다(subtitle은 BMS #TITLE의 난이도 표기와 같아 뺍니다)."""
    data = _bmson(chart)
    if data is not None:
        info = _bmson_info(data)
        return {
            "title": _bmson_text(info.get("title")),
            "artist": _bmson_text(info.get("artist")),
            "bpm": _bmson_text(info.get("init_bpm")),
            "genre": _bmson_text(info.get("genre")),
        }
    encoding = chart_encoding(chart)
    meta = {}
    for line in chart.splitlines():
        line = line.strip()
        if not line.startswith(b"#"):
            continue
        for field, header in META_HEADERS.items():
            if field not in meta and line[:len(header)].upper() == header and line[len(header):len(header) + 1] in (b" ", b"\t"):
                meta[field] = line[len(header):].strip().decode(encoding)
        if len(meta) == len(META_HEADERS):
            break
    return {field: meta.get(field, "") for field in META_HEADERS}


def _meta_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).lower().split())


def title_key(title: str) -> str:
    """곡 이름 비교 키. 대소문자·전각·공백을 맞추고 끝의 난이도 표기를 뺍니다. 다 빠지면 원래 이름."""
    key = _meta_text(title)
    while True:
        stripped = _TITLE_SUFFIX.sub("", key)
        if stripped == key or not stripped:
            break
        key = stripped
    return key[:META_KEY_LENGTH]


def artist_key(artist: str) -> str:
    """작곡가 비교 키. 'obj:' 같은 채보 제작자 표기는 보통 '/' 뒤나 괄호에 붙으므로 그 앞만 씁니다."""
    key = _meta_text(artist).split("/")[0].strip()
    return title_key(key) if key else ""


def bpm_key(bpm: str) -> str:
    """BPM 비교 키. 숫자로 읽어 소수 둘째 자리까지(150.00 → '150'). 숫자가 아니거나 0 이하면 ''."""
    try:
        value = float(bpm.strip())
    except ValueError:
        return ""
    return f"{round(value, 2):g}" if 0 < value < 1e9 else ""


def meta_keys(meta: dict[str, str]) -> dict[str, str]:
    """chart_meta 값의 비교 키 {title, artist, bpm, genre}."""
    return {
        "title": title_key(meta["title"]),
        "artist": artist_key(meta["artist"]),
        "bpm": bpm_key(meta["bpm"]),
        "genre": _meta_text(meta["genre"])[:META_KEY_LENGTH],
    }


def chart_meta_keys(chart: bytes) -> tuple[str, ...]:
    """(title_key, artist_key, bpm_key, genre_key)."""
    return tuple(meta_keys(chart_meta(chart)).values())


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


def _bmson_refs(data: dict, base: str) -> tuple[list[tuple[str, tuple[str, ...]]], set[str]]:
    """bmson 차트가 가리키는 파일(_chart_refs와 같은 형식). info의 이미지·프리뷰가 사전, sound_channels·bga_header가 키음·BGA입니다."""
    def path(name) -> str | None:
        if not isinstance(name, str) or not name.strip():
            return None
        return posixpath.normpath(posixpath.join(base, name.strip().replace("\\", "/"))).lower()

    pre_targets = []
    info = _bmson_info(data)
    for field, formats in BMSON_PRE_FIELDS.items():
        target = path(info.get(field))
        if target is not None and posixpath.splitext(target)[1] in formats:
            pre_targets.append((target, formats))
    play_stems = set()
    bga = data.get("bga")
    items = [
        *(data.get("sound_channels") or []),
        *((bga.get("bga_header") or []) if isinstance(bga, dict) else []),
    ]
    for item in items:
        target = path(item.get("name")) if isinstance(item, dict) else None
        if target is not None:
            play_stems.add(posixpath.splitext(target)[0])
    return pre_targets, play_stems


def _chart_refs(chart: bytes, base: str) -> tuple[list[tuple[str, tuple[str, ...]]], set[str]]:
    """차트가 가리키는 파일. (사전 헤더의 (소문자 경로, 대체 가능한 확장자) 목록, 키음·BGA의 소문자 확장자 뺀 경로)를
    반환합니다. 사전 헤더 값의 확장자가 그 헤더의 형식이 아니면(확장자 없음 포함) 무시합니다. base는 차트가 있는 폴더입니다."""
    data = _bmson(chart)
    if data is not None:
        return _bmson_refs(data, base)
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


def chart_sounds(chart: bytes, base: str) -> list[frozenset[str]]:
    """차트가 쓰는 키음(#WAV, bmson은 sound_channels). 키음마다 소문자 확장자 뺀 경로 후보(헤더 값의 인코딩 후보별)의 집합입니다."""
    data = _bmson(chart)
    if data is not None:
        stems = set()
        for item in data.get("sound_channels") or []:
            name = item.get("name") if isinstance(item, dict) else None
            if isinstance(name, str) and name.strip():
                path = posixpath.normpath(posixpath.join(base, name.strip().replace("\\", "/"))).lower()
                stems.add(frozenset([posixpath.splitext(path)[0]]))
        return sorted(stems, key=sorted)
    stems = set()
    for line in chart.splitlines():
        line = line.strip()
        if line[:4].upper() == b"#WAV" and PLAY_HEADER.match(line):
            paths = _header_paths(line[6:].strip(), base)
            if paths:
                stems.add(frozenset(posixpath.splitext(path)[0] for path in paths))
    return sorted(stems, key=sorted)


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


def _zip_count(data: bytes) -> int:
    """zip 항목 수. 비어 있으면 0."""
    if not data:
        return 0
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return len(zf.infolist())


class _Progress:
    """오래 걸리는 기존 곡 처리의 진행도를 BACKFILL_PROGRESS_SECONDS마다 로그로 남깁니다(서버 시작·migrate)."""

    def __init__(self, logger: logging.Logger, name: str, total: int):
        self.logger = logger
        self.name = name
        self.total = total
        self.start = self.last = time.monotonic()

    def update(self, done: int) -> None:
        now = time.monotonic()
        if done < self.total and now - self.last < constant.BACKFILL_PROGRESS_SECONDS:
            return
        self.last = now
        elapsed = now - self.start
        eta = f", about {elapsed / done * (self.total - done):.0f}s left" if 0 < done < self.total else ""
        self.logger.info(f"{self.name}: {done}/{self.total} ({done * 100 // max(self.total, 1)}%, {elapsed:.0f}s{eta})")


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


class ChartInUseError(ValueError):
    """복원하려는 곡의 차트가 그 사이 다른 곡에 들어가 있습니다."""

    def __init__(self, song_ids: list[int]):
        super().__init__(f"charts are used by songs {song_ids}")
        self.song_ids = song_ids


class BlobReader:
    """곡 zip·청크를 나눠 읽습니다. 저장소 파일(path)이 있으면 파일을 열어 두고 읽고, 없으면(옮기기 전) DB BLOB을
    매 chunk 읽기마다 단기 연결로 읽습니다. 파일은 만들 때 열어 두므로 그 뒤 새 판으로 바뀌어 옛 파일이 지워져도 끝까지 읽습니다.
    DB BLOB을 읽는 도중 마이그레이션이 같은 내용을 저장소 파일로 옮기면(조각이 지워짐) 그 파일을 열어 이어 읽습니다."""

    def __init__(self, table: str, row_id: int, size: int, sha256: str, path: str | None = None):
        self.table = table
        self.row_id = row_id
        self.size = size
        self.sha256 = sha256
        self.path = path
        self._fd = os.open(store.full_path(path), os.O_RDONLY) if path else None

    def _open_moved_file(self) -> None:
        """읽던 DB BLOB이 사라졌을 때 옮겨진 저장소 파일을 엽니다. 내용(sha256)이 바뀌었으면 이어 읽지 않고 실패합니다."""
        with connect() as con, con.cursor() as cur:
            cur.execute(f"SELECT path, sha256 FROM {self.table} WHERE id = %s", (self.row_id,))
            row = cur.fetchone()
        if row is None or row[0] is None or row[1] != self.sha256:
            raise RuntimeError(f"{self.table} {self.row_id} changed while reading")
        self.path = row[0]
        self._fd = os.open(store.full_path(self.path), os.O_RDONLY)

    def read(self, start: int, length: int) -> bytes:
        """start부터 length 바이트를 읽습니다."""
        if self._fd is None:
            data = self._read_db(start, length)
            if data is not None:
                return data
            self._open_moved_file()
        return os.pread(self._fd, length, start)

    def _read_db(self, start: int, length: int) -> bytes | None:
        """DB BLOB에서 읽습니다. 옮겨져 없으면 None."""
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
                row = cur.fetchone()
                return row[0] if row else None
        else:
            raise ValueError(self.table)

    def iter_range(self, start: int, end: int) -> Iterator[bytes]:
        """[start, end] 바이트(양끝 포함)를 BLOB_READ_SIZE 단위로 읽습니다."""
        step = constant.BLOB_READ_SIZE
        pos = start
        if self._fd is None:
            for piece in self._iter_db(start, end):
                if piece is None:
                    self._open_moved_file()
                    break
                pos += len(piece)
                yield piece
        if self._fd is not None:
            for p in range(pos, end + 1, step):
                yield os.pread(self._fd, min(step, end + 1 - p), p)

    def _iter_db(self, start: int, end: int) -> Iterator[bytes | None]:
        """DB BLOB의 [start, end]를 나눠 읽습니다. 그 사이 옮겨져 다음 조각이 없으면 None을 내고 멈춥니다."""
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
                    if row is None:
                        yield None
                        return
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
                    if row is None or row[0] is None:
                        yield None
                        return
                    yield row[0]
        elif self.table == "pre_chunk":
            for seq in range(start // step, end // step + 1):
                with connect() as con, con.cursor() as cur:
                    cur.execute(
                        "SELECT data FROM pre_chunk_part WHERE chunk_id = %s AND seq = %s",
                        (self.row_id, seq),
                    )
                    row = cur.fetchone()
                if row is None:
                    yield None
                    return
                chunk_start = seq * step
                yield row[0][max(0, start - chunk_start) : end + 1 - chunk_start]
        elif self.table == "chart_chunk":
            # SUBSTRING도 InnoDB가 BLOB 전체를 읽은 뒤 자르므로 step마다 나눠 읽으면 64MB 청크 하나에 4GB를 읽습니다.
            # 차트 청크는 BYTE_PER_CHUNK 이하라 한 번에 읽고 나눠 보냅니다. 옮기면 data를 빈 값으로 둡니다.
            with connect() as con, con.cursor() as cur:
                cur.execute(
                    "SELECT SUBSTRING(data, %s, %s) FROM chart_chunk WHERE id = %s",
                    (start + 1, end - start + 1, self.row_id),
                )
                row = cur.fetchone()
            if row is None or not row[0]:
                yield None
                return
            data = row[0]
            for pos in range(0, len(data), step):
                yield data[pos : pos + step]
        else:
            raise ValueError(self.table)

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def __del__(self):
        self.close()


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
            for table, name, column in MISSING_INDEXES:
                cur.execute(
                    """
                    SELECT COUNT(*) FROM information_schema.STATISTICS
                    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND INDEX_NAME = %s
                    """,
                    (table, name),
                )
                if cur.fetchone()[0] == 0:
                    cur.execute(f"ALTER TABLE {table} ADD INDEX {name} ({column})")
                    self.logger.info(f"generate_database: Added index {table}.{name}")
            for table, column in DROPPED_COLUMNS:
                cur.execute(
                    """
                    SELECT COUNT(*) FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s
                    """,
                    (table, column),
                )
                if cur.fetchone()[0] == 0:
                    continue
                if (table, column) == ("user_identity", "name"):
                    # 예전에는 user.display_name도 OAuth 계정 이름으로 채웠습니다. 이 컬럼이 남아 있으면 아직
                    # 정리하지 않은 DB이므로 표시 이름도 비우고, 다음 로그인 때 닉네임을 새로 정하게 합니다.
                    cur.execute("UPDATE user SET display_name = ''")
                    self.logger.info("generate_database: Cleared user.display_name taken from OAuth")
                cur.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
                self.logger.info(f"generate_database: Dropped column {table}.{column}")
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

    def _append_charts_to_chunk(self, cur, charts: list[tuple[str, bytes]]) -> None:
        """마지막 chart chunk에 chart (항목 이름, 내용)들을 추가합니다. 청크마다 CHARTS_PER_CHUNK개까지 넣고,
        넘치거나 청크가 BYTE_PER_CHUNK를 넘으면 다음 번호의 새 청크에 이어 넣습니다."""
        cur.execute("SELECT id FROM chart_chunk ORDER BY id DESC LIMIT 1 FOR UPDATE")
        row = cur.fetchone()
        if row is None:
            chunk_no, data = 0, b""
        else:
            chunk_no = row[0]
            data = self._chart_chunk_data(cur, chunk_no)
        count = _zip_count(data)

        pending = list(charts)
        while pending:
            if count >= constant.CHARTS_PER_CHUNK or len(data) > constant.BYTE_PER_CHUNK:
                chunk_no, data, count = chunk_no + 1, b"", 0
            take, pending = pending[: constant.CHARTS_PER_CHUNK - count], pending[constant.CHARTS_PER_CHUNK - count :]
            buf = io.BytesIO(data)
            with zipfile.ZipFile(buf, mode="a", compression=zipfile.ZIP_STORED) as zf:
                for arcname, content in take:
                    zf.writestr(zipfile.ZipInfo(arcname, date_time=(1980, 1, 1, 0, 0, 0)), content)
            data = buf.getvalue()
            count += len(take)
            self._write_chart_chunk(cur, chunk_no, data)

    def _write_chart_chunk(self, cur, chunk_no: int, data: bytes) -> None:
        """차트 청크를 저장소 파일로 쓰고 행을 그 파일로 바꿉니다. 옛 파일(또는 data)은 비웁니다."""
        sha256 = hashlib.sha256(data).hexdigest()
        path = store.write(store.chart_chunk_path(chunk_no, sha256), data)
        cur.execute(
            """
            INSERT INTO chart_chunk (id, size, sha256, data, path) VALUES (%s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE size = VALUES(size), sha256 = VALUES(sha256), data = VALUES(data), path = VALUES(path)
            """,
            (chunk_no, len(data), sha256, b"", path),
        )

    def _chart_chunk_data(self, cur, chunk_no: int) -> bytes | None:
        """차트 청크 내용. 없는 청크면 None."""
        cur.execute("SELECT path FROM chart_chunk WHERE id = %s", (chunk_no,))
        row = cur.fetchone()
        if row is None:
            return None
        if row[0]:
            return store.read(row[0])
        cur.execute("SELECT data FROM chart_chunk WHERE id = %s", (chunk_no,))
        return cur.fetchone()[0]

    def get_chart_chunk_data(self, chunk_no: int) -> bytes | None:
        with connect() as con, con.cursor() as cur:
            return self._chart_chunk_data(cur, chunk_no)

    def _setting(self, cur, name: str) -> str | None:
        cur.execute("SELECT value FROM setting WHERE name = %s", (name,))
        row = cur.fetchone()
        return None if row is None else row[0]

    def _set_setting(self, cur, name: str, value: str | None) -> None:
        cur.execute("DELETE FROM setting WHERE name = %s", (name,))
        if value is not None:
            cur.execute("INSERT INTO setting (name, value) VALUES (%s, %s)", (name, value))

    def rechunk_charts(self) -> int:
        """차트 청크의 크기(CHART_CHUNK_SETTING)가 CHARTS_PER_CHUNK와 다르면 모든 차트를 CHARTS_PER_CHUNK개씩 다시 묶습니다.
        새 청크를 만든 수를 반환합니다.

        새 청크는 기존 마지막 번호 뒤에 만들고 다 만든 뒤 옛 청크를 지우므로, 그 사이에 동기화한 클라이언트는 차트를
        두 번 볼 뿐 빠뜨리지 않습니다. 진행 단계를 setting에 남겨 도중에 꺼지면 다음 시작 때 이어서 합니다.
        끝날 때까지 쓰기 잠금을 잡으므로 그동안 임포트는 기다립니다. md5가 없는 차트는 이때 함께 채웁니다."""
        with connect() as con, con.cursor() as cur:
            if self._setting(cur, CHART_CHUNK_SETTING) == str(constant.CHARTS_PER_CHUNK):
                return 0
        with self._write_lock:
            with connect() as con, con.cursor() as cur:
                old_max = self._setting(cur, CHART_RECHUNK_SETTING)
                phase = self._setting(cur, CHART_RECHUNK_PHASE_SETTING)
                cur.execute("SELECT id FROM chart_chunk ORDER BY id")
                ids = [row[0] for row in cur.fetchall()]
                if old_max is None:
                    if not ids:
                        self._set_setting(cur, CHART_CHUNK_SETTING, str(constant.CHARTS_PER_CHUNK))
                        con.commit()
                        return 0
                    old_max, phase = str(ids[-1]), "copy"
                    self._set_setting(cur, CHART_RECHUNK_SETTING, old_max)
                    self._set_setting(cur, CHART_RECHUNK_PHASE_SETTING, phase)
                    con.commit()
                old_max = int(old_max)
                old_ids = [i for i in ids if i <= old_max]
                if phase == "copy":
                    # 지난번에 만들다 만 새 청크는 지우고 처음부터 다시 묶습니다.
                    for chunk_no in (i for i in ids if i > old_max):
                        cur.execute("DELETE FROM chart_chunk WHERE id = %s", (chunk_no,))
                    cur.execute("SELECT id FROM chart WHERE md5 IS NULL")
                    no_md5 = {row[0] for row in cur.fetchall()}
                    con.commit()
            self.logger.info(f"rechunk_charts: {phase} phase, {len(old_ids)} old chart chunks.")

            made = 0
            if phase == "copy":
                made = self._copy_charts_to_new_chunks(old_ids, old_max + 1, no_md5)
                with connect() as con, con.cursor() as cur:
                    self._set_setting(cur, CHART_RECHUNK_PHASE_SETTING, "delete")
                    con.commit()
            progress = _Progress(self.logger, "rechunk_charts(delete)", len(old_ids))
            for i, chunk_no in enumerate(old_ids, 1):
                with connect() as con, con.cursor() as cur:
                    cur.execute("DELETE FROM chart_chunk WHERE id = %s", (chunk_no,))
                    con.commit()
                progress.update(i)
            with connect() as con, con.cursor() as cur:
                self._set_setting(cur, CHART_RECHUNK_SETTING, None)
                self._set_setting(cur, CHART_RECHUNK_PHASE_SETTING, None)
                self._set_setting(cur, CHART_CHUNK_SETTING, str(constant.CHARTS_PER_CHUNK))
                con.commit()
        self.logger.info(f"rechunk_charts: Rewrote {len(old_ids)} chart chunks into {made}.")
        return made

    def _copy_charts_to_new_chunks(self, old_ids: list[int], first_id: int, no_md5: set[str]) -> int:
        """old_ids 청크의 차트를 순서대로 CHARTS_PER_CHUNK개씩 first_id부터 새 청크에 씁니다. 새 청크마다 커밋합니다."""
        chunk_no = first_id
        entries: list[tuple[str, bytes]] = []
        size = 0
        md5s: list[tuple[str, str]] = []

        def flush() -> None:
            nonlocal chunk_no, entries, size, md5s
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_STORED) as zf:
                for arcname, content in entries:
                    zf.writestr(zipfile.ZipInfo(arcname, date_time=(1980, 1, 1, 0, 0, 0)), content)
            with connect() as con, con.cursor() as cur:
                try:
                    self._write_chart_chunk(cur, chunk_no, buf.getvalue())
                    for md5, sha256 in md5s:
                        cur.execute("UPDATE chart SET md5 = %s WHERE id = %s AND md5 IS NULL", (md5, sha256))
                    con.commit()
                except Exception:
                    con.rollback()
                    raise
            chunk_no, entries, size, md5s = chunk_no + 1, [], 0, []

        progress = _Progress(self.logger, "rechunk_charts(copy, old chunks)", len(old_ids))
        for i, old in enumerate(old_ids, 1):
            data = self.get_chart_chunk_data(old)
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                for info in zf.infolist():
                    content = zf.read(info)
                    sha256 = info.filename.rsplit(".", 1)[0]
                    if sha256 in no_md5:
                        md5s.append((hashlib.md5(content).hexdigest(), sha256))
                    if entries and (
                        len(entries) >= constant.CHARTS_PER_CHUNK or size + len(content) > constant.BYTE_PER_CHUNK
                    ):
                        flush()
                    entries.append((info.filename, content))
                    size += len(content)
            progress.update(i)
        if entries:
            flush()
        return chunk_no - first_id

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

        previews = self._previews(cur, lo, hi)
        cur.execute(
            "SELECT id, folder, size, sha256, files FROM song WHERE id >= %s AND id < %s ORDER BY id",
            (lo, hi),
        )
        songs = []
        for song_id, folder, size, sha256, files in cur.fetchall():
            song = {
                "song_id": song_id,
                "folder": folder,
                "zip_size": size,
                "zip_sha256": sha256,
                "charts": charts[song_id],
                "chart_files": chart_files[song_id],
                "files": json.loads(files) if files else [],
            }
            preview = previews.get(song_id)
            if preview is not None and preview["song_sha256"] == sha256:
                # 곡 zip에 없는 사전 파일. 사전 청크와 /api/pre/song/{id}/file로 받습니다.
                song["preview"] = {k: preview[k] for k in ("path", "size", "crc32")}
            songs.append(song)
        body = json.dumps(songs, ensure_ascii=False, separators=(",", ":")).encode()
        data = gzip.compress(body, mtime=0)
        cur.execute(
            """
            INSERT INTO manifest_chunk (id, sha256, data) VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE sha256 = VALUES(sha256), data = VALUES(data)
            """,
            (chunk_no, hashlib.sha256(body).hexdigest(), data),
        )

    def _previews(self, cur, lo: int, hi: int) -> dict[int, dict]:
        """song id가 [lo, hi)인 곡 중 지금 판의 미리듣기가 있는 곡. 곡 zip이 바뀌었는지는 song_sha256으로 봅니다."""
        cur.execute(
            """
            SELECT song_id, song_sha256, path, size, crc32 FROM song_preview
            WHERE song_id >= %s AND song_id < %s AND version = %s AND path IS NOT NULL
            """,
            (lo, hi, preview.PREVIEW_VERSION),
        )
        return {
            row[0]: {"song_sha256": row[1], "path": row[2], "size": row[3], "crc32": row[4]}
            for row in cur.fetchall()
        }

    def get_preview(self, song_id: int, path: str) -> tuple[dict, bytes] | None:
        """곡의 지금 미리듣기가 path면 (매니페스트 항목, 데이터). 아니면 None."""
        with connect() as con, con.cursor() as cur:
            cur.execute(
                """
                SELECT p.size, p.crc32, p.data FROM song_preview p JOIN song s ON s.id = p.song_id
                WHERE p.song_id = %s AND p.path = %s AND p.version = %s AND p.song_sha256 = s.sha256
                """,
                (song_id, path, preview.PREVIEW_VERSION),
            )
            row = cur.fetchone()
        if row is None or row[2] is None:
            return None
        return {"path": path, "size": row[0], "crc32": row[1]}, bytes(row[2])

    def render_previews(self) -> int:
        """미리듣기가 없거나 판·곡 zip이 바뀐 곡의 미리듣기를 만듭니다(preview.py). 바뀐 곡 수를 반환합니다.
        렌더링은 쓰기 잠금 밖에서 하고, 사전 청크(SONGS_PER_PRE_CHUNK곡)마다 결과를 넣고 청크를 다시 만들어 커밋합니다.
        도중에 꺼져도 다음 시작 때 남은 곡부터 이어서 만듭니다."""
        with connect() as con, con.cursor() as cur:
            cur.execute(
                """
                SELECT s.id, s.sha256 FROM song s LEFT JOIN song_preview p ON p.song_id = s.id
                WHERE p.song_id IS NULL OR p.song_sha256 <> s.sha256 OR p.version <> %s
                ORDER BY s.id
                """,
                (preview.PREVIEW_VERSION,),
            )
            pending = cur.fetchall()
        if not pending:
            return 0
        self.logger.info(f"render_previews: Rendering previews of {len(pending)} songs.")
        groups: dict[int, list[tuple[int, str]]] = defaultdict(list)
        for song_id, sha256 in pending:
            groups[song_id // constant.SONGS_PER_PRE_CHUNK].append((song_id, sha256))
        done = seen = 0
        progress = _Progress(self.logger, "render_previews", len(pending))
        for chunk_no, songs in sorted(groups.items()):
            progress.update(seen)
            seen += len(songs)
            results = []
            for song_id, sha256 in songs:
                files = self.get_song_files(song_id)
                if files is None:
                    continue
                path = out = None
                try:
                    with connect() as con, con.cursor() as cur:
                        rendered = preview.render(files, lambda entry: self._read_song_member(cur, song_id, entry))
                    note = "" if rendered else "skipped"
                    if rendered:
                        path, out = rendered
                except RuntimeError as e:
                    if str(e) == "ffmpeg not found":
                        self.logger.warning("render_previews: ffmpeg not found. Previews are not rendered.")
                        return done
                    self.logger.warning(f"render_previews: Song[{song_id}] failed: {e}")
                    note = f"error: {e}"[:255]
                except Exception as e:
                    self.logger.exception(f"render_previews: Song[{song_id}] failed")
                    note = f"error: {type(e).__name__}"[:255]
                results.append((song_id, sha256, path, out, note))
            if not results:
                continue
            with self._write_lock, connect() as con, con.cursor() as cur:
                try:
                    changed = set()
                    for song_id, sha256, path, out, note in results:
                        cur.execute("SELECT sha256 FROM song WHERE id = %s", (song_id,))
                        row = cur.fetchone()
                        if row is None or row[0] != sha256:
                            # 렌더링하는 동안 곡이 바뀌거나 지워졌으면 다음에 다시 만듭니다.
                            continue
                        cur.execute("SELECT path FROM song_preview WHERE song_id = %s", (song_id,))
                        old = cur.fetchone()
                        cur.execute("DELETE FROM song_preview WHERE song_id = %s", (song_id,))
                        cur.execute(
                            """
                            INSERT INTO song_preview (song_id, song_sha256, version, path, size, crc32, data, note)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                            """,
                            (
                                song_id, sha256, preview.PREVIEW_VERSION, path, len(out) if out else 0,
                                f"{zlib.crc32(out):08x}" if out else None, out, note,
                            ),
                        )
                        if out is not None or (old is not None and old[0] is not None):
                            changed.add(song_id)
                    if changed:
                        self._rebuild_pre_chunk(cur, chunk_no)
                        for manifest_no in {i // constant.SONGS_PER_MANIFEST_CHUNK for i in changed}:
                            self._rebuild_manifest_chunk(cur, manifest_no)
                    con.commit()
                except Exception:
                    con.rollback()
                    raise
            done += len(changed)
        progress.update(len(pending))
        self.logger.info(f"render_previews: Rendered {done} previews.")
        return done

    def _read_song_range(self, cur, song_id: int, start: int, length: int) -> bytes:
        """곡 zip의 start부터 length 바이트. 같은 트랜잭션에서 넣은 곡도 읽도록 cur를 씁니다."""
        if length <= 0:
            return b""
        cur.execute("SELECT path FROM song WHERE id = %s", (song_id,))
        row = cur.fetchone()
        if row is not None and row[0]:
            return store.read_range(row[0], start, length)
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
        previews = self._previews(cur, lo, hi)
        cur.execute("SELECT id, files, sha256 FROM song WHERE id >= %s AND id < %s ORDER BY id", (lo, hi))
        songs = cur.fetchall()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_STORED) as zf:
            for song_id, files, sha256 in songs:
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
                preview_row = previews.get(song_id)
                if preview_row is not None and preview_row["song_sha256"] == sha256:
                    cur.execute("SELECT data FROM song_preview WHERE song_id = %s", (song_id,))
                    info = zipfile.ZipInfo(pre_arcname(song_id, preview_row["path"]), date_time=(1980, 1, 1, 0, 0, 0))
                    zf.writestr(info, bytes(cur.fetchone()[0]))
        data = buf.getvalue()

        sha256 = hashlib.sha256(data).hexdigest()
        path = store.write(store.pre_chunk_path(chunk_no, sha256), data)
        cur.execute("DELETE FROM pre_chunk_part WHERE chunk_id = %s", (chunk_no,))
        cur.execute(
            """
            INSERT INTO pre_chunk (id, size, sha256, path) VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE size = VALUES(size), sha256 = VALUES(sha256), path = VALUES(path)
            """,
            (chunk_no, len(data), sha256, path),
        )

    def backfill_pre_chunks(self) -> int:
        """곡은 있는데 사전 청크가 없는 구간(이전 버전에서 넣은 곡)의 사전 청크를 만듭니다. 만든 청크 수를 반환합니다.
        청크마다 따로 커밋하므로 도중에 꺼져도 다음 시작 때 남은 청크부터 이어서 만듭니다."""
        with self._write_lock, connect() as con, con.cursor() as cur:
            if self._setting(cur, PRE_CHUNK_SETTING) != str(constant.SONGS_PER_PRE_CHUNK):
                # 청크 크기가 바뀌었으면 모두 다시 만듭니다. 조각(pre_chunk_part)은 청크를 다시 만들 때 덮어쓰고,
                # 남는 번호의 조각은 아래에서 지웁니다. 다 만들기 전에는 클라이언트가 파일별 사전 API로 받습니다.
                cur.execute("SELECT COUNT(*) FROM pre_chunk")
                if cur.fetchone()[0]:
                    self.logger.info(
                        f"backfill_pre_chunks: Rebuilding every pre chunk with {constant.SONGS_PER_PRE_CHUNK} songs."
                    )
                cur.execute("DELETE FROM pre_chunk")
                self._set_setting(cur, PRE_CHUNK_SETTING, str(constant.SONGS_PER_PRE_CHUNK))
                con.commit()
        with connect() as con, con.cursor() as cur:
            cur.execute("SELECT id FROM song")
            wanted = {row[0] // constant.SONGS_PER_PRE_CHUNK for row in cur.fetchall()}
            cur.execute("SELECT id FROM pre_chunk")
            missing = sorted(wanted - {row[0] for row in cur.fetchall()})
        if missing:
            self.logger.info(f"backfill_pre_chunks: Building {len(missing)} pre chunks.")
        progress = _Progress(self.logger, "backfill_pre_chunks", len(missing))
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
            progress.update(i)
        if missing:
            self.logger.info(f"backfill_pre_chunks: Built {len(missing)} pre chunks.")
        self._drop_orphan_pre_parts()
        return len(missing)

    def _drop_orphan_pre_parts(self) -> None:
        """청크가 없는 사전 청크 조각(청크 크기를 바꾸기 전 번호)을 지웁니다."""
        with connect() as con, con.cursor() as cur:
            cur.execute("SELECT DISTINCT chunk_id FROM pre_chunk_part")
            parts = {row[0] for row in cur.fetchall()}
            cur.execute("SELECT id FROM pre_chunk")
            orphans = sorted(parts - {row[0] for row in cur.fetchall()})
        for chunk_no in orphans:
            with self._write_lock, connect() as con, con.cursor() as cur:
                cur.execute("SELECT id FROM pre_chunk WHERE id = %s", (chunk_no,))
                if cur.fetchone() is None:
                    cur.execute("DELETE FROM pre_chunk_part WHERE chunk_id = %s", (chunk_no,))
                con.commit()
        if orphans:
            self.logger.info(f"backfill_pre_chunks: Dropped parts of {len(orphans)} old pre chunks.")

    def backfill_chart_md5(self) -> int:
        """md5가 없는 차트(이전 버전에서 넣은 차트)의 md5를 차트 청크에서 채웁니다. 채운 차트 수를 반환합니다.
        청크마다 따로 커밋하므로 도중에 꺼져도 다음 시작 때 남은 차트부터 이어서 채웁니다."""
        with connect() as con, con.cursor() as cur:
            cur.execute("SELECT id FROM chart WHERE md5 IS NULL")
            missing = {row[0] for row in cur.fetchall()}
            if not missing:
                return 0
            cur.execute("SELECT id FROM chart_chunk ORDER BY id")
            chunk_ids = [row[0] for row in cur.fetchall()]
        self.logger.info(f"backfill_chart_md5: Filling md5 of {len(missing)} charts.")
        filled = 0
        broken = False
        progress = _Progress(self.logger, "backfill_chart_md5(chart chunks)", len(chunk_ids))
        for n, chunk_no in enumerate(chunk_ids):
            progress.update(n)
            data = self.get_chart_chunk_data(chunk_no)
            if data is None:
                continue
            found = []
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as zf:
                    for info in zf.infolist():
                        sha256 = info.filename.rsplit(".", 1)[0]
                        if sha256 in missing:
                            found.append((hashlib.md5(zf.read(info)).hexdigest(), sha256))
            except (zipfile.BadZipFile, zlib.error):
                self.logger.exception(f"backfill_chart_md5: Skipped broken chart chunk {chunk_no}")
                broken = True
                continue
            if not found:
                continue
            with self._write_lock, connect() as con, con.cursor() as cur:
                try:
                    for md5, sha256 in found:
                        cur.execute("UPDATE chart SET md5 = %s WHERE id = %s AND md5 IS NULL", (md5, sha256))
                    con.commit()
                except Exception:
                    con.rollback()
                    raise
            missing.difference_update(sha256 for _, sha256 in found)
            filled += len(found)
        progress.update(len(chunk_ids))
        self.logger.info(f"backfill_chart_md5: Filled md5 of {filled} charts.")
        if missing and not broken:
            # 어느 청크에도 없는 차트는 ''로 두어 시작할 때마다 청크를 다시 읽지 않게 합니다(난이도표 md5와 맞지 않음).
            self.logger.warning(f"backfill_chart_md5: {len(missing)} charts are not in any chart chunk.")
            with self._write_lock, connect() as con, con.cursor() as cur:
                for sha256 in missing:
                    cur.execute("UPDATE chart SET md5 = '' WHERE id = %s AND md5 IS NULL", (sha256,))
                con.commit()
        return filled

    def backfill_chart_meta(self) -> int:
        """비교 키(title_key, artist_key, bpm_key, genre_key)가 없는 차트(이전 버전에서 넣은 차트)를 차트 청크에서 채웁니다.
        채운 차트 수를 반환합니다. 청크마다 따로 커밋하므로 도중에 꺼져도 다음 시작 때 이어서 채웁니다."""
        with connect() as con, con.cursor() as cur:
            cur.execute("SELECT id FROM chart WHERE title_key IS NULL")
            missing = {row[0] for row in cur.fetchall()}
            if not missing:
                return 0
            cur.execute("SELECT id FROM chart_chunk ORDER BY id")
            chunk_ids = [row[0] for row in cur.fetchall()]
        self.logger.info(f"backfill_chart_meta: Filling meta keys of {len(missing)} charts.")
        filled = 0
        broken = False
        progress = _Progress(self.logger, "backfill_chart_meta(chart chunks)", len(chunk_ids))
        for n, chunk_no in enumerate(chunk_ids):
            progress.update(n)
            data = self.get_chart_chunk_data(chunk_no)
            if data is None:
                continue
            found = []
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as zf:
                    for info in zf.infolist():
                        sha256 = info.filename.rsplit(".", 1)[0]
                        if sha256 in missing:
                            try:
                                found.append((*chart_meta_keys(zf.read(info)), sha256))
                            except ChartEncodingError as e:
                                # 이미 들어간 차트라 비교 키만 비워 둡니다(다음 시작 때 다시 읽지 않도록).
                                self.logger.warning(f"backfill_chart_meta: {sha256}: {e}")
                                found.append(("", "", "", "", sha256))
            except (zipfile.BadZipFile, zlib.error):
                self.logger.exception(f"backfill_chart_meta: Skipped broken chart chunk {chunk_no}")
                broken = True
                continue
            if not found:
                continue
            with self._write_lock, connect() as con, con.cursor() as cur:
                try:
                    for *keys, sha256 in found:
                        cur.execute(
                            "UPDATE chart SET title_key = %s, artist_key = %s, bpm_key = %s, genre_key = %s"
                            " WHERE id = %s AND title_key IS NULL",
                            (*keys, sha256),
                        )
                    con.commit()
                except Exception:
                    con.rollback()
                    raise
            missing.difference_update(sha256 for *_, sha256 in found)
            filled += len(found)
        progress.update(len(chunk_ids))
        self.logger.info(f"backfill_chart_meta: Filled meta keys of {filled} charts.")
        if missing and not broken:
            # 어느 청크에도 없는 차트는 ''로 두어 시작할 때마다 청크를 다시 읽지 않게 합니다.
            self.logger.warning(f"backfill_chart_meta: {len(missing)} charts are not in any chart chunk.")
            with self._write_lock, connect() as con, con.cursor() as cur:
                for sha256 in missing:
                    cur.execute(
                        "UPDATE chart SET title_key = '', artist_key = '', bpm_key = '', genre_key = ''"
                        " WHERE id = %s AND title_key IS NULL", (sha256,)
                    )
                con.commit()
        return filled

    def backfill_chart_formats(self) -> int:
        """BMS_FORMAT에 새로 더한 확장자(.bmson 등)의 기존 곡 파일을 차트로 등록합니다. 등록한 차트 수를 반환합니다.

        이전에는 차트가 아니어서 곡 zip에만 있던 파일입니다. 곡 매니페스트에서 그 확장자 항목을 찾아 곡 zip에서 읽고,
        chart 테이블과 차트 청크에 넣습니다(insert_song과 같음). 같은 차트가 이미 있으면(다른 곡에 있거나 지운 곡에 남은 차트)
        지운 곡의 것만 이 곡에 다시 붙입니다. BACKFILL_BATCH_SONGS곡마다 커밋하고, 모두 끝나면 CHART_FORMATS_SETTING을
        현재 BMS_FORMAT으로 바꿉니다. 실패한 곡이 있으면 바꾸지 않아 다음 시작 때 다시 시도합니다(이미 넣은 차트는 건너뜀).
        pre/play kind는 FILE_KINDS_VERSION을 올려 backfill_file_kinds가 다시 판정합니다."""
        with connect() as con, con.cursor() as cur:
            saved = self._setting(cur, CHART_FORMATS_SETTING)
        old = tuple(saved.split(",")) if saved is not None else LEGACY_CHART_FORMATS
        added = [ext for ext in constant.BMS_FORMAT if ext not in old]
        if not added:
            if saved != ",".join(constant.BMS_FORMAT):
                with self._write_lock, connect() as con, con.cursor() as cur:
                    self._set_setting(cur, CHART_FORMATS_SETTING, ",".join(constant.BMS_FORMAT))
                    con.commit()
            return 0

        with connect() as con, con.cursor() as cur:
            # 매니페스트는 JSON이라 경로 뒤에 '"'가 붙습니다. 대소문자는 파이썬에서 다시 확인합니다.
            where = " OR ".join("LOWER(files) LIKE %s" for _ in added)
            cur.execute(f"SELECT id FROM song WHERE files IS NOT NULL AND ({where}) ORDER BY id", [f'%{ext}"%' for ext in added])
            song_ids = [row[0] for row in cur.fetchall()]
        if song_ids:
            self.logger.info(f"backfill_chart_formats: Checking {len(song_ids)} songs for {', '.join(added)} charts.")

        inserted = failed = 0
        step = constant.BACKFILL_BATCH_SONGS
        progress = _Progress(self.logger, "backfill_chart_formats", len(song_ids))
        for i in range(0, len(song_ids), step):
            with self._write_lock, connect() as con, con.cursor() as cur:
                try:
                    new_charts = []
                    for song_id in song_ids[i : i + step]:
                        cur.execute("SAVEPOINT song")
                        try:
                            new_charts += self._register_song_charts(cur, song_id, added)
                            cur.execute("RELEASE SAVEPOINT song")
                        except Exception:
                            cur.execute("ROLLBACK TO SAVEPOINT song")
                            failed += 1
                            self.logger.exception(f"backfill_chart_formats: Skipped song[{song_id}]")
                    if new_charts:
                        self._append_charts_to_chunk(cur, new_charts)
                    con.commit()
                    inserted += len(new_charts)
                except Exception:
                    con.rollback()
                    failed += 1
                    self.logger.exception(f"backfill_chart_formats: Skipped songs from song[{song_ids[i]}]")
            progress.update(min(i + step, len(song_ids)))

        if failed:
            self.logger.warning(f"backfill_chart_formats: {failed} songs failed. Retrying on next start.")
        else:
            with self._write_lock, connect() as con, con.cursor() as cur:
                self._set_setting(cur, CHART_FORMATS_SETTING, ",".join(constant.BMS_FORMAT))
                con.commit()
        if inserted or song_ids:
            self.logger.info(f"backfill_chart_formats: Registered {inserted} charts.")
        return inserted

    def _register_song_charts(self, cur, song_id: int, exts: list[str]) -> list[tuple[str, bytes]]:
        """곡 zip에서 확장자가 exts인 파일을 차트로 등록합니다. 차트 청크에 넣을 (항목 이름, 내용) 목록을 반환합니다."""
        cur.execute("SELECT files FROM song WHERE id = %s", (song_id,))
        row = cur.fetchone()
        files = json.loads(row[0]) if row and row[0] else []
        new_charts = []
        for entry in files:
            path = pathlib.PurePosixPath(entry["path"])
            if path.suffix.lower() not in exts:
                continue
            content = self._read_song_member(cur, song_id, entry)
            sha256 = hashlib.sha256(content).hexdigest()
            cur.execute(
                "INSERT IGNORE INTO chart (id, song_id, size, filename, md5, title_key, artist_key, bpm_key, genre_key)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (sha256, song_id, len(content), entry["path"], hashlib.md5(content).hexdigest(), *chart_meta_keys(content)),
            )
            if cur.rowcount == 1:
                new_charts.append((chart_arcname(sha256, path), content))
            else:
                cur.execute(
                    "UPDATE chart SET song_id = %s, filename = %s WHERE id = %s AND size = %s AND song_id IS NULL",
                    (song_id, entry["path"], sha256, len(content)),
                )
        return new_charts

    def get_song_data(self, song_id: int) -> bytes | None:
        """곡 id의 전체 zip data를 반환합니다. 저장소 파일, 없으면(옮기기 전) song_part 또는 song.data에서 읽어옵니다."""
        with connect() as con, con.cursor() as cur:
            return self._get_song_data_with_cur(cur, song_id)

    def _get_song_data_with_cur(self, cur, song_id: int) -> bytes | None:
        cur.execute("SELECT path FROM song WHERE id = %s", (song_id,))
        row = cur.fetchone()
        if row is None:
            return None
        if row[0]:
            return store.read(row[0])
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
        또한 chart 테이블에 filename이 채워지지 않은 항목의 파일명을 song zip에서 추출해 채웁니다.

        서버 시작 때 실행하므로 곡마다 예외를 잡아 로그를 남기고 건너뜁니다(손상된 zip 하나가 시작을 막지 않게, #45).
        BACKFILL_BATCH_SONGS곡마다 커밋하고, 건너뛴 곡은 다음 시작 때 다시 시도합니다."""
        # 1) 아주 오래된 song.data BLOB은 song_part를 거치지 않고 저장소 파일로 옮깁니다(곡마다 커밋).
        #    song_part에 있는 곡은 migrate_to_store가 서버를 켠 채로 천천히 옮깁니다.
        with connect() as con, con.cursor() as cur:
            cur.execute("SELECT id FROM song WHERE data IS NOT NULL AND path IS NULL ORDER BY id")
            legacy_ids = [row[0] for row in cur.fetchall()]
        if legacy_ids:
            self.logger.info(f"backfill_manifest: Moving {len(legacy_ids)} songs to the store.")
        progress = _Progress(self.logger, "backfill_manifest(store)", len(legacy_ids))
        for n, song_id in enumerate(legacy_ids, 1):
            with self._write_lock, connect() as con, con.cursor() as cur:
                try:
                    cur.execute("SELECT data FROM song WHERE id = %s AND data IS NOT NULL", (song_id,))
                    row = cur.fetchone()
                    if row is not None:
                        self._put_song_data(cur, song_id, row[0])
                    con.commit()
                except Exception:
                    con.rollback()
                    self.logger.exception(f"backfill_manifest: Skipped moving song[{song_id}] data to the store")
            progress.update(n)

        # 2) 매니페스트 항목·차트 파일명 채우기
        with connect() as con, con.cursor() as cur:
            cur.execute(
                "SELECT id FROM song WHERE files IS NULL OR files NOT LIKE %s OR files NOT LIKE %s ORDER BY id",
                ('%"kind"%', '%"data_offset"%'),
            )
            song_ids_no_files = set(row[0] for row in cur.fetchall())
            cur.execute("SELECT DISTINCT song_id FROM chart WHERE filename = '' AND song_id IS NOT NULL")
            song_ids_no_filenames = set(row[0] for row in cur.fetchall())

        affected_song_ids = sorted(song_ids_no_files | song_ids_no_filenames)
        if affected_song_ids:
            self.logger.info(f"backfill_manifest: Filling manifest of {len(affected_song_ids)} songs.")
        updated = failed = 0
        step = constant.BACKFILL_BATCH_SONGS
        progress = _Progress(self.logger, "backfill_manifest", len(affected_song_ids))
        for i in range(0, len(affected_song_ids), step):
            batch = affected_song_ids[i : i + step]
            with self._write_lock, connect() as con, con.cursor() as cur:
                try:
                    done = []
                    for song_id in batch:
                        cur.execute("SAVEPOINT song")
                        try:
                            self._backfill_song_manifest(
                                cur, song_id, song_id in song_ids_no_files, song_id in song_ids_no_filenames
                            )
                            cur.execute("RELEASE SAVEPOINT song")
                            done.append(song_id)
                        except Exception:
                            cur.execute("ROLLBACK TO SAVEPOINT song")
                            failed += 1
                            self.logger.exception(f"backfill_manifest: Skipped song[{song_id}]")
                    for chunk_no in sorted({s // constant.SONGS_PER_MANIFEST_CHUNK for s in done}):
                        self._rebuild_manifest_chunk(cur, chunk_no)
                    con.commit()
                    updated += len(done)
                except Exception:
                    con.rollback()
                    failed += len(batch)
                    self.logger.exception(f"backfill_manifest: Skipped songs {batch[0]}..{batch[-1]}")
            progress.update(i + len(batch))
        if updated:
            self.logger.info(f"backfill_manifest: Updated manifest of {updated} songs.")
        if failed:
            self.logger.warning(f"backfill_manifest: {failed} songs failed. Retrying on next start.")

    def _backfill_song_manifest(self, cur, song_id: int, fill_files: bool, fill_filenames: bool) -> None:
        """곡 하나의 매니페스트 항목(fill_files)·차트 파일명(fill_filenames)을 song zip에서 채웁니다."""
        song_data = self._get_song_data_with_cur(cur, song_id)
        if not song_data:
            return
        if fill_files:
            files = zip_entries(song_data)
            cur.execute(
                "UPDATE song SET folder = IF(folder = '', %s, folder), files = %s WHERE id = %s",
                (str(song_id), json.dumps(files), song_id),
            )
        if fill_filenames:
            with zipfile.ZipFile(io.BytesIO(song_data)) as zf:
                for info in zf.infolist():
                    if not info.is_dir() and pathlib.PurePosixPath(info.filename).suffix.lower() in constant.BMS_FORMAT:
                        sha256 = hashlib.sha256(zf.read(info)).hexdigest()
                        cur.execute(
                            "UPDATE chart SET filename = %s WHERE song_id = %s AND id = %s AND filename = ''",
                            (info.filename, song_id, sha256),
                        )

    def backfill_file_kinds(self) -> int:
        """pre/play 판정 규칙(FILE_KINDS_VERSION)이 바뀌었으면 기존 곡의 kind를 다시 판정합니다. kind가 바뀐 곡 수를 반환합니다.

        곡 zip 전체를 읽지 않고 매니페스트의 항목 목록과 차트 파일만 읽어 판정합니다. kind가 바뀐 곡은 같은 트랜잭션에서
        매니페스트 청크를 다시 만들고, 사전 청크는 지워 backfill_pre_chunks가 새 규칙으로 다시 만들게 합니다
        (그동안 클라이언트는 파일별 사전 API로 받고, 이 API도 새 kind를 봅니다).
        BACKFILL_BATCH_SONGS곡마다 커밋하고, 같은 트랜잭션에 어디까지 했는지(FILE_KINDS_PROGRESS_SETTING) 적어 두어
        도중에 꺼지면 다음 시작 때 그 뒤부터 이어서 합니다. 판정하지 못한 곡은 로그를 남기고 건너뛰며, 그때는 버전을 올리지 않고
        진행 기록도 지워 다음 시작 때 처음부터 다시 시도합니다.
        """
        with connect() as con, con.cursor() as cur:
            cur.execute("SELECT value FROM setting WHERE name = %s", (FILE_KINDS_SETTING,))
            row = cur.fetchone()
            if row is not None and int(row[0]) >= FILE_KINDS_VERSION:
                return 0
            after = 0
            version, _, last = (self._setting(cur, FILE_KINDS_PROGRESS_SETTING) or "").partition(":")
            if version == str(FILE_KINDS_VERSION) and last.isdigit():
                after = int(last)
            cur.execute("SELECT id FROM song WHERE id > %s ORDER BY id", (after,))
            song_ids = [r[0] for r in cur.fetchall()]
        resumed = f" after song[{after}]" if after else ""
        self.logger.info(f"backfill_file_kinds: Rechecking pre/play kinds of {len(song_ids)} songs{resumed} (rule version {FILE_KINDS_VERSION}).")

        changed = failed = 0
        step = constant.BACKFILL_BATCH_SONGS
        progress = _Progress(self.logger, "backfill_file_kinds", len(song_ids))
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
                    if not failed:
                        last_id = song_ids[min(i + step, len(song_ids)) - 1]
                        self._set_setting(cur, FILE_KINDS_PROGRESS_SETTING, f"{FILE_KINDS_VERSION}:{last_id}")
                    con.commit()
                    changed += len(batch_changed)
                except Exception:
                    con.rollback()
                    failed += 1
                    self.logger.exception(f"backfill_file_kinds: Skipped songs from song[{song_ids[i]}]")
            progress.update(min(i + step, len(song_ids)))

        if failed:
            self.logger.warning(f"backfill_file_kinds: {failed} songs failed. Retrying on next start.")
            with self._write_lock, connect() as con, con.cursor() as cur:
                self._set_setting(cur, FILE_KINDS_PROGRESS_SETTING, None)
                con.commit()
        else:
            with self._write_lock, connect() as con, con.cursor() as cur:
                self._set_setting(cur, FILE_KINDS_PROGRESS_SETTING, None)
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
            progress = _Progress(self.logger, "migrate_chart_chunk_names", len(chunk_ids))
            try:
                for n, chunk_no in enumerate(chunk_ids, 1):
                    progress.update(n - 1)
                    cur.execute("SELECT id FROM chart_chunk WHERE id = %s FOR UPDATE", (chunk_no,))
                    old = self._chart_chunk_data(cur, chunk_no)
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
                    self._write_chart_chunk(cur, chunk_no, buf.getvalue())
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
            사용 중지한 곡과 차트가 겹치면 넣지 않고 "skipped": "unused"와 그 곡 번호를 돌려줍니다.
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

    def _registered_charts(self, bms_files: list, cur) -> dict[str, int]:
        """폴더의 차트 중 이미 곡에 붙어 있는 것. {sha256: 곡 번호}"""
        found = {}
        for _, size, sha256 in bms_files:
            cur.execute("SELECT song_id FROM chart WHERE id = %s AND size = %s", (sha256, size))
            row = cur.fetchone()
            if row is not None and row[0] is not None:
                found[sha256] = row[0]
        return found

    def _deleted_song_for_charts(self, bms_files: list, cur) -> int | None:
        """폴더의 차트 중 사용 중지한 곡의 차트(song_id가 비어 남은 차트 행)가 있으면 그 곡 번호, 없으면 None.
        사용 중지 기록(deleted_song)에 없는 차트는 보지 않습니다."""
        for _, size, sha256 in bms_files:
            cur.execute("SELECT 1 FROM chart WHERE id = %s AND size = %s AND song_id IS NULL", (sha256, size))
            if cur.fetchone() is None:
                continue
            cur.execute(
                "SELECT song_id FROM deleted_song WHERE chart_list LIKE %s ORDER BY song_id DESC LIMIT 1",
                (f'%"{sha256}"%',),
            )
            row = cur.fetchone()
            if row is not None:
                return row[0]
        return None

    def _insert_new_song(self, root: pathlib.Path, cur) -> tuple[int | None, int]:
        """곡 zip을 넣고 (song_id, zip 크기)를 반환합니다. 패킷 한도를 넘으면 (None, 0)."""
        data = self.create_zip(root)
        cur.execute(
            "INSERT INTO song (id, size, sha256, folder, files) VALUES (%s, %s, %s, %s, %s)",
            (
                self._next_song_id(cur),
                len(data),
                hashlib.sha256(data).hexdigest(),
                root.resolve().name,
                json.dumps(zip_entries(data)),
            ),
        )
        song_id = cur.lastrowid
        self._put_song_data(cur, song_id, data)
        self.logger.info(f"insert_song: Inserted new song[{song_id}, {len(data)} bytes]")
        return song_id, len(data)

    def _next_song_id(self, cur) -> int | None:
        """새 곡 번호. 보통은 None(AUTO_INCREMENT)이고, 지운 곡 번호가 가장 크면 그 뒤 번호를 정해 결번을 다시 쓰지 않게 합니다
        (AUTO_INCREMENT가 되돌아가는 경우: 오래된 MySQL 재시작, 백업 복원 등)."""
        cur.execute("SELECT MAX(song_id) FROM deleted_song")
        deleted = cur.fetchone()[0]
        if deleted is None:
            return None
        cur.execute("SELECT MAX(id) FROM song")
        current = cur.fetchone()[0]
        return deleted + 1 if current is None or current < deleted else None

    def _put_song_data(self, cur, song_id: int, data: bytes) -> None:
        """곡 zip을 저장소 파일로 쓰고 song.path를 그 파일로 바꿉니다. DB에 남은 옛 BLOB(song.data, song_part)은 지웁니다.
        song 행의 size·sha256은 호출한 쪽이 맞춥니다."""
        path = store.write(store.song_path(song_id, hashlib.sha256(data).hexdigest()), data)
        cur.execute("UPDATE song SET path = %s, data = NULL WHERE id = %s", (path, song_id))
        cur.execute("DELETE FROM song_part WHERE song_id = %s", (song_id,))

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
        cur.execute(
            "UPDATE song SET size = %s, sha256 = %s, files = %s WHERE id = %s",
            (len(data), hashlib.sha256(data).hexdigest(), json.dumps(zip_entries(data)), song_id),
        )
        self._put_song_data(cur, song_id, data)
        added = sorted(arcname for arcname, _ in new_files)
        self.logger.info(f"insert_song: Merged {len(added)} files into song[{song_id}, {len(data)} bytes]")
        return len(data), added, conflicts

    def _insert_or_update_charts(self, root: pathlib.Path, bms_files: list, song_id: int, cur) -> tuple[list, int]:
        """(차트 청크에 넣을 새 차트, 지운 곡에서 남은 차트를 이 곡에 다시 붙인 수)를 반환합니다."""
        new_charts = []
        reattached = 0
        for (chart_file_path, size, sha256) in bms_files:
            filename = chart_file_path.relative_to(root).as_posix()
            content = chart_file_path.read_bytes()
            cur.execute(
                "INSERT IGNORE INTO chart (id, song_id, size, filename, md5, title_key, artist_key, bpm_key, genre_key)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (str(sha256), song_id, size, filename, hashlib.md5(content).hexdigest(), *chart_meta_keys(content)),
            )
            if cur.rowcount == 1:
                new_charts.append((chart_arcname(sha256, chart_file_path), content))
            elif song_id is not None:
                # 지운 곡의 차트는 차트 청크에 그대로 있으므로 청크에 다시 넣지 않고 곡만 붙입니다.
                cur.execute(
                    "UPDATE chart SET song_id = %s, filename = %s WHERE id = %s AND size = %s AND song_id IS NULL",
                    (song_id, filename, str(sha256), size),
                )
                if cur.rowcount == 1:
                    reattached += 1
                    continue
                cur.execute(
                    "UPDATE chart SET filename = %s WHERE id = %s AND size = %s AND filename = ''",
                    (filename, str(sha256), size),
                )
        return new_charts, reattached

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

    def get_chunk_sizes(self) -> dict[str, dict[int, dict]]:
        """차트·사전 청크별 SHA-256과 바이트 크기. 클라이언트가 동기화 전에 받을 용량을 셉니다."""
        out: dict[str, dict[int, dict]] = {}
        with connect() as con, con.cursor() as cur:
            for key, table in (("chart", "chart_chunk"), ("asset", "pre_chunk")):
                cur.execute(f"SELECT id, sha256, size FROM {table} ORDER BY id")
                out[key] = {row[0]: {"sha256": row[1], "size": row[2]} for row in cur.fetchall()}
        return out

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

    def _lock(self, timeout: float | None) -> None:
        if not self._write_lock.acquire(timeout=-1 if timeout is None else timeout):
            raise TimeoutError("song import is holding the write lock")

    @staticmethod
    def unused_dir(song_id: int) -> pathlib.Path:
        """사용 중지한 곡을 옮겨 두는 폴더. song.zip과 charts/{sha256}{확장자}가 들어갑니다."""
        return constant.UNUSED_DIR / str(song_id)

    def _song_charts(self, cur, song_id: int, data: bytes, charts: list[list]) -> dict[str, bytes]:
        """곡의 차트 내용을 {차트 청크 항목 이름: 내용}으로 모읍니다.
        곡 zip의 같은 경로에서 먼저 찾고, 곡 zip에 없거나 내용이 다른 차트는 차트 청크에서 꺼냅니다."""
        found: dict[str, bytes] = {}
        wanted: dict[str, str] = {}
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = set(zf.namelist())
            for chart_id, size, filename in charts:
                arcname = chart_arcname(chart_id, pathlib.PurePosixPath(filename or f"{chart_id}.bms"))
                content = zf.read(filename) if filename in names else None
                if content is not None and len(content) == size and hashlib.sha256(content).hexdigest() == chart_id:
                    found[arcname] = content
                else:
                    wanted[chart_id] = arcname
        if wanted:
            # 차트 청크 항목 이름은 sha256 + 확장자. 최근 청크부터 찾습니다.
            cur.execute("SELECT id FROM chart_chunk ORDER BY id DESC")
            for (chunk_no,) in cur.fetchall():
                with zipfile.ZipFile(io.BytesIO(self._chart_chunk_data(cur, chunk_no))) as zf:
                    for name in zf.namelist():
                        chart_id = name.split(".", 1)[0]
                        if chart_id in wanted:
                            found[wanted.pop(chart_id)] = zf.read(name)
                if not wanted:
                    break
        if wanted:
            self.logger.warning(f"delete_song: Song[{song_id}] charts not found anywhere: {sorted(wanted)}")
        return found

    def delete_song(
        self, song_id: int, reason: str = "", deleted_by: str | None = None, timeout: float | None = None
    ) -> bool:
        """곡을 사용 중지합니다. 없는 곡이면 False.

        곡 zip과 차트(곡 zip에 없는 차트는 차트 청크에서 꺼냄)를 UNUSED_DIR/{song_id}/로 옮기고 DB의 곡 데이터를 지운 뒤,
        그 곡이 든 매니페스트·사전 청크를 다시 만듭니다. deleted_song에 사유와 차트 목록을 남기고,
        번호는 결번으로 두어 restore_song으로 같은 번호로 되돌릴 수 있습니다.
        차트 청크에서는 뺄 수 없으므로 차트 행은 song_id만 비워 남깁니다.
        임포트 배치가 쓰기 잠금을 잡고 있으면 timeout(초)까지 기다리고, 넘으면 TimeoutError.
        """
        self._lock(timeout)
        target = self.unused_dir(song_id)
        written = False
        try:
            with connect() as con, con.cursor() as cur:
                try:
                    cur.execute("SELECT folder, size, sha256 FROM song WHERE id = %s FOR UPDATE", (song_id,))
                    row = cur.fetchone()
                    if row is None:
                        return False
                    folder, size, sha256 = row
                    data = self._get_song_data_with_cur(cur, song_id)
                    if data is None or hashlib.sha256(data).hexdigest() != sha256:
                        raise ValueError(f"song {song_id}: zip data is missing or does not match its sha256")
                    cur.execute("SELECT id, size, filename FROM chart WHERE song_id = %s ORDER BY id", (song_id,))
                    charts = [list(r) for r in cur.fetchall()]
                    chart_data = self._song_charts(cur, song_id, data, charts)

                    # DB에서 지우기 전에 파일을 끝까지 써 둡니다. 다 쓴 뒤 폴더 이름을 바꿔 반쯤 쓴 폴더가 남지 않게 합니다.
                    tmp = constant.UNUSED_DIR / f".{song_id}.tmp"
                    shutil.rmtree(tmp, ignore_errors=True)
                    (tmp / "charts").mkdir(parents=True)
                    for name, content in [("song.zip", data), *((f"charts/{n}", c) for n, c in chart_data.items())]:
                        with open(tmp / name, "wb") as f:
                            f.write(content)
                            f.flush()
                            os.fsync(f.fileno())
                    shutil.rmtree(target, ignore_errors=True)
                    os.replace(tmp, target)
                    written = True

                    cur.execute(
                        """
                        INSERT INTO deleted_song
                            (song_id, folder, size, sha256, charts, chart_list, reason, deleted_by, deleted_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (song_id, folder, size, sha256, len(charts), json.dumps(charts, ensure_ascii=False), reason,
                         deleted_by, int(time.time())),
                    )
                    cur.execute("UPDATE chart SET song_id = NULL WHERE song_id = %s", (song_id,))
                    cur.execute("DELETE FROM download_grant WHERE song_id = %s", (song_id,))
                    cur.execute("DELETE FROM song_part WHERE song_id = %s", (song_id,))
                    cur.execute("DELETE FROM song WHERE id = %s", (song_id,))
                    self._rebuild_manifest_chunk(cur, song_id // constant.SONGS_PER_MANIFEST_CHUNK)
                    self._rebuild_pre_chunk(cur, song_id // constant.SONGS_PER_PRE_CHUNK)
                    con.commit()
                except Exception:
                    con.rollback()
                    if written:
                        shutil.rmtree(target, ignore_errors=True)
                    raise
        finally:
            self._write_lock.release()
        self.logger.info(f"delete_song: Moved song[{song_id}] to {target}")
        return True

    def restore_song(self, song_id: int, timeout: float | None = None) -> None:
        """사용 중지한 곡을 같은 번호로 되돌립니다. 차트 행이 없어진 차트는 옮겨 둔 차트 파일로 차트 청크에 다시 넣습니다.

        사용 중지한 곡이 아니면 LookupError, 옮겨 둔 파일이 없거나 내용이 다르면 FileNotFoundError·ValueError,
        그 사이 같은 차트가 다른 곡으로 임포트됐으면 ChartInUseError. 잠금을 못 잡으면 TimeoutError.
        """
        self._lock(timeout)
        source = self.unused_dir(song_id)
        try:
            with connect() as con, con.cursor() as cur:
                try:
                    cur.execute(
                        "SELECT folder, size, sha256, chart_list FROM deleted_song WHERE song_id = %s FOR UPDATE",
                        (song_id,),
                    )
                    row = cur.fetchone()
                    if row is None:
                        raise LookupError(f"song {song_id} is not unused")
                    folder, size, sha256, chart_list = row
                    data = (source / "song.zip").read_bytes()
                    if len(data) != size or hashlib.sha256(data).hexdigest() != sha256:
                        raise ValueError(f"{source}/song.zip does not match song {song_id}")
                    charts = json.loads(chart_list)
                    taken = set()
                    readd = []
                    for chart_id, chart_size, filename in charts:
                        cur.execute("SELECT song_id FROM chart WHERE id = %s AND size = %s", (chart_id, chart_size))
                        found = cur.fetchone()
                        if found is None:
                            arcname = chart_arcname(chart_id, pathlib.PurePosixPath(filename or f"{chart_id}.bms"))
                            content = (source / "charts" / arcname).read_bytes()
                            if hashlib.sha256(content).hexdigest() != chart_id:
                                raise ValueError(f"{source}/charts/{arcname} does not match its sha256")
                            readd.append((chart_id, chart_size, filename, arcname, content))
                        elif found[0] is not None:
                            taken.add(found[0])
                    if taken:
                        raise ChartInUseError(sorted(taken))

                    cur.execute(
                        "INSERT INTO song (id, size, sha256, folder, files) VALUES (%s, %s, %s, %s, %s)",
                        (song_id, size, sha256, folder, json.dumps(zip_entries(data))),
                    )
                    self._put_song_data(cur, song_id, data)
                    for chart_id, chart_size, _ in charts:
                        cur.execute(
                            "UPDATE chart SET song_id = %s WHERE id = %s AND size = %s", (song_id, chart_id, chart_size)
                        )
                    for chart_id, chart_size, filename, _, content in readd:
                        cur.execute(
                            "INSERT INTO chart (id, song_id, size, filename, md5, title_key, artist_key, bpm_key, genre_key)"
                            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                            (chart_id, song_id, chart_size, filename, hashlib.md5(content).hexdigest(), *chart_meta_keys(content)),
                        )
                    if readd:
                        self._append_charts_to_chunk(cur, [(arcname, content) for *_, arcname, content in readd])
                    cur.execute("DELETE FROM deleted_song WHERE song_id = %s", (song_id,))
                    self._rebuild_manifest_chunk(cur, song_id // constant.SONGS_PER_MANIFEST_CHUNK)
                    self._rebuild_pre_chunk(cur, song_id // constant.SONGS_PER_PRE_CHUNK)
                    con.commit()
                except Exception:
                    con.rollback()
                    raise
        finally:
            self._write_lock.release()
        shutil.rmtree(source, ignore_errors=True)
        self.logger.info(f"restore_song: Restored song[{song_id}]")
        preview.kick()

    def deleted_reason(self, song_id: int) -> str | None:
        """지운 곡이면 삭제 사유(없으면 빈 문자열), 아니면 None."""
        with connect() as con, con.cursor() as cur:
            cur.execute("SELECT reason FROM deleted_song WHERE song_id = %s", (song_id,))
            row = cur.fetchone()
        return None if row is None else row[0]

    def store_gc(self) -> int:
        """아무 행도 가리키지 않는 저장소 파일(지운 곡, 바뀌기 전 판, 커밋하지 못한 파일·임시 파일)을 지웁니다. 지운 수를 반환합니다.
        쓰기 잠금을 잡고 보므로 이 프로세스에서 커밋 전인 파일은 없고(잠금 밖에서 쓴 파일은 store.hold),
        최근 STORE_GC_GRACE_SECONDS 안에 쓴 파일은 건드리지 않습니다. 받는 중인 다운로드는 열어 둔 파일로 끝까지 받습니다."""
        candidates = store.files(store.cutoff())
        if not candidates:
            return 0
        with self._write_lock, connect() as con, con.cursor() as cur:
            used = set()
            for table in STORE_TABLES:
                cur.execute(f"SELECT path FROM {table} WHERE path IS NOT NULL")
                used.update(row[0] for row in cur.fetchall())
            held = store.held()
            removed = store.remove(rel for rel in candidates if rel not in used and rel not in held)
        if removed:
            self.logger.info(f"store_gc: Removed {removed} unused files.")
        return removed

    def _blob_data(self, cur, table: str, row_id: int) -> bytes | None:
        """아직 DB BLOB에 있는 곡 zip·청크의 내용."""
        if table == "song":
            return self._get_song_data_with_cur(cur, row_id)
        if table == "chart_chunk":
            return self._chart_chunk_data(cur, row_id)
        cur.execute("SELECT data FROM pre_chunk_part WHERE chunk_id = %s ORDER BY seq", (row_id,))
        return b"".join(row[0] for row in cur.fetchall())

    def _move_to_store(self, table: str, row_id: int) -> bool:
        """DB BLOB에 있는 행 하나를 저장소 파일로 옮깁니다. 옮겼으면 True, 그 사이 바뀌었거나 없어졌으면 False.
        읽기와 파일 쓰기는 쓰기 잠금 밖에서 하고, 행을 파일로 바꾸고 BLOB을 지우는 것만 잠금 안에서 커밋합니다."""
        with connect() as con, con.cursor() as cur:
            cur.execute(f"SELECT size, sha256 FROM {table} WHERE id = %s AND path IS NULL", (row_id,))
            row = cur.fetchone()
            if row is None:
                return False
            size, sha256 = row
            data = self._blob_data(cur, table, row_id)
        if data is None:
            raise ValueError(f"{table} {row_id}: data not found")
        if len(data) != size or hashlib.sha256(data).hexdigest() != sha256:
            # 옮기기 전부터 깨진 데이터입니다. 받는 쪽이 지금과 같은 오류를 보도록 그대로 옮깁니다.
            self.logger.warning(f"migrate_to_store: {table} {row_id} does not match its size/sha256. Moved as is.")
        rel = {"song": store.song_path, "chart_chunk": store.chart_chunk_path, "pre_chunk": store.pre_chunk_path}[table](
            row_id, sha256
        )
        with store.hold(rel):
            store.write(rel, data)
            with self._write_lock, connect() as con, con.cursor() as cur:
                try:
                    cur.execute(f"SELECT sha256 FROM {table} WHERE id = %s AND path IS NULL FOR UPDATE", (row_id,))
                    row = cur.fetchone()
                    if row is None or row[0] != sha256:
                        con.rollback()
                        return False
                    cur.execute(f"UPDATE {table} SET path = %s WHERE id = %s", (rel, row_id))
                    if table == "song":
                        cur.execute("UPDATE song SET data = NULL WHERE id = %s", (row_id,))
                        cur.execute("DELETE FROM song_part WHERE song_id = %s", (row_id,))
                    elif table == "chart_chunk":
                        cur.execute("UPDATE chart_chunk SET data = %s WHERE id = %s", (b"", row_id))
                    else:
                        cur.execute("DELETE FROM pre_chunk_part WHERE chunk_id = %s", (row_id,))
                    con.commit()
                except Exception:
                    con.rollback()
                    raise
        return True

    def _wait_for_store_room(self, size: int, stop: threading.Event) -> bool:
        """임포트가 쓰기 잠금을 잡고 있지 않고 저장소 디스크에 size + STORE_MIN_FREE_BYTES가 남을 때까지 기다립니다.
        stop이 켜지면 False."""
        warned = False
        while not stop.is_set():
            if self._write_lock.locked():
                stop.wait(5)
                continue
            free = store.free_bytes()
            if free >= size + constant.STORE_MIN_FREE_BYTES:
                return True
            if not warned:
                self.logger.warning(
                    f"migrate_to_store: Paused. {free} bytes free on the store disk; needs {size} + "
                    f"EBMS_STORE_MIN_FREE({constant.STORE_MIN_FREE_BYTES}). Checking again every {constant.STORE_MIGRATE_WAIT}s."
                )
                warned = True
            stop.wait(constant.STORE_MIGRATE_WAIT)
        return False

    def migrate_to_store(self, stop: threading.Event | None = None) -> int:
        """DB BLOB에 남은 사전 청크·차트 청크·곡 zip을 저장소 파일로 하나씩 옮깁니다(#152). 옮긴 수를 반환합니다.

        서버를 켠 채로 천천히 하도록 하나마다 일한 시간 x STORE_MIGRATE_REST_RATIO 만큼 쉬고, 임포트가 쓰기 잠금을
        잡고 있으면 기다립니다. 작은 테이블부터 옮기고 테이블 하나를 다 옮기면 그 BLOB을 비워 디스크 공간을 돌려받습니다
        (_reclaim_blob_space). 하나씩 커밋하므로 도중에 꺼져도 다음 시작 때 남은 것부터 이어서 합니다."""
        stop = stop or threading.Event()
        moved = 0
        for table in STORE_TABLES:
            with connect() as con, con.cursor() as cur:
                cur.execute(f"SELECT id, size FROM {table} WHERE path IS NULL ORDER BY id")
                pending = cur.fetchall()
            if pending:
                total = sum(size for _, size in pending)
                self.logger.info(f"migrate_to_store: Moving {len(pending)} {table} rows ({total} bytes) to the store.")
            progress = _Progress(self.logger, f"migrate_to_store({table})", len(pending))
            failed = 0
            for n, (row_id, size) in enumerate(pending, 1):
                if not self._wait_for_store_room(size, stop):
                    return moved
                started = time.monotonic()
                try:
                    moved += self._move_to_store(table, row_id)
                except Exception:
                    failed += 1
                    self.logger.exception(f"migrate_to_store: Skipped {table} {row_id}")
                progress.update(n)
                stop.wait(max(constant.STORE_MIGRATE_REST_MIN, (time.monotonic() - started) * constant.STORE_MIGRATE_REST_RATIO))
            if failed:
                self.logger.warning(f"migrate_to_store: {failed} {table} rows were not moved. Retrying on next start.")
                continue
            try:
                self._reclaim_blob_space(table)
            except Exception:
                self.logger.exception(f"migrate_to_store: Failed to reclaim space of {table}")
        return moved

    def _reclaim_blob_space(self, table: str) -> None:
        """table의 모든 행이 저장소 파일로 옮겨졌으면 BLOB이 쓰던 InnoDB 공간을 디스크에 돌려줍니다(테이블마다 한 번).
        조각 테이블(pre_chunk_part, song_part)은 TRUNCATE로 바로 비우고, 행이 남는 테이블(chart_chunk, song)은
        BLOB을 비운 뒤라 남은 작은 행만 복사하는 OPTIMIZE로 줄입니다."""
        with self._write_lock, connect() as con, con.cursor() as cur:
            done = set(filter(None, (self._setting(cur, STORE_RECLAIMED_SETTING) or "").split(",")))
            if table in done:
                return
            cur.execute(f"SELECT COUNT(*) FROM {table} WHERE path IS NULL")
            if cur.fetchone()[0]:
                return
            con.commit()
            self.logger.info(f"migrate_to_store: Reclaiming disk space of {table} BLOBs.")
            if table == "pre_chunk":
                cur.execute("TRUNCATE TABLE pre_chunk_part")
            elif table == "chart_chunk":
                cur.execute("OPTIMIZE TABLE chart_chunk")
                cur.fetchall()
            else:
                cur.execute("TRUNCATE TABLE song_part")
                cur.execute("OPTIMIZE TABLE song")
                cur.fetchall()
            self._set_setting(cur, STORE_RECLAIMED_SETTING, ",".join(sorted(done | {table})))
            con.commit()
        self.logger.info(f"migrate_to_store: Reclaimed disk space of {table} BLOBs.")

    def open_blob(self, table: str, row_id: int) -> BlobReader | None:
        """song / chart_chunk / pre_chunk 테이블의 BLOB 메타데이터를 단기 연결로 조회해 BlobReader를 반환합니다.

        전송 중 DB 연결과 트랜잭션을 잡지 않도록 단기 연결을 사용합니다.

        Returns:
            BlobReader. 존재하지 않는 id의 경우 None을 반환합니다.
        """
        if table not in ("song", "chart_chunk", "pre_chunk"):
            raise ValueError(table)
        # 파일을 여는 사이에 새 판으로 바뀌어 옛 파일이 지워졌으면 한 번 더 조회합니다.
        for attempt in range(2):
            with connect() as con, con.cursor() as cur:
                # LENGTH(data)는 InnoDB가 BLOB 전체를 읽어야 하므로 저장해 둔 size를 씁니다.
                cur.execute(f"SELECT size, sha256, path FROM {table} WHERE id = %s", (row_id,))
                row = cur.fetchone()
            if row is None:
                return None
            try:
                return BlobReader(table, row_id, row[0], row[1], row[2])
            except FileNotFoundError:
                if attempt:
                    raise
        return None


class SongBatch:
    """곡 여러 개를 한 트랜잭션으로 넣고, 청크(차트·매니페스트·사전)는 커밋 직전에 배치마다 한 번씩만 갱신합니다.

    곡마다 청크를 읽고 다시 쓰면 대량 임포트에서 같은 청크를 수천 번 읽고 쓰게 되므로 묶어서 처리합니다.
    곡과 청크 갱신이 같은 트랜잭션이라 도중에 꺼지면 배치 전체가 롤백되고, 다시 임포트하면 처음부터 들어갑니다.
    곡 하나가 실패하면 SAVEPOINT로 그 곡만 되돌리고 배치는 이어갑니다.
    새 차트가 BYTE_PER_CHUNK, 곡 데이터가 IMPORT_BATCH_BYTES, 곡 수가 IMPORT_BATCH_SONGS에 이르면 커밋합니다.
    배치가 열려 있는 동안(커밋 전까지)만 쓰기 잠금을 잡습니다. max_bytes·max_songs를 바꾸면 다음 add부터 그 한도로 커밋합니다.

    with db.batch() as batch:
        info = batch.add(song_dir)          # 등록 정보 또는 None. 실패하면 예외(그 곡만 되돌림)
    # with를 빠져나갈 때 남은 곡을 커밋합니다. 커밋 전에 예외가 나면 배치 전체를 롤백합니다.
    """

    def __init__(self, db: "Database"):
        self.db = db
        self.logger = db.logger
        self._con = None
        self._cur = None
        self.max_bytes = constant.IMPORT_BATCH_BYTES
        self.max_songs = constant.IMPORT_BATCH_SONGS
        self._reset()

    @property
    def open(self) -> bool:
        """커밋하지 않은 곡이 있어 쓰기 잠금을 잡고 있는지."""
        return self._con is not None

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

    def add(self, song_path: os.PathLike, remove: bool = False, target: str | int | None = None) -> dict | None:
        """곡 하나를 배치에 넣습니다. 등록 정보를 반환하고, 넣지 못하면 None.
        반환한 dict는 배치 커밋이 실패하면 "error" 키가 채워집니다.

        target은 넣을 곳입니다. None이면 겹치는 차트로 정하고(없으면 새 곡, 하나면 그 곡에 더함, 여럿이면 AmbiguousSongError),
        "new"면 겹치는 차트가 있어도 새 곡으로, 곡 번호면 그 곡에 더합니다. 이미 다른 곡에 있는 차트는 어느 경우든 원래 곡에 둡니다.
        새 곡으로 지정했는데 차트가 모두 이미 등록돼 있거나, 지정한 곡이 없으면 ValueError."""
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
            deleted = self.db._deleted_song_for_charts(bms_files, cur)
            if deleted is not None:
                # 사용 중지한 곡은 다시 들이지 않습니다(되살리려면 복원). 원본은 처리한 것으로 보고 지웁니다.
                cur.execute("RELEASE SAVEPOINT song")
                self.logger.warning(f"insert_song: Skipped unused song[{deleted}][{root}]")
                if remove:
                    self._remove.append(root)
                return {"song_id": deleted, "new_song": False, "skipped": "unused", "charts": len(bms_files), "new_charts": 0}
            if target == "new":
                registered = self.db._registered_charts(bms_files, cur)
                if len(registered) == len(bms_files):
                    raise ValueError("차트가 모두 이미 다른 곡에 등록돼 있어 새 곡을 만들지 않았습니다.")
                existing = []
            elif target is not None:
                cur.execute("SELECT 1 FROM song WHERE id = %s", (int(target),))
                if cur.fetchone() is None:
                    raise ValueError(f"곡 #{target}이 없습니다(사용 중지했거나 번호가 틀림).")
                existing = [int(target)]
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
            new_charts, reattached = self.db._insert_or_update_charts(root, bms_files, song_id, cur)
            cur.execute("RELEASE SAVEPOINT song")
        except Exception:
            cur.execute("ROLLBACK TO SAVEPOINT song")
            raise

        self._charts.extend(new_charts)
        self._chart_bytes += sum(len(content) for _, content in new_charts)
        self._song_bytes += song_bytes
        self._songs += 1
        if new_song or new_charts or reattached or added:
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
            or self._song_bytes >= self.max_bytes
            or self._songs >= self.max_songs
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
        new_previews = bool(self._pre_chunks)
        self._close()
        for path in remove:
            shutil.rmtree(path, ignore_errors=True)
        if new_previews:
            preview.kick()
