
# cogs/twitch/twitch_chat_bot.py
"""
Twitch IRC Chat Bot für Twitch-Bot-Steuerung.

Streamer können den Twitch-Bot direkt über Twitch-Chat-Commands steuern:
- !raid_enable / !raidbot - Aktiviert Auto-Raids
- !raid_disable / !raidbot_off - Deaktiviert Auto-Raids
- !raid_status - Zeigt den Status an
- !raid_history - Zeigt die letzten Raids
- !clip - Erstellt einen Clip und postet den Link
"""
import asyncio
import copy
import logging
import logging.handlers
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Deque, Dict, Optional, Set, Tuple

import discord

from .constants import TWITCH_NOTIFY_CHANNEL_ID, TWITCH_TARGET_GAME_NAME
from .storage import get_conn
from .token_manager import TwitchBotTokenManager
from .twitch_chat_bot_commands import RaidCommandsMixin
from .twitch_chat_bot_connection import ConnectionMixin
from .twitch_chat_bot_constants import (
    _PROMO_ACTIVITY_ENABLED,
    PROMO_LOOP_INTERVAL_SEC,
    PROMO_MESSAGES,
    PROMO_VIEWER_SPIKE_ENABLED,
    SPAM_MIN_MATCHES,
    WHITELISTED_BOTS,
)
from .twitch_chat_bot_deps import (
    TWITCHIO_AVAILABLE,
    twitchio_commands,
    twitchio_web,
)
from .twitch_chat_bot_moderation import ModerationMixin
from .twitch_chat_bot_promos import PromoMixin
from .twitch_chat_bot_tokens import (
    _KEYRING_SERVICE,
    TokenPersistenceMixin,
    load_bot_tokens,
)


# Dedizierter Twitch-Logger Setup
def _setup_twitch_logging():
    twitch_log = logging.getLogger("TwitchStreams")
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)

    file_path = log_dir / "twitch_bot.log"
    # Prüfe ob Handler bereits existiert (um Duplikate bei Reloads zu vermeiden)
    exists = False
    for h in twitch_log.handlers:
        if isinstance(h, logging.handlers.RotatingFileHandler) and str(h.baseFilename).endswith("twitch_bot.log"):
            exists = True
            break

    if not exists:
        handler = logging.handlers.RotatingFileHandler(
            file_path,
            maxBytes=5 * 1024 * 1024,  # 5MB
            backupCount=5,
            encoding="utf-8"
        )
        formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        handler.setFormatter(formatter)
        twitch_log.addHandler(handler)
        twitch_log.propagate = True  # Auch weiterhin im Master-Log behalten


_setup_twitch_logging()
log = logging.getLogger("TwitchStreams.ChatBot")


if TWITCHIO_AVAILABLE:
    class RaidChatBot(
        TokenPersistenceMixin,
        ModerationMixin,
        PromoMixin,
        ConnectionMixin,
        RaidCommandsMixin,
        twitchio_commands.Bot,
    ):
        """Twitch IRC Bot für Raid-Commands im Chat."""

        # TwitchIO 3.x Component compatibility: guards list expected by Command._run_guards
        __all_guards__: list = []

        async def component_before_invoke(self, ctx) -> None:
            """TwitchIO 3.x Component hook stub – required when _injected is set on commands."""
            pass

        async def component_command_error(self, payload) -> None:
            """TwitchIO 3.x Component hook stub – required when _injected is set on commands."""
            pass

        async def component_after_invoke(self, ctx) -> None:
            """TwitchIO 3.x Component hook stub – required when _injected is set on commands."""
            pass

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
            # Speichere bot_id als Instanzvariable BEVOR wir super().__init__ aufrufen
            self._bot_id_stored = bot_id
            super().__init__(
                client_id=client_id,
                client_secret=client_secret,
                bot_id=bot_id or "",  # Fallback auf leeren String falls None (für TwitchIO Kompatibilität)
                prefix=prefix,
                **base_kwargs,
            )
            self.prefix = prefix
            self._client_id = client_id
            self._bot_token = token
            self._bot_refresh_token = refresh_token
            self._token_manager = token_manager
            if self._token_manager:
                self._token_manager.set_refresh_callback(self._on_token_manager_refresh)
            self._raid_bot = None  # Wird später gesetzt
            self._initial_channels = initial_channels or []
            self._monitored_streamers: Set[str] = set()
            self._session_cache: Dict[str, Tuple[int, datetime]] = {}
            self._last_autoban: Dict[str, Dict[str, str]] = {}
            # Cooldown: Verhindert, dass _ensure_bot_is_mod auf einem
            # gebannten Channel sekundlich wiederholt wird.
            # Key = channel_login (lowercase), Value = nächster erlaubter Zeitpunkt.
            self._mod_retry_cooldown: Dict[str, datetime] = {}
            self._autoban_log = Path("logs") / "twitch_autobans.log"
            self._target_game_lower = (TWITCH_TARGET_GAME_NAME or "").strip().lower()
            # Cache for category checks in chat tracking (login -> (monotonic_ts, is_target_game))
            self._chat_category_cache: Dict[str, Tuple[float, bool]] = {}
            self._chat_category_cache_ttl_sec = 15.0
            # Periodische Chat-Promos
            self._channel_ids: Dict[str, str] = {}          # login -> broadcaster_id
            self._last_promo_sent: Dict[str, float] = {}    # login -> monotonic timestamp
            self._last_promo_attempt: Dict[str, float] = {} # login -> monotonic timestamp
            self._last_raw_chat_message_ts: Dict[str, float] = {}
            self._raw_msg_count_since_promo: Dict[str, int] = {}
            self._promo_activity: Dict[str, Deque[Tuple[float, str]]] = {}
            self._promo_chatter_dedupe: Dict[str, Dict[str, float]] = {}
            self._last_promo_viewer_spike: Dict[str, float] = {}
            self._promo_task: Optional[asyncio.Task] = None
            self._last_invite_reply: Dict[str, float] = {}
            self._last_invite_reply_user: Dict[Tuple[str, str], float] = {}
            self._discord_bot: Optional[discord.Client] = None
            self._discord_invite_channel_id: Optional[int] = None
            self._promo_invite_cache: Dict[str, str] = {}
            self._register_inline_commands()
            log.info("Twitch Chat Bot initialized with %d initial channels", len(self._initial_channels))

        def _register_inline_commands(self) -> None:
            """Register @command methods on the Bot class (TwitchIO 3.x does not auto-register)."""
            for cls in self.__class__.mro():
                for _, value in cls.__dict__.items():
                    if not isinstance(value, twitchio_commands.Command):
                        continue
                    if value.name in self.commands:
                        continue
                    cmd = copy.copy(value)
                    cmd._injected = self
                    try:
                        self.add_command(cmd)
                    except Exception:
                        log.debug("Konnte Command nicht registrieren: %s", cmd.name, exc_info=True)

        @property
        def bot_id_safe(self) -> Optional[str]:
            """Gibt eine sichere bot_id zurück (None statt leerer String)."""
            # Prüfe zuerst die gespeicherte ID
            if self._bot_id_stored and str(self._bot_id_stored).strip():
                return str(self._bot_id_stored)
            # Fallback auf die TwitchIO bot_id Property
            bot_id = getattr(self, 'bot_id', None)
            if bot_id and str(bot_id).strip():
                return str(bot_id)
            return None

        def set_raid_bot(self, raid_bot):
            """Setzt die RaidBot-Instanz für OAuth-URLs."""
            self._raid_bot = raid_bot

        def set_discord_bot(
            self,
            discord_bot: Optional[discord.Client],
            *,
            invite_channel_id: Optional[int] = None,
        ) -> None:
            """Assign the Discord bot instance for promo invite creation."""
            self._discord_bot = discord_bot
            channel_id: Optional[int] = None
            if invite_channel_id:
                try:
                    channel_id = int(invite_channel_id)
                except (TypeError, ValueError):
                    channel_id = None
            if not channel_id:
                try:
                    default_id = int(TWITCH_NOTIFY_CHANNEL_ID or 0)
                except (TypeError, ValueError):
                    default_id = 0
                if default_id:
                    channel_id = default_id
            self._discord_invite_channel_id = channel_id
            log.info(
                "Discord bot set for promo invites (channel_id=%s)",
                str(channel_id) if channel_id else "-",
            )

        def _load_streamer_invite_from_db(self, login: str) -> Optional[str]:
            login_norm = (login or "").strip().lower()
            if not login_norm:
                return None
            try:
                with get_conn() as conn:
                    row = conn.execute(
                        """
                        SELECT invite_url, invite_code
                          FROM twitch_streamer_invites
                         WHERE streamer_login = ?
                        """,
                        (login_norm,),
                    ).fetchone()
                if not row:
                    return None
                invite_url = row["invite_url"] if hasattr(row, "keys") else row[0]
                invite_code = row["invite_code"] if hasattr(row, "keys") else row[1]
                if invite_url:
                    return str(invite_url)
                if invite_code:
                    return f"https://discord.gg/{invite_code}"
            except Exception:
                log.debug("Promo invite DB lookup failed for %s", login_norm, exc_info=True)
            return None

        def _store_streamer_invite(
            self,
            login: str,
            *,
            guild_id: int,
            channel_id: int,
            invite_code: str,
            invite_url: str,
        ) -> None:
            login_norm = (login or "").strip().lower()
            if not login_norm:
                return
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            try:
                with get_conn() as conn:
                    conn.execute(
                        """
                        INSERT INTO twitch_streamer_invites (
                            streamer_login,
                            guild_id,
                            channel_id,
                            invite_code,
                            invite_url,
                            created_at,
                            last_sent_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(streamer_login) DO UPDATE SET
                            guild_id = excluded.guild_id,
                            channel_id = excluded.channel_id,
                            invite_code = excluded.invite_code,
                            invite_url = excluded.invite_url,
                            created_at = excluded.created_at
                        """,
                        (
                            login_norm,
                            int(guild_id),
                            int(channel_id),
                            str(invite_code),
                            str(invite_url),
                            now,
                            None,
                        ),
                    )
                    conn.execute(
                        """
                        INSERT INTO discord_invite_codes (
                            guild_id,
                            invite_code,
                            created_at,
                            last_seen_at
                        ) VALUES (?, ?, ?, ?)
                        ON CONFLICT(guild_id, invite_code)
                        DO UPDATE SET last_seen_at = excluded.last_seen_at
                        """,
                        (int(guild_id), str(invite_code), now, now),
                    )
                    conn.commit()
            except Exception:
                log.debug("Could not store promo invite for %s", login_norm, exc_info=True)

        def _mark_streamer_invite_sent(self, login: str) -> None:
            login_norm = (login or "").strip().lower()
            if not login_norm:
                return
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            try:
                with get_conn() as conn:
                    conn.execute(
                        "UPDATE twitch_streamer_invites SET last_sent_at = ? WHERE streamer_login = ?",
                        (now, login_norm),
                    )
                    conn.commit()
            except Exception:
                log.debug("Could not update promo invite last_sent_at for %s", login_norm, exc_info=True)

        async def _candidate_invite_channels(self) -> list:
            bot = self._discord_bot
            if not bot:
                return []

            channels = []
            seen = set()

            def _add_channel(channel) -> None:
                if not channel or not hasattr(channel, "create_invite"):
                    return
                cid = getattr(channel, "id", None)
                if cid is None or cid in seen:
                    return
                seen.add(cid)
                channels.append(channel)

            if self._discord_invite_channel_id:
                channel = bot.get_channel(self._discord_invite_channel_id)
                if channel is None and hasattr(bot, "fetch_channel"):
                    try:
                        channel = await bot.fetch_channel(self._discord_invite_channel_id)
                    except Exception:
                        channel = None
                _add_channel(channel)

            for guild in getattr(bot, "guilds", []):
                _add_channel(getattr(guild, "system_channel", None))

            for guild in getattr(bot, "guilds", []):
                for channel in getattr(guild, "text_channels", []):
                    _add_channel(channel)

            return channels

        async def _create_streamer_invite(self, login: str) -> Optional[str]:
            bot = self._discord_bot
            if not bot:
                return None

            if hasattr(bot, "wait_until_ready"):
                try:
                    await bot.wait_until_ready()
                except Exception:
                    log.debug("Discord bot readiness check failed", exc_info=True)

            candidates = await self._candidate_invite_channels()
            if not candidates:
                return None

            for channel in candidates:
                try:
                    invite = await channel.create_invite(
                        max_uses=0,
                        max_age=0,
                        unique=True,
                        reason=f"Twitch promo invite for {login}",
                    )
                except discord.Forbidden:
                    continue
                except discord.HTTPException:
                    continue
                except Exception:
                    log.debug(
                        "Failed to create invite in channel %s for %s",
                        getattr(channel, "id", "?"),
                        login,
                        exc_info=True,
                    )
                    continue

                invite_code = str(getattr(invite, "code", "") or "").strip()
                invite_url = str(getattr(invite, "url", "") or "").strip()
                if not invite_url and invite_code:
                    invite_url = f"https://discord.gg/{invite_code}"

                guild = getattr(channel, "guild", None)
                guild_id = getattr(guild, "id", None) if guild else None
                channel_id = getattr(channel, "id", None)
                if not invite_code or not invite_url or not guild_id or not channel_id:
                    continue

                self._store_streamer_invite(
                    login,
                    guild_id=int(guild_id),
                    channel_id=int(channel_id),
                    invite_code=invite_code,
                    invite_url=invite_url,
                )
                log.info(
                    "Created promo invite for %s (guild=%s, channel=%s, code=%s)",
                    login,
                    guild_id,
                    channel_id,
                    invite_code,
                )
                return invite_url

            return None

        async def _resolve_streamer_invite(self, login: str) -> tuple[Optional[str], bool]:
            login_norm = (login or "").strip().lower()
            if not login_norm:
                return None, False

            cached = self._promo_invite_cache.get(login_norm)
            if cached:
                return cached, True

            invite_url = self._load_streamer_invite_from_db(login_norm)
            if invite_url:
                self._promo_invite_cache[login_norm] = invite_url
                return invite_url, True

            invite_url = await self._create_streamer_invite(login_norm)
            if invite_url:
                self._promo_invite_cache[login_norm] = invite_url
                return invite_url, True

            return None, False

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
                    log.info("Bot auth added (refresh available: %s).", "yes" if self._bot_refresh_token else "no")  # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
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

            # Initial channels werden NICHT hier gejoined –
            # setup_hook() läuft vor event_ready(), die WS-Session von TwitchIO
            # ist noch nicht aufgebaut. Joins hier führen zu
            # "invalid transport and auth combination" (400).
            # Defer nach event_ready().

        async def event_ready(self):
            """Wird aufgerufen, wenn der Bot verbunden ist."""
            name = self.user.name if self.user else "Unknown"
            log.info("Twitch Chat Bot ready | Logged in as: %s", name)
            # Debug: Registrierte Commands loggen
            cmds = ", ".join(sorted(self.commands.keys()))
            log.info("Registered Chat Commands: %s", cmds)

            # Initial channels erst hier joinen – WS-Session ist jetzt bereit
            if self._initial_channels:
                log.info("Joining %d initial channels...", len(self._initial_channels))
                for channel in self._initial_channels:
                    try:
                        success = await self.join(channel)
                        if success:
                            await asyncio.sleep(0.2)  # Rate limiting
                    except Exception as e:
                        log.debug("Konnte initialem Channel %s nicht beitreten: %s", channel, e)

            if PROMO_MESSAGES and (_PROMO_ACTIVITY_ENABLED or PROMO_VIEWER_SPIKE_ENABLED):
                if not self._promo_task or self._promo_task.done():
                    self._promo_task = asyncio.create_task(
                        self._periodic_promo_loop(),
                        name="twitch.chat_bot.promos",
                    )
                    log.info(
                        "Chat-Promo-Loop gestartet (Check alle %ss)",
                        max(15, int(PROMO_LOOP_INTERVAL_SEC)),
                    )

        async def close(self):
            promo_task = self._promo_task
            self._promo_task = None
            if promo_task and not promo_task.done():
                promo_task.cancel()
                try:
                    await promo_task
                except asyncio.CancelledError:
                    log.debug("Promo-Task wurde beim Shutdown abgebrochen")
                except Exception:
                    log.debug("Promo-Task konnte nicht sauber beendet werden", exc_info=True)
            await super().close()

        async def event_command_error(self, payload):
            """Fehlerbehandlung für Commands."""
            ctx = payload.context
            error = payload.exception

            if isinstance(error, twitchio_commands.CommandNotFound):
                # Kein Traceback für unbekannte Commands, nur Debug
                log.debug("Command not found: %s", ctx.message.content)
                return

            # Andere Fehler loggen
            log.exception("Error invoking command %s: %s", ctx.command.name if ctx.command else "Unknown", error)

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

        async def event_message(self, message):
            """Wird bei jeder Chat-Nachricht aufgerufen."""
            # Compatibility layer for TwitchIO 3.x EventSub
            # In 3.x, message is a ChatMessage with text/chatter/broadcaster
            # and optional source_broadcaster (shared chat).
            # In 2.x, message is a Message with content/author/channel

            # Detect 3.x by presence of 'text' or 'chatter' (and absence of 'content')
            is_3x = hasattr(message, "chatter") and not hasattr(message, "content")
            
            if is_3x:
                # Aliases for 2.x compatibility
                if not hasattr(message, "content"):
                    message.content = getattr(message, "text", "")
                if not hasattr(message, "author"):
                    message.author = message.chatter
                
                # Ensure author has 2.x style flags if missing
                author = message.author
                if not hasattr(author, "moderator") and hasattr(author, "is_moderator"):
                    author.moderator = author.is_moderator
                if not hasattr(author, "broadcaster") and hasattr(author, "is_broadcaster"):
                    author.broadcaster = author.is_broadcaster

                # In 3.x EventSub payloads there is no message.channel by default.
                # Normalize to a 2.x-like shape expected by downstream code.
                channel = getattr(message, "channel", None)
                if channel is None:
                    channel = getattr(message, "source_broadcaster", None) or getattr(message, "broadcaster", None)
                    if channel is not None:
                        try:
                            message.channel = channel
                        except (AttributeError, TypeError):
                            log.debug(
                                "Could not assign normalized channel on EventSub message",
                                exc_info=True,
                            )

                if channel is not None and not hasattr(channel, "name") and hasattr(channel, "login"):
                    try:
                        channel.name = channel.login
                    except (AttributeError, TypeError):
                        log.debug(
                            "Could not normalize channel.name from channel.login",
                            exc_info=True,
                        )

            # Fallback for echo if still missing (unlikely in 3.x)
            if not hasattr(message, "echo"):
                safe_bot_id = self.bot_id_safe or self.bot_id or ""
                message.echo = str(getattr(message, "chatter", message).id) == str(safe_bot_id)

            # Ignoriere Bot-Nachrichten
            if message.echo:
                return

            # Whitelist-Check: Bekannte Bot-Accounts überspringen Spam-Prüfung
            author_name = getattr(message.author, "name", "").lower()
            if author_name in WHITELISTED_BOTS:
                # Bot ist whitelisted - überspringe Spam-Detection komplett
                try:
                    await self._track_chat_health(message)
                except Exception:
                    log.debug("Konnte Chat-Health nicht loggen", exc_info=True)
                await self.process_commands(message)
                return

            try:
                channel_login = self._normalize_channel_login_safe(getattr(message, "channel", None))
                spam_score, spam_reasons = self._calculate_spam_score(message.content or "")
                mention_score, mention_reasons = await self._score_mention_patterns(
                    message.content or "",
                    host_login=channel_login,
                )
                if mention_score:
                    spam_score += mention_score
                    spam_reasons.extend(mention_reasons)

                # 2. Faktor: Account-Alter prüft nur den letzten fehlenden Punkt zum Ban.
                # Ein junges Konto soll nur dann eskalieren, wenn bereits zwei Signale vorliegen.
                if spam_score == (SPAM_MIN_MATCHES - 1):
                    try:
                        author_id = getattr(message.author, "id", None)
                        if author_id:
                            # fetch_users benötigt IDs. Twitch IDs sind numerisch.
                            users = await self.fetch_users(ids=[int(author_id)])
                            if users and users[0].created_at:
                                created_at = users[0].created_at
                                if created_at.tzinfo is None:
                                    created_at = created_at.replace(tzinfo=timezone.utc)

                                age = datetime.now(timezone.utc) - created_at
                                if age.days < 90:  # Jünger als 3 Monate
                                    spam_score += 1
                                    spam_reasons.append(f"Account-Alter: {age.days} Tage")
                    except Exception:
                        log.debug("Konnte User-Alter für Spam-Check nicht laden", exc_info=True)

                if spam_score >= SPAM_MIN_MATCHES:
                    enforced = await self._auto_ban_and_cleanup(message)
                    if not enforced:
                        channel_obj = getattr(message, "channel", None)
                        channel_name = (
                            getattr(channel_obj, "name", "")
                            or getattr(channel_obj, "login", "")
                            or "unknown"
                        )
                        log.warning("Spam erkannt in %s (Score: %d, Treffer: %s), aber Auto-Ban konnte nicht durchgesetzt werden.", channel_name, spam_score, ", ".join(spam_reasons))
                    return
                elif spam_score > 0:
                    channel_obj = getattr(message, "channel", None)
                    channel_name = (
                        getattr(channel_obj, "name", "")
                        or getattr(channel_obj, "login", "")
                        or "unknown"
                    )
                    author_name = getattr(message.author, "name", "unknown")
                    author_id = str(getattr(message.author, "id", ""))

                    # Logge Verdacht in Datei für Feinabstimmung
                    reasons_str = ", ".join(spam_reasons)
                    self._record_autoban(
                        channel_name=channel_name,
                        chatter_login=author_name,
                        chatter_id=author_id,
                        content=message.content or "",
                        status=f"SUSPICIOUS({spam_score})",
                        reason=reasons_str
                    )

                    log.info("Verdächtige Nachricht (Score %d, Treffer: %s) in %s von %s: %s", spam_score, reasons_str, channel_name, author_name, message.content)
            except Exception:
                log.debug("Auto-Ban Prüfung fehlgeschlagen", exc_info=True)

            try:
                await self._track_chat_health(message)
            except Exception:
                log.debug("Konnte Chat-Health nicht loggen", exc_info=True)

            try:
                login_for_raw = self._normalize_channel_login_safe(getattr(message, "channel", None))
                if login_for_raw:
                    self._record_raw_chat_message(login_for_raw)
            except Exception:
                log.debug("Raw-Chat-Activity konnte nicht erfasst werden", exc_info=True)

            sent_invite = False
            try:
                sent_invite = await self._maybe_send_deadlock_access_hint(message)
            except Exception:
                log.debug("Deadlock-Invite-Check fehlgeschlagen", exc_info=True)

            if _PROMO_ACTIVITY_ENABLED and not sent_invite:
                try:
                    await self._maybe_send_activity_promo(message)
                except Exception:
                    log.debug("Promo-Activity-Check fehlgeschlagen", exc_info=True)

            # Verarbeite Commands
            await self.process_commands(message)

        def _get_streamer_by_channel(self, channel_name: str) -> Optional[tuple]:
            """Findet Streamer-Daten anhand des Channel-Namens."""
            normalized = self._normalize_channel_login(channel_name)
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

        @staticmethod
        def _normalize_channel_login(channel_name: str) -> str:
            return (channel_name or "").lower().lstrip("#")

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

if not TWITCHIO_AVAILABLE:
    class RaidChatBot:  # type: ignore[redefined-outer-name]
        """Stub, damit Import-Caller nicht crashen, wenn twitchio fehlt."""
        pass


async def create_twitch_chat_bot(
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    raid_bot=None,
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
            """
        ).fetchall()

    initial_channels = []
    for login, user_id, scopes_raw, is_live in partners:
        login_norm = (login or "").strip()
        if not login_norm:
            continue
        scopes = [s.strip().lower() for s in (scopes_raw or "").split() if s.strip()]
        has_chat_scope = any(
            s in {"user:read:chat", "user:write:chat", "chat:read", "chat:edit"} for s in scopes
        )
        if not has_chat_scope:
            continue
        # Nur live Channels beim Start; Offline-Partner joinen später via EventSub stream.online
        if is_live is None or not bool(is_live):
            continue
        initial_channels.append(login_norm)

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

    adapter_host = (os.getenv("TWITCH_CHAT_ADAPTER_HOST") or "").strip() or "127.0.0.1"
    adapter_port_raw = (os.getenv("TWITCH_CHAT_ADAPTER_PORT") or "").strip()
    adapter_port = 4343
    if adapter_port_raw:
        try:
            adapter_port = int(adapter_port_raw)
        except ValueError:
            log.warning(
                "TWITCH_CHAT_ADAPTER_PORT '%s' ist ungueltig - es wird der Standardport 4343 genutzt",
                adapter_port_raw,
            )
            adapter_port = 4343

    # Adapter nur starten wenn TWITCH_CHAT_ADAPTER nicht explizit deaktiviert ist
    # UND der Port frei ist. TwitchIO 3.x erstellt intern einen Default-Adapter wenn
    # keiner übergeben wird – wir kontrollieren das hier explizit, um Port-Konflikte
    # bei Cog-Reloads zu vermeiden.
    adapter_disabled = (os.getenv("TWITCH_CHAT_ADAPTER") or "").strip().lower() in {"0", "false", "off", "no"}
    web_adapter = None
    if not adapter_disabled:
        import socket as _socket
        try:
            # Versuch einer Verbindung, um zu prüfen, ob der Port belegt ist
            # Connect-Check ist passiv und verursacht kein TIME_WAIT wie Bind-Check
            with _socket.create_connection((adapter_host, adapter_port), timeout=0.2):
                # Verbindung erfolgreich -> Port ist belegt
                log.debug(
                    "TwitchIO Web Adapter Port %s auf %s ist belegt (Verbindung erfolgreich) – starte ohne Adapter "
                    "(Webhooks/OAuth für Chat-Bot ausgeschaltet).",
                    adapter_port, adapter_host,
                )
                web_adapter = None
        except OSError:
            # Verbindung fehlgeschlagen -> Port ist wahrscheinlich frei
            try:
                web_adapter = twitchio_web.AiohttpAdapter(
                    host=adapter_host,
                    port=adapter_port,
                )
                log.info("TwitchIO Web Adapter wird auf %s:%s gestartet", adapter_host, adapter_port)
            except Exception as e:
                log.error("Fehler beim Erstellen des Adapters trotz freiem Port: %s", e)
                web_adapter = None
    else:
        log.info("TwitchIO Web Adapter deaktiviert per TWITCH_CHAT_ADAPTER.")

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
