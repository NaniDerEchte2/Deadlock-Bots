from __future__ import annotations

import asyncio
import json
import logging
from logging.handlers import RotatingFileHandler
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

import discord
from discord import app_commands
from discord.ext import commands

from service import db
from cogs.steam import SCHNELL_LINK_AVAILABLE, SchnellLinkButton
from cogs.steam.friend_requests import queue_friend_request
from cogs.steam.steam_master import SteamTaskClient
from cogs.welcome_dm import base as welcome_base

BETA_INVITE_CHANNEL_URL = getattr(
    welcome_base,
    "BETA_INVITE_CHANNEL_URL",
    "https://discord.com/channels/1289721245281292288/1428745737323155679",
)
BETA_INVITE_SUPPORT_CONTACT = getattr(
    welcome_base,
    "BETA_INVITE_SUPPORT_CONTACT",
    "@earlysalty",
)

log = logging.getLogger(__name__)

_failure_log = logging.getLogger(f"{__name__}.failures")
if not _failure_log.handlers:
    logs_dir = Path(__file__).resolve().parents[2] / "logs"
    logs_dir.mkdir(exist_ok=True)
    handler = RotatingFileHandler(
        logs_dir / "beta_invite_failures.log",
        maxBytes=512 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    )
    _failure_log.addHandler(handler)
    _failure_log.setLevel(logging.INFO)
    _failure_log.propagate = False

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


def _lookup_primary_steam_id(discord_id: int) -> Optional[str]:
    with db.get_conn() as conn:
        row = conn.execute(
            """
            SELECT steam_id
            FROM steam_links
            WHERE user_id = ? AND steam_id != ''
            ORDER BY primary_account DESC, verified DESC, updated_at DESC
            LIMIT 1
            """,
            (int(discord_id),),
        ).fetchone()
    if not row:
        return None
    steam_id = str(row["steam_id"] or "").strip()
    return steam_id or None


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
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM steam_beta_invites WHERE discord_id = ?",
            (int(discord_id),),
        ).fetchone()
    return _row_to_record(row)


def _fetch_invite_by_id(record_id: int) -> Optional[BetaInviteRecord]:
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM steam_beta_invites WHERE id = ?",
            (int(record_id),),
        ).fetchone()
    return _row_to_record(row)


def _format_gc_response_error(response: Mapping[str, Any]) -> Optional[str]:
    message = str(response.get("message") or "").strip()

    code_text: Optional[str] = None
    if "code" in response:
        raw_code = response.get("code")
        try:
            code_value = int(str(raw_code))
        except (TypeError, ValueError):
            code_candidate = str(raw_code or "").strip()
            code_text = f"Code {code_candidate}" if code_candidate else None
        else:
            code_text = f"Code {code_value}"

    key_text = str(response.get("key") or "").strip()

    parts: list[str] = []
    if message:
        parts.append(message)

    meta_parts = [part for part in [code_text, key_text if key_text else None] if part]
    if meta_parts:
        parts.append(f"({' / '.join(meta_parts)})")

    formatted = " ".join(parts).strip()
    return formatted or None


def _create_or_reset_invite(discord_id: int, steam_id64: str, account_id: Optional[int]) -> BetaInviteRecord:
    with db.get_conn() as conn:
        conn.execute(
            """
            DELETE FROM steam_beta_invites
            WHERE steam_id64 = ? AND discord_id != ?
            """,
            (str(steam_id64), int(discord_id)),
        )
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

    with db.get_conn() as conn:
        conn.execute(
            f"UPDATE steam_beta_invites SET {', '.join(assignments)} WHERE id = ?",
            params,
        )
        row = conn.execute(
            "SELECT * FROM steam_beta_invites WHERE id = ?",
            (int(record_id),),
        ).fetchone()
    return _row_to_record(row)


def steam64_to_account_id(steam_id64: str) -> int:
    """
    Konvertiert Steam ID64 zu Account ID für Steam API Calls.
    
    Args:
        steam_id64: Steam ID64 als String (z.B. "76561199678060816")
        
    Returns:
        Account ID als Integer (z.B. 1717795088)
        
    Raises:
        ValueError: Bei ungültiger Steam ID64
    """
    try:
        value = int(str(steam_id64))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"SteamID64 muss numerisch sein: {steam_id64}") from exc
    
    if value < STEAM64_BASE:
        raise ValueError(f"SteamID64 {value} liegt unterhalb des gültigen Bereichs (min: {STEAM64_BASE})")
    
    # Zusätzliche Validierung für vernünftige Obergrenze
    max_reasonable = STEAM64_BASE + 2**32  # Ungefähr bis 2038
    if value > max_reasonable:
        raise ValueError(f"SteamID64 {value} liegt oberhalb des erwarteten Bereichs (max: {max_reasonable})")
    
    account_id = value - STEAM64_BASE
    log.debug("Steam ID conversion: %s -> %s", steam_id64, account_id)
    return account_id


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

    def _build_link_prompt_view(self, user: discord.abc.User) -> discord.ui.View:
        login_url: Optional[str] = None
        steam_url: Optional[str] = None
        try:
            steam_cog = self.bot.get_cog("SteamLink")
        except Exception:  # pragma: no cover - defensive
            steam_cog = None

        if steam_cog and hasattr(steam_cog, "discord_start_url_for"):
            try:
                candidate = steam_cog.discord_start_url_for(int(user.id))  # type: ignore[attr-defined]
                login_url = str(candidate) or None
            except Exception:
                log.debug("Konnte Discord-Link für BetaInvite nicht bauen", exc_info=True)

        if steam_cog and hasattr(steam_cog, "steam_start_url_for"):
            try:
                candidate = steam_cog.steam_start_url_for(int(user.id))  # type: ignore[attr-defined]
                steam_url = str(candidate) or None
            except Exception:
                log.debug("Konnte Steam-Link für BetaInvite nicht bauen", exc_info=True)

        view = discord.ui.View(timeout=180)
        if login_url:
            view.add_item(
                discord.ui.Button(
                    label="Via Discord bei Steam anmelden",
                    style=discord.ButtonStyle.link,
                    url=login_url,
                    emoji="🔗",
                    row=0,
                )
            )
        else:
            view.add_item(
                discord.ui.Button(
                    label="Via Discord bei Steam anmelden",
                    style=discord.ButtonStyle.secondary,
                    disabled=True,
                    emoji="🔗",
                    row=0,
                )
            )

        if steam_url:
            view.add_item(
                discord.ui.Button(
                    label="Direkt bei Steam anmelden",
                    style=discord.ButtonStyle.link,
                    url=steam_url,
                    emoji="🎮",
                    row=0,
                )
            )
        else:
            view.add_item(
                discord.ui.Button(
                    label="Direkt bei Steam anmelden",
                    style=discord.ButtonStyle.secondary,
                    disabled=True,
                    emoji="🎮",
                    row=0,
                )
            )

        view.add_item(SchnellLinkButton(row=1, source="beta_invite_prompt"))
        return view

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

        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except discord.errors.NotFound:
            log.warning("Confirmation interaction expired before defer")
            await interaction.followup.send(
                "⏱️ Die Bestätigung hat zu lange gedauert. Bitte versuche es erneut.",
                ephemeral=True
            )
            return
        except Exception as e:
            log.error(f"Failed to defer confirmation interaction: {e}")
            return

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
                
                # Debug-Logging für Freundschaftsstatus
                log.info(
                    "Friendship check: discord_id=%s, steam_id64=%s, friend_ok=%s, relationship=%s",
                    record.discord_id, record.steam_id64, friend_ok, relationship_name
                )
                
                if data.get("account_id") is not None and data.get("account_id") != record.account_id:
                    log.info(
                        "Account ID updated: old=%s, new=%s",
                        record.account_id, data.get("account_id")
                    )
                    record = _update_invite(record.id, account_id=int(data["account_id"])) or record
        else:
            log.warning(
                "Friendship check failed: discord_id=%s, steam_id64=%s, ok=%s, error=%s",
                record.discord_id, record.steam_id64, friend_outcome.ok, friend_outcome.error
            )
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
        
        # Debug-Logging für bessere Nachverfolgung
        log.info(
            "Sending Steam invite: discord_id=%s, steam_id64=%s, account_id=%s",
            record.discord_id, record.steam_id64, account_id
        )
        
        # Use longer timeouts to handle Game Coordinator delays
        invite_outcome = await self.tasks.run(
            "AUTH_SEND_PLAYTEST_INVITE",
            {
                "steam_id": record.steam_id64,
                "account_id": account_id,
                "location": "discord-betainvite",
                "timeout_ms": 45000,  # Increased from 15s to 45s
            },
            timeout=60.0,  # Increased from 25s to 60s
        )
        
        # Log das Ergebnis für bessere Diagnose
        log.info(
            "Steam invite result: ok=%s, status=%s, timed_out=%s",
            invite_outcome.ok, invite_outcome.status, invite_outcome.timed_out
        )

        if not invite_outcome.ok:
            error_text = invite_outcome.error or "Game Coordinator hat die Einladung abgelehnt."
            is_timeout = invite_outcome.timed_out
            
            # Verbesserte Fehlerbehandlung für spezifische Steam GC Errors
            if invite_outcome.result and isinstance(invite_outcome.result, dict):
                result_error = invite_outcome.result.get("error")
                if result_error:
                    candidate = str(result_error).strip()
                    if candidate:
                        error_text = candidate
                        
                data = invite_outcome.result.get("data")
                if isinstance(data, dict):
                    response = data.get("response")
                    if isinstance(response, Mapping):
                        formatted = _format_gc_response_error(response)
                        if formatted:
                            error_text = formatted
                    
                    # Spezielle Behandlung für bekannte Deadlock GC Probleme
                    error_lower = str(result_error or error_text).lower()
                    if "timeout" in error_lower or is_timeout:
                        if "deadlock" in error_lower or "gc" in error_lower:
                            error_text = "⏱️ Deadlock Game Coordinator ist überlastet. Bitte versuche es in 10-15 Minuten erneut."
                        else:
                            error_text = "⏱️ Timeout beim Warten auf Steam-Antwort. Bitte versuche es erneut."
                    elif "already has game" in error_lower or "already has access" in error_lower:
                        error_text = "✅ Account besitzt bereits Deadlock-Zugang"
                    elif "invite limit" in error_lower or "limit reached" in error_lower:
                        error_text = "📊 Tägliches Invite-Limit erreicht. Bitte morgen erneut versuchen."
                    elif "not friends long enough" in error_lower:
                        error_text = "⏰ Steam-Freundschaft muss mindestens 30 Tage bestehen"
                    elif "limited user" in error_lower or "restricted account" in error_lower:
                        error_text = "🔒 Steam-Account ist eingeschränkt (Limited User). Aktiviere deinen Account in Steam."
                    elif "invalid friend" in error_lower:
                        error_text = "👥 Accounts sind nicht als Steam-Freunde verknüpft"
            
            # Spezielle Behandlung für Timeout-Fälle
            if is_timeout and "timeout" not in error_text.lower():
                error_text = f"⏱️ Timeout: {error_text}"

            details = {
                "discord_id": record.discord_id,
                "steam_id64": record.steam_id64,
                "account_id": account_id,
                "task_status": invite_outcome.status,
                "timed_out": invite_outcome.timed_out,
                "task_error": invite_outcome.error,
                "task_result": invite_outcome.result,
                "record_id": record.id,
                "error_text": error_text,
            }
            try:
                serialized_details = json.dumps(details, ensure_ascii=False, default=str)
            except TypeError:
                serialized_details = str(details)
            _failure_log.error("Invite task failed: %s", serialized_details)
            _update_invite(
                record.id,
                status=STATUS_ERROR,
                last_error=str(error_text),
            )
            await interaction.followup.send(
                f"❌ Ein Problem ist aufgetreten – der Invite hat nicht geklappt. Bitte wende dich an {BETA_INVITE_SUPPORT_CONTACT}.",
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
            "und nimm die Einladung dort an. Danach erscheint Deadlock automatisch in deiner Bibliothek.\n"
            f"Alle weiteren Infos findest du in <{BETA_INVITE_CHANNEL_URL}> – bei Problemen ping bitte {BETA_INVITE_SUPPORT_CONTACT}."
        )
        await interaction.followup.send(message, ephemeral=True)

        try:
            await interaction.user.send(message)
        except Exception:  # pragma: no cover - DM optional
            log.debug("Konnte Bestätigungs-DM nicht senden", exc_info=True)

    @app_commands.command(name="betainvite", description="Automatisiert eine Deadlock-Playtest-Einladung anfordern.")
    async def betainvite(self, interaction: discord.Interaction) -> None:
        # Quick initial response to prevent timeout
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except discord.errors.NotFound:
            # Interaction already expired, try to respond with followup
            log.warning("Interaction expired before defer, using followup")
            await interaction.followup.send(
                "⏱️ Die Anfrage hat zu lange gedauert. Bitte versuche `/betainvite` erneut.",
                ephemeral=True
            )
            return
        except Exception as e:
            log.error(f"Failed to defer interaction: {e}")
            return

        try:
            existing = _fetch_invite_by_discord(interaction.user.id)
            primary_link = _lookup_primary_steam_id(interaction.user.id)
            resolved = primary_link or (existing.steam_id64 if existing else None)
        except Exception as e:
            log.error(f"Database lookup failed: {e}")
            await interaction.followup.send(
                "❌ Datenbankfehler beim Abrufen der Steam-Verknüpfung. Bitte versuche es erneut.",
                ephemeral=True
            )
            return

        if not resolved:
            view = self._build_link_prompt_view(interaction.user)
            prompt = (
                "ℹ️ Es ist noch kein Steam-Account mit deinem Discord verknüpft.\n"
                "Melde dich mit den unten verfügbaren Optionen bei Steam an, und nachdem du dies getan hast führe /betainvite erneut aus."
            )
            if SCHNELL_LINK_AVAILABLE:
                prompt += ""
            else:
                prompt += "Der Schnell-Link ist derzeit nicht verfügbar"
            prompt += ""

            await interaction.followup.send(
                prompt,
                view=view,
                ephemeral=True,
            )
            return

        try:
            account_id = steam64_to_account_id(resolved)
        except ValueError as exc:
            log.warning("Gespeicherte SteamID ungültig", exc_info=True)
            await interaction.followup.send(
                f"❌ Gespeicherte SteamID ist ungültig: {exc}. Bitte verknüpfe deinen Account erneut.",
                ephemeral=True,
            )
            return

        if existing and existing.status == STATUS_INVITE_SENT and existing.steam_id64 == resolved:
            await interaction.followup.send(
                "✅ Du bist bereits eingeladen. Prüfe unter https://store.steampowered.com/account/playtestinvites .",
                ephemeral=True,
            )
            return

        if not existing or existing.steam_id64 != resolved:
            record = _create_or_reset_invite(interaction.user.id, resolved, account_id)
        else:
            record = existing
            if record.account_id != account_id:
                record = _update_invite(record.id, account_id=account_id) or record

        if record.status == STATUS_INVITE_SENT and record.steam_id64 == resolved:
            await interaction.followup.send(
                "✅ Du bist bereits eingeladen. Prüfe unter https://store.steampowered.com/account/playtestinvites .",
                ephemeral=True,
            )
            return

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
    beta_invite_cog = BetaInviteFlow(bot)
    await bot.add_cog(beta_invite_cog)

    try:
        bot.tree.add_command(beta_invite_cog.betainvite)
    except app_commands.CommandAlreadyRegistered:
        bot.tree.remove_command(
            beta_invite_cog.betainvite.name,
            type=discord.AppCommandType.chat_input,
        )
        bot.tree.add_command(beta_invite_cog.betainvite)
