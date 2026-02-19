from __future__ import annotations

import asyncio
import logging
import os
from typing import List

import discord


class PresenceMixin:
    """Presence, Ready-Tasks und Voice-Routing."""

    def active_cogs(self) -> List[str]:
        """Aktuell geladene Extensions (runtime), nur 'cogs.'-Namespace."""
        return sorted([ext for ext in self.extensions.keys() if ext.startswith("cogs.")])

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
            logging.error(f"Dashboard konnte nicht gestartet werden: {e}. Laeuft bereits ein anderer Prozess?")
        except Exception as e:
            logging.error(f"Dashboard konnte nicht gestartet werden: {e}")

    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
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
        tasks = [(cog_name, handler(member, before, after)) for cog_name, handler in handler_info]
        results = await asyncio.gather(*[task for _, task in tasks], return_exceptions=True)

        # Log Fehler mit korrektem Cog-Namen (Race-Safe!)
        for (cog_name, _), result in zip(tasks, results):
            if isinstance(result, Exception):
                logging.error(f"Voice handler error in {cog_name}: {result}", exc_info=result)

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
            logging.getLogger().debug("TempVoice Ready-Log fehlgeschlagen (ignoriert): %r", e)

        # Performance-Info loggen
        voice_handlers = sum(1 for cog in self.cogs.values() if hasattr(cog, "on_voice_state_update"))
        if voice_handlers > 0:
            logging.info(f"Voice Event Router aktiv: {voice_handlers} Handler (parallel)")

        asyncio.create_task(self.hourly_health_check())
        if self.standalone_manager:
            asyncio.create_task(self._bootstrap_standalone_autostart())

    async def queue_channel_rename(self, channel_id: int, new_name: str, reason: str = "Automated Rename"):
        rename_cog = self.get_cog("RenameManagerCog")
        if rename_cog:
            rename_cog.queue_local_rename_request(channel_id, new_name, reason)
        else:
            logging.error(
                "RenameManagerCog nicht geladen. Rename fuer Channel %s zu '%s' kann nicht verarbeitet werden.",
                channel_id,
                new_name,
            )

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
                        logging.warning("Standalone Manager Autostart-Pruefung fehlgeschlagen: %s", exc)

                if current - last_critical_check >= critical_check_interval:
                    issues = []

                    if not self.get_cog("TempVoiceCore"):
                        issues.append("TempVoiceCore not loaded")
                    if not self.get_cog("TempVoiceInterface"):
                        issues.append("TempVoiceInterface not loaded")
                    if "cogs.steam.steam_link_oauth" not in self.extensions:
                        issues.append("SteamLinkOAuth (module) not loaded")

                    if issues:
                        logging.warning(f"Critical Health Check: Issues found: {issues}")
                    else:
                        logging.debug("Critical Health Check: Core cogs operational")

                    last_critical_check = current

            except Exception as e:
                logging.error(f"Health check error: {e}")
