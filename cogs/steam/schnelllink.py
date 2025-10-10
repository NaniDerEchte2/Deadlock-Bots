# -*- coding: utf-8 -*-
# filename: cogs/steam/schnelllink.py
from __future__ import annotations

import os
import re
import sqlite3
import time as _time
import datetime as _dt
from dataclasses import dataclass
from typing import Optional

import discord

# -----------------------------------------------------------------------------
# Konfiguration
# -----------------------------------------------------------------------------

# Zentraler DB-Pfad: per ENV (kompletter Pfad) oder Verzeichnis + Standardname
ENV_DB_PATH = "DEADLOCK_DB_PATH"
ENV_DB_DIR  = "DEADLOCK_DB_DIR"
DEFAULT_DB_NAME = "deadlock.sqlite3"

def _resolve_db_path() -> str:
    p = os.getenv(ENV_DB_PATH)
    if p:
        return p
    d = os.getenv(ENV_DB_DIR)
    if d:
        return os.path.join(d, DEFAULT_DB_NAME)
    # Windows: %USERPROFILE%\Documents\Deadlock\service\deadlock.sqlite3
    up = os.environ.get("USERPROFILE")
    if up:
        return os.path.join(up, "Documents", "Deadlock", "service", DEFAULT_DB_NAME)
    # Unix:
    from pathlib import Path
    return str(Path.home() / "Documents" / "Deadlock" / "service" / DEFAULT_DB_NAME)

DB_PATH = _resolve_db_path()

# Gültiges s.team-Format: zwei Segmente nach /p/
INVITE_RX = re.compile(r"^https://s\.team/p/[A-Za-z0-9-]+/[A-Za-z0-9]+(?:\?.*)?$")

# -----------------------------------------------------------------------------
# Datentyp
# -----------------------------------------------------------------------------

@dataclass
class SchnellLink:
    url: Optional[str]            # Der echte Quick-Invite-Link (falls vorhanden)
    token: Optional[str] = None   # DB-Token (steam_quick_invites.token), wenn reserviert
    friend_code: Optional[str] = None
    expires_at: Optional[int] = None
    single_use: bool = False      # True, wenn aus der DB (Single-Use)

# -----------------------------------------------------------------------------
# DB / Quick-Invite Reservierung
# -----------------------------------------------------------------------------

def _conn() -> sqlite3.Connection:
    # Autocommit, Row-Factory
    c = sqlite3.connect(DB_PATH, isolation_level=None)
    c.row_factory = sqlite3.Row
    return c

def _now() -> int:
    return int(_time.time())

def _valid_invite_link(url: Optional[str]) -> bool:
    if not url:
        return False
    return bool(INVITE_RX.match(url.strip()))

def _friend_code() -> Optional[str]:
    """
    Friend-Code als Fallback anzeigen (z.B. '820142646').
    Dieser erzeugt KEINEN gültigen s.team/p/…-Link!
    """
    code = (os.getenv("STEAM_FRIEND_CODE") or "").strip()
    return code or None

def _reserve_pre_generated_link(discord_user_id: Optional[int]) -> Optional[SchnellLink]:
    """
    Holt einen vorbereiteten Quick-Invite aus der Tabelle steam_quick_invites
    und markiert ihn als 'reserved'. NUR Links mit korrektem s.team-Format.
    Schema (aus deinem Projekt):
      token TEXT PRIMARY KEY,
      invite_link TEXT NOT NULL,
      invite_limit INTEGER DEFAULT 1,
      invite_duration INTEGER,
      created_at INTEGER NOT NULL,
      expires_at INTEGER,
      status TEXT DEFAULT 'available',
      reserved_by INTEGER,
      reserved_at INTEGER,
      last_seen INTEGER
    """
    try:
        with _conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT token, invite_link, expires_at
                  FROM steam_quick_invites
                 WHERE status='available'
                   AND (expires_at IS NULL OR expires_at > strftime('%s','now'))
                 ORDER BY created_at ASC
                 LIMIT 1
                """
            ).fetchone()

            if not row:
                conn.execute("ROLLBACK")
                return None

            url = str(row["invite_link"] or "").strip()
            if not _valid_invite_link(url):
                # Ungültigen Eintrag überspringen
                conn.execute(
                    "UPDATE steam_quick_invites SET status='invalid', last_seen=? WHERE token=?",
                    (_now(), row["token"])
                )
                conn.execute("COMMIT")
                return None

            conn.execute(
                """
                UPDATE steam_quick_invites
                   SET status='reserved',
                       reserved_by=?,
                       reserved_at=?,
                       last_seen=?
                 WHERE token=? AND status='available'
                """,
                (int(discord_user_id or 0), _now(), _now(), row["token"])
            )
            conn.execute("COMMIT")

        expires_at = row["expires_at"]
        try:
            expires_at = int(expires_at) if expires_at is not None else None
        except Exception:
            expires_at = None

        return SchnellLink(
            url=str(row["invite_link"]),
            token=str(row["token"]),
            friend_code=_friend_code(),
            expires_at=expires_at,
            single_use=True,
        )
    except Exception:
        # bewusst schlank – calling code darf „None“ behandeln
        return None

# -----------------------------------------------------------------------------
# Fallback-Strategie (ohne „erfundene“ s.team/p/<friend_code>)
# -----------------------------------------------------------------------------

def _fallback_link() -> Optional[SchnellLink]:
    """
    1) Wenn STEAM_FRIEND_LINK gesetzt **und gültig**, verwende ihn.
    2) Optional: STEAM_PROFILE_URL als generischer Link (kein Quick-Invite).
    3) Immer Friend-Code mitliefern – aber **keinen** s.team/p/<friend_code> bauen.
    """
    friend_code = _friend_code()

    url_env = (os.getenv("STEAM_FRIEND_LINK") or "").strip()
    if _valid_invite_link(url_env):
        return SchnellLink(url=url_env, friend_code=friend_code, single_use=False)

    profile = (os.getenv("STEAM_PROFILE_URL") or "").strip()
    if profile.startswith("http"):
        return SchnellLink(url=profile, friend_code=friend_code, single_use=False)

    # Kein gültiger Link – nur Friend-Code zurückgeben (Caller formatiert den Text)
    return SchnellLink(url=None, friend_code=friend_code, single_use=False)

# -----------------------------------------------------------------------------
# Public API
# -----------------------------------------------------------------------------

def get_schnelllink(discord_user_id: Optional[int]) -> Optional[SchnellLink]:
    """
    Bevorzugt reservierten Quick-Invite aus der DB; sonst sauberer Fallback.
    """
    link = _reserve_pre_generated_link(discord_user_id)
    if link:
        return link
    return _fallback_link()

def format_link_message(link: SchnellLink) -> str:
    """
    Baut den Nachrichtentext für Discord.
    - Wenn link.url ein echter Quick-Invite ist → anzeigen
    - Wenn link.url None ist → nur Friend-Code kommunizieren
    """
    parts = ["⚡ **Hier ist dein Schnell-Link zum Steam-Bot:**\n"]

    if link.url and _valid_invite_link(link.url):
        parts.append(link.url)
        if link.single_use:
            parts.append("\nDieser Link kann genau **einmal** verwendet werden.")
            if link.expires_at:
                expires_dt = _dt.datetime.fromtimestamp(link.expires_at, tz=_dt.timezone.utc)
                parts.append(
                    "\nGültig bis {} ({}).".format(
                        discord.utils.format_dt(expires_dt, style="R"),
                        discord.utils.format_dt(expires_dt, style="f"),
                    )
                )
    elif link.url:
        # Generischer Link (Profilseite) – trotzdem anzeigen
        parts.append(link.url)

    # Friend-Code als alternative Option nennen
    if link.friend_code:
        parts.append(
            f"\n\n🧩 **Alternativ** kannst du diesen **Friend-Code** im Steam-Client verwenden: "
            f"`{link.friend_code}`\n"
            "Öffne dazu im Client **Freunde hinzufügen** und gib den Code ein."
        )

    return "".join(parts)

