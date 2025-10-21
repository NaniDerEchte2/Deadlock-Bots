from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Optional, Tuple

import aiohttp
import discord

from service import db

log = logging.getLogger(__name__)

SCHNELL_LINK_CUSTOM_ID = "steam:schnelllink"
_INVITE_LINK_PATTERN = re.compile(r"^https://s\.team/p/[A-Za-z0-9-]+/[A-Za-z0-9]+$")


@dataclass(slots=True)
class SchnellLink:
    """Represents a generated Steam link for the bot account."""

    url: str
    token: str
    friend_code: Optional[str] = None
    expires_at: Optional[int] = None
    single_use: bool = True


_SELECT_AVAILABLE = """
SELECT rowid AS _rowid_, token, invite_link, invite_limit, invite_duration,
       created_at, expires_at, status, reserved_by, reserved_at
FROM steam_quick_invites
WHERE status = 'available'
  AND (expires_at IS NULL OR expires_at > strftime('%s','now'))
ORDER BY created_at ASC
LIMIT 1
"""

_UPDATE_RESERVED = """
UPDATE steam_quick_invites
SET status = 'reserved',
    reserved_by = ?,
    reserved_at = strftime('%s','now'),
    last_seen = strftime('%s','now')
WHERE rowid = ? AND status = 'available'
"""

_MARK_INVALID_BY_ROWID = """
UPDATE steam_quick_invites
SET status = 'invalid',
    last_seen = strftime('%s','now')
WHERE rowid = ?
"""


def _enqueue_ensure_pool(conn: Optional[sqlite3.Connection] = None) -> bool:
    connection = conn or db.connect()
    payload = json.dumps(
        {"target": 5, "invite_limit": 1, "invite_duration": None},
        separators=(",", ":"),
    )
    existing = connection.execute(
        """
        SELECT 1
        FROM steam_tasks
        WHERE type = ?
          AND status = 'PENDING'
        LIMIT 1
        """,
        ("AUTH_QUICK_INVITE_ENSURE_POOL",),
    ).fetchone()
    if existing:
        return False

    connection.execute(
        "INSERT INTO steam_tasks(type, payload, status) VALUES (?, ?, 'PENDING')",
        ("AUTH_QUICK_INVITE_ENSURE_POOL", payload),
    )
    return True


def reserve_invite(discord_user_id: Optional[int]) -> SchnellLink:
    conn = db.connect()
    reserved_row = None

    with db._LOCK:  # type: ignore[attr-defined]
        attempts = 0
        while True:
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:  # noqa: PERF203
                attempts += 1
                log.debug("Could not begin reservation transaction: %s", exc)
                if attempts >= 5:
                    raise RuntimeError(
                        "Kein Quick-Invite verfügbar – Produktion angestoßen"
                    ) from exc
                time.sleep(0.05)
                continue

            row = conn.execute(_SELECT_AVAILABLE).fetchone()
            if not row:
                conn.execute("ROLLBACK")
                enqueued = _enqueue_ensure_pool(conn)
                if enqueued:
                    log.info("Triggered quick invite production because pool is empty")
                raise RuntimeError("Kein Quick-Invite verfügbar – Produktion angestoßen")

            invite_link = str(row["invite_link"])
            if not _INVITE_LINK_PATTERN.fullmatch(invite_link):
                conn.execute(_MARK_INVALID_BY_ROWID, (row["_rowid_"],))
                conn.execute("COMMIT")
                log.warning(
                    "Discarded malformed quick invite link",
                    extra={"token": row["token"], "invite_link": invite_link},
                )
                continue

            reserved_by = int(discord_user_id) if discord_user_id is not None else None
            cursor = conn.execute(_UPDATE_RESERVED, (reserved_by, row["_rowid_"]))
            if cursor.rowcount != 1:
                conn.execute("ROLLBACK")
                continue

            conn.execute("COMMIT")
            reserved_row = row
            break

    expires_at = reserved_row["expires_at"]
    try:
        expires_at_int = int(expires_at) if expires_at is not None else None
    except Exception:  # pragma: no cover - defensive, DB should ensure type
        expires_at_int = None

    invite_limit = reserved_row["invite_limit"]
    single_use = True
    if invite_limit is not None:
        try:
            single_use = int(invite_limit) == 1
        except Exception:  # pragma: no cover - defensive
            single_use = True

    return SchnellLink(
        url=str(reserved_row["invite_link"]),
        token=str(reserved_row["token"]),
        expires_at=expires_at_int,
        single_use=single_use,
    )


def mark_used(token: str) -> bool:
    conn = db.connect()
    with db._LOCK:  # type: ignore[attr-defined]
        cursor = conn.execute(
            """
            UPDATE steam_quick_invites
            SET status = 'used',
                last_seen = strftime('%s','now')
            WHERE token = ?
            """,
            (token,),
        )
    return cursor.rowcount > 0


def mark_invalid(token: str) -> bool:
    conn = db.connect()
    with db._LOCK:  # type: ignore[attr-defined]
        cursor = conn.execute(
            """
            UPDATE steam_quick_invites
            SET status = 'invalid',
                last_seen = strftime('%s','now')
            WHERE token = ?
            """,
            (token,),
        )
    return cursor.rowcount > 0


def ensure_pool(min_available: int = 5) -> Tuple[int, bool]:
    conn = db.connect()
    with db._LOCK:  # type: ignore[attr-defined]
        row = conn.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM steam_quick_invites
            WHERE status = 'available'
              AND (expires_at IS NULL OR expires_at > strftime('%s','now'))
            """,
        ).fetchone()
        available = int(row["cnt"]) if row is not None else 0
        if available >= min_available:
            return available, False

        enqueued = _enqueue_ensure_pool(conn)
        if enqueued:
            log.info(
                "Triggered quick invite production because available=%s below threshold %s",
                available,
                min_available,
            )
        return available, enqueued


def expire_links() -> int:
    conn = db.connect()
    with db._LOCK:  # type: ignore[attr-defined]
        cursor = conn.execute(
            """
            UPDATE steam_quick_invites
            SET status = 'invalid',
                last_seen = strftime('%s','now')
            WHERE status IN ('available', 'reserved')
              AND expires_at IS NOT NULL
              AND expires_at <= strftime('%s','now')
            """,
        )
    return cursor.rowcount


def _format_link_message(link: SchnellLink) -> str:
    parts = ["\u26a1 **Hier ist dein Schnell-Link zum Steam-Bot:**\n", link.url]

    if link.single_use:
        parts.append("\nDieser Link kann genau **einmal** verwendet werden.")
        if link.expires_at:
            expires_dt = _dt.datetime.fromtimestamp(link.expires_at, tz=_dt.timezone.utc)
            parts.append(
                "\nG\u00fcltig bis {} ({}).".format(
                    discord.utils.format_dt(expires_dt, style="R"),
                    discord.utils.format_dt(expires_dt, style="f"),
                )
            )
        else:
            parts.append("\nDieser Link verf\u00e4llt erst, wenn er eingel\u00f6st wurde.")
    else:
        parts.append("\nDieser Link kann mehrfach verwendet werden.")

    return "".join(parts)


async def _check_link_validity(url: str) -> Optional[bool]:
    """Return ``True`` if the link appears valid, ``False`` if definitely invalid."""

    timeout = aiohttp.ClientTimeout(total=6)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for method in ("HEAD", "GET"):
                try:
                    async with session.request(method, url, allow_redirects=False) as response:
                        status = int(response.status)
                except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    log.debug(
                        "Quick invite validity check inconclusive",
                        extra={"url": url, "error": str(exc), "method": method},
                    )
                    return None

                if status in {404, 410}:
                    return False

                if 200 <= status < 400 or status in {401, 403}:
                    return True

                if method == "HEAD" and status in {405}:
                    # Retry with GET which is more broadly supported.
                    continue

                if status == 429:
                    return None

            return None
    except Exception:  # pragma: no cover - defensive
        log.exception("Unexpected error while checking quick invite validity", extra={"url": url})
        return None


async def respond_with_schnelllink(
    interaction: discord.Interaction,
    *,
    source: Optional[str] = None,
    extra_note: Optional[str] = None,
    suppress_embeds: bool = False,
) -> None:
    """Respond to the interaction with either a single-use or fallback Steam link."""

    followup = interaction.response.is_done()
    if not followup:
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            followup = True
        except Exception as exc:  # noqa: BLE001
            log.debug(
                "Schnelllink defer failed",
                exc_info=True,
                extra={"source": source, "error": str(exc)},
            )
            followup = False

    if extra_note:
        extra_note = extra_note.strip()

    async def _send(message: str) -> None:
        if extra_note:
            message = f"{message}\n\n{extra_note}"
        if followup:
            await interaction.followup.send(message, ephemeral=True, suppress_embeds=suppress_embeds)
        else:
            await interaction.response.send_message(message, ephemeral=True, suppress_embeds=suppress_embeds)

    attempts = 0
    link: Optional[SchnellLink] = None
    while attempts < 3 and link is None:
        attempts += 1
        try:
            candidate = reserve_invite(getattr(interaction.user, "id", None))
        except RuntimeError as exc:
            await _send(str(exc))
            return
        except Exception:  # pragma: no cover - defensive
            log.exception("Unexpected error while reserving quick invite")
            await _send(
                "\u26a0\ufe0f Aktuell k\u00f6nnen keine Links erzeugt werden. Bitte versuche es sp\u00e4ter erneut."
            )
            return

        is_valid: Optional[bool]
        try:
            is_valid = await _check_link_validity(candidate.url)
        except Exception:  # pragma: no cover - defensive
            log.exception(
                "Unexpected error while validating quick invite link",
                extra={"source": source, "url": candidate.url},
            )
            is_valid = None

        if is_valid is False:
            log.warning(
                "Discarded invalid quick invite link during delivery",
                extra={"token": candidate.token, "url": candidate.url},
            )
            mark_invalid(candidate.token)
            ensure_pool(min_available=1)
            continue

        link = candidate

    if link is None:
        await _send(
            "\u26a0\ufe0f Aktuell k\u00f6nnen keine g\u00fcltigen Schnell-Links bereitgestellt werden. Bitte versuche es sp\u00e4ter erneut."
        )
        return

    await _send(_format_link_message(link))
    ensure_pool(min_available=1)


class SchnellLinkButton(discord.ui.Button):
    def __init__(
        self,
        *,
        label: str = "Schnelle Anfrage senden",
        style: discord.ButtonStyle = discord.ButtonStyle.success,
        emoji: Optional[str] = "\u26a1",
        custom_id: str = SCHNELL_LINK_CUSTOM_ID,
        row: Optional[int] = None,
        source: Optional[str] = None,
    ) -> None:
        super().__init__(label=label, style=style, emoji=emoji, custom_id=custom_id, row=row)
        self._source = source or "schnelllink-button"

    async def callback(self, interaction: discord.Interaction) -> None:  # noqa: D401
        await respond_with_schnelllink(interaction, source=self._source)


__all__ = [
    "SCHNELL_LINK_CUSTOM_ID",
    "SchnellLink",
    "ensure_pool",
    "expire_links",
    "mark_invalid",
    "mark_used",
    "reserve_invite",
    "SchnellLinkButton",
    "respond_with_schnelllink",
]
