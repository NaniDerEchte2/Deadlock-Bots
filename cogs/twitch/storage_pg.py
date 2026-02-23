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
from typing import Iterable

import psycopg
from psycopg.rows import dict_row

log = logging.getLogger("TwitchStreams.StoragePG")

KEYRING_SERVICE = "DeadlockBot"
ENV_DSN = "TWITCH_ANALYTICS_DSN"

_COMPAT_INSTALLED = False


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
    raise RuntimeError(
        f"{ENV_DSN} not set (env or Windows Credential Manager '{KEYRING_SERVICE}')"
    )


def _placeholder_sql(sql: str) -> str:
    """Convert sqlite-style '?' placeholders to psycopg '%s'."""
    return sql.replace("?", "%s")


def _ensure_compat_functions(conn: psycopg.Connection) -> None:
    """Install lightweight sqlite compatibility helpers (strftime, printf) once per process."""
    global _COMPAT_INSTALLED
    if _COMPAT_INSTALLED:
        return
    with conn.cursor() as cur:
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
    conn.commit()
    _COMPAT_INSTALLED = True


@contextlib.contextmanager
def get_conn():
    """Context manager returning a psycopg connection with dict rows and autocommit."""
    dsn = _load_dsn()
    conn = psycopg.connect(dsn, row_factory=dict_row, autocommit=True)
    try:
        _ensure_compat_functions(conn)
        yield conn
    finally:
        conn.close()


def execute(sql: str, params: Iterable | None = None):
    with get_conn() as conn:
        return conn.execute(_placeholder_sql(sql), params or [])


def query_one(sql: str, params: Iterable | None = None):
    with get_conn() as conn:
        return conn.execute(_placeholder_sql(sql), params or []).fetchone()


def query_all(sql: str, params: Iterable | None = None):
    with get_conn() as conn:
        return conn.execute(_placeholder_sql(sql), params or []).fetchall()
