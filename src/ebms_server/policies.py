"""개인정보 처리방침(`/privacy`)과 이용약관(`/terms`).

기본 내용은 `policy_templates/{kind}.md` 템플릿이고, 관리자가 `/admin/policies`에서 고쳐 저장하면 그 판을 보여줍니다.
저장할 때마다 새 판(시행일 포함)을 쌓으므로 변경 이력이 남습니다. 시행일이 오늘(KST) 이전인 판 중 가장 최근 것이
지금 판이고, 시행일이 아직 오지 않은 판은 "변경 예정"으로 함께 알립니다. 저장한 판이 없으면 템플릿을 보여줍니다.

본문은 마크다운의 일부(제목, 목록, 표, 굵게, 링크)만 지원하고, 모든 글자는 이스케이프한 뒤 태그를 붙입니다.
`[[...]]`는 운영자가 채워야 할 칸이며 관리자 페이지에서 남은 개수를 알려 줍니다.
"""
import datetime
import html
import re
import time
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from markupsafe import Markup
from pydantic import BaseModel, Field

from . import auth, constant, db
from .accounts import User
from .admin import _forbidden, web_admin
from .quota import KST
from .templating import templates

router = APIRouter()

KINDS = {"privacy": "개인정보 처리방침", "terms": "이용약관"}
COLUMNS = ("id", "kind", "body", "effective_on", "created_at", "created_by")
TEMPLATE_DIR = constant.BASE_DIR / "src" / "ebms_server" / "policy_templates"
PLACEHOLDER = re.compile(r"\[\[[^\]\n]+\]\]")


def today(now: float | None = None) -> str:
    return datetime.datetime.fromtimestamp(time.time() if now is None else now, KST).strftime("%Y-%m-%d")


def template(kind: str) -> str:
    return (TEMPLATE_DIR / f"{kind}.md").read_text(encoding="utf-8")


def _row(row) -> dict:
    return dict(zip(COLUMNS, row))


def versions(kind: str) -> list[dict]:
    """저장한 모든 판. 시행일이 늦은 순(같으면 나중에 저장한 순)."""
    with db.connect() as con, con.cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(COLUMNS)} FROM policy WHERE kind = %s ORDER BY effective_on DESC, id DESC",
            (kind,),
        )
        return [_row(row) for row in cur.fetchall()]


def get_version(version_id: int) -> dict:
    with db.connect() as con, con.cursor() as cur:
        cur.execute(f"SELECT {', '.join(COLUMNS)} FROM policy WHERE id = %s", (version_id,))
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="policy version not found")
    return _row(row)


def state(kind: str, now: float | None = None) -> dict:
    """지금 판(current, 없으면 None), 변경 예정 판(upcoming, 시행일이 이른 순), 지난 판(past)."""
    day = today(now)
    rows = versions(kind)
    upcoming = [v for v in rows if v["effective_on"] > day]
    started = [v for v in rows if v["effective_on"] <= day]
    return {
        "current": started[0] if started else None,
        "upcoming": upcoming[::-1],
        "past": started[1:],
    }


def signup_versions(now: float | None = None) -> tuple[int | None, int | None]:
    """가입 화면이 보여주는 (이용약관, 개인정보 처리방침) 판 id. 템플릿이면 None."""
    return tuple((state(kind, now)["current"] or {}).get("id") for kind in ("terms", "privacy"))


def signup_docs(now: float | None = None) -> list[dict]:
    """가입 화면에 보여줄 이용약관·개인정보 처리방침(지금 판, 없으면 템플릿)."""
    docs = []
    for kind in ("terms", "privacy"):
        current = state(kind, now)["current"]
        docs.append({
            "kind": kind,
            "title": KINDS[kind],
            "version": current["id"] if current else None,
            "content": render(_strip_title(current["body"] if current else template(kind))),
        })
    return docs


def current_body(kind: str, now: float | None = None) -> str:
    current = state(kind, now)["current"]
    return current["body"] if current else template(kind)


# ---- 본문 렌더링 ----

_INLINE = re.compile(r"\*\*(.+?)\*\*|\[([^\]\n]+)\]\(([^)\s]+)\)")
_SAFE_URL = re.compile(r"(https?://|mailto:|/)", re.IGNORECASE)


def _inline(text: str) -> str:
    out, pos = [], 0
    for m in _INLINE.finditer(text):
        out.append(html.escape(text[pos:m.start()]))
        if m.group(1) is not None:
            out.append(f"<strong>{html.escape(m.group(1))}</strong>")
        elif _SAFE_URL.match(m.group(3)) and not m.group(3).startswith("//"):
            out.append(f'<a href="{html.escape(m.group(3))}">{html.escape(m.group(2))}</a>')
        else:
            out.append(html.escape(m.group(0)))
        pos = m.end()
    out.append(html.escape(text[pos:]))
    return "".join(out)


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def render(body: str) -> Markup:
    """마크다운 일부를 HTML로 바꿉니다: `#`~`###` 제목, `-` 목록, `1.` 번호 목록, `|` 표, 빈 줄로 나눈 문단."""
    out: list[str] = []
    lines = body.replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        if not line.strip():
            i += 1
            continue
        if m := re.match(r"(#{1,3})\s+(.*)", line):
            level = len(m.group(1))
            out.append(f"<h{level}>{_inline(m.group(2))}</h{level}>")
            i += 1
        elif line.lstrip().startswith("|"):
            rows = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                rows.append(_cells(lines[i]))
                i += 1
            head = rows[0]
            if len(rows) > 1 and all(re.fullmatch(r":?-+:?", c) for c in rows[1]):
                rows = rows[2:]
            else:
                head, rows = None, rows
            table = ['<div class="table-responsive"><table class="table table-sm table-bordered">']
            if head:
                table.append("<thead><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in head) + "</tr></thead>")
            table.append("<tbody>")
            table += ["<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in rows]
            table.append("</tbody></table></div>")
            out.append("".join(table))
        elif re.match(r"\s*(-|\*|\d+\.)\s+", line):
            tag = "ol" if re.match(r"\s*\d+\.", line) else "ul"
            items = []
            while i < len(lines) and (m := re.match(r"\s*(?:-|\*|\d+\.)\s+(.*)", lines[i])):
                items.append(f"<li>{_inline(m.group(1).rstrip())}</li>")
                i += 1
            out.append(f"<{tag}>{''.join(items)}</{tag}>")
        else:
            para = []
            while i < len(lines) and lines[i].strip() and not re.match(r"\s*(#{1,3}\s|\||-\s|\*\s|\d+\.\s)", lines[i]):
                para.append(_inline(lines[i].strip()))
                i += 1
            out.append(f"<p>{'<br>'.join(para)}</p>")
    return Markup("\n".join(out))


def _strip_title(body: str) -> str:
    """본문 첫 줄의 `# 제목`은 페이지 제목과 겹치므로 뺍니다."""
    return re.sub(r"\A\s*#\s+[^\n]*\n?", "", body)


# ---- 공개 페이지 ----

def _page(request: Request, kind: str, user: User | None, version: dict | None = None):
    st = state(kind)
    shown = version or st["current"]
    body = shown["body"] if shown else template(kind)
    return templates.TemplateResponse(
        request=request,
        name="pages/policy.html",
        context={
            "user": user,
            "kind": kind,
            "title": KINDS[kind],
            "content": render(_strip_title(body)),
            "shown": shown,
            "is_current": shown is not None and st["current"] is not None and shown["id"] == st["current"]["id"],
            "is_template": shown is None,
            "state": st,
        },
    )


def _public(kind: str):
    def page(request: Request, session: Annotated[tuple[User, int] | None, Depends(auth.optional_session)]):
        return _page(request, kind, session[0] if session else None)

    def version_page(
        version_id: int,
        request: Request,
        session: Annotated[tuple[User, int] | None, Depends(auth.optional_session)],
    ):
        version = get_version(version_id)
        if version["kind"] != kind:
            raise HTTPException(status_code=404, detail="policy version not found")
        return _page(request, kind, session[0] if session else None, version)

    router.add_api_route(f"/{kind}", page, methods=["GET"], response_class=HTMLResponse)
    router.add_api_route(f"/{kind}/{{version_id}}", version_page, methods=["GET"], response_class=HTMLResponse)


for _kind in KINDS:
    _public(_kind)


# ---- 관리자 ----

class PolicyBody(BaseModel):
    body: str = Field(min_length=1, max_length=200000)
    # 시행일(KST, YYYY-MM-DD). 비우면 오늘
    effective_on: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")


@router.get("/admin/policies", response_class=HTMLResponse)
def policies_page(
    request: Request,
    session: Annotated[tuple[User, int] | None, Depends(auth.optional_session)],
):
    if session is None:
        return auth._login_redirect(request)
    if not session[0].is_admin:
        return _forbidden(request)
    docs = []
    for kind, title in KINDS.items():
        st = state(kind)
        # 편집 칸에는 가장 최근에 저장한 판(예정 판 포함), 없으면 템플릿을 채웁니다
        latest = (st["upcoming"][-1] if st["upcoming"] else None) or st["current"]
        body = latest["body"] if latest else template(kind)
        docs.append({
            "kind": kind,
            "title": title,
            "body": body,
            "placeholders": len(PLACEHOLDER.findall(body)),
            "state": st,
            "versions": versions(kind),
        })
    return templates.TemplateResponse(
        request=request,
        name="pages/admin_policies.html",
        context={"user": session[0], "docs": docs, "today": today()},
    )


@router.get("/api/admin/policies/{kind}/template")
def admin_template(kind: Literal[tuple(KINDS)], admin: Annotated[User, Depends(web_admin)]):
    return {"kind": kind, "body": template(kind)}


@router.post("/api/admin/policies/{kind}")
def admin_save(kind: Literal[tuple(KINDS)], body: PolicyBody, admin: Annotated[User, Depends(web_admin)]):
    """새 판을 저장합니다. 이전 판은 변경 이력으로 남습니다."""
    effective_on = body.effective_on or today()
    try:
        datetime.date.fromisoformat(effective_on)
    except ValueError:
        raise HTTPException(status_code=422, detail="effective_on is not a valid date")
    with db.connect() as con, con.cursor() as cur:
        cur.execute(
            "INSERT INTO policy (kind, body, effective_on, created_at, created_by) VALUES (%s, %s, %s, %s, %s)",
            (kind, body.body, effective_on, int(time.time()), admin.id),
        )
        version_id = cur.lastrowid
        con.commit()
    return get_version(version_id)


@router.delete("/api/admin/policies/versions/{version_id}")
def admin_delete(version_id: int, admin: Annotated[User, Depends(web_admin)]):
    """잘못 저장한 판을 지웁니다. 모든 판을 지우면 템플릿을 보여줍니다."""
    get_version(version_id)
    with db.connect() as con, con.cursor() as cur:
        cur.execute("DELETE FROM policy WHERE id = %s", (version_id,))
        con.commit()
    return {"ok": True}
