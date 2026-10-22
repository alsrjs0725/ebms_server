import logging

from ebms_server import constant, db


def test_backfill_progress_logs_until_done(caplog, monkeypatch):
    monkeypatch.setattr(constant, "BACKFILL_PROGRESS_SECONDS", 0)
    caplog.set_level(logging.INFO)
    progress = db._Progress(logging.getLogger("test"), "job", 250)
    for done in (100, 200, 250):
        progress.update(done)
    messages = [r.message for r in caplog.records]
    assert messages[0].startswith("job: 100/250 (40%")
    assert "left" in messages[0]
    assert messages[-1].startswith("job: 250/250 (100%")


def test_backfill_progress_throttles(caplog):
    caplog.set_level(logging.INFO)
    progress = db._Progress(logging.getLogger("test"), "job", 300)
    progress.update(100)
    progress.update(200)
    progress.update(300)
    assert [r.message.split(" (")[0] for r in caplog.records] == ["job: 300/300"]
