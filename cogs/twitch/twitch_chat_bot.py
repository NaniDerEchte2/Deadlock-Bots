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

from httpx import stream

try:
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
# Whitelist für bekannte legitime Bots (keine Spam-Prüfung)
_WHITELISTED_BOTS = {
    "streamelements",
    "nightbot",
    "streamlabs",
    "moobot",
    "fossabot",
    "wizebot",
    "pretzelrocks",
    "soundalerts",
}

_SPAM_PHRASES = (
    "Best viewers streamboo.com",
    "Best viewers streamboo .com",
    "Best viewers streamboo com",
    "Best viewers smmtop32.online",
    "Best viewers smmtop32 .online",
    "Best viewers smmtop32 online",
    "Best viewers on",
    "Best viewers",
    "B̟est viewers",
    "Cheap Viewers",
    "Ch͟eap viewers",
    "(remove the space)",
    "Cool overlay \N{THUMBS UP SIGN} Honestly, it\N{RIGHT SINGLE QUOTATION MARK}s so hard to get found on the directory lately. I have small tips on beating the algorithm. Mind if I send you an share?",
    "Mind if I send you an share",
    " Viewers https://smmbest5.online",
    "Viewers smmbest4.online",
    "Viewers streamboo .com",
    
)
# Entferne "viewer" und "viewers" aus den Fragmenten - zu allgemein und führt zu False Positives
_SPAM_FRAGMENTS = (
    "best viewers",  # Nur die Kombination ist verdächtig
    "cheap viewers",  # Nur die Kombination ist verdächtig
    "streamboo.com",
    "streamboo .com",
    "streamboo com",
    "streamboo",
    "smmtop32.online",
    "smmtop32 .online",
    "smmtop32 online",
    "smmtop32",
    "remove the space",
    "cool overlay",
    "get found on the directory",
    "beating the algorithm",
    "d!sc",
    "smmbest4.online",
    "smmbest5.online",
    "rookie",
)
_SPAM_MIN_MATCHES = 2


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
            # Speichere bot_id als Instanzvariable BEVOR wir super().__init__ aufrufen
            self._bot_id_stored = bot_id
            super().__init__(
                client_id=client_id,
                client_secret=client_secret,
                bot_id=bot_id or "", # Fallback auf leeren String falls None (für TwitchIO Kompatibilität)
                prefix=prefix,
                **base_kwargs,
            )
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
            self._autoban_log = Path("logs") / "twitch_autobans.log"
            log.info("Twitch Chat Bot initialized with %d initial channels", len(self._initial_channels))

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
            # Zeige initial channels (monitored_streamers wird erst nach join() befüllt)
            initial = ", ".join(self._initial_channels[:10]) if self._initial_channels else "(none yet)"
            log.info("Initial channels to join: %s", initial)

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
                normalized_login = channel_login.lower().lstrip("#")
                
                # Prüfe ZUERST, ob wir bereits subscribed sind
                if normalized_login in self._monitored_streamers:
                    log.debug("Channel %s already monitored, skipping subscribe", channel_login)
                    return True
                
                if not channel_id:
                    user = await self.fetch_user(login=channel_login.lstrip("#"))
                    if not user:
                        log.error("Could not find user ID for channel %s", channel_login)
                        return False
                    channel_id = str(user.id)

                # Wir nutzen IMMER den Bot-Token für alle Channels.
                # Das hält die Anzahl der WebSocket-Verbindungen auf 1 (Limit bei Twitch ist 3 pro Client ID).
                # Voraussetzung: Der Bot muss Moderator im Ziel-Kanal sein.
                safe_bot_id = self.bot_id_safe or self.bot_id or ""
                payload = eventsub.ChatMessageSubscription(
                    broadcaster_user_id=str(channel_id), 
                    user_id=str(safe_bot_id)
                )
                
                # Wir abonnieren über den Standard-WebSocket des Bots
                await self.subscribe_websocket(payload=payload)

                self._monitored_streamers.add(normalized_login)
                return True
            except Exception as e:
                msg = str(e)
                if "403" in msg and "subscription missing proper authorization" in msg:
                    log.warning(
                        "Cannot join chat for %s. Reasons: Bot account missing 'user:read:chat' scope "
                        "OR Bot is not a Moderator in the channel (please /mod bot).",
                        channel_login
                    )
                elif "429" in msg or "transport limit exceeded" in msg.lower():
                    log.error(
                        "Cannot join chat for %s: WebSocket Transport Limit (429) reached. "
                        "Ensure the bot uses only one WebSocket connection.",
                        channel_login
                    )
                else:
                    log.error("Failed to join channel %s: %s", channel_login, e)
                return False

        async def event_message(self, message):
            """Wird bei jeder Chat-Nachricht aufgerufen."""
            # Compatibility layer for TwitchIO 3.x EventSub
            if not hasattr(message, "echo"):
                safe_bot_id = self.bot_id_safe or self.bot_id or ""
                message.echo = str(getattr(message.chatter, "id", "")) == str(safe_bot_id)
            
            if not hasattr(message, "content"):
                message.content = getattr(message, "text", "")
            
            if not hasattr(message, "author"):
                message.author = message.chatter
            
            # Mock channel object mit allen benötigten Attributen
            if not hasattr(message, "channel"):
                broadcaster_login = getattr(message.broadcaster, "login", None) or getattr(message.broadcaster, "name", "unknown")
                broadcaster_id = getattr(message.broadcaster, "id", None)
                
                class MockChannel:
                    def __init__(self, login, channel_id=None):
                        self.name = login
                        self.id = channel_id
                    def __str__(self):
                        return self.name
                    async def send(self, content):
                        # Fallback send method
                        pass
                
                message.channel = MockChannel(broadcaster_login, broadcaster_id)

            # Ignoriere Bot-Nachrichten
            if message.echo:
                return

            # Whitelist-Check: Bekannte Bot-Accounts überspringen Spam-Prüfung
            author_name = getattr(message.author, "name", "").lower()
            if author_name in _WHITELISTED_BOTS:
                # Bot ist whitelisted - überspringe Spam-Detection komplett
                try:
                    await self._track_chat_health(message)
                except Exception:
                    log.debug("Konnte Chat-Health nicht loggen", exc_info=True)
                await self.process_commands(message)
                return

            try:
                spam_score = self._calculate_spam_score(message.content or "")

                # 2. Faktor: Account-Alter prüfen, wenn Verdacht besteht (Score 1)
                # Wenn Keyword matcht UND Account < 6 Monate -> Ban (Score >= 2)
                if 0 < spam_score < _SPAM_MIN_MATCHES:
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
                                if age.days < 180: # Jünger als 6 Monate
                                    spam_score += 1
                                    # Info-Log für interne Feinabstimmung (wie gewünscht)
                                    # Broadcaster sieht davon nichts (außer Bann-Nachricht falls Score >= 2)
                    except Exception:
                        log.debug("Konnte User-Alter für Spam-Check nicht laden", exc_info=True)

                if spam_score >= _SPAM_MIN_MATCHES:
                    enforced = await self._auto_ban_and_cleanup(message)
                    if not enforced:
                        log.warning("Spam erkannt (Score: %d), aber Auto-Ban konnte nicht durchgesetzt werden.", spam_score)
                    return
                elif spam_score == 1:
                    channel_name = getattr(message.channel, "name", "unknown")
                    author_name = getattr(message.author, "name", "unknown")
                    author_id = str(getattr(message.author, "id", ""))
                    
                    # Logge Verdacht in Datei für Feinabstimmung
                    self._record_autoban(
                        channel_name=channel_name,
                        chatter_login=author_name,
                        chatter_id=author_id,
                        content=message.content or "",
                        status="SUSPICIOUS"
                    )
                    
                    log.info("Verdächtige Nachricht (1 Hit, Account > 6 Monate) in %s von %s: %s", channel_name, author_name, message.content)
            except Exception:
                log.debug("Auto-Ban Prüfung fehlgeschlagen", exc_info=True)

            try:
                await self._track_chat_health(message)
            except Exception:
                log.debug("Konnte Chat-Health nicht loggen", exc_info=True)

            # Verarbeite Commands
            await self.process_commands(message)

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

        def _calculate_spam_score(self, content: str) -> int:
            """Berechnet einen Spam-Score. >= _SPAM_MIN_MATCHES ist ein Ban."""
            if not content:
                return 0

            raw = content.strip()
            # Direkte Phrasen sind sofortiger Ban -> hoher Score
            if any(phrase in raw for phrase in _SPAM_PHRASES):
                return 999

            lowered = raw.casefold()
            if any(phrase.casefold() in lowered for phrase in _SPAM_PHRASES):
                return 999

            # Prüfe Fragmente mit Wortgrenzen (\b), um Teiltreffer in längeren Wörtern zu vermeiden
            hits = sum(1 for frag in _SPAM_FRAGMENTS if re.search(r'\b' + re.escape(frag.casefold()) + r'\b', lowered))

            # Muster: "viewer [name]" (oft ein Merkmal von Bots)
            if re.search(r"\bviewer\s+\w+", lowered):
                hits += 1

            compact = re.sub(r"[^a-z0-9]", "", lowered)
            if "streamboocom" in compact:
                hits += 1

            return hits

        async def _get_moderation_context(self, twitch_user_id: str) -> tuple[Optional[object], Optional[dict]]:
            """Holt Session + Auth-Header für Moderationscalls."""
            auth_mgr = getattr(self._raid_bot, "auth_manager", None) if self._raid_bot else None
            http_session = getattr(self._raid_bot, "session", None) if self._raid_bot else None
            if not auth_mgr or not http_session:
                return None, None
            try:
                tokens = await auth_mgr.get_tokens_for_user(str(twitch_user_id), http_session)
                if not tokens:
                    return None, None
                access_token = tokens[0]
                headers = {
                    "Client-ID": self._client_id,
                    "Authorization": f"Bearer {access_token}",
                }
                return http_session, headers
            except Exception:
                log.debug("Konnte Moderations-Kontext nicht laden (%s)", twitch_user_id, exc_info=True)
                return None, None

        def _record_autoban(self, *, channel_name: str, chatter_login: str, chatter_id: str, content: str, status: str = "BANNED") -> None:
            """Persistiert Auto-Ban-Ereignis oder Verdacht für spätere Review."""
            try:
                self._autoban_log.parent.mkdir(parents=True, exist_ok=True)
                ts = datetime.now(timezone.utc).isoformat()
                safe_content = content.replace("\n", " ")[:500]
                line = f"{ts}\t[{status}]\t{channel_name}\t{chatter_login or '-'}\t{chatter_id}\t{safe_content}\n"
                with self._autoban_log.open("a", encoding="utf-8") as f:
                    f.write(line)
            except Exception:
                log.debug("Konnte Auto-Ban Review-Log nicht schreiben", exc_info=True)

        async def _send_chat_message(self, channel, text: str) -> bool:
            """Best-effort Chat-Nachricht senden (EventSub-kompatibel)."""
            try:
                # 1. Direktes .send() (z.B. Context, 2.x Channel oder 3.x Broadcaster)
                if channel and hasattr(channel, "send"):
                    await channel.send(text)
                    return True
                
                # 2. Fallback: Direkte Helix API Call (TwitchIO 3.x kompatibel)
                # Hinweis: send_message() existiert NICHT in TwitchIO 3.x
                b_id = None
                if hasattr(channel, "id"):
                    b_id = str(channel.id)
                elif hasattr(channel, "broadcaster") and hasattr(channel.broadcaster, "id"):
                    b_id = str(channel.broadcaster.id)
                
                # Wenn wir keine ID haben, aber einen Namen (MockChannel), fetch_user
                if not b_id and hasattr(channel, "name"):
                    user = await self.fetch_user(login=channel.name.lstrip("#"))
                    if user:
                        b_id = str(user.id)

                safe_bot_id = self.bot_id_safe or self.bot_id
                if b_id and safe_bot_id and self._token_manager:
                    # Nutze Helix API direkt (user:write:chat scope erforderlich)
                    try:
                        tokens = await self._token_manager.get_valid_token()
                        if not tokens:
                            log.debug("No valid bot token for Helix chat message")
                            return False
                        
                        access_token, _ = tokens
                        url = "https://api.twitch.tv/helix/chat/messages"
                        headers = {
                            "Client-ID": self._client_id,
                            "Authorization": f"Bearer {access_token}",
                            "Content-Type": "application/json"
                        }
                        payload = {
                            "broadcaster_id": str(b_id),
                            "sender_id": str(safe_bot_id),
                            "message": text
                        }
                        
                        # Nutze aiohttp direkt
                        import aiohttp
                        async with aiohttp.ClientSession() as session:
                            async with session.post(url, headers=headers, json=payload) as r:
                                if r.status in {200, 204}:
                                    return True
                                else:
                                    log.debug("Helix chat message failed: HTTP %s", r.status)
                    except Exception as e:
                        log.debug("Helix chat message exception: %s", e)

            except Exception:
                log.debug("Konnte Chat-Nachricht nicht senden", exc_info=True)
            return False

        @staticmethod
        def _extract_message_id(message) -> Optional[str]:
            """Best-effort message_id Extraktion für Moderations-APIs."""
            for attr in ("id", "message_id"):
                msg_id = str(getattr(message, attr, "") or "").strip()
                if msg_id:
                    return msg_id
            try:
                tags = getattr(message, "tags", None)
                if isinstance(tags, dict):
                    msg_id = str(tags.get("id") or tags.get("message-id") or "").strip()
                    if msg_id:
                        return msg_id
            except Exception as exc:
                log.debug("Konnte message-id aus Tags nicht lesen", exc_info=exc)
            return None

        async def _auto_ban_and_cleanup(self, message) -> bool:
            """Bannt erkannte Spam-Bots und löscht die Nachricht."""
            channel_name = getattr(message.channel, "name", "") or ""
            streamer_data = self._get_streamer_by_channel(channel_name)
            if not streamer_data:
                return False

            twitch_login, twitch_user_id, _raid_enabled = streamer_data
            author = getattr(message, "author", None)
            chatter_login = getattr(author, "name", "") if author else ""
            chatter_id = str(getattr(author, "id", "") or "")
            original_content = message.content or ""

            if not chatter_id:
                return False
            if chatter_id == str(twitch_user_id):
                return False
            if getattr(author, "is_mod", False) or getattr(author, "is_broadcaster", False):
                return False

            session, headers = await self._get_moderation_context(str(twitch_user_id))
            if not headers:
                log.warning("Spam erkannt in %s, aber kein gültiger Token für Moderation verfügbar.", channel_name)
                return False

            if session is None:
                log.warning("Keine HTTP-Session für Auto-Ban verfügbar (%s).", channel_name)
                return False

            message_id = self._extract_message_id(message)
            if message_id:
                try:
                    async with session.delete(
                        "https://api.twitch.tv/helix/moderation/chat",
                        headers=headers,
                        params={
                            "broadcaster_id": twitch_user_id,
                            "moderator_id": twitch_user_id,
                            "message_id": message_id,
                        },
                    ) as resp:
                        if resp.status not in {200, 204}:
                            txt = await resp.text()
                            log.debug(
                                "Konnte Nachricht nicht löschen (%s/%s): HTTP %s %s",
                                channel_name,
                                message_id,
                                resp.status,
                                txt[:180].replace("\n", " "),
                            )
                except Exception:
                    log.debug("Auto-Delete fehlgeschlagen (%s)", channel_name, exc_info=True)

            try:
                payload = {"data": {"user_id": chatter_id, "reason": "Automatischer Spam-Ban (Bot-Phrase)"}}
                async with session.post(
                    "https://api.twitch.tv/helix/moderation/bans",
                    headers=headers,
                    params={"broadcaster_id": twitch_user_id, "moderator_id": twitch_user_id},
                    json=payload,
                ) as resp:
                    if resp.status in {200, 201, 202}:
                        log.info("Auto-Ban ausgelöst in %s für %s", channel_name, chatter_login or chatter_id)
                        self._last_autoban[channel_name.lower()] = {
                            "user_id": chatter_id,
                            "login": chatter_login,
                            "content": original_content,
                            "ts": datetime.now(timezone.utc).isoformat(),
                        }
                        self._record_autoban(
                            channel_name=channel_name,
                            chatter_login=chatter_login,
                            chatter_id=chatter_id,
                            content=original_content,
                            status="BANNED"
                        )
                        await self._send_chat_message(
                            message.channel,
                            f"Nachricht gelöscht und Nutzer gebannt: {chatter_login or chatter_id}. Original Nachricht: {original_content}",
                        )
                        return True
                    txt = await resp.text()
                    log.warning(
                        "Auto-Ban fehlgeschlagen in %s (user=%s): HTTP %s %s",
                        channel_name,
                        chatter_id,
                        resp.status,
                        txt[:180].replace("\n", " "),
                    )
            except Exception:
                log.debug("Auto-Ban Exception in %s", channel_name, exc_info=True)

            # Wenn wir hier sind, ist Ban fehlgeschlagen
            return False

        async def _unban_user(self, *, broadcaster_id: str, target_user_id: str, channel_name: str, login_hint: str = "") -> bool:
            """Hebt einen Ban auf (z. B. per !uban nach Fehlalarm)."""
            session, headers = await self._get_moderation_context(broadcaster_id)
            if not session or not headers:
                log.warning("Unban nicht möglich (kein Token) in %s", channel_name)
                return False
            try:
                async with session.delete(
                    "https://api.twitch.tv/helix/moderation/bans",
                    headers=headers,
                    params={
                        "broadcaster_id": broadcaster_id,
                        "moderator_id": broadcaster_id,
                        "user_id": target_user_id,
                    },
                ) as resp:
                    if resp.status in {200, 204}:
                        log.info("Unban ausgeführt in %s für %s", channel_name, login_hint or target_user_id)
                        return True
                    txt = await resp.text()
                    log.warning(
                        "Unban fehlgeschlagen in %s (user=%s): HTTP %s %s",
                        channel_name,
                        target_user_id,
                        resp.status,
                        txt[:180].replace("\n", " "),
                    )
            except Exception:
                log.debug("Unban Exception in %s", channel_name, exc_info=True)
            return False

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

        @twitchio_commands.command(name="uban")
        async def cmd_uban(self, ctx: twitchio_commands.Context):
            """!uban - hebt den letzten Auto-Ban im aktuellen Channel auf."""
            if not (ctx.author.is_broadcaster or ctx.author.is_mod):
                await ctx.send(f"@{ctx.author.name} Nur der Broadcaster oder Mods.")
                return

            channel_name = ctx.channel.name
            streamer_data = self._get_streamer_by_channel(channel_name)
            if not streamer_data:
                await ctx.send(f"@{ctx.author.name} Dieser Kanal ist nicht als Partner registriert.")
                return

            twitch_login, twitch_user_id, _ = streamer_data
            last = self._last_autoban.get(channel_name.lower())
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

            is_partner_raid = True
            target = None
            
            if candidates:
                # Fairness-Auswahl wiederverwenden
                target = self._raid_bot._select_fairest_candidate(candidates, broadcaster_id=twitch_user_id)  # type: ignore[attr-defined]
            
            if not target:
                # Fallback auf DE Deadlock-Streamer
                try:
                    from .constants import TWITCH_TARGET_GAME_NAME
                    category_id = await api.get_category_id(TWITCH_TARGET_GAME_NAME)
                    if category_id:
                        de_streams = await api.get_streams_by_category(category_id, language="de", limit=50)
                        # Filter out self
                        de_streams = [s for s in de_streams if str(s.get("user_id")) != str(twitch_user_id)]
                        if de_streams:
                            is_partner_raid = False
                            target = de_streams[0]
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
                    from datetime import datetime, timezone
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
                    session=api_session,
                )
            except Exception as exc:
                log.exception("Manual raid failed for %s -> %s", twitch_login, target_login)
                await ctx.send(f"@{ctx.author.name} Raid fehlgeschlagen: {exc}")
                return

            if success:
                await ctx.send(f"@{ctx.author.name} Raid auf {target_login} gestartet! (Twitch-Countdown ~90s)")
                
                # Bei Nicht-Partner-Raid: Chat-Nachricht senden
                if not is_partner_raid and hasattr(self._raid_bot, "_send_recruitment_message"):
                    await self._raid_bot._send_recruitment_message(
                        from_broadcaster_login=twitch_login,
                        to_broadcaster_login=target_login,
                        target_stream_data=target,
                    )
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
            """Joint alle Kanäle, die live sind und den Bot autorisiert haben (Partner oder !traid)."""
            with get_conn() as conn:
                # Wir holen alle Partner ODER jeden, der raid_enabled = 1 in twitch_raid_auth hat
                partners = conn.execute(
                    """
                    SELECT DISTINCT s.twitch_login, s.twitch_user_id, a.scopes, l.is_live
                    FROM twitch_streamers s
                    JOIN twitch_raid_auth a ON s.twitch_user_id = a.twitch_user_id
                    LEFT JOIN twitch_live_state l ON s.twitch_user_id = l.twitch_user_id
                    WHERE (
                        (s.manual_verified_permanent = 1 OR s.manual_verified_until IS NOT NULL OR s.manual_verified_at IS NOT NULL)
                        OR a.raid_enabled = 1
                    )
                    AND s.manual_partner_opt_out = 0
                    """
                ).fetchall()

            channels_to_join = []
            for login, uid, scopes_raw, is_live in partners:
                login_norm = (login or "").strip()
                if not login_norm:
                    continue
                scopes = [s.strip().lower() for s in (scopes_raw or "").split() if s.strip()]
                has_chat_scope = any(
                    s in {"user:read:chat", "user:write:chat", "chat:read", "chat:edit"} for s in scopes
                )
                if not has_chat_scope:
                    continue
                if is_live is None or not bool(is_live):
                    continue
                # Normalisieren und prüfen
                normalized_login = login_norm.lower().lstrip("#")
                if normalized_login in self._monitored_streamers:
                    continue
                channels_to_join.append((login_norm, uid))

            if channels_to_join:
                log.info(
                    "Joining %d new LIVE partner channels: %s",
                    len(channels_to_join),
                    ", ".join([c[0] for c in channels_to_join[:10]]),
                )
                for login, uid in channels_to_join:
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
    saved_types = []
    if access_token:
        # Wir speichern nur noch im Format ZWECK@DeadlockBot
        service_access = _KEYRING_SERVICE if _KEYRING_SERVICE.startswith("TWITCH_BOT_TOKEN@") else f"TWITCH_BOT_TOKEN@{_KEYRING_SERVICE}"
        tasks.append(_save_one(service_access, "TWITCH_BOT_TOKEN", access_token))
        saved_types.append("ACCESS_TOKEN")
    if refresh_token:
        # Wir speichern nur noch im Format ZWECK@DeadlockBot
        service_refresh = _KEYRING_SERVICE if _KEYRING_SERVICE.startswith("TWITCH_BOT_REFRESH_TOKEN@") else f"TWITCH_BOT_REFRESH_TOKEN@{_KEYRING_SERVICE}"
        tasks.append(_save_one(service_refresh, "TWITCH_BOT_REFRESH_TOKEN", refresh_token))
        saved_types.append("REFRESH_TOKEN")

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
        log.info("Twitch Bot Tokens (%s) im Windows Credential Manager gespeichert (Dienst: %s).", "+".join(saved_types), _KEYRING_SERVICE)


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
