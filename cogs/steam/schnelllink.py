from __future__ import annotations

import datetime as _dt
import logging
import os
import sqlite3
from dataclasses import dataclass
from typing import Optional

import discord

from service import db

log = logging.getLogger(__name__)

SCHNELL_LINK_CUSTOM_ID = "steam:schnelllink"


@dataclass(slots=True)
class SchnellLink:
    """Represents a generated Steam link for the bot account."""

    url: str
    token: Optional[str] = None
    friend_code: Optional[str] = None
    expires_at: Optional[int] = None
    single_use: bool = False


_SELECT_AVAILABLE = """
SELECT token, invite_link, invite_limit, invite_duration, created_at,
       expires_at, status, reserved_by, reserved_at
FROM steam_quick_invites
WHERE status = 'available'
  AND (expires_at IS NULL OR expires_at > strftime('%s','now'))
ORDER BY created_at ASC
LIMIT 1
"""

_MARK_SHARED = """
UPDATE steam_quick_invites
SET status = 'shared',
    reserved_by = ?,
    reserved_at = strftime('%s','now')
WHERE token = ? AND status = 'available'
"""


def _reserve_pre_generated_link(discord_user_id: Optional[int]) -> Optional[SchnellLink]:
    """Fetch a pre-generated single-use link from the shared SQLite pool."""

    try:
        conn = db.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            log.debug("Failed to open transaction for schnelllink reservation: %s", exc)
            return None

        try:
            row = conn.execute(_SELECT_AVAILABLE).fetchone()
            if not row:
                conn.execute("ROLLBACK")
                return None

            token = row["token"]
            cursor = conn.execute(
                _MARK_SHARED,
                (int(discord_user_id) if discord_user_id else None, token),
            )
            if cursor.rowcount < 1:
                conn.execute("ROLLBACK")
                return None

            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    except Exception:
        log.exception("Failed to reserve schnelllink from DB", extra={"user_id": discord_user_id})
        return None

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


def _friend_code() -> Optional[str]:
    code = (os.getenv("STEAM_FRIEND_CODE") or "820142646").strip()
    return code or None


def _fallback_link() -> Optional[SchnellLink]:
    url = (os.getenv("STEAM_FRIEND_LINK") or "").strip()
    friend_code = _friend_code()

    if not url:
        if friend_code:
            url = f"https://s.team/p/{friend_code}"
        else:
            profile = (os.getenv("STEAM_PROFILE_URL") or "").strip()
            if profile:
                url = profile

    if not url:
        return None

    return SchnellLink(url=url, friend_code=friend_code, single_use=False)


def _format_link_message(link: SchnellLink) -> str:
    parts = ["⚡ **Hier ist dein Schnell-Link zum Steam-Bot:**\n", link.url]

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
        else:
            parts.append("\nDieser Link verfällt erst, wenn er eingelöst wurde.")
    else:
        parts.append("\nDieser Link kann mehrfach verwendet werden.")

    friend_code = link.friend_code or _friend_code()
    if friend_code:
        parts.append(f"\nAlternativ bleibt der Freundescode **{friend_code}** verfügbar.")

    return "".join(parts)


async def respond_with_schnelllink(
    interaction: discord.Interaction,
    *,
    source: Optional[str] = None,
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

    async def _send(message: str) -> None:
        if followup:
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    link: Optional[SchnellLink] = _reserve_pre_generated_link(
        getattr(interaction.user, "id", None)
    )

    if not link:
        link = _fallback_link()

    if not link:
        await _send("⚠️ Aktuell können keine Links erzeugt werden. Bitte versuche es später erneut.")
        return

    await _send(_format_link_message(link))


class SchnellLinkButton(discord.ui.Button):
    def __init__(
        self,
        *,
        label: str = "Schnelle Anfrage senden",
        style: discord.ButtonStyle = discord.ButtonStyle.success,
        emoji: Optional[str] = "⚡",
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
    "SchnellLinkButton",
    "respond_with_schnelllink",
]
