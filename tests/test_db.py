import io
import threading
import zipfile

import pytest

from ebms_server import db as db_module
from ebms_server.db import ConnectionPool, zip_entries


def test_zip_entries_empty_zip():
    # Create an empty zip file in memory
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w"):
        pass

    entries = zip_entries(buf.getvalue())
    assert entries == []


def test_zip_entries_invalid_zip():
    with pytest.raises(zipfile.BadZipFile):
        zip_entries(b"not a zip file")


def test_zip_entries_empty_bytes():
    with pytest.raises(zipfile.BadZipFile):
        zip_entries(b"")


# ---- 연결 풀 (#40) ----

class FakeConnection:
    created = []

    def __init__(self, **params):
        self.params = params
        self.open = True
        self.alive = True
        self.rollbacks = 0
        self.pings = 0
        FakeConnection.created.append(self)

    def ping(self, reconnect=True):
        self.pings += 1
        if not self.alive:
            raise ConnectionError("gone")

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.open = False


@pytest.fixture
def fake_pymysql(monkeypatch):
    FakeConnection.created = []
    monkeypatch.setattr(db_module.pymysql, "connect", lambda **params: FakeConnection(**params))
    return FakeConnection


def test_pool_reuses_connection_and_rolls_back(fake_pymysql):
    pool = ConnectionPool(size=2)
    with pool.connect({"database": "a"}) as con:
        first = con
    assert first.rollbacks == 1 and first.open
    with pool.connect({"database": "a"}) as con:
        assert con is first and con.pings == 1
    # 예외로 빠져나가도 rollback 후 돌려받음
    with pytest.raises(RuntimeError):
        with pool.connect({"database": "a"}) as con:
            raise RuntimeError
    assert first.rollbacks == 3
    # 접속 정보(데이터베이스)가 다르면 다른 연결
    with pool.connect({"database": "b"}) as con:
        assert con is not first
    assert len(fake_pymysql.created) == 2


def test_pool_close_returns_once(fake_pymysql):
    pool = ConnectionPool(size=2)
    wrapper = pool.connect({})
    wrapper.close()
    wrapper.close()
    with wrapper:
        pass
    assert fake_pymysql.created[0].rollbacks == 1
    # 같은 연결이 풀에 두 번 들어가지 않음
    a, b = pool.connect({}), pool.connect({})
    assert a._con is not b._con


def test_pool_discards_dead_and_caps_idle(fake_pymysql):
    pool = ConnectionPool(size=2)
    wrappers = [pool.connect({}) for _ in range(3)]
    for w in wrappers:
        w.close()
    assert [c.open for c in fake_pymysql.created] == [True, True, False]
    fake_pymysql.created[1].alive = False
    with pool.connect({}) as con:
        # 마지막에 돌려준 연결이 끊겨 있어 버리고 다음 연결을 씀
        assert con is fake_pymysql.created[0]
    assert not fake_pymysql.created[1].open
    # rollback이 실패한 연결은 남기지 않음
    w = pool.connect({})
    w._con.rollback = lambda: (_ for _ in ()).throw(ConnectionError("gone"))
    w.close()
    assert not w._con.open
    with pool.connect({}) as con:
        assert con is not w._con


def test_pool_thread_safe(fake_pymysql):
    pool = ConnectionPool(size=4)
    in_use = set()
    lock = threading.Lock()
    errors = []

    def work():
        for _ in range(200):
            with pool.connect({}) as con:
                with lock:
                    if id(con) in in_use:
                        errors.append("shared")
                    in_use.add(id(con))
                with lock:
                    in_use.discard(id(con))

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert sum(c.open for c in fake_pymysql.created) <= 4
