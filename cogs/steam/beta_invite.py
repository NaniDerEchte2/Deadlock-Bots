from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

import asyncio
import discord
from discord import app_commands
from discord.ext import commands

from service import db
from cogs.steam import SCHNELL_LINK_AVAILABLE, SchnellLinkButton
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

_trace_log = logging.getLogger(f"{__name__}.trace")
if not _trace_log.handlers:
    logs_dir = Path(__file__).resolve().parents[2] / "logs"
    logs_dir.mkdir(exist_ok=True)
    handler = RotatingFileHandler(
        logs_dir / "beta_invite_trace.log",
        maxBytes=512 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    )
    _trace_log.addHandler(handler)
    _trace_log.setLevel(logging.INFO)
    _trace_log.propagate = False

def _trace(event: str, **fields: Any) -> None:
    payload = {"event": event}
    payload.update(fields)
    try:
        _trace_log.info(json.dumps(payload, ensure_ascii=False, default=str))
    except Exception:
        log.debug("Trace log failed", exc_info=True)

STEAM64_BASE = 76561197960265728

STATUS_PENDING = "pending"
STATUS_WAITING = "waiting_friend"
STATUS_INVITE_SENT = "invite_sent"
STATUS_ERROR = "error"

SERVER_LEAVE_BAN_REASON = "Ausschluss aus der Community wegen Leaven des Servers"

_ALLOWED_UPDATE_FIELDS = {
    "status",
    "last_error",
    "friend_requested_at",
    "friend_confirmed_at",
    "invite_sent_at",
    "last_notified_at",
    "account_id",
}

def _ensure_invite_audit_table() -> None:
    with db.get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS beta_invite_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER,
                discord_id INTEGER NOT NULL,
                discord_name TEXT,
                steam_id64 TEXT NOT NULL,
                steam_profile TEXT NOT NULL,
                invited_at INTEGER NOT NULL
            )
            """
        )


def _format_discord_name(user: discord.abc.User) -> str:
    try:
        if getattr(user, "global_name", None):
            return str(user.global_name)
        discrim = getattr(user, "discriminator", None)
        if discrim and discrim != "0":
            return f"{user.name}#{discrim}"
        display = getattr(user, "display_name", None)
        if display:
            return str(display)
        return str(user.name)
    except Exception:
        return str(getattr(user, "name", "unknown"))


def _log_invite_grant(
    guild_id: Optional[int],
    discord_id: int,
    discord_name: str,
    steam_id64: str,
    invited_at: int,
) -> None:
    _ensure_invite_audit_table()
    profile_url = f"https://steamcommunity.com/profiles/{steam_id64}"
    with db.get_conn() as conn:
        conn.execute(
            """
            INSERT INTO beta_invite_audit
            (guild_id, discord_id, discord_name, steam_id64, steam_profile, invited_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                int(guild_id) if guild_id else None,
                int(discord_id),
                discord_name,
                steam_id64,
                profile_url,
                int(invited_at),
            ),
        )


def _has_successful_invite(discord_id: int) -> bool:
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM steam_beta_invites WHERE discord_id=? AND status=? LIMIT 1",
            (int(discord_id), STATUS_INVITE_SENT),
        ).fetchone()
    return row is not None


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


INTENT_COMMUNITY = "community"
INTENT_INVITE_ONLY = "invite_only"


@dataclass(slots=True)
class BetaIntentDecision:
    discord_id: int
    intent: str
    decided_at: int
    locked: bool


def _intent_row_to_record(row: Optional[db.sqlite3.Row]) -> Optional[BetaIntentDecision]:  # type: ignore[attr-defined]
    if row is None:
        return None
    return BetaIntentDecision(
        discord_id=int(row["discord_id"]),
        intent=str(row["intent"]),
        decided_at=int(row["decided_at"]),
        locked=bool(row["locked"]),
    )


def _get_intent_record(discord_id: int) -> Optional[BetaIntentDecision]:
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT discord_id, intent, decided_at, locked FROM beta_invite_intent WHERE discord_id = ?",
            (int(discord_id),),
        ).fetchone()
    return _intent_row_to_record(row)


def _persist_intent_once(discord_id: int, intent: str) -> BetaIntentDecision:
    existing = _get_intent_record(discord_id)
    if existing:
        return existing

    now_ts = int(time.time())
    with db.get_conn() as conn:
        conn.execute(
            """
            INSERT INTO beta_invite_intent(discord_id, intent, decided_at, locked)
            VALUES (?, ?, ?, 1)
            """,
            (int(discord_id), str(intent), now_ts),
        )
        row = conn.execute(
            "SELECT discord_id, intent, decided_at, locked FROM beta_invite_intent WHERE discord_id = ?",
            (int(discord_id),),
        ).fetchone()

    record = _intent_row_to_record(row)
    if record is None:  # pragma: no cover - defensive
        raise RuntimeError("Konnte beta_invite_intent-Eintrag nicht erstellen")
    return record


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


class BetaIntentGateView(discord.ui.View):
    def __init__(self, cog: "BetaInviteFlow", requester_id: int) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.requester_id = requester_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Nur der ursprngliche Nutzer kann diese Auswahl treffen.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Ich will mitspielen/aktiv sein", style=discord.ButtonStyle.success)
    async def choose_join(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.handle_intent_selection(interaction, INTENT_COMMUNITY)

    @discord.ui.button(label="Nur schnell den Invite abholen", style=discord.ButtonStyle.secondary)
    async def choose_invite_only(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.handle_intent_selection(interaction, INTENT_INVITE_ONLY)


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
        _ensure_invite_audit_table()

    async def _reply_no_invites(self, interaction: discord.Interaction) -> None:
        _trace("betainvite_unavailable_start", discord_id=getattr(interaction.user, "id", None))
        await asyncio.sleep(10)
        message = (
            "Sorry, aktuell sind keine Deadlock-Einladungen verfgbar. "
            "Wir melden uns, sobald wieder Pl„tze frei sind."
        )
        try:
            await interaction.followup.send(message, ephemeral=True)
        except Exception:
            log.debug("Konnte Unavailable-Antwort nicht senden", exc_info=True)
        _trace("betainvite_unavailable_done", discord_id=getattr(interaction.user, "id", None))

    async def _prompt_intent_gate(self, interaction: discord.Interaction) -> None:
        prompt = (
            "Kurze Frage bevor wir loslegen: Willst du wirklich mitspielen bzw. aktiv in der Community sein "
            "oder nur schnell einen Steam-Invite abholen? Deine Antwort wird gespeichert und kann nicht umgeschaltet werden."
        )
        view = BetaIntentGateView(self, interaction.user.id)
        _trace("betainvite_intent_prompt", discord_id=interaction.user.id)
        await interaction.followup.send(prompt, view=view, ephemeral=True)

    async def handle_intent_selection(self, interaction: discord.Interaction, intent_choice: str) -> None:
        if intent_choice not in (INTENT_COMMUNITY, INTENT_INVITE_ONLY):
            await interaction.response.send_message(
                "Ungltige Auswahl.",
                ephemeral=True,
            )
            return

        existing = _get_intent_record(interaction.user.id)
        if existing and existing.intent != intent_choice and existing.locked:
            await interaction.response.send_message(
                "Deine Entscheidung ist bereits gespeichert. Falls das ein Fehler ist, melde dich bei einem Mod.",
                ephemeral=True,
            )
            _trace(
                "betainvite_intent_locked",
                discord_id=interaction.user.id,
                intent=existing.intent,
            )
            return

        record = existing or _persist_intent_once(interaction.user.id, intent_choice)
        _trace(
            "betainvite_intent_saved",
            discord_id=interaction.user.id,
            intent=record.intent,
            locked=record.locked,
        )

        if intent_choice == INTENT_INVITE_ONLY:
            await interaction.response.edit_message(
                content=(
                    "Verstanden – aktuell geben wir Einladungen nur an Leute raus, die wirklich mitspielen wollen. "
                    "Deine Antwort ist gespeichert."
                ),
                view=None,
            )
            return

        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except discord.errors.NotFound:
            await interaction.followup.send(
                "Die Auswahl ist abgelaufen. Bitte starte `/betainvite` erneut.",
                ephemeral=True,
            )
            return
        except Exception as exc:
            log.error("Failed to defer intent interaction: %s", exc)
            return

        try:
            await interaction.message.edit(view=None)
        except Exception:
            log.debug("Konnte Intent-View nicht entfernen", exc_info=True)

        await self._reply_no_invites(interaction)

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

    async def _process_invite_request(self, interaction: discord.Interaction) -> None:
        try:
            existing = _fetch_invite_by_discord(interaction.user.id)
            primary_link = _lookup_primary_steam_id(interaction.user.id)
            resolved = primary_link or (existing.steam_id64 if existing else None)
        except Exception as e:
            log.error(f"Database lookup failed: {e}")
            _trace(
                "betainvite_db_error",
                discord_id=getattr(interaction.user, "id", None),
                error=str(e),
            )
            await interaction.followup.send(
                "? Datenbankfehler beim Abrufen der Steam-Verknpfung. Bitte versuche es erneut.",
                ephemeral=True
            )
            return

        if not resolved:
            view = self._build_link_prompt_view(interaction.user)
            prompt = (
                "?? Es ist noch kein Steam-Account mit deinem Discord verknpft.\n"
                "Melde dich mit den unten verfgbaren Optionen bei Steam an, und nachdem du dies getan hast fhre /betainvite erneut aus."
            )
            if SCHNELL_LINK_AVAILABLE:
                prompt += ""
            else:
                prompt += "Der Schnell-Link ist derzeit nicht verfgbar"
            prompt += ""
            _trace(
                "betainvite_no_link",
                discord_id=interaction.user.id,
            )

            await interaction.followup.send(
                prompt,
                view=view,
                ephemeral=True,
            )
            return

        try:
            account_id = steam64_to_account_id(resolved)
        except ValueError as exc:
            log.warning("Gespeicherte SteamID ungltig", exc_info=True)
            _trace(
                "betainvite_invalid_steamid",
                discord_id=interaction.user.id,
                steam_id=resolved,
                error=str(exc),
            )
            await interaction.followup.send(
                f"? Gespeicherte SteamID ist ungltig: {exc}. Bitte verknpfe deinen Account erneut.",
                ephemeral=True,
            )
            return

        if existing and existing.status == STATUS_INVITE_SENT and existing.steam_id64 == resolved:
            await interaction.followup.send(
                "? Du bist bereits eingeladen. Prfe unter https://store.steampowered.com/account/playtestinvites .",
                ephemeral=True,
            )
            _trace(
                "betainvite_already_invited",
                discord_id=interaction.user.id,
                steam_id64=resolved,
            )
            return

        if not existing or existing.steam_id64 != resolved:
            record = _create_or_reset_invite(interaction.user.id, resolved, account_id)
            _trace(
                "betainvite_record_created",
                discord_id=interaction.user.id,
                steam_id64=resolved,
                account_id=account_id,
            )
        else:
            record = existing
            if record.account_id != account_id:
                record = _update_invite(record.id, account_id=account_id) or record

        if record.status == STATUS_INVITE_SENT and record.steam_id64 == resolved:
            await interaction.followup.send(
                "? Du bist bereits eingeladen. Prfe unter https://store.steampowered.com/account/playtestinvites .",
                ephemeral=True,
            )
            _trace(
                "betainvite_already_invited_existing",
                discord_id=interaction.user.id,
                steam_id64=resolved,
            )
            return

        friend_ok = False
        account_id_from_friend: Optional[int] = None
        try:
            precheck_outcome = await self.tasks.run(
                "AUTH_CHECK_FRIENDSHIP",
                {"steam_id": resolved},
                timeout=15.0,
            )
            if precheck_outcome.ok and isinstance(precheck_outcome.result, dict):
                data = precheck_outcome.result.get("data") if isinstance(precheck_outcome.result, dict) else None
                if isinstance(data, dict):
                    try:
                        if data.get("account_id") is not None:
                            account_id_from_friend = int(data["account_id"])
                    except Exception:
                        account_id_from_friend = None
                    if account_id_from_friend is not None and account_id_from_friend != record.account_id:
                        record = _update_invite(record.id, account_id=account_id_from_friend) or record
                    friend_ok = bool(data.get("friend"))
            _trace(
                "betainvite_friend_precheck",
                discord_id=interaction.user.id,
                steam_id64=resolved,
                ok=precheck_outcome.ok if "precheck_outcome" in locals() else None,
                status=getattr(precheck_outcome, "status", None),
                friend=friend_ok,
                account_id=account_id_from_friend,
                error=getattr(precheck_outcome, "error", None),
            )
        except Exception:
            log.exception(
                "Friendship pre-check fr betainvite fehlgeschlagen",
                extra={"discord_id": interaction.user.id, "steam_id": resolved},
            )
            _trace(
                "betainvite_friend_precheck_error",
                discord_id=interaction.user.id,
                steam_id64=resolved,
            )

        if friend_ok:
            await self._send_invite_after_friend(
                interaction,
                record,
                account_id_hint=account_id_from_friend,
            )
            _trace(
                "betainvite_friend_ok_direct_invite",
                discord_id=interaction.user.id,
                steam_id64=resolved,
                account_id=account_id_from_friend,
            )
            return

        try:
            fr_outcome = await self.tasks.run(
                "AUTH_SEND_FRIEND_REQUEST",
                {"steam_id": resolved},
                timeout=20.0,
            )
        except Exception as exc:
            log.exception("Konnte Steam-Freundschaftsanfrage nicht senden")
            _trace(
                "friend_request_exception",
                discord_id=interaction.user.id,
                steam_id64=resolved,
                error=str(exc),
            )
            _update_invite(
                record.id,
                status=STATUS_ERROR,
                last_error=f"Freundschaftsanfrage fehlgeschlagen: {exc}",
            )
            await interaction.followup.send(
                "? Konnte die Freundschaftsanfrage nicht senden. Bitte versuche es sp„ter erneut.",
                ephemeral=True,
            )
            return

        if not fr_outcome.ok:
            error_msg = fr_outcome.error or "Unbekannter Fehler beim Senden der Freundschaftsanfrage"
            error_lower = str(error_msg).lower()
            duplicate_request = any(
                token in error_lower
                for token in (
                    "duplicatename",
                    "duplicate name",
                    "already friend",
                    "already friends",
                    "already on your friend",
                    "already in your friend",
                )
            )

            if duplicate_request:
                friend_ok = False
                friendship_details = {}
                try:
                    friend_outcome = await self.tasks.run(
                        "AUTH_CHECK_FRIENDSHIP",
                        {"steam_id": resolved},
                        timeout=15.0,
                    )
                    if friend_outcome.ok and isinstance(friend_outcome.result, dict):
                        data = friend_outcome.result.get("data") if isinstance(friend_outcome.result, dict) else None
                        if isinstance(data, dict):
                            friendship_details = data
                            friend_ok = bool(data.get("friend"))
                            if data.get("account_id") is not None and data.get("account_id") != record.account_id:
                                record = _update_invite(record.id, account_id=int(data["account_id"])) or record
                except Exception:
                    log.exception(
                        "Friendship re-check nach DuplicateName fehlgeschlagen: discord_id=%s, steam_id=%s",
                        interaction.user.id,
                        resolved,
                    )
                    _trace(
                        "friend_request_duplicate_check_failed",
                        discord_id=interaction.user.id,
                        steam_id64=resolved,
                    )

                log.info(
                    "Freundschaftsanfrage bereits vorhanden oder Freundschaft besteht: discord_id=%s, steam_id=%s, error=%s, friend_ok=%s, details=%s",
                    interaction.user.id,
                    resolved,
                    error_msg,
                    friend_ok,
                    friendship_details,
                )
                _trace(
                    "friend_request_duplicate",
                    discord_id=interaction.user.id,
                    steam_id64=resolved,
                    error=error_msg,
                    friend_ok=friend_ok,
                    details=friendship_details,
                )

                now_ts = int(time.time())
                record = _update_invite(
                    record.id,
                    status=STATUS_WAITING,
                    account_id=account_id,
                    friend_requested_at=now_ts,
                    last_error=None,
                ) or record

                status_line = "? Wir sind laut Steam schon befreundet." if friend_ok else "?? Die Steam-Anfrage scheint bereits zu bestehen."
                message = (
                    f"{status_line}\n"
                    "Klicke unten auf \"Freundschaft best„tigt\", dann schicken wir dir den Deadlock-Invite."
                )
                view = BetaInviteConfirmView(self, record.id, interaction.user.id, resolved)
                await interaction.followup.send(message, view=view, ephemeral=True)
                return

            log.warning(
                "Freundschaftsanfrage fehlgeschlagen: discord_id=%s, steam_id=%s, error=%s",
                interaction.user.id,
                resolved,
                error_msg,
            )
            _trace(
                "friend_request_failed",
                discord_id=interaction.user.id,
                steam_id64=resolved,
                error=error_msg,
            )
            _update_invite(
                record.id,
                status=STATUS_ERROR,
                last_error=f"Freundschaftsanfrage fehlgeschlagen: {error_msg}",
            )
            await interaction.followup.send(
                "? Konnte die Freundschaftsanfrage nicht senden. Bitte prfe deine Steam-Privatsph„reeinstellungen und versuche es erneut.",
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
        _trace(
            "friend_request_sent",
            discord_id=interaction.user.id,
            steam_id64=resolved,
            account_id=account_id,
            record_id=record.id,
        )

        message = (
            "? Freundschaftsanfrage verschickt!\n"
            "Sobald du die Anfrage angenommen hast, klicke unten auf \"Freundschaft best„tigt\", damit wir die Einladung senden k”nnen."
        )
        view = BetaInviteConfirmView(self, record.id, interaction.user.id, resolved)
        await interaction.followup.send(message, view=view, ephemeral=True)

    def _record_successful_invite(
        self,
        interaction: discord.Interaction,
        record: BetaInviteRecord,
        invited_at: int,
    ) -> None:
        try:
            _log_invite_grant(
                guild_id=int(interaction.guild.id) if interaction.guild else None,
                discord_id=int(interaction.user.id),
                discord_name=_format_discord_name(interaction.user),
                steam_id64=record.steam_id64,
                invited_at=int(invited_at),
            )
        except Exception:
            log.exception(
                "BetaInvite: Protokollieren der Einladung für Nutzer %s fehlgeschlagen",
                getattr(interaction.user, "id", "?"),
            )

    async def _send_invite_after_friend(
        self,
        interaction: discord.Interaction,
        record: BetaInviteRecord,
        *,
        account_id_hint: Optional[int] = None,
    ) -> bool:
        _trace(
            "invite_start",
            discord_id=record.discord_id,
            steam_id64=record.steam_id64,
            account_id_hint=account_id_hint,
            record_status=record.status,
        )
        now_ts = int(time.time())
        record = _update_invite(
            record.id,
            status=STATUS_WAITING,
            friend_confirmed_at=now_ts,
            last_error=None,
        ) or record

        account_id = account_id_hint or record.account_id or steam64_to_account_id(record.steam_id64)

        log.info(
            "Sending Steam invite: discord_id=%s, steam_id64=%s, account_id=%s",
            record.discord_id,
            record.steam_id64,
            account_id
        )
        _trace(
            "invite_send",
            discord_id=record.discord_id,
            steam_id64=record.steam_id64,
            account_id=account_id,
        )

        invite_timeout_ms = 30000
        gc_ready_timeout_ms = 20000
        invite_attempts = 1
        gc_ready_attempts = 1
        runtime_budget_ms = (
            gc_ready_timeout_ms * max(gc_ready_attempts, 1)
            + invite_timeout_ms * max(invite_attempts, 1)
        )
        invite_task_timeout = min(120.0, max(60.0, runtime_budget_ms / 1000 + 15.0))

        log.info(
            "Steam invite timing config: invite_timeout_ms=%s, gc_ready_timeout_ms=%s, invite_attempts=%s, gc_ready_attempts=%s, task_timeout=%s",
            invite_timeout_ms, gc_ready_timeout_ms, invite_attempts, gc_ready_attempts, invite_task_timeout
        )

        invite_outcome = await self.tasks.run(
            "AUTH_SEND_PLAYTEST_INVITE",
            {
                "steam_id": record.steam_id64,
                "account_id": account_id,
                "location": "discord-betainvite",
                "timeout_ms": invite_timeout_ms,
                "retry_attempts": invite_attempts,
                "gc_ready_timeout_ms": gc_ready_timeout_ms,
                "gc_ready_retry_attempts": gc_ready_attempts,
            },
            timeout=invite_task_timeout,
        )

        if invite_outcome.timed_out and str(invite_outcome.status or "").upper() == "RUNNING":
            log.warning(
                "Steam invite task %s still running after initial timeout, extending wait by %.1fs",
                getattr(invite_outcome, "task_id", "?"),
                invite_task_timeout,
            )
            try:
                invite_outcome = await self.tasks.wait(
                    invite_outcome.task_id,
                    timeout=invite_task_timeout,
                )
            except Exception:
                log.exception("Extended wait for Steam invite task failed")
        
        # Log das Ergebnis für bessere Diagnose
        log.info(
            "Steam invite result: ok=%s, status=%s, timed_out=%s",
            invite_outcome.ok, invite_outcome.status, invite_outcome.timed_out
        )
        _trace(
            "invite_result",
            discord_id=record.discord_id,
            steam_id64=record.steam_id64,
            ok=invite_outcome.ok,
            status=invite_outcome.status,
            timed_out=invite_outcome.timed_out,
            error=invite_outcome.error,
            result=invite_outcome.result,
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
                            error_text = "⚠️ Deadlock Game Coordinator ist überlastet. Bitte versuche es in 10-15 Minuten erneut."
                        else:
                            error_text = "⚠️ Timeout beim Warten auf Steam-Antwort. Bitte versuche es erneut."
                    elif "already has game" in error_lower or "already has access" in error_lower:
                        error_text = "✅ Account besitzt bereits Deadlock-Zugang"
                    elif "invite limit" in error_lower or "limit reached" in error_lower:
                        error_text = "⚠️ Tägliches Invite-Limit erreicht. Bitte morgen erneut versuchen."
                    elif "not friends long enough" in error_lower:
                        error_text = "ℹ️ Steam-Freundschaft muss mindestens 30 Tage bestehen"
                    elif "limited user" in error_lower or "restricted account" in error_lower:
                        error_text = "⚠️ Steam-Account ist eingeschränkt (Limited User). Aktiviere deinen Account in Steam."
                    elif "invalid friend" in error_lower:
                        error_text = "ℹ️ Accounts sind nicht als Steam-Freunde verknüpft"
            
            # Spezielle Behandlung für Timeout-Fälle
            if is_timeout and "timeout" not in error_text.lower():
                error_text = f"⚠️ Timeout: {error_text}"

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
                f"⚠️ Ein Problem ist aufgetreten - der Invite hat nicht geklappt. Bitte wende dich an {BETA_INVITE_SUPPORT_CONTACT}.",
                ephemeral=True,
            )
            _trace(
                "invite_failed",
                discord_id=record.discord_id,
                steam_id64=record.steam_id64,
                error_text=error_text,
                timed_out=is_timeout,
                task_status=invite_outcome.status,
            )
            return False

        record = _update_invite(
            record.id,
            status=STATUS_INVITE_SENT,
            invite_sent_at=now_ts,
            last_notified_at=now_ts,
            last_error=None,
        ) or record
        self._record_successful_invite(interaction, record, now_ts)
        _trace(
            "invite_sent",
            discord_id=record.discord_id,
            steam_id64=record.steam_id64,
            invite_sent_at=now_ts,
        )

        message = (
            "✅ Einladung verschickt!\n"
            "Bitte schaue in 1-2 Stunden unter https://store.steampowered.com/account/playtestinvites "
            "und nimm die Einladung dort an. Danach erscheint Deadlock automatisch in deiner Bibliothek.\n"
            f"Alle weiteren Infos findest du in <{BETA_INVITE_CHANNEL_URL}> - bei Problemen ping bitte {BETA_INVITE_SUPPORT_CONTACT}.\n"
            "⚠️ Verlässt du den Server wird der Invite ungültig, egal ob dein Invite noch aussteht oder du Deadlock schon hast."
        )
        await interaction.followup.send(message, ephemeral=True)

        try:
            await interaction.user.send(message)
        except Exception:  # pragma: no cover - DM optional
            log.debug("Konnte Bestätigungs-DM nicht senden", exc_info=True)

        return True

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
        _trace(
            "confirm_start",
            discord_id=interaction.user.id,
            steam_id64=record.steam_id64,
            record_status=record.status,
        )

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
        account_id_from_friend: Optional[int] = None
        if friend_outcome.ok and friend_outcome.result:
            data = friend_outcome.result.get("data") if isinstance(friend_outcome.result, dict) else None
            if isinstance(data, dict):
                friend_ok = bool(data.get("friend"))
                relationship_name = str(data.get("relationship_name") or relationship_name)
                friend_source = str(data.get("friend_source") or "unknown")
                cache_age = data.get("webapi_cache_age_ms")
                try:
                    if data.get("account_id") is not None:
                        account_id_from_friend = int(data["account_id"])
                except Exception:
                    account_id_from_friend = None
                
                # Debug-Logging für Freundschaftsstatus
                log.info(
                    "Friendship check: discord_id=%s, steam_id64=%s, friend_ok=%s, relationship=%s, source=%s, cache_age_ms=%s",
                    record.discord_id, record.steam_id64, friend_ok, relationship_name, friend_source, cache_age
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
        _trace(
            "confirm_friend_status",
            discord_id=record.discord_id,
            steam_id64=record.steam_id64,
            ok=friend_outcome.ok,
            status=getattr(friend_outcome, "status", None),
            friend=friend_ok,
            relationship=relationship_name,
            error=getattr(friend_outcome, "error", None),
            account_id=record.account_id,
        )
        if not friend_ok:
            await interaction.followup.send(
                "ℹ️ Wir sind noch keine bestätigten Steam-Freunde. Bitte nimm die Freundschaftsanfrage an und probiere es erneut.",
                ephemeral=True,
            )
            _trace(
                "confirm_not_friend",
                discord_id=record.discord_id,
                steam_id64=record.steam_id64,
            )
            return
        await self._send_invite_after_friend(
            interaction,
            record,
            account_id_hint=account_id_from_friend,
        )

    @app_commands.command(name="betainvite", description="Automatisiert eine Deadlock-Playtest-Einladung anfordern.")
    async def betainvite(self, interaction: discord.Interaction) -> None:
        # Quick initial response to prevent timeout
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except discord.errors.NotFound:
            log.warning("Interaction expired before defer, using followup")
            _trace("betainvite_defer_expired", discord_id=interaction.user.id)
            await interaction.followup.send(
                "?? Die Anfrage hat zu lange gedauert. Bitte versuche `/betainvite` erneut.",
                ephemeral=True
            )
            return
        except Exception as e:
            log.error(f"Failed to defer interaction: {e}")
            _trace("betainvite_defer_error", discord_id=getattr(interaction.user, "id", None), error=str(e))
            return

        intent_record = _get_intent_record(interaction.user.id)
        if intent_record is None:
            await self._prompt_intent_gate(interaction)
            return
        if intent_record.intent == INTENT_INVITE_ONLY:
            await interaction.followup.send(
                "Du hattest angegeben, nur den Invite abholen zu wollen. Aktuell vergeben wir Einladungen nur an Leute, die mitspielen wollen.",
                ephemeral=True,
            )
            _trace(
                "betainvite_intent_blocked",
                discord_id=interaction.user.id,
                intent=intent_record.intent,
            )
            return

        _trace(
            "betainvite_intent_ok",
            discord_id=interaction.user.id,
            intent=intent_record.intent,
        )
        await self._reply_no_invites(interaction)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        try:
            invited = _has_successful_invite(member.id)
        except Exception:
            log.exception("BetaInvite: Konnte Invite-Status für %s nicht prüfen", member.id)
            return
        if not invited or not member.guild:
            return
        try:
            await member.guild.ban(member, reason=SERVER_LEAVE_BAN_REASON, delete_message_seconds=0)
            log.info("BetaInvite: %s wurde wegen Server-Verlassen nach Invite gebannt.", member.id)
        except discord.Forbidden:
            log.warning("BetaInvite: Fehlende Rechte um %s zu bannen.", member.id)
        except discord.HTTPException as exc:
            log.warning("BetaInvite: HTTP-Fehler beim Bannen von %s: %s", member.id, exc)


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
