from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

import discord
from discord import app_commands
from discord.ext import commands

from service import db
from cogs.steam.friend_requests import queue_friend_request
from cogs.steam.steam_master import SteamTaskClient

log = logging.getLogger(__name__)

STEAM64_BASE = 76561197960265728

STATUS_PENDING = "pending"
STATUS_WAITING = "waiting_friend"
STATUS_INVITE_SENT = "invite_sent"
STATUS_ERROR = "error"

_ALLOWED_UPDATE_FIELDS = {
    "status",
    "last_error",
    "friend_requested_at",
    "friend_confirmed_at",
    "invite_sent_at",
    "last_notified_at",
    "account_id",
}


@dataclass(slots=True)
class BetaInviteRecord:
    id: int
    discord_id: int
    steam_id64: str
    account_id: Optional[int]
    status: str
    last_error: Optional[str]
    friend_requested_at: Optional[int]
    friend_confirmed_at: Optional[int]
    invite_sent_at: Optional[int]
    last_notified_at: Optional[int]
    created_at: Optional[int]
    updated_at: Optional[int]


def _row_to_record(row: Optional[db.sqlite3.Row]) -> Optional[BetaInviteRecord]:  # type: ignore[attr-defined]
    if row is None:
        return None
    return BetaInviteRecord(
        id=int(row["id"]),
        discord_id=int(row["discord_id"]),
        steam_id64=str(row["steam_id64"]),
        account_id=int(row["account_id"]) if row["account_id"] is not None else None,
        status=str(row["status"]),
        last_error=str(row["last_error"]) if row["last_error"] is not None else None,
        friend_requested_at=int(row["friend_requested_at"]) if row["friend_requested_at"] is not None else None,
        friend_confirmed_at=int(row["friend_confirmed_at"]) if row["friend_confirmed_at"] is not None else None,
        invite_sent_at=int(row["invite_sent_at"]) if row["invite_sent_at"] is not None else None,
        last_notified_at=int(row["last_notified_at"]) if row["last_notified_at"] is not None else None,
        created_at=int(row["created_at"]) if row["created_at"] is not None else None,
        updated_at=int(row["updated_at"]) if row["updated_at"] is not None else None,
    )


def _fetch_invite_by_discord(discord_id: int) -> Optional[BetaInviteRecord]:
    row = db.connect().execute(
        "SELECT * FROM steam_beta_invites WHERE discord_id = ?",
        (int(discord_id),),
    ).fetchone()
    return _row_to_record(row)


def _fetch_invite_by_id(record_id: int) -> Optional[BetaInviteRecord]:
    row = db.connect().execute(
        "SELECT * FROM steam_beta_invites WHERE id = ?",
        (int(record_id),),
    ).fetchone()
    return _row_to_record(row)


def _create_or_reset_invite(discord_id: int, steam_id64: str, account_id: Optional[int]) -> BetaInviteRecord:
    with db._LOCK:  # type: ignore[attr-defined]
        conn = db.connect()
        conn.execute(
            """
            INSERT INTO steam_beta_invites(discord_id, steam_id64, account_id, status)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(discord_id) DO UPDATE SET
              steam_id64 = excluded.steam_id64,
              account_id = excluded.account_id,
              status = excluded.status,
              last_error = NULL,
              friend_requested_at = NULL,
              friend_confirmed_at = NULL,
              invite_sent_at = NULL,
              last_notified_at = NULL,
              updated_at = strftime('%s','now')
            """,
            (int(discord_id), str(steam_id64), account_id, STATUS_PENDING),
        )
        row = conn.execute(
            "SELECT * FROM steam_beta_invites WHERE discord_id = ?",
            (int(discord_id),),
        ).fetchone()
    record = _row_to_record(row)
    if record is None:  # pragma: no cover - defensive
        raise RuntimeError("Konnte steam_beta_invites-Eintrag nicht erstellen")
    return record


def _update_invite(record_id: int, **fields) -> Optional[BetaInviteRecord]:
    assignments = []
    params = []
    for key, value in fields.items():
        if key not in _ALLOWED_UPDATE_FIELDS:
            continue
        assignments.append(f"{key} = ?")
        params.append(value)
    if not assignments:
        return _fetch_invite_by_id(record_id)

    assignments.append("updated_at = strftime('%s','now')")
    params.append(int(record_id))

    with db._LOCK:  # type: ignore[attr-defined]
        conn = db.connect()
        conn.execute(
            f"UPDATE steam_beta_invites SET {', '.join(assignments)} WHERE id = ?",
            params,
        )
        row = conn.execute(
            "SELECT * FROM steam_beta_invites WHERE id = ?",
            (int(record_id),),
        ).fetchone()
    return _row_to_record(row)


def _ensure_steam_link(discord_id: int, steam_id64: str) -> None:
    try:
        with db._LOCK:  # type: ignore[attr-defined]
            db.connect().execute(
                """
                INSERT INTO steam_links(user_id, steam_id, name, verified)
                VALUES(?, ?, '', 0)
                ON CONFLICT(user_id, steam_id) DO UPDATE SET
                  updated_at = CURRENT_TIMESTAMP
                """,
                (int(discord_id), str(steam_id64)),
            )
    except Exception:  # pragma: no cover - best-effort
        log.exception("Konnte steam_links-Eintrag nicht aktualisieren")


def steam64_to_account_id(steam_id64: str) -> int:
    try:
        value = int(str(steam_id64))
    except (TypeError, ValueError) as exc:
        raise ValueError("SteamID64 muss numerisch sein") from exc
    if value < STEAM64_BASE:
        raise ValueError("SteamID64 liegt unterhalb des gültigen Bereichs")
    return value - STEAM64_BASE


class BetaInviteConfirmView(discord.ui.View):
    def __init__(self, cog: "BetaInviteFlow", record_id: int, discord_id: int, steam_id64: str) -> None:
        super().__init__(timeout=600)
        self.cog = cog
        self.record_id = record_id
        self.discord_id = discord_id
        self.steam_id64 = steam_id64

    @discord.ui.button(label="Freundschaft bestätigt", style=discord.ButtonStyle.success, emoji="🤝")
    async def confirm_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.user.id != self.discord_id:
            await interaction.response.send_message(
                "Nur der ursprüngliche Nutzer kann diese Einladung bestätigen.",
                ephemeral=True,
            )
            return
        await self.cog.handle_confirmation(interaction, self.record_id)


class BetaInviteFlow(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.tasks = SteamTaskClient(poll_interval=0.5, default_timeout=30.0)

    async def _resolve_steam_id(self, raw: str) -> Optional[str]:
        candidate = (raw or "").strip()
        if not candidate:
            return None

        steam_cog = None
        try:
            steam_cog = self.bot.get_cog("SteamLink")
        except Exception:  # pragma: no cover - defensive
            steam_cog = None

        if steam_cog and hasattr(steam_cog, "_resolve_steam_input"):
            try:
                resolved = await steam_cog._resolve_steam_input(candidate)  # type: ignore[attr-defined]
                if resolved:
                    return str(resolved)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.debug("SteamLink-Resolver schlug fehl", exc_info=True)

        if re.fullmatch(r"\d{17}", candidate):
            return candidate

        try:
            parsed = urlparse(candidate)
        except Exception:
            parsed = None
        if parsed and parsed.scheme in {"http", "https"}:
            host = (parsed.hostname or "").lower().rstrip(".")
            path = (parsed.path or "").rstrip("/")
            match = re.fullmatch(r"/profiles/(\d{17})", path)
            if host.endswith("steamcommunity.com") and match:
                return match.group(1)
        return None

    async def handle_confirmation(self, interaction: discord.Interaction, record_id: int) -> None:
        record = _fetch_invite_by_id(record_id)
        if record is None:
            await interaction.response.send_message(
                "❌ Kein Eintrag für diese Einladung gefunden. Bitte starte den Vorgang mit `/betainvite` neu.",
                ephemeral=True,
            )
            return

        if record.discord_id != interaction.user.id:
            await interaction.response.send_message(
                "❌ Diese Einladung gehört einem anderen Nutzer.",
                ephemeral=True,
            )
            return

        if record.status == STATUS_INVITE_SENT:
            await interaction.response.send_message(
                "✅ Du wurdest bereits eingeladen. Prüfe in deiner Steam-Bibliothek oder unter https://store.steampowered.com/account/playtestinvites .",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        friend_outcome = await self.tasks.run(
            "AUTH_CHECK_FRIENDSHIP",
            {"steam_id": record.steam_id64},
            timeout=20.0,
        )

        friend_ok = False
        relationship_name = "unknown"
        if friend_outcome.ok and friend_outcome.result:
            data = friend_outcome.result.get("data") if isinstance(friend_outcome.result, dict) else None
            if isinstance(data, dict):
                friend_ok = bool(data.get("friend"))
                relationship_name = str(data.get("relationship_name") or relationship_name)
                if data.get("account_id") is not None and data.get("account_id") != record.account_id:
                    record = _update_invite(record.id, account_id=int(data["account_id"])) or record
        if not friend_ok:
            await interaction.followup.send(
                "ℹ️ Wir sind noch keine bestätigten Steam-Freunde. Bitte nimm die Freundschaftsanfrage an und probiere es erneut.",
                ephemeral=True,
            )
            return

        now_ts = int(time.time())
        record = _update_invite(
            record.id,
            status=STATUS_WAITING,
            friend_confirmed_at=now_ts,
            last_error=None,
        ) or record

        account_id = record.account_id or steam64_to_account_id(record.steam_id64)
        invite_outcome = await self.tasks.run(
            "AUTH_SEND_PLAYTEST_INVITE",
            {
                "steam_id": record.steam_id64,
                "account_id": account_id,
                "location": "discord-betainvite",
                "timeout_ms": 15000,
            },
            timeout=25.0,
        )

        if not invite_outcome.ok:
            error_text = invite_outcome.error or "Game Coordinator hat die Einladung abgelehnt."
            if invite_outcome.result and isinstance(invite_outcome.result, dict):
                data = invite_outcome.result.get("data")
                if isinstance(data, dict):
                    response = data.get("response")
                    if isinstance(response, dict) and response.get("message"):
                        error_text = str(response["message"])
            _update_invite(
                record.id,
                status=STATUS_ERROR,
                last_error=str(error_text),
            )
            await interaction.followup.send(
                f"❌ Einladung konnte nicht versendet werden: {error_text}",
                ephemeral=True,
            )
            return

        record = _update_invite(
            record.id,
            status=STATUS_INVITE_SENT,
            invite_sent_at=now_ts,
            last_notified_at=now_ts,
            last_error=None,
        ) or record

        message = (
            "✅ Einladung verschickt!\n"
            "Bitte schaue in 1-2 Stunden unter https://store.steampowered.com/account/playtestinvites "
            "und nimm die Einladung dort an. Danach erscheint Deadlock automatisch in deiner Bibliothek."
        )
        await interaction.followup.send(message, ephemeral=True)

        try:
            await interaction.user.send(message)
        except Exception:  # pragma: no cover - DM optional
            log.debug("Konnte Bestätigungs-DM nicht senden", exc_info=True)

    @app_commands.command(name="betainvite", description="Automatisiert eine Deadlock-Playtest-Einladung anfordern.")
    @app_commands.describe(steam_id="SteamID64 oder steamcommunity.com/profiles/<id>-Link")
    async def betainvite(self, interaction: discord.Interaction, steam_id: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        resolved = await self._resolve_steam_id(steam_id)
        if not resolved:
            await interaction.followup.send(
                "❌ Konnte deine SteamID nicht bestimmen. Bitte nutze die 17-stellige SteamID64 oder einen `/profiles/<id>`-Link.",
                ephemeral=True,
            )
            return

        try:
            account_id = steam64_to_account_id(resolved)
        except ValueError as exc:
            await interaction.followup.send(f"❌ Ungültige SteamID: {exc}", ephemeral=True)
            return

        existing = _fetch_invite_by_discord(interaction.user.id)
        if existing and existing.status == STATUS_INVITE_SENT and existing.steam_id64 == resolved:
            await interaction.followup.send(
                "✅ Du bist bereits eingeladen. Prüfe unter https://store.steampowered.com/account/playtestinvites .",
                ephemeral=True,
            )
            return

        record = _create_or_reset_invite(interaction.user.id, resolved, account_id)
        _ensure_steam_link(interaction.user.id, resolved)

        try:
            queue_friend_request(resolved)
        except Exception as exc:
            log.exception("Konnte Steam-Freundschaftsanfrage nicht einreihen")
            _update_invite(
                record.id,
                status=STATUS_ERROR,
                last_error=f"Konnte Freundschaftsanfrage nicht vormerken: {exc}",
            )
            await interaction.followup.send(
                "❌ Konnte die Freundschaftsanfrage nicht vormerken. Bitte versuche es später erneut.",
                ephemeral=True,
            )
            return

        now_ts = int(time.time())
        record = _update_invite(
            record.id,
            status=STATUS_WAITING,
            account_id=account_id,
            friend_requested_at=now_ts,
            last_error=None,
        ) or record

        message = (
            "✅ Freundschaftsanfrage verschickt!\n"
            "Sobald du die Anfrage angenommen hast, klicke unten auf „Freundschaft bestätigt“, damit wir die Einladung senden können."
        )
        view = BetaInviteConfirmView(self, record.id, interaction.user.id, resolved)
        await interaction.followup.send(message, view=view, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(BetaInviteFlow(bot))
