"""관리자 접속기록과 로그 보관 설정 테스트 (#101)."""
import logging

from ebms_server import audit, constant, main

from test_auth import client, oauth  # noqa: F401  (fixture 재사용)
from test_client_auth import client_login
from test_quota import admin_login


def audit_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == audit.__name__]


def test_admin_user_access_is_recorded(client, monkeypatch, caplog):
    _, user_id = client_login(client, monkeypatch)
    admin_login(client, monkeypatch)
    caplog.set_level(logging.INFO, logger=audit.__name__)
    admin_id = next(u["id"] for u in client.get("/api/admin/users", params={"q": "admin"}).json())
    caplog.clear()

    client.get("/api/admin/users", params={"q": "alic"})
    client.get(f"/api/admin/users/{user_id}")
    client.put(f"/api/admin/users/{user_id}", json={"status": "banned", "max_tickets": None})
    client.post(f"/api/admin/users/{user_id}/refill")
    client.get("/admin", params={"q": "alice smith"})
    # 없는 사용자·잘못된 값은 아무것도 보거나 바꾸지 않았으므로 남기지 않습니다.
    assert client.get("/api/admin/users/nope").status_code == 404
    assert client.put(f"/api/admin/users/{user_id}", json={"role": "x"}).status_code == 400

    head = f"admin={admin_id} ip=testclient"
    assert audit_lines(caplog) == [
        f"{head} action=user.search target={user_id} q=alic",
        f"{head} action=user.view target={user_id}",
        f"{head} action=user.update target={user_id} status=banned max_tickets=null",
        f"{head} action=user.refill target={user_id}",
        f"{head} action=user.search target=- q='alice smith'",
    ]


def test_configure_logging_files(tmp_path, monkeypatch):
    monkeypatch.setattr(constant, "LOG_DIR", tmp_path)
    access = logging.getLogger("uvicorn.access")
    uvicorn_logger = logging.getLogger("uvicorn")
    saved = access.handlers[:], access.propagate, logging.root.handlers[:], logging.root.level
    saved_uvicorn = uvicorn_logger.handlers[:], uvicorn_logger.propagate
    try:
        main.configure_logging()
        # uvicorn 접근 로그는 access.log에만(stdout·ebms.log로 가지 않음), 날마다 돌려 ACCESS_LOG_DAYS개
        [handler] = access.handlers
        assert handler.baseFilename == str(tmp_path / "access.log")
        assert handler.backupCount == constant.ACCESS_LOG_DAYS
        assert access.propagate is False
        access.info('%s - "%s %s HTTP/%s" %d', "203.0.113.5:1234", "GET", "/api/play/song/1", "1.1", 200)

        [audit_handler] = logging.getLogger(audit.__name__).handlers
        assert audit_handler.baseFilename == str(tmp_path / "audit.log")
        # 7일 × (53 + 지금 파일) ≥ 1년
        assert audit.ROTATE_DAYS * (audit.BACKUP_COUNT + 1) >= 366
        logging.getLogger(audit.__name__).info("admin=a ip=1.2.3.4 action=user.view target=u")

        # 요청 처리 중 난 예외(uvicorn이 남기는 트레이스백)는 ebms.log에도
        assert uvicorn_logger.handlers == [] and uvicorn_logger.propagate is True
        logging.getLogger("uvicorn.error").error("Exception in ASGI application")
    finally:
        for h in access.handlers + logging.getLogger(audit.__name__).handlers + logging.root.handlers:
            h.close()
        access.handlers[:] = saved[0]
        access.propagate = saved[1]
        uvicorn_logger.handlers[:] = saved_uvicorn[0]
        uvicorn_logger.propagate = saved_uvicorn[1]
        logging.root.handlers[:] = saved[2]
        logging.root.setLevel(saved[3])
        logging.getLogger(audit.__name__).handlers.clear()

    assert '203.0.113.5:1234 - "GET /api/play/song/1 HTTP/1.1" 200' in (tmp_path / "access.log").read_text()
    assert "203.0.113.5" not in (tmp_path / "ebms.log").read_text()
    assert "action=user.view" in (tmp_path / "audit.log").read_text()
    assert "ERROR uvicorn.error: Exception in ASGI application" in (tmp_path / "ebms.log").read_text()
    assert "action=user.view" in (tmp_path / "ebms.log").read_text()
