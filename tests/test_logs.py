"""관리자 서버 로그 조회 테스트."""
import pytest

from ebms_server import constant, logs

from test_auth import client, oauth  # noqa: F401  (fixture 재사용)
from test_client_auth import client_login
from test_quota import admin_login


@pytest.fixture(autouse=True)
def log_dir(tmp_path, monkeypatch):
    d = tmp_path / "log"
    d.mkdir()
    monkeypatch.setattr(constant, "LOG_DIR", d)
    return d


OLD = (
    "2026-10-04 10:00:00,000 INFO ebms_server.main: started\n"
    "2026-10-04 10:00:01,000 WARNING ebms_server.importer: import skipped[a]: ambiguous\n"
)
CURRENT = (
    "2026-10-11 09:00:00,000 INFO ebms_server.importer: import job done[x.zip]: 3 songs\n"
    "2026-10-11 09:00:01,000 ERROR ebms_server.importer: import failed[b]\n"
    "Traceback (most recent call last):\n"
    "  File \"x.py\", line 1\n"
    "ValueError: 깨진 차트\n"
    "2026-10-11 09:00:02,000 INFO ebms_server.db: insert_song: Song[3] kept existing files\n"
)


def write(log_dir):
    (log_dir / "ebms.log.2026-10-04").write_text(OLD, encoding="utf-8")
    (log_dir / "ebms.log").write_text(CURRENT, encoding="utf-8")


def messages(result):
    return [e["message"] for e in result["entries"]]


def test_search_newest_first_with_traceback(log_dir):
    write(log_dir)
    r = logs.search()
    assert messages(r) == [
        "insert_song: Song[3] kept existing files",
        "import failed[b]",
        "import job done[x.zip]: 3 songs",
        "import skipped[a]: ambiguous",
        "started",
    ]
    assert r["entries"][1]["detail"].endswith("ValueError: 깨진 차트")
    assert r["entries"][1]["file"] == "ebms.log"
    assert r["next"] is None


def test_search_filters(log_dir):
    write(log_dir)
    assert messages(logs.search(level="warning")) == ["import failed[b]", "import skipped[a]: ambiguous"]
    assert len(logs.search(logger="ebms_server.importer")["entries"]) == 3
    assert len(logs.search(logger="ebms_server")["entries"]) == 5
    assert logs.search(logger="ebms_server.import")["entries"] == []
    assert messages(logs.search(q="깨진")) == ["import failed[b]"]  # 트레이스백도 검색
    assert messages(logs.search(q="2026-10-04 10:00:01")) == ["import skipped[a]: ambiguous"]
    with pytest.raises(ValueError):
        logs.search(level="LOUD")
    with pytest.raises(ValueError):
        logs.search(cursor="../etc/passwd:0")


def test_search_pages_with_cursor(log_dir, monkeypatch):
    write(log_dir)
    monkeypatch.setattr(logs, "BLOCK", 16)  # 블록 경계에 걸친 줄도 맞게 읽는지
    seen = []
    cursor = ""
    while True:
        r = logs.search(cursor=cursor, limit=2)
        seen += messages(r)
        if not r["next"]:
            break
        cursor = r["next"]
    assert seen == messages(logs.search())


def test_logs_api(client, monkeypatch, log_dir):
    write(log_dir)
    assert client.get("/admin/logs", follow_redirects=False).status_code == 303
    assert client.get("/api/admin/logs").status_code == 401
    client_login(client, monkeypatch)
    assert client.get("/admin/logs").status_code == 403
    assert client.get("/api/admin/logs").status_code == 403

    admin_login(client, monkeypatch)
    assert client.get("/admin/logs").status_code == 200
    r = client.get("/api/admin/logs", params={"level": "ERROR"})
    assert r.status_code == 200
    assert messages(r.json()) == ["import failed[b]"]
    assert client.get("/api/admin/logs", params={"cursor": "nope:1"}).status_code == 400
