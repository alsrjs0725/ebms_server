"""관리자(개인정보취급자) 접속기록. 관리자가 사용자 정보를 보거나 바꿀 때 누가·어디서·누구의·무엇을 했는지 남깁니다.

`var/log/audit.log`에 따로 쓰고 1년 넘게 보관합니다(안전성 확보조치 기준 고시: 접속기록 1년 이상).
`ebms.log`에도 같은 줄이 들어가 관리자 로그 화면에서 로거 `ebms_server.audit`로 볼 수 있습니다.
"""
import logging
import logging.handlers

from fastapi import Request

from . import constant

logger = logging.getLogger(__name__)

LOG_NAME = "audit.log"
# 7일 단위 회전 × 53개 + 지금 파일 = 1년 이상
ROTATE_DAYS = 7
BACKUP_COUNT = 53


def file_handler(formatter: logging.Formatter) -> logging.Handler:
    handler = logging.handlers.TimedRotatingFileHandler(
        constant.LOG_DIR / LOG_NAME,
        when="D",
        interval=ROTATE_DAYS,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setFormatter(formatter)
    return handler


def _value(value) -> str:
    """공백·따옴표가 있으면 repr로 감싸 한 줄에서 항목이 섞이지 않게 합니다."""
    text = str(value)
    if not text or any(c.isspace() or c in "\"'=" for c in text):
        return repr(text)
    return text


def record(request: Request, admin_id: str, action: str, target: str | list[str] = "", **detail) -> None:
    """`admin=<id> ip=<접속지> action=<행위> target=<대상 user_id> key=value ...` 한 줄을 남깁니다.
    target이 여러 명이면 쉼표로 잇습니다. 시각은 로그 줄 머리에 붙습니다."""
    if isinstance(target, list):
        target = ",".join(target)
    ip = request.client.host if request.client else ""
    parts = [f"admin={admin_id}", f"ip={ip}", f"action={action}", f"target={target or '-'}"]
    parts += [f"{key}={_value(value)}" for key, value in detail.items()]
    logger.info(" ".join(parts))
