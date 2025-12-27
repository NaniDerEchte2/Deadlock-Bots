# cogs/twitch/twitch_chat_bot.py
"""
Twitch IRC Chat Bot für Raid-Bot-Steuerung.

Streamer können den Raid-Bot direkt über Twitch-Chat-Commands steuern:
- !raid_enable / !raidbot - Aktiviert Auto-Raids
- !raid_disable / !raidbot_off - Deaktiviert Auto-Raids
- !raid_status - Zeigt den Status an
- !raid_history - Zeigt die letzten Raids
"""
import asyncio
import logging
import re
from typing import Optional, Set
import os

try:
    from twitchio.ext import commands as twitchio_commands
    from twitchio import Message, Channel
    TWITCHIO_AVAILABLE = True
except ImportError:
    TWITCHIO_AVAILABLE = False
    log = logging.getLogger("TwitchStreams.ChatBot")
    log.warning(
        "twitchio nicht installiert. Twitch Chat Bot wird nicht verfügbar sein. "
        "Installation: pip install twitchio"
    )

from .storage import get_conn

log = logging.getLogger("TwitchStreams.ChatBot")


if TWITCHIO_AVAILABLE:
    class RaidChatBot(twitchio_commands.Bot):
        """Twitch IRC Bot für Raid-Commands im Chat."""

        def __init__(
            self,
            token: str,
            client_id: str,
            prefix: str = "!",
            initial_channels: Optional[list] = None,
        ):
            super().__init__(
                token=token,
                client_id=client_id,
                prefix=prefix,
                initial_channels=initial_channels or [],
            )
            self._raid_bot = None  # Wird später gesetzt
            self._monitored_streamers: Set[str] = set()
            log.info("Twitch Chat Bot initialized with %d channels", len(initial_channels or []))

        def set_raid_bot(self, raid_bot):
            """Setzt die RaidBot-Instanz für OAuth-URLs."""
            self._raid_bot = raid_bot

        async def event_ready(self):
            """Wird aufgerufen, wenn der Bot verbunden ist."""
            log.info("Twitch Chat Bot ready | Logged in as: %s", self.nick)
            log.info("Connected to channels: %s", ", ".join([c.name for c in self.connected_channels]))

        async def event_message(self, message: Message):
            """Wird bei jeder Chat-Nachricht aufgerufen."""
            # Ignoriere Bot-Nachrichten
            if message.echo:
                return

            # Verarbeite Commands
            await self.handle_commands(message)

        def _get_streamer_by_channel(self, channel_name: str) -> Optional[tuple]:
            """Findet Streamer-Daten anhand des Channel-Namens."""
            normalized = channel_name.lower().lstrip("#")
            with get_conn() as conn:
                row = conn.execute(
                    """
                    SELECT twitch_login, twitch_user_id, raid_bot_enabled
                    FROM twitch_streamers
                    WHERE LOWER(twitch_login) = ?
                    """,
                    (normalized,),
                ).fetchone()
            return row

        @twitchio_commands.command(name="raid_enable", aliases=["raidbot"])
        async def cmd_raid_enable(self, ctx: twitchio_commands.Context):
            """!raid_enable - Aktiviert den Auto-Raid-Bot."""
            # Nur Broadcaster oder Mods dürfen den Bot steuern
            if not (ctx.author.is_broadcaster or ctx.author.is_mod):
                await ctx.send(
                    f"@{ctx.author.name} Nur der Broadcaster oder Mods können den Raid-Bot steuern."
                )
                return

            channel_name = ctx.channel.name
            streamer_data = self._get_streamer_by_channel(channel_name)

            if not streamer_data:
                await ctx.send(
                    f"@{ctx.author.name} Dieser Kanal ist nicht als Partner registriert. "
                    "Kontaktiere einen Admin für Details."
                )
                return

            twitch_login, twitch_user_id, raid_bot_enabled = streamer_data

            # Prüfen, ob bereits autorisiert
            with get_conn() as conn:
                auth_row = conn.execute(
                    "SELECT raid_enabled FROM twitch_raid_auth WHERE twitch_user_id = ?",
                    (twitch_user_id,),
                ).fetchone()

            if not auth_row:
                # Noch nicht autorisiert -> OAuth-Link senden
                if not self._raid_bot:
                    await ctx.send(
                        f"@{ctx.author.name} Der Raid-Bot ist derzeit nicht verfügbar. "
                        "Kontaktiere einen Admin."
                    )
                    return

                auth_url = self._raid_bot.auth_manager.generate_auth_url(twitch_login)
                await ctx.send(
                    f"@{ctx.author.name} Um den Auto-Raid-Bot zu nutzen, musst du ihn zuerst autorisieren. "
                    f"Klicke hier: {auth_url} (Der Bot raidet automatisch andere Partner, wenn du offline gehst)"
                )
                log.info("Sent raid auth link to %s via chat", twitch_login)
                return

            # Bereits autorisiert -> aktivieren
            raid_enabled = auth_row[0]
            if raid_enabled:
                await ctx.send(
                    f"@{ctx.author.name} ✅ Auto-Raid ist bereits aktiviert! "
                    "Der Bot raidet automatisch andere Partner, wenn du offline gehst."
                )
                return

            # Aktivieren
            with get_conn() as conn:
                conn.execute(
                    "UPDATE twitch_raid_auth SET raid_enabled = 1 WHERE twitch_user_id = ?",
                    (twitch_user_id,),
                )
                conn.execute(
                    "UPDATE twitch_streamers SET raid_bot_enabled = 1 WHERE twitch_user_id = ?",
                    (twitch_user_id,),
                )
                conn.commit()

            await ctx.send(
                f"@{ctx.author.name} ✅ Auto-Raid aktiviert! "
                "Wenn du offline gehst, raidet der Bot automatisch den Partner mit der kürzesten Stream-Zeit."
            )
            log.info("Enabled auto-raid for %s via chat", twitch_login)

        @twitchio_commands.command(name="raid_disable", aliases=["raidbot_off"])
        async def cmd_raid_disable(self, ctx: twitchio_commands.Context):
            """!raid_disable - Deaktiviert den Auto-Raid-Bot."""
            if not (ctx.author.is_broadcaster or ctx.author.is_mod):
                await ctx.send(
                    f"@{ctx.author.name} Nur der Broadcaster oder Mods können den Raid-Bot steuern."
                )
                return

            channel_name = ctx.channel.name
            streamer_data = self._get_streamer_by_channel(channel_name)

            if not streamer_data:
                await ctx.send(
                    f"@{ctx.author.name} Dieser Kanal ist nicht als Partner registriert."
                )
                return

            twitch_login, twitch_user_id, _ = streamer_data

            with get_conn() as conn:
                conn.execute(
                    "UPDATE twitch_raid_auth SET raid_enabled = 0 WHERE twitch_user_id = ?",
                    (twitch_user_id,),
                )
                conn.execute(
                    "UPDATE twitch_streamers SET raid_bot_enabled = 0 WHERE twitch_user_id = ?",
                    (twitch_user_id,),
                )
                conn.commit()

            await ctx.send(
                f"@{ctx.author.name} 🛑 Auto-Raid deaktiviert. "
                "Du kannst es jederzeit mit !raid_enable wieder aktivieren."
            )
            log.info("Disabled auto-raid for %s via chat", twitch_login)

        @twitchio_commands.command(name="raid_status", aliases=["raidbot_status"])
        async def cmd_raid_status(self, ctx: twitchio_commands.Context):
            """!raid_status - Zeigt den Raid-Bot-Status an."""
            channel_name = ctx.channel.name
            streamer_data = self._get_streamer_by_channel(channel_name)

            if not streamer_data:
                await ctx.send(
                    f"@{ctx.author.name} Dieser Kanal ist nicht als Partner registriert."
                )
                return

            twitch_login, twitch_user_id, raid_bot_enabled = streamer_data

            with get_conn() as conn:
                auth_row = conn.execute(
                    """
                    SELECT raid_enabled, authorized_at
                    FROM twitch_raid_auth
                    WHERE twitch_user_id = ?
                    """,
                    (twitch_user_id,),
                ).fetchone()

                # Statistiken
                stats = conn.execute(
                    """
                    SELECT COUNT(*) as total, SUM(success) as successful
                    FROM twitch_raid_history
                    WHERE from_broadcaster_id = ?
                    """,
                    (twitch_user_id,),
                ).fetchone()
                total_raids, successful_raids = stats if stats else (0, 0)

                # Letzter Raid
                last_raid = conn.execute(
                    """
                    SELECT to_broadcaster_login, viewer_count, executed_at, success
                    FROM twitch_raid_history
                    WHERE from_broadcaster_id = ?
                    ORDER BY executed_at DESC
                    LIMIT 1
                    """,
                    (twitch_user_id,),
                ).fetchone()

            # Status bestimmen
            if not auth_row:
                status = "❌ Nicht autorisiert"
                action = "Verwende !raid_enable zum Aktivieren."
            elif auth_row[0]:  # raid_enabled
                status = "✅ Aktiv"
                action = "Auto-Raids sind aktiviert."
            else:
                status = "🛑 Deaktiviert"
                action = "Aktiviere mit !raid_enable."

            # Nachricht zusammenstellen
            message = f"@{ctx.author.name} Raid-Bot Status: {status}. {action}"

            if total_raids:
                message += f" | Statistik: {total_raids} Raids ({successful_raids or 0} erfolgreich)"

            if last_raid:
                to_login, viewers, executed_at, success = last_raid
                icon = "✅" if success else "❌"
                time_str = executed_at[:16] if executed_at else "?"
                message += f" | Letzter Raid {icon}: {to_login} ({viewers} Viewer) am {time_str}"

            await ctx.send(message)

        @twitchio_commands.command(name="raid_history", aliases=["raidbot_history"])
        async def cmd_raid_history(self, ctx: twitchio_commands.Context):
            """!raid_history - Zeigt die letzten 3 Raids an."""
            channel_name = ctx.channel.name
            streamer_data = self._get_streamer_by_channel(channel_name)

            if not streamer_data:
                return

            twitch_login, twitch_user_id, _ = streamer_data

            with get_conn() as conn:
                raids = conn.execute(
                    """
                    SELECT to_broadcaster_login, viewer_count, executed_at, success
                    FROM twitch_raid_history
                    WHERE from_broadcaster_id = ?
                    ORDER BY executed_at DESC
                    LIMIT 3
                    """,
                    (twitch_user_id,),
                ).fetchall()

            if not raids:
                await ctx.send(f"@{ctx.author.name} Noch keine Raids durchgeführt.")
                return

            raids_text = " | ".join([
                f"{'✅' if success else '❌'} {to_login} ({viewers}V, {executed_at[:10] if executed_at else '?'})"
                for to_login, viewers, executed_at, success in raids
            ])

            await ctx.send(f"@{ctx.author.name} Letzte Raids: {raids_text}")

        async def join_partner_channels(self):
            """Joint alle Partner-Channels."""
            with get_conn() as conn:
                partners = conn.execute(
                    """
                    SELECT DISTINCT twitch_login
                    FROM twitch_streamers
                    WHERE (manual_verified_permanent = 1
                           OR manual_verified_until IS NOT NULL
                           OR manual_verified_at IS NOT NULL)
                      AND manual_partner_opt_out = 0
                    """
                ).fetchall()

            channels_to_join = [row[0] for row in partners if row[0]]
            new_channels = [c for c in channels_to_join if c.lower() not in self._monitored_streamers]

            if new_channels:
                log.info("Joining %d new partner channels: %s", len(new_channels), ", ".join(new_channels[:10]))
                for channel in new_channels:
                    try:
                        await self.join_channels([channel])
                        self._monitored_streamers.add(channel.lower())
                        await asyncio.sleep(0.5)  # Rate limiting
                    except Exception as e:
                        log.exception("Failed to join channel %s: %s", channel, e)


    async def create_twitch_chat_bot(
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        raid_bot = None,
    ) -> Optional[RaidChatBot]:
        """
        Erstellt einen Twitch Chat Bot mit Bot-Account-Token.

        Env-Variablen:
        - TWITCH_BOT_TOKEN: OAuth-Token für den Bot-Account
        - TWITCH_BOT_NAME: Name des Bot-Accounts (optional)
        """
        bot_token = os.getenv("TWITCH_BOT_TOKEN", "").strip()
        if not bot_token:
            log.warning(
                "TWITCH_BOT_TOKEN nicht gesetzt. Twitch Chat Bot wird nicht gestartet. "
                "Bitte setze ein OAuth-Token für den Bot-Account."
            )
            return None

        # Partner-Channels abrufen
        with get_conn() as conn:
            partners = conn.execute(
                """
                SELECT DISTINCT twitch_login
                FROM twitch_streamers
                WHERE (manual_verified_permanent = 1
                       OR manual_verified_until IS NOT NULL
                       OR manual_verified_at IS NOT NULL)
                  AND manual_partner_opt_out = 0
                """
            ).fetchall()

        initial_channels = [row[0] for row in partners if row[0]]
        log.info("Creating Twitch Chat Bot for %d partner channels", len(initial_channels))

        bot = RaidChatBot(
            token=bot_token,
            client_id=client_id,
            prefix="!",
            initial_channels=initial_channels,
        )
        bot.set_raid_bot(raid_bot)

        return bot

else:
    # Fallback wenn twitchio nicht installiert ist
    class RaidChatBot:
        pass

    async def create_twitch_chat_bot(*args, **kwargs):
        log.error(
            "TwitchIO nicht installiert. Twitch Chat Bot kann nicht erstellt werden. "
            "Installation: pip install twitchio"
        )
        return None
