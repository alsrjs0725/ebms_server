"""계정·로그인 수단·세션 저장소입니다.

계정은 내부 UUID(user.id)로만 구분합니다. OAuth는 로그인 순간 UUID를 찾는 데만 쓰고,
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
SESSION_SECONDS = {"web": constant.WEB_SESSION_SECONDS, "client": constant.CLIENT_SESSION_SECONDS}
# last_used_at/expires_at 갱신 최소 간격. 요청마다 UPDATE하지 않기 위함입니다.
TOUCH_INTERVAL = 60


@dataclass
class User:
    id: str
    display_name: str
    role: str
    status: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


@dataclass
class Identity:
    id: int
    oauth: str
    oauth_user_id: str
    linked_at: int


@dataclass
class Session:
    id: int
    kind: str
    created_at: int
    last_used_at: int
    expires_at: int


@dataclass
class Profile:
    """OAuth가 알려준 계정 식별자. 이메일·계정 이름은 받지 않습니다(#102)."""
    oauth: str
    oauth_user_id: str


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _promote_admin(cur, user_id: str) -> None:
    """EBMS_ADMIN_USER_IDS에 있는 계정을 admin으로 올립니다.

    승격만 합니다. 목록에서 빼도 강등되지 않으므로 관리자를 내리려면 관리자 페이지에서 role을 바꿉니다.
    """
    if user_id in constant.ADMIN_USER_IDS:
        cur.execute("UPDATE user SET role = 'admin' WHERE id = %s AND role <> 'admin'", (user_id,))


def get_user(user_id: str) -> User | None:
    with db.connect() as con, con.cursor() as cur:
        cur.execute("SELECT id, display_name, role, status FROM user WHERE id = %s", (user_id,))
        row = cur.fetchone()
    return User(*row) if row else None


NICKNAME_MAX = 32


def clean_nickname(text: str) -> str | None:
    """앞뒤 공백을 지운 닉네임. 비었거나 너무 길거나 제어문자가 있으면 None."""
    text = text.strip()
    if not text or len(text) > NICKNAME_MAX or any(ord(c) < 0x20 or ord(c) == 0x7F for c in text):
        return None
    return text


def login(profile: Profile) -> User | None:
    """OAuth 계정으로 로그인합니다. 처음 보는 OAuth 계정이면 None(가입 필요, signup)입니다.

    다른 OAuth 계정을 기존 user에 자동으로 합치지 않습니다.
    """
    now = int(time.time())
    with db.connect() as con, con.cursor() as cur:
        cur.execute(
            "SELECT user_id FROM user_identity WHERE oauth = %s AND oauth_user_id = %s",
            (profile.oauth, profile.oauth_user_id),
        )
        row = cur.fetchone()
        if row is None:
            return None
        user_id = row[0]
        cur.execute("UPDATE user SET last_login_at = %s WHERE id = %s", (now, user_id))
        _promote_admin(cur, user_id)
        con.commit()
    return get_user(user_id)


def signup(profile: Profile, nickname: str) -> User:
    """사용자가 정한 닉네임으로 새 user를 만들고 OAuth 계정을 연결합니다.

    같은 OAuth 계정의 가입이 동시에 들어와 다른 쪽이 먼저 만들었으면 그 user로 로그인합니다.
    """
    now = int(time.time())
    user_id = str(uuid.uuid4())
    with db.connect() as con, con.cursor() as cur:
        try:
            cur.execute(
                "INSERT INTO user (id, display_name, created_at, last_login_at) VALUES (%s, %s, %s, %s)",
                (user_id, nickname, now, now),
            )
            _insert_identity(cur, user_id, profile, now)
            _promote_admin(cur, user_id)
            con.commit()
        except pymysql.err.IntegrityError:
            con.rollback()
            user = login(profile)
            if user is None:
                raise
            return user
    return get_user(user_id)


def set_nickname(user_id: str, nickname: str) -> None:
    with db.connect() as con, con.cursor() as cur:
        cur.execute("UPDATE user SET display_name = %s WHERE id = %s", (nickname, user_id))
        con.commit()


def _insert_identity(cur, user_id: str, profile: Profile, now: int) -> None:
    cur.execute(
        "INSERT INTO user_identity (user_id, oauth, oauth_user_id, linked_at) VALUES (%s, %s, %s, %s)",
        (user_id, profile.oauth, profile.oauth_user_id, now),
    )


def link(user_id: str, profile: Profile) -> str:
    """OAuth 계정을 user에 연결합니다.

    반환값: "linked"(새로 연결), "already"(이미 이 user에 연결됨), "taken"(다른 user에 연결돼 있어 거부)
    """
    now = int(time.time())
    with db.connect() as con, con.cursor() as cur:
        cur.execute(
            "SELECT user_id FROM user_identity WHERE oauth = %s AND oauth_user_id = %s",
            (profile.oauth, profile.oauth_user_id),
        )
        row = cur.fetchone()
        if row:
            return "already" if row[0] == user_id else "taken"
        try:
            _insert_identity(cur, user_id, profile, now)
            con.commit()
        except pymysql.err.IntegrityError:
            con.rollback()
            return "taken"
    return "linked"


def list_identities(user_id: str) -> list[Identity]:
    with db.connect() as con, con.cursor() as cur:
        cur.execute(
            """
            SELECT id, oauth, oauth_user_id, linked_at
            FROM user_identity WHERE user_id = %s ORDER BY id
            """,
            (user_id,),
        )
        return [Identity(*row) for row in cur.fetchall()]


def unlink(user_id: str, identity_id: int) -> str:
    """OAuth 연결을 해제합니다. 반환값: "ok", "not_found", "last"(마지막 1개라 거부)"""
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


def create_session(user_id: str, kind: str) -> str:
    """세션을 만들고 토큰 원문을 반환합니다. 원문은 이때만 알 수 있습니다.

    기기 이름(User-Agent, PC 호스트명)은 받지 않습니다(#102). 기기 목록은 종류와 시각으로 구분합니다.
    """
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    with db.connect() as con, con.cursor() as cur:
        cur.execute(
            """
            INSERT INTO session (token_sha256, user_id, kind, created_at, last_used_at, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (token_hash(token), user_id, kind, now, now, now + SESSION_SECONDS[kind]),
        )
        con.commit()
    return token


def authenticate(token: str, kind: str) -> tuple[User, int] | None:
    """유효한 세션이면 (user, session id)를 반환하고 만료 시각을 연장합니다. 정지된 계정은 None입니다."""
    now = int(time.time())
    with db.connect() as con, con.cursor() as cur:
        cur.execute(
            """
            SELECT s.id, s.last_used_at, u.id, u.display_name, u.role, u.status
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
        # env에 UUID를 넣고 재시작하면 다시 로그인하지 않아도 다음 요청부터 관리자가 됩니다.
        if user.id in constant.ADMIN_USER_IDS and user.role != "admin":
            _promote_admin(cur, user.id)
            con.commit()
            user.role = "admin"
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
            SELECT id, kind, created_at, last_used_at, expires_at
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


def create_auth_code(user_id: str, code_challenge: str, redirect_uri: str) -> str:
    """클라이언트 로그인용 1회용 코드를 만들고 원문을 반환합니다."""
    code = secrets.token_urlsafe(32)
    now = int(time.time())
    with db.connect() as con, con.cursor() as cur:
        # 쓰였거나 만료된 코드는 더 필요 없으므로 새로 만들 때 같이 지웁니다.
        cur.execute("DELETE FROM auth_code WHERE expires_at < %s", (now - 3600,))
        cur.execute(
            """
            INSERT INTO auth_code (code_sha256, user_id, code_challenge, redirect_uri, created_at, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (token_hash(code), user_id, code_challenge, redirect_uri, now, now + constant.AUTH_CODE_SECONDS),
        )
        con.commit()
    return code


def consume_auth_code(code: str) -> tuple[str, str] | None:
    """유효한 코드를 사용 처리하고 (user_id, code_challenge)를 반환합니다.

    코드는 PKCE 검증 결과와 상관없이 한 번만 쓸 수 있습니다. 없거나 만료·사용됐으면 None.
    """
    now = int(time.time())
    with db.connect() as con, con.cursor() as cur:
        # 동시에 같은 코드로 요청해도 UPDATE에 성공한 한 쪽만 통과합니다.
        cur.execute(
            "UPDATE auth_code SET used_at = %s WHERE code_sha256 = %s AND used_at IS NULL AND expires_at > %s",
            (now, token_hash(code), now),
        )
        if cur.rowcount != 1:
            con.commit()
            return None
        cur.execute(
            "SELECT user_id, code_challenge FROM auth_code WHERE code_sha256 = %s",
            (token_hash(code),),
        )
        row = cur.fetchone()
        con.commit()
    return tuple(row) if row else None

