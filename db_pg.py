"""PostgreSQL (Neon) adapter.

Lets the older SQLite-style route modules (community.py, guild_routes.py, game_engine.py ...)
keep calling ``conn.execute("... ? ...", params)`` while talking to Postgres.

What it does for them:
  * ``?`` -> ``%s`` and literal ``%`` -> ``%%`` (so ``LIKE 'dm:%'`` keeps working)
  * INSERT OR IGNORE -> ON CONFLICT DO NOTHING
  * INTEGER PRIMARY KEY AUTOINCREMENT -> BIGSERIAL, BLOB -> BYTEA, REAL -> DOUBLE PRECISION,
    COLLATE NOCASE removed (CREATE TABLE only)
  * ``cursor.lastrowid`` (INSERT ... RETURNING id for the known serial tables)
  * rows that work as both ``row['col']`` and ``row[0]``
  * PRAGMA table_info(...) and other PRAGMAs
  * a small thread-safe connection pool (a fresh SSL connection to Neon costs 100-300 ms)
"""
import os
import re
import time
import logging
import threading
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import psycopg2
from psycopg2.pool import ThreadedConnectionPool
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger(__name__)

_raw_url = os.getenv("DATABASE_URL") or os.getenv("NEON_DATABASE_URL")
if not _raw_url:
    raise ValueError("DATABASE_URL (Neon connection string) is missing.")


def _clean_url(url):
    # channel_binding=require breaks with some libpq builds; sslmode is passed separately below.
    p = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(p.query) if k != "channel_binding"]
    return urlunsplit(p._replace(query=urlencode(q)))


DATABASE_URL = _clean_url(_raw_url)

# Tables whose `id` column is a serial: INSERTs into them get "RETURNING id" so cursor.lastrowid works.
ID_TABLES = {
    "activity_log", "announcements", "auctions", "guild_messages",
    "community_messages", "community_message_edits", "community_gift_log",
    "community_emojis", "community_uploads", "community_reports", "community_admin_log",
}

_INSERT_RE = re.compile(r"\s*INSERT\s+(?:OR\s+\w+\s+)?INTO\s+(\w+)", re.I)
_PRAGMA_TABLE_INFO = re.compile(r"\s*PRAGMA\s+table_info\s*\(\s*(\w+)\s*\)\s*;?\s*$", re.I)


def translate_sql(sql, has_params):
    """SQLite-flavoured SQL -> PostgreSQL. Returns (sql, wants_returning_id)."""
    upper = sql.upper()
    ignore = "INSERT OR IGNORE" in upper
    sql = re.sub(r"INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT", "BIGSERIAL PRIMARY KEY", sql, flags=re.I)
    if re.match(r"\s*CREATE\s+TABLE", sql, re.I):
        sql = re.sub(r"\s+COLLATE\s+NOCASE", "", sql, flags=re.I)
        sql = re.sub(r"\bBLOB\b", "BYTEA", sql, flags=re.I)
        sql = re.sub(r"\bREAL\b", "DOUBLE PRECISION", sql, flags=re.I)
    sql = re.sub(r"INSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", sql, flags=re.I)
    if ignore and "ON CONFLICT" not in sql.upper():
        sql = sql.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    m = _INSERT_RE.match(sql)
    wants_id = bool(m and m.group(1).lower() in ID_TABLES and "RETURNING" not in sql.upper())
    if wants_id:
        sql = sql.rstrip().rstrip(";") + " RETURNING id"
    if has_params:
        # literal % must be doubled once psycopg2 does parameter substitution
        sql = sql.replace("%", "%%").replace("?", "%s")
    return sql, wants_id


class Row(dict):
    """dict that also supports positional access: row['name'] and row[0] both work."""
    __slots__ = ("_vals",)

    def __init__(self, names, values):
        super().__init__(zip(names, values))
        self._vals = tuple(values)

    def __getitem__(self, k):
        if isinstance(k, (int, slice)):
            return self._vals[k]
        return dict.__getitem__(self, k)


class PGCursor:
    def __init__(self, cursor):
        self._cursor = cursor
        self._compat_rows = None
        self.lastrowid = None
        self.rowcount = 0

    def _names(self):
        d = self._cursor.description
        return [c[0] for c in d] if d else []

    def execute(self, sql, params=()):
        self.lastrowid = None
        m = _PRAGMA_TABLE_INFO.match(sql)
        if m:
            self._cursor.execute(
                "SELECT column_name, data_type, is_nullable, column_default "
                "FROM information_schema.columns WHERE table_schema = current_schema() "
                "AND table_name = %s ORDER BY ordinal_position", (m.group(1),))
            # SQLite layout: cid, name, type, notnull, dflt_value, pk
            self._compat_rows = [
                (i, r[0], r[1], 0 if r[2] == "YES" else 1, r[3], 0)
                for i, r in enumerate(self._cursor.fetchall())
            ]
            return self
        if re.match(r"\s*PRAGMA\b", sql, re.I):      # connection tuning has no Postgres equivalent
            self._compat_rows = []
            return self
        self._compat_rows = None
        params = tuple(params) if params else ()
        sql, wants_id = translate_sql(sql, bool(params))
        if params:
            self._cursor.execute(sql, params)
        else:
            self._cursor.execute(sql)                 # no substitution: '%' stays literal
        if wants_id:
            r = self._cursor.fetchone()
            self.lastrowid = r[0] if r else None
        self.rowcount = self._cursor.rowcount
        return self

    def fetchone(self):
        if self._compat_rows is not None:
            return self._compat_rows.pop(0) if self._compat_rows else None
        v = self._cursor.fetchone()
        return None if v is None else Row(self._names(), v)

    def fetchall(self):
        if self._compat_rows is not None:
            rows, self._compat_rows = self._compat_rows, []
            return rows
        names = self._names()
        return [Row(names, v) for v in self._cursor.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())


# ── pool ────────────────────────────────────────────────────────────────────
POOL_MAX = int(os.getenv("DB_POOL_MAX", "8"))
_pool = None
_pool_lock = threading.Lock()
_slots = threading.BoundedSemaphore(POOL_MAX)
_last_used = {}


def _get_pool():
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ThreadedConnectionPool(
                1, POOL_MAX, DATABASE_URL, sslmode=os.getenv("DB_SSLMODE", "require"), connect_timeout=10,
                keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=3)
        return _pool


def _discard(pool, c):
    try:
        pool.putconn(c, close=True)
    except Exception:
        pass
    _last_used.pop(id(c), None)


def _checkout():
    pool = _get_pool()
    for _ in range(3):
        c = pool.getconn()
        try:
            if c.closed:
                raise psycopg2.InterfaceError("closed")
            if time.time() - _last_used.get(id(c), 0) > 20:   # Neon drops idle connections; probe stale ones
                with c.cursor() as cur:
                    cur.execute("SELECT 1")
                c.rollback()
            return c
        except psycopg2.Error:
            _discard(pool, c)
    raise psycopg2.OperationalError("could not obtain a database connection")


class PGConnection:
    def __init__(self):
        if not _slots.acquire(timeout=15):
            raise psycopg2.OperationalError("database connection pool exhausted")
        try:
            self.raw = _checkout()
        except BaseException:
            _slots.release()
            raise
        self._done = False

    def cursor(self):
        return PGCursor(self.raw.cursor())

    def execute(self, sql, params=()):
        return self.cursor().execute(sql, params)

    def commit(self):
        self.raw.commit()

    def rollback(self):
        try:
            self.raw.rollback()
        except Exception:
            pass

    def close(self):
        """Returns the connection to the pool. Anything not committed is discarded."""
        if self._done:
            return
        self._done = True
        try:
            pool = _get_pool()
            if self.raw.closed:
                _discard(pool, self.raw)
            else:
                self.raw.rollback()
                _last_used[id(self.raw)] = time.time()
                pool.putconn(self.raw)
        except Exception:
            log.debug("[db] could not return connection to pool", exc_info=True)
        finally:
            _slots.release()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type:
            self.rollback()
        else:
            self.commit()
        return False


def fix_sequences(conn, tables=None):
    """After copying rows from SQLite, serial sequences still start at 1. Move them past MAX(id). Idempotent."""
    for t in (tables or sorted(ID_TABLES)):
        try:
            conn.raw.cursor().execute(
                "SELECT setval(pg_get_serial_sequence(%s, 'id'), "
                "COALESCE((SELECT MAX(id) FROM " + t + "), 0) + 1, false)", (t,))
        except psycopg2.Error:
            conn.rollback()          # table/column/sequence not there: nothing to fix
    conn.commit()
