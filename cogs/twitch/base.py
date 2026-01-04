"""Base implementation shared across the Twitch cog mixins."""

from __future__ import annotations

import asyncio
import os
import re
from typing import Any, Coroutine, Dict, List, Optional, Set

from urllib.parse import urlparse

from aiohttp import web
from discord import Forbidden, Guild, HTTPException
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
from .raid_manager import RaidBot
from .twitch_chat_bot import TWITCHIO_AVAILABLE, create_twitch_chat_bot, load_bot_token


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
        self._language_filters = self._parse_language_filters(TWITCH_LANGUAGE)
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
        embedded_env = (os.getenv("TWITCH_DASHBOARD_EMBEDDED", "") or "").strip().lower()
        self._dashboard_embedded = embedded_env not in {"0", "false", "no", "off"}
        if not self._dashboard_embedded:
            log.info(
                "TWITCH_DASHBOARD_EMBEDDED disabled - assuming external reverse proxy serves the dashboard"
            )
        self._partner_dashboard_token = os.getenv("TWITCH_PARTNER_TOKEN") or None
        self._required_marker_default = TWITCH_REQUIRED_DISCORD_MARKER or None

        if not self.client_id or not self.client_secret:
            log.error("TWITCH_CLIENT_ID/SECRET not configured; cog disabled")
            self.api = None
            return

        self.api = TwitchAPI(self.client_id, self.client_secret)

        # Raid-Bot initialisieren
        self._raid_bot: Optional[RaidBot] = None
        self._twitch_chat_bot = None
        self._twitch_bot_token: Optional[str] = load_bot_token(log_missing=False)
        redirect_uri = os.getenv("TWITCH_RAID_REDIRECT_URI", "").strip()
        if not redirect_uri:
            # Fallback: Dashboard-URL verwenden
            redirect_uri = f"http://{self._dashboard_host}:{self._dashboard_port}/twitch/raid/callback"
        self._raid_redirect_uri = redirect_uri

        try:
            session = self.api.get_http_session()
            self._raid_bot = RaidBot(
                client_id=self.client_id,
                client_secret=self.client_secret,
                redirect_uri=redirect_uri,
                session=session,
            )
            log.info("Raid-Bot initialisiert (redirect_uri: %s)", redirect_uri)

            # Twitch Chat Bot starten (falls Token vorhanden)
            if self._twitch_bot_token:
                self._spawn_bg_task(self._init_twitch_chat_bot(), "twitch.chat_bot")
            else:
                log.info(
                    "Twitch Chat Bot nicht verfuegbar (kein Token gesetzt). "
                    "Setze TWITCH_BOT_TOKEN oder TWITCH_BOT_TOKEN_FILE, um den Chat-Bot zu aktivieren."
                )
        except Exception:
            log.exception("Fehler beim Initialisieren des Raid-Bots")
            self._raid_bot = None

        # Background tasks
        self.poll_streams.start()
        self.invites_refresh.start()
        self._spawn_bg_task(self._ensure_category_id(), "twitch.ensure_category_id")
        if self._dashboard_embedded:
            self._spawn_bg_task(self._start_dashboard(), "twitch.start_dashboard")
        else:
            log.info("Skipping internal Twitch dashboard server startup")
        self._spawn_bg_task(self._refresh_all_invites(), "twitch.refresh_all_invites")

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

            # Twitch Chat Bot stoppen
            if self._twitch_chat_bot:
                try:
                    if hasattr(self._twitch_chat_bot, "close"):
                        await self._twitch_chat_bot.close()
                except Exception:
                    log.exception("Twitch Chat Bot shutdown fehlgeschlagen")

            if self._web:
                try:
                    await self._stop_dashboard()
                except Exception:
                    log.exception("Dashboard shutdown fehlgeschlagen")

            if self.api is not None:
                try:
                    await self.api.aclose()
                except asyncio.CancelledError as exc:
                    log.debug("Schließen der TwitchAPI-Session abgebrochen: %s", exc)
                    raise
                except Exception:
                    log.exception("TwitchAPI-Session konnte nicht geschlossen werden")

        self._spawn_bg_task(_graceful_shutdown(), "twitch.shutdown")

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

    def _spawn_bg_task(self, coro: Coroutine[Any, Any, Any], name: str) -> None:
        """Start a background coroutine without relying on Bot.loop (removed in d.py 2.4)."""
        try:
            asyncio.create_task(coro, name=name)
        except RuntimeError as exc:
            log.error("Cannot start background task %s (no running loop yet): %s", name, exc)
        except Exception:
            log.exception("Failed to start background task %s", name)

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

    async def _refresh_guild_invites(self, guild: Guild):
        codes: Set[str] = set()
        try:
            invites = await guild.invites()
            for inv in invites:
                if inv.code:
                    codes.add(inv.code)
        except Forbidden:
            log.warning("Fehlende Berechtigung, um Invites von Guild %s zu lesen", guild.id)
        except HTTPException:
            log.exception("HTTP-Fehler beim Abruf der Invites für Guild %s", guild.id)

        self._invite_codes[guild.id] = codes

    async def _init_twitch_chat_bot(self):
        """Initialisiert den Twitch Chat Bot für Raid-Commands."""
        try:
            await self.bot.wait_until_ready()
            if not self._raid_bot:
                log.info("Raid-Bot nicht verfügbar, überspringe Twitch Chat Bot")
                return
            if not TWITCHIO_AVAILABLE:
                log.info("twitchio nicht installiert; Twitch Chat Bot wird übersprungen.")
                return

            token = self._twitch_bot_token or load_bot_token(log_missing=False)
            if not token:
                log.info(
                    "Twitch Chat Bot nicht verfuegbar (kein Token gesetzt). "
                    "Setze TWITCH_BOT_TOKEN oder TWITCH_BOT_TOKEN_FILE, um den Chat-Bot zu aktivieren."
                )
                return
            self._twitch_bot_token = token

            self._twitch_chat_bot = await create_twitch_chat_bot(
                client_id=self.client_id,
                client_secret=self.client_secret,
                redirect_uri=self._raid_redirect_uri,
                raid_bot=self._raid_bot,
                bot_token=token,
                log_missing=False,
            )

            if self._twitch_chat_bot:
                # Bot im Hintergrund laufen lassen
                asyncio.create_task(self._twitch_chat_bot.start(), name="twitch.chat_bot.start")
                log.info("Twitch Chat Bot gestartet")

                # Verknüpfe Chat-Bot mit Raid-Bot für Recruitment-Messages
                if self._raid_bot:
                    self._raid_bot.set_chat_bot(self._twitch_chat_bot)
                    log.info("Chat-Bot mit Raid-Bot verknüpft für Recruitment-Messages")

                # Periodisch neue Partner-Channels joinen
                asyncio.create_task(self._periodic_channel_join(), name="twitch.chat_bot.join_channels")

        except Exception:
            log.exception("Fehler beim Initialisieren des Twitch Chat Bots")

    async def _periodic_channel_join(self):
        """Joint periodisch neue Partner-Channels."""
        if not self._twitch_chat_bot:
            return

        await self.bot.wait_until_ready()
        await asyncio.sleep(60)  # Initial delay

        while True:
            try:
                if hasattr(self._twitch_chat_bot, "join_partner_channels"):
                    await self._twitch_chat_bot.join_partner_channels()
            except Exception:
                log.exception("Fehler beim Joinen von Partner-Channels")

            await asyncio.sleep(3600)  # Alle Stunde prüfen

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
        login = re.sub(r"[^a-z0-9_]", "", login.lower())
        return login

    @staticmethod
    def _parse_language_filters(raw: Optional[str]) -> Optional[List[str]]:
        """Allow TWITCH_LANGUAGE to define multiple comma/whitespace separated codes."""
        value = (raw or "").strip()
        if not value:
            return None
        tokens = [tok.strip().lower() for tok in re.split(r"[,\s;|]+", value) if tok.strip()]
        if not tokens:
            return None
        if any(tok in {"*", "any", "all"} for tok in tokens):
            return None
        seen: List[str] = []
        for tok in tokens:
            if tok not in seen:
                seen.append(tok)
        return seen or None
