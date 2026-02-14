"""Base implementation shared across the Twitch cog mixins."""

from __future__ import annotations

import asyncio
import inspect
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
from .constants import log
from .api.twitch_api import TwitchAPI
from .raid.manager import RaidBot
from .chat.bot import TWITCHIO_AVAILABLE, create_twitch_chat_bot, load_bot_tokens
from .api.token_manager import TwitchBotTokenManager


def _parse_env_bool(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def _parse_env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


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
        self._dashboard_token = (os.getenv("TWITCH_DASHBOARD_TOKEN") or "").strip() or None
        self._dashboard_noauth = _parse_env_bool(
            "TWITCH_DASHBOARD_NOAUTH",
            bool(TWITCH_DASHBOARD_NOAUTH),
        )
        env_dashboard_host = (os.getenv("TWITCH_DASHBOARD_HOST") or "").strip()
        self._dashboard_host = env_dashboard_host or TWITCH_DASHBOARD_HOST or (
            "127.0.0.1" if self._dashboard_noauth else "0.0.0.0"  # nosec B104
        )
        self._dashboard_port = _parse_env_int("TWITCH_DASHBOARD_PORT", int(TWITCH_DASHBOARD_PORT))
        embedded_env = (os.getenv("TWITCH_DASHBOARD_EMBEDDED", "") or "").strip().lower()
        self._dashboard_embedded = embedded_env not in {"0", "false", "no", "off"}
        if not self._dashboard_embedded:
            log.info(
                "TWITCH_DASHBOARD_EMBEDDED disabled - assuming external reverse proxy serves the dashboard"
            )
        self._partner_dashboard_token = (os.getenv("TWITCH_PARTNER_TOKEN") or "").strip() or None
        self._dashboard_auth_redirect_uri = (
            (os.getenv("TWITCH_DASHBOARD_AUTH_REDIRECT_URI") or "").strip()
            or "https://twitch.earlysalty.com/twitch/auth/callback"
        )
        self._dashboard_session_ttl = max(
            1800,
            _parse_env_int("TWITCH_DASHBOARD_SESSION_TTL_SEC", 12 * 3600),
        )
        self._legacy_stats_url = (os.getenv("TWITCH_LEGACY_STATS_URL") or "").strip() or None
        self._required_marker_default = TWITCH_REQUIRED_DISCORD_MARKER or None

        # EventSub Webhook Handler – früh initialisieren damit er sowohl im Dashboard
        # als auch in _start_eventsub_listener verfügbar ist.
        _webhook_secret = (os.getenv("TWITCH_WEBHOOK_SECRET") or "").strip()
        if _webhook_secret:
            try:
                from .monitoring.eventsub_webhook import EventSubWebhookHandler
                self._eventsub_webhook_handler = EventSubWebhookHandler(
                    secret=_webhook_secret,
                    logger=log,
                )
                # Webhook-Basis-URL aus dem Auth-Redirect-URI ableiten
                _parsed_redirect = urlparse(self._dashboard_auth_redirect_uri)
                self._webhook_base_url: Optional[str] = (
                    f"{_parsed_redirect.scheme}://{_parsed_redirect.netloc}"
                    if _parsed_redirect.netloc else None
                )
                self._webhook_secret: Optional[str] = _webhook_secret
                log.info(
                    "EventSub Webhook Handler initialisiert (base_url=%s)",
                    self._webhook_base_url,
                )
            except Exception:
                log.exception("EventSub Webhook Handler konnte nicht initialisiert werden")
                self._eventsub_webhook_handler = None
                self._webhook_base_url = None
                self._webhook_secret = None
        else:
            log.info(
                "TWITCH_WEBHOOK_SECRET nicht gesetzt – EventSub Webhook deaktiviert, "
                "WebSocket-Fallback wird verwendet."
            )
            self._eventsub_webhook_handler = None
            self._webhook_base_url = None
            self._webhook_secret = None

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
                self._raid_bot.set_discord_bot(self.bot)
                self._raid_bot.set_cog(self)  # For dynamic EventSub subscriptions
                log.info("Raid-Bot initialisiert (redirect_uri: %s)", redirect_uri)

                # Persistente Views registrieren (Button-Callbacks überleben Bot-Neustart)
                self._register_persistent_raid_auth_views()

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
        # invites_refresh.start() → DEAKTIVIERT: On-Demand statt periodisch
        self._spawn_bg_task(self._ensure_category_id(), "twitch.ensure_category_id")
        self._spawn_bg_task(self._load_invite_codes_from_db(), "twitch.load_invites")
        if self._dashboard_embedded:
            self._spawn_bg_task(self._start_dashboard(), "twitch.start_dashboard")
        else:
            log.info("Skipping internal Twitch dashboard server startup")
        self._spawn_bg_task(self._refresh_all_invites(), "twitch.refresh_all_invites")
        # NUR EINEN EventSub Listener starten (konsolidiert stream.online + stream.offline)
        self._spawn_bg_task(self._start_eventsub_listener(), "twitch.eventsub")
        # Beim Start fehlende user_ids in twitch_streamers nachfüllen
        if self.api:
            self._spawn_bg_task(self._sync_missing_user_ids(), "twitch.sync_user_ids")

    def _register_persistent_raid_auth_views(self) -> None:
        """Registriert persistente RaidAuthGenerateViews für alle Streamer in der DB.
        Muss bei Bot-Start aufgerufen werden damit Buttons nach Neustart funktionieren."""
        from .raid.views import RaidAuthGenerateView
        try:
            with storage.get_conn() as conn:
                rows = conn.execute(
                    "SELECT twitch_login FROM twitch_raid_auth WHERE twitch_login IS NOT NULL"
                ).fetchall()
            count = 0
            for row in rows:
                login = str(row[0] if not hasattr(row, "keys") else row["twitch_login"]).strip().lower()
                if login:
                    self.bot.add_view(RaidAuthGenerateView(twitch_login=login))
                    count += 1
            log.info("Persistente RaidAuthViews registriert: %d Streamer", count)
        except Exception:
            log.exception("Fehler beim Registrieren persistenter RaidAuthViews")

    # -------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------
    async def cog_unload(self):
        """Ensure background resources are torn down when the cog is removed.
        
        Shutdown-Reihenfolge (wichtig!):
        1. Tasks/Loops canceln
        2. EventSub WebSocket Listeners stoppen
        3. Chat Bot sauber beenden (inklusive Port-Freigabe)
        4. Token Manager cleanup
        5. Dashboard stoppen (inklusive Port-Freigabe)
        6. Raid Bot cleanup
        7. API Session schließen
        8. Commands deregistrieren
        """
        log.info("Twitch Cog Unload gestartet – fahre alle Ressourcen herunter...")
        
        # 1. Background Loops canceln
        loops = (self.poll_streams, self.invites_refresh)
        for lp in loops:
            try:
                if lp.is_running():
                    lp.cancel()
                    log.debug("Loop gecancelt: %r", lp)
            except Exception:
                log.exception("Konnte Loop nicht canceln: %r", lp)
        
        # 2. EventSub: Webhook Handler hat keinen persistenten Zustand der explizit
        #    gestoppt werden muss. Etwaige Background-Tasks (dispatch) werden beim
        #    asyncio-Shutdown automatisch gecancelt.
        log.debug("EventSub Webhook: kein expliziter Teardown nötig")

        # 3. Twitch Chat Bot sauber beenden (Port 4343 freigeben)
        if self._twitch_chat_bot:
            log.info("Beende Twitch Chat Bot...")
            try:
                # TwitchIO Bot hat keine explizite shutdown-Methode in 3.x,
                # aber close() schließt die WebSocket-Session und den Adapter.
                if hasattr(self._twitch_chat_bot, "close"):
                    await self._twitch_chat_bot.close()
                    log.debug("Chat Bot close() abgeschlossen")
                
                # Warte explizit auf Port-Freigabe (4343)
                adapter = getattr(self._twitch_chat_bot, "adapter", None)
                if adapter:
                    adapter_host = getattr(adapter, "_host", "127.0.0.1")
                    adapter_port = int(getattr(adapter, "_port", 4343))
                    
                    # Gebe dem Adapter Zeit zum Herunterfahren
                    await asyncio.sleep(2.0)
                    
                    # Prüfe ob der Port frei ist
                    for retry in range(10):  # Max 10 Sekunden warten
                        can_bind, _ = await self._can_bind_port_async(adapter_host, adapter_port)
                        if can_bind:
                            log.info("Chat Bot Adapter Port %s:%s erfolgreich freigegeben", adapter_host, adapter_port)
                            break
                        if retry < 9:
                            log.debug("Warte auf Port-Freigabe %s:%s... (%d/10)", adapter_host, adapter_port, retry + 1)
                            await asyncio.sleep(1.0)
                        else:
                            log.warning("Port %s:%s nach 10s noch belegt – fahre trotzdem fort", adapter_host, adapter_port)
                
                log.info("Twitch Chat Bot beendet")
            except Exception:
                log.exception("Twitch Chat Bot shutdown fehlgeschlagen")
        
        # 4. Token Manager cleanup
        if self._bot_token_manager:
            try:
                await self._bot_token_manager.cleanup()
                log.debug("Bot Token Manager cleanup abgeschlossen")
            except Exception:
                log.exception("Twitch Bot Token Manager shutdown fehlgeschlagen")

        # 5. Dashboard stoppen (Port 8765 freigeben)
        if self._web:
            log.info("Stoppe Twitch Dashboard...")
            try:
                await self._stop_dashboard()
                
                # Warte explizit auf Port-Freigabe (8765)
                dashboard_port = self._dashboard_port
                dashboard_host = self._dashboard_host
                
                # Gebe dem Dashboard Zeit zum Herunterfahren
                await asyncio.sleep(2.0)
                
                # Prüfe ob der Port frei ist
                for retry in range(10):  # Max 10 Sekunden warten
                    can_bind, _ = await self._can_bind_port_async(dashboard_host, dashboard_port)
                    if can_bind:
                        log.info("Dashboard Port %s:%s erfolgreich freigegeben", dashboard_host, dashboard_port)
                        break
                    if retry < 9:
                        log.debug("Warte auf Port-Freigabe %s:%s... (%d/10)", dashboard_host, dashboard_port, retry + 1)
                        await asyncio.sleep(1.0)
                    else:
                        log.warning("Port %s:%s nach 10s noch belegt – fahre trotzdem fort", dashboard_host, dashboard_port)
                
                log.info("Twitch Dashboard gestoppt")
            except Exception:
                log.exception("Dashboard shutdown fehlgeschlagen")

        # 6. RaidBot Cleanup
        if self._raid_bot:
            try:
                await self._raid_bot.cleanup()
                log.debug("RaidBot cleanup abgeschlossen")
            except Exception:
                log.exception("RaidBot cleanup fehlgeschlagen")

        # 7. API Session schließen (mit Grace Period für laufende Requests)
        if self.api is not None:
            log.info("Schließe Twitch API Session...")
            try:
                # Warte kurz damit laufende Requests abgeschlossen werden können
                await asyncio.sleep(1.0)
                await self.api.aclose()
                log.info("Twitch API Session geschlossen")
            except asyncio.CancelledError as exc:
                log.debug("Schließen der TwitchAPI-Session abgebrochen: %s", exc)
                raise
            except Exception:
                log.exception("TwitchAPI-Session konnte nicht geschlossen werden")

        # 8. Commands deregistrieren
        try:
            if self._twl_command is not None:
                existing = self.bot.get_command(self._twl_command.name)
                if existing is self._twl_command:
                    self.bot.remove_command(self._twl_command.name)
                    log.debug("!twl Command deregistriert")
        except Exception:
            log.exception("Konnte !twl-Command nicht deregistrieren")
        finally:
            self._twl_command = None
        
        # Finale Pause damit alle async Tasks sauber beendet werden können
        await asyncio.sleep(0.5)
        log.info("Twitch Cog Unload abgeschlossen")

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

        guilds = list(self.bot.guilds)
        if not guilds:
            return

        # Delay zwischen Guilds einbauen um Rate Limits zu vermeiden
        delay_between_guilds = max(2.0, 30.0 / len(guilds))  # Minimum 2s, verteilt über 30s
        
        for i, guild in enumerate(guilds):
            try:
                await self._refresh_guild_invites(guild)
                # Warte zwischen Guilds, außer beim letzten
                if i < len(guilds) - 1:
                    await asyncio.sleep(delay_between_guilds)
            except Exception:
                log.exception("Einladungen für Guild %s fehlgeschlagen", guild.id)

    async def _load_invite_codes_from_db(self):
        """Load cached invite codes from database on startup."""
        try:
            await self.bot.wait_until_ready()
        except Exception:
            log.exception("wait_until_ready fehlgeschlagen")
            return
        
        try:
            with storage.get_conn() as conn:
                rows = conn.execute(
                    "SELECT guild_id, invite_code FROM discord_invite_codes"
                ).fetchall()
            
            if not rows:
                log.info("Keine Invite-Codes in DB gefunden - werden beim ersten Gebrauch abgerufen")
                return
            
            # Gruppiere nach Guild
            by_guild: Dict[int, Set[str]] = {}
            for guild_id, code in rows:
                if guild_id not in by_guild:
                    by_guild[guild_id] = set()
                by_guild[guild_id].add(code)
            
            # Lade in RAM-Cache
            for guild_id, codes in by_guild.items():
                self._invite_codes[guild_id] = codes
            
            total_codes = sum(len(codes) for codes in by_guild.values())
            log.info(
                "Invite-Codes aus DB geladen: %s Guilds, %s Codes gesamt",
                len(by_guild), total_codes
            )
        except Exception:
            log.exception("Konnte Invite-Codes nicht aus DB laden")

    async def _sync_missing_user_ids(self):
        """Beim Start fehlende twitch_user_id in twitch_streamers nachfüllen.

        Strategie:
          1. Aus twitch_raid_auth übernehmen (kein API-Call noetig).
          2. Verbleibende per Twitch-API (Helix /users) auflösen.
        Wird nur beim Hochfahren ausgeführt – neue Einträge bekommen
        ihre user_id bereits beim Anlegen in _cmd_add / _dashboard_save_discord_profile.
        """
        try:
            await self.bot.wait_until_ready()
        except Exception:
            log.exception("wait_until_ready in _sync_missing_user_ids fehlgeschlagen")
            return

        # --- Phase 1: Sync aus raid_auth (offline, instant) ---
        try:
            with storage.get_conn() as conn:
                conn.execute("""
                    UPDATE twitch_streamers
                    SET twitch_user_id = (
                        SELECT tra.twitch_user_id
                        FROM twitch_raid_auth tra
                        WHERE LOWER(tra.twitch_login) = LOWER(twitch_streamers.twitch_login)
                    )
                    WHERE twitch_user_id IS NULL
                      AND EXISTS (
                          SELECT 1 FROM twitch_raid_auth tra
                          WHERE LOWER(tra.twitch_login) = LOWER(twitch_streamers.twitch_login)
                            AND tra.twitch_user_id IS NOT NULL
                      )
                """)
                synced = conn.execute(
                    "SELECT changes()"
                ).fetchone()[0]
            if synced:
                log.info("_sync_missing_user_ids: %d user_ids aus raid_auth übernommen", synced)
        except Exception:
            log.exception("_sync_missing_user_ids: Phase 1 (raid_auth) fehlgeschlagen")

        # --- Phase 2: Rest per API auflösen ---
        try:
            with storage.get_conn() as conn:
                rows = conn.execute(
                    "SELECT twitch_login FROM twitch_streamers WHERE twitch_user_id IS NULL"
                ).fetchall()
            missing = [row[0] for row in rows]
        except Exception:
            log.exception("_sync_missing_user_ids: Konnte fehlende Logins nicht laden")
            return

        if not missing:
            log.debug("_sync_missing_user_ids: alle user_ids vorhanden, nichts zu tun")
            return

        log.info("_sync_missing_user_ids: %d Logins ohne user_id, frage Twitch API ab", len(missing))

        try:
            # get_users gibt ein Dict {login: {id, login, ...}} zurück
            users = await self.api.get_users(missing)
        except Exception:
            log.exception("_sync_missing_user_ids: API-Aufruf fehlgeschlagen")
            return

        if not users:
            log.warning(
                "_sync_missing_user_ids: API hat keine Ergebnisse für %s zurückgegeben",
                missing,
            )
            return

        try:
            with storage.get_conn() as conn:
                for login, user_data in users.items():
                    uid = user_data.get("id")
                    if uid:
                        conn.execute(
                            "UPDATE twitch_streamers SET twitch_user_id = ? "
                            "WHERE LOWER(twitch_login) = LOWER(?) AND twitch_user_id IS NULL",
                            (uid, login),
                        )
            log.info("_sync_missing_user_ids: %d user_ids per API aktualisiert", len(users))
        except Exception:
            log.exception("_sync_missing_user_ids: DB-Update nach API-Aufruf fehlgeschlagen")

        # --- Abschliessender Bericht ---
        try:
            with storage.get_conn() as conn:
                still_missing = conn.execute(
                    "SELECT twitch_login FROM twitch_streamers WHERE twitch_user_id IS NULL"
                ).fetchall()
            if still_missing:
                log.warning(
                    "_sync_missing_user_ids: %d Logins konnten nicht aufgelöst werden: %s",
                    len(still_missing),
                    [r[0] for r in still_missing],
                )
            else:
                log.info("_sync_missing_user_ids: alle user_ids erfolgreich gesetzt")
        except Exception:
            log.debug("_sync_missing_user_ids: Abschliessender Check fehlgeschlagen", exc_info=True)

    async def _refresh_guild_invites(self, guild: Guild):
        codes: Set[str] = set()
        max_retries = 3
        retry_delay = 5.0  # Initial 5 Sekunden
        
        for attempt in range(max_retries):
            try:
                invites = await guild.invites()
                for inv in invites:
                    if inv.code:
                        codes.add(inv.code)
                break  # Erfolg, Schleife verlassen
            except Forbidden:
                log.warning("Fehlende Berechtigung, um Invites von Guild %s zu lesen", guild.id)
                break  # Keine Retries bei Permission-Fehler
            except HTTPException as e:
                if attempt < max_retries - 1 and "429" in str(e):  # Rate Limit
                    wait_time = retry_delay * (2 ** attempt)  # Exponential backoff
                    log.warning(
                        "Rate Limit bei Invite-Refresh für Guild %s - warte %s Sekunden (Versuch %s/%s)",
                        guild.id, wait_time, attempt + 1, max_retries
                    )
                    await asyncio.sleep(wait_time)
                else:
                    # Letzter Versuch oder anderer Fehler - loggen und abbrechen
                    if "429" in str(e):
                        log.error(
                            "HTTP-Fehler beim Abruf der Invites für Guild %s nach %s Versuchen - überspringe",
                            guild.id, max_retries
                        )
                    else:
                        log.exception("HTTP-Fehler beim Abruf der Invites für Guild %s", guild.id)
                    break

        # Cache im RAM
        self._invite_codes[guild.id] = codes
        
        # Persistiere in DB für spätere Verwendung
        if codes:
            try:
                from datetime import datetime, timezone
                now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                with storage.get_conn() as conn:
                    # Lösche alte Codes die nicht mehr existieren
                    existing = {row[0] for row in conn.execute(
                        "SELECT invite_code FROM discord_invite_codes WHERE guild_id = ?",
                        (guild.id,)
                    ).fetchall()}
                    
                    to_remove = existing - codes
                    if to_remove:
                        for invite_code in to_remove:
                            conn.execute(
                                "DELETE FROM discord_invite_codes WHERE guild_id = ? AND invite_code = ?",
                                (guild.id, invite_code),
                            )
                    
                    # Füge neue hinzu oder update last_seen_at
                    for code in codes:
                        conn.execute(
                            """INSERT INTO discord_invite_codes (guild_id, invite_code, created_at, last_seen_at)
                               VALUES (?, ?, ?, ?)
                               ON CONFLICT(guild_id, invite_code) 
                               DO UPDATE SET last_seen_at = ?""",
                            (guild.id, code, now, now, now)
                        )
                    conn.commit()
                    log.debug("Invite-Codes für Guild %s in DB gespeichert: %s", guild.id, len(codes))
            except Exception:
                log.exception("Konnte Invite-Codes nicht in DB speichern für Guild %s", guild.id)

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
                try:
                    if hasattr(self._twitch_chat_bot, "set_discord_bot"):
                        invite_channel_id = self._notify_channel_id or None
                        self._twitch_chat_bot.set_discord_bot(
                            self.bot,
                            invite_channel_id=invite_channel_id,
                        )
                except Exception:
                    log.debug("Konnte Discord-Bot nicht an Chat-Bot binden", exc_info=True)
                # Bot im Hintergrund laufen lassen
                start_with_adapter = await self._should_start_chat_adapter()
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

        offline_logins: list[str] = []
        offline_ids: dict[str, str] = {}

        try:
            with storage.get_conn() as conn:
                rows = []
                for login in monitored:
                    row = conn.execute(
                        """
                        SELECT s.twitch_login, l.is_live, s.twitch_user_id
                          FROM twitch_streamers s
                          LEFT JOIN twitch_live_state l ON s.twitch_user_id = l.twitch_user_id
                         WHERE LOWER(s.twitch_login) = ?
                        """,
                        (login,),
                    ).fetchone()
                    if row is not None:
                        rows.append(row)

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
            subs_result = chat_bot.fetch_eventsub_subscriptions()
            # TwitchIO liefert je nach Version ein awaitable, das einen HTTPAsyncIterator zurückgibt.
            if inspect.isawaitable(subs_result):
                subs_result = await subs_result

            subs_list = []

            async def _consume_async_iter(source) -> bool:
                if source is None:
                    return False
                if hasattr(source, "__aiter__"):
                    async for sub in source:
                        subs_list.append(sub)
                    return True
                if hasattr(source, "__anext__"):
                    while True:
                        try:
                            sub = await source.__anext__()
                        except StopAsyncIteration:
                            break
                        subs_list.append(sub)
                    return True
                return False

            if subs_result is None:
                log.warning("Cleanup: fetch_eventsub_subscriptions returned None")
            else:
                handled = await _consume_async_iter(subs_result)
                if not handled:
                    # TwitchIO 3.x gibt EventsubSubscriptions zurück – Einträge in .subscriptions
                    inner = getattr(subs_result, "subscriptions", None)
                    if inner is not None:
                        handled = await _consume_async_iter(inner)
                        if not handled:
                            try:
                                subs_list.extend(list(inner))
                            except TypeError:
                                log.warning(
                                    "Cleanup: fetch_eventsub_subscriptions returned unexpected subscriptions type: %s",
                                    type(inner),
                                )
                    else:
                        try:
                            subs_list.extend(list(subs_result))
                        except TypeError:
                            log.warning(
                                "Cleanup: fetch_eventsub_subscriptions returned unexpected type: %s",
                                type(subs_result),
                            )

            for sub in subs_list:
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

    async def _should_start_chat_adapter(self) -> bool:
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

        can_bind, error = await self._can_bind_port_async(host, port)
        if not can_bind:
            log.warning(
                "Twitch Chat Web Adapter Port %s auf %s bereits belegt (%s) - starte ohne Adapter (Webhooks/OAuth ausgeschaltet).",
                port,
                host,
                error or "address already in use",
            )
        return can_bind

    @staticmethod
    async def _can_bind_port_async(host: str, port: int) -> tuple[bool, Optional[str]]:
        """Try binding to the given host/port with retries; return False if something is already listening."""
        max_retries = 5
        retry_delay = 0.5
        last_error: Optional[str] = None

        for attempt in range(max_retries):
            try:
                families = [info[0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]
            except Exception as exc:
                families = [socket.AF_INET]
                last_error = str(exc)

            success = False
            seen = set()
            for family in families or [socket.AF_INET]:
                if family in seen:
                    continue
                seen.add(family)
                try:
                    with socket.socket(family, socket.SOCK_STREAM) as sock:
                        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                        sock.bind((host, port))
                    success = True
                    break
                except OSError as exc:
                    last_error = str(exc)
                    continue
            
            if success:
                return True, None
            
            if attempt < max_retries - 1:
                log.debug("Port %s:%s belegt, versuche es erneut in %ss... (Versuch %s/%s)", 
                          host, port, retry_delay, attempt + 1, max_retries)
                await asyncio.sleep(retry_delay)
                retry_delay *= 2
            else:
                break

        return False, last_error

    @staticmethod
    def _can_bind_port(host: str, port: int) -> tuple[bool, Optional[str]]:
        """Synchronous version for compatibility (if needed), but prefers async version."""
        # For compatibility we keep the sync one but the async one should be used where possible
        try:
            families = [info[0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]
        except Exception as exc:
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

    async def _send_alert_message(self, message: str) -> None:
        """Send a warning to the configured alert channel (Discord)."""
        channel_id = int(getattr(self, "_alert_channel_id", 0) or 0)
        if not channel_id:
            return
        content = f"{self._alert_mention} {message}".strip() if self._alert_mention else message
        try:
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                channel = await self.bot.fetch_channel(channel_id)
            if channel is None or not hasattr(channel, "send"):
                return
            await channel.send(content=content)
        except (Forbidden, HTTPException):
            log.debug("Konnte Alert nicht senden (Discord-Zugriff verweigert).", exc_info=True)
        except Exception:
            log.debug("Konnte Alert nicht senden.", exc_info=True)

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
