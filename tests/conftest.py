import re
import sqlite3
import pymysql
import pytest

class SQLiteCursor:
    def __init__(self, raw_cur):
        self._cur = raw_cur
        self.rowcount = -1
        self.lastrowid = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def execute(self, query, args=None):
        sql = query

        # DDL conversions
        sql = re.sub(r"ENGINE=\w+", "", sql, flags=re.IGNORECASE)
        sql = re.sub(r"DEFAULT CHARSET=\w+", "", sql, flags=re.IGNORECASE)
        sql = re.sub(r"COLLATE=\w+", "", sql, flags=re.IGNORECASE)
        sql = re.sub(r"CHARACTER SET \w+", "", sql, flags=re.IGNORECASE)
        sql = re.sub(r"COLLATE \w+", "", sql, flags=re.IGNORECASE)
        sql = re.sub(r"AUTO_INCREMENT", "AUTOINCREMENT", sql, flags=re.IGNORECASE)
        sql = re.sub(r"INT UNSIGNED NOT NULL AUTOINCREMENT", "INTEGER PRIMARY KEY AUTOINCREMENT", sql, flags=re.IGNORECASE)
        sql = re.sub(r"INT UNSIGNED", "INTEGER", sql, flags=re.IGNORECASE)
        sql = re.sub(r"BIGINT UNSIGNED", "INTEGER", sql, flags=re.IGNORECASE)
        sql = re.sub(r"LONGBLOB", "BLOB", sql, flags=re.IGNORECASE)
        sql = re.sub(r"MEDIUMTEXT", "TEXT", sql, flags=re.IGNORECASE)
        sql = re.sub(r"CHAR\(64\)", "TEXT", sql, flags=re.IGNORECASE)
        sql = re.sub(r"VARCHAR\(\d+\)", "TEXT", sql, flags=re.IGNORECASE)
        if "PRIMARY KEY AUTOINCREMENT" in sql:
            sql = re.sub(r"PRIMARY KEY\s*\(\s*id\s*\)\s*,?", "", sql, flags=re.IGNORECASE)
        if "CREATE TABLE IF NOT EXISTS chart_chunk" in sql:
            sql = sql.replace("id INTEGER NOT NULL", "id INTEGER PRIMARY KEY")
            sql = re.sub(r"PRIMARY KEY\s*\(\s*id\s*\)", "", sql, flags=re.IGNORECASE)
        if "CREATE TABLE IF NOT EXISTS manifest_chunk" in sql:
            sql = sql.replace("id INTEGER NOT NULL", "id INTEGER PRIMARY KEY")
            sql = re.sub(r"PRIMARY KEY\s*\(\s*id\s*\)", "", sql, flags=re.IGNORECASE)

        sql = re.sub(r"FOR UPDATE", "", sql, flags=re.IGNORECASE)
        if "START TRANSACTION" in sql or "BEGIN" in sql:
            return

        sql = re.sub(r"SUBSTRING\(", "SUBSTR(", sql, flags=re.IGNORECASE)
        sql = re.sub(r",\s*\)", "\n)", sql)

        if "SELECT @@max_allowed_packet" in sql:
            self._cur.execute("SELECT 1073741824")
            return

        if "information_schema.COLUMNS" in sql:
            table_name, col_name = args[0], args[1]
            try:
                self._cur.execute(f"PRAGMA table_info({table_name})")
                cols = [r[1] for r in self._cur.fetchall()]
                count = 1 if col_name in cols else 0
            except sqlite3.OperationalError:
                count = 0
            self._cur.execute(f"SELECT {count}")
            return

        if "ALTER TABLE song DROP COLUMN folder, DROP COLUMN files" in sql:
            self._cur.execute("ALTER TABLE song DROP COLUMN folder")
            self._cur.execute("ALTER TABLE song DROP COLUMN files")
            return

        if "ON DUPLICATE KEY UPDATE" in sql:
            sql = re.sub(r"ON DUPLICATE KEY UPDATE", "ON CONFLICT(id) DO UPDATE SET", sql, flags=re.IGNORECASE)
            sql = re.sub(r"VALUES\((\w+)\)", r"excluded.\1", sql, flags=re.IGNORECASE)

        sql = re.sub(r"INSERT IGNORE INTO", "INSERT OR IGNORE INTO", sql, flags=re.IGNORECASE)

        sql = sql.replace("%s", "?")
        if args is None:
            args = ()

        self._cur.execute(sql, args)
        self.rowcount = self._cur.rowcount
        self.lastrowid = self._cur.lastrowid

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return tuple(self._cur.fetchall())

    def close(self):
        self._cur.close()

class SQLiteConnection:
    _dbs = {}

    @classmethod
    def get_db(cls, dbname):
        if dbname not in cls._dbs:
            con = sqlite3.connect(":memory:", check_same_thread=False)
            con.isolation_level = None
            con.create_function("IF", 3, lambda cond, t, f: t if cond else f)
            con.create_function("LENGTH", 1, lambda val: len(val) if val is not None else 0)
            cls._dbs[dbname] = con
        return cls._dbs[dbname]

    @classmethod
    def reset_db(cls, dbname):
        if dbname in cls._dbs:
            try:
                cls._dbs[dbname].close()
            except Exception:
                pass
            del cls._dbs[dbname]

    def __init__(self, database="default"):
        self.database = database or "default"
        self._con = self.get_db(self.database)
        self.open = True

    def cursor(self):
        return SQLiteCursor(self._con.cursor())

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        self.open = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

@pytest.fixture(autouse=True)
def mock_pymysql_if_no_mysql(monkeypatch, tmp_path):
    import ebms_server.db as db_module
    import ebms_server.constant as constant
    monkeypatch.setattr(constant, "TMP_DIR", tmp_path / "var_tmp")
    constant.TMP_DIR.mkdir(parents=True, exist_ok=True)
    original_connect = db_module.connect
    try:
        con = original_connect(database=None)
        con.close()
    except pymysql.err.OperationalError:
        db_module.Database._instance = None
        db_module.Database._initialized = False
        def fake_connect(**kwargs):
            db = kwargs.get("database", constant.DB_NAME)
            if db is None:
                db = constant.DB_NAME
            return SQLiteConnection(database=db)
        monkeypatch.setattr(db_module, "connect", fake_connect)

        # Handle DROP DATABASE / CREATE DATABASE in test setup
        orig_cursor = SQLiteConnection.cursor
        def cursor_with_db_ops(self):
            cur = orig_cursor(self)
            orig_exec = cur.execute
            def exec_with_db_ops(query, args=None):
                if "DROP DATABASE IF EXISTS" in query:
                    m = re.search(r"DATABASE IF EXISTS (\w+)", query, flags=re.IGNORECASE)
                    if m:
                        SQLiteConnection.reset_db(m.group(1))
                    return
                if "CREATE DATABASE" in query:
                    return
                orig_exec(query, args)
            cur.execute = exec_with_db_ops
            return cur
        monkeypatch.setattr(SQLiteConnection, "cursor", cursor_with_db_ops)
