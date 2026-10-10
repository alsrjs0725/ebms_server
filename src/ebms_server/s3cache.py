"""S3 호환 스토리지(R2·MinIO·B2·AWS 등)를 다운로드 앞단 캐시로 씁니다. EBMS_S3_BUCKET을 주면 켜집니다.

원본은 MySQL에 그대로 두고, 곡 zip·차트 청크·사전 청크를 버킷에 올린 뒤 presigned URL로 302 리다이렉트합니다.
그래서 큰 파일은 서버(와 앞단 CDN 프록시)를 거치지 않고 버킷에서 바로 나갑니다.

- 키는 내용 주소입니다: {종류}/{id}/{sha256}.zip. 내용이 바뀌면 새 키로 올리고 이전 키는 지웁니다.
- 버킷에 없으면 백그라운드로 올리고 S3_UPLOAD_WAIT_SECONDS까지 기다립니다. 그 안에 못 올리면 None(호출자가 503).
- S3_CACHE_BYTES > 0이면 곡 zip 합이 넘을 때 오래 안 쓴 것부터 지웁니다(롤링 캐시). 차트·사전 청크는 한도에 세지 않고 항상 둡니다.
- 버킷에 올린 객체는 s3_object 테이블에 적어 둡니다. 서버 프로세스 1개(워커 1개)를 전제로 합니다.
"""
import concurrent.futures
import datetime
import hashlib
import hmac
import logging
import threading
import time
import urllib.parse

import httpx

from . import constant, db
from .db import BlobReader, Database

logger = logging.getLogger(__name__)

# 테이블 → 키 접두사. song만 롤링 대상입니다.
PREFIXES = {"song": "song", "chart_chunk": "chart", "pre_chunk": "pre"}
ROLLING_KIND = "song"
# 같은 객체를 받을 때 last_access를 다시 쓰는 최소 간격(초)
TOUCH_SECONDS = 60


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def _quote(s: str, safe: str = "-_.~") -> str:
    return urllib.parse.quote(s, safe=safe)


class S3Client:
    """S3 API 중 필요한 것(PUT·DELETE·presigned GET)만 SigV4로 부르는 최소 클라이언트. 경로 방식 URL을 씁니다."""

    def __init__(self, endpoint: str, bucket: str, access_key: str, secret_key: str, region: str = "auto",
                 transport: httpx.BaseTransport | None = None):
        parts = urllib.parse.urlsplit(endpoint)
        self.scheme = parts.scheme or "https"
        self.host = parts.netloc
        self.bucket = bucket
        self.access_key = access_key
        self.secret_key = secret_key
        self.region = region
        self.http = httpx.Client(transport=transport, timeout=httpx.Timeout(60.0, connect=10.0))

    def _path(self, key: str) -> str:
        return f"/{_quote(self.bucket)}/{_quote(key, '/-_.~')}"

    def _scope(self, now: datetime.datetime) -> tuple[str, str, str]:
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date = amz_date[:8]
        return amz_date, date, f"{date}/{self.region}/s3/aws4_request"

    def _signature(self, date: str, scope: str, amz_date: str, canonical: str) -> str:
        to_sign = "\n".join(
            ["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()]
        )
        k = _sign(("AWS4" + self.secret_key).encode(), date)
        for part in (self.region, "s3", "aws4_request"):
            k = _sign(k, part)
        return hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()

    def presign_path(self, method: str, path: str, expires: int, now: datetime.datetime | None = None) -> str:
        """path(이미 인코딩됨)에 대한 서명된 쿼리 문자열을 돌려줍니다."""
        amz_date, date, scope = self._scope(now or datetime.datetime.now(datetime.UTC))
        query = {
            "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
            "X-Amz-Credential": f"{self.access_key}/{scope}",
            "X-Amz-Date": amz_date,
            "X-Amz-Expires": str(expires),
            "X-Amz-SignedHeaders": "host",
        }
        qs = "&".join(f"{_quote(k)}={_quote(v)}" for k, v in sorted(query.items()))
        canonical = "\n".join([method, path, qs, f"host:{self.host}", "", "host", "UNSIGNED-PAYLOAD"])
        return f"{qs}&X-Amz-Signature={self._signature(date, scope, amz_date, canonical)}"

    def presign_get(self, key: str, expires: int) -> str:
        path = self._path(key)
        return f"{self.scheme}://{self.host}{path}?{self.presign_path('GET', path, expires)}"

    def _request(self, method: str, key: str, headers: dict | None = None, content=None) -> httpx.Response:
        path = self._path(key)
        amz_date, date, scope = self._scope(datetime.datetime.now(datetime.UTC))
        signed = {"host": self.host, "x-amz-content-sha256": "UNSIGNED-PAYLOAD", "x-amz-date": amz_date}
        names = ";".join(sorted(signed))
        canonical = "\n".join(
            [method, path, "", *(f"{k}:{signed[k]}" for k in sorted(signed)), "", names, "UNSIGNED-PAYLOAD"]
        )
        auth = (
            f"AWS4-HMAC-SHA256 Credential={self.access_key}/{scope}, SignedHeaders={names}, "
            f"Signature={self._signature(date, scope, amz_date, canonical)}"
        )
        all_headers = {**signed, "authorization": auth, **(headers or {})}
        del all_headers["host"]
        resp = self.http.request(method, f"{self.scheme}://{self.host}{path}", headers=all_headers, content=content)
        if resp.status_code >= 300 and not (method == "DELETE" and resp.status_code == 404):
            raise RuntimeError(f"S3 {method} {key} failed: {resp.status_code} {resp.text[:200]}")
        return resp

    def put(self, key: str, chunks, size: int, content_type: str = "application/zip") -> None:
        self._request("PUT", key, {"content-length": str(size), "content-type": content_type}, chunks)

    def delete(self, key: str) -> None:
        self._request("DELETE", key)


class S3Cache:
    def __init__(self, client: S3Client, cache_bytes: int = 0, url_seconds: int = 600,
                 upload_wait: float = 20.0, upload_threads: int = 2):
        self.client = client
        self.cache_bytes = cache_bytes
        self.url_seconds = url_seconds
        self.upload_wait = upload_wait
        self._executor = concurrent.futures.ThreadPoolExecutor(upload_threads, thread_name_prefix="s3_upload")
        self._pending: dict[str, concurrent.futures.Future] = {}
        self._lock = threading.Lock()
        self._evict_lock = threading.Lock()

    @staticmethod
    def key(table: str, row_id: int, sha256: str) -> str:
        return f"{PREFIXES[table]}/{row_id}/{sha256}.zip"

    def url(self, table: str, blob: BlobReader, wait: float | None = None) -> str | None:
        """blob을 버킷에서 받을 presigned URL. 버킷에 없으면 올리고, wait초 안에 못 올리면 None."""
        key = self.key(table, blob.row_id, blob.sha256)
        if not self._touch(key):
            with self._lock:
                fut = self._pending.get(key)
                if fut is None:
                    fut = self._executor.submit(self._upload, table, blob.row_id, blob.size, blob.sha256, key)
                    self._pending[key] = fut
                    fut.add_done_callback(lambda _f, k=key: self._forget(k))
            try:
                fut.result(timeout=self.upload_wait if wait is None else wait)
            except concurrent.futures.TimeoutError:
                return None
        return self.client.presign_get(key, self.url_seconds)

    def _forget(self, key: str) -> None:
        with self._lock:
            self._pending.pop(key, None)

    def _touch(self, key: str) -> bool:
        """버킷에 있으면 last_access를 갱신하고 True."""
        now = int(time.time())
        with db.connect() as con, con.cursor() as cur:
            cur.execute("SELECT last_access FROM s3_object WHERE object_key = %s", (key,))
            row = cur.fetchone()
            if row is None:
                return False
            if now - int(row[0]) >= TOUCH_SECONDS:
                cur.execute("UPDATE s3_object SET last_access = %s WHERE object_key = %s", (now, key))
                con.commit()
        return True

    def _upload(self, table: str, row_id: int, size: int, sha256: str, key: str) -> None:
        if self._touch(key):
            return
        started = time.monotonic()
        blob = BlobReader(table, row_id, size, sha256)
        self.client.put(key, blob.iter_range(0, size - 1) if size else iter([b""]), size)
        now = int(time.time())
        kind = PREFIXES[table]
        with db.connect() as con, con.cursor() as cur:
            cur.execute(
                "INSERT INTO s3_object (object_key, kind, row_id, size, last_access) VALUES (%s, %s, %s, %s, %s)",
                (key, kind, row_id, size, now),
            )
            # 같은 행의 이전 내용(곡 병합, 청크 재작성)은 더 쓰지 않으므로 지웁니다.
            cur.execute(
                "SELECT object_key FROM s3_object WHERE kind = %s AND row_id = %s AND object_key <> %s",
                (kind, row_id, key),
            )
            stale = [r[0] for r in cur.fetchall()]
            con.commit()
        logger.info(f"s3 uploaded {key} ({size} bytes, {time.monotonic() - started:.1f}s)")
        for old in stale:
            self._delete(old)
        self.evict()

    def _delete(self, key: str) -> None:
        self.client.delete(key)
        with db.connect() as con, con.cursor() as cur:
            cur.execute("DELETE FROM s3_object WHERE object_key = %s", (key,))
            con.commit()
        logger.info(f"s3 deleted {key}")

    def evict(self) -> int:
        """곡 zip 합이 S3_CACHE_BYTES를 넘으면 오래 안 쓴 것부터 지웁니다. URL 유효시간 안에 받은 것은 남깁니다.

        차트·사전 청크는 이 한도에 세지 않습니다(항상 둠).

        지울 것이 없으면 잠시 넘은 채로 둡니다(무료 한도는 월평균 용량 기준). 지운 개수를 돌려줍니다.
        """
        if self.cache_bytes <= 0:
            return 0
        removed = 0
        with self._evict_lock:
            with db.connect() as con, con.cursor() as cur:
                cur.execute("SELECT COALESCE(SUM(size), 0) FROM s3_object WHERE kind = %s", (ROLLING_KIND,))
                total = int(cur.fetchone()[0])
                if total <= self.cache_bytes:
                    return 0
                cur.execute(
                    "SELECT object_key, size FROM s3_object WHERE kind = %s AND last_access < %s "
                    "ORDER BY last_access, object_key",
                    (ROLLING_KIND, int(time.time()) - self.url_seconds),
                )
                candidates = cur.fetchall()
            for key, size in candidates:
                if total <= self.cache_bytes:
                    break
                with self._lock:
                    if key in self._pending:
                        continue
                try:
                    self._delete(key)
                except Exception:
                    logger.exception(f"s3 evict {key} failed")
                    continue
                total -= int(size)
                removed += 1
        return removed

    def prewarm(self) -> None:
        """차트·사전 청크를 모두 버킷에 올려 둡니다(서버 시작 시 백그라운드)."""
        for table in ("chart_chunk", "pre_chunk"):
            with db.connect() as con, con.cursor() as cur:
                cur.execute(f"SELECT id FROM {table} ORDER BY id")
                ids = [r[0] for r in cur.fetchall()]
            for row_id in ids:
                blob = Database().open_blob(table, row_id)
                if blob is None:
                    continue
                try:
                    self.url(table, blob, wait=3600)
                except Exception:
                    logger.exception(f"s3 prewarm {table} {row_id} failed")


_cache: S3Cache | None = None
_cache_lock = threading.Lock()


def enabled() -> bool:
    return bool(constant.S3_BUCKET)


def get() -> S3Cache | None:
    """설정돼 있으면 S3Cache, 아니면 None."""
    global _cache
    if not enabled():
        return None
    with _cache_lock:
        if _cache is None:
            client = S3Client(
                constant.S3_ENDPOINT, constant.S3_BUCKET, constant.S3_ACCESS_KEY_ID,
                constant.S3_SECRET_ACCESS_KEY, constant.S3_REGION,
            )
            _cache = S3Cache(
                client, constant.S3_CACHE_BYTES, constant.S3_URL_SECONDS,
                constant.S3_UPLOAD_WAIT_SECONDS, constant.S3_UPLOAD_THREADS,
            )
        return _cache
