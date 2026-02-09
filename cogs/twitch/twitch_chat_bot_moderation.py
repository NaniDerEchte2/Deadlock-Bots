import logging
import re
import time
from datetime import datetime, timezone
from typing import Optional

from .storage import get_conn
from .twitch_chat_bot_constants import (
    _DEADLOCK_INVITE_REPLY,
    _INVITE_ACCESS_RE,
    _INVITE_QUESTION_CHANNEL_COOLDOWN_SEC,
    _INVITE_QUESTION_RE,
    _INVITE_QUESTION_USER_COOLDOWN_SEC,
    _SPAM_FRAGMENTS,
    _SPAM_PHRASES,
)

log = logging.getLogger("TwitchStreams.ChatBot")


class ModerationMixin:
    def _calculate_spam_score(self, content: str) -> tuple[int, list]:
        """Berechnet einen Spam-Score. >= _SPAM_MIN_MATCHES ist ein Ban."""
        if not content:
            return 0, []

        reasons = []
        raw = content.strip()
        hits = 0

        # Spam-Phrasen: +2 Punkte
        for phrase in _SPAM_PHRASES:
            if phrase in raw:
                hits += 2
                reasons.append(f"Phrase(Exact): {phrase}")
                break  # Nur einmal zählen

        lowered = raw.casefold()
        if hits == 0:  # Nur prüfen wenn noch keine exakte Phrase gefunden
            for phrase in _SPAM_PHRASES:
                if phrase.casefold() in lowered:
                    hits += 2
                    reasons.append(f"Phrase(Casefold): {phrase}")
                    break

        # Prüfe Fragmente mit Wortgrenzen: +1 Punkt pro Fragment
        for frag in _SPAM_FRAGMENTS:
            if re.search(r"\b" + re.escape(frag.casefold()) + r"\b", lowered):
                hits += 1
                reasons.append(f"Fragment: {frag}")

        # Muster: "viewer [name]": +1 Punkt
        if re.search(r"\bviewer\s+\w+", lowered):
            hits += 1
            reasons.append("Muster: viewer + name")

        # Kompakte Form "streamboocom": +1 Punkt
        compact = re.sub(r"[^a-z0-9]", "", lowered)
        if "streamboocom" in compact:
            hits += 1
            reasons.append("Muster: streamboocom (kompakt)")

        # NEU: Random @ String Pattern (z.B. @0kyuMlG8): +1 Punkt
        if re.search(r"@[A-Za-z0-9]{8}\b", raw):
            hits += 1
            reasons.append("Muster: @ + 8 random chars")

        return hits, reasons

    def _looks_like_deadlock_access_question(self, content: str) -> bool:
        if not content:
            return False
        raw = content.strip().lower()
        if "deadlock" not in raw:
            return False
        if not _INVITE_ACCESS_RE.search(raw):
            return False
        if "?" in raw or _INVITE_QUESTION_RE.search(raw):
            return True
        return False

    async def _maybe_send_deadlock_access_hint(self, message) -> bool:
        """Antwortet auf Deadlock-Zugangsfragen mit einem Discord-Invite (mit Cooldown)."""
        content = message.content or ""
        if not self._looks_like_deadlock_access_question(content):
            return False
        if content.strip().startswith(self.prefix or "!"):
            return False

        channel_name = getattr(message.channel, "name", "") or ""
        login = channel_name.lstrip("#").lower()
        if not login:
            return False

        now = time.monotonic()
        last_channel = self._last_invite_reply.get(login)
        if last_channel and (now - last_channel) < _INVITE_QUESTION_CHANNEL_COOLDOWN_SEC:
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

        mention = f"@{getattr(author, 'name', '')} " if getattr(author, "name", None) else ""
        msg = mention + _DEADLOCK_INVITE_REPLY.format(invite=invite)
        ok = await self._send_chat_message(message.channel, msg)
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
        """Persistiert Auto-Ban-Ereignis oder Verdacht für spätere Review."""
        try:
            self._autoban_log.parent.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(timezone.utc).isoformat()
            safe_content = content.replace("\n", " ")[:500]
            line = f"{ts}\t[{status}]\t{channel_name}\t{chatter_login or '-'}\t{chatter_id}\t{reason or '-'}\t{safe_content}\n"
            with self._autoban_log.open("a", encoding="utf-8") as f:
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
            pass
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

    def _blacklist_streamer_for_promo(self, channel, status: Optional[int], text: str) -> None:
        """Blacklist a streamer when the bot gets banned due to auto-promo."""
        login = self._normalize_channel_login_safe(channel)
        if not login:
            return

        raw_id = str(getattr(channel, "id", "") or "").strip()
        target_id = raw_id if raw_id else None
        snippet = (text or "").replace("\n", " ").strip()[:180]
        reason = "auto_promo_bot_banned"
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
                log.debug("Konnte Auto-Promo-Blacklist nicht schreiben fuer %s", login, exc_info=True)

        log.warning("Auto-Promo Ban erkannt: %s auf Raid-Blacklist gesetzt.", login)

    async def _send_chat_message(self, channel, text: str, source: Optional[str] = None) -> bool:
        """Best-effort Chat-Nachricht senden (EventSub-kompatibel)."""
        try:
            # 1. Direktes .send() (z.B. Context, 2.x Channel oder 3.x Broadcaster)
            if channel and hasattr(channel, "send"):
                try:
                    await channel.send(text)
                    return True
                except Exception as exc:
                    if source == "promo" and self._looks_like_ban_error(None, str(exc)):
                        self._blacklist_streamer_for_promo(channel, None, str(exc))
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
                            "Content-Type": "application/json"
                        }
                        payload = {
                            "broadcaster_id": str(b_id),
                            "sender_id": str(safe_bot_id),
                            "message": text
                        }

                        async with aiohttp.ClientSession() as session:
                            async with session.post(url, headers=headers, json=payload) as r:
                                if r.status in {200, 204}:
                                    return True
                                if r.status == 401 and attempt == 0:
                                    log.debug("_send_chat_message: 401 in %s, triggere Token-Refresh", b_id)
                                    await self._token_manager.get_valid_token(force_refresh=True)
                                    continue
                                txt = await r.text()
                                if source == "promo" and self._looks_like_ban_error(r.status, txt):
                                    self._blacklist_streamer_for_promo(channel, r.status, txt)
                                log.warning("Twitch hat die Bot-Nachricht abgelehnt: HTTP %s - %s", r.status, txt)
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
        channel_name = getattr(message.channel, "name", "") or ""
        channel_key = self._normalize_channel_login(channel_name)
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

        # --- ÄNDERUNG: Wir nutzen jetzt das BOT-Token für Moderation, nicht das Streamer-Token ---
        safe_bot_id = self.bot_id_safe or self.bot_id
        if not safe_bot_id or not self._token_manager:
            log.warning("Spam erkannt in %s, aber kein Bot-ID oder Token-Manager für Auto-Ban verfügbar.", channel_name)
            return False

        for attempt in range(2):  # Maximal 2 Versuche (Original + 1 Retry nach Refresh)
            try:
                tokens = await self._token_manager.get_valid_token()
                if not tokens:
                    log.warning("Spam erkannt in %s, aber kein valides Bot-Token für Auto-Ban verfügbar.", channel_name)
                    return False
                access_token, _ = tokens

                headers = {
                    "Client-ID": self._client_id,
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json"
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
                                    log.warning("Delete message 401 in %s, triggering refresh...", channel_name)
                                    await self._token_manager.get_valid_token(force_refresh=True)
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
                            log.debug("Auto-Delete fehlgeschlagen (%s)", channel_name, exc_info=True)

                    # 2. User bannen
                    try:
                        payload = {"data": {"user_id": chatter_id, "reason": "Automatischer Spam-Ban (Bot-Phrase)"}}
                        async with session.post(
                            "https://api.twitch.tv/helix/moderation/bans",
                            headers=headers,
                            params={"broadcaster_id": twitch_user_id, "moderator_id": safe_bot_id},  # Bot ist der Moderator
                            json=payload,
                        ) as resp:
                            if resp.status in {200, 201, 202}:
                                log.info("Auto-Ban (durch Bot) ausgelöst in %s für %s", channel_name, chatter_login or chatter_id)
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
                                    status="BANNED"
                                )
                                # Nachricht an den Chat senden, WARUM gebannt wurde
                                await self._send_chat_message(
                                    message.channel,
                                    f"🛡️ Auto-Mod: {chatter_login} wurde wegen Spam-Verdacht gebannt. (!unban zum Rückgängigmachen)"
                                )
                                return True

                            if resp.status == 401 and attempt == 0:
                                log.warning("Ban user 401 in %s, triggering refresh...", channel_name)
                                await self._token_manager.get_valid_token(force_refresh=True)
                                continue  # Retry outer loop

                            txt = await resp.text()
                            if resp.status == 403:
                                log.warning("Auto-Ban fehlgeschlagen in %s (403 Forbidden): Bot ist wahrscheinlich kein Moderator!", channel_name)
                            elif resp.status == 401:
                                log.warning("Auto-Ban fehlgeschlagen in %s (401 Unauthorized) nach Refresh!", channel_name)
                            else:
                                log.warning(
                                    "Auto-Ban fehlgeschlagen in %s (user=%s): HTTP %s %s",
                                    channel_name,
                                    chatter_id,
                                    resp.status,
                                    txt[:180].replace("\n", " "),
                                )
                    except Exception:
                        log.debug("Auto-Ban Exception in %s", channel_name, exc_info=True)

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
            log.warning("Unban nicht möglich: Keine Bot-Auth/ID verfügbar in %s", channel_name)  # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
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
                            log.info("Unban (durch Bot) ausgeführt in %s für %s", channel_name, login_hint or target_user_id)
                            return True

                        if resp.status == 401 and attempt == 0:
                            log.warning("Unban 401 in %s, triggering refresh...", channel_name)
                            await self._token_manager.get_valid_token(force_refresh=True)
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
                log.debug("Unban Exception in %s (Versuch %d)", channel_name, attempt + 1, exc_info=True)
                if attempt == 1:
                    break
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
