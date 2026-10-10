"""S3 호환 스토리지 캐시(s3cache) 테스트. 버킷은 httpx.MockTransport로 흉내 냅니다."""
import datetime
import threading
import time
import urllib.parse

import httpx
import pytest

from ebms_server import db, s3cache
from ebms_server.db import Database
from ebms_server.s3cache import S3Cache, S3Client

from test_blob_storage import client, database, make_song, sha  # noqa: F401


def test_presign_matches_aws_example():
    # https://docs.aws.amazon.com/AmazonS3/latest/API/sigv4-query-string-auth.html 예시
    c = S3Client(
        "https://examplebucket.s3.amazonaws.com", "examplebucket",
        "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "us-east-1",
    )
    qs = c.presign_path("GET", "/test.txt", 86400, datetime.datetime(2013, 5, 24, tzinfo=datetime.UTC))
    assert qs.endswith("X-Amz-Signature=aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404")
    assert "X-Amz-Credential=AKIAIOSFODNN7EXAMPLE%2F20130524%2Fus-east-1%2Fs3%2Faws4_request" in qs


class FakeBucket:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.puts: list[str] = []
        self.gate = threading.Event()
        self.gate.set()

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"].startswith("AWS4-HMAC-SHA256 Credential=ak/")
        key = urllib.parse.unquote(request.url.path).removeprefix("/bucket/")
        if request.method == "PUT":
            self.gate.wait(5)
            body = request.read()
            assert int(request.headers["content-length"]) == len(body)
            self.objects[key] = body
            self.puts.append(key)
            return httpx.Response(200)
        if request.method == "DELETE":
            self.objects.pop(key, None)
            return httpx.Response(204)
        return httpx.Response(405)

    def get(self, url: str) -> bytes:
        return self.objects[urllib.parse.unquote(urllib.parse.urlsplit(url).path).removeprefix("/bucket/")]


@pytest.fixture
def bucket(monkeypatch):
    b = FakeBucket()
    client = S3Client("https://s3.example.com", "bucket", "ak", "sk", transport=httpx.MockTransport(b.handler))
    cache = S3Cache(client, cache_bytes=0, url_seconds=600, upload_wait=5)
    monkeypatch.setattr(s3cache, "_cache", cache)
    monkeypatch.setattr(s3cache, "enabled", lambda: True)
    b.cache = cache
    return b


def rows():
    with db.connect() as con, con.cursor() as cur:
        cur.execute("SELECT object_key, kind, row_id, size FROM s3_object ORDER BY object_key")
        return [tuple(r) for r in cur.fetchall()]


def test_play_redirects_to_bucket(tmp_path, client, bucket):
    Database().insert_song(make_song(tmp_path, "song1", {"a.bms": b"#TITLE A\n"}))
    data = Database().get_song_data(1)

    res = client.get("/api/play/song/1", follow_redirects=False)
    assert res.status_code == 302
    url = res.headers["location"]
    assert url.startswith(f"https://s3.example.com/bucket/song/1/{sha(data)}.zip?")
    assert "X-Amz-Signature=" in url
    assert res.headers["etag"] == f'"{sha(data)}"'
    assert res.headers["cache-control"] == "no-store"
    assert bucket.get(url) == data
    assert rows() == [(f"song/1/{sha(data)}.zip", "song", 1, len(data))]

    # 버킷에 있으면 다시 올리지 않습니다.
    assert client.get("/api/play/song/1", follow_redirects=False).status_code == 302
    assert bucket.puts == [f"song/1/{sha(data)}.zip"]

    # If-None-Match가 맞으면 리다이렉트 없이 304
    res = client.get("/api/play/song/1", headers={"If-None-Match": f'"{sha(data)}"'}, follow_redirects=False)
    assert res.status_code == 304
    assert client.get("/api/play/song/99", follow_redirects=False).status_code == 404


def test_chunks_redirect_and_replace_stale(tmp_path, client, bucket):
    Database().insert_song(make_song(tmp_path, "song1", {"a.bms": b"#TITLE A\n"}))
    Database().backfill_pre_chunks()
    for path in ("/api/pre/chart/0", "/api/pre/asset/0"):
        res = client.get(path, follow_redirects=False)
        assert res.status_code == 302, path
    old = {k for k, kind, *_ in rows() if kind == "chart"}

    Database().insert_song(make_song(tmp_path, "song2", {"b.bms": b"#TITLE B\n"}))
    res = client.get("/api/pre/chart/0", follow_redirects=False)
    assert res.status_code == 302
    chunk = bucket.get(res.headers["location"])
    assert sha(chunk) == client.get("/api/pre/charthash").json()["0"]
    # 이전 내용은 버킷과 테이블에서 지웁니다.
    assert [k for k, kind, *_ in rows() if kind == "chart"] == [f"chart/0/{sha(chunk)}.zip"]
    assert not old & bucket.objects.keys()


def test_slow_upload_returns_503_then_redirects(tmp_path, client, bucket):
    Database().insert_song(make_song(tmp_path, "song1", {"a.bms": b"#TITLE A\n"}))
    bucket.cache.upload_wait = 0.1
    bucket.gate.clear()
    res = client.get("/api/play/song/1", follow_redirects=False)
    assert res.status_code == 503
    assert res.headers["retry-after"] == "5"

    bucket.gate.set()
    bucket.cache.upload_wait = 5
    res = client.get("/api/play/song/1", follow_redirects=False)
    assert res.status_code == 302
    assert len(bucket.puts) == 1
    # 503 뒤의 재요청은 grant 안이라 티켓을 한 번만 씁니다.
    assert client.get("/api/me").json()["tickets"]["available"] == 4


def test_rolling_eviction(tmp_path, client, bucket):
    for i in range(3):
        Database().insert_song(make_song(tmp_path, f"song{i}", {f"{i}.bms": f"#TITLE {i}\n".encode()}))
    Database().backfill_pre_chunks()
    client.get("/api/pre/chart/0", follow_redirects=False)
    sizes = [Database().open_blob("song", i).size for i in (1, 2, 3)]
    # 곡 2개까지만 들어가는 크기(청크는 세지 않음)
    bucket.cache.cache_bytes = sizes[0] + sizes[1] + sizes[2] // 2

    for i in (1, 2):
        assert client.get(f"/api/play/song/{i}", follow_redirects=False).status_code == 302
    # URL 유효시간이 지난 것처럼 만들고, 곡 1을 곡 2보다 오래전에 쓴 것으로 둡니다.
    old = int(time.time()) - 3600
    with db.connect() as con, con.cursor() as cur:
        cur.execute("UPDATE s3_object SET last_access = %s WHERE kind = 'song' AND row_id = 1", (old - 10,))
        cur.execute("UPDATE s3_object SET last_access = %s WHERE kind = 'song' AND row_id = 2", (old,))
        cur.execute("UPDATE s3_object SET last_access = %s WHERE kind = 'chart'", (old - 100,))
        con.commit()

    assert client.get("/api/play/song/3", follow_redirects=False).status_code == 302
    kept = {(kind, row_id) for _, kind, row_id, _ in rows()}
    assert kept == {("chart", 0), ("song", 2), ("song", 3)}
    assert not any(k.startswith("song/1/") for k in bucket.objects)


def test_recently_issued_url_is_not_evicted(tmp_path, client, bucket):
    for i in range(2):
        Database().insert_song(make_song(tmp_path, f"song{i}", {f"{i}.bms": f"#TITLE {i}\n".encode()}))
    bucket.cache.cache_bytes = 1
    for i in (1, 2):
        assert client.get(f"/api/play/song/{i}", follow_redirects=False).status_code == 302
    # 둘 다 방금 URL을 줬으므로 한도를 넘어도 남깁니다.
    assert {row_id for _, _, row_id, _ in rows()} == {1, 2}
