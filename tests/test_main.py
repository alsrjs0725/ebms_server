"""서버 시작·종료(lifespan), 시작 후 백필, 저장소 gc 루프, 처리하지 못한 예외 응답 테스트."""
import logging
import threading

from fastapi.testclient import TestClient

from ebms_server import auth, constant, importer, main, preview, retention, staging, tables

from test_auth import client  # noqa: F401  (fixture 재사용)

def fake_database(calls: list, fail: set = frozenset()):
    """메서드를 부를 때마다 이름을 calls에 남기는 가짜 Database. fail에 든 이름은 예외를 던집니다."""

    class FakeDatabase:
        def __getattr__(self, name):
            def method():
                calls.append(name)
                if name in fail:
                    raise RuntimeError(f"{name} broke")

            return method

    return FakeDatabase


def test_background_startup_keeps_going_after_a_step_fails(monkeypatch, caplog):
    calls = []
    monkeypatch.setattr(main, "Database", fake_database(calls, fail={"backfill_pre_chunks"}))
    monkeypatch.setattr(main.pre_rebalance, "kick", lambda: calls.append("pre_rebalance.kick"))
    monkeypatch.setattr(preview, "kick", lambda: calls.append("preview.kick"))

    main.background_startup()

    # 사전 청크 만들기가 실패해도 재배치·미리듣기는 그대로 돕니다.
    assert calls == ["backfill_pre_chunks", "pre_rebalance.kick", "preview.kick"]
    failed = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert failed == ["backfill_pre_chunks failed"]


def test_store_gc_loop_retries_after_error_until_stopped(monkeypatch, caplog):
    stop = threading.Event()
    calls = []

    class FakeDatabase:
        def store_gc(self):
            calls.append(len(calls))
            if len(calls) == 1:
                raise OSError("disk gone")
            if len(calls) == 3:
                stop.set()

    monkeypatch.setattr(main, "Database", FakeDatabase)
    monkeypatch.setattr(constant, "STORE_GC_HOURS", 0)

    main.store_gc_loop(stop)
    # 처음 실패해도 루프는 이어 돌고, stop이 서면 멈춥니다.
    assert calls == [0, 1, 2]
    assert "store_gc failed" in caplog.text


def test_store_gc_loop_does_nothing_when_already_stopped(monkeypatch):
    stop = threading.Event()
    stop.set()
    monkeypatch.setattr(main, "Database", fake_database(calls := []))
    main.store_gc_loop(stop)
    assert calls == []


def test_unhandled_error_hides_cause_outside_admin_api(client, monkeypatch):
    """관리자 API가 아닌 곳의 500은 예외 내용을 보여주지 않습니다."""

    def broken():
        raise RuntimeError("secret detail /app/var")

    monkeypatch.setattr(auth, "configured_oauths", broken)
    quiet = TestClient(client.app, raise_server_exceptions=False)
    r = quiet.get("/api/version")
    assert r.status_code == 500
    assert r.headers["content-type"].startswith("text/plain")
    assert r.text == "Internal Server Error"
    assert "secret" not in r.text and "RuntimeError" not in r.text


def test_lifespan_starts_and_stops_background_work(client, monkeypatch, caplog, tmp_path):
    calls = []
    retention_stop, tables_stop = threading.Event(), threading.Event()
    store_stops = []
    started = threading.Event()

    monkeypatch.setattr(main, "configure_logging", lambda: calls.append("configure_logging"))
    monkeypatch.setattr(main, "background_startup", lambda: (calls.append("backfill"), started.set()))
    monkeypatch.setattr(main, "store_gc_loop", lambda stop: store_stops.append(stop))
    monkeypatch.setattr(importer, "recover", lambda: calls.append("importer.recover"))
    monkeypatch.setattr(staging, "recover", lambda: calls.append("staging.recover"))
    monkeypatch.setattr(retention, "start", lambda: retention_stop)
    monkeypatch.setattr(tables, "start", lambda: tables_stop)
    monkeypatch.setattr(constant, "STORE_GC_HOURS", 1)
    monkeypatch.setattr(constant, "SECRET_KEY", "")
    monkeypatch.setattr(constant, "PUBLIC_URL", "http://ebms.example.com")
    monkeypatch.setattr(auth, "configured_oauths", lambda: [])
    # 이전 실행에서 미리듣기를 만들다 남긴 임시 파일은 지웁니다.
    work = constant.CACHE_DIR / "work"
    work.mkdir(parents=True)
    (work / "left.wav").write_bytes(b"x")
    monkeypatch.setattr(constant, "TMP_DIR", tmp_path / "fresh_tmp")

    with caplog.at_level(logging.WARNING), TestClient(client.app) as c:
        assert c.get("/api/version").status_code == 200
        assert started.wait(5)
        assert not work.exists()
        assert constant.TMP_DIR.is_dir()
        assert not retention_stop.is_set() and not tables_stop.is_set()
        assert len(store_stops) == 1 and not store_stops[0].is_set()

    # 백필은 뒤 스레드에서 돌아 순서가 정해지지 않습니다.
    assert [c for c in calls if c != "backfill"] == ["configure_logging", "importer.recover", "staging.recover"]
    assert calls.count("backfill") == 1
    # 종료할 때 뒤에서 도는 작업에 멈추라고 알립니다.
    assert retention_stop.is_set() and tables_stop.is_set() and store_stops[0].is_set()
    warnings = caplog.text
    assert "EBMS_SECRET_KEY is not set" in warnings
    assert "is not https" in warnings
    assert "No OAuth login is configured" in warnings


def test_lifespan_without_optional_workers(client, monkeypatch, caplog):
    store_stops = []
    monkeypatch.setattr(main, "configure_logging", lambda: None)
    monkeypatch.setattr(main, "background_startup", lambda: None)
    monkeypatch.setattr(main, "store_gc_loop", lambda stop: store_stops.append(stop))
    monkeypatch.setattr(constant, "STORE_GC_HOURS", 0)
    monkeypatch.setattr(constant, "RETENTION_INTERVAL_HOURS", 0)
    monkeypatch.setattr(constant, "TABLE_REFRESH_HOURS", 0)
    monkeypatch.setattr(constant, "PUBLIC_URL", "https://ebms.example.com")

    # 설정이 갖춰져 있으면 경고 없이 뜨고, 꺼 둔 작업은 띄우지 않아도 종료가 됩니다.
    with caplog.at_level(logging.WARNING), TestClient(client.app) as c:
        assert c.get("/api/version").status_code == 200
    assert store_stops == []
    assert "is not https" not in caplog.text
    assert "No OAuth login" not in caplog.text
    assert "EBMS_SECRET_KEY" not in caplog.text
