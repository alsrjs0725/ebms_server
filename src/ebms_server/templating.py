import functools
import hashlib

import jinja2
from fastapi.templating import Jinja2Templates

from . import constant

STATIC_DIR = constant.BASE_DIR / "src" / "ebms_server" / "static"

templates = Jinja2Templates(
    directory=constant.BASE_DIR / "src" / "ebms_server" / "templates"
)


@functools.cache
def _static_version(path: str) -> str:
    try:
        return hashlib.sha256((STATIC_DIR / path.lstrip("/")).read_bytes()).hexdigest()[:12]
    except OSError:
        return ""


@jinja2.pass_context
def static_url(context, path: str) -> str:
    """static 파일 주소에 내용 해시(?v=)를 붙입니다. 서버를 업데이트하면 주소가 바뀌어
    브라우저가 캐시해 둔 예전 스크립트·스타일을 쓰지 않습니다."""
    url = str(context["request"].url_for("static", path=path))
    version = _static_version(path)
    return f"{url}?v={version}" if version else url


templates.env.globals["static_url"] = static_url
