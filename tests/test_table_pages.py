"""공유용 난이도표 정적 페이지(/table/{표 이름}.html)."""
from fastapi.testclient import TestClient

from ebms_server import constant, main


def test_table_page_served(tmp_path, monkeypatch):
    (tmp_path / "sample.html").write_text('<meta name="bmstable" content="sample/header.json">', encoding="utf-8")
    (tmp_path / "sample").mkdir()
    (tmp_path / "sample" / "header.json").write_text('{"name": "sample", "symbol": "s", "data_url": "data.json"}')
    route = next(r for r in main.app.routes if getattr(r, "name", None) == "table")
    monkeypatch.setattr(route.app, "directory", tmp_path)
    monkeypatch.setattr(route.app, "all_directories", [tmp_path])
    client = TestClient(main.app)

    r = client.get("/table/sample.html")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert 'name="bmstable"' in r.text
    assert r.headers["access-control-allow-origin"] == "*"
    assert r.headers["content-security-policy"] == main.SECURITY_HEADERS["Content-Security-Policy"]

    r = client.get("/table/sample/header.json")
    assert r.status_code == 200
    assert r.json()["name"] == "sample"

    assert client.get("/table/nope.html").status_code == 404
    assert client.get("/table/../main.py").status_code == 404


def test_table_dir_exists():
    assert constant.TABLE_PAGE_DIR.is_dir()


def test_shared_tables_parse():
    """폴더의 난이도표가 beatoraja 방식(tables.parse_table)으로 읽히는지."""
    from ebms_server import tables

    client = TestClient(main.app)

    def get(url):
        r = client.get(url)
        assert r.status_code == 200, url
        return r.content

    for page in constant.TABLE_PAGE_DIR.glob("*.html"):
        table = tables.parse_table(f"http://testserver/table/{page.name}", get=get)
        assert table["charts"], page.name
