"""공지. 관리자가 `/admin/notices`에서 쓰고, 클라이언트는 시작할 때 `GET /api/notices`로 받아 띄웁니다.

공지 조회는 로그인 없이 쓸 수 있습니다(로그인 전이나 세션이 만료된 클라이언트에도 알려야 하므로).
"""
import time
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, model_validator

from . import auth, db
from .accounts import User
from .admin import _forbidden, web_admin
from .templating import templates

router = APIRouter()

LEVELS = ("info", "warning")
COLUMNS = ("id", "title", "body", "level", "published", "starts_at", "ends_at", "created_at", "updated_at")


def _row(row) -> dict:
    notice = dict(zip(COLUMNS, row))
    notice["published"] = bool(notice["published"])
    return notice


def list_notices() -> list[dict]:
    """모든 공지(관리자용). 최근에 만든 순."""
    with db.connect() as con, con.cursor() as cur:
        cur.execute(f"SELECT {', '.join(COLUMNS)} FROM notice ORDER BY id DESC")
        return [_row(row) for row in cur.fetchall()]


def active_notices(now: int | None = None) -> list[dict]:
    """지금 보여줄 공지. 게시 중이고 시작·종료 시각 안에 있는 것만, 최근에 만든 순."""
    now = int(time.time()) if now is None else now
    with db.connect() as con, con.cursor() as cur:
        cur.execute(
            f"""
            SELECT {', '.join(COLUMNS)} FROM notice
            WHERE published = 1 AND (starts_at IS NULL OR starts_at <= %s) AND (ends_at IS NULL OR ends_at > %s)
            ORDER BY id DESC
            """,
            (now, now),
        )
        rows = [_row(row) for row in cur.fetchall()]
    return [{k: n[k] for k in ("id", "title", "body", "level", "starts_at", "ends_at", "updated_at")} for n in rows]


def get_notice(notice_id: int) -> dict:
    with db.connect() as con, con.cursor() as cur:
        cur.execute(f"SELECT {', '.join(COLUMNS)} FROM notice WHERE id = %s", (notice_id,))
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="notice not found")
    return _row(row)


class NoticeBody(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    body: str = Field(default="", max_length=20000)
    level: Literal[LEVELS] = "info"
    published: bool = True
    # unix 초. None이면 제한 없음
    starts_at: int | None = Field(default=None, ge=0)
    ends_at: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _check_range(self):
        self.title = self.title.strip()
        if not self.title:
            raise ValueError("title is empty")
        if self.starts_at is not None and self.ends_at is not None and self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be after starts_at")
        return self


# ---- 공개 API ----

@router.get("/api/notices")
def get_notices():
    """지금 게시 중인 공지. 로그인 없이 씁니다."""
    return active_notices()


# ---- 관리자 ----

@router.get("/admin/notices", response_class=HTMLResponse)
def notices_page(
    request: Request,
    session: Annotated[tuple[User, int] | None, Depends(auth.optional_session)],
):
    if session is None:
        return auth._login_redirect(request)
    if not session[0].is_admin:
        return _forbidden(request)
    return templates.TemplateResponse(
        request=request,
        name="pages/admin_notices.html",
        context={"user": session[0], "notices": list_notices(), "now": int(time.time())},
    )


@router.get("/api/admin/notices")
def admin_list(admin: Annotated[User, Depends(web_admin)]):
    return list_notices()


@router.post("/api/admin/notices")
def admin_create(body: NoticeBody, admin: Annotated[User, Depends(web_admin)]):
    now = int(time.time())
    with db.connect() as con, con.cursor() as cur:
        cur.execute(
            """
            INSERT INTO notice (title, body, level, published, starts_at, ends_at, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (body.title, body.body, body.level, int(body.published), body.starts_at, body.ends_at, now, now),
        )
        notice_id = cur.lastrowid
        con.commit()
    return get_notice(notice_id)


@router.put("/api/admin/notices/{notice_id}")
def admin_update(notice_id: int, body: NoticeBody, admin: Annotated[User, Depends(web_admin)]):
    """공지 전체를 바꿉니다. updated_at이 바뀌므로 클라이언트는 이미 본 공지도 다시 띄웁니다."""
    get_notice(notice_id)
    with db.connect() as con, con.cursor() as cur:
        cur.execute(
            """
            UPDATE notice SET title = %s, body = %s, level = %s, published = %s, starts_at = %s, ends_at = %s,
                updated_at = %s
            WHERE id = %s
            """,
            (
                body.title, body.body, body.level, int(body.published), body.starts_at, body.ends_at,
                int(time.time()), notice_id,
            ),
        )
        con.commit()
    return get_notice(notice_id)


@router.delete("/api/admin/notices/{notice_id}")
def admin_delete(notice_id: int, admin: Annotated[User, Depends(web_admin)]):
    get_notice(notice_id)
    with db.connect() as con, con.cursor() as cur:
        cur.execute("DELETE FROM notice WHERE id = %s", (notice_id,))
        con.commit()
    return {"ok": True}
