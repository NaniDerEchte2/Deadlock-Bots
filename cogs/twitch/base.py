"""Base implementation shared across the Twitch cog mixins."""

from __future__ import annotations

import asyncio
import os
import re
import socket
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
    TWITCH_RAID_REDIRECT_URI,
    TWITCH_REQUIRED_DISCORD_MARKER,
    TWITCH_TARGET_GAME_NAME,
)
from .logger import log
from .twitch_api import TwitchAPI
from .raid_manager import RaidBot
from .twitch_chat_bot import TWITCHIO_AVAILABLE, create_twitch_chat_bot, load_bot_tokens
from .token_manager import TwitchBotTokenManager


class TwitchBaseCog(commands.Cog):
    """Handle shared initialisation, shutdown and utility helpers."""

    def __init__(self, bot: commands.Bot):
        super().__init__()
        self.bot = bot

        # Diagnose: Welche Keys sind da?
        twitch_keys = [k for k in os.environ.keys() if k.startswith("TWITCH_")]
        log.debug("Detected Twitch Keys in ENV: %s", ", ".join(twitch_keys))

        # 🔒 Secrets nur aus ENV (nicht hardcoden!)
        # TWITCH_CLIENT_ID/SECRET sind für die Haupt-App (Raids, Dashboard)
        self.client_id = os.getenv("TWITCH_CLIENT_ID") or ""
        self.client_secret = os.getenv("TWITCH_CLIENT_SECRET")
        
        # TWITCH_BOT_CLIENT_ID ist speziell für den Chat-Bot (Fallback auf Haupt-App)
        self._twitch_bot_client_id: str = os.getenv("TWITCH_BOT_CLIENT_ID", "").strip() or self.client_id
        
        # Bot-Secret laden: 1. Spezieller Key, 2. Fallback auf Haupt-Secret (wenn ID identisch)
        bot_secret_env = os.getenv("TWITCH_BOT_CLIENT_SECRET", "").strip()
        if bot_secret_env:
            self._twitch_bot_secret = bot_secret_env
        elif self._twitch_bot_client_id == self.client_id:
            self._twitch_bot_secret = self.client_secret
        else:
            self._twitch_bot_secret = ""

        # Runtime attributes initialised even if the cog is disabled
        self.api: Optional[TwitchAPI]
        self._web: Optional[web.AppRunner] = None
        self._web_app: Optional[web.Application] = None
        self._category_id: Optional[str] = None
        self._language_filters = self._parse_language_filters(TWITCH_LANGUAGE)
        self._tick_count = 0
        self._log_every_n = max(1, int(TWITCH_LOG_EVERY_N_TICKS or 5))
        self._category_sample_limit = max(50, int(TWITCH_CATEGORY_SAMPLE_LIMIT or 400))
        self._active_sessions: Dict[str, int] = {}
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

        if not self.client_id:
            log.error("TWITCH_CLIENT_ID not configured; Twitch features will be limited or disabled.")
            self.api = None
            # Wir machen hier nicht 'return', damit der Chat-Bot (der seine eigene ID hat) evtl. trotzdem starten kann.
        else:
            if not self.client_secret:
                log.warning("TWITCH_CLIENT_SECRET missing. API calls and Raids will fail, but Chat Bot might work.")
                self.api = None
            else:
                self.api = TwitchAPI(self.client_id, self.client_secret)

        if self.api:
            # Rehydrate offene Streams/Sessions nach einem Neustart
            try:
                self._rehydrate_active_sessions()
            except Exception:
                log.debug("Konnte aktive Twitch-Sessions nicht rehydrieren", exc_info=True)

        # Raid-Bot initialisieren
        self._raid_bot: Optional[RaidBot] = None
        self._twitch_chat_bot = None
        bot_token, bot_refresh_token, _ = load_bot_tokens(log_missing=False)
        self._twitch_bot_token: Optional[str] = bot_token
        self._twitch_bot_refresh_token: Optional[str] = bot_refresh_token
        env_bot_client_id = os.getenv("TWITCH_BOT_CLIENT_ID", "").strip()
        self._twitch_bot_client_id = env_bot_client_id or self._twitch_bot_client_id or self.client_id
        if not self._twitch_bot_secret:
            env_bot_secret = os.getenv("TWITCH_BOT_CLIENT_SECRET", "").strip()
            if env_bot_secret:
                self._twitch_bot_secret = env_bot_secret
            elif self._twitch_bot_client_id == self.client_id:
                self._twitch_bot_secret = self.client_secret
            else:
                self._twitch_bot_secret = None
        self._bot_token_manager: Optional[TwitchBotTokenManager] = None
        if self._twitch_bot_client_id:
            self._bot_token_manager = TwitchBotTokenManager(
                self._twitch_bot_client_id,
                (self._twitch_bot_secret or self.client_secret or ""),
            )
        
        # Redirect-URL: Priorität 1: ENV/Tresor, Priorität 2: Constant
        redirect_uri = os.getenv("TWITCH_RAID_REDIRECT_URI", "").strip() or TWITCH_RAID_REDIRECT_URI
        self._raid_redirect_uri = redirect_uri

        if self.api:
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
        else:
            log.warning("Raid-Bot und Chat-Bot deaktiviert, da TWITCH_CLIENT_ID/SECRET fehlen.")

        # Background tasks
        self.poll_streams.start()
        self.invites_refresh.start()
        self._spawn_bg_task(self._ensure_category_id(), "twitch.ensure_category_id")
        if self._dashboard_embedded:
            self._spawn_bg_task(self._start_dashboard(), "twitch.start_dashboard")
        else:
            log.info("Skipping internal Twitch dashboard server startup")
        self._spawn_bg_task(self._refresh_all_invites(), "twitch.refresh_all_invites")
        # NUR EINEN EventSub Listener starten (konsolidiert stream.online + stream.offline)
        self._spawn_bg_task(self._start_eventsub_listener(), "twitch.eventsub")

    # -------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------
    async def cog_unload(self):
        """Ensure background resources are torn down when the cog is removed."""
        loops = (self.poll_streams, self.invites_refresh)

        for lp in loops:
            try:
                if lp.is_running():
                    lp.cancel()
            except Exception:
                log.exception("Konnte Loop nicht canceln: %r", lp)
        
        # EventSub Listener stoppen
        es_ws_listener = getattr(self, "_eventsub_ws_listener", None)
        if es_ws_listener and hasattr(es_ws_listener, "stop"):
            es_ws_listener.stop()

        # RaidBot Cleanup
        if self._raid_bot:
            try:
                await self._raid_bot.cleanup()
            except Exception:
                log.exception("RaidBot cleanup fehlgeschlagen")

        await asyncio.sleep(0.1)  # Give event loop a moment

        # Twitch Chat Bot stoppen
        if self._twitch_chat_bot:
            try:
                if hasattr(self._twitch_chat_bot, "close"):
                    await self._twitch_chat_bot.close()
            except Exception:
                log.exception("Twitch Chat Bot shutdown fehlgeschlagen")
        if self._bot_token_manager:
            try:
                await self._bot_token_manager.cleanup()
            except Exception:
                log.exception("Twitch Bot Token Manager shutdown fehlgeschlagen")

        if self._web:
            try:
                await self._stop_dashboard()
                log.info("Twitch dashboard stopped during unload.")
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

            token = self._twitch_bot_token
            refresh_token = self._twitch_bot_refresh_token

            if not token:
                token, refresh_from_store, _ = load_bot_tokens(log_missing=False)
                refresh_token = refresh_token or refresh_from_store

            refresh_env = os.getenv("TWITCH_BOT_REFRESH_TOKEN", "").strip() or None
            if refresh_env:
                refresh_token = refresh_env
            
            if not token:
                log.info(
                    "Twitch Chat Bot nicht verfuegbar (kein Token gesetzt). "
                    "Setze TWITCH_BOT_TOKEN oder TWITCH_BOT_TOKEN_FILE, um den Chat-Bot zu aktivieren."
                )
                return
            self._twitch_bot_token = token
            self._twitch_bot_refresh_token = refresh_token
            if self._bot_token_manager is None and self._twitch_bot_client_id:
                self._bot_token_manager = TwitchBotTokenManager(
                    self._twitch_bot_client_id,
                    (self._twitch_bot_secret or self.client_secret or ""),
                )

            self._twitch_chat_bot = await create_twitch_chat_bot(
                client_id=self._twitch_bot_client_id,
                client_secret=self._twitch_bot_secret or "",  # TwitchIO mag None manchmal nicht, Empty String ist sicherer
                redirect_uri=self._raid_redirect_uri,
                raid_bot=self._raid_bot,
                bot_token=token,
                bot_refresh_token=refresh_token,
                log_missing=False,
                token_manager=self._bot_token_manager,
            )

            if self._twitch_chat_bot:
                if self._bot_token_manager:
                    self._twitch_bot_token = self._bot_token_manager.access_token or self._twitch_bot_token
                    self._twitch_bot_refresh_token = self._bot_token_manager.refresh_token or self._twitch_bot_refresh_token
                # Bot im Hintergrund laufen lassen
                start_with_adapter = self._should_start_chat_adapter()
                asyncio.create_task(
                    self._twitch_chat_bot.start(
                        with_adapter=start_with_adapter,
                        load_tokens=False,  # vermeidet kaputte .tio.tokens.json ohne scope
                        save_tokens=False,
                    ),
                    name="twitch.chat_bot.start",
                )
                log.info(
                    "Twitch Chat Bot gestartet (Web Adapter: %s)",
                    "on" if start_with_adapter else "off",
                )

                # Verknüpfe Chat-Bot mit Raid-Bot für Recruitment-Messages
                if self._raid_bot:
                    self._raid_bot.set_chat_bot(self._twitch_chat_bot)
                    log.info("Chat-Bot mit Raid-Bot verknüpft für Recruitment-Messages")

                # Periodisch neue Partner-Channels joinen
                asyncio.create_task(self._periodic_channel_join(), name="twitch.chat_bot.join_channels")

        except Exception:
            log.exception("Fehler beim Initialisieren des Twitch Chat Bots")

    async def _periodic_channel_join(self):
        """Joint periodisch neue Partner-Channels und räumt Offline-Channels auf."""
        if not self._twitch_chat_bot:
            return

        await self.bot.wait_until_ready()
        await asyncio.sleep(60)  # Initial delay

        while True:
            try:
                if hasattr(self._twitch_chat_bot, "join_partner_channels"):
                    await self._twitch_chat_bot.join_partner_channels()
                await self._cleanup_offline_channels()
            except Exception:
                log.exception("Fehler in periodic channel maintenance")

            await asyncio.sleep(1800)  # Alle 30 Minuten prüfen

    async def _cleanup_offline_channels(self):
        """Verlässt Channels von Partnern, die offline sind."""
        chat_bot = getattr(self, "_twitch_chat_bot", None)
        if not chat_bot:
            return

        monitored = {login.lower() for login in getattr(chat_bot, "_monitored_streamers", set())}
        if not monitored:
            return

        placeholders = ",".join("?" for _ in monitored)
        offline_logins: list[str] = []
        offline_ids: dict[str, str] = {}

        try:
            with storage.get_conn() as conn:
                rows = conn.execute(
                    f"""
                    SELECT s.twitch_login, l.is_live, s.twitch_user_id
                      FROM twitch_streamers s
                      LEFT JOIN twitch_live_state l ON s.twitch_user_id = l.twitch_user_id
                     WHERE LOWER(s.twitch_login) IN ({placeholders})
                    """,
                    tuple(monitored),
                ).fetchall()

            for row in rows:
                login = str(row["twitch_login"] if hasattr(row, "keys") else row[0]).strip().lower()
                is_live = row["is_live"] if hasattr(row, "keys") else row[1]
                user_id = str(row["twitch_user_id"] if hasattr(row, "keys") else row[2]).strip()
                if not login:
                    continue
                if bool(is_live):
                    continue
                offline_logins.append(login)
                if user_id:
                    offline_ids[login] = user_id
        except Exception:
            log.debug("Cleanup: konnte Live-Status nicht laden", exc_info=True)
            return

        if not offline_logins:
            return

        offline_id_set = set(offline_ids.values())
        unsubscribed = 0

        try:
            subs = await chat_bot.fetch_eventsub_subscriptions()
            for sub in subs or []:
                try:
                    sub_type = getattr(sub, "type", "") or getattr(sub, "subscription_type", "")
                    if sub_type != "channel.chat.message":
                        continue
                    condition = getattr(sub, "condition", None)
                    broadcaster_id = ""
                    if isinstance(condition, dict):
                        broadcaster_id = str(
                            condition.get("broadcaster_user_id")
                            or condition.get("broadcaster_id")
                            or ""
                        ).strip()
                    else:
                        broadcaster_id = str(
                            getattr(condition, "broadcaster_user_id", "")
                            or getattr(condition, "broadcaster_id", "")
                            or ""
                        ).strip()

                    if not broadcaster_id or broadcaster_id not in offline_id_set:
                        continue

                    sub_id = (
                        getattr(sub, "id", None)
                        or getattr(sub, "subscription_id", None)
                        or getattr(sub, "uuid", None)
                    )
                    if sub_id:
                        try:
                            await chat_bot.delete_eventsub_subscription(sub_id)
                            unsubscribed += 1
                        except Exception:
                            log.debug("Cleanup: konnte EventSub-Subscription %s nicht löschen", sub_id, exc_info=True)
                except Exception:
                    log.debug("Cleanup: Fehler beim Prüfen von EventSub-Subscriptions", exc_info=True)
        except Exception:
            log.debug("Cleanup: konnte EventSub-Subscriptions nicht abrufen", exc_info=True)

        for login in offline_logins:
            chat_bot._monitored_streamers.discard(login)

        log.info(
            "Cleanup: %d offline Channels entfernt (unsubscribed: %d)",
            len(offline_logins),
            unsubscribed,
        )

    def _should_start_chat_adapter(self) -> bool:
        """Decide whether to start the TwitchIO web adapter (avoids port collisions)."""
        override = (os.getenv("TWITCH_CHAT_ADAPTER") or "").strip().lower()
        if override in {"0", "false", "off", "no"}:
            log.info("Twitch Chat Web Adapter deaktiviert per TWITCH_CHAT_ADAPTER.")
            return False

        bot = self._twitch_chat_bot
        adapter = getattr(bot, "adapter", None)
        if adapter is None:
            return False

        host = getattr(adapter, "_host", "localhost")
        port_raw = getattr(adapter, "_port", 4343)
        try:
            port = int(port_raw)
        except Exception:
            port = 4343

        can_bind, error = self._can_bind_port(host, port)
        if not can_bind:
            log.warning(
                "Twitch Chat Web Adapter Port %s auf %s bereits belegt (%s) - starte ohne Adapter (Webhooks/OAuth ausgeschaltet).",
                port,
                host,
                error or "address already in use",
            )
        return can_bind

    @staticmethod
    def _can_bind_port(host: str, port: int) -> tuple[bool, Optional[str]]:
        """Try binding to the given host/port; return False if something is already listening."""
        last_error: Optional[str] = None
        try:
            families = [info[0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]
        except Exception as exc:  # socket.gaierror or OSError
            families = [socket.AF_INET]
            last_error = str(exc)

        seen = set()
        for family in families or [socket.AF_INET]:
            if family in seen:
                continue
            seen.add(family)
            try:
                with socket.socket(family, socket.SOCK_STREAM) as sock:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    sock.bind((host, port))
                return True, None
            except OSError as exc:
                last_error = str(exc)
                continue

        return False, last_error

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
