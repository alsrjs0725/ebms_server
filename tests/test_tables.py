"""난이도표 읽기(beatoraja 방식), 관리자 등록, 사전 청크 태그 테스트."""
import hashlib
import json
import threading

import pytest

from ebms_server import constant, db as db_module, tables

from test_auth import client, oauth  # noqa: F401  (fixture 재사용)
from test_client_auth import client_login
from test_import import import_dir, make_zip, upload  # noqa: F401
from test_quota import admin_login

A = b"#TITLE A\n"
B = b"#TITLE B\n"
C = b"#TITLE C\n"


def md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fake_web(pages: dict[str, object]):
    """주소 -> 본문(bytes·str·JSON으로 바꿀 값). 없는 주소는 HTTP 404."""
    calls = []

    def get(url):
        calls.append(url)
        if url not in pages:
            raise tables.TableError(f"{url}: HTTP 404")
        body = pages[url]
        if isinstance(body, bytes):
            return body
        if isinstance(body, str):
            return body.encode()
        return json.dumps(body).encode()

    get.calls = calls
    return get


def test_parse_html_meta_and_relative_urls():
    html = """<html><head>
    <meta charset="utf-8">
    <META content='data/header.json' NAME='bmstable'>
    </head></html>"""
    rows = [
        {"md5": md5(A).upper(), "level": "1", "title": "A"},
        {"sha256": sha256(B), "level": 2, "title": "B"},
        # level이 없거나 해시가 짧으면 뺍니다.
        {"md5": md5(C), "title": "no level"},
        {"md5": "abc", "level": "1"},
        # title이 문자열이 아닌 행은 건너뜁니다.
        {"md5": md5(C), "level": "1", "title": 3},
        # 같은 차트는 처음 것만
        {"md5": md5(A), "level": "9"},
        "not a row",
    ]
    get = fake_web({
        "https://t.example/table/": html,
        "https://t.example/table/data/header.json": {"name": " Insane ", "symbol": "★", "data_url": "../score.json"},
        "https://t.example/table/score.json": rows,
    })
    table = tables.parse_table("https://t.example/table/", get)
    assert table["name"] == "Insane" and table["symbol"] == "★"
    assert table["header_url"] == "https://t.example/table/data/header.json"
    assert table["charts"] == [(md5(A), "", "1"), ("", sha256(B), "2")]


def test_parse_json_header_and_data_rule():
    get = fake_web({
        "https://t.example/h.json": {
            "name": "N", "symbol": "n",
            "data_url": ["a.json", "missing.json", "b.json"],
            "data_rule": [{"1": "", "2": "II"}],
        },
        "https://t.example/a.json": [
            {"md5": md5(A), "level": "1"},
            {"md5": md5(B), "level": "2"},
        ],
        "https://t.example/b.json": [{"md5": md5(C), "level": "1"}],
    })
    table = tables.parse_table("https://t.example/h.json", get)
    # 헤더 JSON 주소면 HTML을 읽지 않습니다.
    assert get.calls[0] == "https://t.example/h.json"
    # data_rule로 레벨 1은 빼고 2는 II로. 읽지 못한 data_url은 건너뜁니다. 규칙은 data_url 순서대로 붙습니다.
    assert table["charts"] == [(md5(B), "", "II"), (md5(C), "", "1")]


@pytest.mark.parametrize(
    "pages, message",
    [
        ({"https://t.example/": "<html></html>"}, "bmstable"),
        ({"https://t.example/": '<meta name="bmstable" content="h.json">', "https://t.example/h.json": "{"}, "JSON"),
        ({"https://t.example/": '<meta name="bmstable" content="h.json">', "https://t.example/h.json": {"name": "x"}}, "symbol"),
        (
            {"https://t.example/": '<meta name="bmstable" content="h.json">',
             "https://t.example/h.json": {"name": "x", "symbol": "x", "data_url": "d.json"}},
            "404",
        ),
    ],
)
def test_parse_errors(pages, message):
    with pytest.raises(tables.TableError, match=message):
        tables.parse_table("https://t.example/", fake_web(pages))


def test_http_get_rejects_other_schemes():
    with pytest.raises(tables.TableError):
        tables.http_get("file:///etc/passwd")


def test_tables_admin_only(client, monkeypatch):
    assert client.get("/api/pre/tables").status_code == 401
    assert client.post("/api/admin/tables", json={"url": "https://t.example/"}).status_code == 401
    client_login(client, monkeypatch)
    assert client.get("/api/pre/tables").status_code == 200
    assert client.post("/api/admin/tables", json={"url": "https://t.example/"}).status_code == 403
    assert client.get("/admin/tables").status_code == 403


def test_register_and_chunk_tags(client, monkeypatch):
    monkeypatch.setattr(constant, "SONGS_PER_PRE_CHUNK", 2)
    admin_login(client, monkeypatch)
    for name, chart in (("a.zip", A), ("b.zip", B), ("c.zip", C)):
        upload(client, name, make_zip({"x.bms": chart, "banner.png": b"png"}))

    pages = {
        "https://t.example/insane.html": '<meta name="bmstable" content="h.json">',
        "https://t.example/h.json": {"name": "Insane", "symbol": "★", "data_url": "d.json"},
        # 서버에 없는 차트도 실립니다.
        "https://t.example/d.json": [
            {"md5": md5(A), "level": "1"},
            {"md5": "f" * 32, "level": "2"},
        ],
        "https://t.example/other.json": {"name": "Other", "symbol": "o", "data_url": "o.json"},
        "https://t.example/o.json": [{"sha256": sha256(C), "level": "1"}, {"sha256": sha256(A), "level": "1"}],
    }
    monkeypatch.setattr(tables, "http_get", fake_web(pages))

    assert client.get("/admin/tables").status_code == 200
    r = client.post("/api/admin/tables", json={"url": " https://t.example/insane.html "})
    assert r.status_code == 200, r.text
    insane = r.json()
    assert (insane["name"], insane["symbol"], insane["charts"], insane["error"]) == ("Insane", "★", 2, None)
    assert client.post("/api/admin/tables", json={"url": "https://t.example/insane.html"}).status_code == 400
    assert client.post("/api/admin/tables", json={"url": "https://t.example/none.html"}).status_code == 400
    other = client.post("/api/admin/tables", json={"url": "https://t.example/other.json"}).json()
    assert "Insane" in client.get("/admin/tables").text

    listed = {t["id"]: t for t in client.get("/api/admin/tables").json()}
    assert (listed[insane["id"]]["matched_charts"], listed[insane["id"]]["songs"]) == (1, 1)
    assert (listed[other["id"]]["matched_charts"], listed[other["id"]]["songs"]) == (2, 2)

    # 곡 1(A)·2(B)·3(C)는 표가 없을 때 들어와 2곡씩 청크 0(곡 1·2)·1(곡 3)에 있습니다.
    pre = client.get("/api/pre/tables").json()
    assert pre["chunks"] == {"0": [insane["id"], other["id"]], "1": [other["id"]]}
    assert [t["name"] for t in pre["tables"]] == ["Insane", "Other"]
    assert pre["tables"][0]["songs"] == 1

    # 다시 읽기: 표가 바뀌면 차트 목록을 바꾸고, 읽지 못하면 이전 목록을 두고 오류만 남깁니다.
    pages["https://t.example/d.json"] = [{"md5": md5(B), "level": "1"}]
    r = client.post(f"/api/admin/tables/{insane['id']}/refresh").json()
    assert r["charts"] == 1 and r["error"] is None
    assert client.get("/api/pre/tables").json()["chunks"] == {"0": [insane["id"], other["id"]], "1": [other["id"]]}
    # 재배치: 청크 0에 섞인 곡 2(Insane)를 새 청크로 옮겨 Insane만 고르면 곡 2만 받습니다.
    assert db_module.Database().rebalance_pre_chunks() == 1
    assert client.get("/api/pre/tables").json()["chunks"] == {
        "0": [other["id"]], "1": [other["id"]], "2": [insane["id"]],
    }
    assert db_module.Database().rebalance_pre_chunks() == 0
    del pages["https://t.example/d.json"]
    r = client.post(f"/api/admin/tables/{insane['id']}/refresh").json()
    assert r["charts"] == 1 and "404" in r["error"]
    assert client.get("/api/pre/tables").json()["chunks"]["2"] == [insane["id"]]

    assert client.delete(f"/api/admin/tables/{insane['id']}").json() == {"ok": True}
    assert client.delete(f"/api/admin/tables/{insane['id']}").status_code == 404
    assert client.post(f"/api/admin/tables/{insane['id']}/refresh").status_code == 404
    assert client.get("/api/pre/tables").json()["chunks"] == {"0": [other["id"]], "1": [other["id"]]}

    # 지운 곡은 태그에서 빠집니다.
    assert client.request("DELETE", "/api/admin/songs/3", json={"reason": "x"}).status_code == 200
    assert client.get("/api/pre/tables").json()["chunks"] == {"0": [other["id"]]}


def test_backfill_chart_md5(client, monkeypatch):
    admin_login(client, monkeypatch)
    upload(client, "a.zip", make_zip({"x.bms": A}))
    upload(client, "b.zip", make_zip({"y.bms": B}))
    database = db_module.Database()
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("SELECT id, md5 FROM chart ORDER BY song_id")
        assert cur.fetchall() == ((sha256(A), md5(A)), (sha256(B), md5(B)))
        # 이전 버전에서 넣은 차트, 어느 청크에도 없는 차트
        cur.execute("UPDATE chart SET md5 = NULL")
        cur.execute("INSERT INTO chart (id, song_id, size, filename) VALUES (%s, NULL, 1, 'z.bms')", ("e" * 64,))
        con.commit()
    assert database.backfill_chart_md5() == 2
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("SELECT id, md5 FROM chart ORDER BY id")
        assert dict(cur.fetchall()) == {sha256(A): md5(A), sha256(B): md5(B), "e" * 64: ""}
    assert database.backfill_chart_md5() == 0


class FakeResponse:
    def __init__(self, body: bytes):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n):
        return self.body[:n]


def test_http_get(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout):
        seen["ua"], seen["timeout"] = req.get_header("User-agent"), timeout
        return FakeResponse(b"body")

    monkeypatch.setattr(tables.urllib.request, "urlopen", fake_urlopen)
    assert tables.http_get("https://t.example/") == b"body"
    # User-Agent가 없으면 403을 주는 사이트가 있습니다.
    assert seen == {"ua": "ebms-server (difficulty table)", "timeout": constant.TABLE_FETCH_TIMEOUT}


def test_http_get_rejects_too_large(monkeypatch):
    monkeypatch.setattr(constant, "TABLE_MAX_BYTES", 4)
    monkeypatch.setattr(tables.urllib.request, "urlopen", lambda req, timeout: FakeResponse(b"12345"))
    with pytest.raises(tables.TableError, match="4 바이트보다 큽니다"):
        tables.http_get("https://t.example/")
    monkeypatch.setattr(tables.urllib.request, "urlopen", lambda req, timeout: FakeResponse(b"1234"))
    assert tables.http_get("https://t.example/") == b"1234"


@pytest.mark.parametrize(
    "error, message",
    [
        (lambda url: tables.urllib.error.HTTPError(url, 503, "busy", {}, None), "HTTP 503"),
        (lambda url: tables.urllib.error.URLError("name not resolved"), "name not resolved"),
        (lambda url: TimeoutError("timed out"), "timed out"),
    ],
)
def test_http_get_errors_become_table_error(monkeypatch, error, message):
    def fake_urlopen(req, timeout):
        raise error(req.full_url)

    monkeypatch.setattr(tables.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(tables.TableError, match=message):
        tables.http_get("https://t.example/")


@pytest.mark.parametrize(
    "header, data, message",
    [
        ({"name": "x", "symbol": "x"}, None, "data_url이 없습니다"),
        ({"name": "x", "symbol": "x", "data_url": ["", "  "]}, None, "data_url이 없습니다"),
        ({"name": "x", "symbol": "x", "data_url": "d.json"}, {"not": "a list"}, "배열이 아닙니다"),
    ],
)
def test_parse_table_without_data(header, data, message):
    pages = {"https://t.example/h.json": header}
    if data is not None:
        pages["https://t.example/d.json"] = data
    with pytest.raises(tables.TableError, match=message):
        tables.parse_table("https://t.example/h.json", fake_web(pages))


def test_parse_table_skips_bad_rows_and_trims_fields():
    get = fake_web({
        "https://t.example/h.json": {
            "name": "N" * 300, "symbol": "S" * 100,
            # data_url 목록의 문자열이 아닌 값, 사전이 아닌 규칙은 무시합니다.
            "data_url": ["d.json", 3], "data_rule": ["not a dict"],
        },
        "https://t.example/d.json": [
            # 길이는 길지만 16진수가 아닌 해시는 뺍니다.
            {"md5": "z" * 32, "level": "1"},
            {"md5": md5(A), "sha256": sha256(A), "level": "L" * 100},
            # 같은 차트를 sha256으로 다시 실어도 처음 것만
            {"sha256": sha256(A), "level": "2"},
        ],
    })
    table = tables.parse_table("https://t.example/h.json", get)
    assert len(table["name"]) == 255 and len(table["symbol"]) == 64
    assert table["charts"] == [(md5(A), sha256(A), "L" * 64)]


def test_add_table_rejects_other_schemes(client, monkeypatch):
    with pytest.raises(tables.TableError, match="http"):
        tables.add_table("ftp://t.example/table.json")
    admin_login(client, monkeypatch)
    r = client.post("/api/admin/tables", json={"url": "javascript:alert(1)"})
    assert r.status_code == 400


def test_tables_page_needs_login(client):
    r = client.get("/admin/tables", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login")


def test_refresh_all_continues_after_failure(client, monkeypatch, caplog):
    pages = {
        "https://t.example/a.json": {"name": "A", "symbol": "a", "data_url": "da.json"},
        "https://t.example/da.json": [{"md5": md5(A), "level": "1"}],
        "https://t.example/b.json": {"name": "B", "symbol": "b", "data_url": "db.json"},
        "https://t.example/db.json": [{"md5": md5(B), "level": "1"}],
    }
    first = tables.add_table("https://t.example/a.json", fake_web(pages))
    second = tables.add_table("https://t.example/b.json", fake_web(pages))
    pages["https://t.example/db.json"].append({"md5": md5(C), "level": "2"})

    refreshed = []
    real_refresh = tables.refresh_table

    def flaky_refresh(table_id, get=None):
        refreshed.append(table_id)
        if table_id == first["id"]:
            raise RuntimeError("db hiccup")
        return real_refresh(table_id, get)

    monkeypatch.setattr(tables, "refresh_table", flaky_refresh)
    tables.refresh_all(fake_web(pages))
    # 한 표가 실패해도 다음 표는 다시 읽습니다.
    assert refreshed == [first["id"], second["id"]]
    assert tables.get_table(second["id"])["charts"] == 2
    assert tables.get_table(first["id"])["charts"] == 1
    assert f"difficulty table {first['id']}: refresh failed" in caplog.text


def test_start(monkeypatch):
    monkeypatch.setattr(constant, "TABLE_REFRESH_HOURS", 0)
    assert tables.start() is None

    calls = []
    monkeypatch.setattr(tables, "refresh_all", lambda: calls.append(1))
    monkeypatch.setattr(constant, "TABLE_REFRESH_HOURS", 1)
    stop = tables.start()
    [thread] = [t for t in threading.enumerate() if t.name == "difficulty_tables"]
    stop.set()
    thread.join(5)
    # 등록할 때 이미 읽었으므로 시작 직후에는 읽지 않고, 멈추면 스레드가 끝납니다.
    assert not thread.is_alive()
    assert calls == []


def test_new_song_goes_to_table_chunk(client, monkeypatch):
    monkeypatch.setattr(constant, "SONGS_PER_PRE_CHUNK", 2)
    admin_login(client, monkeypatch)
    pages = {
        "https://t.example/h.json": {"name": "Insane", "symbol": "★", "data_url": "d.json"},
        "https://t.example/d.json": [{"md5": md5(A), "level": "1"}, {"md5": md5(C), "level": "2"}],
    }
    monkeypatch.setattr(tables, "http_get", fake_web(pages))
    insane = client.post("/api/admin/tables", json={"url": "https://t.example/h.json"}).json()
    # 표가 있는 곡(A, C)은 표가 없는 곡(B)과 다른 청크에 들어갑니다.
    for name, chart in (("a.zip", A), ("b.zip", B), ("c.zip", C)):
        upload(client, name, make_zip({"x.bms": chart, "banner.png": b"png"}))
    assert client.get("/api/pre/tables").json()["chunks"] == {"0": [insane["id"]]}
    with db_module.connect() as con, con.cursor() as cur:
        cur.execute("SELECT id, pre_chunk FROM song ORDER BY id")
        assert [tuple(r) for r in cur.fetchall()] == [(1, 0), (2, 1), (3, 0)]
        cur.execute("SELECT id, sig FROM pre_chunk ORDER BY id")
        assert [tuple(r) for r in cur.fetchall()] == [(0, str(insane["id"])), (1, "")]
    assert db_module.Database().rebalance_pre_chunks() == 0
