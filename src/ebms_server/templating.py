from fastapi.templating import Jinja2Templates

from . import constant

templates = Jinja2Templates(
    directory=constant.BASE_DIR / "src" / "ebms_server" / "templates"
)
