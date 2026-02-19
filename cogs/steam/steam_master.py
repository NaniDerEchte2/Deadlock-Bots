"""Steam hub cog.

The Python "Steam Master" acts as the management plane for the Node.js Steam
bridge.  It orchestrates work via the shared ``steam_tasks`` table while the
bridge performs the actual Steam actions.

Responsibilities of this cog:

* Provide Discord commands (and programmatic helpers) that enqueue Steam auth
  tasks.
* Await task completion and surface the status/result back to administrators.
* Offer utility helpers for inspecting the persisted refresh-/machine tokens
  the bridge keeps on disk.

All realtime Steam interactions live in :mod:`cogs.steam.steam_presence`.  This
cog focuses purely on task management and reporting.
"""

from __future__ import annotations

import asyncio
import enum
import json
import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from discord.ext import commands

from cogs.steam.token_vault import (
    clear_tokens as clear_steam_tokens,
    machine_auth_token_exists,
    refresh_token_exists,
    token_storage_mode,
)
from service import db

log = logging.getLogger(__name__)


class SteamMasterMode(enum.Enum):
    """Operational mode for the hub cog."""

    HUB = "hub"
    DISABLED = "disabled"


class SteamTaskError(RuntimeError):
    """Raised when task orchestration fails."""


class SteamLoginFlags(commands.FlagConverter, case_insensitive=True):
    """Supported flags for the ``steam_login`` command."""

    use_refresh_token: Optional[bool] = commands.flag(default=None, aliases=["refresh"])
    force_credentials: bool = commands.flag(default=False, aliases=["force", "credentials"])
    account_name: Optional[str] = commands.flag(default=None, aliases=["account", "user", "username"])
    password: Optional[str] = commands.flag(default=None, aliases=["pass", "pw"])
    refresh_token: Optional[str] = commands.flag(default=None, aliases=["rtoken"])
    two_factor_code: Optional[str] = commands.flag(default=None, aliases=["twofactor", "totp"])
    auth_code: Optional[str] = commands.flag(default=None, aliases=["guard"])
    remember_password: Optional[bool] = commands.flag(default=None, aliases=["remember"])
    machine_auth_token: Optional[str] = commands.flag(default=None, aliases=["machine"])


@dataclass(slots=True)
class SteamTaskOutcome:
    """Represents the observed outcome of a Steam task."""

    task_id: int
    status: str
    result: Optional[Any]
    error: Optional[str]
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.status.upper() == "DONE" and not self.timed_out


class SteamTaskClient:
    """Small helper around the ``steam_tasks`` table."""

    def __init__(self, *, poll_interval: float = 0.5, default_timeout: float = 15.0) -> None:
        self.poll_interval = poll_interval
        self.default_timeout = default_timeout

    @staticmethod
    def _encode_payload(payload: Optional[Dict[str, Any]]) -> Optional[str]:
        if payload is None:
            return None
        try:
            return json.dumps(payload)
        except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
            raise SteamTaskError(f"Ungültiger Payload für Steam-Task: {exc}") from exc

    def enqueue(self, task_type: str, payload: Optional[Dict[str, Any]] = None) -> int:
        payload_json = self._encode_payload(payload)
        with db.get_conn() as conn:
            cur = conn.execute(
                "INSERT INTO steam_tasks(type, payload, status) VALUES(?, ?, 'PENDING')",
                (task_type, payload_json),
            )
            task_id = int(cur.lastrowid)
        log.debug("Enqueued steam task", extra={"task_id": task_id, "type": task_type})
        return task_id

    @staticmethod
    def _decode_result(result: Optional[str]) -> Optional[Any]:
        if result is None:
            return None
        try:
            return json.loads(result)
        except (TypeError, ValueError):  # pragma: no cover - logging only
            log.warning("Konnte Steam-Task-Resultat nicht als JSON lesen", extra={"result": result})
            return result

    async def wait(self, task_id: int, *, timeout: Optional[float] = None) -> SteamTaskOutcome:
        poll_interval = max(0.1, float(self.poll_interval))
        timeout = timeout if timeout is not None else self.default_timeout
        deadline = time.monotonic() + max(poll_interval, float(timeout))

        while True:
            with db.get_conn() as conn:
                row = conn.execute(
                    "SELECT status, result, error FROM steam_tasks WHERE id = ?",
                    (task_id,),
                ).fetchone()

            if row is None:
                return SteamTaskOutcome(task_id, "MISSING", None, "Task nicht gefunden", timed_out=True)

            status = str(row["status"]) if row["status"] is not None else "UNKNOWN"
            result = self._decode_result(row["result"])
            error = str(row["error"]) if row["error"] is not None else None

            if status.upper() in {"DONE", "FAILED"}:
                return SteamTaskOutcome(task_id, status.upper(), result, error, timed_out=False)

            if time.monotonic() >= deadline:
                return SteamTaskOutcome(task_id, status.upper(), result, error, timed_out=True)

            await asyncio.sleep(poll_interval)

    async def run(
        self,
        task_type: str,
        payload: Optional[Dict[str, Any]] = None,
        *,
        timeout: Optional[float] = None,
    ) -> SteamTaskOutcome:
        task_id = self.enqueue(task_type, payload)
        return await self.wait(task_id, timeout=timeout)


def _presence_data_dir() -> Path:
    """Resolve the Node.js presence bridge data directory."""

    configured = (os.getenv("STEAM_PRESENCE_DATA_DIR") or "").strip()
    if configured:
        return Path(configured).expanduser()

    return Path(__file__).resolve().parent / "steam_presence" / ".steam-data"


def _refresh_token_path() -> Path:
    return _presence_data_dir() / "refresh.token"


def _machine_auth_path() -> Path:
    return _presence_data_dir() / "machine_auth_token.txt"


def _determine_mode() -> SteamMasterMode:
    """Determine whether the hub should be active."""

    raw = (os.getenv("STEAM_MASTER_MODE") or "hub").strip().lower()
    if raw in {"disabled", "off", "none"}:
        return SteamMasterMode.DISABLED
    return SteamMasterMode.HUB


def _fetch_group_counts(sql: str) -> Dict[str, int]:
    stats: Dict[str, int] = defaultdict(int)
    try:
        with db.get_conn() as conn:
            rows = conn.execute(sql).fetchall()
        for row in rows:
            status = str(row[0]) if row[0] is not None else "unknown"
            try:
                stats[status] += int(row[1])
            except (TypeError, ValueError):
                stats[status] += 0
    except Exception:  # pragma: no cover - logging only
        log.exception("Failed to collect Steam statistics", extra={"query": sql})
    return stats


def _count_single(sql: str) -> Optional[int]:
    try:
        with db.get_conn() as conn:
            row = conn.execute(sql).fetchone()
        if not row:
            return 0
        return int(row[0])
    except Exception:  # pragma: no cover - logging only
        log.exception("Failed to fetch Steam counter", extra={"query": sql})
        return None


class SteamMaster(commands.Cog):
    """Discord cog providing hub-style Steam helpers."""

    def __init__(self, bot: commands.Bot, *, mode: Optional[SteamMasterMode] = None) -> None:
        self.bot = bot
        self.mode = mode or _determine_mode()
        self.tasks = SteamTaskClient()
        log.info("SteamMaster initialised in %s mode", self.mode.value)

    def enqueue_task(self, task_type: str, payload: Optional[Dict[str, Any]] = None) -> int:
        """Expose task creation for other components."""

        return self.tasks.enqueue(task_type, payload)

    async def run_task(
        self,
        task_type: str,
        payload: Optional[Dict[str, Any]] = None,
        *,
        timeout: Optional[float] = None,
    ) -> SteamTaskOutcome:
        """Expose the awaitable task helper for other components."""

        return await self.tasks.run(task_type, payload, timeout=timeout)

    @staticmethod
    def _format_stats(title: str, stats: Dict[str, int]) -> str:
        if not stats:
            return f"{title}: keine Einträge"
        parts = ", ".join(f"{status}={count}" for status, count in sorted(stats.items()))
        return f"{title}: {parts}"

    async def _bridge_status_lines(self) -> Dict[str, str]:
        outcome = await self.tasks.run("AUTH_STATUS", timeout=10.0)
        lines: Dict[str, str] = {}

        status_line = f"task_status={outcome.status.lower()}"
        if outcome.timed_out:
            status_line += " (timeout)"
        lines["task"] = status_line

        if outcome.error:
            lines["error"] = f"error={outcome.error}"

        payload = outcome.result if isinstance(outcome.result, dict) else {}
        if payload:
            last_error = payload.get("last_error")
            if isinstance(last_error, dict):
                last_error = last_error.get("message")
            lines["bridge"] = (
                "logged_on={lo} logging_in={li} guard_required={gr} last_error={err}".format(
                    lo="yes" if payload.get("logged_on") else "no",
                    li="yes" if payload.get("logging_in") else "no",
                    gr=payload.get("guard_required", "no"),
                    err=last_error or "-",
                )
            )
            lines["account"] = "account={acct} steam_id64={sid}".format(
                acct=payload.get("account_name") or "<unbekannt>",
                sid=payload.get("steam_id64") or "-",
            )
            lines["tokens"] = "tokens refresh={r} machine_auth={m}".format(
                r="yes" if payload.get("refresh_token_present") else "no",
                m="yes" if payload.get("machine_token_present") else "no",
            )

        return lines

    async def _hub_status(self) -> str:
        lines = ["mode=hub"]

        bridge_lines = await self._bridge_status_lines()
        lines.extend(bridge_lines.values())

        refresh = _refresh_token_path()
        machine = _machine_auth_path()
        lines.append(f"token_storage={token_storage_mode()}")
        lines.append(f"refresh_token={'yes' if refresh_token_exists(refresh) else 'no'}")
        lines.append(f"machine_auth={'yes' if machine_auth_token_exists(machine) else 'no'}")
        lines.append(f"token_fallback_paths refresh={refresh} machine={machine}")

        fr_stats = _fetch_group_counts(
            "SELECT status, COUNT(*) FROM steam_friend_requests GROUP BY status"
        )
        lines.append(self._format_stats("friend_requests", fr_stats))

        invite_stats = _fetch_group_counts(
            "SELECT status, COUNT(*) FROM steam_quick_invites GROUP BY status"
        )
        lines.append(self._format_stats("quick_invites", invite_stats))

        links_count = _count_single(
            "SELECT COUNT(DISTINCT steam_id) FROM steam_links WHERE steam_id IS NOT NULL AND steam_id != ''"
        )
        if links_count is not None:
            lines.append(f"linked_accounts={links_count}")

        return "\n".join(lines)

    # ---------- commands ----------
    @commands.command(name="steam_login")
    @commands.has_permissions(administrator=True)
    async def cmd_login(
        self,
        ctx: commands.Context,
        *,
        flags: Optional[SteamLoginFlags] = None,
    ) -> None:
        """Trigger a login attempt via the Node.js bridge.

        Beispiele:
        ``!steam_login`` – nutzt vorhandene Tokens/Zugangsdaten.
        ``!steam_login --force --account=mybot --password=...`` – zwingt
        Benutzername/Passwort.
        ``!steam_login --refresh=false`` – unterdrückt den Refresh-Token.
        """

        payload: Dict[str, Any] = {}
        if flags:
            if flags.use_refresh_token is not None:
                payload["use_refresh_token"] = bool(flags.use_refresh_token)
            if flags.force_credentials:
                payload["force_credentials"] = True
            if flags.account_name:
                payload["account_name"] = flags.account_name
            if flags.password:
                payload["password"] = flags.password
            if flags.refresh_token:
                payload["refresh_token"] = flags.refresh_token
            if flags.two_factor_code:
                payload["two_factor_code"] = flags.two_factor_code
            if flags.auth_code:
                payload["auth_code"] = flags.auth_code
            if flags.remember_password is not None:
                payload["remember_password"] = bool(flags.remember_password)
            if flags.machine_auth_token:
                payload["machine_auth_token"] = flags.machine_auth_token

        async with ctx.typing():
            outcome = await self.tasks.run("AUTH_LOGIN", payload or None, timeout=20.0)

        if outcome.timed_out:
            await ctx.reply(
                f"⏳ Login-Task #{outcome.task_id} wartet noch auf den Bridge-Worker."
            )
            return

        if not outcome.ok:
            error = outcome.error or "unbekannter Fehler"
            await ctx.reply(f"❌ Login fehlgeschlagen: {error}")
            return

        result = outcome.result if isinstance(outcome.result, dict) else {}
        if not result.get("started", False):
            reason = result.get("reason", "unbekannt")
            await ctx.reply(f"ℹ️ Kein Login gestartet ({reason}).")
            return

        via = "Refresh-Token" if result.get("using_refresh_token") else "Zugangsdaten"
        await ctx.reply(f"✅ Login gestartet über {via}.")

    @commands.command(name="steam_guard", aliases=["sg", "steamguard"])
    @commands.has_permissions(administrator=True)
    async def cmd_guard(self, ctx: commands.Context, code: str) -> None:
        """Submit a Steam Guard code through the bridge."""

        payload = {"code": code.strip()}
        async with ctx.typing():
            outcome = await self.tasks.run("AUTH_GUARD_CODE", payload, timeout=15.0)

        if outcome.timed_out:
            await ctx.reply(
                f"⏳ Guard-Task #{outcome.task_id} wurde noch nicht vom Bridge-Worker verarbeitet."
            )
            return

        if not outcome.ok:
            await ctx.reply(f"❌ Guard-Code fehlgeschlagen: {outcome.error or 'unbekannter Fehler'}")
            return

        result = outcome.result if isinstance(outcome.result, dict) else {}
        guard_type = result.get("type") or "unbekannt"
        await ctx.reply(f"✅ Guard-Code akzeptiert ({guard_type}).")

    @commands.command(name="steam_logout")
    @commands.has_permissions(administrator=True)
    async def cmd_logout(self, ctx: commands.Context) -> None:
        """Force a logout via the bridge."""

        async with ctx.typing():
            outcome = await self.tasks.run("AUTH_LOGOUT", timeout=10.0)

        if outcome.timed_out:
            await ctx.reply(
                f"⏳ Logout-Task #{outcome.task_id} wartet noch auf Verarbeitung."
            )
            return

        if not outcome.ok:
            await ctx.reply(f"❌ Logout fehlgeschlagen: {outcome.error or 'unbekannter Fehler'}")
            return

        await ctx.reply("✅ Logout ausgelöst.")

    @commands.command(name="steam_status")
    @commands.has_permissions(administrator=True)
    async def cmd_status(self, ctx: commands.Context) -> None:
        """Show current hub state."""

        async with ctx.typing():
            status_text = await self._hub_status()
        await ctx.reply(f"```{status_text}```")

    @commands.command(name="steam_token")
    @commands.has_permissions(administrator=True)
    async def cmd_token(self, ctx: commands.Context) -> None:
        """Display stored token information."""

        refresh = _refresh_token_path()
        machine = _machine_auth_path()
        await ctx.reply(
            "🔐 Speicher: {storage}\n"
            "refresh_token: {r}\n"
            "machine_auth: {m}\n"
            "Fallback-Pfade:\n- `{rp}`\n- `{mp}`".format(
                storage=token_storage_mode(),
                r="vorhanden" if refresh_token_exists(refresh) else "nicht vorhanden",
                m="vorhanden" if machine_auth_token_exists(machine) else "nicht vorhanden",
                rp=refresh,
                mp=machine,
            )
        )

    @commands.command(name="steam_token_clear")
    @commands.has_permissions(administrator=True)
    async def cmd_token_clear(self, ctx: commands.Context) -> None:
        """Clean up persisted refresh/machine tokens."""

        refresh = _refresh_token_path()
        machine = _machine_auth_path()
        removed = clear_steam_tokens(refresh_file_path=refresh, machine_file_path=machine)

        if removed:
            await ctx.reply("🧹 Gelöscht: {}".format(", ".join(removed)))
        else:
            await ctx.reply("ℹ️ Keine Tokens gefunden.")


async def setup(bot: commands.Bot) -> None:
    mode = _determine_mode()
    if mode is SteamMasterMode.DISABLED:
        log.info("SteamMaster cog disabled via STEAM_MASTER_MODE=%s", os.getenv("STEAM_MASTER_MODE"))
        return
    await bot.add_cog(SteamMaster(bot, mode=mode))
