"""난이도표 읽기(beatoraja 방식), 관리자 등록, 사전 청크 태그 테스트."""
import hashlib
import json

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

    # 곡 1(A)·2(B)·3(C), 청크 = song_id // 2 → 곡 1은 청크 0, 곡 3은 청크 1
    pre = client.get("/api/pre/tables").json()
    assert pre["chunks"] == {"0": [insane["id"], other["id"]], "1": [other["id"]]}
    assert [t["name"] for t in pre["tables"]] == ["Insane", "Other"]
    assert pre["tables"][0]["songs"] == 1

    # 다시 읽기: 표가 바뀌면 차트 목록을 바꾸고, 읽지 못하면 이전 목록을 두고 오류만 남깁니다.
    pages["https://t.example/d.json"] = [{"md5": md5(B), "level": "1"}]
    r = client.post(f"/api/admin/tables/{insane['id']}/refresh").json()
    assert r["charts"] == 1 and r["error"] is None
    assert client.get("/api/pre/tables").json()["chunks"] == {"0": [other["id"]], "1": [insane["id"], other["id"]]}
    del pages["https://t.example/d.json"]
    r = client.post(f"/api/admin/tables/{insane['id']}/refresh").json()
    assert r["charts"] == 1 and "404" in r["error"]
    assert client.get("/api/pre/tables").json()["chunks"]["1"] == [insane["id"], other["id"]]

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
