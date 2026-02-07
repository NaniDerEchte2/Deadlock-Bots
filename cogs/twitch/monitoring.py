"""Background polling and monitoring helpers for Twitch streams."""

from __future__ import annotations

import asyncio
import secrets
import sqlite3
import time
from datetime import datetime, timezone, timedelta
import os
from typing import Dict, List, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import discord
from discord.ext import tasks

from . import storage
from .constants import (
    INVITES_REFRESH_INTERVAL_HOURS,
    POLL_INTERVAL_SECONDS,
    TWITCH_BRAND_COLOR_HEX,
    TWITCH_BUTTON_LABEL,
    TWITCH_DISCORD_REF_CODE,
    TWITCH_VOD_BUTTON_LABEL,
    TWITCH_TARGET_GAME_NAME,
)
from .logger import log


class TwitchMonitoringMixin:
    """Polling loops and helpers used by the Twitch cog."""

    def _get_target_game_lower(self) -> str:
        target = getattr(self, "_target_game_lower", None)
        if isinstance(target, str) and target:
            return target
        resolved = (TWITCH_TARGET_GAME_NAME or "").strip().lower()
        # Cache for subsequent lookups to avoid repeated normalization
        setattr(self, "_target_game_lower", resolved)
        return resolved

    def _stream_is_in_target_category(self, stream: Optional[dict]) -> bool:
        if not stream:
            return False
        target_game_lower = self._get_target_game_lower()
        if not target_game_lower:
            return False
        game_name = (stream.get("game_name") or "").strip().lower()
        return game_name == target_game_lower

    def _language_filter_values(self) -> List[Optional[str]]:
        filters: Optional[List[str]] = getattr(self, "_language_filters", None)
        if not filters:
            return [None]
        seen: List[str] = []
        for entry in filters:
            normalized = (entry or "").strip().lower()
            if not normalized or normalized in seen:
                continue
            seen.append(normalized)
        return [*seen] or [None]

    def _get_raid_enabled_streamers_for_eventsub(self) -> List[Dict[str, str]]:
        """Broadcaster-Liste für EventSub stream.offline (nur raid_bot_enabled=1)."""
        try:
            with storage.get_conn() as c:
                rows = c.execute(
                    """
                    SELECT twitch_user_id, twitch_login
                      FROM twitch_streamers
                     WHERE raid_bot_enabled = 1
                       AND twitch_user_id IS NOT NULL
                       AND twitch_login IS NOT NULL
                       AND archived_at IS NULL
                    """
                ).fetchall()
            return [
                {
                    "twitch_user_id": str(r["twitch_user_id"] if hasattr(r, "keys") else r[0]),
                    "twitch_login": str(r["twitch_login"] if hasattr(r, "keys") else r[1]).lower(),
                }
                for r in rows
            ]
        except Exception:
            log.debug("EventSub: konnte raid_enabled Streamer nicht laden", exc_info=True)
            return []

    def _get_chat_scope_streamers_for_eventsub(self) -> List[Dict[str, str]]:
        """Broadcaster mit OAuth + Chat-Scopes (aktuell nicht für EventSub genutzt, nur für Info)."""
        try:
            with storage.get_conn() as c:
                rows = c.execute(
                    """
                    SELECT s.twitch_user_id, s.twitch_login, a.scopes
                      FROM twitch_streamers s
                      JOIN twitch_raid_auth a ON s.twitch_user_id = a.twitch_user_id
                     WHERE (s.manual_verified_permanent = 1
                            OR s.manual_verified_until IS NOT NULL
                            OR s.manual_verified_at IS NOT NULL)
                       AND s.manual_partner_opt_out = 0
                       AND s.archived_at IS NULL
                       AND s.twitch_user_id IS NOT NULL
                       AND s.twitch_login IS NOT NULL
                    """
                ).fetchall()
            out: List[Dict[str, str]] = []
            seen: set[str] = set()
            for row in rows:
                user_id = str(row["twitch_user_id"] if hasattr(row, "keys") else row[0]).strip()
                login = str(row["twitch_login"] if hasattr(row, "keys") else row[1]).strip().lower()
                scopes_raw = row["scopes"] if hasattr(row, "keys") else row[2]
                scopes = [s.strip().lower() for s in (scopes_raw or "").split() if s.strip()]
                has_chat_scope = any(
                    s in {"user:read:chat", "user:write:chat", "chat:read", "chat:edit"} for s in scopes
                )
                if not has_chat_scope or not user_id or not login:
                    continue
                key = f"{user_id}:{login}"
                if key in seen:
                    continue
                seen.add(key)
                out.append({"twitch_user_id": user_id, "twitch_login": login})
            return out
        except Exception:
            log.debug("EventSub online: konnte Streamer-Liste nicht laden", exc_info=True)
            return []

    def _get_tracked_logins_for_eventsub(self) -> List[str]:
        """Alle bekannten Streamer-Logins (für Online-Status der Partner bei EventSub)."""
        try:
            with storage.get_conn() as c:
                rows = c.execute(
                    "SELECT twitch_login FROM twitch_streamers WHERE twitch_login IS NOT NULL AND archived_at IS NULL"
                ).fetchall()
            return [str(r["twitch_login"] if hasattr(r, "keys") else r[0]).lower() for r in rows]
        except Exception:
            log.debug("EventSub: konnte tracked Logins nicht laden", exc_info=True)
            return []


    async def _fetch_streams_by_logins_quick(self, logins: List[str]) -> Dict[str, dict]:
        """Hol Live-Streams fœr angegebene Logins (reduziert auf einmal pro EventSub-Offline)."""
        if not getattr(self, "api", None):
            return {}
        streams_by_login: Dict[str, dict] = {}
        logins = [lg for lg in logins if lg]
        if not logins:
            return {}
        for language in self._language_filter_values():
            try:
                streams = await self.api.get_streams_by_logins(logins, language=language)
            except Exception:
                label = language or "any"
                log.debug("EventSub: Streams fetch failed (language=%s)", label, exc_info=True)
                continue
            for stream in streams:
                login = (stream.get("user_login") or "").lower()
                if login:
                    streams_by_login[login] = stream
        return streams_by_login

    def _load_live_state_row(self, login_lower: str) -> Dict:
        """Lädt letzten Live-State aus DB, damit EventSub-Offlines sofort Daten haben."""
        if not login_lower:
            return {}
        try:
            with storage.get_conn() as c:
                row = c.execute(
                    """
                    SELECT is_live, last_seen_at, last_title, last_game, last_viewer_count,
                           last_stream_id, last_started_at, had_deadlock_in_session
                      FROM twitch_live_state
                     WHERE streamer_login = ?
                    """,
                    (login_lower,),
                ).fetchone()
            return dict(row) if row else {}
        except Exception:
            log.debug("EventSub: konnte live_state für %s nicht laden", login_lower, exc_info=True)
            return {}

    async def _on_eventsub_stream_offline(self, broadcaster_id: str, broadcaster_login: Optional[str]) -> None:
        """Direkter Auto-Raid-Trigger bei stream.offline EventSub."""
        if not broadcaster_id:
            return
        login_lower = (broadcaster_login or "").lower()
        # Doppel-Trigger (Polling + EventSub) vermeiden
        throttle = getattr(self, "_eventsub_offline_throttle", None)
        if throttle is None:
            throttle = {}
            setattr(self, "_eventsub_offline_throttle", throttle)
        now = time.time()
        last_ts = throttle.get(broadcaster_id)
        if last_ts and now - last_ts < 90:
            return
        throttle[broadcaster_id] = now

        previous_state = self._load_live_state_row(login_lower)

        # Frische Online-Streams sammeln, damit Auto-Raid Partner erkennen kann
        tracked_logins = self._get_tracked_logins_for_eventsub()
        streams_by_login = await self._fetch_streams_by_logins_quick(tracked_logins)

        try:
            await self._handle_auto_raid_on_offline(
                login=login_lower or broadcaster_login or "",
                twitch_user_id=broadcaster_id,
                previous_state=previous_state,
                streams_by_login=streams_by_login,
            )
        except Exception:
            log.exception("EventSub: Auto-Raid offline handling failed for %s", broadcaster_login or broadcaster_id)

    async def _start_eventsub_listener(self):
        """Startet konsolidierte EventSub WebSocket Listener (verteilt auf bis zu 3 Verbindungen)."""
        if getattr(self, "_eventsub_ws_started", False):
            log.debug("EventSub WS Listener bereits gestartet, überspringe.")
            return
        setattr(self, "_eventsub_ws_started", True)
        
        if not getattr(self, "api", None):
            log.warning("EventSub WS: Keine API vorhanden, Listener wird nicht gestartet.")
            return
            
        try:
            await self.bot.wait_until_ready()
        except Exception:
            log.exception("EventSub WS: wait_until_ready fehlgeschlagen")
            return

        # 1. Broadcaster sammeln
        # Strategie: Polling erkennt Go-Live (alle 15s), dann wird stream.offline dynamisch hinzugefügt
        # Beim Start: Subscribe stream.offline NUR für aktuell live Streams
        # So sparen wir Subscription-Slots: nur Live-Streams werden überwacht
        raid_enabled_streamers = self._get_raid_enabled_streamers_for_eventsub()

        if not raid_enabled_streamers:
            log.info("EventSub WS: Keine Streamer für EventSub monitoring gefunden.")
            setattr(self, "_eventsub_ws_started", False)  # Reset flag
            return

        # Hole aktuellen Live-Status für alle raid-enabled Streamer
        raid_logins = [s["twitch_login"] for s in raid_enabled_streamers]
        currently_live_streams = {}
        try:
            live_streams = await self.api.get_streams_by_logins(raid_logins)
            for stream in live_streams:
                login_lower = (stream.get("user_login") or "").lower()
                if login_lower:
                    currently_live_streams[login_lower] = stream
            log.info(
                "EventSub WS: %d von %d raid-enabled Streamern sind aktuell live",
                len(currently_live_streams),
                len(raid_enabled_streamers)
            )
        except Exception:
            log.exception("EventSub WS: Konnte Live-Status nicht abrufen, subscribe keine stream.offline beim Start")
        
        # 2. Token Resolver vorbereiten
        token_resolver = None
        bot_token_mgr = getattr(self, "_bot_token_manager", None)
        if bot_token_mgr:
            async def _resolve_bot_token(_user_id: str) -> Optional[str]:
                try:
                    token, _ = await bot_token_mgr.get_valid_token()
                    return token
                except Exception:
                    log.debug("EventSub WS: konnte Bot-Token nicht laden", exc_info=True)
                    return None
            token_resolver = _resolve_bot_token
        else:
            log.warning("EventSub WS: Kein Token Manager vorhanden, Subscriptions könnten fehlschlagen.")

        from .eventsub_ws import EventSubWSListener
        
        # Wir verwalten eine Liste von Listenern (max 3)
        listeners: List[EventSubWSListener] = []
        setattr(self, "_eventsub_ws_listeners", listeners)

        # Callbacks vorbereiten
        async def _offline_cb(bid: str, login: str, _event: dict):
            try:
                await self._on_eventsub_stream_offline(bid, login)
                # stream.offline Subscription bleibt aktiv bis zum nächsten Restart
                # oder bis der Stream wieder live geht (dann wird sie eh neu erstellt)
            except Exception:
                log.exception("EventSub WS: Offline-Callback fehlgeschlagen für %s", login)

        # stream.online wird NICHT mehr über EventSub überwacht!
        # Polling (_tick) erkennt Go-Live und ruft dann _handle_stream_went_live() auf
        async def _handle_stream_went_live(bid: str, login: str):
            """
            Wird vom Polling aufgerufen wenn ein Stream live geht.
            Subscribed stream.offline und joined Chat-Bot.
            """
            try:
                # 1. Chat-Bot joinen (falls Partner mit Chat-Scope)
                chat_bot = getattr(self, "_twitch_chat_bot", None)
                if chat_bot:
                    login_norm = login or ""
                    if not login_norm:
                        try:
                            with storage.get_conn() as c:
                                row = c.execute(
                                    "SELECT twitch_login FROM twitch_streamers WHERE twitch_user_id = ?",
                                    (bid,)
                                ).fetchone()
                            if row:
                                login_norm = str(row[0]).lower()
                        except Exception:
                            pass
                    if login_norm:
                        monitored = getattr(chat_bot, "_monitored_streamers", set())
                        if login_norm not in monitored:
                            success = await chat_bot.join(login_norm, channel_id=bid)
                            if success:
                                log.info("Polling: Chat-Bot joined %s (%s) nach Go-Live", login_norm, bid)

                # 2. Dynamisch stream.offline Subscription hinzufügen
                # Jetzt wo der Stream live ist, wollen wir wissen, wann er offline geht (für Auto-Raid)
                listeners_list = getattr(self, "_eventsub_ws_listeners", [])
                target_listener = None
                for l in listeners_list:
                    if l.has_capacity and not l.is_failed:
                        target_listener = l
                        break

                if target_listener:
                    # Hole Bot Token
                    bot_token_mgr = getattr(self, "_bot_token_manager", None)
                    if bot_token_mgr:
                        try:
                            token, _ = await bot_token_mgr.get_valid_token()
                            if token:
                                token = token.strip()
                                if token.lower().startswith("oauth:"):
                                    token = token[6:]

                                success = await target_listener.add_subscription_dynamic(
                                    sub_type="stream.offline",
                                    broadcaster_id=bid,
                                    oauth_token=token
                                )
                                if success:
                                    log.info(
                                        "Polling: stream.offline dynamisch hinzugefügt für %s (jetzt live → warte auf offline)",
                                        login or bid
                                    )
                        except Exception:
                            log.exception("Polling: Konnte stream.offline nicht dynamisch hinzufügen für %s", login or bid)
                else:
                    log.warning("Polling: Kein EventSub Listener mit Kapazität für stream.offline von %s", login or bid)

            except Exception:
                log.exception("Polling: Go-Live Handler fehlgeschlagen für %s", login or bid)

        # Speichere Handler als Instanz-Attribut damit Polling darauf zugreifen kann
        setattr(self, "_handle_stream_went_live", _handle_stream_went_live)

        async def _raid_cb(to_bid: str, to_login: str, event: dict):
            """
            Callback für channel.raid EventSub.

            Wird gefeuert, wenn ein Raid beim Ziel-Broadcaster ankommt.
            """
            try:
                raid_bot = getattr(self, "_raid_bot", None)
                if not raid_bot:
                    log.debug("EventSub WS: Raid-Bot nicht verfügbar für channel.raid von %s", to_login)
                    return

                from_login = (event.get("from_broadcaster_user_login") or "").strip().lower()
                viewer_count = int(event.get("viewers") or 0)

                if not from_login:
                    log.warning("EventSub WS: channel.raid event ohne from_broadcaster_user_login")
                    return

                log.info(
                    "EventSub WS: channel.raid Event empfangen: %s -> %s (%d viewers)",
                    from_login,
                    to_login,
                    viewer_count
                )

                await raid_bot.on_raid_arrival(
                    to_broadcaster_id=to_bid,
                    to_broadcaster_login=to_login,
                    from_broadcaster_login=from_login,
                    viewer_count=viewer_count,
                )
            except Exception:
                log.exception("EventSub WS: Raid-Callback fehlgeschlagen für %s", to_login or to_bid)

        def get_or_create_listener() -> Optional[EventSubWSListener]:
            # Versuche existierenden Listener mit Platz (< 10 cost per transport limit)
            for l in listeners:
                if l.cost < 10:
                    return l
            # Wenn kein Platz und < 3 Listener (Twitch erlaubt max 3 WebSocket Transports), erstelle neuen
            if len(listeners) < 3:
                new_l = EventSubWSListener(self.api, log, token_resolver=token_resolver)
                new_l.set_callback("stream.offline", _offline_cb)
                new_l.set_callback("channel.raid", _raid_cb)
                listeners.append(new_l)
                return new_l
            return None

        # 3. Subscriptions verteilen (stream.offline NUR für aktuell live Streams)
        # Go-Live Detection erfolgt über Polling (_tick), das dann _handle_stream_went_live() aufruft
        # channel.raid wird dynamisch erstellt wenn ein Raid gestartet wird
        subs_added = 0
        for entry in raid_enabled_streamers:
            bid = entry.get("twitch_user_id")
            login = entry.get("twitch_login", "").lower()
            if not bid:
                continue

            # Subscribe stream.offline NUR wenn Stream aktuell live ist
            if login in currently_live_streams:
                l = get_or_create_listener()
                if l:
                    l.add_subscription("stream.offline", str(bid))
                    subs_added += 1
                    log.debug("EventSub WS: stream.offline beim Start subscribed für %s (aktuell live)", login)
                else:
                    log.error("EventSub WS: Limit erreicht (3 Connections, alle voll). Konnte stream.offline für %s nicht abonnieren.", login)

        log.info(
            "EventSub WS: %d stream.offline Subscriptions auf %d WebSocket(s) verteilt (nur für live Streams). "
            "Go-Live Detection erfolgt via Polling.",
            subs_added, len(listeners)
        )

        # 4. Alle Listener starten
        tasks = []
        for i, l in enumerate(listeners):
            log.info("EventSub WS: Starte Listener #%d...", i + 1)
            tasks.append(asyncio.create_task(l.run(), name=f"twitch.eventsub.ws.{i}"))
        
        if tasks:
            try:
                await asyncio.gather(*tasks)
            except asyncio.CancelledError:
                log.info("EventSub WS: Alle Listener wurden abgebrochen")
                raise
            except Exception:
                log.exception("EventSub WS: Ein oder mehrere Listener beendet mit Fehler")
                setattr(self, "_eventsub_ws_started", False)


    async def subscribe_raid_target_dynamic(self, broadcaster_id: str, broadcaster_login: str) -> bool:
        """
        Erstellt dynamisch eine channel.raid EventSub subscription für einen spezifischen Broadcaster.

        Diese Methode wird aufgerufen, wenn wir einen Raid starten, um zu erkennen,
        wann der Raid beim Ziel ankommt.

        Returns:
            True wenn die Subscription erfolgreich erstellt wurde, False sonst.
        """
        listeners: List[EventSubWSListener] = getattr(self, "_eventsub_ws_listeners", [])
        if not listeners:
            log.warning("EventSub: Keine aktiven Listener verfügbar für dynamische raid subscription")
            return False

        # Finde einen Listener mit Kapazität
        target_listener = None
        for l in listeners:
            if l.has_capacity and not l.is_failed:
                target_listener = l
                break

        if not target_listener:
            log.error(
                "EventSub: Kein verfügbarer Listener mit Kapazität für channel.raid subscription (target: %s)",
                broadcaster_login
            )
            return False

        # Hole Bot Token für die Subscription
        bot_token_mgr = getattr(self, "_bot_token_manager", None)
        if not bot_token_mgr:
            log.error("EventSub: Kein Bot Token Manager verfügbar")
            return False

        try:
            token, _ = await bot_token_mgr.get_valid_token()
            if not token:
                log.error("EventSub: Konnte Bot-Token nicht laden für channel.raid subscription")
                return False

            # Strip "oauth:" prefix wenn vorhanden
            token = token.strip()
            if token.lower().startswith("oauth:"):
                token = token[6:]

        except Exception:
            log.exception("EventSub: Token-Abruf fehlgeschlagen für channel.raid subscription")
            return False

        # Session ID aus dem Listener holen (wir müssen das via API machen, nicht über den Listener)
        # Da der Listener bereits läuft, müssen wir die subscription direkt via API hinzufügen
        # Das ist ein Problem - wir brauchen die session_id, die der Listener hat

        # Workaround: Wir nutzen die stored session_id vom letzten Connect
        session_id = getattr(target_listener, "_session_id", None)
        if not session_id:
            log.error("EventSub: Listener hat keine session_id für dynamische subscription")
            return False

        try:
            condition = {"broadcaster_user_id": str(broadcaster_id)}
            await self.api.subscribe_eventsub_websocket(
                session_id=session_id,
                sub_type="channel.raid",
                condition=condition,
                oauth_token=token,
            )
            log.info(
                "EventSub: Dynamische channel.raid subscription erstellt für %s (ID: %s)",
                broadcaster_login,
                broadcaster_id
            )
            return True

        except Exception as e:
            log.error(
                "EventSub: Dynamische channel.raid subscription fehlgeschlagen für %s: %s",
                broadcaster_login,
                str(e)
            )
            return False

    async def _start_eventsub_offline_listener(self):
        """Kompatibilitäts-Stub (wird nun über _start_eventsub_listener erledigt)."""
        await self._start_eventsub_listener()

    async def _start_eventsub_online_listener(self):
        """Kompatibilitäts-Stub (wird nun über _start_eventsub_listener erledigt)."""
        pass

    @tasks.loop(seconds=POLL_INTERVAL_SECONDS)
    async def poll_streams(self):
        if self.api is None:
            return
        try:
            await self._tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Polling-Tick fehlgeschlagen")

    @poll_streams.before_loop
    async def _before_poll(self):
        await self.bot.wait_until_ready()

    @tasks.loop(hours=INVITES_REFRESH_INTERVAL_HOURS)
    async def invites_refresh(self):
        try:
            await self._refresh_all_invites()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Invite-Refresh fehlgeschlagen")

    @invites_refresh.before_loop
    async def _before_invites(self):
        await self.bot.wait_until_ready()

    async def _ensure_category_id(self):
        if self.api is None:
            return
        try:
            self._category_id = await self.api.get_category_id(TWITCH_TARGET_GAME_NAME)
            if self._category_id:
                log.info("Deadlock category_id = %s", self._category_id)
        except Exception:
            log.exception("Konnte Twitch-Kategorie-ID nicht ermitteln")

    async def _tick(self):
        """Ein Tick: tracked Streamer + Kategorie-Streams prüfen, Postings/DB aktualisieren, Stats loggen."""
        if self.api is None:
            return

        if not self._category_id:
            await self._ensure_category_id()

        partner_logins: set[str] = set()
        try:
            with storage.get_conn() as c:
                rows = c.execute(
                    "SELECT twitch_login, twitch_user_id, require_discord_link, "
                    "       manual_verified_permanent, manual_verified_until, archived_at "
                    "FROM twitch_streamers"
                ).fetchall()
            tracked: List[Dict[str, object]] = []
            now_utc = datetime.now(timezone.utc)
            for row in rows:
                row_dict = dict(row)
                login = str(row_dict.get("twitch_login") or "").strip()
                if not login:
                    continue
                user_id = str(row_dict.get("twitch_user_id") or "").strip()
                require_link = bool(row_dict.get("require_discord_link"))
                archived_at_raw = row_dict.get("archived_at")
                archived_dt: Optional[datetime] = None
                if archived_at_raw:
                    try:
                        archived_dt = datetime.fromisoformat(str(archived_at_raw))
                    except Exception:
                        archived_dt = None
                is_archived = archived_dt is not None
                is_verified = False
                try:
                    is_verified = self._is_partner_verified(row_dict, now_utc)
                except Exception:
                    log.debug("Konnte Verifizierungsstatus für %s nicht bestimmen", login, exc_info=True)

                tracked.append(
                    {
                        "login": login,
                        "twitch_user_id": user_id,
                        "require_link": require_link,
                        "is_verified": is_verified,
                        "archived_at": archived_at_raw,
                        "is_archived": is_archived,
                    }
                )
                login_lower = login.lower()
                if login_lower and is_verified and not is_archived:
                    partner_logins.add(login_lower)
        except Exception:
            log.exception("Konnte tracked Streamer nicht aus DB lesen")
            tracked = []
            partner_logins = set()

        logins = [str(entry.get("login") or "") for entry in tracked if entry.get("login")]
        language_filters = self._language_filter_values()
        streams_by_login: Dict[str, dict] = {}

        if logins:
            for language in language_filters:
                try:
                    streams = await self.api.get_streams_by_logins(logins, language=language)
                except Exception:
                    label = language or "any"
                    log.exception("Konnte Streams für tracked Logins nicht abrufen (language=%s)", label)
                    continue
                for stream in streams:
                    login = (stream.get("user_login") or "").lower()
                    if login:
                        streams_by_login[login] = stream

        for login, stream in list(streams_by_login.items()):
            if login in partner_logins:
                stream["is_partner"] = True

        category_streams: List[dict] = []
        if self._category_id:
            collected: Dict[str, dict] = {}
            for language in language_filters:
                remaining = self._category_sample_limit - len(collected)
                if remaining <= 0:
                    break
                try:
                    streams = await self.api.get_streams_by_category(
                        self._category_id,
                        language=language,
                        limit=max(1, remaining),
                    )
                except Exception:
                    label = language or "any"
                    log.exception("Konnte Kategorie-Streams nicht abrufen (language=%s)", label)
                    continue
                for stream in streams:
                    login = (stream.get("user_login") or "").lower()
                    if login and login not in collected:
                        collected[login] = stream
            category_streams = list(collected.values())

        for stream in category_streams:
            login = (stream.get("user_login") or "").lower()
            if login in partner_logins:
                stream["is_partner"] = True

        try:
            await self._process_postings(tracked, streams_by_login)
        except Exception:
            log.exception("Fehler in _process_postings")

        self._tick_count += 1
        if self._tick_count % self._log_every_n == 0:
            try:
                await self._log_stats(streams_by_login, category_streams)
            except Exception:
                log.exception("Fehler beim Stats-Logging")

        # Partner-Rekrutierung (intern rate-limitiert auf 30 min)
        try:
            await self._run_partner_recruit(category_streams)
        except Exception:
            log.exception("Fehler bei Partner-Rekrutierung")

    async def _process_postings(
        self,
        tracked: List[Dict[str, object]],
        streams_by_login: Dict[str, dict],
    ):
        notify_ch: Optional[discord.TextChannel] = None
        if self._notify_channel_id:
            notify_ch = self.bot.get_channel(self._notify_channel_id) or None  # type: ignore[assignment]

        now_utc = datetime.now(tz=timezone.utc)
        now_iso = now_utc.isoformat(timespec="seconds")
        pending_state_rows: List[
            tuple[
                str,
                str,
                int,
                str,
                Optional[str],
                Optional[str],
                int,
                Optional[str],
                Optional[str],
                Optional[str],
                Optional[str],
                int,
                Optional[int],
                Optional[str],
            ]
        ] = []

        with storage.get_conn() as c:
            live_state_rows = c.execute("SELECT * FROM twitch_live_state").fetchall()

        live_state: Dict[str, dict] = {}
        for row in live_state_rows:
            row_dict = dict(row)
            key = str(row_dict.get("streamer_login") or "").lower()
            if key:
                live_state[key] = row_dict

        target_game_lower = self._get_target_game_lower()

        for entry in tracked:
            login = str(entry.get("login") or "").strip()
            if not login:
                continue

            referral_url = self._build_referral_url(login)
            login_lower = login.lower()
            stream = streams_by_login.get(login_lower)
            previous_state = live_state.get(login_lower, {})
            archived_at_raw = entry.get("archived_at")
            is_archived = bool(entry.get("is_archived"))
            was_live = bool(previous_state.get("is_live", 0))
            is_live = bool(stream)

            # Go-Live Detection: Subscribe stream.offline für raid-enabled Streamer
            if not was_live and is_live and twitch_user_id:
                # Stream ist gerade live gegangen!
                handler = getattr(self, "_handle_stream_went_live", None)
                if handler:
                    # Checke ob der Streamer raid_bot_enabled hat
                    try:
                        with storage.get_conn() as c:
                            raid_enabled_row = c.execute(
                                "SELECT raid_bot_enabled FROM twitch_streamers WHERE twitch_user_id = ?",
                                (twitch_user_id,)
                            ).fetchone()
                        if raid_enabled_row and bool(raid_enabled_row[0]):
                            # Asynchron aufrufen (fire-and-forget, blockiert nicht den Tick)
                            asyncio.create_task(
                                handler(twitch_user_id, login_lower),
                                name=f"golive.{login_lower}"
                            )
                    except Exception:
                        log.debug("Go-Live: Konnte raid_enabled Status nicht checken für %s", login_lower, exc_info=True)

            # Auto-Entarchivierung sobald jemand wieder streamt
            if is_live and is_archived:
                try:
                    await self._dashboard_archive(login, "unarchive")
                    is_archived = False
                    entry["is_archived"] = False
                except Exception:
                    log.debug("Auto-Unarchive fehlgeschlagen für %s", login, exc_info=True)
            previous_game = (previous_state.get("last_game") or "").strip()
            previous_game_lower = previous_game.lower()
            was_deadlock = previous_game_lower == target_game_lower
            twitch_user_id = str(entry.get("twitch_user_id") or "").strip() or None
            stream_started_at_value = self._extract_stream_start(stream, previous_state)
            previous_stream_id = (previous_state.get("last_stream_id") or "").strip()
            current_stream_id_raw = stream.get("id") if stream else ""
            current_stream_id = str(current_stream_id_raw or "").strip()
            stream_id_value = current_stream_id or previous_stream_id or None
            had_deadlock_prev = bool(int(previous_state.get("had_deadlock_in_session", 0) or 0))
            active_session_id: Optional[int] = None
            previous_last_deadlock_seen = (previous_state.get("last_deadlock_seen_at") or "").strip() or None

            if is_live and stream:
                try:
                    active_session_id = await self._ensure_stream_session(
                        login=login_lower,
                        stream=stream,
                        previous_state=previous_state,
                        twitch_user_id=twitch_user_id,
                    )
                except Exception:
                    log.exception("Konnte Streamsitzung nicht starten: %s", login)
            elif was_live and not is_live:
                try:
                    await self._finalize_stream_session(login=login_lower, reason="offline")
                except Exception:
                    log.exception("Konnte Streamsitzung nicht abschliessen: %s", login)
            elif not is_live and previous_state.get("active_session_id"):
                try:
                    await self._finalize_stream_session(login=login_lower, reason="stale")
                except Exception:
                    log.debug("Konnte alte Session nicht bereinigen: %s", login, exc_info=True)

            if not was_live:
                had_deadlock_prev = False
            elif is_live and previous_stream_id and current_stream_id and previous_stream_id != current_stream_id:
                had_deadlock_prev = False

            message_id_previous = str(previous_state.get("last_discord_message_id") or "").strip() or None
            message_id_to_store = message_id_previous
            tracking_token_previous = (
                str(previous_state.get("last_tracking_token") or "").strip() or None
            )
            tracking_token_to_store = tracking_token_previous

            need_link = bool(entry.get("require_link"))
            is_verified = bool(entry.get("is_verified"))

            game_name = (stream.get("game_name") or "").strip() if stream else ""
            game_name_lower = game_name.lower()
            is_deadlock = is_live and bool(target_game_lower) and game_name_lower == target_game_lower
            had_deadlock_in_session = had_deadlock_prev or is_deadlock
            had_deadlock_to_store = had_deadlock_in_session if is_live else False
            last_title_value = (stream.get("title") if stream else previous_state.get("last_title")) or None
            last_game_value = (game_name or previous_state.get("last_game") or "").strip() or None
            last_viewer_count_value = (
                int(stream.get("viewer_count") or 0)
                if stream
                else int(previous_state.get("last_viewer_count") or 0)
            )
            last_deadlock_seen_at_value: Optional[str] = None
            if is_deadlock:
                last_deadlock_seen_at_value = now_iso
            elif had_deadlock_to_store and previous_last_deadlock_seen:
                last_deadlock_seen_at_value = previous_last_deadlock_seen

            should_post = (
                notify_ch is not None
                and is_deadlock
                and (not was_live or not was_deadlock or not message_id_previous)
                and is_verified
                and not is_archived
            )

            if should_post:
                referral_url = self._build_referral_url(login)
                display_name = stream.get("user_name") or login
                message_prefix: List[str] = []
                if self._alert_mention:
                    message_prefix.append(self._alert_mention)
                stream_title = (stream.get("title") or "").strip()
                live_announcement = f"**{display_name}** ist live! Schau ueber den Button unten rein."
                if stream_title:
                    live_announcement = f"{live_announcement} - {stream_title}"
                message_prefix.append(live_announcement)
                content = " ".join(part for part in message_prefix if part).strip()

                embed = self._build_live_embed(login, stream)
                new_tracking_token = self._generate_tracking_token()
                view = self._build_live_view(
                    login,
                    referral_url,
                    new_tracking_token,
                )

                try:
                    message = await notify_ch.send(content=content or None, embed=embed, view=view)
                except Exception:
                    log.exception("Konnte Go-Live-Posting nicht senden: %s", login)
                else:
                    message_id_to_store = str(message.id)
                    tracking_token_to_store = new_tracking_token
                    if view is not None:
                        view.bind_to_message(channel_id=getattr(notify_ch, "id", None), message_id=message.id)
                        self._register_live_view(
                            tracking_token=new_tracking_token,
                            view=view,
                            message_id=message.id,
                        )
                    # Store notification text if we have an active session
                    if active_session_id:
                        try:
                            with storage.get_conn() as c:
                                c.execute(
                                    "UPDATE twitch_stream_sessions SET notification_text = ? WHERE id = ?",
                                    (content or "", active_session_id),
                                )
                        except Exception:
                            log.debug("Could not save notification text for %s", login, exc_info=True)

            ended_deadlock_posting = (
                notify_ch is not None
                and message_id_previous
                and (not is_live or not is_deadlock)
            )
            should_auto_raid = (
                notify_ch is not None
                and was_live
                and not is_live
                and had_deadlock_in_session
                and not is_archived
            )

            # Auto-Raid beim Offline-Gehen (Throttle gemeinsam mit EventSub-Pfad)
            if should_auto_raid:
                raid_uid = str(twitch_user_id or previous_state.get("twitch_user_id") or "")
                throttle = getattr(self, "_eventsub_offline_throttle", None)
                if throttle is None:
                    throttle = {}
                    setattr(self, "_eventsub_offline_throttle", throttle)
                last_ts = throttle.get(raid_uid)
                if not (last_ts and time.time() - last_ts < 90):
                    throttle[raid_uid] = time.time()
                    await self._handle_auto_raid_on_offline(
                        login=login,
                        twitch_user_id=raid_uid,
                        previous_state=previous_state,
                        streams_by_login=streams_by_login,
                    )

            if ended_deadlock_posting:
                display_name = (
                    (stream.get("user_name") if stream else previous_state.get("streamer_login"))
                    or login
                )
                try:
                    message_id_int = int(message_id_previous)
                except (TypeError, ValueError):
                    message_id_int = None

                if message_id_int is None:
                    log.warning("Ungültige Message-ID für Deadlock-Ende bei %s: %r", login, message_id_previous)
                else:
                    try:
                        fetched_message = await notify_ch.fetch_message(message_id_int)
                    except discord.NotFound:
                        log.warning(
                            "Deadlock-Ende-Posting nicht mehr vorhanden für %s (ID %s)",
                            login,
                            message_id_previous,
                        )
                        message_id_to_store = None
                        tracking_token_to_store = None
                        self._drop_live_view(tracking_token_previous)
                    except Exception:
                        log.exception("Konnte Deadlock-Ende-Posting nicht laden: %s", login)
                    else:
                        preview_image_url = await self._get_latest_vod_preview_url(
                            login=login,
                            twitch_user_id=twitch_user_id or previous_state.get("twitch_user_id"),
                        )

                        ended_content = f"**{display_name}** ist OFFLINE - VOD per Button."
                        offline_embed = self._build_offline_embed(
                            login=login,
                            display_name=display_name,
                            last_title=last_title_value,
                            last_game=last_game_value,
                            preview_image_url=preview_image_url,
                        )
                        offline_view = self._build_offline_link_view(referral_url, label=TWITCH_VOD_BUTTON_LABEL)
                        try:
                            await fetched_message.edit(content=ended_content, embed=offline_embed, view=offline_view)
                        except Exception:
                            log.exception(
                                "Konnte Deadlock-Ende-Posting nicht aktualisieren: %s", login
                            )
                        else:
                            message_id_to_store = None
                            tracking_token_to_store = None
                            self._drop_live_view(tracking_token_previous)

            db_user_id = twitch_user_id or previous_state.get("twitch_user_id") or login_lower
            db_user_id = str(db_user_id)
            db_message_id = str(message_id_to_store) if message_id_to_store else None
            db_streamer_login = login_lower

            pending_state_rows.append(
                (
                    db_user_id,
                    db_streamer_login,
                    int(is_live),
                    now_iso,
                    last_title_value,
                    last_game_value,
                    last_viewer_count_value,
                    db_message_id,
                    tracking_token_to_store,
                    stream_id_value,
                    stream_started_at_value,
                    int(had_deadlock_to_store),
                    active_session_id,
                    last_deadlock_seen_at_value,
                )
            )

            if need_link and self._alert_channel_id and (now_utc.minute % 10 == 0) and is_live:
                # Platzhalter für deinen Profil-/Panel-Check
                pass

        await self._persist_live_state_rows(pending_state_rows)
        await self._auto_archive_inactive_streamers()

    async def _persist_live_state_rows(
        self,
        rows: List[
            tuple[
                str,
                str,
                int,
                str,
                Optional[str],
                Optional[str],
                int,
                Optional[str],
                Optional[str],
                Optional[str],
                Optional[str],
                int,
                Optional[int],
            ]
        ],
    ) -> None:
        if not rows:
            return

        retry_delay = 0.5
        for attempt in range(3):
            try:
                with storage.get_conn() as c:
                    c.executemany(
                        "INSERT OR REPLACE INTO twitch_live_state "
                        "("
                        "twitch_user_id, streamer_login, is_live, last_seen_at, last_title, last_game, "
                        "last_viewer_count, last_discord_message_id, last_tracking_token, last_stream_id, "
                        "last_started_at, had_deadlock_in_session, active_session_id, last_deadlock_seen_at"
                        ") "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        rows,
                    )
                return
            except sqlite3.OperationalError as exc:
                locked = "locked" in str(exc).lower()
                if not locked or attempt == 2:
                    log.exception(
                        "Konnte Live-State-Updates nicht speichern (%s Eintraege)",
                        len(rows),
                    )
                    return
                await asyncio.sleep(retry_delay)
                retry_delay *= 2

    async def _auto_archive_inactive_streamers(self, *, days: int = 10) -> None:
        """
        Archiviert Partner automatisch, wenn sie länger als `days` Tage nicht gestreamt haben.
        Läuft maximal alle 15 Minuten, um DB-Load gering zu halten.
        """
        now = datetime.now(timezone.utc)
        last_run = getattr(self, "_last_archive_check", 0.0)
        if time.time() - last_run < 900:
            return
        setattr(self, "_last_archive_check", time.time())

        cutoff = now - timedelta(days=days)

        try:
            target_game = (os.getenv("TWITCH_TARGET_GAME_NAME") or TWITCH_TARGET_GAME_NAME or "").strip()
            with storage.get_conn() as c:
                rows = c.execute(
                    """
                    SELECT s.twitch_login,
                           s.archived_at,
                           MAX(
                               CASE
                                 WHEN LOWER(COALESCE(sess.game_name,'')) = LOWER(?)
                                 THEN COALESCE(sess.ended_at, sess.started_at)
                               END
                           ) AS last_deadlock_stream_at
                      FROM twitch_streamers s
                      LEFT JOIN twitch_stream_sessions sess
                        ON LOWER(sess.streamer_login) = LOWER(s.twitch_login)
                     WHERE s.manual_partner_opt_out = 0
                       AND (s.manual_verified_permanent = 1
                            OR s.manual_verified_until IS NOT NULL
                            OR s.manual_verified_at IS NOT NULL)
                    GROUP BY s.twitch_login, s.archived_at
                    """,
                    (target_game,),
                ).fetchall()
        except Exception:
            log.debug("Auto-Archivierung: konnte Streamer-Liste nicht laden", exc_info=True)
            return

        for row in rows:
            try:
                login = (row["twitch_login"] if hasattr(row, "keys") else row[0] or "").strip()
            except Exception:
                continue
            if not login:
                continue

            archived_at = row["archived_at"] if hasattr(row, "keys") else row[1]
            if archived_at:
                continue

            last_stream_raw = row["last_deadlock_stream_at"] if hasattr(row, "keys") else row[2]
            if not last_stream_raw:
                # Keine Historie -> keine automatische Archivierung
                continue

            try:
                last_dt = datetime.fromisoformat(str(last_stream_raw).replace("Z", "+00:00"))
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
            except Exception:
                log.debug("Auto-Archivierung: Datum unlesbar für %s (%r)", login, last_stream_raw, exc_info=True)
                continue

            if last_dt < cutoff:
                try:
                    await self._dashboard_archive(login, "archive")
                    log.info(
                        "Auto-archiviert %s (letzter Stream %s, cutoff %s)",
                        login,
                        last_dt.date().isoformat(),
                        cutoff.date().isoformat(),
                    )
                except Exception:
                    log.debug("Auto-Archivierung fehlgeschlagen für %s", login, exc_info=True)

    @staticmethod
    def _parse_dt(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except Exception:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    def _extract_stream_start(self, stream: Optional[dict], previous_state: dict) -> Optional[str]:
        candidate = None
        if stream:
            candidate = stream.get("started_at") or stream.get("start_time")
        if not candidate:
            candidate = previous_state.get("last_started_at")
        dt = self._parse_dt(candidate)
        if dt:
            return dt.isoformat(timespec="seconds")
        return None

    def _get_active_sessions_cache(self) -> Dict[str, int]:
        cache = getattr(self, "_active_sessions", None)
        if cache is None:
            cache = {}
            setattr(self, "_active_sessions", cache)
        return cache

    def _rehydrate_active_sessions(self) -> None:
        cache = self._get_active_sessions_cache()
        cache.clear()
        try:
            with storage.get_conn() as c:
                rows = c.execute(
                    "SELECT id, streamer_login FROM twitch_stream_sessions WHERE ended_at IS NULL"
                ).fetchall()
        except Exception:
            log.debug("Konnte offene Twitch-Sessions nicht laden", exc_info=True)
            return
        for row in rows:
            try:
                session_id = int(row["id"] if hasattr(row, "keys") else row[0])
                login = str(row["streamer_login"] if hasattr(row, "keys") else row[1]).lower()
            except Exception:
                continue
            if login:
                cache[login] = session_id

    def _lookup_open_session_id(self, login: str) -> Optional[int]:
        try:
            with storage.get_conn() as c:
                row = c.execute(
                    "SELECT id FROM twitch_stream_sessions WHERE streamer_login = ? AND ended_at IS NULL "
                    "ORDER BY started_at DESC LIMIT 1",
                    (login.lower(),),
                ).fetchone()
        except Exception:
            log.debug("Lookup offene Session fehlgeschlagen fuer %s", login, exc_info=True)
            return None
        if not row:
            return None
        session_id = int(row["id"] if hasattr(row, "keys") else row[0])
        cache = self._get_active_sessions_cache()
        cache[login.lower()] = session_id
        return session_id

    def _get_active_session_id(self, login: str) -> Optional[int]:
        cache = self._get_active_sessions_cache()
        cached = cache.get(login.lower())
        if cached:
            return cached
        return self._lookup_open_session_id(login)

    async def _ensure_stream_session(
        self,
        *,
        login: str,
        stream: dict,
        previous_state: dict,
        twitch_user_id: Optional[str],
    ) -> Optional[int]:
        login_lower = login.lower()
        stream_id = str(stream.get("id") or "").strip() or None

        session_id = self._get_active_session_id(login_lower)
        if session_id:
            try:
                with storage.get_conn() as c:
                    row = c.execute(
                        "SELECT stream_id FROM twitch_stream_sessions WHERE id = ?",
                        (session_id,),
                    ).fetchone()
                current_stream_id = str(row["stream_id"] if hasattr(row, "keys") else row[0] or "").strip() if row else ""
            except Exception:
                current_stream_id = ""
            if current_stream_id and stream_id and current_stream_id != stream_id:
                await self._finalize_stream_session(login=login_lower, reason="restarted")
                session_id = None

        if session_id:
            return session_id

        followers_start = await self._fetch_followers_total_safe(
            twitch_user_id=twitch_user_id,
            login=login_lower,
            stream=stream,
        )
        started_at_iso = self._extract_stream_start(stream, previous_state)
        stream_title = str(stream.get("title") or "").strip()
        language = str(stream.get("language") or "").strip()
        is_mature = bool(stream.get("is_mature"))
        tags_list = stream.get("tags") or []
        tags_str = ",".join(tags_list) if isinstance(tags_list, list) else ""

        return self._start_stream_session(
            login=login_lower,
            stream=stream,
            started_at_iso=started_at_iso,
            twitch_user_id=twitch_user_id,
            followers_start=followers_start,
            title=stream_title,
            language=language,
            is_mature=is_mature,
            tags=tags_str,
        )

    def _start_stream_session(
        self,
        *,
        login: str,
        stream: dict,
        started_at_iso: Optional[str],
        twitch_user_id: Optional[str],
        followers_start: Optional[int],
        title: str = "",
        language: str = "",
        is_mature: bool = False,
        tags: str = "",
    ) -> Optional[int]:
        start_ts = started_at_iso or datetime.now(timezone.utc).isoformat(timespec="seconds")
        viewer_count = int(stream.get("viewer_count") or 0)
        stream_id = str(stream.get("id") or "").strip() or None
        game_name = (stream.get("game_name") or "").strip() or None
        had_deadlock_initial = 1 if self._stream_is_in_target_category(stream) else 0
        session_id: Optional[int] = None
        try:
            with storage.get_conn() as c:
                c.execute(
                    """
                    INSERT INTO twitch_stream_sessions (
                        streamer_login, stream_id, started_at, start_viewers, peak_viewers,
                        end_viewers, avg_viewers, samples, followers_start, stream_title,
                        language, is_mature, tags, game_name, had_deadlock_in_session
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        login,
                        stream_id,
                        start_ts,
                        viewer_count,
                        viewer_count,
                        viewer_count,
                        float(viewer_count),
                        0,
                        followers_start,
                        title,
                        language,
                        1 if is_mature else 0,
                        tags,
                        game_name,
                        had_deadlock_initial,
                    ),
                )
                session_id = int(c.execute("SELECT last_insert_rowid()").fetchone()[0])
                c.execute(
                    "UPDATE twitch_live_state SET active_session_id = ? WHERE streamer_login = ?",
                    (session_id, login),
                )
        except Exception:
            log.debug("Konnte neue Twitch-Session nicht speichern: %s", login, exc_info=True)
            return None
        if session_id is not None:
            self._get_active_sessions_cache()[login] = session_id
        return session_id

    def _record_session_sample(self, *, login: str, stream: dict) -> None:
        session_id = self._get_active_session_id(login)
        if session_id is None:
            return
        now_dt = datetime.now(timezone.utc)
        viewer_count = int(stream.get("viewer_count") or 0)
        try:
            with storage.get_conn() as c:
                session_row = c.execute(
                    "SELECT started_at, samples, avg_viewers, start_viewers, peak_viewers "
                    "FROM twitch_stream_sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if not session_row:
                    return
                start_dt = self._parse_dt(session_row["started_at"] if hasattr(session_row, "keys") else session_row[0]) or now_dt
                minutes_from_start = int(max(0, (now_dt - start_dt).total_seconds() // 60))
                c.execute(
                    """
                    INSERT OR REPLACE INTO twitch_session_viewers
                        (session_id, ts_utc, minutes_from_start, viewer_count)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        now_dt.isoformat(timespec="seconds"),
                        minutes_from_start,
                        viewer_count,
                    ),
                )
                samples = int(session_row["samples"] if hasattr(session_row, "keys") else session_row[1] or 0)
                avg_prev = float(session_row["avg_viewers"] if hasattr(session_row, "keys") else session_row[2] or 0.0)
                new_samples = samples + 1
                new_avg = ((avg_prev * samples) + viewer_count) / max(1, new_samples)
                start_viewers = int(session_row["start_viewers"] if hasattr(session_row, "keys") else session_row[3] or 0) or viewer_count
                peak_viewers = int(session_row["peak_viewers"] if hasattr(session_row, "keys") else session_row[4] or 0)
                peak_viewers = max(peak_viewers, viewer_count)
                c.execute(
                    """
                    UPDATE twitch_stream_sessions
                       SET samples = ?, avg_viewers = ?, peak_viewers = ?, end_viewers = ?, start_viewers = ?
                     WHERE id = ?
                    """,
                    (
                        new_samples,
                        new_avg,
                        peak_viewers,
                        viewer_count,
                        start_viewers,
                        session_id,
                    ),
                )
        except Exception:
            log.debug("Konnte Session-Sample nicht speichern fuer %s", login, exc_info=True)

    async def _finalize_stream_session(self, *, login: str, reason: str = "done") -> None:
        login_lower = login.lower()
        cache = self._get_active_sessions_cache()
        session_id = cache.pop(login_lower, None) or self._lookup_open_session_id(login_lower)
        if session_id is None:
            return

        now_dt = datetime.now(timezone.utc)
        try:
            with storage.get_conn() as c:
                session_row = c.execute(
                    "SELECT * FROM twitch_stream_sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
        except Exception:
            log.debug("Konnte Session nicht laden fuer Abschluss: %s", login, exc_info=True)
            return
        if not session_row:
            return

        def _row_val(row, key, idx, default=None):
            if hasattr(row, "keys"):
                try:
                    return row[key]
                except Exception:
                    return default
            try:
                return row[idx]
            except Exception:
                return default

        started_at_raw = _row_val(session_row, "started_at", 3, None)
        start_dt = self._parse_dt(started_at_raw) or now_dt
        duration_seconds = int(max(0, (now_dt - start_dt).total_seconds()))

        try:
            with storage.get_conn() as c:
                viewer_rows = c.execute(
                    "SELECT minutes_from_start, viewer_count FROM twitch_session_viewers WHERE session_id = ? ORDER BY ts_utc",
                    (session_id,),
                ).fetchall()
        except Exception:
            viewer_rows = []

        def _retention_at(minutes: int, start_viewers: int) -> Optional[float]:
            if start_viewers <= 0:
                return None
            best: Optional[tuple[int, int]] = None
            for row in viewer_rows:
                mins = int(_row_val(row, "minutes_from_start", 0, 0) or 0)
                val = int(_row_val(row, "viewer_count", 1, 0) or 0)
                if mins < minutes:
                    continue
                if best is None or mins < best[0]:
                    best = (mins, val)
            if best is None and viewer_rows:
                last = viewer_rows[-1]
                best = (
                    int(_row_val(last, "minutes_from_start", 0, 0) or 0),
                    int(_row_val(last, "viewer_count", 1, 0) or 0),
                )
            if best is None:
                return None
            return max(0.0, min(1.0, best[1] / start_viewers))

        start_viewers = int(_row_val(session_row, "start_viewers", 6, 0) or 0)
        end_viewers = int(_row_val(session_row, "end_viewers", 8, 0) or 0)
        peak_viewers = int(_row_val(session_row, "peak_viewers", 7, 0) or 0)
        avg_viewers = float(_row_val(session_row, "avg_viewers", 9, 0.0) or 0.0)
        samples = int(_row_val(session_row, "samples", 10, 0) or 0)

        if viewer_rows:
            end_viewers = int(_row_val(viewer_rows[-1], "viewer_count", 1, end_viewers) or end_viewers)
            peak_viewers = max(peak_viewers, *(int(_row_val(vr, "viewer_count", 1, 0) or 0) for vr in viewer_rows))
            samples = max(samples, len(viewer_rows))
            try:
                avg_viewers = sum(int(_row_val(vr, "viewer_count", 1, 0) or 0) for vr in viewer_rows) / max(
                    1, len(viewer_rows)
                )
            except Exception as exc:
                log.debug("Konnte Durchschnitts-Viewerzahl nicht berechnen", exc_info=exc)

        retention_5 = _retention_at(5, start_viewers)
        retention_10 = _retention_at(10, start_viewers)
        retention_20 = _retention_at(20, start_viewers)

        dropoff_pct: Optional[float] = None
        dropoff_label = ""
        prev_val = start_viewers or (viewer_rows[0]["viewer_count"] if viewer_rows else 0)
        for row in viewer_rows:
            current_val = int(_row_val(row, "viewer_count", 1, 0) or 0)
            mins = int(_row_val(row, "minutes_from_start", 0, 0) or 0)
            if prev_val > 0 and current_val < prev_val:
                delta = prev_val - current_val
                pct = delta / prev_val
                if dropoff_pct is None or pct > dropoff_pct:
                    dropoff_pct = pct
                    dropoff_label = f"t={mins}m ({prev_val}->{current_val})"
            prev_val = current_val

        try:
            with storage.get_conn() as c:
                chatter_row = c.execute(
                    """
                    SELECT COUNT(*) AS uniq,
                           SUM(is_first_time_global) AS firsts
                      FROM twitch_session_chatters
                     WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone()
        except Exception:
            chatter_row = None
        unique_chatters = int(_row_val(chatter_row, "uniq", 0, 0) or 0) if chatter_row else 0
        first_time_chatters = int(_row_val(chatter_row, "firsts", 1, 0) or 0) if chatter_row else 0
        returning_chatters = max(0, unique_chatters - first_time_chatters)

        followers_start = _row_val(session_row, "followers_start", 19, None)

        twitch_user_id: Optional[str] = None
        had_deadlock_state = False
        try:
            with storage.get_conn() as c:
                state_row = c.execute(
                    "SELECT twitch_user_id, last_game, had_deadlock_in_session FROM twitch_live_state WHERE streamer_login = ?",
                    (login_lower,),
                ).fetchone()
            if state_row is not None:
                twitch_user_id = _row_val(state_row, "twitch_user_id", 0, None)
                last_game_value = _row_val(state_row, "last_game", 1, None)
                had_deadlock_state = bool(int(_row_val(state_row, "had_deadlock_in_session", 2, 0) or 0))
            else:
                last_game_value = None
        except Exception:
            last_game_value = None
            twitch_user_id = None
            had_deadlock_state = False

        followers_end = await self._fetch_followers_total_safe(
            twitch_user_id=twitch_user_id,
            login=login_lower,
            stream=None,
        )
        follower_delta = None
        if followers_start is not None and followers_end is not None:
            follower_delta = int(followers_end) - int(followers_start)

        target_game_lower = self._get_target_game_lower()
        last_game_lower = (last_game_value or "").strip().lower() if last_game_value else ""
        had_deadlock_session = had_deadlock_state or (
            bool(target_game_lower) and last_game_lower == target_game_lower
        )

        try:
            with storage.get_conn() as c:
                c.execute(
                    """
                    UPDATE twitch_stream_sessions
                       SET ended_at = ?,
                           duration_seconds = ?,
                           end_viewers = ?,
                           peak_viewers = ?,
                           avg_viewers = ?,
                           samples = ?,
                           retention_5m = ?,
                           retention_10m = ?,
                           retention_20m = ?,
                           dropoff_pct = ?,
                           dropoff_label = ?,
                           unique_chatters = ?,
                           first_time_chatters = ?,
                           returning_chatters = ?,
                           followers_end = ?,
                           follower_delta = ?,
                           notes = ?,
                           had_deadlock_in_session = ?,
                           game_name = COALESCE(game_name, ?)
                     WHERE id = ?
                    """,
                    (
                        now_dt.isoformat(timespec="seconds"),
                        duration_seconds,
                        end_viewers,
                        peak_viewers,
                        avg_viewers,
                        samples,
                        retention_5,
                        retention_10,
                        retention_20,
                        dropoff_pct,
                        dropoff_label,
                        unique_chatters,
                        first_time_chatters,
                        returning_chatters,
                        followers_end,
                        follower_delta,
                        reason,
                        1 if had_deadlock_session else 0,
                        last_game_value,
                        session_id,
                    ),
                )
                c.execute(
                    "UPDATE twitch_live_state SET active_session_id = NULL WHERE streamer_login = ?",
                    (login_lower,),
                )
        except Exception:
            log.debug("Konnte Session-Abschluss nicht speichern: %s", login_lower, exc_info=True)
        finally:
            cache.pop(login_lower, None)

    async def _fetch_followers_total_safe(
        self,
        *,
        twitch_user_id: Optional[str],
        login: str,
        stream: Optional[dict],
    ) -> Optional[int]:
        if self.api is None:
            return None
        user_id = twitch_user_id
        if not user_id and stream:
            user_id = stream.get("user_id")

        user_token: Optional[str] = None
        try:
            if hasattr(self, "_raid_bot") and self._raid_bot and self.api is not None:
                session = self.api.get_http_session()
                result = await self._raid_bot.auth_manager.get_valid_token_for_login(login, session)
                if result:
                    auth_user_id, token = result
                    user_id = user_id or auth_user_id
                    user_token = token
        except Exception:
            log.debug("Konnte OAuth-Token fuer Follower-Check nicht laden: %s", login, exc_info=True)

        if not user_id:
            return None
        try:
            return await self.api.get_followers_total(str(user_id), user_token=user_token)
        except Exception:
            log.debug("Follower-Abfrage fehlgeschlagen fuer %s", login, exc_info=True)
            return None

    async def _log_stats(self, streams_by_login: Dict[str, dict], category_streams: List[dict]):
        now_utc = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")

        try:
            with storage.get_conn() as c:
                for stream in streams_by_login.values():
                    if not self._stream_is_in_target_category(stream):
                        continue
                    login = (stream.get("user_login") or "").lower()
                    viewers = int(stream.get("viewer_count") or 0)
                    is_partner = 1 if stream.get("is_partner") else 0
                    c.execute(
                        "INSERT INTO twitch_stats_tracked (ts_utc, streamer, viewer_count, is_partner) VALUES (?, ?, ?, ?)",
                        (now_utc, login, viewers, is_partner),
                    )
        except Exception:
            log.exception("Konnte tracked-Stats nicht loggen")

        try:
            for stream in streams_by_login.values():
                if not self._stream_is_in_target_category(stream):
                    continue
                login = (stream.get("user_login") or "").lower()
                self._record_session_sample(login=login, stream=stream)
        except Exception:
            log.debug("Konnte Session-Metrik nicht loggen", exc_info=True)

        try:
            with storage.get_conn() as c:
                for stream in category_streams:
                    login = (stream.get("user_login") or "").lower()
                    viewers = int(stream.get("viewer_count") or 0)
                    is_partner = 1 if stream.get("is_partner") else 0
                    c.execute(
                        "INSERT INTO twitch_stats_category (ts_utc, streamer, viewer_count, is_partner) VALUES (?, ?, ?, ?)",
                        (now_utc, login, viewers, is_partner),
                    )
        except Exception:
            log.exception("Konnte category-Stats nicht loggen")

    async def _get_latest_vod_preview_url(self, *, login: str, twitch_user_id: Optional[str]) -> Optional[str]:
        """Hole das juengste VOD-Thumbnail; faellt bei Fehler still auf None."""
        if self.api is None:
            return None
        try:
            return await self.api.get_latest_vod_thumbnail(user_id=twitch_user_id, login=login)
        except Exception:
            log.exception("Konnte VOD-Thumbnail nicht laden: %s", login)
            return None

    def _build_live_embed(self, login: str, stream: dict) -> discord.Embed:
        """Erzeuge ein Discord-Embed für das Go-Live-Posting mit Stream-Vorschau."""

        display_name = stream.get("user_name") or login
        game = stream.get("game_name") or TWITCH_TARGET_GAME_NAME
        title = stream.get("title") or "Live!"
        viewer_count = int(stream.get("viewer_count") or 0)

        timestamp = datetime.now(tz=timezone.utc)
        started_at_raw = stream.get("started_at")
        if isinstance(started_at_raw, str) and started_at_raw:
            try:
                timestamp = datetime.fromisoformat(started_at_raw.replace("Z", "+00:00"))
            except ValueError as exc:
                log.debug("Ungültiger started_at-Wert '%s': %s", started_at_raw, exc)

        embed = discord.Embed(
            title=f"{display_name} ist LIVE in {game}!",
            description=title,
            colour=discord.Color(TWITCH_BRAND_COLOR_HEX),
            timestamp=timestamp,
        )

        embed.add_field(name="Viewer", value=str(viewer_count), inline=True)
        embed.add_field(name="Kategorie", value=game, inline=True)

        thumbnail_url = (stream.get("thumbnail_url") or "").strip()
        if thumbnail_url:
            thumbnail_url = thumbnail_url.replace("{width}", "1280").replace("{height}", "720")
            cache_bust = int(datetime.now(tz=timezone.utc).timestamp())
            embed.set_image(url=f"{thumbnail_url}?rand={cache_bust}")

        embed.set_footer(text="Auf Twitch ansehen fuer mehr Deadlock-Action!")
        embed.set_author(name=f"LIVE: {display_name}")

        return embed

    def _build_offline_embed(
        self,
        *,
        login: str,
        display_name: str,
        last_title: Optional[str],
        last_game: Optional[str],
        preview_image_url: Optional[str],
    ) -> discord.Embed:
        """Offline-Overlay: gleicher Stil wie live, aber klar als VOD markiert."""

        game = last_game or TWITCH_TARGET_GAME_NAME or "Twitch"
        description = last_title or "Letzten Stream als VOD ansehen."

        embed = discord.Embed(
            title=f"{display_name} ist OFFLINE",
            description=description,
            colour=discord.Color(TWITCH_BRAND_COLOR_HEX),
            timestamp=datetime.now(tz=timezone.utc),
        )

        embed.add_field(name="Status", value="OFFLINE", inline=True)
        embed.add_field(name="Kategorie", value=game, inline=True)
        embed.add_field(name="Hinweis", value="VOD ueber den Button abrufen.", inline=False)

        if preview_image_url:
            embed.set_image(url=preview_image_url)

        embed.set_footer(text="Letzten Stream auf Twitch ansehen.")
        embed.set_author(name=f"OFFLINE: {display_name}")

        return embed

    def _build_offline_link_view(self, referral_url: str, *, label: Optional[str] = None) -> discord.ui.View:
        """Offline-Ansicht: einfacher Link-Button ohne Tracking."""
        view = discord.ui.View(timeout=None)
        view.add_item(
            discord.ui.Button(
                label=label or TWITCH_BUTTON_LABEL,
                style=discord.ButtonStyle.link,
                url=referral_url,
            )
        )
        return view

    async def cog_load(self) -> None:
        await super().cog_load()
        spawner = getattr(self, "_spawn_bg_task", None)
        if callable(spawner):
            spawner(self._register_persistent_live_views(), "twitch.register_live_views")
        else:
            asyncio.create_task(self._register_persistent_live_views(), name="twitch.register_live_views")

    def _build_live_view(
        self,
        streamer_login: str,
        referral_url: str,
        tracking_token: str,
    ) -> Optional["_TwitchLiveAnnouncementView"]:
        """Create a persistent view that tracks button clicks before redirecting."""
        if not tracking_token:
            return None
        return _TwitchLiveAnnouncementView(
            cog=self,
            streamer_login=streamer_login,
            referral_url=referral_url,
            tracking_token=tracking_token,
        )

    @staticmethod
    def _generate_tracking_token() -> str:
        return secrets.token_hex(8)

    def _build_referral_url(self, login: str) -> str:
        """Append the configured referral parameter to the Twitch URL."""
        normalized_login = (login or "").strip()
        base_url = f"https://www.twitch.tv/{normalized_login}" if normalized_login else "https://www.twitch.tv/"
        ref_code = (TWITCH_DISCORD_REF_CODE or "").strip()
        if not ref_code:
            return base_url
        parsed = urlparse(base_url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query["ref"] = ref_code
        encoded = urlencode(query)
        return urlunparse(parsed._replace(query=encoded))

    async def _register_persistent_live_views(self) -> None:
        """Re-register live announcement views after a restart."""
        if not self._notify_channel_id:
            return
        try:
            await self.bot.wait_until_ready()
        except Exception:
            log.exception("wait_until_ready für Twitch-Views fehlgeschlagen")
            return

        try:
            with storage.get_conn() as c:
                rows = c.execute(
                    "SELECT streamer_login, last_discord_message_id, last_tracking_token "
                    "FROM twitch_live_state "
                    "WHERE last_discord_message_id IS NOT NULL AND last_tracking_token IS NOT NULL"
                ).fetchall()
        except Exception:
            log.exception("Konnte persistente Twitch-Views nicht registrieren")
            return

        for row in rows:
            login = (row["streamer_login"] or "").strip()
            token = (row["last_tracking_token"] or "").strip()
            message_id_raw = row["last_discord_message_id"]
            if not login or not token or not message_id_raw:
                continue
            try:
                message_id = int(message_id_raw)
            except (TypeError, ValueError):
                continue
            referral_url = self._build_referral_url(login)
            view = self._build_live_view(login, referral_url, token)
            if view is None:
                continue
            view.bind_to_message(channel_id=self._notify_channel_id, message_id=message_id)
            self._register_live_view(tracking_token=token, view=view, message_id=message_id)

    def _get_live_view_registry(self) -> Dict[str, "_TwitchLiveAnnouncementView"]:
        registry = getattr(self, "_live_view_registry", None)
        if registry is None:
            registry = {}
            setattr(self, "_live_view_registry", registry)
        return registry

    def _register_live_view(
        self,
        *,
        tracking_token: str,
        view: "_TwitchLiveAnnouncementView",
        message_id: int,
    ) -> None:
        if not tracking_token:
            return
        registry = self._get_live_view_registry()
        registry[tracking_token] = view
        try:
            self.bot.add_view(view, message_id=message_id)
        except Exception:
            log.exception("Konnte View für Twitch-Posting %s nicht registrieren", message_id)

    def _drop_live_view(self, tracking_token: Optional[str]) -> None:
        if not tracking_token:
            return
        registry = self._get_live_view_registry()
        view = registry.pop(tracking_token, None)
        if view is None:
            return
        
        # discord.py hat kein natives remove_view am Bot-Objekt. 
        # view.stop() reicht aus, um die Interaktionen zu beenden.
        view.stop()
        log.debug("Live-View gestoppt und aus Registry entfernt: %s", tracking_token)

    def _log_link_click(
        self,
        *,
        interaction: discord.Interaction,
        view: "_TwitchLiveAnnouncementView",
    ) -> None:
        clicked_at = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
        user = interaction.user
        user_id = str(getattr(user, "id", "") or "") or None
        username = str(user) if user else None
        guild_id = str(interaction.guild_id) if interaction.guild_id else None
        channel_source = interaction.channel_id or view.channel_id
        channel_id = str(channel_source) if channel_source else None
        if interaction.message and interaction.message.id:
            message_id = str(interaction.message.id)
        elif view.message_id:
            message_id = str(view.message_id)
        else:
            message_id = None
        ref_code = (TWITCH_DISCORD_REF_CODE or "").strip() or None

        try:
            with storage.get_conn() as c:
                c.execute(
                    """
                    INSERT INTO twitch_link_clicks (
                        clicked_at,
                        streamer_login,
                        tracking_token,
                        discord_user_id,
                        discord_username,
                        guild_id,
                        channel_id,
                        message_id,
                        ref_code,
                        source_hint
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        clicked_at,
                        view.streamer_login.lower(),
                        view.tracking_token,
                        user_id,
                        username,
                        guild_id,
                        channel_id,
                        message_id,
                        ref_code,
                        "live_button",
                    ),
                )
        except Exception:
            log.exception("Konnte Twitch-Link-Klick nicht speichern")

    async def _handle_tracked_button_click(
        self,
        interaction: discord.Interaction,
        view: "_TwitchLiveAnnouncementView",
    ) -> None:
        try:
            self._log_link_click(interaction=interaction, view=view)
        except Exception:
            log.exception("Konnte Klick nicht loggen")

        content = f"Hier ist dein Twitch-Link für **{view.streamer_login}**."
        response_view = _TwitchReferralLinkView(view.referral_url)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(content, view=response_view, ephemeral=True)
            else:
                await interaction.response.send_message(content, view=response_view, ephemeral=True)
        except Exception:
            log.exception("Antwort mit Referral-Link fehlgeschlagen")


class _TwitchReferralLinkView(discord.ui.View):
    """Ephemeral view with a direct Twitch hyperlink."""

    def __init__(self, referral_url: str):
        super().__init__(timeout=60)
        self.add_item(
            discord.ui.Button(
                label=TWITCH_BUTTON_LABEL,
                style=discord.ButtonStyle.link,
                url=referral_url,
            )
        )


class _TrackedTwitchButton(discord.ui.Button):
    def __init__(self, parent: "_TwitchLiveAnnouncementView", *, custom_id: str):
        super().__init__(label=TWITCH_BUTTON_LABEL, style=discord.ButtonStyle.primary, custom_id=custom_id)
        self._view_ref = parent  # Renamed from _parent to avoid discord.py conflict

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        await self._view_ref.handle_click(interaction)


class _TwitchLiveAnnouncementView(discord.ui.View):
    """Persistent live announcement view that tracks clicks before redirecting."""

    def __init__(
        self,
        *,
        cog: TwitchMonitoringMixin,
        streamer_login: str,
        referral_url: str,
        tracking_token: str,
    ):
        super().__init__(timeout=None)
        self.cog = cog
        self.streamer_login = streamer_login
        self.referral_url = referral_url
        self.tracking_token = tracking_token
        self.message_id: Optional[int] = None
        self.channel_id: Optional[int] = None

        custom_id = self._build_custom_id(streamer_login, tracking_token)
        self.add_item(_TrackedTwitchButton(self, custom_id=custom_id))

    @staticmethod
    def _build_custom_id(streamer_login: str, tracking_token: str) -> str:
        login_part = "".join(ch for ch in streamer_login.lower() if ch.isalnum())[:24] or "stream"
        token_part = (tracking_token or "")[:32] or secrets.token_hex(4)
        return f"twitch-live:{login_part}:{token_part}"

    def bind_to_message(self, *, channel_id: Optional[int], message_id: Optional[int]) -> None:
        self.channel_id = channel_id
        self.message_id = message_id

    async def handle_click(self, interaction: discord.Interaction) -> None:
        await self.cog._handle_tracked_button_click(interaction, self)
