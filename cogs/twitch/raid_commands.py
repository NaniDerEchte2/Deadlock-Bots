# cogs/twitch/raid_commands.py
"""Discord Commands für Raid-Bot-Steuerung durch Streamer."""

import logging
from typing import Optional

import discord
from discord.ext import commands

from .storage import get_conn

log = logging.getLogger("TwitchStreams.RaidCommands")


class RaidCommandsMixin:
    """Discord-Commands für Raid-Bot-Verwaltung durch Streamer."""

    @commands.hybrid_command(name="raid_enable", aliases=["raidbot"])
    async def cmd_raid_enable(self, ctx: commands.Context):
        """Aktiviere den Auto-Raid-Bot für deinen Twitch-Kanal."""
        # Discord User ID des Aufrufers
        discord_user_id = str(ctx.author.id)

        # Finde Streamer in DB über Discord-ID
        with get_conn() as conn:
            row = conn.execute(
                """
                SELECT twitch_login, twitch_user_id, raid_bot_enabled
                FROM twitch_streamers
                WHERE discord_user_id = ?
                """,
                (discord_user_id,),
            ).fetchone()

        if not row:
            await ctx.send(
                "❌ Du bist nicht als Streamer-Partner registriert. "
                "Verwende zuerst `/streamer`, um dich zu verifizieren.",
                ephemeral=True,
            )
            return

        twitch_login, twitch_user_id, raid_bot_enabled = row

        # Prüfen, ob bereits autorisiert
        with get_conn() as conn:
            auth_row = conn.execute(
                "SELECT raid_enabled FROM twitch_raid_auth WHERE twitch_user_id = ?",
                (twitch_user_id,),
            ).fetchone()

        if not auth_row:
            # Noch nicht autorisiert -> OAuth-Link generieren
            if not hasattr(self, "_raid_bot") or not self._raid_bot:
                await ctx.send(
                    "❌ Der Raid-Bot ist derzeit nicht verfügbar. "
                    "Bitte kontaktiere einen Admin.",
                    ephemeral=True,
                )
                return

            auth_url = self._raid_bot.auth_manager.generate_auth_url(twitch_login)
            embed = discord.Embed(
                title="🎯 Raid-Bot Autorisierung",
                description=(
                    f"Hallo **{twitch_login}**!\n\n"
                    "Um den Auto-Raid-Bot zu nutzen, musst du ihn zuerst auf Twitch autorisieren.\n\n"
                    "**Was macht der Raid-Bot?**\n"
                    "• Wenn du offline gehst, raidet der Bot automatisch einen anderen Online-Partner\n"
                    "• Es wird der Partner mit der kürzesten Stream-Zeit geraidet\n"
                    "• Alle Raids werden mit Metadaten gespeichert\n\n"
                    "Klicke auf den Button unten, um den Bot zu autorisieren:"
                ),
                color=0x9146FF,
            )
            view = discord.ui.View()
            view.add_item(
                discord.ui.Button(
                    label="Auf Twitch autorisieren",
                    url=auth_url,
                    style=discord.ButtonStyle.link,
                )
            )
            await ctx.send(embed=embed, view=view, ephemeral=True)
            log.info("Sent raid auth link to %s (%s)", twitch_login, discord_user_id)
            return

        # Bereits autorisiert -> aktivieren
        raid_enabled = auth_row[0]
        if raid_enabled:
            await ctx.send(
                f"✅ Auto-Raid ist bereits für **{twitch_login}** aktiviert!",
                ephemeral=True,
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
            f"✅ Auto-Raid wurde für **{twitch_login}** aktiviert!\n"
            "Wenn du offline gehst, raidet der Bot automatisch einen anderen Online-Partner.",
            ephemeral=True,
        )
        log.info("Enabled auto-raid for %s (%s)", twitch_login, discord_user_id)

    @commands.hybrid_command(name="raid_disable", aliases=["raidbot_off"])
    async def cmd_raid_disable(self, ctx: commands.Context):
        """Deaktiviere den Auto-Raid-Bot für deinen Twitch-Kanal."""
        discord_user_id = str(ctx.author.id)

        with get_conn() as conn:
            row = conn.execute(
                "SELECT twitch_login, twitch_user_id FROM twitch_streamers WHERE discord_user_id = ?",
                (discord_user_id,),
            ).fetchone()

        if not row:
            await ctx.send(
                "❌ Du bist nicht als Streamer-Partner registriert.",
                ephemeral=True,
            )
            return

        twitch_login, twitch_user_id = row

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
            f"🛑 Auto-Raid wurde für **{twitch_login}** deaktiviert.\n"
            "Du kannst es jederzeit mit `/raid_enable` wieder aktivieren.",
            ephemeral=True,
        )
        log.info("Disabled auto-raid for %s (%s)", twitch_login, discord_user_id)

    @commands.hybrid_command(name="raid_status", aliases=["raidbot_status"])
    async def cmd_raid_status(self, ctx: commands.Context):
        """Zeige den Status deines Auto-Raid-Bots an."""
        discord_user_id = str(ctx.author.id)

        with get_conn() as conn:
            row = conn.execute(
                """
                SELECT s.twitch_login, s.twitch_user_id, s.raid_bot_enabled,
                       a.raid_enabled, a.authorized_at, a.token_expires_at
                FROM twitch_streamers s
                LEFT JOIN twitch_raid_auth a ON s.twitch_user_id = a.twitch_user_id
                WHERE s.discord_user_id = ?
                """,
                (discord_user_id,),
            ).fetchone()

        if not row:
            await ctx.send(
                "❌ Du bist nicht als Streamer-Partner registriert.",
                ephemeral=True,
            )
            return

        twitch_login, twitch_user_id, raid_bot_enabled, raid_enabled, authorized_at, token_expires_at = row

        # Raid-History abrufen
        with get_conn() as conn:
            history = conn.execute(
                """
                SELECT COUNT(*) as total, SUM(success) as successful
                FROM twitch_raid_history
                WHERE from_broadcaster_id = ?
                """,
                (twitch_user_id,),
            ).fetchone()
            total_raids, successful_raids = history if history else (0, 0)

            recent_raids = conn.execute(
                """
                SELECT to_broadcaster_login, viewer_count, executed_at, success
                FROM twitch_raid_history
                WHERE from_broadcaster_id = ?
                ORDER BY executed_at DESC
                LIMIT 5
                """,
                (twitch_user_id,),
            ).fetchall()

        embed = discord.Embed(
            title=f"🎯 Raid-Bot Status für {twitch_login}",
            color=0x9146FF if raid_enabled else 0x808080,
        )

        # Status
        if not authorized_at:
            status = "❌ Nicht autorisiert"
            status_desc = "Verwende `/raid_enable`, um den Bot zu autorisieren."
        elif raid_enabled:
            status = "✅ Aktiv"
            status_desc = "Auto-Raids sind aktiviert."
        else:
            status = "🛑 Deaktiviert"
            status_desc = "Auto-Raids sind deaktiviert. Aktiviere sie mit `/raid_enable`."

        embed.add_field(name="Status", value=status, inline=True)
        embed.add_field(name="Beschreibung", value=status_desc, inline=False)

        # Statistiken
        if total_raids:
            embed.add_field(
                name="Statistik",
                value=f"**{total_raids}** Raids insgesamt\n**{successful_raids or 0}** erfolgreich",
                inline=True,
            )

        # Letzte Raids
        if recent_raids:
            raids_text = ""
            for to_login, viewers, executed_at, success in recent_raids:
                icon = "✅" if success else "❌"
                time_str = executed_at[:16] if executed_at else "?"
                raids_text += f"{icon} **{to_login}** ({viewers} Viewer) - {time_str}\n"
            embed.add_field(name="Letzte Raids", value=raids_text, inline=False)

        # Token-Ablauf
        if token_expires_at:
            embed.add_field(
                name="Autorisierung läuft ab am",
                value=token_expires_at[:16],
                inline=True,
            )

        await ctx.send(embed=embed, ephemeral=True)

    @commands.hybrid_command(name="raid_history", aliases=["raidbot_history"])
    async def cmd_raid_history(self, ctx: commands.Context, limit: int = 10):
        """Zeige deine Raid-History an (max 20 Einträge)."""
        discord_user_id = str(ctx.author.id)
        limit = min(max(1, limit), 20)  # Zwischen 1 und 20

        with get_conn() as conn:
            row = conn.execute(
                "SELECT twitch_login, twitch_user_id FROM twitch_streamers WHERE discord_user_id = ?",
                (discord_user_id,),
            ).fetchone()

        if not row:
            await ctx.send(
                "❌ Du bist nicht als Streamer-Partner registriert.",
                ephemeral=True,
            )
            return

        twitch_login, twitch_user_id = row

        with get_conn() as conn:
            raids = conn.execute(
                """
                SELECT to_broadcaster_login, viewer_count, stream_duration_sec,
                       executed_at, success, error_message, candidates_count
                FROM twitch_raid_history
                WHERE from_broadcaster_id = ?
                ORDER BY executed_at DESC
                LIMIT ?
                """,
                (twitch_user_id, limit),
            ).fetchall()

        if not raids:
            await ctx.send(
                f"Noch keine Raids für **{twitch_login}** durchgeführt.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title=f"🎯 Raid-History für {twitch_login}",
            description=f"Letzte {len(raids)} Raids",
            color=0x9146FF,
        )

        for to_login, viewers, duration_sec, executed_at, success, error_msg, candidates in raids:
            icon = "✅" if success else "❌"
            time_str = executed_at[:16] if executed_at else "?"
            duration_min = (duration_sec or 0) // 60

            field_value = f"{icon} **{viewers}** Viewer, Stream-Dauer: **{duration_min}** Min\n"
            field_value += f"Kandidaten: **{candidates or 0}**\n"
            if not success and error_msg:
                field_value += f"Fehler: `{error_msg[:100]}`\n"

            embed.add_field(
                name=f"{time_str} → {to_login}",
                value=field_value,
                inline=False,
            )

        await ctx.send(embed=embed, ephemeral=True)
