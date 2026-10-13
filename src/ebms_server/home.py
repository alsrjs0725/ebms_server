"""메인화면(`/`)의 소개 글.

관리자가 `/admin/home`에서 소개 글, 클라이언트 다운로드 링크, 문의 연락처 등을 마크다운으로 적어 저장하면
메인화면 위쪽에 보입니다. 저장한 적이 없으면 `DEFAULT`를 보여주고, 비워서 저장하면 아무것도 보여주지 않습니다.
채우지 않은 칸(`[[...]]`)이 있는 줄은 메인화면에서 뺍니다.
본문은 처리방침과 같은 마크다운 일부(`policies.render`)만 지원합니다.
"""
import time
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from markupsafe import Markup
from pydantic import BaseModel, Field

from . import auth, db
from .accounts import User
from .admin import _forbidden, web_admin
from .policies import PLACEHOLDER, render
from .templating import templates

router = APIRouter()

NAME = "home"
DEFAULT = """EBMS는 BMS 곡을 가상 드라이브로 보여주고, 플레이할 때 필요한 파일만 받아오는 서비스입니다.

## 클라이언트 받기
- [GitHub 릴리스](https://github.com/alsrjs0725/ebms_client/releases) (Windows)

## 문의
- [[문의 연락처]]
"""


def saved() -> dict | None:
    """저장한 글 `{body, updated_at, updated_by}`. 없으면 None."""
    with db.connect() as con, con.cursor() as cur:
        cur.execute("SELECT body, updated_at, updated_by FROM site_text WHERE name = %s", (NAME,))
        row = cur.fetchone()
    return dict(zip(("body", "updated_at", "updated_by"), row)) if row else None


def body() -> str:
    row = saved()
    return row["body"] if row else DEFAULT


def content() -> Markup:
    """메인화면에 넣을 HTML. 빈 글이면 빈 문자열. 채우지 않은 칸(`[[...]]`)이 있는 줄은 빼고 보여줍니다."""
    return render("\n".join(line for line in body().splitlines() if not PLACEHOLDER.search(line)))


# ---- 관리자 ----

class HomeBody(BaseModel):
    # 비우면 메인화면에 소개 글을 보여주지 않습니다
    body: str = Field(max_length=200000)


@router.get("/admin/home", response_class=HTMLResponse)
def home_page(
    request: Request,
    session: Annotated[tuple[User, int] | None, Depends(auth.optional_session)],
):
    if session is None:
        return auth._login_redirect(request)
    if not session[0].is_admin:
        return _forbidden(request)
    row = saved()
    text = row["body"] if row else DEFAULT
    return templates.TemplateResponse(
        request=request,
        name="pages/admin_home.html",
        context={
            "user": session[0],
            "body": text,
            "saved": row,
            "placeholders": len(PLACEHOLDER.findall(text)),
        },
    )


@router.get("/api/admin/home")
def admin_get(admin: Annotated[User, Depends(web_admin)]):
    row = saved()
    return {"body": row["body"] if row else DEFAULT, "default": DEFAULT, "saved": row is not None}


@router.put("/api/admin/home")
def admin_save(body: HomeBody, admin: Annotated[User, Depends(web_admin)]):
    with db.connect() as con, con.cursor() as cur:
        cur.execute("DELETE FROM site_text WHERE name = %s", (NAME,))
        cur.execute(
            "INSERT INTO site_text (name, body, updated_at, updated_by) VALUES (%s, %s, %s, %s)",
            (NAME, body.body, int(time.time()), admin.id),
        )
        con.commit()
    return {"body": body.body, "saved": True}


@router.delete("/api/admin/home")
def admin_reset(admin: Annotated[User, Depends(web_admin)]):
    """저장한 글을 지우고 기본 글로 되돌립니다."""
    with db.connect() as con, con.cursor() as cur:
        cur.execute("DELETE FROM site_text WHERE name = %s", (NAME,))
        con.commit()
    return {"body": DEFAULT, "saved": False}
