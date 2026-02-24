# cogs/twitch/raid_commands.py
"""Discord Commands für Twitch-Bot-Steuerung durch Streamer."""

import logging
import secrets

import discord
from discord.ext import commands

from ..chat.constants import PROMO_MESSAGES
from ..storage import get_conn as _sqlite_get_conn
from ..storage_pg import get_conn
from .views import RaidAuthGenerateView, build_raid_requirements_embed

log = logging.getLogger("TwitchStreams.RaidCommands")


class RaidCommandsMixin:
    """Discord-Commands für Twitch-Bot-Verwaltung durch Streamer."""

    @commands.hybrid_command(name="traid", aliases=["twitch_raid_auth"])
    async def cmd_twitch_raid_auth(self, ctx: commands.Context):
        """Sende den Twitch-OAuth-Link für Raid/Follower/Chat-Scopes."""
        discord_user_id = str(ctx.author.id)

        with get_conn() as conn:
            row = conn.execute(
                """
                SELECT twitch_login, twitch_user_id
                FROM twitch_streamers
                WHERE discord_user_id = ?
                """,
                (discord_user_id,),
            ).fetchone()

        if not row:
            await ctx.send(
                "❌ Du bist nicht als Streamer-Partner registriert. Bitte zuerst verifizieren (z. B. `/streamer`).",
                ephemeral=True,
            )
            return

        twitch_login, twitch_user_id = row

        if not hasattr(self, "_raid_bot") or not self._raid_bot:
            await ctx.send(
                "⚠️ Der Twitch-Bot ist derzeit nicht verfügbar. Bitte wende dich an @earlyalty.",
                ephemeral=True,
            )
            return

        view = RaidAuthGenerateView(
            auth_manager=self._raid_bot.auth_manager,
            twitch_login=twitch_login,
        )
        await ctx.send(
            "Klicke auf den Button, um einen frischen Twitch-OAuth-Link zu erzeugen.",
            view=view,
            ephemeral=True,
        )
        log.info("Sent traid auth link to %s (discord_id=%s)", twitch_login, discord_user_id)

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
        with _sqlite_get_conn() as conn:
            auth_row = conn.execute(
                "SELECT raid_enabled FROM twitch_raid_auth WHERE twitch_user_id = ?",
                (twitch_user_id,),
            ).fetchone()

        if not auth_row:
            # Noch nicht autorisiert -> OAuth-Link generieren
            if not hasattr(self, "_raid_bot") or not self._raid_bot:
                await ctx.send(
                    "❌ Der Twitch-Bot ist derzeit nicht verfügbar. Bitte kontaktiere einen Admin.",
                    ephemeral=True,
                )
                return

            embed = build_raid_requirements_embed(twitch_login)
            view = RaidAuthGenerateView(
                auth_manager=self._raid_bot.auth_manager,
                twitch_login=twitch_login,
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
        with _sqlite_get_conn() as conn:
            conn.execute(
                "UPDATE twitch_raid_auth SET raid_enabled = 1 WHERE twitch_user_id = ?",
                (twitch_user_id,),
            )
            conn.commit()
        with get_conn() as conn:
            conn.execute(
                "UPDATE twitch_streamers SET raid_bot_enabled = 1 WHERE twitch_user_id = ?",
                (twitch_user_id,),
            )

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

        with _sqlite_get_conn() as conn:
            conn.execute(
                "UPDATE twitch_raid_auth SET raid_enabled = 0 WHERE twitch_user_id = ?",
                (twitch_user_id,),
            )
            conn.commit()
        with get_conn() as conn:
            conn.execute(
                "UPDATE twitch_streamers SET raid_bot_enabled = 0 WHERE twitch_user_id = ?",
                (twitch_user_id,),
            )

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
            _s_row = conn.execute(
                "SELECT twitch_login, twitch_user_id, raid_bot_enabled FROM twitch_streamers WHERE discord_user_id = ?",
                (discord_user_id,),
            ).fetchone()

        if not _s_row:
            await ctx.send(
                "❌ Du bist nicht als Streamer-Partner registriert.",
                ephemeral=True,
            )
            return

        twitch_login, twitch_user_id, raid_bot_enabled = _s_row[0], _s_row[1], _s_row[2]

        with _sqlite_get_conn() as conn:
            _a_row = conn.execute(
                "SELECT raid_enabled, authorized_at, token_expires_at FROM twitch_raid_auth WHERE twitch_user_id = ?",
                (twitch_user_id,),
            ).fetchone()
        raid_enabled = _a_row[0] if _a_row else None
        authorized_at = _a_row[1] if _a_row else None
        token_expires_at = _a_row[2] if _a_row else None

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
            title=f"🎯 Twitch-Bot Status für {twitch_login}",
            color=0x9146FF if raid_enabled else 0x808080,
        )

        # Status
        if not authorized_at:
            status = "❌ Nicht autorisiert (OAuth fehlt)"
            status_desc = "Anforderung: Twitch-Bot autorisieren mit `/raid_enable`."
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

        for (
            to_login,
            viewers,
            duration_sec,
            executed_at,
            success,
            error_msg,
            candidates,
        ) in raids:
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

    @commands.hybrid_command(name="sendchatpromo")
    @commands.has_permissions(administrator=True)
    async def cmd_sendchatpromo(self, ctx: commands.Context, streamer: str):
        """Sendet testweise eine Chat-Promo an einen Twitch-Streamer."""
        chat_bot = getattr(self, "_twitch_chat_bot", None)
        if not chat_bot:
            await ctx.send("Der Twitch Chat Bot ist nicht aktiv.", ephemeral=True)
            return

        login = streamer.strip().lower().lstrip("@#")
        if not login:
            await ctx.send("Bitte einen Streamer-Namen angeben.", ephemeral=True)
            return

        # Streamer-ID aus DB holen
        with get_conn() as conn:
            row = conn.execute(
                "SELECT twitch_user_id FROM twitch_streamers WHERE LOWER(twitch_login) = ?",
                (login,),
            ).fetchone()

        if not row or not row[0]:
            await ctx.send(f"Streamer **{login}** nicht in der DB gefunden.", ephemeral=True)
            return

        channel_id = str(row[0])

        # Invite ermitteln
        invite, is_specific = await chat_bot._get_promo_invite(login)
        if not invite:
            await ctx.send(f"Kein Discord-Invite für **{login}** verfügbar.", ephemeral=True)
            return

        msg = secrets.choice(PROMO_MESSAGES).format(invite=invite)

        # Nachricht senden via Announcement (Fallback auf normale Message)
        ok = await chat_bot._send_announcement(
            chat_bot._make_promo_channel(login, channel_id),
            msg,
            color="purple",
            source="promo",
        )

        if ok:
            await ctx.send(f"Promo an **{login}** gesendet:\n> {msg}", ephemeral=True)
            log.info("Manual promo sent to %s by %s", login, ctx.author)
        else:
            await ctx.send(f"Promo an **{login}** konnte nicht gesendet werden.", ephemeral=True)
            log.warning("Manual promo to %s failed (triggered by %s)", login, ctx.author)

    @commands.hybrid_command(name="reauth_all")
    @commands.has_permissions(administrator=True)
    async def cmd_reauth_all(self, ctx: commands.Context):
        """(Admin) Alle Streamer zur Neu-Autorisierung auffordern (neue Scopes)."""
        auth_manager = getattr(getattr(self, "_raid_bot", None), "auth_manager", None)
        if not auth_manager:
            await ctx.send("❌ Raid-Bot nicht verfügbar.", ephemeral=True)
            return

        await ctx.defer(ephemeral=True)

        # Tokens sichern und needs_reauth=1 setzen
        count = await auth_manager.snapshot_and_flag_reauth()

        # Persistente Views (neu) registrieren damit Buttons sofort klickbar sind
        if hasattr(self, "_register_persistent_raid_auth_views"):
            self._register_persistent_raid_auth_views()

        # Alle Streamer mit needs_reauth=1 und Discord-User-ID holen
        with _sqlite_get_conn() as conn:
            _auth_rows = conn.execute(
                "SELECT twitch_user_id, twitch_login FROM twitch_raid_auth WHERE needs_reauth = 1"
            ).fetchall()
        rows = []
        if _auth_rows:
            _uids = [r[0] for r in _auth_rows]
            _ph = ",".join("?" * len(_uids))
            with get_conn() as conn:
                _discord_rows = conn.execute(
                    f"SELECT twitch_user_id, discord_user_id FROM twitch_streamers WHERE twitch_user_id IN ({_ph})",
                    _uids,
                ).fetchall()
            _discord_map = {str(r[0]): r[1] for r in _discord_rows}
            rows = [(r[0], r[1], _discord_map.get(str(r[0]))) for r in _auth_rows]

        sent, deleted_total = 0, 0
        failed_list: list[str] = []
        for row in rows:
            twitch_user_id = row[0] if not hasattr(row, "keys") else row["twitch_user_id"]
            twitch_login = row[1] if not hasattr(row, "keys") else row["twitch_login"]
            discord_uid = row[2] if not hasattr(row, "keys") else row["discord_user_id"]
            if not discord_uid:
                failed_list.append(f"`{twitch_login}` (keine Discord-ID)")
                continue
            try:
                user = await ctx.bot.fetch_user(int(discord_uid))
                dm_channel = await user.create_dm()

                # Alte Bot-Nachrichten in der DM löschen (letzten 50 Msgs)
                async for msg in dm_channel.history(limit=50):
                    if msg.author.id == ctx.bot.user.id:
                        try:
                            await msg.delete()
                            deleted_total += 1
                        except Exception:
                            log.debug(
                                "reauth_all: Konnte DM-Nachricht %s nicht löschen für %s",
                                msg.id,
                                twitch_login,
                                exc_info=True,
                            )

                # Neue Nachricht mit persistentem Button senden
                embed = build_raid_requirements_embed(twitch_login)
                view = RaidAuthGenerateView(twitch_login=twitch_login)
                await dm_channel.send(
                    "🔄 **Neue Twitch-Autorisierung erforderlich** – der Bot benötigt "
                    "zusätzliche Scopes (Bits, Hype Train, Subscriptions, Ads). "
                    "Bitte autorisiere deinen Account neu:",
                    embed=embed,
                    view=view,
                )
                # reauth_notified_at aktualisieren
                with _sqlite_get_conn() as conn:
                    conn.execute(
                        "UPDATE twitch_raid_auth SET reauth_notified_at=CURRENT_TIMESTAMP "
                        "WHERE twitch_user_id=?",
                        (twitch_user_id,),
                    )
                    conn.commit()
                sent += 1
                log.info("reauth_all: DM gesendet an %s (%s)", twitch_login, discord_uid)
            except Exception as e:
                reason = str(e)[:60] if str(e) else "Unbekannt"
                failed_list.append(f"`{twitch_login}` (<@{discord_uid}>) – {reason}")
                log.warning(
                    "reauth_all: DM fehlgeschlagen für %s (%s)",
                    twitch_login,
                    discord_uid,
                    exc_info=True,
                )

        failed = len(failed_list)
        summary = (
            f"✅ Re-Auth gestartet:\n"
            f"• **{count}** Tokens gesichert (needs_reauth=1)\n"
            f"• **{deleted_total}** alte Bot-Nachrichten gelöscht\n"
            f"• **{sent}** DMs gesendet\n"
            f"• **{failed}** fehlgeschlagen"
        )
        if failed_list:
            summary += "\n\n**Fehlgeschlagen:**\n" + "\n".join(failed_list)

        # Discord-Limit: max 2000 Zeichen pro Nachricht
        if len(summary) > 1990:
            summary = summary[:1990] + "…"

        await ctx.send(summary, ephemeral=True)
        log.info(
            "reauth_all: %d gesichert, %d gelöscht, %d DMs, %d fehlgeschlagen",
            count,
            deleted_total,
            sent,
            failed,
        )

    @commands.command(name="tte")
    @commands.is_owner()
    async def cmd_test_token_error(
        self,
        ctx: commands.Context,
        target: discord.User = None,
        mode: str = "initial",
    ):
        """(Owner) Sendet Token-Error-DM an einen User. Usage: !tte [@user] [initial|reminder]"""
        handler = getattr(getattr(self, "_raid_bot", None), "auth_manager", None)
        handler = getattr(handler, "token_error_handler", None) if handler else None
        if not handler:
            await ctx.send("❌ Token error handler nicht verfügbar.", ephemeral=True)
            return

        handler.discord_bot = self.bot  # type: ignore[attr-defined]
        is_reminder = mode.lower() == "reminder"

        if target is None:
            # Kein User angegeben → Test-DM an Admin mit Fake-Daten
            ADMIN_TARGET_ID = 662995601738170389
            original = handler._get_discord_user_id
            handler._get_discord_user_id = lambda *_: str(ADMIN_TARGET_ID)
            try:
                success = await handler._send_user_dm_token_error(
                    str(ADMIN_TARGET_ID),
                    "teststreamer",
                    'HTTP 400: {"status":400,"message":"Invalid refresh token"}',
                    is_reminder=is_reminder,
                )
            finally:
                handler._get_discord_user_id = original

            if success:
                await ctx.send(
                    f"✅ Test-DM (`{mode}`) gesendet an <@{ADMIN_TARGET_ID}>.",
                    ephemeral=True,
                )
            else:
                await ctx.send(
                    "❌ DM konnte nicht gesendet werden (DMs geschlossen?).",
                    ephemeral=True,
                )
            return

        # User angegeben → echte Daten aus DB holen
        discord_user_id = str(target.id)
        with get_conn() as conn:
            _s_row = conn.execute(
                "SELECT twitch_user_id FROM twitch_streamers WHERE discord_user_id = ? LIMIT 1",
                (discord_user_id,),
            ).fetchone()

        if not _s_row or not _s_row[0]:
            await ctx.send(f"❌ Kein Twitch-Account für <@{target.id}> gefunden.", ephemeral=True)
            return

        twitch_user_id = _s_row[0]
        with _sqlite_get_conn() as conn:
            _a_row = conn.execute(
                "SELECT twitch_login FROM twitch_raid_auth WHERE twitch_user_id = ?",
                (twitch_user_id,),
            ).fetchone()
        with get_conn() as conn:
            _b_row = conn.execute(
                "SELECT error_message, notified, user_dm_sent FROM twitch_token_blacklist WHERE twitch_user_id = ?",
                (twitch_user_id,),
            ).fetchone()
        twitch_login = _a_row[0] if _a_row else None
        error_message = _b_row[0] if _b_row else None
        notified = _b_row[1] if _b_row else None
        user_dm_sent = _b_row[2] if _b_row else None
        error_message = (
            error_message or 'HTTP 400: {"status":400,"message":"Invalid refresh token"}'
        )

        # Monkey-Patch damit die DM an den richtigen User geht
        original = handler._get_discord_user_id
        handler._get_discord_user_id = lambda *_: discord_user_id
        try:
            success = await handler._send_user_dm_token_error(
                twitch_user_id,
                twitch_login,
                error_message,
                is_reminder=is_reminder,
            )
        finally:
            handler._get_discord_user_id = original

        if success:
            # Blacklist-Eintrag als benachrichtigt markieren damit der Bot nicht nochmal sendet
            with get_conn() as conn:
                conn.execute(
                    """
                    UPDATE twitch_token_blacklist
                    SET notified = 1, user_dm_sent = 1
                    WHERE twitch_user_id = ?
                    """,
                    (twitch_user_id,),
                )
                conn.commit()
            await ctx.send(
                f"✅ Token-Error-DM (`{mode}`) gesendet an <@{target.id}> (`{twitch_login}`).\n"
                f"Blacklist-Eintrag als `notified=1, user_dm_sent=1` markiert – Bot sendet nicht erneut.",
                ephemeral=True,
            )
        else:
            await ctx.send(
                f"❌ DM an <@{target.id}> fehlgeschlagen (DMs geschlossen?).",
                ephemeral=True,
            )
