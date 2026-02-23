"""PostgreSQL/TimescaleDB storage layer for Twitch analytics (Windows Tresor friendly).

- DSN lookup order: env TWITCH_ANALYTICS_DSN, then Windows Credential Manager (service: DeadlockBot, key: TWITCH_ANALYTICS_DSN).
- Provides a sqlite-like interface: get_conn() yields a psycopg connection; execute() etc. available via conn.
- Supports sqlite-style '?' placeholders by translating to '%s'.
- Adds minimal compatibility functions (strftime, printf) inside the target DB so existing analytics SQL keeps running.
"""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import Iterable, Sequence

import psycopg

log = logging.getLogger("TwitchStreams.StoragePG")

KEYRING_SERVICE = "DeadlockBot"
ENV_DSN = "TWITCH_ANALYTICS_DSN"

_COMPAT_INSTALLED = False


class RowCompat:
    """Row that supports both numeric and name-based access."""

    __slots__ = ("_values", "_map")

    def __init__(self, names: Sequence[str], values: Sequence[object]):
        self._values = tuple(values)
        self._map = {name: val for name, val in zip(names, values, strict=False)}

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return self._map[key]

    def get(self, key, default=None):
        return self._map.get(key, default)

    def keys(self):
        return self._map.keys()

    def values(self):
        return self._map.values()

    def items(self):
        return self._map.items()

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"RowCompat({self._map})"


def _compat_row_factory(cursor: psycopg.Cursor) -> psycopg.rows.RowMaker[RowCompat]:
    """Row factory returning RowCompat with both index and name access."""
    names = [col.name for col in cursor.description] if cursor.description else []

    def _maker(values: Sequence[object]) -> RowCompat:
        return RowCompat(names, values)

    return _maker


def _load_dsn() -> str:
    dsn = os.environ.get(ENV_DSN)
    if dsn:
        return dsn
    try:
        import keyring  # type: ignore

        val = keyring.get_password(KEYRING_SERVICE, ENV_DSN) or keyring.get_password(
            f"{ENV_DSN}@{KEYRING_SERVICE}", ENV_DSN
        )
        if val:
            return val
    except Exception as exc:  # pragma: no cover - best-effort Tresor lookup
        log.debug("Keyring lookup failed: %s", exc)
    raise RuntimeError(f"{ENV_DSN} not set (env or Windows Credential Manager '{KEYRING_SERVICE}')")


def _placeholder_sql(sql: str) -> str:
    """Escape literal '%' and convert sqlite-style '?' to psycopg placeholders."""
    # Escape all percent signs first; psycopg treats '%%' as literal '%'.
    sql = sql.replace("%", "%%")
    # Restore the valid placeholder forms.
    sql = sql.replace("%%s", "%s").replace("%%b", "%b").replace("%%t", "%t")
    # Translate sqlite-style '?' placeholders to '%s'.
    return sql.replace("?", "%s")


class _CompatCursor:
    """Lightweight wrapper to apply placeholder translation on execute calls."""

    def __init__(self, cursor: psycopg.Cursor):
        self._cursor = cursor

    def execute(self, sql: str, params=None, *args, **kwargs):
        return self._cursor.execute(_placeholder_sql(sql), params or (), *args, **kwargs)

    def executemany(self, sql: str, params_seq, *args, **kwargs):
        return self._cursor.executemany(_placeholder_sql(sql), params_seq, *args, **kwargs)

    # Passthrough for fetch* and iteration
    def __getattr__(self, item):
        return getattr(self._cursor, item)

    def __iter__(self):
        return iter(self._cursor)

    def __enter__(self):
        self._cursor.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._cursor.__exit__(exc_type, exc, tb)


class _CompatConnection:
    """Wrapper exposing a psycopg connection with sqlite-style execute()."""

    def __init__(self, conn: psycopg.Connection):
        self._conn = conn

    # Basic helpers expected by callers
    def execute(self, sql: str, params=None, *args, **kwargs):
        return self._conn.execute(_placeholder_sql(sql), params or (), *args, **kwargs)

    def cursor(self, *args, **kwargs):
        return _CompatCursor(self._conn.cursor(*args, **kwargs))

    # Context manager support
    def __enter__(self):
        self._conn.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._conn.__exit__(exc_type, exc, tb)

    # Delegate everything else to the real connection
    def __getattr__(self, item):
        return getattr(self._conn, item)


def _ensure_compat_functions(conn: psycopg.Connection) -> None:
    """Install lightweight sqlite compatibility helpers (strftime, printf, julianday, datetime) once per process."""
    global _COMPAT_INSTALLED
    if _COMPAT_INSTALLED:
        return
    with conn.cursor() as _cur:
        cur = _CompatCursor(_cur)
        cur.execute(
            """
            CREATE OR REPLACE FUNCTION strftime(fmt text, ts timestamptz)
            RETURNS text
            LANGUAGE plpgsql IMMUTABLE AS $$
            DECLARE p text := fmt;
            BEGIN
              IF fmt = '%w' THEN
                RETURN (EXTRACT(dow FROM ts))::int::text; -- 0=Sonntag wie SQLite
              END IF;
              p := replace(p, '%Y', 'YYYY');
              p := replace(p, '%m', 'MM');
              p := replace(p, '%d', 'DD');
              p := replace(p, '%H', 'HH24');
              p := replace(p, '%M', 'MI');
              RETURN to_char(ts, p);
            END;
            $$;
            """
        )
        cur.execute(
            """
            CREATE OR REPLACE FUNCTION printf(fmt text, arg numeric)
            RETURNS text
            LANGUAGE plpgsql IMMUTABLE AS $$
            BEGIN
              IF fmt = '%02d' THEN
                RETURN lpad((arg::int)::text, 2, '0');
              END IF;
              RETURN format(fmt, arg);
            END;
            $$;
            """
        )
        cur.execute(
            """
            CREATE OR REPLACE FUNCTION julianday(ts timestamptz)
            RETURNS double precision
            LANGUAGE plpgsql IMMUTABLE AS $$
            BEGIN
              RETURN EXTRACT(EPOCH FROM ts) / 86400.0 + 2440587.5;
            END;
            $$;
            """
        )
        cur.execute(
            """
            CREATE OR REPLACE FUNCTION julianday(ts text)
            RETURNS double precision
            LANGUAGE sql IMMUTABLE AS $$
              SELECT julianday(ts::timestamptz);
            $$;
            """
        )
        cur.execute(
            """
            CREATE OR REPLACE FUNCTION datetime(ts text, modifier text DEFAULT NULL)
            RETURNS timestamptz
            LANGUAGE plpgsql STABLE AS $$
            DECLARE
              base_ts timestamptz;
            BEGIN
              IF ts IS NULL THEN
                RETURN NULL;
              END IF;
              IF lower(ts) = 'now' THEN
                base_ts := NOW();
              ELSE
                base_ts := ts::timestamptz;
              END IF;
              IF modifier IS NOT NULL THEN
                base_ts := base_ts + modifier::interval;
              END IF;
              RETURN base_ts;
            END;
            $$;
            """
        )
    conn.commit()
    _COMPAT_INSTALLED = True


@contextlib.contextmanager
def get_conn():
    """Context manager returning a psycopg connection with dict rows and autocommit."""
    dsn = _load_dsn()
    conn = psycopg.connect(dsn, row_factory=_compat_row_factory, autocommit=True)
    try:
        _ensure_compat_functions(conn)
        yield _CompatConnection(conn)
    finally:
        conn.close()


def execute(sql: str, params: Iterable | None = None):
    with get_conn() as conn:
        return conn.execute(sql, params or [])


def query_one(sql: str, params: Iterable | None = None):
    with get_conn() as conn:
        return conn.execute(sql, params or []).fetchone()


def query_all(sql: str, params: Iterable | None = None):
    with get_conn() as conn:
        return conn.execute(sql, params or []).fetchall()


def backfill_tracked_stats_from_category(conn, login: str) -> int:
    """Copy historic category stats into tracked stats for one streamer (idempotent)."""
    normalized = (login or "").strip().lower()
    if not normalized:
        return 0

    cur = conn.execute(
        """
        INSERT INTO twitch_stats_tracked
            (ts_utc, streamer, viewer_count, is_partner, game_name, stream_title, tags)
        SELECT c.ts_utc, c.streamer, c.viewer_count, c.is_partner,
               c.game_name, c.stream_title, c.tags
          FROM twitch_stats_category c
         WHERE LOWER(c.streamer) = ?
           AND NOT EXISTS (
               SELECT 1
                 FROM twitch_stats_tracked t
                WHERE LOWER(t.streamer) = LOWER(c.streamer)
                  AND t.ts_utc = c.ts_utc
           )
        """,
        (normalized,),
    )
    return int(cur.rowcount or 0)
