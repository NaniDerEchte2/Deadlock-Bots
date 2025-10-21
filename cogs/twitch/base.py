"""Base implementation shared across the Twitch cog mixins."""

from __future__ import annotations

import asyncio
import os
from typing import Dict, Optional, Set

from urllib.parse import urlparse

import discord
from aiohttp import web
from discord.ext import commands

from . import storage
from .constants import (
    TWITCH_ALERT_CHANNEL_ID,
    TWITCH_ALERT_MENTION,
    TWITCH_CATEGORY_SAMPLE_LIMIT,
    TWITCH_DASHBOARD_HOST,
    TWITCH_DASHBOARD_NOAUTH,
    TWITCH_DASHBOARD_PORT,
    TWITCH_LANGUAGE,
    TWITCH_LOG_EVERY_N_TICKS,
    TWITCH_NOTIFY_CHANNEL_ID,
    TWITCH_REQUIRED_DISCORD_MARKER,
    TWITCH_TARGET_GAME_NAME,
)
from .logger import log
from .twitch_api import TwitchAPI


class TwitchBaseCog(commands.Cog):
    """Handle shared initialisation, shutdown and utility helpers."""

    def __init__(self, bot: commands.Bot):
        super().__init__()
        self.bot = bot

        # 🔒 Secrets nur aus ENV (nicht hardcoden!)
        self.client_id = os.getenv("TWITCH_CLIENT_ID") or ""
        self.client_secret = os.getenv("TWITCH_CLIENT_SECRET")

        # Runtime attributes initialised even if the cog is disabled
        self.api: Optional[TwitchAPI]
        self._web: Optional[web.AppRunner] = None
        self._web_app: Optional[web.Application] = None
        self._category_id: Optional[str] = None
        self._language_filter = (TWITCH_LANGUAGE or "").strip() or None
        self._tick_count = 0
        self._log_every_n = max(1, int(TWITCH_LOG_EVERY_N_TICKS or 5))
        self._category_sample_limit = max(50, int(TWITCH_CATEGORY_SAMPLE_LIMIT or 400))
        self._notify_channel_id = int(TWITCH_NOTIFY_CHANNEL_ID or 0)
        self._alert_channel_id = int(TWITCH_ALERT_CHANNEL_ID or 0)
        self._alert_mention = TWITCH_ALERT_MENTION or ""
        self._invite_codes: Dict[int, Set[str]] = {}
        self._twl_command: Optional[commands.Command] = None
        self._target_game_name = (TWITCH_TARGET_GAME_NAME or "").strip()
        self._target_game_lower = self._target_game_name.lower()

        # Dashboard/Auth (aus Config-Header)
        self._dashboard_token = os.getenv("TWITCH_DASHBOARD_TOKEN") or None
        self._dashboard_noauth = bool(TWITCH_DASHBOARD_NOAUTH)
        self._dashboard_host = TWITCH_DASHBOARD_HOST or (
            "127.0.0.1" if self._dashboard_noauth else "0.0.0.0"
        )
        self._dashboard_port = int(TWITCH_DASHBOARD_PORT)
        self._partner_dashboard_token = os.getenv("TWITCH_PARTNER_TOKEN") or None
        self._required_marker_default = TWITCH_REQUIRED_DISCORD_MARKER or None

        if not self.client_id or not self.client_secret:
            log.error("TWITCH_CLIENT_ID/SECRET not configured; cog disabled")
            self.api = None
            return

        self.api = TwitchAPI(self.client_id, self.client_secret)

        # Background tasks
        self.poll_streams.start()
        self.invites_refresh.start()
        self.bot.loop.create_task(self._ensure_category_id())
        self.bot.loop.create_task(self._start_dashboard())
        self.bot.loop.create_task(self._refresh_all_invites())

    # -------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------
    def cog_unload(self):
        """Ensure background resources are torn down when the cog is removed."""
        loops = (self.poll_streams, self.invites_refresh)

        async def _graceful_shutdown():
            for lp in loops:
                try:
                    if lp.is_running():
                        lp.cancel()
                except Exception:
                    log.exception("Konnte Loop nicht canceln: %r", lp)
            await asyncio.sleep(0)

            if self._web:
                try:
                    await self._stop_dashboard()
                except Exception:
                    log.exception("Dashboard shutdown fehlgeschlagen")

            if self.api is not None:
                try:
                    await self.api.aclose()
                except asyncio.CancelledError:
                    pass
                except Exception:
                    log.exception("TwitchAPI-Session konnte nicht geschlossen werden")

        try:
            self.bot.loop.create_task(_graceful_shutdown())
        except Exception:
            log.exception("Fehler beim Start des Shutdown-Tasks")

        try:
            if self._twl_command is not None:
                existing = self.bot.get_command(self._twl_command.name)
                if existing is self._twl_command:
                    self.bot.remove_command(self._twl_command.name)
        except Exception:
            log.exception("Konnte !twl-Command nicht deregistrieren")
        finally:
            self._twl_command = None

    def set_prefix_command(self, command: commands.Command) -> None:
        """Speichert die Referenz auf den dynamisch registrierten Prefix-Command."""
        self._twl_command = command

    # -------------------------------------------------------
    # DB-Helpers / Guild-Setup / Invites
    # -------------------------------------------------------
    def _set_channel(self, guild_id: int, channel_id: int) -> None:
        with storage.get_conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO twitch_guild_settings (guild_id, notify_channel_id) VALUES (?, ?)",
                (int(guild_id), int(channel_id)),
            )
        if self._notify_channel_id == 0:
            self._notify_channel_id = int(channel_id)

    async def _refresh_all_invites(self):
        """Alle Guild-Einladungen sammeln (für Link-Checks/Partner-Validierung sinnvoll)."""
        try:
            await self.bot.wait_until_ready()
        except Exception:
            log.exception("wait_until_ready fehlgeschlagen")
            return

        for guild in list(self.bot.guilds):
            try:
                await self._refresh_guild_invites(guild)
            except Exception:
                log.exception("Einladungen für Guild %s fehlgeschlagen", guild.id)

    async def _refresh_guild_invites(self, guild: discord.Guild):
        codes: Set[str] = set()
        try:
            invites = await guild.invites()
            for inv in invites:
                if inv.code:
                    codes.add(inv.code)
        except discord.Forbidden:
            log.warning("Fehlende Berechtigung, um Invites von Guild %s zu lesen", guild.id)
        except discord.HTTPException:
            log.exception("HTTP-Fehler beim Abruf der Invites für Guild %s", guild.id)

        self._invite_codes[guild.id] = codes

    # -------------------------------------------------------
    # Utils
    # -------------------------------------------------------
    @staticmethod
    def _normalize_login(raw: str) -> str:
        login = (raw or "").strip()
        if not login:
            return ""
        login = login.split("?")[0].split("#")[0].strip()
        lowered = login.lower()
        if "twitch.tv" in lowered:
            if "//" not in login:
                login = f"https://{login}"
            try:
                parsed = urlparse(login)
            except Exception:
                return ""
            path = (parsed.path or "").strip("/")
            if path:
                login = path.split("/")[0]
            else:
                return ""
        login = login.strip().lstrip("@")
        from re import sub

        login = sub(r"[^a-z0-9_]", "", login.lower())
        return login

