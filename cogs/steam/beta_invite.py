from __future__ import annotations

import http.client
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import socket
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Optional, Union
from urllib.parse import parse_qs

import asyncio
import secrets
import discord
from discord import app_commands
from discord.ext import commands

from service import db
from cogs.steam.steam_master import SteamTaskClient
from cogs.welcome_dm import base as welcome_base

try:
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import JSONResponse
    import uvicorn
except Exception:  # pragma: no cover - optional dependency in some environments
    FastAPI = None
    HTTPException = None
    Request = None
    JSONResponse = None
    uvicorn = None

SUPPORT_CHANNEL = "https://discord.com/channels/1289721245281292288/1459628609705738539"
BETA_INVITE_CHANNEL_URL = "https://discord.com/channels/1289721245281292288/1464736918951432222"

BETA_INVITE_SUPPORT_CONTACT = getattr(
    welcome_base,
    "BETA_INVITE_SUPPORT_CONTACT",
    "@earlysalty",
)
BETA_MAIN_GUILD_ID = getattr(welcome_base, "MAIN_GUILD_ID", None)
BETA_INVITE_PANEL_CUSTOM_ID = "betainvite:panel:start"
KOFI_VERIFICATION_TOKEN = os.getenv("KOFI_VERIFICATION_TOKEN")

EXPRESS_SUCCESS_DM = "Vielen Dank für deinen Support! 🚑 Dein Deadlock-Invite wird jetzt verarbeitet."
STEAM_LINK_REQUIRED_DM = "Zahlung erhalten! Aber du musst erst deinen Steam-Account verknüpfen. Nutze danach /betainvite oder klicke im Panel auf Weiter."
def _make_payment_message(token: str) -> str:
    return (
        f"Damit wir dir den Invite schicken können, brauchen wir deine Hilfe!\n\n"
        f"Um unseren Server werbefrei und technisch auf dem neuesten Stand zu halten, erheben wir einen kleinen Infrastruktur-Beitrag. "
        f"Ohne diesen können wir den Service und die Bot-Entwicklung leider nicht dauerhaft anbieten.\n\n"
        f"**Wichtig:** Kopiere diesen Code in das Nachrichtenfeld bei Ko-fi:\n"
        f"```\n{token}\n```"
        f"Nur so können wir dich automatisch zuordnen. Schreib **nichts anderes** in das Feld.\n\n"
        f"Nachdem du deinen Beitrag geleistet hast, wird der Invite automatisch per DM an dich verschickt."
    )
KOFI_PAYMENT_URL = "https://ko-fi.com/deutschedeadlockcommunity"
_raw_log_channel_id = os.getenv("BETA_INVITE_LOG_CHANNEL_ID", "1234567890")
try:
    BETA_INVITE_LOG_CHANNEL_ID = int(_raw_log_channel_id)
except (TypeError, ValueError):
    BETA_INVITE_LOG_CHANNEL_ID = None
KOFI_WEBHOOK_HOST = os.getenv("KOFI_WEBHOOK_HOST", "127.0.0.1")
KOFI_WEBHOOK_PORT = int(os.getenv("KOFI_WEBHOOK_PORT", "8932"))
KOFI_WEBHOOK_PATH = "/kofi-webhook"

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
    payload = {
        "event": event,
        "ts_unix_ms": int(time.time() * 1000),
        "ts_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    }
    payload.update(fields)
    try:
        _trace_log.info(json.dumps(payload, ensure_ascii=False, default=str))
    except Exception:
        log.debug("Trace log failed", exc_info=True)


def _preview_text(value: Any, *, max_len: int = 700) -> Optional[str]:
    if value is None:
        return None
    text = str(value).replace("\r", "").strip()
    if len(text) <= max_len:
        return text
    return f"{text[:max_len]}...(+{len(text) - max_len} chars)"


def _view_snapshot(view: Any) -> Optional[dict[str, Any]]:
    if view is None:
        return None

    snapshot: dict[str, Any] = {
        "class": view.__class__.__name__,
        "timeout": getattr(view, "timeout", None),
    }

    children = getattr(view, "children", None)
    if not isinstance(children, list):
        return snapshot

    items: list[dict[str, Any]] = []
    for child in children[:12]:
        style_obj = getattr(child, "style", None)
        style = getattr(style_obj, "name", None) or (str(style_obj) if style_obj is not None else None)
        item = {
            "type": child.__class__.__name__,
            "label": getattr(child, "label", None),
            "custom_id": getattr(child, "custom_id", None),
            "style": style,
            "disabled": bool(getattr(child, "disabled", False)),
            "url": getattr(child, "url", None),
            "row": getattr(child, "row", None),
        }
        emoji = getattr(child, "emoji", None)
        if emoji is not None:
            item["emoji"] = str(emoji)
        items.append(item)

    snapshot["item_count"] = len(children)
    snapshot["items"] = items
    return snapshot


def _interaction_snapshot(interaction: Optional[discord.Interaction]) -> dict[str, Any]:
    if interaction is None:
        return {}

    data = interaction.data if isinstance(getattr(interaction, "data", None), Mapping) else {}
    command_name: Optional[str] = None
    try:
        command = getattr(interaction, "command", None)
        command_name = getattr(command, "name", None) if command else None
    except Exception:
        command_name = None

    interaction_type = getattr(interaction, "type", None)
    interaction_type_name = getattr(interaction_type, "name", None) or (str(interaction_type) if interaction_type is not None else None)

    created_at = getattr(interaction, "created_at", None)
    created_at_utc = created_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if created_at else None

    message = getattr(interaction, "message", None)
    channel = getattr(interaction, "channel", None)
    user = getattr(interaction, "user", None)
    guild = getattr(interaction, "guild", None)
    response_done: Optional[bool] = None
    response_obj = getattr(interaction, "response", None)
    if response_obj is not None and hasattr(response_obj, "is_done"):
        try:
            response_done = bool(response_obj.is_done())
        except Exception:
            response_done = None

    return {
        "interaction_id": getattr(interaction, "id", None),
        "interaction_type": interaction_type_name,
        "command_name": command_name,
        "custom_id": data.get("custom_id"),
        "component_type": data.get("component_type"),
        "response_done": response_done,
        "created_at_utc": created_at_utc,
        "guild_id": getattr(guild, "id", None),
        "channel_id": getattr(channel, "id", None),
        "message_id": getattr(message, "id", None),
        "discord_id": getattr(user, "id", None),
        "discord_name": _format_discord_name(user) if user is not None else None,
    }


def _trace_interaction_event(
    event: str,
    interaction: Optional[discord.Interaction] = None,
    **fields: Any,
) -> None:
    payload = _interaction_snapshot(interaction)
    payload.update(fields)
    _trace(event, **payload)


def _can_bind_port(host: str, port: int) -> tuple[bool, Optional[str]]:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((host, port))
        return True, None
    except OSError as exc:
        return False, str(exc)


def _probe_kofi_health(
    host: str,
    port: int,
    path: str = "/kofi-health",
) -> tuple[bool, Optional[str]]:
    conn: Optional[http.client.HTTPConnection] = None
    try:
        conn = http.client.HTTPConnection(host, port, timeout=2.0)
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read()
    except Exception as exc:
        return False, str(exc)
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                log.debug("Ko-fi health probe connection close failed", exc_info=True)

    if resp.status != 200:
        return False, f"Status {resp.status}"
    try:
        data = json.loads(body.decode("utf-8", errors="ignore"))
    except Exception as exc:
        return False, f"Antwort konnte nicht gelesen werden: {exc}"
    if data.get("ok") is True:
        return True, None
    return False, f"Unerwartete Antwort: {data}"


class _WebhookFollowup:
    def __init__(
        self,
        user: discord.abc.User,
        log_channel: Optional[discord.abc.Messageable],
    ) -> None:
        self.user = user
        self.log_channel = log_channel

    async def send(self, content: str, **kwargs: Any) -> None:
        _trace(
            "ui_delivery_attempt",
            call="webhook_followup.user.send",
            destination=f"user:{getattr(self.user, 'id', None)}",
            content_preview=_preview_text(content),
        )
        try:
            await self.user.send(content)
            _trace(
                "ui_delivery_success",
                call="webhook_followup.user.send",
                destination=f"user:{getattr(self.user, 'id', None)}",
                content_preview=_preview_text(content),
            )
            return
        except Exception as exc:
            _trace(
                "ui_delivery_failed",
                call="webhook_followup.user.send",
                destination=f"user:{getattr(self.user, 'id', None)}",
                content_preview=_preview_text(content),
                error=str(exc),
                error_type=type(exc).__name__,
            )
            log.debug(
                "Ko-fi followup DM fehlgeschlagen für %s",
                getattr(self.user, "id", None),
                exc_info=True,
            )
        if self.log_channel:
            _trace(
                "ui_delivery_attempt",
                call="webhook_followup.log_channel.send",
                destination=f"channel:{getattr(self.log_channel, 'id', None)}",
                content_preview=_preview_text(content),
            )
            try:
                await self.log_channel.send(content)
                _trace(
                    "ui_delivery_success",
                    call="webhook_followup.log_channel.send",
                    destination=f"channel:{getattr(self.log_channel, 'id', None)}",
                    content_preview=_preview_text(content),
                )
            except Exception as exc:
                _trace(
                    "ui_delivery_failed",
                    call="webhook_followup.log_channel.send",
                    destination=f"channel:{getattr(self.log_channel, 'id', None)}",
                    content_preview=_preview_text(content),
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                log.debug("Ko-fi Followup-Log fehlgeschlagen", exc_info=True)


class _WebhookInteractionProxy:
    def __init__(
        self,
        user: discord.abc.User,
        guild: Optional[discord.Guild],
        log_channel: Optional[discord.abc.Messageable],
    ) -> None:
        self.user = user
        self.guild = guild
        self.followup = _WebhookFollowup(user, log_channel)

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


def _format_invite_sent_utc(invite_sent_at: Optional[int]) -> Optional[str]:
    if not invite_sent_at:
        return None
    try:
        return datetime.fromtimestamp(int(invite_sent_at), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return None


def _build_already_invited_message(record: BetaInviteRecord, steam_id64: str) -> str:
    sent_utc = _format_invite_sent_utc(record.invite_sent_at)
    profile_url = f"https://steamcommunity.com/profiles/{steam_id64}"
    lines = [
        "✅ Du bist bereits eingeladen.",
        f"Steam-Account: {profile_url}",
    ]
    if sent_utc:
        lines.append(f"Zuletzt erfolgreich gesendet: {sent_utc}")
    lines.append("Prüfe unter https://store.steampowered.com/account/playtestinvites .")
    lines.append("Wichtig: Prüfe genau den oben verknüpften Steam-Account.")
    return "\n".join(lines)


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


def _register_pending_payment(discord_id: int, discord_name: str) -> str:
    """Registriert eine ausstehende Zahlung und gibt den zugehörigen Token zurück."""
    now_ts = int(time.time())
    token = f"DDL-{secrets.token_hex(4).upper()}"
    with db.get_conn() as conn:
        # Prüfe ob bereits ein Token existiert (bei erneutem Klick alten Token wiederverwenden)
        existing = conn.execute(
            "SELECT token FROM beta_invite_pending_payments WHERE discord_id = ?",
            (int(discord_id),),
        ).fetchone()
        if existing and existing["token"]:
            return existing["token"]
        conn.execute(
            """
            INSERT INTO beta_invite_pending_payments(discord_id, discord_name, token, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(discord_id) DO UPDATE SET
                discord_name=excluded.discord_name,
                token=COALESCE(token, excluded.token),
                created_at=excluded.created_at
            """,
            (int(discord_id), str(discord_name), token, now_ts),
        )
    return token


def _get_pending_payment_by_token(token: str) -> Optional[int]:
    """Sucht nach einer offenen Zahlung via Token (Case-Insensitive)."""
    clean = token.strip().upper()
    if not clean:
        return None
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT discord_id FROM beta_invite_pending_payments WHERE UPPER(token) = ?",
            (clean,),
        ).fetchone()
        if row:
            return int(row["discord_id"])
    return None


def _get_pending_payment(username_or_id: Union[str, int]) -> Optional[int]:
    """Sucht nach einer offenen Zahlung via ID oder Name (Username-Case-Insensitive)."""
    with db.get_conn() as conn:
        # Erst nach ID suchen
        d_id: Optional[int] = None
        try:
            d_id = int(username_or_id)
        except (ValueError, TypeError):
            d_id = None

        if d_id is not None:
            row = conn.execute(
                "SELECT discord_id FROM beta_invite_pending_payments WHERE discord_id = ?",
                (d_id,),
            ).fetchone()
            if row:
                return int(row["discord_id"])

        # Dann nach Name suchen (Case-Insensitive)
        name_clean = str(username_or_id).strip().lstrip("@").lower()
        row = conn.execute(
            "SELECT discord_id FROM beta_invite_pending_payments WHERE LOWER(discord_name) = ?",
            (name_clean,),
        ).fetchone()
        if row:
            return int(row["discord_id"])
    return None


def _track_panel_click(discord_id: int) -> None:
    """Zählt Panel-Klicks pro User in der DB."""
    now_ts = int(time.time())
    with db.get_conn() as conn:
        conn.execute(
            """
            INSERT INTO beta_invite_panel_clicks(discord_id, click_count, first_clicked_at, last_clicked_at)
            VALUES (?, 1, ?, ?)
            ON CONFLICT(discord_id) DO UPDATE SET
                click_count = click_count + 1,
                last_clicked_at = excluded.last_clicked_at
            """,
            (int(discord_id), now_ts, now_ts),
        )


def _get_funnel_stats() -> dict:
    """Liest alle Funnel-Metriken aus der DB."""
    with db.get_conn() as conn:
        panel_clicks = conn.execute(
            "SELECT COUNT(DISTINCT discord_id) as unique_users, COALESCE(SUM(click_count),0) as total_clicks FROM beta_invite_panel_clicks"
        ).fetchone()

        intent_counts = {
            row["intent"]: row["n"]
            for row in conn.execute(
                "SELECT intent, COUNT(*) as n FROM beta_invite_intent GROUP BY intent"
            ).fetchall()
        }

        invite_statuses = {
            row["status"]: row["n"]
            for row in conn.execute(
                "SELECT status, COUNT(*) as n FROM steam_beta_invites GROUP BY status"
            ).fetchall()
        }

        community_funnel = conn.execute(
            """
            SELECT
                COUNT(DISTINCT i.discord_id) as chose_community,
                SUM(CASE WHEN b.discord_id IS NOT NULL THEN 1 ELSE 0 END) as has_record,
                SUM(CASE WHEN b.status = 'invite_sent' THEN 1 ELSE 0 END) as invite_sent
            FROM beta_invite_intent i
            LEFT JOIN steam_beta_invites b ON b.discord_id = i.discord_id
            WHERE i.intent = 'community'
            """
        ).fetchone()

        invite_only_funnel = conn.execute(
            """
            SELECT
                COUNT(DISTINCT i.discord_id) as chose_invite_only,
                SUM(CASE WHEN b.discord_id IS NOT NULL THEN 1 ELSE 0 END) as has_record,
                SUM(CASE WHEN b.status = 'invite_sent' THEN 1 ELSE 0 END) as invite_sent,
                SUM(CASE WHEN p.discord_id IS NOT NULL THEN 1 ELSE 0 END) as got_payment_link
            FROM beta_invite_intent i
            LEFT JOIN steam_beta_invites b ON b.discord_id = i.discord_id
            LEFT JOIN beta_invite_pending_payments p ON p.discord_id = i.discord_id
            WHERE i.intent = 'invite_only'
            """
        ).fetchone()

        # Geier: invite_only gewählt, kein Invite-Record, entschieden vor mehr als 1h
        geier_cutoff = int(time.time()) - 3600
        geier_rows = conn.execute(
            """
            SELECT i.discord_id, i.decided_at
            FROM beta_invite_intent i
            LEFT JOIN steam_beta_invites b ON b.discord_id = i.discord_id
            WHERE i.intent = 'invite_only'
              AND b.discord_id IS NULL
              AND i.decided_at < ?
            ORDER BY i.decided_at DESC
            """,
            (geier_cutoff,),
        ).fetchall()

        # Community-Abbrecher: community gewählt, kein Invite-Record, vor mehr als 1h
        dropout_cutoff = int(time.time()) - 3600
        community_dropout = conn.execute(
            """
            SELECT COUNT(*) as n
            FROM beta_invite_intent i
            LEFT JOIN steam_beta_invites b ON b.discord_id = i.discord_id
            WHERE i.intent = 'community'
              AND b.discord_id IS NULL
              AND i.decided_at < ?
            """,
            (dropout_cutoff,),
        ).fetchone()

    return {
        "panel_unique": panel_clicks["unique_users"] if panel_clicks else 0,
        "panel_total": panel_clicks["total_clicks"] if panel_clicks else 0,
        "intent_community": intent_counts.get("community", 0),
        "intent_invite_only": intent_counts.get("invite_only", 0),
        "community_has_record": community_funnel["has_record"] or 0,
        "community_invite_sent": community_funnel["invite_sent"] or 0,
        "community_dropout": community_dropout["n"] if community_dropout else 0,
        "invite_only_got_link": invite_only_funnel["got_payment_link"] or 0,
        "invite_only_invite_sent": invite_only_funnel["invite_sent"] or 0,
        "invite_only_total": invite_only_funnel["chose_invite_only"] or 0,
        "geier": [{"discord_id": r["discord_id"], "decided_at": r["decided_at"]} for r in geier_rows],
        "invite_statuses": invite_statuses,
    }


def _cleanup_pending_payments() -> int:
    """Entfernt Einträge, die älter als 24 Stunden sind."""
    expiry = int(time.time()) - (24 * 3600)
    with db.get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM beta_invite_pending_payments WHERE created_at < ?",
            (expiry,),
        )
        return cur.rowcount


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
    filtered = {k: v for k, v in fields.items() if k in _ALLOWED_UPDATE_FIELDS}
    if not filtered:
        return _fetch_invite_by_id(record_id)

    def _flag_and_value(key: str):
        if key in filtered:
            return 1, filtered[key]
        return 0, None

    with db.get_conn() as conn:
        status_set, status_val = _flag_and_value("status")
        last_error_set, last_error_val = _flag_and_value("last_error")
        friend_requested_set, friend_requested_val = _flag_and_value("friend_requested_at")
        friend_confirmed_set, friend_confirmed_val = _flag_and_value("friend_confirmed_at")
        invite_sent_set, invite_sent_val = _flag_and_value("invite_sent_at")
        last_notified_set, last_notified_val = _flag_and_value("last_notified_at")
        account_id_set, account_id_val = _flag_and_value("account_id")

        conn.execute(
            """
            UPDATE steam_beta_invites
               SET status = CASE WHEN ? THEN ? ELSE status END,
                   last_error = CASE WHEN ? THEN ? ELSE last_error END,
                   friend_requested_at = CASE WHEN ? THEN ? ELSE friend_requested_at END,
                   friend_confirmed_at = CASE WHEN ? THEN ? ELSE friend_confirmed_at END,
                   invite_sent_at = CASE WHEN ? THEN ? ELSE invite_sent_at END,
                   last_notified_at = CASE WHEN ? THEN ? ELSE last_notified_at END,
                   account_id = CASE WHEN ? THEN ? ELSE account_id END,
                   updated_at = strftime('%s','now')
             WHERE id = ?
            """,
            (
                status_set,
                status_val,
                last_error_set,
                last_error_val,
                friend_requested_set,
                friend_requested_val,
                friend_confirmed_set,
                friend_confirmed_val,
                invite_sent_set,
                invite_sent_val,
                last_notified_set,
                last_notified_val,
                account_id_set,
                account_id_val,
                int(record_id),
            ),
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
        allowed = interaction.user.id == self.requester_id
        self.cog._trace_user_action(
            interaction,
            "intent_gate.interaction_check",
            allowed=allowed,
            requester_id=self.requester_id,
        )
        if interaction.user.id != self.requester_id:
            await self.cog._response_send_message(
                interaction,
                "Nur der ursprüngliche Nutzer kann diese Auswahl treffen.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Nur schnell den Invite abholen", style=discord.ButtonStyle.primary)
    async def choose_invite_only(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.cog._trace_user_action(interaction, "intent_gate.choose_invite_only")
        await self.cog.handle_intent_selection(interaction, INTENT_INVITE_ONLY)

    @discord.ui.button(label="Ich will mitspielen/aktiv sein", style=discord.ButtonStyle.primary)
    async def choose_join(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.cog._trace_user_action(interaction, "intent_gate.choose_community")
        await self.cog.handle_intent_selection(interaction, INTENT_COMMUNITY)


class InviteOnlyPaymentView(discord.ui.View):
    def __init__(self, kofi_url: str) -> None:
        super().__init__(timeout=300)
        self.add_item(
            discord.ui.Button(
                label="Supporten & Invite abholen :)",
                style=discord.ButtonStyle.link,
                url=kofi_url,
                emoji="💙",
            )
        )


class BetaInviteLinkPromptView(discord.ui.View):
    def __init__(self, cog: "BetaInviteFlow", user_id: int, login_url: Optional[str], steam_url: Optional[str]) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.user_id = user_id
        
        if login_url:
            self.add_item(
                discord.ui.Button(
                    label="Via Discord bei Steam anmelden",
                    style=discord.ButtonStyle.link,
                    url=login_url,
                    emoji="🔗",
                    row=0,
                )
            )
        else:
            self.add_item(
                discord.ui.Button(
                    label="Via Discord bei Steam anmelden",
                    style=discord.ButtonStyle.secondary,
                    disabled=True,
                    emoji="🔗",
                    row=0,
                )
            )

        if steam_url:
            self.add_item(
                discord.ui.Button(
                    label="Direkt bei Steam anmelden",
                    style=discord.ButtonStyle.link,
                    url=steam_url,
                    emoji="🎮",
                    row=0,
                )
            )
        else:
            self.add_item(
                discord.ui.Button(
                    label="Direkt bei Steam anmelden",
                    style=discord.ButtonStyle.secondary,
                    disabled=True,
                    emoji="🎮",
                    row=0,
                )
            )

    @discord.ui.button(label="Ich habe mich verknüpft – Weiter", style=discord.ButtonStyle.success, emoji="➡️", row=1)
    async def next_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.cog._trace_user_action(interaction, "link_prompt.next_clicked")
        if interaction.user.id != self.user_id:
            await self.cog._response_send_message(
                interaction,
                "Nur der ursprüngliche Nutzer kann diese Auswahl treffen.",
                ephemeral=True,
            )
            return
        
        # Sofortiges Feedback und Buttons entfernen um Double-Clicks zu verhindern
        try:
            await self.cog._response_edit_message(
                interaction,
                content="⏳ Status wird geprüft... Bitte warten.",
                view=None,
            )
        except Exception as exc:
            log.debug("Link prompt status message could not be updated before restarting flow: %s", exc)

        # Den Flow erneut starten (der nun die Verknüpfung finden sollte)
        await self.cog.start_invite_from_panel(interaction)


class BetaInviteConfirmView(discord.ui.View):
    def __init__(self, cog: "BetaInviteFlow", record_id: int, discord_id: int, steam_id64: str) -> None:
        super().__init__(timeout=600)
        self.cog = cog
        self.record_id = record_id
        self.discord_id = discord_id
        self.steam_id64 = steam_id64

    @discord.ui.button(label="Freundschaft bestätigt", style=discord.ButtonStyle.success, emoji="🤝")
    async def confirm_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.cog._trace_user_action(
            interaction,
            "confirm_view.confirm_clicked",
            record_id=self.record_id,
            steam_id64=self.steam_id64,
        )
        if interaction.user.id != self.discord_id:
            await self.cog._response_send_message(
                interaction,
                "Nur der ursprüngliche Nutzer kann diese Einladung bestätigen.",
                ephemeral=True,
            )
            return
        await self.cog.handle_confirmation(interaction, self.record_id)


class BetaInvitePanelView(discord.ui.View):
    def __init__(self, cog: "BetaInviteFlow") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Invite starten",
        style=discord.ButtonStyle.primary,
        emoji="🎟️",
        custom_id=BETA_INVITE_PANEL_CUSTOM_ID,
    )
    async def start_invite(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.cog._trace_user_action(interaction, "panel.start_invite_clicked")
        try:
            _track_panel_click(interaction.user.id)
        except Exception:
            log.debug("Panel-Click-Tracking fehlgeschlagen", exc_info=True)
        try:
            await self.cog.start_invite_from_panel(interaction)
        except Exception:
            log.exception("Start Invite aus Panel fehlgeschlagen", exc_info=True)
            try:
                await self.cog._response_send_message(
                    interaction,
                    "❌ Diese Interaktion ist fehlgeschlagen. Bitte versuche es erneut.",
                    ephemeral=True,
                )
            except Exception:
                try:
                    await self.cog._followup_send(
                        interaction,
                        "❌ Diese Interaktion ist fehlgeschlagen. Bitte versuche es erneut.",
                        ephemeral=True,
                    )
                except Exception:
                    log.debug("Followup-Fehlernachricht konnte nicht gesendet werden", exc_info=True)


class BetaInviteFlow(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.tasks = SteamTaskClient(poll_interval=0.5, default_timeout=30.0)
        self._kofi_webhook_task: Optional[asyncio.Task] = None
        self._kofi_server = None
        self._log_channel_cache: Optional[discord.abc.Messageable] = None
        _ensure_invite_audit_table()

    def _trace_user_action(
        self,
        interaction: Optional[discord.Interaction],
        action: str,
        **fields: Any,
    ) -> None:
        _trace_interaction_event("ui_action", interaction, action=action, **fields)

    async def _run_ui_delivery(
        self,
        *,
        interaction: Optional[discord.Interaction],
        call: str,
        op: Callable[[], Awaitable[Any]],
        content: Optional[Any] = None,
        ephemeral: Optional[bool] = None,
        view: Optional[Any] = None,
        destination: Optional[str] = None,
        **fields: Any,
    ) -> Any:
        payload = {
            "call": call,
            "destination": destination,
            "ephemeral": ephemeral,
            "content_preview": _preview_text(content),
            "view": _view_snapshot(view),
        }
        payload.update(fields)
        _trace_interaction_event("ui_delivery_attempt", interaction, **payload)
        try:
            result = await op()
        except Exception as exc:
            _trace_interaction_event(
                "ui_delivery_failed",
                interaction,
                error=str(exc),
                error_type=type(exc).__name__,
                **payload,
            )
            raise
        _trace_interaction_event("ui_delivery_success", interaction, **payload)
        return result

    async def _response_send_message(
        self,
        interaction: discord.Interaction,
        content: str,
        *,
        ephemeral: bool = False,
        view: Optional[Any] = None,
    ) -> Any:
        send_kwargs: dict[str, Any] = {"ephemeral": ephemeral}
        if view is not None:
            send_kwargs["view"] = view
        return await self._run_ui_delivery(
            interaction=interaction,
            call="interaction.response.send_message",
            content=content,
            ephemeral=ephemeral,
            view=view,
            op=lambda: interaction.response.send_message(content, **send_kwargs),
        )

    async def _followup_send(
        self,
        interaction: discord.Interaction,
        content: str,
        *,
        ephemeral: bool = False,
        view: Optional[Any] = None,
    ) -> Any:
        send_kwargs: dict[str, Any] = {"ephemeral": ephemeral}
        if view is not None:
            send_kwargs["view"] = view
        return await self._run_ui_delivery(
            interaction=interaction,
            call="interaction.followup.send",
            content=content,
            ephemeral=ephemeral,
            view=view,
            op=lambda: interaction.followup.send(content, **send_kwargs),
        )

    async def _response_edit_message(
        self,
        interaction: discord.Interaction,
        *,
        content: Optional[str] = None,
        view: Optional[Any] = None,
    ) -> Any:
        return await self._run_ui_delivery(
            interaction=interaction,
            call="interaction.response.edit_message",
            content=content,
            view=view,
            op=lambda: interaction.response.edit_message(content=content, view=view),
        )

    async def _response_defer(
        self,
        interaction: discord.Interaction,
        *,
        ephemeral: bool = False,
        thinking: bool = False,
    ) -> Any:
        return await self._run_ui_delivery(
            interaction=interaction,
            call="interaction.response.defer",
            content=f"defer(thinking={thinking})",
            ephemeral=ephemeral,
            op=lambda: interaction.response.defer(ephemeral=ephemeral, thinking=thinking),
        )

    async def _edit_original_response(
        self,
        interaction: discord.Interaction,
        *,
        content: Optional[str] = None,
        view: Optional[Any] = None,
    ) -> Any:
        return await self._run_ui_delivery(
            interaction=interaction,
            call="interaction.edit_original_response",
            content=content,
            view=view,
            op=lambda: interaction.edit_original_response(content=content, view=view),
        )

    async def _send_user_dm(
        self,
        user: discord.abc.User,
        content: str,
        *,
        interaction: Optional[discord.Interaction] = None,
    ) -> Any:
        return await self._run_ui_delivery(
            interaction=interaction,
            call="user.send",
            destination=f"user:{getattr(user, 'id', None)}",
            content=content,
            op=lambda: user.send(content),
        )

    async def _send_channel_message(
        self,
        channel: Union[discord.TextChannel, discord.Thread, discord.abc.Messageable],
        *,
        content: Optional[str] = None,
        embed: Optional[discord.Embed] = None,
        view: Optional[Any] = None,
        interaction: Optional[discord.Interaction] = None,
    ) -> Any:
        content_preview = content
        if content_preview is None and embed is not None:
            content_preview = f"[embed] title={getattr(embed, 'title', None)!r} description={_preview_text(getattr(embed, 'description', None), max_len=240)!r}"
        return await self._run_ui_delivery(
            interaction=interaction,
            call="channel.send",
            destination=f"channel:{getattr(channel, 'id', None)}",
            content=content_preview,
            view=view,
            op=lambda: channel.send(content=content, embed=embed, view=view),
        )

    async def _await_animation_task(self, task: Optional[asyncio.Task]) -> None:
        if task is None:
            return
        await asyncio.gather(task)

    async def cog_load(self) -> None:
        self.bot.add_view(BetaInvitePanelView(self))
        # Cleanup expired pending payments (older than 24h)
        try:
            removed = _cleanup_pending_payments()
            if removed > 0:
                log.info("BetaInvite: %s abgelaufene ausstehende Zahlungen bereinigt", removed)
        except Exception:
            log.debug("Cleanup of pending payments failed", exc_info=True)

        if BETA_MAIN_GUILD_ID:
            try:
                guild_obj = discord.Object(id=int(BETA_MAIN_GUILD_ID))
                synced = await self.bot.tree.sync(guild=guild_obj)
                log.info(
                    "BetaInvite: Commands für Guild %s synchronisiert (%s)",
                    BETA_MAIN_GUILD_ID,
                    len(synced),
                )
            except Exception as exc:
                log.warning(
                    "BetaInvite: Command-Sync für Guild %s fehlgeschlagen: %s",
                    BETA_MAIN_GUILD_ID,
                    exc,
                )
        else:
            log.info("BetaInvite: MAIN_GUILD_ID nicht gesetzt, kein Guild-Sync durchgeführt")
        log.info("BetaInvite: Panel-View registriert")

    async def cog_unload(self) -> None:
        log.info("BetaInvite: Unloading cog, stopping Ko-fi webhook server...")
        if self._kofi_server is not None:
            try:
                self._kofi_server.should_exit = True
            except Exception:
                log.debug("Konnte Ko-fi Server nicht zum Stoppen markieren", exc_info=True)

        if self._kofi_webhook_task and not self._kofi_webhook_task.done():
            try:
                self._kofi_webhook_task.cancel()
                # Wait for the task to actually finish to release the port
                try:
                    await asyncio.wait_for(self._kofi_webhook_task, timeout=5.0)
                except (asyncio.CancelledError, asyncio.TimeoutError) as exc:
                    log.debug(
                        "Ko-fi Webhook Task beim Unload beendet: %s",
                        type(exc).__name__,
                    )
            except Exception:
                log.debug("Fehler beim Warten auf Ko-fi Webhook Task", exc_info=True)

        self._kofi_server = None
        self._kofi_webhook_task = None
        log.info("BetaInvite: Cog unloaded and server task cleaned up.")

    def _main_guild(self) -> Optional[discord.Guild]:
        try:
            guild_id = int(BETA_MAIN_GUILD_ID) if BETA_MAIN_GUILD_ID else None
        except (TypeError, ValueError):
            return None
        return self.bot.get_guild(guild_id) if guild_id else None

    async def _get_log_channel(self) -> Optional[discord.abc.Messageable]:
        if self._log_channel_cache:
            return self._log_channel_cache
        if not BETA_INVITE_LOG_CHANNEL_ID:
            return None

        channel = self.bot.get_channel(BETA_INVITE_LOG_CHANNEL_ID)
        if channel:
            self._log_channel_cache = channel  # type: ignore[assignment]
            return channel
        try:
            fetched = await self.bot.fetch_channel(BETA_INVITE_LOG_CHANNEL_ID)
        except Exception:
            log.debug("Konnte Beta-Invite-Log-Channel nicht abrufen", exc_info=True)
            return None
        self._log_channel_cache = fetched  # type: ignore[assignment]
        return fetched

    async def _notify_log_channel(self, message: str) -> None:
        channel = await self._get_log_channel()
        if channel is None:
            log.warning("BetaInvite-Log (Fallback): %s", message)
            return
        _trace(
            "ui_delivery_attempt",
            call="notify_log_channel.send",
            destination=f"channel:{getattr(channel, 'id', None)}",
            content_preview=_preview_text(message),
        )
        try:
            await channel.send(message)
            _trace(
                "ui_delivery_success",
                call="notify_log_channel.send",
                destination=f"channel:{getattr(channel, 'id', None)}",
                content_preview=_preview_text(message),
            )
        except Exception as exc:
            _trace(
                "ui_delivery_failed",
                call="notify_log_channel.send",
                destination=f"channel:{getattr(channel, 'id', None)}",
                content_preview=_preview_text(message),
                error=str(exc),
                error_type=type(exc).__name__,
            )
            log.debug("Senden an BetaInvite-Log-Channel fehlgeschlagen", exc_info=True)

    @staticmethod
    def _extract_discord_username(message: str) -> Optional[str]:
        text = (message or "").strip()
        if not text:
            return None
        parts = text.split()
        for part in parts:
            if part.startswith("@") and len(part) > 1:
                candidate = part.lstrip("@").strip()
                if candidate:
                    return candidate
        return text.lstrip("@").strip() or None

    async def _find_member_by_username(self, guild: discord.Guild, username: str) -> Optional[discord.Member]:
        clean = username.strip().lstrip("@")
        if not clean:
            return None

        # 1. Lokaler Cache
        member = guild.get_member_named(clean)
        if member:
            return member

        # 2. Case-insensitive Cache Suche
        clean_lower = clean.lower()
        for candidate in guild.members:
            names = [
                str(getattr(candidate, "name", "")).lower(),
                str(getattr(candidate, "global_name", "") or "").lower(),
                str(getattr(candidate, "display_name", "") or "").lower(),
            ]
            if clean_lower in names:
                return candidate

        # 3. API Query (Smarter Fallback)
        try:
            # Suche nach exaktem Namen via API
            found = await guild.query_members(query=clean, limit=5)
            for m in found:
                if m.name.lower() == clean_lower or (m.global_name and m.global_name.lower() == clean_lower):
                    return m
        except Exception:
            log.debug("API member query failed", exc_info=True)

        return None

    async def _safe_dm(self, user: discord.abc.User, content: str) -> bool:
        _trace(
            "ui_delivery_attempt",
            call="safe_dm.user.send",
            destination=f"user:{getattr(user, 'id', None)}",
            content_preview=_preview_text(content),
        )
        try:
            await user.send(content)
            _trace(
                "ui_delivery_success",
                call="safe_dm.user.send",
                destination=f"user:{getattr(user, 'id', None)}",
                content_preview=_preview_text(content),
            )
            return True
        except Exception as exc:
            _trace(
                "ui_delivery_failed",
                call="safe_dm.user.send",
                destination=f"user:{getattr(user, 'id', None)}",
                content_preview=_preview_text(content),
                error=str(exc),
                error_type=type(exc).__name__,
            )
            log.debug("DM konnte nicht gesendet werden an %s", getattr(user, "id", None), exc_info=True)
            return False

    async def handle_kofi_webhook(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        raw_message = str(data.get("message") or "").strip()

        _trace(
            "kofi_webhook_received",
            raw_message=raw_message,
            payload_keys=list(data.keys()) if isinstance(data, Mapping) else None,
        )

        guild = self._main_guild()
        if guild is None:
            await self._notify_log_channel("Ko-fi Webhook: MAIN_GUILD_ID ist nicht gesetzt oder Guild nicht im Cache.")
            return {"ok": False, "reason": "guild_unavailable"}

        if not raw_message:
            await self._notify_log_channel(
                "⚠️ Ko-fi Webhook: Nachrichtenfeld leer – kein Token angegeben. Bitte manuell prüfen!"
            )
            return {"ok": False, "reason": "missing_message"}

        # Token-Lookup (primär): Format DDL-XXXXXXXX
        member = None
        token_candidate = raw_message.strip().upper()
        pending_id = _get_pending_payment_by_token(token_candidate)
        if pending_id:
            member = guild.get_member(pending_id)
            if not member:
                try:
                    member = await guild.fetch_member(pending_id)
                except Exception:
                    log.debug("Could not fetch member from pending_id %s", pending_id)

        # Fallback: Alter Username-basierter Lookup (für Abwärtskompatibilität)
        if member is None:
            username_candidate = self._extract_discord_username(raw_message)
            if username_candidate:
                pending_id_by_name = _get_pending_payment(username_candidate)
                if pending_id_by_name:
                    member = guild.get_member(pending_id_by_name)
                    if not member:
                        try:
                            member = await guild.fetch_member(pending_id_by_name)
                        except Exception:
                            log.debug("Could not fetch member from pending_id %s", pending_id_by_name)
                if member is None:
                    member = await self._find_member_by_username(guild, username_candidate)

        if member is None:
            await self._notify_log_channel(
                f"⚠️ Ko-fi Webhook: Kein Nutzer für Token/Message `{raw_message!r}` gefunden. Bitte manuell prüfen!"
            )
            _trace(
                "kofi_member_not_found",
                raw_message=raw_message,
                guild_id=getattr(guild, "id", None),
            )
            return {"ok": False, "reason": "user_not_found", "raw_message": raw_message}

        steam_id = _lookup_primary_steam_id(member.id)
        if not steam_id:
            # Das sollte eigentlich durch den neuen Flow (Steam First) kaum noch passieren,
            # außer der Nutzer entkoppelt seinen Account genau zwischen Klick und Zahlung.
            dm_ok = await self._safe_dm(member, STEAM_LINK_REQUIRED_DM)
            if not dm_ok:
                await self._notify_log_channel(
                    f"⚠️ Ko-fi Webhook: DM an {member.mention} fehlgeschlagen (Steam-Link fehlt)."
                )
            _trace(
                "kofi_missing_steam_link",
                discord_id=member.id,
                username=username_candidate,
                raw_message=raw_message,
            )
            return {"ok": False, "reason": "missing_steam_link", "user_id": member.id}

        try:
            account_id = steam64_to_account_id(steam_id)
        except ValueError as exc:
            await self._notify_log_channel(
                f"⚠️ Ko-fi Webhook: Ungültige SteamID {steam_id} für {member.mention} ({exc})."
            )
            _trace(
                "kofi_invalid_steam_id",
                discord_id=member.id,
                steam_id64=steam_id,
                error=str(exc),
            )
            return {"ok": False, "reason": "invalid_steam_id", "user_id": member.id}

        record = _create_or_reset_invite(member.id, steam_id, account_id)
        log_channel = await self._get_log_channel()
        interaction = _WebhookInteractionProxy(member, guild, log_channel)
        
        # Info-Log
        await self._notify_log_channel(f"💰 Zahlung von {member.mention} erhalten. Starte Beta-Invite...")
        await interaction.followup.send(EXPRESS_SUCCESS_DM)

        try:
            ok = await self._send_invite_after_friend(
                interaction,
                record,
                account_id_hint=account_id,
            )
        except Exception as exc:
            log.exception("Ko-fi Invite-Flow fehlgeschlagen", exc_info=True)
            await self._notify_log_channel(
                f"⚠️ Ko-fi Webhook: Invite für {member.mention} (Steam: {steam_id}) fehlgeschlagen: {exc}"
            )
            _trace(
                "kofi_invite_exception",
                discord_id=member.id,
                steam_id64=steam_id,
                error=str(exc),
            )
            return {"ok": False, "reason": "invite_exception", "user_id": member.id}

        _trace(
            "kofi_invite_result",
            discord_id=member.id,
            steam_id64=steam_id,
            ok=ok,
        )
        return {
            "ok": bool(ok),
            "user_id": member.id,
            "steam_id64": steam_id,
        }


    async def _prompt_intent_gate(self, interaction: discord.Interaction) -> None:
        prompt = (
            "Kurze Frage bevor wir loslegen: Willst du hier aktiv mitspielen bzw. aktiv in der Community sein "
            "oder nur schnell einen Invite abholen?"
        )
        view = BetaIntentGateView(self, interaction.user.id)
        _trace("betainvite_intent_prompt", discord_id=interaction.user.id)
        await self._followup_send(interaction, prompt, view=view, ephemeral=True)

    async def handle_intent_selection(self, interaction: discord.Interaction, intent_choice: str) -> None:
        self._trace_user_action(interaction, "intent_selection", intent=intent_choice)
        if intent_choice not in (INTENT_COMMUNITY, INTENT_INVITE_ONLY):
            await self._response_send_message(
                interaction,
                "Ungültige Auswahl.",
                ephemeral=True,
            )
            return

        existing = _get_intent_record(interaction.user.id)
        if existing and existing.intent != intent_choice and existing.locked:
            await self._response_send_message(
                interaction,
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
            payment_token = _register_pending_payment(interaction.user.id, interaction.user.name)
            view = InviteOnlyPaymentView(KOFI_PAYMENT_URL)
            try:
                await self._response_edit_message(
                    interaction,
                    content=_make_payment_message(payment_token),
                    view=view,
                )
            except Exception:
                await self._followup_send(
                    interaction,
                    _make_payment_message(payment_token),
                    view=view,
                    ephemeral=True,
                )
            return

        try:
            # Edit initial message to show progress and remove buttons
            await self._response_edit_message(
                interaction,
                content="⏳ Deine Auswahl wird verarbeitet... Bitte warten.",
                view=None,
            )
        except discord.errors.NotFound:
            await self._followup_send(
                interaction,
                "Die Auswahl ist abgelaufen. Bitte starte `/betainvite` erneut.",
                ephemeral=True,
            )
            return
        except Exception as exc:
            log.error("Failed to edit intent interaction: %s", exc)
            # Fallback to defer if edit fails
            try:
                await self._response_defer(interaction, ephemeral=True, thinking=True)
            except Exception as defer_exc:
                log.debug("Intent interaction could not be deferred after edit failure: %s", defer_exc)

        await self._process_invite_request(interaction)

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

        return BetaInviteLinkPromptView(self, user.id, login_url, steam_url)

    async def _process_invite_request(self, interaction: discord.Interaction) -> None:
        self._trace_user_action(interaction, "process_invite_request.start")
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
            await self._followup_send(
                interaction,
                "⚠️ Datenbankfehler beim Abrufen der Steam-Verknüpfung. Bitte versuche es erneut.",
                ephemeral=True
            )
            return

        if not resolved:
            view = self._build_link_prompt_view(interaction.user)
            prompt = (
                "🚨 Es ist noch kein Steam-Account mit deinem Discord verknüpft.\n"
                "Melde dich mit den unten verfügbaren Optionen bei Steam an. Sobald du fertig bist, klicke auf **Weiter**."
            )
            _trace(
                "betainvite_no_link",
                discord_id=interaction.user.id,
            )

            await self._followup_send(
                interaction,
                prompt,
                view=view,
                ephemeral=True,
            )
            return

        try:
            account_id = steam64_to_account_id(resolved)
        except ValueError as exc:
            log.warning("Gespeicherte SteamID ungültig", exc_info=True)
            _trace(
                "betainvite_invalid_steamid",
                discord_id=interaction.user.id,
                steam_id=resolved,
                error=str(exc),
            )
            await self._followup_send(
                interaction,
                f"⚠️ Gespeicherte SteamID ist ungültig: {exc}. Bitte verknüpfe deinen Account erneut.",
                ephemeral=True,
            )
            return

        if existing and existing.status == STATUS_INVITE_SENT and existing.steam_id64 == resolved:
            await self._followup_send(
                interaction,
                _build_already_invited_message(existing, resolved),
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
            await self._followup_send(
                interaction,
                _build_already_invited_message(record, resolved),
                ephemeral=True,
            )
            _trace(
                "betainvite_already_invited_existing",
                discord_id=interaction.user.id,
                steam_id64=resolved,
            )
            return

        if record.status == STATUS_ERROR and record.friend_confirmed_at is not None:
            record = _update_invite(record.id, status=STATUS_WAITING, last_error=None) or record
            _trace(
                "betainvite_error_retry_after_friend_confirmed",
                discord_id=interaction.user.id,
                steam_id64=resolved,
                previous_error=existing.last_error if existing else None,
            )

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
                "Friendship pre-check für betainvite fehlgeschlagen",
                extra={"discord_id": interaction.user.id, "steam_id": resolved},
            )
            _trace(
                "betainvite_friend_precheck_error",
                discord_id=interaction.user.id,
                steam_id64=resolved,
            )

        if friend_ok:
            # Bereits Freunde → sofort verified=1 + Rolle
            await self._sync_verified_on_friendship(interaction.user.id, resolved)

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
            await self._followup_send(
                interaction,
                "⚠️ Konnte die Freundschaftsanfrage nicht senden. Bitte versuche es später erneut.",
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

                status_line = "✅ Wir sind laut Steam schon befreundet." if friend_ok else "⏳ Die Steam-Anfrage scheint bereits zu bestehen."
                message = (
                    f"{status_line}\n"
                    "Klicke unten auf \"Freundschaft bestätigt\", dann schicken wir dir den Deadlock-Invite."
                )
                view = BetaInviteConfirmView(self, record.id, interaction.user.id, resolved)
                await self._followup_send(interaction, message, view=view, ephemeral=True)
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
            await self._followup_send(
                interaction,
                "⚠️ Konnte die Freundschaftsanfrage nicht senden. Bitte prüfe deine Steam-Privatsphäreeinstellungen und versuche es erneut.",
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
            "✅ Freundschaftsanfrage verschickt!\n"
            "Sobald du die Anfrage angenommen hast, klicke unten auf \"Freundschaft bestätigt\", damit wir die Einladung senden können."
        )
        view = BetaInviteConfirmView(self, record.id, interaction.user.id, resolved)
        await self._followup_send(interaction, message, view=view, ephemeral=True)

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
        if isinstance(interaction, discord.Interaction):
            self._trace_user_action(
                interaction,
                "send_invite_after_friend.start",
                record_id=record.id,
                steam_id64=record.steam_id64,
            )
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
        gc_ready_timeout_ms = 40000
        invite_attempts = 3
        gc_ready_attempts = 3
        runtime_budget_ms = (
            gc_ready_timeout_ms * max(gc_ready_attempts, 1)
            + invite_timeout_ms * max(invite_attempts, 1)
        )
        invite_task_timeout = min(300.0, max(60.0, runtime_budget_ms / 1000 + 15.0))

        log.info(
            "Steam invite timing config: invite_timeout_ms=%s, gc_ready_timeout_ms=%s, invite_attempts=%s, gc_ready_attempts=%s, task_timeout=%s",
            invite_timeout_ms, gc_ready_timeout_ms, invite_attempts, gc_ready_attempts, invite_task_timeout
        )

        stop_anim = asyncio.Event()
        anim_task = None
        if isinstance(interaction, discord.Interaction):
            base_msg = "⏳ Einladung wird über Steam verschickt"
            try:
                if interaction.response.is_done():
                    await self._edit_original_response(interaction, content=f"{base_msg}...")
                else:
                    await self._response_send_message(interaction, f"{base_msg}...", ephemeral=True)
                anim_task = asyncio.create_task(self._animate_processing(interaction, base_msg, stop_anim))
            except Exception as exc:
                log.debug("Could not start invite progress animation: %s", exc)

        try:
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
            
            # Stoppe Animation vor dem Senden des Endergebnisses
            stop_anim.set()
            await self._await_animation_task(anim_task)

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
        except Exception as exc:
            stop_anim.set()
            await self._await_animation_task(anim_task)
            log.exception("Steam invite task failed with exception")
            _update_invite(
                record.id,
                status=STATUS_ERROR,
                last_error=f"Interner Fehler: {exc}",
            )
            await self._followup_send(
                interaction,
                "❌ Einladung fehlgeschlagen wegen eines internen Fehlers. Bitte versuche es später erneut.",
                ephemeral=True,
            )
            return False
        
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
                    # GC-Response ist doppelt verschachtelt: data.response.response
                    inner_response = None
                    if isinstance(response, Mapping):
                        inner_response = response.get("response")
                        if isinstance(inner_response, Mapping):
                            formatted = _format_gc_response_error(inner_response)
                            if formatted:
                                error_text = formatted
                        else:
                            formatted = _format_gc_response_error(response)
                            if formatted:
                                error_text = formatted

                    # Sammle alle Error-Texte für pattern matching (inner response + outer error)
                    gc_key = ""
                    gc_message = ""
                    if isinstance(inner_response, Mapping):
                        gc_key = str(inner_response.get("key") or "").lower()
                        gc_message = str(inner_response.get("message") or "").lower()
                    all_error_text = f"{result_error or ''} {error_text} {gc_key} {gc_message}".lower()

                    # Spezielle Behandlung für bekannte Deadlock GC Probleme
                    if "timeout" in all_error_text or is_timeout:
                        if "deadlock" in all_error_text or "gc" in all_error_text:
                            error_text = "⚠️ Deadlock Game Coordinator ist überlastet. Bitte versuche es in 10-15 Minuten erneut."
                        else:
                            error_text = "⚠️ Timeout beim Warten auf Steam-Antwort. Bitte versuche es erneut."
                    elif "alreadyhasgame" in gc_key or "already has game" in all_error_text or "already has access" in all_error_text or "bereits" in gc_message:
                        error_text = "✅ Account besitzt bereits Deadlock-Zugang. Prüfe deine Steam-Bibliothek."
                    elif "invite limit" in all_error_text or "limit reached" in all_error_text:
                        error_text = "⚠️ Tägliches Invite-Limit erreicht. Bitte morgen erneut versuchen."
                    elif "not friends long enough" in all_error_text:
                        error_text = "ℹ️ Steam-Freundschaft muss mindestens 30 Tage bestehen"
                    elif "limiteduser" in gc_key or "limited user" in all_error_text or "restricted account" in all_error_text or "eingeschränkt" in gc_message:
                        error_text = "⚠️ Steam-Account ist eingeschränkt (Limited User). Aktiviere deinen Account in Steam."
                    elif "invalid friend" in all_error_text:
                        error_text = "ℹ️ Accounts sind nicht als Steam-Freunde verknüpft"
            
            # Spezielle Behandlung für Timeout-Fälle
            if is_timeout and "timeout" not in error_text.lower():
                error_text = f"⚠️ Timeout: {error_text}"

            # AlreadyHasGame ist kein echter Fehler - User hat das Spiel schon
            already_has_game = "bereits" in error_text.lower() or "already" in error_text.lower()

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
                "already_has_game": already_has_game,
            }
            try:
                serialized_details = json.dumps(details, ensure_ascii=False, default=str)
            except TypeError:
                serialized_details = str(details)

            if already_has_game:
                # Spiel ist schon vorhanden - als Erfolg werten
                _failure_log.info("Invite not needed (already has game): %s", serialized_details)
                _update_invite(
                    record.id,
                    status=STATUS_INVITE_SENT,
                    invite_sent_at=now_ts,
                    last_error=None,
                )
                self._record_successful_invite(interaction, record, now_ts)
                msg = "✅ Dein Account besitzt bereits Deadlock-Zugang! Prüfe deine Steam-Bibliothek oder https://store.steampowered.com/account/playtestinvites ."
                if isinstance(interaction, discord.Interaction) and interaction.response.is_done():
                    try:
                        await self._edit_original_response(interaction, content=msg, view=None)
                    except Exception:
                        await self._followup_send(interaction, msg, ephemeral=True)
                else:
                    await self._followup_send(interaction, msg, ephemeral=True)
                await self._trigger_immediate_role_assignment(record.discord_id)
                return True

            _failure_log.error("Invite task failed: %s", serialized_details)
            _update_invite(
                record.id,
                status=STATUS_ERROR,
                last_error=str(error_text),
            )

            err_msg = f"❌ Einladung fehlgeschlagen:\n**{error_text}**\n\nFalls du denkst, dass das ein Fehler ist, melde dich bitte bei {BETA_INVITE_SUPPORT_CONTACT}."
            if isinstance(interaction, discord.Interaction) and interaction.response.is_done():
                try:
                    await self._edit_original_response(interaction, content=err_msg, view=None)
                except Exception:
                    await self._followup_send(interaction, err_msg, ephemeral=True)
            else:
                await self._followup_send(interaction, err_msg, ephemeral=True)

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
            f"Steam-Account: https://steamcommunity.com/profiles/{record.steam_id64}\n"
            "Bitte schaue in 1-2 Tagen unter https://store.steampowered.com/account/playtestinvites rein "
            "und nimm die Einladung dort an. Danach erscheint Deadlock automatisch in deiner Bibliothek.\n"
            "⚠️ Verlässt du den Server wird der Invite ungültig, egal ob dein Invite noch aussteht oder du Deadlock schon hast."
        )
        
        if isinstance(interaction, discord.Interaction) and interaction.response.is_done():
            try:
                await self._edit_original_response(interaction, content=message, view=None)
            except Exception:
                await self._followup_send(interaction, message, ephemeral=True)
        else:
            await self._followup_send(interaction, message, ephemeral=True)

        try:
            await self._send_user_dm(interaction.user, message, interaction=interaction)
        except Exception:  # pragma: no cover - DM optional
            log.debug("Konnte Bestätigungs-DM nicht senden", exc_info=True)

        return True

    async def handle_confirmation(self, interaction: discord.Interaction, record_id: int) -> None:
        self._trace_user_action(interaction, "handle_confirmation.start", record_id=record_id)
        record = _fetch_invite_by_id(record_id)
        if record is None:
            await self._response_send_message(
                interaction,
                "❌ Kein Eintrag für diese Einladung gefunden. Bitte starte den Vorgang mit `/betainvite` neu.",
                ephemeral=True,
            )
            return

        if record.discord_id != interaction.user.id:
            await self._response_send_message(
                interaction,
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
            await self._response_send_message(
                interaction,
                _build_already_invited_message(record, record.steam_id64),
                ephemeral=True,
            )
            return

        stop_anim = asyncio.Event()
        anim_task = None
        try:
            # Buttons sofort entfernen und Feedback geben
            base_msg = "⏳ Freundschaft wird geprüft"
            await self._response_edit_message(interaction, content=f"{base_msg}...", view=None)
            anim_task = asyncio.create_task(self._animate_processing(interaction, base_msg, stop_anim))
        except discord.errors.NotFound:
            log.warning("Confirmation interaction expired before edit")
            await self._followup_send(
                interaction,
                "⏱️ Die Bestätigung hat zu lange gedauert. Bitte versuche es erneut.",
                ephemeral=True
            )
            return
        except Exception as e:
            log.error(f"Failed to edit confirmation interaction: {e}")
            try:
                await self._response_defer(interaction, ephemeral=True, thinking=True)
            except Exception as defer_exc:
                log.debug("Confirmation interaction could not be deferred after edit failure: %s", defer_exc)

        try:
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
                    
                    log.info(
                        "Friendship check: discord_id=%s, steam_id64=%s, friend_ok=%s, relationship=%s, source=%s, cache_age_ms=%s",
                        record.discord_id, record.steam_id64, friend_ok, relationship_name, friend_source, cache_age
                    )
                    
                    if data.get("account_id") is not None and data.get("account_id") != record.account_id:
                        record = _update_invite(record.id, account_id=int(data["account_id"])) or record
            
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
                stop_anim.set()
                await self._await_animation_task(anim_task)
                await self._edit_original_response(
                    interaction,
                    content="ℹ️ Wir sind noch keine bestätigten Steam-Freunde. Bitte nimm die Freundschaftsanfrage an und probiere es erneut."
                )
                return

            # Freundschaft bestätigt → sofort verified=1 + Rolle
            await self._sync_verified_on_friendship(record.discord_id, record.steam_id64)

            await self._send_invite_after_friend(
                interaction,
                record,
                account_id_hint=account_id_from_friend,
            )
        finally:
            stop_anim.set()
            await self._await_animation_task(anim_task)

    def _build_panel_embed(self) -> discord.Embed:
        description = (
            "Klick auf **Invite starten**, um den eine Deadlock einladung zu erhalten.\n"
            "\n"
            f"Fragen? {SUPPORT_CHANNEL}."
        )
        return discord.Embed(
            title="🎟️ Deadlock Beta-Invite abholen",
            description=description,
            color=discord.Color.blurple(),
        )

    async def start_invite_from_panel(self, interaction: discord.Interaction) -> None:
        await self._start_betainvite_flow(interaction)

    async def _animate_processing(self, interaction: discord.Interaction, base_text: str, stop_event: asyncio.Event) -> None:
        """Anmiert Punkte (. .. ...) hinter einem Text, bis stop_event gesetzt ist."""
        _trace_interaction_event("ui_animation_started", interaction, base_text=base_text)
        dots = ["", ".", "..", "..."]
        idx = 0
        try:
            while not stop_event.is_set():
                dot_text = dots[idx % len(dots)]
                try:
                    await interaction.edit_original_response(content=f"{base_text}{dot_text}")
                except Exception:
                    break # Abbrechen wenn Nachricht gelöscht o.ä.
                idx += 1
                await asyncio.sleep(1.2)
        except asyncio.CancelledError:
            return
        finally:
            _trace_interaction_event("ui_animation_stopped", interaction, base_text=base_text)

    async def _sync_verified_on_friendship(self, discord_id: int, steam_id64: str) -> None:
        """Sofort-Sync wenn eine Steam-Freundschaft bestätigt wird: verified=1 setzen + Rolle geben."""
        try:
            with db.get_conn() as conn:
                conn.execute(
                    "UPDATE steam_links SET verified=1, updated_at=CURRENT_TIMESTAMP WHERE user_id=? AND steam_id=?",
                    (int(discord_id), steam_id64),
                )
            log.info("Friend-Sync: verified=1 gesetzt für discord=%s, steam=%s", discord_id, steam_id64)
        except Exception:
            log.exception("Friend-Sync: Konnte verified=1 nicht setzen für discord=%s", discord_id)

        await self._trigger_immediate_role_assignment(discord_id)
        _trace("friend_sync_verified", discord_id=discord_id, steam_id64=steam_id64)

    async def _trigger_immediate_role_assignment(self, user_id: int) -> None:
        """Versucht, dem Nutzer sofort die Steam-Verified Rolle zu geben."""
        try:
            verified_cog = self.bot.get_cog("SteamVerifiedRole")
            if verified_cog and hasattr(verified_cog, "assign_verified_role"):
                result = await verified_cog.assign_verified_role(user_id)
                if result:
                    log.info("Sofort-Rollen-Zuweisung erfolgreich für User %s", user_id)
                else:
                    log.warning("Sofort-Rollen-Zuweisung fehlgeschlagen für User %s (assign returned False)", user_id)
            else:
                log.warning("SteamVerifiedRole Cog nicht gefunden - Sofort-Zuweisung nicht möglich für User %s", user_id)
        except Exception:
            log.exception("Konnte Sofort-Rollen-Zuweisung nicht triggern für User %s", user_id)

    async def _start_betainvite_flow(self, interaction: discord.Interaction) -> None:
        self._trace_user_action(interaction, "betainvite_flow.start")
        stop_anim = asyncio.Event()
        anim_task = None
        try:
            # Unterscheiden zwischen Panel (öffentlich) und Button (ephemeral)
            is_panel = False
            if interaction.data and isinstance(interaction.data, dict):
                is_panel = interaction.data.get("custom_id") == BETA_INVITE_PANEL_CUSTOM_ID

            base_msg = "⏳ Status wird geprüft"
            if interaction.response.is_done():
                await self._edit_original_response(interaction, content=f"{base_msg}...", view=None)
            elif is_panel:
                await self._response_defer(interaction, ephemeral=True, thinking=True)
                await self._edit_original_response(interaction, content="⏳ Einladung wird vorbereitet...")
                base_msg = "⏳ Einladung wird vorbereitet"
            else:
                await self._response_edit_message(interaction, content=f"{base_msg}...", view=None)
            
            # Starte Animation im Hintergrund
            anim_task = asyncio.create_task(self._animate_processing(interaction, base_msg, stop_anim))
        except Exception as e:
            log.error(f"Failed to defer/edit interaction: {e}")
            _trace("betainvite_defer_error", discord_id=getattr(interaction.user, "id", None), error=str(e))
            return

        try:
            # 1. Zuerst Steam-Verknüpfung prüfen
            #    Retry mit kurzen Pausen: OAuth-Callback könnte noch in Flight sein
            #    wenn der User "Weiter" direkt nach dem Steam-Login klickt.
            steam_id = _lookup_primary_steam_id(interaction.user.id)
            if not steam_id:
                for _ in range(5):
                    await asyncio.sleep(3)
                    steam_id = _lookup_primary_steam_id(interaction.user.id)
                    if steam_id:
                        break

            if not steam_id:
                stop_anim.set()
                await self._await_animation_task(anim_task)

                view = self._build_link_prompt_view(interaction.user)
                prompt = (
                    "Bevor wir fortfahren können, musst du deinen Steam-Account verknüpfen.\n"
                    "Nutze einen der unten verfügbaren Login-Optionen. Sobald du fertig bist, klicke auf **Weiter**."
                )
                await self._edit_original_response(interaction, content=prompt, view=view)
                _trace("betainvite_no_link", discord_id=interaction.user.id)
                return

            # Wenn verknüpft: Sofort verifiziert setzen und Rolle geben
            await self._sync_verified_on_friendship(interaction.user.id, steam_id)

            # 2. Intent prüfen / abfragen
            intent_record = _get_intent_record(interaction.user.id)
            if intent_record is None:
                stop_anim.set()
                await self._await_animation_task(anim_task)
                await self._prompt_intent_gate(interaction)
                return

            # 3. Wenn Invite-Only, Zahlung tracken und Info senden
            if intent_record.intent == INTENT_INVITE_ONLY:
                stop_anim.set()
                await self._await_animation_task(anim_task)
                
                # Merke uns den Nutzer für den Webhook (24h), Token für Zuordnung
                payment_token = _register_pending_payment(interaction.user.id, interaction.user.name)

                # Backup Benachrichtigung für Admin (DM)
                admin_id = 662995601738170389
                try:
                    admin_user = self.bot.get_user(admin_id) or await self.bot.fetch_user(admin_id)
                    if admin_user:
                        await self._send_user_dm(
                            admin_user,
                            f"ℹ️ **Zahlungs-Backup**: {interaction.user.mention} (`{interaction.user.name}`) "
                            f"hat gerade die Bezahl-Info angefordert (Token: `{payment_token}`) und könnte jetzt bezahlen.",
                            interaction=interaction,
                        )
                except Exception as e:
                    log.debug(f"Konnte Admin-Backup-DM nicht senden: {e}")

                view = InviteOnlyPaymentView(KOFI_PAYMENT_URL)
                await self._edit_original_response(
                    interaction,
                    content=_make_payment_message(payment_token),
                    view=view,
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
            await self._process_invite_request(interaction)
        finally:
            stop_anim.set()
            await self._await_animation_task(anim_task)

    @app_commands.command(name="betainvite", description="Automatisiert eine Deadlock-Playtest-Einladung anfordern.")
    async def betainvite(self, interaction: discord.Interaction) -> None:
        self._trace_user_action(interaction, "command.betainvite")
        await self._start_betainvite_flow(interaction)

    @app_commands.command(
        name="publish_betainvite_panel",
        description="(Admin) Beta-Invite-Panel im aktuellen oder angegebenen Kanal posten.",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def publish_betainvite_panel(
        self,
        interaction: discord.Interaction,
        channel: Optional[Union[discord.TextChannel, discord.Thread]] = None,
    ) -> None:
        self._trace_user_action(interaction, "command.publish_betainvite_panel")
        target_channel = channel or interaction.channel
        if not isinstance(target_channel, (discord.TextChannel, discord.Thread)):
            await self._response_send_message(
                interaction,
                "❌ Bitte führe den Befehl in einem Textkanal aus oder gib einen Textkanal an.",
                ephemeral=True,
            )
            return

        embed = self._build_panel_embed()
        view = BetaInvitePanelView(self)
        try:
            await self._send_channel_message(target_channel, embed=embed, view=view, interaction=interaction)
        except Exception as exc:  # pragma: no cover - nur Laufzeit-Rechtefehler
            log.warning("Konnte Beta-Invite-Panel nicht senden: %s", exc)
            await self._response_send_message(
                interaction,
                "❌ Panel konnte nicht gesendet werden (fehlende Rechte?).",
                ephemeral=True,
            )
            return

        await self._response_send_message(
            interaction,
            f"✅ Panel in {target_channel.mention} gesendet.",
            ephemeral=True,
        )

    @app_commands.command(
        name="betainvite_stats",
        description="(Admin) Funnel-Auswertung: Wer hat angeklickt, wer hat abgebrochen, wer ist Geier?",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def betainvite_stats(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            stats = await asyncio.to_thread(_get_funnel_stats)
        except Exception as exc:
            await interaction.followup.send(f"❌ Fehler beim Laden der Stats: {exc}", ephemeral=True)
            return

        guild = interaction.guild
        total_intent = stats["intent_community"] + stats["intent_invite_only"]

        def pct(part: int, total: int) -> str:
            return f"{round(part / total * 100)}%" if total else "–"

        # Geier-Namen auflösen
        geier_lines: list[str] = []
        for entry in stats["geier"][:25]:  # max 25 in der Anzeige
            uid = entry["discord_id"]
            member = guild.get_member(uid) if guild else None
            if not member and guild:
                try:
                    member = await guild.fetch_member(uid)
                except Exception:
                    log.debug("Beta invite stats: failed to fetch member uid=%s", uid, exc_info=True)
            decided = datetime.fromtimestamp(entry["decided_at"], tz=timezone.utc).strftime("%d.%m.%y")
            name = f"{member.mention} (`{member.name}`)" if member else f"<@{uid}> (nicht mehr im Server)"
            geier_lines.append(f"  • {name} — seit {decided}")

        geier_count = len(stats["geier"])
        geier_text = "\n".join(geier_lines) if geier_lines else "  Keine"
        if geier_count > 25:
            geier_text += f"\n  … und {geier_count - 25} weitere"

        msg = (
            "## 📊 Beta Invite Funnel\n"
            f"**Panel angeklickt:** {stats['panel_unique']} Unique-User ({stats['panel_total']} gesamt, ab jetzt getrackt)\n"
            f"**Intent-Wahl gesamt:** {total_intent}\n\n"
            "### 🎮 Mitspielen (Community)\n"
            f"  Haben gewählt: **{stats['intent_community']}**\n"
            f"  Invite-Record erstellt: **{stats['community_has_record']}** ({pct(stats['community_has_record'], stats['intent_community'])})\n"
            f"  Invite erfolgreich: **{stats['community_invite_sent']}** ({pct(stats['community_invite_sent'], stats['intent_community'])})\n"
            f"  Abgebrochen (>1h, kein Record): **{stats['community_dropout']}**\n\n"
            "### 💰 Invite Only (Ko-fi)\n"
            f"  Haben gewählt: **{stats['invite_only_total']}**\n"
            f"  Ko-fi-Link erhalten (Token): **{stats['invite_only_got_link']}**\n"
            f"  Invite bekommen: **{stats['invite_only_invite_sent']}** ({pct(stats['invite_only_invite_sent'], stats['invite_only_total'])})\n"
            f"  Nie bezahlt (Geier 🦅): **{geier_count}**\n\n"
            f"**Invite-Statuses gesamt:** {stats['invite_statuses']}\n\n"
            f"### 🦅 Geier-Liste (invite_only gewählt, nie bezahlt)\n{geier_text}"
        )

        # Discord-Limit: 2000 Zeichen per Nachricht
        if len(msg) > 1900:
            await interaction.followup.send(msg[:1900] + "\n…(abgeschnitten)", ephemeral=True)
        else:
            await interaction.followup.send(msg, ephemeral=True)

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


def _extract_kofi_token(payload: Mapping[str, Any], headers: Mapping[str, Any]) -> str:
    candidates: list[str] = []
    for source in (
        payload,
        payload.get("data") if isinstance(payload.get("data"), Mapping) else {},
    ):
        if not isinstance(source, Mapping):
            continue
        for key in ("verification_token", "verificationToken"):
            value = source.get(key)
            if value:
                candidates.append(str(value).strip())

    for header_key in ("X-Verification-Token", "X-Kofi-Token"):
        header_value = headers.get(header_key) if hasattr(headers, "get") else None
        if header_value:
            candidates.append(str(header_value).strip())

    return next((candidate for candidate in candidates if candidate), "")


async def _parse_kofi_request_payload(request: Any) -> Mapping[str, Any]:
    try:
        raw_body = await request.body()
    except Exception:
        return {}

    text = raw_body.decode("utf-8", errors="ignore").strip()
    if not text:
        return {}

    parsed: Any
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        qs_payload = parse_qs(text)
        if "data" in qs_payload and qs_payload.get("data"):
            data_field = qs_payload.get("data", [""])[0]
            try:
                parsed = json.loads(data_field)
            except json.JSONDecodeError:
                parsed = {"data": data_field}
        else:
            parsed = {key: values[0] if len(values) == 1 else values for key, values in qs_payload.items()}

    if not isinstance(parsed, Mapping):
        return {}

    data_field = parsed.get("data")
    if isinstance(data_field, str):
        try:
            parsed["data"] = json.loads(data_field)
        except json.JSONDecodeError:
            parsed["data"] = data_field
    return parsed


async def _start_kofi_webhook_server(beta_invite: BetaInviteFlow) -> None:
    if not all((FastAPI, HTTPException, Request, JSONResponse, uvicorn)):
        log.warning("Ko-fi Webhook deaktiviert (fastapi/uvicorn fehlt)")
        beta_invite._kofi_webhook_task = None
        return

    # Ist bereits ein Ko-fi-Server erreichbar? Dann nicht doppelt starten.
    already_running, health_error = await asyncio.to_thread(
        _probe_kofi_health,
        KOFI_WEBHOOK_HOST,
        int(KOFI_WEBHOOK_PORT),
    )
    if already_running:
        message = (
            "Ko-fi Webhook-Server läuft bereits auf "
            f"{KOFI_WEBHOOK_HOST}:{KOFI_WEBHOOK_PORT}; überspringe zweiten Start."
        )
        log.info(message)
        _trace(
            "kofi_webhook_server_already_running",
            host=KOFI_WEBHOOK_HOST,
            port=KOFI_WEBHOOK_PORT,
        )
        beta_invite._kofi_server = None
        beta_invite._kofi_webhook_task = None
        return
    if health_error:
        log.debug(
            "Ko-fi Webhook Health-Check fehlgeschlagen (vermutlich kein Server aktiv): %s",
            health_error,
        )

    # Retry logic for port availability during reloads
    max_retries = 5
    retry_delay = 0.5
    port_available = False
    port_error = None

    for attempt in range(max_retries):
        port_available, port_error = _can_bind_port(KOFI_WEBHOOK_HOST, int(KOFI_WEBHOOK_PORT))
        if port_available:
            break
        if attempt < max_retries - 1:
            log.debug("Port %s:%s belegt, versuche es erneut in %ss... (Versuch %s/%s)", 
                      KOFI_WEBHOOK_HOST, KOFI_WEBHOOK_PORT, retry_delay, attempt + 1, max_retries)
            await asyncio.sleep(retry_delay)
            retry_delay *= 2  # Exponential backoff

    if not port_available:
        message = (
            f"Ko-fi Webhook-Server konnte nicht starten: "
            f"Port {KOFI_WEBHOOK_HOST}:{KOFI_WEBHOOK_PORT} belegt ({port_error})"
        )
        log.error(message)
        await beta_invite._notify_log_channel(message)
        beta_invite._kofi_server = None
        beta_invite._kofi_webhook_task = None
        return

    app = FastAPI(
        title="Deadlock Ko-fi Webhook",
        docs_url=None,
        redoc_url=None,
    )

    @app.get("/kofi-health")
    async def kofi_health() -> Mapping[str, Any]:
        return {"ok": True}

    async def kofi_webhook(request: Any) -> JSONResponse:
        payload = await _parse_kofi_request_payload(request)
        if not payload:
            raise HTTPException(status_code=400, detail="Invalid payload")

        payload_keys = list(payload.keys()) if isinstance(payload, Mapping) else None
        _trace("kofi_webhook_http_received", payload_keys=payload_keys)

        token = _extract_kofi_token(payload, request.headers)
        expected = str(KOFI_VERIFICATION_TOKEN or "").strip()
        if expected and token != expected:
            _trace(
                "kofi_webhook_invalid_verification_token",
                token_present=bool(token),
                token_len=len(token or ""),
                expected_len=len(expected or ""),
            )
            raise HTTPException(status_code=401, detail="Invalid verification token")

        asyncio.create_task(beta_invite.handle_kofi_webhook(payload))
        return JSONResponse(content={"ok": True, "queued": True}, status_code=200)

    # Use a plain Starlette route here to avoid FastAPI parameter model parsing
    # edge cases for "request" in postponed annotations mode.
    app.add_route(KOFI_WEBHOOK_PATH, kofi_webhook, methods=["POST"])

    config = uvicorn.Config(
        app=app,
        host=KOFI_WEBHOOK_HOST,
        port=int(KOFI_WEBHOOK_PORT),
        log_level="info",
        loop="asyncio",
    )
    server = uvicorn.Server(config=config)
    beta_invite._kofi_server = server
    _trace(
        "kofi_webhook_server_start",
        host=KOFI_WEBHOOK_HOST,
        port=KOFI_WEBHOOK_PORT,
        path=KOFI_WEBHOOK_PATH,
    )
    try:
        await server.serve()
    except asyncio.CancelledError:
        server.should_exit = True
        raise
    except SystemExit:  # pragma: no cover - uvicorn exits with SystemExit on startup errors
        message = (
            "Ko-fi Webhook-Server gestoppt: Start fehlgeschlagen "
            f"(Port {KOFI_WEBHOOK_HOST}:{KOFI_WEBHOOK_PORT} bereits belegt?)."
        )
        log.error(message, exc_info=True)
        await beta_invite._notify_log_channel(message)
    except Exception as exc:  # pragma: no cover - runtime network errors
        log.exception("Ko-fi Webhook-Server gestoppt aufgrund eines Fehlers", exc_info=True)
        await beta_invite._notify_log_channel(f"Ko-fi Webhook-Server gestoppt: {exc}")
    finally:
        beta_invite._kofi_server = None
        beta_invite._kofi_webhook_task = None
        _trace("kofi_webhook_server_stopped")


async def setup(bot: commands.Bot) -> None:
    beta_invite_cog = BetaInviteFlow(bot)
    await bot.add_cog(beta_invite_cog)
    if not beta_invite_cog._kofi_webhook_task:
        beta_invite_cog._kofi_webhook_task = asyncio.create_task(
            _start_kofi_webhook_server(beta_invite_cog)
        )

    for command in (
        beta_invite_cog.betainvite,
        beta_invite_cog.publish_betainvite_panel,
    ):
        try:
            bot.tree.add_command(command)
        except app_commands.CommandAlreadyRegistered:
            bot.tree.remove_command(
                command.name,
                type=discord.AppCommandType.chat_input,
            )
            bot.tree.add_command(command)
