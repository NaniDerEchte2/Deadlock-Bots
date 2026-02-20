import logging
import re
import time
from datetime import datetime, timezone
from typing import Optional

from ..storage import get_conn
from .constants import (
    DEADLOCK_INVITE_REPLY,
    INVITE_ACCESS_RE,
    INVITE_GAME_CONTEXT_RE,
    _INVITE_QUESTION_CHANNEL_COOLDOWN_SEC,
    _INVITE_QUESTION_RE,
    INVITE_STRONG_ACCESS_RE,
    _INVITE_QUESTION_USER_COOLDOWN_SEC,
    SPAM_FRAGMENTS,
    SPAM_PHRASES,
)

log = logging.getLogger("TwitchStreams.ChatBot")


class ModerationMixin:
    @staticmethod
    def _resolve_message_channel(message):
        """Best-effort channel resolution for TwitchIO 2.x and 3.x messages."""
        if message is None:
            return None
        channel = getattr(message, "channel", None)
        if channel is not None:
            return channel
        return getattr(message, "source_broadcaster", None) or getattr(
            message, "broadcaster", None
        )

    @staticmethod
    def _extract_mentions(content: str) -> list[str]:
        """Extrahiert Twitch-ähnliche @mentions aus einer Nachricht."""
        return re.findall(r"(?<!\w)@([A-Za-z0-9_]{3,25})\b", content or "")

    @staticmethod
    def _looks_like_random_mention_token(token: str) -> bool:
        """
        Fallback-Heuristik, wenn Twitch-User-Lookup nicht möglich ist:
        - mindestens 8 Zeichen
        - nur alphanumerisch (ohne "_")
        - enthält Zahl ODER gemischte Groß-/Kleinschreibung
        """
        normalized = (token or "").strip()
        if len(normalized) < 8:
            return False
        if not re.fullmatch(r"[A-Za-z0-9]+", normalized):
            return False
        has_digit = any(ch.isdigit() for ch in normalized)
        has_lower = any(ch.islower() for ch in normalized)
        has_upper = any(ch.isupper() for ch in normalized)
        return has_digit or (has_lower and has_upper)

    def _is_known_channel_chatter(self, channel_login: str, mention_login: str) -> bool:
        """Prüft, ob ein Login als Chatter im Streamer-Kontext bekannt ist."""
        streamer = (channel_login or "").strip().lower().lstrip("#@")
        mention = (mention_login or "").strip().lower().lstrip("@")
        if not streamer or not mention:
            return False

        now = time.monotonic()
        cache_ttl_sec = 600.0
        cache = getattr(self, "_mention_chatter_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            setattr(self, "_mention_chatter_cache", cache)

        cache_key = (streamer, mention)
        cached = cache.get(cache_key)
        if isinstance(cached, tuple) and len(cached) == 2:
            cached_ts, cached_value = cached
            if now - float(cached_ts) <= cache_ttl_sec:
                return bool(cached_value)

        known = False
        try:
            with get_conn() as conn:
                row = conn.execute(
                    """
                    SELECT 1
                      FROM twitch_session_chatters
                     WHERE streamer_login = ? AND chatter_login = ?
                     LIMIT 1
                    """,
                    (streamer, mention),
                ).fetchone()
                if row is None:
                    row = conn.execute(
                        """
                        SELECT 1
                          FROM twitch_chatter_rollup
                         WHERE streamer_login = ? AND chatter_login = ?
                         LIMIT 1
                        """,
                        (streamer, mention),
                    ).fetchone()
                known = row is not None
        except Exception:
            log.debug(
                "Konnte Mention-Chatter-Check nicht laden (streamer=%s mention=%s)",
                streamer,
                mention,
                exc_info=True,
            )

        cache[cache_key] = (now, known)
        if len(cache) > 4096:
            stale_before = now - (cache_ttl_sec * 4.0)
            stale_keys = []
            for key, value in cache.items():
                if not isinstance(value, tuple) or len(value) != 2:
                    stale_keys.append(key)
                    continue
                if float(value[0]) < stale_before:
                    stale_keys.append(key)
            for key in stale_keys:
                cache.pop(key, None)

        return known

    async def _resolve_existing_twitch_users(
        self, logins: list[str]
    ) -> tuple[set[str], bool]:
        """Löst Logins via Twitch auf. Rückgabe: (gefunden, lookup_ok)."""
        normalized = []
        seen = set()
        for login in logins or []:
            value = (login or "").strip().lower().lstrip("@")
            if value and value not in seen:
                normalized.append(value)
                seen.add(value)
        if not normalized:
            return set(), True

        now = time.monotonic()
        cache_ttl_sec = 21600.0  # 6h
        cache = getattr(self, "_mention_user_exists_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            setattr(self, "_mention_user_exists_cache", cache)

        found = set()
        to_lookup = []
        for login in normalized:
            cached = cache.get(login)
            if isinstance(cached, tuple) and len(cached) == 2:
                cached_ts, exists = cached
                if now - float(cached_ts) <= cache_ttl_sec:
                    if bool(exists):
                        found.add(login)
                    continue
            to_lookup.append(login)

        if not to_lookup:
            return found, True
        if not hasattr(self, "fetch_users"):
            return found, False

        try:
            users = await self.fetch_users(logins=to_lookup)
            resolved = set()
            for user in users or []:
                login = (
                    (getattr(user, "login", None) or getattr(user, "name", None) or "")
                    .strip()
                    .lower()
                )
                if login:
                    resolved.add(login)

            for login in to_lookup:
                exists = login in resolved
                cache[login] = (now, exists)
                if exists:
                    found.add(login)

            if len(cache) > 8192:
                stale_before = now - (cache_ttl_sec * 4.0)
                stale_keys = []
                for key, value in cache.items():
                    if not isinstance(value, tuple) or len(value) != 2:
                        stale_keys.append(key)
                        continue
                    if float(value[0]) < stale_before:
                        stale_keys.append(key)
                for key in stale_keys:
                    cache.pop(key, None)

            return found, True
        except Exception:
            log.debug("Konnte Twitch-User für Mentions nicht auflösen", exc_info=True)
            return found, False

    async def _score_mention_patterns(
        self,
        content: str,
        host_login: str = "",
        *,
        allow_host_bonus: bool = False,
    ) -> tuple[int, list]:
        """Bewertet Mentions. Host-Mentions zählen nur optional als Bonus."""
        raw = (content or "").strip()
        if not raw:
            return 0, []

        mentions = [m.lower() for m in self._extract_mentions(raw)]
        if not mentions:
            return 0, []

        hits = 0
        reasons = []
        normalized_host = (host_login or "").strip().lower().lstrip("#@")

        if allow_host_bonus and normalized_host and normalized_host in mentions:
            hits += 1
            reasons.append("Muster: @host mention")

        candidates = sorted({m for m in mentions if m != normalized_host})
        if not candidates:
            return hits, reasons

        maybe_random = []
        for mention in candidates:
            if self._is_known_channel_chatter(normalized_host, mention):
                continue
            maybe_random.append(mention)

        if not maybe_random:
            return hits, reasons

        existing_users, lookup_ok = await self._resolve_existing_twitch_users(
            maybe_random
        )
        unresolved = [m for m in maybe_random if m not in existing_users]
        if not unresolved:
            return hits, reasons

        if lookup_ok:
            hits += 1
            reasons.append("Muster: @unknown mention")
        elif any(self._looks_like_random_mention_token(m) for m in unresolved):
            hits += 1
            reasons.append("Muster: @ + random chars (fallback)")

        return hits, reasons

    def _calculate_spam_score(self, content: str) -> tuple[int, list]:
        """Berechnet einen Spam-Score. >= SPAM_MIN_MATCHES ist ein Ban."""
        if not content:
            return 0, []

        reasons = []
        raw = content.strip()
        hits = 0

        # Spam-Phrasen haben Priorität (zuverlässiger als Fragmente).
        # Es gilt hier: Phrase ODER Fragment-Fallback, nicht beides zusammen.
        phrase_matched = False

        # Spam-Phrasen (exact): +2 Punkte
        for phrase in SPAM_PHRASES:
            if phrase in raw:
                hits += 2
                reasons.append(f"Phrase(Exact): {phrase}")
                phrase_matched = True
                break  # Nur einmal zählen

        lowered = raw.casefold()
        if not phrase_matched:  # Nur prüfen wenn noch keine exakte Phrase gefunden
            for phrase in SPAM_PHRASES:
                if phrase.casefold() in lowered:
                    hits += 2
                    reasons.append(f"Phrase(Casefold): {phrase}")
                    phrase_matched = True
                    break

        # Fragment-/Keyword-Fallback: nur wenn keine Phrase gematcht wurde.
        # Die kompakte Domain-Form wird wie ein Keyword behandelt, nicht als Extra-Bonus.
        if not phrase_matched:
            fragment_hit = False
            for frag in SPAM_FRAGMENTS:
                if re.search(r"\b" + re.escape(frag.casefold()) + r"\b", lowered):
                    hits += 1
                    reasons.append(f"Fragment(Fallback): {frag}")
                    fragment_hit = True
                    break
            if not fragment_hit:
                compact = re.sub(r"[^a-z0-9]", "", lowered)
                if "streamboocom" in compact:
                    hits += 1
                    reasons.append("Fragment(Fallback): streamboocom (kompakt)")

        # Muster: "viewer [name]": +1 Punkt
        if re.search(r"\bviewer\s+\w+", lowered):
            hits += 1
            reasons.append("Muster: viewer + name")

        return hits, reasons

    def _looks_like_deadlock_access_question(self, content: str) -> bool:
        if not content:
            return False
        raw = content.strip().lower()
        has_deadlock_context = "deadlock" in raw
        has_game_context = bool(INVITE_GAME_CONTEXT_RE.search(raw))
        has_strong_access = bool(INVITE_STRONG_ACCESS_RE.search(raw))
        has_access = bool(INVITE_ACCESS_RE.search(raw))
        if not has_access:
            return False
        has_question = "?" in raw or bool(_INVITE_QUESTION_RE.search(raw))
        if not has_question:
            return False
        has_direct_invite_request = bool(
            re.search(
                r"\b(kannst|kann|koennte|könnte|koenntest|könntest|darfst|darf)\s+du\b",
                raw,
            )
            and has_strong_access
        )
        if (
            has_deadlock_context
            or (has_game_context and has_strong_access)
            or has_direct_invite_request
        ):
            return True
        return False

    async def _maybe_send_deadlock_access_hint(self, message) -> bool:
        """
        Antwortet auf Deadlock-Zugangsfragen mit einem Discord-Invite (mit Cooldown).

        WICHTIG: Nur für PARTNER (nicht Monitored-Only)!
        """
        content = message.content or ""
        if not self._looks_like_deadlock_access_question(content):
            return False
        if content.strip().startswith(self.prefix or "!"):
            return False

        channel = self._resolve_message_channel(message)
        channel_name = (
            getattr(channel, "name", "") or getattr(channel, "login", "") or ""
        )
        login = channel_name.lstrip("#").lower()
        if not login:
            return False

        # WICHTIG: Discord-Invite nur für PARTNER senden!
        from ..partner_utils import is_partner_channel_for_chat_tracking

        if not is_partner_channel_for_chat_tracking(login):
            return False

        now = time.monotonic()
        last_channel = self._last_invite_reply.get(login)
        if (
            last_channel
            and (now - last_channel) < _INVITE_QUESTION_CHANNEL_COOLDOWN_SEC
        ):
            return False

        author = getattr(message, "author", None)
        chatter_login = (getattr(author, "name", "") or "").lower()
        if chatter_login:
            user_key = (login, chatter_login)
            last_user = self._last_invite_reply_user.get(user_key)
            if last_user and (now - last_user) < _INVITE_QUESTION_USER_COOLDOWN_SEC:
                return False
        else:
            user_key = None

        invite, is_specific = await self._get_promo_invite(login)
        if not invite:
            return False

        if channel is None:
            return False

        mention = (
            f"@{getattr(author, 'name', '')} " if getattr(author, "name", None) else ""
        )
        msg = mention + DEADLOCK_INVITE_REPLY.format(invite=invite)
        ok = await self._send_chat_message(channel, msg)
        if ok:
            self._last_invite_reply[login] = now
            if user_key:
                self._last_invite_reply_user[user_key] = now
            # Verhindert direkt nach Invite-Hinweis eine zusaetzliche Promo
            self._last_promo_sent[login] = now
            if is_specific:
                marker = getattr(self, "_mark_streamer_invite_sent", None)
                if callable(marker):
                    marker(login)
        return ok

    async def _get_moderation_context(
        self, twitch_user_id: str
    ) -> tuple[Optional[object], Optional[dict]]:
        """Holt Session + Auth-Header für Moderationscalls."""
        auth_mgr = (
            getattr(self._raid_bot, "auth_manager", None) if self._raid_bot else None
        )
        http_session = (
            getattr(self._raid_bot, "session", None) if self._raid_bot else None
        )
        if not auth_mgr or not http_session:
            return None, None
        try:
            tokens = await auth_mgr.get_tokens_for_user(
                str(twitch_user_id), http_session
            )
            if not tokens:
                return None, None
            access_token = tokens[0]
            headers = {
                "Client-ID": self._client_id,
                "Authorization": f"Bearer {access_token}",
            }
            return http_session, headers
        except Exception:
            log.debug(
                "Konnte Moderations-Kontext nicht laden (%s)",
                twitch_user_id,
                exc_info=True,
            )
            return None, None

    def _record_autoban(
        self,
        *,
        channel_name: str,
        chatter_login: str,
        chatter_id: str,
        content: str,
        status: str = "BANNED",
        reason: str = "",
    ) -> None:
        """Persistiert Auto-Ban-Ereignis oder Verdacht in die passende Review-Logdatei."""
        try:
            status_upper = (status or "").strip().upper()
            target_log = self._autoban_log
            if status_upper.startswith("SUSPICIOUS"):
                target_log = getattr(self, "_suspicious_log", self._autoban_log)

            target_log.parent.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(timezone.utc).isoformat()
            safe_content = content.replace("\n", " ")[:500]
            line = f"{ts}\t[{status}]\t{channel_name}\t{chatter_login or '-'}\t{chatter_id}\t{reason or '-'}\t{safe_content}\n"
            with target_log.open("a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            log.debug("Konnte Auto-Ban Review-Log nicht schreiben", exc_info=True)

    def _normalize_channel_login_safe(self, channel) -> str:
        """Best-effort Normalisierung fuer Channel-Logins (lowercase, ohne #)."""
        name = getattr(channel, "name", "") or ""
        try:
            if hasattr(self, "_normalize_channel_login"):
                return self._normalize_channel_login(name)
        except Exception:
            log.debug("Konnte Channel-Login nicht normalisieren", exc_info=True)
        return name.lower().lstrip("#")

    @staticmethod
    def _looks_like_ban_error(status: Optional[int], text: str) -> bool:
        if not text:
            return False
        lowered = text.lower()
        if "banned" in lowered:
            return True
        # Fallback for older messages that might not include the word "banned"
        if status in {400, 403} and "ban" in lowered:
            return True
        return False

    @staticmethod
    def _should_blacklist_for_source(source: Optional[str]) -> bool:
        if not source:
            return False
        return source.strip().lower() in {"promo", "recruitment", "partner_raid"}

    def _blacklist_streamer_for_source(
        self,
        channel,
        status: Optional[int],
        text: str,
        source: Optional[str],
    ) -> None:
        """Blacklist a streamer when outbound bot messages indicate the bot is banned."""
        source_tag = str(source or "").strip().lower()
        if not self._should_blacklist_for_source(source_tag):
            return

        login = self._normalize_channel_login_safe(channel)
        if not login:
            return
        try:
            if self._is_partner_channel_for_chat_tracking(login):
                log.info(
                    "Blacklist übersprungen für Partner-Channel %s (source=%s)",
                    login,
                    source_tag,
                )
                return
        except Exception:
            log.debug(
                "Partner-Check vor Blacklist fehlgeschlagen fuer %s",
                login,
                exc_info=True,
            )

        raw_id = str(getattr(channel, "id", "") or "").strip()
        target_id = raw_id if raw_id else None
        snippet = (text or "").replace("\n", " ").strip()[:180]
        reason = f"{source_tag}_bot_banned"
        if status is not None:
            reason += f" (HTTP {status})"
        if snippet:
            reason += f": {snippet}"

        raid_bot = getattr(self, "_raid_bot", None)
        if raid_bot and hasattr(raid_bot, "_add_to_blacklist"):
            raid_bot._add_to_blacklist(target_id, login, reason)
        else:
            try:
                with get_conn() as conn:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO twitch_raid_blacklist (target_id, target_login, reason)
                        VALUES (?, ?, ?)
                        """,
                        (target_id, login, reason),
                    )
                    conn.commit()
            except Exception:
                log.debug(
                    "Konnte Bot-Ban-Blacklist nicht schreiben fuer %s",
                    login,
                    exc_info=True,
                )

        log.warning(
            "Bot-Ban erkannt (source=%s): %s auf Raid-Blacklist gesetzt.",
            source_tag,
            login,
        )

    def _blacklist_streamer_for_promo(
        self, channel, status: Optional[int], text: str
    ) -> None:
        """Backward-compatible wrapper for promo ban blacklisting."""
        self._blacklist_streamer_for_source(channel, status, text, source="promo")

    async def _send_announcement(
        self, channel, text: str, color: str = "purple", source: Optional[str] = None
    ) -> bool:
        """Sendet eine Announcement (hervorgehobene Nachricht) via Helix API.

        Erfordert ``moderator:manage:announcements`` Scope.
        Fallback: normale Chat-Nachricht, falls Announcement fehlschlägt.
        """
        b_id = None
        if hasattr(channel, "id"):
            b_id = str(channel.id)
        elif hasattr(channel, "broadcaster") and hasattr(channel.broadcaster, "id"):
            b_id = str(channel.broadcaster.id)
        if not b_id and hasattr(channel, "name"):
            try:
                user = await self.fetch_user(login=channel.name.lstrip("#"))
                if user:
                    b_id = str(user.id)
            except Exception as exc:
                log.debug(
                    "Konnte broadcaster_id aus Channel-Namen nicht aufloesen",
                    exc_info=exc,
                )

        safe_bot_id = self.bot_id_safe or self.bot_id
        if not (b_id and safe_bot_id and self._token_manager):
            log.debug(
                "_send_announcement: fehlende IDs oder Token-Manager, Fallback auf normale Nachricht"
            )
            return await self._send_chat_message(channel, text, source=source)

        import aiohttp

        for attempt in range(2):
            try:
                tokens = await self._token_manager.get_valid_token()
                if not tokens:
                    log.debug("_send_announcement: kein gültiger Token")
                    return await self._send_chat_message(channel, text, source=source)

                access_token, _ = tokens
                url = (
                    f"https://api.twitch.tv/helix/chat/announcements"
                    f"?broadcaster_id={b_id}&moderator_id={safe_bot_id}"
                )
                headers = {
                    "Client-ID": self._client_id,
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                }
                payload = {"message": text, "color": color}

                async with aiohttp.ClientSession() as session:
                    async with session.post(url, headers=headers, json=payload) as r:
                        if r.status in {200, 204}:
                            return True
                        if r.status == 401 and attempt == 0:
                            log.debug("_send_announcement: 401, triggere Token-Refresh")
                            await self._token_manager.get_valid_token(
                                force_refresh=True
                            )
                            continue
                        txt = await r.text()
                        if self._should_blacklist_for_source(
                            source
                        ) and self._looks_like_ban_error(r.status, txt):
                            self._blacklist_streamer_for_source(
                                channel, r.status, txt, source
                            )
                        log.warning(
                            "_send_announcement fehlgeschlagen: HTTP %s - %s, Fallback auf normale Nachricht",
                            r.status,
                            txt,
                        )
                        return await self._send_chat_message(
                            channel, text, source=source
                        )
            except Exception as e:
                log.error(
                    "Fehler bei _send_announcement: %s, Fallback auf normale Nachricht",
                    e,
                )
                return await self._send_chat_message(channel, text, source=source)

        return await self._send_chat_message(channel, text, source=source)

    async def _send_chat_message(
        self, channel, text: str, source: Optional[str] = None
    ) -> bool:
        """Best-effort Chat-Nachricht senden (EventSub-kompatibel)."""
        try:
            # 1. Direktes .send() (z.B. Context, 2.x Channel oder 3.x Broadcaster)
            if channel and hasattr(channel, "send"):
                try:
                    await channel.send(text)
                    return True
                except Exception as exc:
                    if self._should_blacklist_for_source(
                        source
                    ) and self._looks_like_ban_error(None, str(exc)):
                        self._blacklist_streamer_for_source(
                            channel, None, str(exc), source
                        )
                    raise

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
                import aiohttp

                for attempt in range(2):
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
                            "Content-Type": "application/json",
                        }
                        payload = {
                            "broadcaster_id": str(b_id),
                            "sender_id": str(safe_bot_id),
                            "message": text,
                        }

                        async with aiohttp.ClientSession() as session:
                            async with session.post(
                                url, headers=headers, json=payload
                            ) as r:
                                if r.status in {200, 204}:
                                    return True
                                if r.status == 401 and attempt == 0:
                                    log.debug(
                                        "_send_chat_message: 401 in %s, triggere Token-Refresh",
                                        b_id,
                                    )
                                    await self._token_manager.get_valid_token(
                                        force_refresh=True
                                    )
                                    continue
                                txt = await r.text()
                                if self._should_blacklist_for_source(
                                    source
                                ) and self._looks_like_ban_error(r.status, txt):
                                    self._blacklist_streamer_for_source(
                                        channel, r.status, txt, source
                                    )
                                log.warning(
                                    "Twitch hat die Bot-Nachricht abgelehnt: HTTP %s - %s",
                                    r.status,
                                    txt,
                                )
                                return False
                    except Exception as e:
                        log.error("Fehler beim Senden der Helix Chat-Nachricht: %s", e)
                        return False

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
        """Bannt erkannte Spam-Bots und löscht die Nachricht (als Bot)."""
        channel = self._resolve_message_channel(message)
        channel_name = (
            getattr(channel, "name", "") or getattr(channel, "login", "") or ""
        )
        channel_key = self._normalize_channel_login(channel_name)
        if not self._is_partner_channel_for_chat_tracking(channel_key):
            return False
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
        if getattr(author, "moderator", False) or getattr(author, "broadcaster", False):
            return False

        # --- ÄNDERUNG: Wir nutzen jetzt das BOT-Token für Moderation, nicht das Streamer-Token ---
        safe_bot_id = self.bot_id_safe or self.bot_id
        if not safe_bot_id or not self._token_manager:
            log.warning(
                "Spam erkannt in %s, aber kein Bot-ID oder Token-Manager für Auto-Ban verfügbar.",
                channel_name,
            )
            return False

        for attempt in range(2):  # Maximal 2 Versuche (Original + 1 Retry nach Refresh)
            try:
                tokens = await self._token_manager.get_valid_token()
                if not tokens:
                    log.warning(
                        "Spam erkannt in %s, aber kein valides Bot-Token für Auto-Ban verfügbar.",
                        channel_name,
                    )
                    return False
                access_token, _ = tokens

                headers = {
                    "Client-ID": self._client_id,
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                }

                # Wir nutzen eine temporäre Session für die API-Calls des Bots
                import aiohttp

                async with aiohttp.ClientSession() as session:
                    # 1. Nachricht löschen
                    message_id = self._extract_message_id(message)
                    if message_id:
                        try:
                            async with session.delete(
                                "https://api.twitch.tv/helix/moderation/chat",
                                headers=headers,
                                params={
                                    "broadcaster_id": twitch_user_id,
                                    "moderator_id": safe_bot_id,  # Bot ist der Moderator
                                    "message_id": message_id,
                                },
                            ) as resp:
                                if resp.status == 401 and attempt == 0:
                                    log.warning(
                                        "Delete message 401 in %s, triggering refresh...",
                                        channel_name,
                                    )
                                    await self._token_manager.get_valid_token(
                                        force_refresh=True
                                    )
                                    continue  # Retry outer loop

                                if resp.status not in {200, 204}:
                                    txt = await resp.text()
                                    log.debug(
                                        "Konnte Nachricht nicht löschen (Bot-Action) (%s/%s): HTTP %s %s",
                                        channel_name,
                                        message_id,
                                        resp.status,
                                        txt[:180].replace("\n", " "),
                                    )
                        except Exception:
                            log.debug(
                                "Auto-Delete fehlgeschlagen (%s)",
                                channel_name,
                                exc_info=True,
                            )

                    # 2. User bannen
                    try:
                        payload = {
                            "data": {
                                "user_id": chatter_id,
                                "reason": "Automatischer Spam-Ban (Bot-Phrase)",
                            }
                        }
                        async with session.post(
                            "https://api.twitch.tv/helix/moderation/bans",
                            headers=headers,
                            params={
                                "broadcaster_id": twitch_user_id,
                                "moderator_id": safe_bot_id,
                            },  # Bot ist der Moderator
                            json=payload,
                        ) as resp:
                            if resp.status in {200, 201, 202}:
                                log.info(
                                    "Auto-Ban (durch Bot) ausgelöst in %s für %s",
                                    channel_name,
                                    chatter_login or chatter_id,
                                )
                                self._last_autoban[channel_key] = {
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
                                    status="BANNED",
                                )
                                # Nachricht an den Chat senden, WARUM gebannt wurde (wenn nicht silent)
                                silent = False
                                try:
                                    with get_conn() as _conn:
                                        _sb_row = _conn.execute(
                                            "SELECT silent_ban FROM twitch_streamers WHERE twitch_user_id = ?",
                                            (twitch_user_id,),
                                        ).fetchone()
                                        silent = bool(
                                            int((_sb_row[0] if _sb_row else 0) or 0)
                                        )
                                except Exception as exc:
                                    log.debug(
                                        "Konnte silent_ban nicht ermitteln fuer %s",
                                        channel_name,
                                        exc_info=exc,
                                    )
                                if not silent:
                                    await self._send_chat_message(
                                        channel,
                                        f"🛡️ Auto-Mod: {chatter_login} wurde wegen Spam-Verdacht gebannt. (!unban zum Rückgängigmachen)",
                                    )
                                return True

                            if resp.status == 401 and attempt == 0:
                                log.warning(
                                    "Ban user 401 in %s, triggering refresh...",
                                    channel_name,
                                )
                                await self._token_manager.get_valid_token(
                                    force_refresh=True
                                )
                                continue  # Retry outer loop

                            txt = await resp.text()
                            if resp.status == 400 and "already banned" in txt.lower():
                                log.info(
                                    "Auto-Ban in %s übersprungen: %s ist bereits gebannt.",
                                    channel_name,
                                    chatter_login or chatter_id,
                                )
                                self._last_autoban[channel_key] = {
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
                                    status="BANNED",
                                    reason="already_banned",
                                )
                                return True

                            if resp.status == 403:
                                log.warning(
                                    "Auto-Ban fehlgeschlagen in %s (403 Forbidden): Bot ist wahrscheinlich kein Moderator!",
                                    channel_name,
                                )
                            elif resp.status == 401:
                                log.warning(
                                    "Auto-Ban fehlgeschlagen in %s (401 Unauthorized) nach Refresh!",
                                    channel_name,
                                )
                            else:
                                log.warning(
                                    "Auto-Ban fehlgeschlagen in %s (user=%s): HTTP %s %s",
                                    channel_name,
                                    chatter_id,
                                    resp.status,
                                    txt[:180].replace("\n", " "),
                                )
                    except Exception:
                        log.debug(
                            "Auto-Ban Exception in %s", channel_name, exc_info=True
                        )

                # Wenn wir hier sind ohne return True, ist der Ban fehlgeschlagen (und kein 401 Retry möglich)
                break

            except Exception:
                log.error("Fehler im Auto-Ban-Versuch %d", attempt + 1, exc_info=True)
                if attempt == 1:
                    break

        # Wenn wir hier sind, ist Ban fehlgeschlagen
        return False

    async def _unban_user(
        self,
        *,
        broadcaster_id: str,
        target_user_id: str,
        channel_name: str,
        login_hint: str = "",
    ) -> bool:
        """Hebt einen Ban auf (als Bot)."""
        safe_bot_id = self.bot_id_safe or self.bot_id
        if not safe_bot_id or not self._token_manager:
            log.warning(
                "Unban nicht möglich: Keine Bot-Auth/ID verfügbar in %s", channel_name
            )  # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
            return False

        for attempt in range(2):
            try:
                tokens = await self._token_manager.get_valid_token()
                if not tokens:
                    log.warning("Kein valides Bot-Token für Unban verfügbar.")
                    return False
                access_token, _ = tokens

                headers = {
                    "Client-ID": self._client_id,
                    "Authorization": f"Bearer {access_token}",
                }

                import aiohttp

                async with aiohttp.ClientSession() as session:
                    async with session.delete(
                        "https://api.twitch.tv/helix/moderation/bans",
                        headers=headers,
                        params={
                            "broadcaster_id": broadcaster_id,
                            "moderator_id": safe_bot_id,  # Bot ist der Moderator
                            "user_id": target_user_id,
                        },
                    ) as resp:
                        if resp.status in {200, 204}:
                            log.info(
                                "Unban (durch Bot) ausgeführt in %s für %s",
                                channel_name,
                                login_hint or target_user_id,
                            )
                            return True

                        if resp.status == 401 and attempt == 0:
                            log.warning(
                                "Unban 401 in %s, triggering refresh...", channel_name
                            )
                            await self._token_manager.get_valid_token(
                                force_refresh=True
                            )
                            continue

                        txt = await resp.text()
                        log.warning(
                            "Unban fehlgeschlagen in %s (user=%s): HTTP %s %s",
                            channel_name,
                            target_user_id,
                            resp.status,
                            txt[:180].replace("\n", " "),
                        )
                break
            except Exception:
                log.debug(
                    "Unban Exception in %s (Versuch %d)",
                    channel_name,
                    attempt + 1,
                    exc_info=True,
                )
                if attempt == 1:
                    break
        return False

    def _is_partner_channel_for_chat_tracking(self, login: str) -> bool:
        """Nur verifizierte Partner-Channels (ohne Opt-out/Archiv) tracken."""
        if not login:
            return False

        now_mono = time.monotonic()
        cache = getattr(self, "_chat_partner_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            setattr(self, "_chat_partner_cache", cache)
        cache_ttl = float(getattr(self, "_chat_partner_cache_ttl_sec", 60.0) or 60.0)

        cached = cache.get(login)
        if isinstance(cached, tuple) and len(cached) == 2:
            cached_ts, cached_value = cached
            try:
                if now_mono - float(cached_ts) <= cache_ttl:
                    return bool(cached_value)
            except (TypeError, ValueError) as exc:
                log.debug(
                    "Partner-Cache-Eintrag ungueltig fuer %s", login, exc_info=exc
                )

        is_partner = False
        try:
            with get_conn() as conn:
                row = conn.execute(
                    """
                    SELECT is_partner_active
                      FROM twitch_streamers_partner_state
                     WHERE LOWER(twitch_login) = ?
                     LIMIT 1
                    """,
                    (login,),
                ).fetchone()

            if row:
                is_partner = bool(
                    row["is_partner_active"] if hasattr(row, "keys") else row[0]
                )
        except Exception:
            log.debug(
                "Konnte Partner-Status für Chat-Tracking nicht prüfen (%s)",
                login,
                exc_info=True,
            )
            is_partner = False

        cache[login] = (now_mono, is_partner)
        if len(cache) > 2048:
            stale_before = now_mono - max(cache_ttl * 4.0, 30.0)
            stale_keys = [
                key
                for key, value in cache.items()
                if not isinstance(value, tuple)
                or len(value) != 2
                or float(value[0]) < stale_before
            ]
            for key in stale_keys:
                cache.pop(key, None)

        return is_partner

    def _is_target_game_live_for_chat(
        self, login: str, session_id: Optional[int]
    ) -> bool:
        """Returns True when chat persistence should run for the channel."""
        target_game_lower = (
            (getattr(self, "_target_game_lower", "") or "").strip().lower()
        )
        if not target_game_lower:
            return True

        now_mono = time.monotonic()
        cache = getattr(self, "_chat_category_cache", None)
        cache_ttl = float(getattr(self, "_chat_category_cache_ttl_sec", 15.0) or 15.0)
        cached = cache.get(login) if isinstance(cache, dict) else None
        if isinstance(cached, tuple) and len(cached) == 2:
            cached_ts, cached_value = cached
            if now_mono - float(cached_ts) <= cache_ttl:
                return bool(cached_value)

        should_track = False
        try:
            with get_conn() as conn:
                state_row = conn.execute(
                    """
                    SELECT is_live, last_game
                      FROM twitch_live_state
                     WHERE streamer_login = ?
                    """,
                    (login,),
                ).fetchone()

                if state_row:
                    is_live = bool(
                        int(
                            (
                                state_row["is_live"]
                                if hasattr(state_row, "keys")
                                else state_row[0]
                            )
                            or 0
                        )
                    )
                    last_game = (
                        str(
                            (
                                state_row["last_game"]
                                if hasattr(state_row, "keys")
                                else state_row[1]
                            )
                            or ""
                        )
                        .strip()
                        .lower()
                    )
                    should_track = is_live and last_game == target_game_lower
                elif session_id is not None:
                    session_row = conn.execute(
                        """
                        SELECT game_name
                          FROM twitch_stream_sessions
                         WHERE id = ? AND ended_at IS NULL
                        """,
                        (session_id,),
                    ).fetchone()
                    if session_row:
                        game_name = (
                            str(
                                (
                                    session_row["game_name"]
                                    if hasattr(session_row, "keys")
                                    else session_row[0]
                                )
                                or ""
                            )
                            .strip()
                            .lower()
                        )
                        should_track = game_name == target_game_lower
        except Exception:
            log.debug(
                "Konnte Chat-Kategorie-Filter nicht pruefen fuer %s",
                login,
                exc_info=True,
            )
            return False

        if isinstance(cache, dict):
            cache[login] = (now_mono, should_track)
            if len(cache) > 2048:
                stale_before = now_mono - max(cache_ttl * 4.0, 30.0)
                stale_keys = [
                    key
                    for key, value in cache.items()
                    if not isinstance(value, tuple)
                    or len(value) != 2
                    or float(value[0]) < stale_before
                ]
                for key in stale_keys:
                    cache.pop(key, None)

        return should_track

    async def _track_chat_health(self, message) -> None:
        """Loggt Chat-Events für Chat-Gesundheit und Retention-Metriken."""
        channel = self._resolve_message_channel(message)
        channel_name = (
            getattr(channel, "name", "") or getattr(channel, "login", "") or ""
        )
        login = channel_name.lstrip("#").lower()
        if not login:
            return
        if not self._is_partner_channel_for_chat_tracking(login):
            return

        author = getattr(message, "author", None)
        chatter_login = (getattr(author, "name", "") or "").lower()
        if not chatter_login:
            return
        chatter_id = str(getattr(author, "id", "") or "") or None
        content = message.content or ""
        if not isinstance(content, str):
            content = str(content)
        if "\x00" in content:
            content = content.replace("\x00", "")
        message_id = self._extract_message_id(message)
        is_command = content.strip().startswith(self.prefix or "!")

        session_id = self._resolve_session_id(login)
        if session_id is None:
            return
        if not self._is_target_game_live_for_chat(login, session_id):
            return

        ts_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

        with get_conn() as conn:
            # Rohes Chat-Event inkl. Klartext-Nachricht
            conn.execute(
                """
                INSERT INTO twitch_chat_messages (
                    session_id,
                    streamer_login,
                    chatter_login,
                    chatter_id,
                    message_id,
                    message_ts,
                    is_command,
                    content
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    login,
                    chatter_login,
                    chatter_id,
                    message_id,
                    ts_iso,
                    1 if is_command else 0,
                    content,
                ),
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
