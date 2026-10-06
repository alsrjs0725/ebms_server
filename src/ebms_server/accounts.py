"""계정·로그인 수단·세션 저장소입니다.

계정은 내부 UUID(user.id)로만 구분합니다. provider는 로그인 순간 UUID를 찾는 데만 쓰고,
세션과 권한은 모두 user.id에 붙습니다. 세션 토큰은 원문 대신 sha256만 저장합니다.
"""
import hashlib
import secrets
import time
import uuid
from dataclasses import dataclass

import pymysql

from . import constant, db

# 세션 종류별 유효기간(초). 쓸 때마다 이만큼 연장됩니다.
SESSION_SECONDS = {"web": constant.WEB_SESSION_SECONDS}
# last_used_at/expires_at 갱신 최소 간격. 요청마다 UPDATE하지 않기 위함입니다.
TOUCH_INTERVAL = 60


@dataclass
class User:
    id: str
    display_name: str
    email: str | None
    role: str
    status: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


@dataclass
class Identity:
    id: int
    provider: str
    provider_user_id: str
    email: str | None
    name: str
    linked_at: int


@dataclass
class Session:
    id: int
    kind: str
    device_name: str
    created_at: int
    last_used_at: int
    expires_at: int


@dataclass
class Profile:
    """provider가 알려준 사용자 정보."""
    provider: str
    provider_user_id: str
    email: str | None
    email_verified: bool
    name: str


_USER_COLUMNS = "id, display_name, email, role, status"


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _is_admin_email(profile: Profile) -> bool:
    return bool(profile.email and profile.email_verified and profile.email.lower() in constant.ADMIN_EMAILS)


def get_user(user_id: str) -> User | None:
    with db.connect() as con, con.cursor() as cur:
        cur.execute(f"SELECT {_USER_COLUMNS} FROM user WHERE id = %s", (user_id,))
        row = cur.fetchone()
    return User(*row) if row else None


def login(profile: Profile) -> User:
    """provider 계정으로 로그인합니다. 처음 보는 provider 계정이면 새 user를 만듭니다.

    이메일이 같아도 기존 user에 자동으로 합치지 않습니다.
    """
    for attempt in range(2):
        try:
            return _login(profile)
        except pymysql.err.IntegrityError:
            # 같은 provider 계정의 첫 로그인이 동시에 들어와 다른 쪽이 먼저 만든 경우. 다시 찾으면 있습니다.
            if attempt:
                raise
    raise AssertionError("unreachable")


def _login(profile: Profile) -> User:
    now = int(time.time())
    with db.connect() as con, con.cursor() as cur:
        try:
            cur.execute(
                "SELECT user_id FROM user_identity WHERE provider = %s AND provider_user_id = %s",
                (profile.provider, profile.provider_user_id),
            )
            row = cur.fetchone()
            if row:
                user_id = row[0]
                cur.execute(
                    "UPDATE user_identity SET email = %s, name = %s WHERE provider = %s AND provider_user_id = %s",
                    (profile.email, profile.name, profile.provider, profile.provider_user_id),
                )
            else:
                user_id = str(uuid.uuid4())
                cur.execute(
                    "INSERT INTO user (id, display_name, email, created_at) VALUES (%s, %s, %s, %s)",
                    (user_id, profile.name, profile.email if profile.email_verified else None, now),
                )
                _insert_identity(cur, user_id, profile, now)
            if _is_admin_email(profile):
                cur.execute("UPDATE user SET role = 'admin' WHERE id = %s", (user_id,))
            cur.execute("UPDATE user SET last_login_at = %s WHERE id = %s", (now, user_id))
            con.commit()
        except Exception:
            con.rollback()
            raise
    return get_user(user_id)


def _insert_identity(cur, user_id: str, profile: Profile, now: int) -> None:
    cur.execute(
        """
        INSERT INTO user_identity (user_id, provider, provider_user_id, email, name, linked_at)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (user_id, profile.provider, profile.provider_user_id, profile.email, profile.name, now),
    )


def link(user_id: str, profile: Profile) -> str:
    """provider 계정을 user에 연결합니다.

    반환값: "linked"(새로 연결), "already"(이미 이 user에 연결됨), "taken"(다른 user에 연결돼 있어 거부)
    """
    now = int(time.time())
    with db.connect() as con, con.cursor() as cur:
        cur.execute(
            "SELECT user_id FROM user_identity WHERE provider = %s AND provider_user_id = %s",
            (profile.provider, profile.provider_user_id),
        )
        row = cur.fetchone()
        if row:
            return "already" if row[0] == user_id else "taken"
        try:
            _insert_identity(cur, user_id, profile, now)
            if _is_admin_email(profile):
                cur.execute("UPDATE user SET role = 'admin' WHERE id = %s", (user_id,))
            con.commit()
        except pymysql.err.IntegrityError:
            con.rollback()
            return "taken"
    return "linked"


def list_identities(user_id: str) -> list[Identity]:
    with db.connect() as con, con.cursor() as cur:
        cur.execute(
            """
            SELECT id, provider, provider_user_id, email, name, linked_at
            FROM user_identity WHERE user_id = %s ORDER BY id
            """,
            (user_id,),
        )
        return [Identity(*row) for row in cur.fetchall()]


def unlink(user_id: str, identity_id: int) -> str:
    """provider 연결을 해제합니다. 반환값: "ok", "not_found", "last"(마지막 1개라 거부)"""
    with db.connect() as con, con.cursor() as cur:
        try:
            # 동시에 두 개를 해제해 0개가 되지 않도록 user의 연결을 잠근 뒤 셉니다.
            cur.execute("SELECT id FROM user_identity WHERE user_id = %s FOR UPDATE", (user_id,))
            ids = [row[0] for row in cur.fetchall()]
            if identity_id not in ids:
                result = "not_found"
            elif len(ids) <= 1:
                result = "last"
            else:
                cur.execute("DELETE FROM user_identity WHERE id = %s AND user_id = %s", (identity_id, user_id))
                result = "ok"
            con.commit()
        except Exception:
            con.rollback()
            raise
    return result


def create_session(user_id: str, kind: str, device_name: str) -> str:
    """세션을 만들고 토큰 원문을 반환합니다. 원문은 이때만 알 수 있습니다."""
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    with db.connect() as con, con.cursor() as cur:
        cur.execute(
            """
            INSERT INTO session (token_sha256, user_id, kind, device_name, created_at, last_used_at, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (token_hash(token), user_id, kind, device_name[:255], now, now, now + SESSION_SECONDS[kind]),
        )
        con.commit()
    return token


def authenticate(token: str, kind: str) -> tuple[User, int] | None:
    """유효한 세션이면 (user, session id)를 반환하고 만료 시각을 연장합니다. 정지된 계정은 None입니다."""
    now = int(time.time())
    with db.connect() as con, con.cursor() as cur:
        cur.execute(
            """
            SELECT s.id, s.last_used_at, u.id, u.display_name, u.email, u.role, u.status
            FROM session s JOIN user u ON u.id = s.user_id
            WHERE s.token_sha256 = %s AND s.kind = %s AND s.revoked_at IS NULL AND s.expires_at > %s
            """,
            (token_hash(token), kind, now),
        )
        row = cur.fetchone()
        if row is None:
            return None
        session_id, last_used_at, *user_row = row
        user = User(*user_row)
        if user.status != "active":
            return None
        if now - last_used_at >= TOUCH_INTERVAL:
            cur.execute(
                "UPDATE session SET last_used_at = %s, expires_at = %s WHERE id = %s",
                (now, now + SESSION_SECONDS[kind], session_id),
            )
            con.commit()
    return user, session_id


def list_sessions(user_id: str) -> list[Session]:
    """만료·폐기되지 않은 세션 목록. 최근에 쓴 순서입니다."""
    with db.connect() as con, con.cursor() as cur:
        cur.execute(
            """
            SELECT id, kind, device_name, created_at, last_used_at, expires_at
            FROM session WHERE user_id = %s AND revoked_at IS NULL AND expires_at > %s
            ORDER BY last_used_at DESC, id DESC
            """,
            (user_id, int(time.time())),
        )
        return [Session(*row) for row in cur.fetchall()]


def revoke_session(user_id: str, session_id: int) -> bool:
    """user의 세션을 폐기합니다. 없거나 이미 폐기됐으면 False."""
    with db.connect() as con, con.cursor() as cur:
        cur.execute(
            "UPDATE session SET revoked_at = %s WHERE id = %s AND user_id = %s AND revoked_at IS NULL",
            (int(time.time()), session_id, user_id),
        )
        changed = cur.rowcount > 0
        con.commit()
    return changed
