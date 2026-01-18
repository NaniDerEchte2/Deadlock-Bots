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
import os
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Optional, Set, Tuple

try:
    import twitchio
    from twitchio import eventsub
    from twitchio import web as twitchio_web
    from twitchio.ext import commands as twitchio_commands
    TWITCHIO_AVAILABLE = True
except ImportError:
    TWITCHIO_AVAILABLE = False
    log = logging.getLogger("TwitchStreams.ChatBot")
    log.warning(
        "twitchio nicht installiert. Twitch Chat Bot wird nicht verfügbar sein. "
        "Installation: pip install twitchio"
    )

from .storage import get_conn
from .token_manager import TwitchBotTokenManager

log = logging.getLogger("TwitchStreams.ChatBot")
_KEYRING_SERVICE = "DeadlockBot"


if TWITCHIO_AVAILABLE:
    class RaidChatBot(twitchio_commands.Bot):
        """Twitch IRC Bot für Raid-Commands im Chat."""

        def __init__(
            self,
            token: str,
            client_id: str,
            client_secret: str,
            bot_id: Optional[str] = None,
            prefix: str = "!",
            initial_channels: Optional[list] = None,
            refresh_token: Optional[str] = None,
            web_adapter: Optional[object] = None,
            token_manager: Optional[TwitchBotTokenManager] = None,
        ):
            # In 3.x ist bot_id ein positionales/keyword Argument in Client, aber REQUIRED in Bot
            base_kwargs = {"adapter": web_adapter} if web_adapter is not None else {}
            super().__init__(
                client_id=client_id,
                client_secret=client_secret,
                bot_id=bot_id or "", # Fallback auf leeren String falls None
                prefix=prefix,
                **base_kwargs,
            )
            self._bot_token = token
            self._bot_refresh_token = refresh_token
            self._token_manager = token_manager
            if self._token_manager:
                self._token_manager.set_refresh_callback(self._on_token_manager_refresh)
            self._raid_bot = None  # Wird später gesetzt
            self._initial_channels = initial_channels or []
            self._monitored_streamers: Set[str] = set()
            self._session_cache: Dict[str, Tuple[int, datetime]] = {}
            log.info("Twitch Chat Bot initialized with %d initial channels", len(self._initial_channels))

        def set_raid_bot(self, raid_bot):
            """Setzt die RaidBot-Instanz für OAuth-URLs."""
            self._raid_bot = raid_bot

        async def setup_hook(self):
            """Wird beim Starten aufgerufen, um initiales Setup zu machen."""
            # Token registrieren, damit TwitchIO ihn nutzt
            try:
                if self._token_manager:
                    access_token, bot_id = await self._token_manager.get_valid_token()
                    if access_token:
                        self._bot_token = access_token
                    # bot_id wird bereits im __init__ oder via add_token gehandelt
                    self._bot_refresh_token = self._token_manager.refresh_token or self._bot_refresh_token

                api_token = (self._bot_token or "").replace("oauth:", "").strip()
                if api_token:
                    # Wir fügen den Token hinzu. Refresh-Token ist bei TMI-Tokens meist nicht vorhanden (None).
                    # ABER: Wenn wir einen haben (aus ENV/Tresor), übergeben wir ihn, damit TwitchIO refreshen kann.
                    await self.add_token(api_token, self._bot_refresh_token)
                    log.info("Bot user token added (Refresh-Token: %s).", "Yes" if self._bot_refresh_token else "No")
                    await self._persist_bot_tokens(
                        access_token=self._bot_token,
                        refresh_token=self._bot_refresh_token,
                        expires_in=None,
                        scopes=None,
                        user_id=self.bot_id,
                    )
                else:
                    log.warning("Kein gültiger TWITCH_BOT_TOKEN gefunden.")
            except Exception as e:
                log.error(
                    "Der TWITCH_BOT_TOKEN ist ungültig oder abgelaufen. "
                    "Bitte führe den OAuth-Flow für den Bot aus (Client-ID/Secret + Redirect), "
                    "um Access- und Refresh-Token zu erhalten. Fehler: %s",
                    e,
                )
                # Wir machen weiter, damit der Bot zumindest "ready" wird und andere Cogs nicht blockiert
            
            # Initial channels beitreten
            if self._initial_channels:
                log.info("Joining %d initial channels...", len(self._initial_channels))
                for channel in self._initial_channels:
                    try:
                        await self.join(channel)
                    except Exception as e:
                        log.debug("Konnte initialem Channel %s nicht beitreten: %s", channel, e)

        async def event_ready(self):
            """Wird aufgerufen, wenn der Bot verbunden ist."""
            name = self.user.name if self.user else "Unknown"
            log.info("Twitch Chat Bot ready | Logged in as: %s", name)
            # In 3.x gibt es connected_channels nicht mehr so einfach
            monitored = ", ".join(list(self._monitored_streamers)[:10])
            log.info("Monitored streamers: %s", monitored)

        async def event_token_refreshed(self, payload):
            """Persistiert erneuerte Bot-Tokens, sobald TwitchIO sie refreshed."""
            try:
                # Wir speichern die ID intern, falls wir sie brauchen, 
                # aber vermeiden das Setzen der read-only Property bot_id
                if payload.user_id:
                    pass 
                if self.bot_id and str(payload.user_id) != str(self.bot_id):
                    return  # Nur den Bot-Token persistieren, nicht Streamer-Tokens
                self._bot_token = f"oauth:{payload.token}" if not payload.token.startswith("oauth:") else payload.token
                self._bot_refresh_token = payload.refresh_token
            except Exception:
                return
            try:
                await self._persist_bot_tokens(
                    access_token=self._bot_token or payload.token,
                    refresh_token=self._bot_refresh_token or payload.refresh_token,
                    expires_in=payload.expires_in,
                    scopes=list(payload.scopes.selected),
                    user_id=payload.user_id,
                )
            except Exception:
                log.debug("Konnte refreshed Bot-Token nicht persistieren", exc_info=True)

        async def _on_token_manager_refresh(
            self,
            access_token: str,
            refresh_token: Optional[str],
            _expires_at: Optional[datetime],
        ) -> None:
            """Registriert neue Tokens aus dem Token Manager und updated TwitchIO."""
            self._bot_token = access_token
            self._bot_refresh_token = refresh_token
            api_token = (access_token or "").replace("oauth:", "").strip()
            if not api_token:
                return
            try:
                await self.add_token(api_token, refresh_token)
            except Exception:
                log.debug("Konnte refreshed Bot-Token nicht in TwitchIO registrieren", exc_info=True)

        async def join(self, channel_login: str, channel_id: Optional[str] = None):
            """Joint einen Channel via EventSub (TwitchIO 3.x)."""
            try:
                if not channel_id:
                    user = await self.fetch_user(login=channel_login.lstrip("#"))
                    if not user:
                        log.error("Could not find user ID for channel %s", channel_login)
                        return
                    channel_id = str(user.id)

                # STRATEGIE: Wir versuchen, den Token des Streamers zu nutzen.
                # Wenn wir den Token haben, abonnieren wir als "Streamer hört Streamer".
                # Das umgeht die Notwendigkeit, dass der Bot Moderator sein muss.
                use_streamer_auth = False
                
                if self._raid_bot:
                    # Session holen (TwitchIO intern)
                    http_session = self._http._session
                    tokens = await self._raid_bot.auth_manager.get_tokens_for_user(channel_id, http_session)
                    scopes = self._raid_bot.auth_manager.get_scopes(channel_id) if hasattr(self._raid_bot, "auth_manager") else []
                    scopes_lower = [s.lower() for s in scopes]
                    has_chat_scope = any(s in {"user:read:chat", "user:write:chat", "chat:read", "chat:edit"} for s in scopes_lower)

                    if tokens and has_chat_scope:
                        access_token, refresh_token = tokens
                        # Token im Client registrieren, damit 'token_for' funktioniert
                        await self.add_token(access_token, refresh_token)
                        use_streamer_auth = True
                        log.info("Using streamer auth for channel %s (No Mod required)", channel_login)
                    elif tokens and not has_chat_scope:
                        log.info("Streamer auth fehlt Chat-Scope für %s -> kein Join", channel_login)
                        return False

                if use_streamer_auth:
                    # User ID = Broadcaster ID -> Streamer hört sich selbst zu
                    payload = eventsub.ChatMessageSubscription(
                        broadcaster_user_id=str(channel_id), 
                        user_id=str(channel_id)
                    )
                    # Wir zwingen die Nutzung des Streamer-Tokens
                    await self.subscribe_websocket(payload=payload, token_for=str(channel_id))
                else:
                    # Fallback: Bot hört zu (braucht Mod-Rechte)
                    payload = eventsub.ChatMessageSubscription(
                        broadcaster_user_id=str(channel_id), 
                        user_id=str(self.bot_id)
                    )
                    await self.subscribe_websocket(payload=payload)

                self._monitored_streamers.add(channel_login.lower().lstrip("#"))
                return True
            except Exception as e:
                msg = str(e)
                if "403" in msg and "subscription missing proper authorization" in msg:
                    log.warning(
                        "Cannot join chat for %s. Reasons: Missing 'user:read:chat' scope (re-auth needed) "
                        "OR Bot is not a Moderator in the channel (please /mod bot).",
                        channel_login
                    )
                else:
                    log.error("Failed to join channel %s: %s", channel_login, e)
                return False

        async def event_message(self, message):
            """Wird bei jeder Chat-Nachricht aufgerufen."""
            # Compatibility layer for TwitchIO 3.x
            if not hasattr(message, "echo"):
                # In EventSub messages, we check if the chatter is the bot
                message.echo = str(message.chatter.id) == str(self.bot_id)
            
            if not hasattr(message, "content"):
                message.content = message.text
            
            if not hasattr(message, "author"):
                message.author = message.chatter
                
            if not hasattr(message, "channel"):
                # Mock channel object for backward compatibility
                class MockChannel:
                    def __init__(self, login):
                        self.name = login
                    def __str__(self):
                        return self.name
                message.channel = MockChannel(message.broadcaster.login)

            # Ignoriere Bot-Nachrichten
            if message.echo:
                return

            try:
                await self._track_chat_health(message)
            except Exception:
                log.debug("Konnte Chat-Health nicht loggen", exc_info=True)

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

        def _resolve_session_id(self, login: str) -> Optional[int]:
            """Best-effort Mapping von Channel zu offener Twitch-Session."""
            cache_key = login.lower()
            cached = self._session_cache.get(cache_key)
            now_ts = datetime.now(timezone.utc)
            if cached:
                cached_id, cached_at = cached
                if (now_ts - cached_at).total_seconds() < 60:
                    return cached_id

            with get_conn() as conn:
                row = conn.execute(
                    """
                    SELECT id FROM twitch_stream_sessions
                     WHERE streamer_login = ? AND ended_at IS NULL
                     ORDER BY started_at DESC
                     LIMIT 1
                    """,
                    (cache_key,),
                ).fetchone()
            if not row:
                return None

            session_id = int(row["id"] if hasattr(row, "keys") else row[0])
            self._session_cache[cache_key] = (session_id, now_ts)
            return session_id

        async def _track_chat_health(self, message) -> None:
            """Loggt Chat-Events für Chat-Gesundheit und Retention-Metriken."""
            channel_name = getattr(message.channel, "name", "") or ""
            login = channel_name.lstrip("#").lower()
            if not login:
                return

            author = getattr(message, "author", None)
            chatter_login = (getattr(author, "name", "") or "").lower()
            if not chatter_login:
                return
            chatter_id = str(getattr(author, "id", "") or "") or None
            content = message.content or ""
            is_command = content.strip().startswith(self.prefix or "!")

            session_id = self._resolve_session_id(login)
            if session_id is None:
                return

            ts_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

            with get_conn() as conn:
                # Rohes Chat-Event (ohne Nachrichtentext)
                conn.execute(
                    """
                    INSERT INTO twitch_chat_messages (session_id, streamer_login, chatter_login, message_ts, is_command)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (session_id, login, chatter_login, ts_iso, 1 if is_command else 0),
                )

                # Rollup pro Session
                existing = conn.execute(
                    """
                    SELECT messages, is_first_time_global
                      FROM twitch_session_chatters
                     WHERE session_id = ? AND chatter_login = ?
                    """,
                    (session_id, chatter_login),
                ).fetchone()

                rollup = conn.execute(
                    """
                    SELECT total_messages, total_sessions
                      FROM twitch_chatter_rollup
                     WHERE streamer_login = ? AND chatter_login = ?
                    """,
                    (login, chatter_login),
                ).fetchone()

                is_first_global = 0 if rollup else 1
                if rollup:
                    total_sessions_inc = 1 if existing is None else 0
                    conn.execute(
                        """
                        UPDATE twitch_chatter_rollup
                           SET total_messages = total_messages + 1,
                               total_sessions = total_sessions + ?,
                               last_seen_at = ?,
                               chatter_id = COALESCE(chatter_id, ?)
                         WHERE streamer_login = ? AND chatter_login = ?
                        """,
                        (total_sessions_inc, ts_iso, chatter_id, login, chatter_login),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO twitch_chatter_rollup (
                            streamer_login, chatter_login, chatter_id, first_seen_at, last_seen_at,
                            total_messages, total_sessions
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (login, chatter_login, chatter_id, ts_iso, ts_iso, 1, 1),
                    )

                if existing:
                    conn.execute(
                        """
                        UPDATE twitch_session_chatters
                           SET messages = messages + 1
                         WHERE session_id = ? AND chatter_login = ?
                        """,
                        (session_id, chatter_login),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO twitch_session_chatters (
                            session_id, streamer_login, chatter_login, chatter_id, first_message_at,
                            messages, is_first_time_global
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            session_id,
                            login,
                            chatter_login,
                            chatter_id,
                            ts_iso,
                            1,
                            is_first_global,
                        ),
                    )

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

        @twitchio_commands.command(name="raid")
        async def cmd_raid(self, ctx: twitchio_commands.Context):
            """!raid - Startet sofort einen Raid auf den bestmöglichen Partner (wie Auto-Raid)."""
            if not (ctx.author.is_broadcaster or ctx.author.is_mod):
                await ctx.send(f"@{ctx.author.name} Nur Broadcaster oder Mods können !raid benutzen.")
                return

            channel_name = ctx.channel.name
            streamer_data = self._get_streamer_by_channel(channel_name)
            if not streamer_data:
                return

            twitch_login, twitch_user_id, _ = streamer_data

            if not self._raid_bot or not self._raid_bot.auth_manager.has_enabled_auth(twitch_user_id):
                await ctx.send(f"@{ctx.author.name} Bitte zuerst autorisieren/aktivieren: !raid_enable")
                return

            api_session = getattr(self._raid_bot, "session", None)
            executor = getattr(self._raid_bot, "raid_executor", None)
            if not api_session or not executor:
                await ctx.send(f"@{ctx.author.name} Raid-Bot nicht verfügbar.")
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

            if not candidates:
                await ctx.send(f"@{ctx.author.name} Kein passender Partner gerade live. Versuch es später erneut.")
                return

            # Fairness-Auswahl wiederverwenden
            target = self._raid_bot._select_fairest_candidate(candidates, broadcaster_id=twitch_user_id)  # type: ignore[attr-defined]
            if not target:
                await ctx.send(f"@{ctx.author.name} Kein Ziel gefunden.")
                return

            target_id = target.get("user_id") or ""
            target_login = target.get("user_login") or ""
            target_started_at = target.get("started_at", "")
            viewer_count = int(target.get("viewer_count") or 0)

            # Streamdauer best-effort
            stream_duration_sec = 0
            try:
                if target_started_at:
                    from datetime import datetime, timezone
                    started_dt = datetime.fromisoformat(target_started_at.replace("Z", "+00:00"))
                    stream_duration_sec = int((datetime.now(timezone.utc) - started_dt).total_seconds())
            except Exception:
                pass

            try:
                success, error = await executor.start_raid(
                    from_broadcaster_id=twitch_user_id,
                    from_broadcaster_login=twitch_login,
                    to_broadcaster_id=target_id,
                    to_broadcaster_login=target_login,
                    viewer_count=viewer_count,
                    stream_duration_sec=stream_duration_sec,
                    target_stream_started_at=target_started_at,
                    candidates_count=len(candidates),
                    session=api_session,
                )
            except Exception as exc:
                log.exception("Manual raid failed for %s -> %s", twitch_login, target_login)
                await ctx.send(f"@{ctx.author.name} Raid fehlgeschlagen: {exc}")
                return

            if success:
                await ctx.send(f"@{ctx.author.name} Raid auf {target_login} gestartet! (Twitch-Countdown ~90s)")
            else:
                await ctx.send(f"@{ctx.author.name} Raid fehlgeschlagen: {error or 'unbekannter Fehler'}")

        async def _persist_bot_tokens(
            self,
            *,
            access_token: str,
            refresh_token: Optional[str],
            expires_in: Optional[int],
            scopes: Optional[list] = None,
            user_id: Optional[str] = None,
        ) -> None:
            """Persist bot tokens in Windows Credential Manager (keyring)."""
            if not access_token:
                return

            if self._token_manager:
                self._token_manager.access_token = access_token
                if refresh_token:
                    self._token_manager.refresh_token = refresh_token
                if user_id:
                    self._token_manager.bot_id = str(user_id)
                if expires_in:
                    self._token_manager.expires_at = datetime.now() + timedelta(seconds=int(expires_in))
                await self._token_manager._save_tokens()
                return

            await _save_bot_tokens_to_keyring(
                access_token=access_token,
                refresh_token=refresh_token,
            )

        async def join_partner_channels(self):
            """Joint alle Partner-Channels."""
            with get_conn() as conn:
                partners = conn.execute(
                    """
                    SELECT DISTINCT s.twitch_login, s.twitch_user_id
                    FROM twitch_streamers s
                    JOIN twitch_raid_auth a ON s.twitch_user_id = a.twitch_user_id
                    WHERE (s.manual_verified_permanent = 1
                           OR s.manual_verified_until IS NOT NULL
                           OR s.manual_verified_at IS NOT NULL)
                      AND s.manual_partner_opt_out = 0
                      AND a.raid_enabled = 1
                    """
                ).fetchall()

            channels_to_join = [(row[0], row[1]) for row in partners if row[0]]
            new_channels = [(login, uid) for login, uid in channels_to_join if login.lower() not in self._monitored_streamers]

            if new_channels:
                log.info("Joining %d new partner channels: %s", len(new_channels), ", ".join([c[0] for c in new_channels[:10]]))
                for login, uid in new_channels:
                    try:
                        # Wir übergeben ID falls vorhanden, sonst wird sie in join() gefetched
                        success = await self.join(login, channel_id=uid)
                        if success:
                            await asyncio.sleep(0.2)  # Rate limiting
                    except Exception as e:
                        log.exception("Unexpected error joining channel %s: %s", login, e)


def _read_keyring_secret(key: str) -> Optional[str]:
    """Read a secret from Windows Credential Manager."""
    try:
        import keyring  # type: ignore
    except Exception:
        return None

    for service in (_KEYRING_SERVICE, f"{key}@{_KEYRING_SERVICE}"):
        try:
            val = keyring.get_password(service, key)
            if val:
                return val
        except Exception:
            continue
    return None


async def _save_bot_tokens_to_keyring(*, access_token: str, refresh_token: Optional[str]) -> None:
    """Persist access/refresh tokens to Windows Credential Manager."""
    try:
        import keyring  # type: ignore
    except Exception:
        log.debug("keyring nicht verfügbar – Tokens können nicht persistiert werden.")
        return

    async def _save_one(service: str, name: str, value: str) -> None:
        await asyncio.to_thread(keyring.set_password, service, name, value)

    tasks = []
    if access_token:
        tasks.append(_save_one(_KEYRING_SERVICE, "TWITCH_BOT_TOKEN", access_token))
        tasks.append(_save_one(f"TWITCH_BOT_TOKEN@{_KEYRING_SERVICE}", "TWITCH_BOT_TOKEN", access_token))
    if refresh_token:
        tasks.append(_save_one(_KEYRING_SERVICE, "TWITCH_BOT_REFRESH_TOKEN", refresh_token))
        tasks.append(_save_one(f"TWITCH_BOT_REFRESH_TOKEN@{_KEYRING_SERVICE}", "TWITCH_BOT_REFRESH_TOKEN", refresh_token))

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
        log.info("Twitch Bot Token im Windows Credential Manager gespeichert.")


def load_bot_tokens(*, log_missing: bool = True) -> Tuple[Optional[str], Optional[str], Optional[int]]:
    """
    Load the Twitch bot OAuth token and refresh token from env/file/Windows keyring.

    Returns:
        (access_token, refresh_token, expiry_ts_utc)
    """
    raw_env = os.getenv("TWITCH_BOT_TOKEN", "") or ""
    raw_refresh = os.getenv("TWITCH_BOT_REFRESH_TOKEN", "") or ""
    token = raw_env.strip()
    refresh = raw_refresh.strip() or None
    expiry_ts: Optional[int] = None

    if token:
        return token, refresh, expiry_ts

    token_file = (os.getenv("TWITCH_BOT_TOKEN_FILE") or "").strip()
    if token_file:
        try:
            candidate = Path(token_file).read_text(encoding="utf-8").strip()
            if candidate:
                return candidate, refresh, expiry_ts
            if log_missing:
                log.warning("TWITCH_BOT_TOKEN_FILE gesetzt (%s), aber leer", token_file)
        except Exception as exc:  # pragma: no cover - defensive logging
            if log_missing:
                log.warning("TWITCH_BOT_TOKEN_FILE konnte nicht gelesen werden (%s): %s", token_file, exc)

    keyring_token = _read_keyring_secret("TWITCH_BOT_TOKEN")
    keyring_refresh = _read_keyring_secret("TWITCH_BOT_REFRESH_TOKEN")
    if keyring_token:
        return keyring_token, keyring_refresh or refresh, expiry_ts

    if log_missing:
        log.warning(
            "TWITCH_BOT_TOKEN nicht gesetzt. Twitch Chat Bot wird nicht gestartet. "
            "Bitte setze ein OAuth-Token für den Bot-Account."
        )
    return None, None, None


def load_bot_token(*, log_missing: bool = True) -> Optional[str]:
    token, _, _ = load_bot_tokens(log_missing=log_missing)
    return token


if not TWITCHIO_AVAILABLE:
    class RaidChatBot:  # type: ignore[redefined-outer-name]
        """Stub, damit Import-Caller nicht crashen, wenn twitchio fehlt."""
        pass

async def create_twitch_chat_bot(
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    raid_bot = None,
    bot_token: Optional[str] = None,
    bot_refresh_token: Optional[str] = None,
    log_missing: bool = True,
    token_manager: Optional[TwitchBotTokenManager] = None,
) -> Optional[RaidChatBot]:
    """
    Erstellt einen Twitch Chat Bot mit Bot-Account-Token.

    Env-Variablen:
    - TWITCH_BOT_TOKEN: OAuth-Token für den Bot-Account
    """
    if not TWITCHIO_AVAILABLE:
        log.warning(
            "TwitchIO nicht installiert – Twitch Chat Bot wird übersprungen. "
            "Installation optional: pip install twitchio"
        )
        return None

    """
    Env-Variablen:
    - TWITCH_BOT_TOKEN: OAuth-Token für den Bot-Account
    - TWITCH_BOT_TOKEN_FILE: Optionaler Dateipfad, der das OAuth-Token enthaelt
    - TWITCH_BOT_NAME: Name des Bot-Accounts (optional)
    """
    if not TWITCHIO_AVAILABLE:
        log.warning(
            "TwitchIO nicht installiert – Twitch Chat Bot wird übersprungen. "
            "Installation optional: pip install twitchio"
        )
        return None

    token = bot_token
    refresh_token = bot_refresh_token

    if not token:
        token, refresh_from_store, _ = load_bot_tokens(log_missing=log_missing)
        refresh_token = refresh_token or refresh_from_store
    else:
        _, refresh_from_store, _ = load_bot_tokens(log_missing=False)
        refresh_token = refresh_token or refresh_from_store

    if not token:
        return None

    token_mgr = token_manager
    token_mgr_created = False
    if token_mgr is None and client_id:
        token_mgr = TwitchBotTokenManager(client_id, client_secret or "", keyring_service=_KEYRING_SERVICE)
        token_mgr_created = True

    bot_id = None
    if token_mgr:
        initialised = await token_mgr.initialize(access_token=token, refresh_token=refresh_token)
        if not initialised:
            log.error("Twitch Bot Token Manager konnte nicht initialisiert werden (kein Refresh-Token?).")
            if token_mgr_created:
                await token_mgr.cleanup()
            return None
        token = token_mgr.access_token or token
        refresh_token = token_mgr.refresh_token or refresh_token
        bot_id = token_mgr.bot_id

    # Partner-Channels abrufen (nur wenn Raid-Auth + Chat-Scopes + aktuell live)
    with get_conn() as conn:
        partners = conn.execute(
            """
            SELECT DISTINCT s.twitch_login, s.twitch_user_id, a.scopes, l.is_live
              FROM twitch_streamers s
              JOIN twitch_raid_auth a ON s.twitch_user_id = a.twitch_user_id
              LEFT JOIN twitch_live_state l ON s.twitch_user_id = l.twitch_user_id
             WHERE (s.manual_verified_permanent = 1
                    OR s.manual_verified_until IS NOT NULL
                    OR s.manual_verified_at IS NOT NULL)
               AND s.manual_partner_opt_out = 0
               AND a.raid_enabled = 1
            """
        ).fetchall()

    initial_channels = []
    for login, user_id, scopes_raw, is_live in partners:
        scopes = [s.strip().lower() for s in (scopes_raw or "").split() if s.strip()]
        has_chat_scope = any(
            s in {"user:read:chat", "user:write:chat", "chat:read", "chat:edit"} for s in scopes
        )
        if not has_chat_scope:
            continue
        if is_live is not None and not bool(is_live):
            # Nur live Channels joinen, um unnötige Joins zu vermeiden
            continue
        initial_channels.append(login)

    log.info("Creating Twitch Chat Bot for %d partner channels (live + chat scope)", len(initial_channels))

    # Bot-ID via API abrufen (TwitchIO braucht diese zwingend bei user:bot Scope)
    if bot_id is None:
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                api_token = token.replace("oauth:", "")
                
                # 1. Versuch: id.twitch.tv/oauth2/validate (oft am tolerantesten für User-IDs)
                # Wir probieren beide Header-Varianten
                for auth_header in [f"OAuth {api_token}", f"Bearer {api_token}"]:
                    async with session.get("https://id.twitch.tv/oauth2/validate", headers={"Authorization": auth_header}) as r:
                        if r.status == 200:
                            val_data = await r.json()
                            bot_id = val_data.get("user_id")
                            if bot_id:
                                log.info("Validated Bot ID: %s", bot_id)
                                break
                    
                # 2. Versuch: Helix users (falls validate fehlschlug)
                if not bot_id:
                    headers = {
                        "Client-ID": client_id,
                        "Authorization": f"Bearer {api_token}"
                    }
                    async with session.get("https://api.twitch.tv/helix/users", headers=headers) as r:
                        if r.status == 200:
                            data = await r.json()
                            if data.get("data"):
                                bot_id = data["data"][0]["id"]
                                log.info("Fetched Bot ID via Helix: %s", bot_id)
                        elif r.status == 401:
                            log.warning("Twitch API 401 Unauthorized: Der TWITCH_BOT_TOKEN scheint ungültig zu sein.")
                        else:
                            log.warning("Could not fetch Bot ID: HTTP %s", r.status)
        except Exception as e:
            log.warning("Failed to fetch Bot ID: %s", e)

    # Fallback: Wenn Fetch fehlschlägt, aber Token existiert, versuchen wir es ohne ID (könnte failen)
    # oder übergeben einen Dummy, falls TwitchIO das schluckt.
    # Besser: Wir übergeben was wir haben.

    adapter_host = (os.getenv("TWITCH_CHAT_ADAPTER_HOST") or "").strip()
    adapter_port_raw = (os.getenv("TWITCH_CHAT_ADAPTER_PORT") or "").strip()
    adapter_port = None
    if adapter_port_raw:
        try:
            adapter_port = int(adapter_port_raw)
        except ValueError:
            log.warning(
                "TWITCH_CHAT_ADAPTER_PORT '%s' ist ungueltig - es wird der Standardport 4343 genutzt",
                adapter_port_raw,
            )
            adapter_port = None

    web_adapter = None
    if adapter_host or adapter_port_raw:
        web_adapter = twitchio_web.AiohttpAdapter(
            host=adapter_host or None,
            port=adapter_port,
        )

    bot = RaidChatBot(
        token=token,
        client_id=client_id,
        client_secret=client_secret,
        bot_id=bot_id,
        prefix="!",
        initial_channels=initial_channels,
        refresh_token=refresh_token,
        web_adapter=web_adapter,
        token_manager=token_mgr,
    )
    bot.set_raid_bot(raid_bot)

    return bot
