from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import List, Optional

import discord


_STEAM_LOG_CHANNEL_ID = 1374364800817303632


class PresenceMixin:
    """Presence, Ready-Tasks und Voice-Routing."""

    def active_cogs(self) -> List[str]:
        """Aktuell geladene Extensions (runtime), nur 'cogs.'-Namespace."""
        return sorted(
            [ext for ext in self.extensions.keys() if ext.startswith("cogs.")]
        )

    async def update_presence(self):
        """Presence immer anhand der echten Runtime-Anzahl setzen."""
        pfx = os.getenv("COMMAND_PREFIX", "!")
        try:
            if not self.is_ready() or getattr(self, "ws", None) is None:
                logging.debug("Presence-Update übersprungen – Bot noch nicht bereit")
                return
            activity = discord.Activity(
                type=discord.ActivityType.watching,
                name=f"{len(self.active_cogs())} Cogs | {pfx}help",
            )
            await self.change_presence(activity=activity)
        except Exception as exc:
            logging.exception("Konnte Presence nicht aktualisieren: %s", exc)

    async def _start_dashboard_background(self) -> None:
        if not self.dashboard:
            return
        try:
            logging.info("Dashboard HTTP server startup task running...")
            await self.dashboard.start()
            logging.info("Dashboard HTTP server startup completed.")
        except RuntimeError as e:
            logging.error(
                f"Dashboard konnte nicht gestartet werden: {e}. Laeuft bereits ein anderer Prozess?"
            )
        except Exception as e:
            logging.error(f"Dashboard konnte nicht gestartet werden: {e}")

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        """
        Voice Event Router - verteilt Voice State Updates parallel an alle Handler-Cogs.
        Verhindert sequenzielle Abarbeitung (40% schneller!).
        """
        # Sammle alle Voice-Handler aus den Cogs (mit Metadaten für Error-Logging)
        handler_info = []
        for cog_name, cog in self.cogs.items():
            if hasattr(cog, "on_voice_state_update"):
                handler = getattr(cog, "on_voice_state_update")
                if callable(handler):
                    handler_info.append((cog_name, handler))

        if not handler_info:
            return

        # Führe alle Handler PARALLEL aus (nicht sequenziell wie discord.py Default!)
        tasks = [
            (cog_name, handler(member, before, after))
            for cog_name, handler in handler_info
        ]
        results = await asyncio.gather(
            *[task for _, task in tasks], return_exceptions=True
        )

        # Log Fehler mit korrektem Cog-Namen (Race-Safe!)
        for (cog_name, _), result in zip(tasks, results):
            if isinstance(result, Exception):
                logging.error(
                    f"Voice handler error in {cog_name}: {result}", exc_info=result
                )

    async def on_ready(self):
        logging.info(f"Bot logged in as {self.user} (ID: {self.user.id})")
        logging.info(f"Connected to {len(self.guilds)} guilds")

        await self.update_presence()

        runtime_loaded = self.active_cogs()
        logging.info(f"Loaded cogs (runtime): {len(runtime_loaded)}")
        logging.info(f"Loaded cogs: {len(runtime_loaded)}/{len(self.cogs_list)}")

        # TempVoice Log (neu)
        try:
            tv_core = self.get_cog("TempVoiceCore")
            if tv_core:
                cnt = len(getattr(tv_core, "created_channels", set()))
                logging.info(f"TempVoiceCore bereit • verwaltete Lanes: {cnt}")
            tv_if = self.get_cog("TempVoiceInterface")
            if tv_if:
                logging.info("TempVoiceInterface bereit • Interface-View registriert")
        except Exception as e:
            logging.getLogger().debug(
                "TempVoice Ready-Log fehlgeschlagen (ignoriert): %r", e
            )

        # Performance-Info loggen
        voice_handlers = sum(
            1 for cog in self.cogs.values() if hasattr(cog, "on_voice_state_update")
        )
        if voice_handlers > 0:
            logging.info(
                f"Voice Event Router aktiv: {voice_handlers} Handler (parallel)"
            )

        asyncio.create_task(self.hourly_health_check())
        if self.standalone_manager:
            asyncio.create_task(self._bootstrap_standalone_autostart())

    async def queue_channel_rename(
        self, channel_id: int, new_name: str, reason: str = "Automated Rename"
    ):
        rename_cog = self.get_cog("RenameManagerCog")
        if rename_cog:
            rename_cog.queue_local_rename_request(channel_id, new_name, reason)
        else:
            logging.error(
                "RenameManagerCog nicht geladen. Rename fuer Channel %s zu '%s' kann nicht verarbeitet werden.",
                channel_id,
                new_name,
            )

    # State für Steam Bridge Login-Check
    _steam_not_logged_in_since: Optional[float] = None
    _steam_login_alert_at: float = 0.0

    async def _check_steam_bridge_login_health(self) -> None:
        """Erkennt wenn die Steam Bridge läuft aber nicht eingeloggt ist, schickt Alert und startet neu."""
        if not self.standalone_manager:
            return
        try:
            state_info = await self.standalone_manager.status("steam")
        except Exception:
            return

        if not state_info.get("running"):
            self._steam_not_logged_in_since = None
            return  # Prozess läuft nicht – ensure_autostart kümmert sich darum

        # Login-Status aus DB lesen
        try:
            from service import db

            def _get_state():
                row = db.query_one(
                    "SELECT heartbeat, payload FROM standalone_bot_state WHERE bot=?",
                    ("steam",),
                )
                if not row:
                    return 0, {}
                payload = {}
                if row["payload"]:
                    try:
                        payload = json.loads(row["payload"])
                    except Exception:  # noqa: S110
                        pass
                return int(row["heartbeat"] or 0), payload

            heartbeat, payload = await asyncio.to_thread(_get_state)
        except Exception as exc:
            logging.debug("Steam login health check: DB-Fehler %s", exc)
            return

        # Heartbeat muss frisch sein (sonst Bridge schreibt gerade noch nicht)
        if not heartbeat or time.time() - heartbeat > 90:
            self._steam_not_logged_in_since = None
            return

        runtime = payload.get("runtime", {})
        logged_on = runtime.get("logged_on", False)

        if logged_on:
            if self._steam_not_logged_in_since is not None:
                logging.info("Steam Bridge ist wieder eingeloggt – Health-Check OK")
            self._steam_not_logged_in_since = None
            return

        # Nicht eingeloggt – Timer starten
        now = time.time()
        if self._steam_not_logged_in_since is None:
            self._steam_not_logged_in_since = now
            return  # Erste Erkennung – erstmal abwarten

        not_logged_in_secs = now - self._steam_not_logged_in_since
        grace_period = 180  # 3 Minuten Toleranz (z.B. kurz nach Start)
        if not_logged_in_secs < grace_period:
            return

        # Cooldown: nicht mehr als einmal alle 10 Minuten
        if now - self._steam_login_alert_at < 600:
            return

        self._steam_login_alert_at = now
        self._steam_not_logged_in_since = None  # Reset – nächste Runde wieder sauber

        minutes_down = int(not_logged_in_secs / 60)
        last_error = runtime.get("last_error")
        error_info = f" (letzter Fehler: `{last_error}`)" if last_error else ""
        logging.warning(
            "Steam Bridge health check: nicht eingeloggt seit %d Min%s – starte Neustart",
            minutes_down,
            error_info or "",
        )

        channel = self.get_channel(_STEAM_LOG_CHANNEL_ID)
        owner_id = getattr(self, "owner_id", None)
        ping = f"<@{owner_id}>" if owner_id else ""

        if channel:
            await channel.send(
                f"{ping} ⚠️ **Steam Bridge nicht eingeloggt** seit {minutes_down} Min.{error_info} – versuche Neustart..."
            )

        try:
            await self.standalone_manager.restart("steam")
            logging.info("Steam Bridge Neustart durch Login-Health-Check ausgelöst")
            if channel:
                await channel.send("🔄 Steam Bridge Neustart wurde gestartet.")
        except Exception as exc:
            logging.error("Steam Bridge Neustart fehlgeschlagen: %s", exc)
            if channel:
                await channel.send(f"❌ Neustart fehlgeschlagen: `{exc}`")

    async def hourly_health_check(self):
        critical_check_interval = 3600  # 1h
        last_critical_check = 0.0

        while not self.is_closed():
            try:
                await asyncio.sleep(300)
                current = asyncio.get_running_loop().time()

                if self.standalone_manager:
                    try:
                        await self.standalone_manager.ensure_autostart()
                    except Exception as exc:
                        logging.warning(
                            "Standalone Manager Autostart-Pruefung fehlgeschlagen: %s",
                            exc,
                        )

                try:
                    await self._check_steam_bridge_login_health()
                except Exception as exc:
                    logging.warning("Steam Bridge Login-Health-Check fehlgeschlagen: %s", exc)

                if current - last_critical_check >= critical_check_interval:
                    issues = []

                    if not self.get_cog("TempVoiceCore"):
                        issues.append("TempVoiceCore not loaded")
                    if not self.get_cog("TempVoiceInterface"):
                        issues.append("TempVoiceInterface not loaded")
                    if "cogs.steam.steam_link_oauth" not in self.extensions:
                        issues.append("SteamLinkOAuth (module) not loaded")

                    if issues:
                        logging.warning(
                            f"Critical Health Check: Issues found: {issues}"
                        )
                    else:
                        logging.debug("Critical Health Check: Core cogs operational")

                    last_critical_check = current

            except Exception as e:
                logging.error(f"Health check error: {e}")
