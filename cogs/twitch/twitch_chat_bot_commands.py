import logging
from datetime import datetime, timezone

from .storage import get_conn
from .twitch_chat_bot_deps import TWITCHIO_AVAILABLE, twitchio_commands

log = logging.getLogger("TwitchStreams.ChatBot")


if TWITCHIO_AVAILABLE:
    class RaidCommandsMixin:
        def _load_last_autoban_from_log(self, channel_key: str):
            """Best-effort Fallback: letzten Auto-Ban aus Logdatei laden (überlebt Bot-Restarts)."""
            autoban_log = getattr(self, "_autoban_log", None)
            if not autoban_log:
                return None
            try:
                with autoban_log.open("r", encoding="utf-8") as handle:
                    lines = handle.read().splitlines()
            except Exception:
                log.debug("Konnte Auto-Ban-Logdatei nicht lesen", exc_info=True)
                return None

            for line in reversed(lines):
                parts = line.split("\t")
                if len(parts) < 5:
                    continue
                status = (parts[1] or "").strip().upper()
                logged_channel = self._normalize_channel_login(parts[2] if len(parts) > 2 else "")
                chatter_login = (parts[3] if len(parts) > 3 else "").strip()
                chatter_id = (parts[4] if len(parts) > 4 else "").strip()
                if status != "[BANNED]" or logged_channel != channel_key:
                    continue
                if not chatter_id:
                    continue
                return {
                    "user_id": chatter_id,
                    "login": chatter_login,
                }
            return None

        @twitchio_commands.command(name="raid_enable", aliases=["raidbot"])
        async def cmd_raid_enable(self, ctx: twitchio_commands.Context):
            """!raid_enable - Aktiviert den Auto-Raid-Bot."""
            # Nur Broadcaster oder Mods dürfen den Bot steuern
            if not (ctx.author.is_broadcaster or ctx.author.is_mod):
                await ctx.send(
                    f"@{ctx.author.name} Nur der Broadcaster oder Mods können den Twitch-Bot steuern."
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
                        f"@{ctx.author.name} Der Twitch-Bot ist derzeit nicht verfügbar. "
                        "Kontaktiere einen Admin."
                    )
                    return

                auth_url = self._raid_bot.auth_manager.generate_auth_url(twitch_login)
                await ctx.send(
                    f"@{ctx.author.name} OAuth fehlt – Anforderung: Twitch-Bot autorisieren (Pflicht für Streamer-Partner). "
                    f"Link: {auth_url} | Auto-Raid, Chat Guard, Discord Auto-Post (Analytics WIP)"
                )
                log.info("Sent raid auth link to %s via chat", twitch_login)
                return

            # Bereits autorisiert -> aktivieren
            raid_enabled = auth_row[0]
            if raid_enabled:
                await ctx.send(
                    f"@{ctx.author.name} ✅ Auto-Raid ist bereits aktiviert! "
                    "Der Twitch-Bot raidet automatisch andere Partner, wenn du offline gehst."
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
                "Wenn du offline gehst, raidet der Twitch-Bot automatisch den Partner mit der kürzesten Stream-Zeit."
            )
            log.info("Enabled auto-raid for %s via chat", twitch_login)

        @twitchio_commands.command(name="raid_disable", aliases=["raidbot_off"])
        async def cmd_raid_disable(self, ctx: twitchio_commands.Context):
            """!raid_disable - Deaktiviert den Auto-Raid-Bot."""
            if not (ctx.author.is_broadcaster or ctx.author.is_mod):
                await ctx.send(
                    f"@{ctx.author.name} Nur der Broadcaster oder Mods können den Twitch-Bot steuern."
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
            """!raid_status - Zeigt den Twitch-Bot-Status an."""
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
                status = "❌ Nicht autorisiert (OAuth fehlt)"
                action = "Anforderung: Twitch-Bot autorisieren mit !raid_enable."
            elif auth_row[0]:  # raid_enabled
                status = "✅ Aktiv"
                action = "Auto-Raids sind aktiviert."
            else:
                status = "🛑 Deaktiviert"
                action = "Aktiviere mit !raid_enable."

            # Nachricht zusammenstellen
            message = f"@{ctx.author.name} Twitch-Bot Status: {status}. {action}"

            if total_raids:
                message += f" | Statistik: {total_raids} Raids ({successful_raids or 0} erfolgreich)"

            if last_raid:
                to_login, viewers, executed_at, success = last_raid
                icon = "✅" if success else "❌"
                time_str = executed_at[:16] if executed_at else "?"
                message += f" | Letzter Raid {icon}: {to_login} ({viewers} Viewer) am {time_str}"

            await ctx.send(message)

        @twitchio_commands.command(name="uban", aliases=["unban"])
        async def cmd_uban(self, ctx: twitchio_commands.Context):
            """!uban / !unban - hebt den letzten Auto-Ban im aktuellen Channel auf."""
            if not (ctx.author.is_broadcaster or ctx.author.is_mod):
                await ctx.send(f"@{ctx.author.name} Nur der Broadcaster oder Mods.")
                return

            channel_name = ctx.channel.name
            streamer_data = self._get_streamer_by_channel(channel_name)
            if not streamer_data:
                await ctx.send(f"@{ctx.author.name} Dieser Kanal ist nicht als Partner registriert.")
                return

            twitch_login, twitch_user_id, _ = streamer_data
            channel_key = self._normalize_channel_login(channel_name)
            last = self._last_autoban.get(channel_key)
            if not last:
                last = self._load_last_autoban_from_log(channel_key)
                if last:
                    self._last_autoban[channel_key] = last
            if not last:
                await ctx.send(f"@{ctx.author.name} Kein Auto-Ban-Eintrag zum Aufheben gefunden.")
                return

            target_user_id = last.get("user_id", "")
            target_login = last.get("login") or target_user_id
            if not target_user_id:
                await ctx.send(f"@{ctx.author.name} Kein Nutzer gespeichert für Unban.")
                return

            success = await self._unban_user(
                broadcaster_id=str(twitch_user_id),
                target_user_id=str(target_user_id),
                channel_name=channel_name,
                login_hint=target_login,
            )
            if success:
                await ctx.send(f"@{ctx.author.name} Unban ausgeführt für {target_login}.")
            else:
                await ctx.send(f"@{ctx.author.name} Unban fehlgeschlagen für {target_login}.")

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

        @twitchio_commands.command(name="clip", aliases=["createclip"])
        async def cmd_clip(self, ctx: twitchio_commands.Context):
            """!clip - Erstellt einen Clip aus dem aktuellen Stream-Buffer und postet den Link."""
            if not (ctx.author.is_broadcaster or ctx.author.is_mod):
                await ctx.send(f"@{ctx.author.name} Nur Broadcaster oder Mods können !clip benutzen.")
                return

            channel_name = ctx.channel.name
            streamer_data = self._get_streamer_by_channel(channel_name)
            if not streamer_data:
                await ctx.send(f"@{ctx.author.name} Dieser Kanal ist nicht als Partner registriert.")
                return

            twitch_login, twitch_user_id, _ = streamer_data

            if not self._raid_bot or not hasattr(self._raid_bot, "auth_manager"):
                await ctx.send(f"@{ctx.author.name} Twitch-Bot nicht verfügbar.")
                return

            auth_manager = self._raid_bot.auth_manager
            api_session = getattr(self._raid_bot, "session", None)
            if not api_session:
                await ctx.send(f"@{ctx.author.name} Twitch-Bot nicht verfügbar (keine API-Session).")
                return

            required_scope = "clips:edit"
            scopes = set(auth_manager.get_scopes(str(twitch_user_id)))
            if required_scope not in scopes:
                auth_url = auth_manager.generate_auth_url(twitch_login)
                await ctx.send(
                    f"@{ctx.author.name} Für !clip fehlt OAuth-Scope clips:edit. "
                    f"Bitte einmal neu autorisieren: {auth_url}"
                )
                return

            try:
                tokens = await auth_manager.get_tokens_for_user(str(twitch_user_id), api_session)
            except Exception:
                log.exception("Clip command: token fetch failed for %s", twitch_login)
                tokens = None

            if not tokens:
                auth_url = auth_manager.generate_auth_url(twitch_login)
                await ctx.send(
                    f"@{ctx.author.name} OAuth fehlt oder ist abgelaufen. "
                    f"Bitte neu autorisieren: {auth_url}"
                )
                return

            access_token, _ = tokens

            try:
                from .twitch_api import TwitchAPI  # lokal importieren, um Zyklus zu vermeiden
                api = TwitchAPI(auth_manager.client_id, auth_manager.client_secret, session=api_session)
                clip = await api.create_clip(
                    str(twitch_user_id),
                    user_token=str(access_token),
                    has_delay=False,
                )
            except Exception:
                log.exception("Clip command failed for %s", twitch_login)
                clip = None

            if not clip:
                await ctx.send(
                    f"@{ctx.author.name} Clip konnte nicht erstellt werden. "
                    "Bitte in 10 Sekunden nochmal versuchen."
                )
                return

            clip_id = str(clip.get("id") or "").strip()
            edit_url = str(clip.get("edit_url") or "").strip()
            clip_url = f"https://clips.twitch.tv/{clip_id}" if clip_id else edit_url
            if not clip_url:
                await ctx.send(
                    f"@{ctx.author.name} Clip wurde angefordert, aber es kam kein Link zurück."
                )
                return

            await ctx.send(f"@{ctx.author.name} 🎬 Clip erstellt (letzte ~Minute): {clip_url}")
            log.info(
                "Clip command successful: %s in #%s (clip_id=%s)",
                twitch_login,
                channel_name,
                clip_id or "-",
            )

        @twitchio_commands.command(name="raid", aliases=["traid"])
        async def cmd_raid(self, ctx: twitchio_commands.Context):
            """!raid / !traid - Startet sofort einen Raid auf den bestmöglichen Partner (wie Auto-Raid)."""
            if not (ctx.author.is_broadcaster or ctx.author.is_mod):
                await ctx.send(f"@{ctx.author.name} Nur Broadcaster oder Mods können !raid benutzen.")
                return

            channel_name = ctx.channel.name
            streamer_data = self._get_streamer_by_channel(channel_name)
            if not streamer_data:
                return

            twitch_login, twitch_user_id, _ = streamer_data

            if not self._raid_bot or not self._raid_bot.auth_manager.has_enabled_auth(twitch_user_id):
                await ctx.send(
                    f"@{ctx.author.name} OAuth fehlt – Anforderung: Twitch-Bot autorisieren mit !raid_enable."
                )
                return

            api_session = getattr(self._raid_bot, "session", None)
            executor = getattr(self._raid_bot, "raid_executor", None)
            if not api_session or not executor:
                await ctx.send(f"@{ctx.author.name} Twitch-Bot nicht verfügbar.")
                return

            # Partner-Kandidaten laden (verifizierte Partner, Opt-out respektieren)
            with get_conn() as conn:
                partners = conn.execute(
                    """
                    SELECT twitch_login, twitch_user_id
                      FROM twitch_streamers
                     WHERE (manual_verified_permanent = 1
                            OR manual_verified_until IS NOT NULL
                            OR manual_verified_at IS NOT NULL)
                       AND manual_partner_opt_out = 0
                       AND twitch_user_id IS NOT NULL
                       AND twitch_login IS NOT NULL
                       AND twitch_user_id != ?
                    """,
                    (twitch_user_id,),
                ).fetchall()

            partner_logins = [str(r[0]).lower() for r in partners]

            # Live-Streams holen
            candidates = []
            api = None
            try:
                from .twitch_api import TwitchAPI  # lokal importieren, um Zyklus zu vermeiden
                api = TwitchAPI(self._raid_bot.auth_manager.client_id, self._raid_bot.auth_manager.client_secret, session=api_session)
                streams = await api.get_streams_by_logins(partner_logins, language=None)
                for stream in streams:
                    user_id = str(stream.get("user_id") or "")
                    user_login = (stream.get("user_login") or "").lower()
                    started_at = stream.get("started_at") or ""
                    candidates.append({
                        "user_id": user_id,
                        "user_login": user_login,
                        "started_at": started_at,
                        "viewer_count": int(stream.get("viewer_count") or 0),
                    })
            except Exception:
                log.exception("Manual raid: konnte Streams nicht abrufen")

            is_partner_raid = True
            target = None

            if candidates:
                # Auswahl nach niedrigsten Viewern wiederverwenden
                target = await self._raid_bot._select_fairest_candidate(candidates, twitch_user_id)  # type: ignore[attr-defined]

            if not target:
                # Fallback auf DE Deadlock-Streamer
                try:
                    if api is None:
                        from .twitch_api import TwitchAPI  # lokal importieren, um Zyklus zu vermeiden
                        api = TwitchAPI(self._raid_bot.auth_manager.client_id, self._raid_bot.auth_manager.client_secret, session=api_session)
                    from .constants import TWITCH_TARGET_GAME_NAME
                    category_id = await api.get_category_id(TWITCH_TARGET_GAME_NAME)
                    if category_id:
                        de_streams = await api.get_streams_by_category(category_id, language="de", limit=50)
                        # Filter out self
                        de_streams = [s for s in de_streams if str(s.get("user_id")) != str(twitch_user_id)]
                        if de_streams:
                            is_partner_raid = False
                            target = await self._raid_bot._select_fairest_candidate(de_streams, twitch_user_id)  # type: ignore[attr-defined]
                            if not target:
                                await ctx.send(f"@{ctx.author.name} Kein geeigneter Fallback-Streamer gefunden.")
                                return
                            # Normalisieren für executor
                            if "user_login" not in target and "user_name" in target:
                                target["user_login"] = target["user_name"].lower()
                        else:
                            await ctx.send(f"@{ctx.author.name} Weder Partner noch andere deutsche Deadlock-Streamer live.")
                            return
                    else:
                        await ctx.send(f"@{ctx.author.name} Kein Partner live (Kategorie-ID nicht gefunden).")
                        return
                except Exception:
                    log.exception("Manual raid fallback failed")
                    await ctx.send(f"@{ctx.author.name} Kein Partner live und Fallback fehlgeschlagen.")
                    return

            target_id = target.get("user_id") or ""
            target_login = target.get("user_login") or ""
            target_started_at = target.get("started_at", "")
            viewer_count = int(target.get("viewer_count") or 0)

            # Streamdauer best-effort
            stream_duration_sec = 0
            try:
                if target_started_at:
                    started_dt = datetime.fromisoformat(target_started_at.replace("Z", "+00:00"))
                    stream_duration_sec = int((datetime.now(timezone.utc) - started_dt).total_seconds())
            except Exception as exc:
                log.debug("Konnte Stream-Dauer nicht berechnen für %s", target_login, exc_info=exc)

            try:
                success, error = await executor.start_raid(
                    from_broadcaster_id=twitch_user_id,
                    from_broadcaster_login=twitch_login,
                    to_broadcaster_id=target_id,
                    to_broadcaster_login=target_login,
                    viewer_count=viewer_count,
                    stream_duration_sec=stream_duration_sec,
                    target_stream_started_at=target_started_at,
                    candidates_count=len(candidates) if is_partner_raid else 0,
                    reason="manual_chat_command",
                    session=api_session,
                )
            except Exception as exc:
                log.exception("Manual raid failed for %s -> %s", twitch_login, target_login)
                await ctx.send(f"@{ctx.author.name} Raid fehlgeschlagen: {exc}")
                return

            if success:
                if hasattr(self._raid_bot, "mark_manual_raid_started"):
                    try:
                        self._raid_bot.mark_manual_raid_started(
                            broadcaster_id=str(twitch_user_id),
                            ttl_seconds=300.0,
                        )
                    except Exception:
                        log.debug("Konnte Manual-Raid-Suppression nicht setzen für %s", twitch_login, exc_info=True)

                await ctx.send(f"@{ctx.author.name} Raid auf {target_login} gestartet! (Twitch-Countdown ~90s)")

                # Pending Raid registrieren (Nachricht wird erst nach EventSub gesendet)
                # Funktioniert für Partner-Raids UND Non-Partner-Raids
                if hasattr(self._raid_bot, "_register_pending_raid"):
                    await self._raid_bot._register_pending_raid(
                        from_broadcaster_login=twitch_login,
                        to_broadcaster_id=target_id,
                        to_broadcaster_login=target_login,
                        target_stream_data=target,
                        is_partner_raid=is_partner_raid,
                        viewer_count=viewer_count,
                    )
            else:
                await ctx.send(f"@{ctx.author.name} Raid fehlgeschlagen: {error or 'unbekannter Fehler'}")
else:
    class RaidCommandsMixin:
        pass
