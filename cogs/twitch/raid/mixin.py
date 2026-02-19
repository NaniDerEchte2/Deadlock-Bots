# cogs/twitch/raid_mixin.py
"""Mixin für Auto-Raid-Integration in TwitchStreamCog."""

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

from ..constants import TWITCH_TARGET_GAME_NAME
from ..storage import get_conn

log = logging.getLogger("TwitchStreams.RaidMixin")


class TwitchRaidMixin:
    """Integration der Raid-Bot-Logik in die Stream-Überwachung."""

    async def _handle_auto_raid_on_offline(
        self,
        login: str,
        twitch_user_id: Optional[str],
        previous_state: Dict,
        streams_by_login: Dict[str, dict],
    ):
        """
        Wird aufgerufen, wenn ein Streamer offline geht.
        Versucht automatisch zu raiden, falls aktiviert.
        """
        now = datetime.now(timezone.utc)
        recency_cap_seconds = 360  # Maximaler Abstand (10min), damit Deadlock noch relevant ist

        if not twitch_user_id:
            log.debug("Kein twitch_user_id für %s, überspringe Auto-Raid", login)
            return

        # Raid-Bot verfügbar?
        if not hasattr(self, "_raid_bot") or not self._raid_bot:
            log.debug("Raid-Bot nicht initialisiert, überspringe Auto-Raid für %s", login)
            return

        # Nur wenn Streamer Auto-Raid explizit aktiviert und autorisiert hat
        try:
            with get_conn() as conn:
                # Wir prüfen: Ist der Streamer in twitch_streamers ODER hat er einfach nur auth gegeben?
                # Wichtig ist vor allem der Check in twitch_raid_auth via has_enabled_auth
                row = conn.execute(
                    "SELECT raid_bot_enabled FROM twitch_streamers WHERE twitch_user_id = ?",
                    (twitch_user_id,),
                ).fetchone()
            
            # Falls er nicht in twitch_streamers steht (noch kein Partner),
            # gehen wir davon aus, dass er den Bot via OAuth aktiviert hat.
            if row and not row[0]:
                log.debug("Auto-Raid übersprungen für %s: deaktiviert in twitch_streamers", login)
                return
        except Exception:
            log.debug("Auto-Raid DB-Check für %s fehlgeschlagen, fahre mit Auth-Check fort", login)

        auth_mgr = getattr(self._raid_bot, "auth_manager", None)
        if not auth_mgr or not auth_mgr.has_enabled_auth(twitch_user_id):
            log.debug(
                "Auto-Raid übersprungen für %s: kein aktiver OAuth-Grant (OAuth via /traid oder !raid_enable erforderlich)",
                login,
            )
            return

        # needs_reauth=1 → Streamer muss erst re-authen, kein Auto-Raid
        if hasattr(self, "_is_fully_authed"):
            try:
                fully_authed = await self._is_fully_authed(twitch_user_id)
            except Exception:
                fully_authed = True  # im Zweifel erlauben
            if not fully_authed:
                log.info(
                    "Auto-Raid übersprungen für %s: needs_reauth=1 (Re-Auth erforderlich)",
                    login,
                )
                return

        if (
            hasattr(self._raid_bot, "is_offline_auto_raid_suppressed")
            and self._raid_bot.is_offline_auto_raid_suppressed(twitch_user_id)
        ):
            log.info(
                "Auto-Raid übersprungen für %s: kürzlich manueller/externer Raid erkannt (in-memory)",
                login,
            )
            return

        get_target_lower = getattr(self, "_get_target_game_lower", None)
        target_game_lower = get_target_lower() if callable(get_target_lower) else ""
        if not target_game_lower:
            target_game_lower = (TWITCH_TARGET_GAME_NAME or "").strip().lower()

        last_game = (previous_state.get("last_game") or "").strip()
        last_game_lower = last_game.lower()
        had_deadlock_session = bool(int(previous_state.get("had_deadlock_in_session", 0) or 0))
        last_deadlock_seen_at_str = (previous_state.get("last_deadlock_seen_at") or "").strip() or None
        allow_auto_raid = False
        if target_game_lower:
            if last_game_lower == target_game_lower:
                allow_auto_raid = True
            elif last_game_lower == "just chatting" and had_deadlock_session:
                allow_auto_raid = True

        def _is_recent_deadlock(ts_str: Optional[str]) -> bool:
            if not ts_str:
                return False
            try:
                dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
            except Exception:
                return False
            return (now - dt).total_seconds() <= recency_cap_seconds

        # BUGFIX: Wenn der Streamer AKTIV Deadlock streamt (last_game == target_game),
        # dann ist die Recency-Prüfung NICHT nötig! Nur bei "Just Chatting" ist sie relevant.
        if allow_auto_raid and target_game_lower:
            # Wenn aktuell Deadlock läuft, ist die Recency automatisch erfüllt
            if last_game_lower == target_game_lower:
                # Aktiv Deadlock -> kein Recency-Check nötig
                log.debug(
                    "Auto-Raid erlaubt für %s: Aktiv %s gestreamt (last_game=%s)",
                    login,
                    target_game_lower.title(),
                    last_game
                )
            else:
                # Just Chatting mit Deadlock-Session -> Recency-Check
                recent_deadlock = _is_recent_deadlock(last_deadlock_seen_at_str)
                if not recent_deadlock:
                    log.info(
                        "Auto-Raid ausgelassen für %s: letzter Deadlock > %ds her (last_seen=%s, last_game=%s)",
                        login,
                        recency_cap_seconds,
                        last_deadlock_seen_at_str or "unknown",
                        last_game or "unbekannt",
                    )
                    return

        if not allow_auto_raid:
            log.info(
                "Auto-Raid ausgelassen für %s: letzte Kategorie '%s' (had_deadlock_session=%s)",
                login,
                last_game or "unbekannt",
                had_deadlock_session,
            )
            return

        # Stream-Dauer berechnen
        started_at_str = previous_state.get("last_started_at")
        stream_duration_sec = 0
        if started_at_str:
            try:
                started_at = datetime.fromisoformat(started_at_str.replace("Z", "+00:00"))
                now_calc = datetime.now(timezone.utc)
                stream_duration_sec = int((now_calc - started_at).total_seconds())
            except Exception:
                log.debug("Konnte Stream-Dauer für %s nicht berechnen", login, exc_info=True)

        # Viewer-Count
        viewer_count = int(previous_state.get("last_viewer_count", 0))

        # Online-Partner finden (nur verifizierte Partner, die gerade live sind)
        online_partners = []
        partner_logins_lower: List[str] = []
        with get_conn() as conn:
            partners = conn.execute(
                """
            SELECT DISTINCT s.twitch_login,
                            s.twitch_user_id,
                            s.archived_at,
                            a.raid_enabled,
                            a.authorized_at
              FROM twitch_streamers_partner_state s
              LEFT JOIN twitch_raid_auth a ON s.twitch_user_id = a.twitch_user_id
             WHERE s.is_partner_active = 1
               AND s.twitch_user_id IS NOT NULL
               AND s.twitch_user_id != ?
            """,
                (twitch_user_id,),
            ).fetchall()

        # Nur Partner, die gerade live sind
        for partner_login, partner_user_id, archived_at, raid_enabled, raid_authorized_at in partners:
            if archived_at:
                continue
            if not raid_enabled and not raid_authorized_at:
                continue
            partner_login_lower = partner_login.lower()
            stream_data = streams_by_login.get(partner_login_lower)
            if stream_data:
                # Stream-Daten mit user_id anreichern
                stream_data["user_id"] = partner_user_id
                stream_data["raid_enabled"] = bool(raid_enabled) or bool(raid_authorized_at)
                online_partners.append(stream_data)
                partner_logins_lower.append(partner_login_lower)

        # Nur Partner raiden, die aktuell (oder mindestens in dieser Session) Deadlock streamen
        eligible_partners = online_partners
        filtered_out: List[str] = []
        if target_game_lower and online_partners:
            live_state_by_login: Dict[str, Dict[str, object]] = {}
            if partner_logins_lower:
                try:
                    placeholders = ",".join("?" for _ in partner_logins_lower)
                    query = (
                        "SELECT streamer_login, had_deadlock_in_session, last_game, last_deadlock_seen_at "  # nosec B608
                        f"FROM twitch_live_state WHERE streamer_login IN ({placeholders})"
                    )
                    with get_conn() as conn:
                        rows = conn.execute(query, partner_logins_lower).fetchall()
                    for row in rows:
                        login_lower = str(row["streamer_login"] if hasattr(row, "keys") else row[0]).strip().lower()
                        live_state_by_login[login_lower] = {
                            "had_deadlock_in_session": bool(
                                int((row["had_deadlock_in_session"] if hasattr(row, "keys") else row[1]) or 0)
                            ),
                            "last_game": (row["last_game"] if hasattr(row, "keys") else row[2]) or "",
                            "last_deadlock_seen_at": (row["last_deadlock_seen_at"] if hasattr(row, "keys") else row[3]) or "",
                        }
                except Exception:
                    log.debug("Konnte Live-State für Partner nicht laden", exc_info=True)

            filtered_active: List[dict] = []
            filtered_recent: List[dict] = []
            for stream_data in online_partners:
                partner_login_lower = (stream_data.get("user_login") or "").lower()
                game_name = (stream_data.get("game_name") or "").strip()
                game_lower = game_name.lower()
                live_state = live_state_by_login.get(partner_login_lower, {})
                had_deadlock_partner = bool(live_state.get("had_deadlock_in_session", False))
                last_game_state = (live_state.get("last_game") or "").strip()
                last_deadlock_seen_partner = (live_state.get("last_deadlock_seen_at") or "").strip() or None
                recent_deadlock_partner = _is_recent_deadlock(last_deadlock_seen_partner)

                allow_partner = False
                if game_lower == target_game_lower:
                    allow_partner = True
                elif game_lower == "just chatting" and had_deadlock_partner and recent_deadlock_partner:
                    allow_partner = True

                if allow_partner:
                    if game_lower == target_game_lower:
                        filtered_active.append(stream_data)
                    else:
                        filtered_recent.append(stream_data)
                else:
                    filtered_out.append(
                        f"{partner_login_lower} (game='{game_name or last_game_state}', "
                        f"had_deadlock_session={had_deadlock_partner}, "
                        f"last_deadlock_seen={last_deadlock_seen_partner or 'none'})"
                    )

            eligible_partners = filtered_active if filtered_active else filtered_recent

        log.info(
            "Auto-Raid triggered für %s (offline): %d Online-Partner gefunden (%d eligible), "
            "Stream-Dauer: %d Sek, Viewer: %d",
            login,
            len(online_partners),
            len(eligible_partners),
            stream_duration_sec,
            viewer_count,
        )
        if filtered_out:
            log.debug("Auto-Raid: Partner ausgeschlossen (Kategorie/Session): %s", "; ".join(filtered_out))

        # Raid ausführen (mit Fallback auf DE-Deadlock-Streamer)
        try:
            target_login = await self._raid_bot.handle_streamer_offline(
                broadcaster_id=twitch_user_id,
                broadcaster_login=login,
                viewer_count=viewer_count,
                stream_duration_sec=stream_duration_sec,
                online_partners=eligible_partners,
                api=self.api if hasattr(self, "api") else None,
                category_id=self._category_id if hasattr(self, "_category_id") else None,
            )
            if target_login:
                log.info("✅ Auto-Raid erfolgreich: %s -> %s", login, target_login)
            else:
                log.debug("Auto-Raid für %s nicht durchgeführt (Bedingungen nicht erfüllt)", login)
        except Exception:
            log.exception("Fehler beim Auto-Raid für %s", login)

    async def _dashboard_raid_history(self, limit: int = 50, from_broadcaster: str = "") -> List[dict]:
        """Callback für Dashboard: Raid-History abrufen."""
        with get_conn() as conn:
            if from_broadcaster:
                rows = conn.execute(
                    """
                    SELECT from_broadcaster_id, from_broadcaster_login,
                           to_broadcaster_id, to_broadcaster_login,
                           viewer_count, stream_duration_sec, executed_at,
                           success, error_message, target_stream_started_at,
                           candidates_count
                    FROM twitch_raid_history
                    WHERE from_broadcaster_login = ?
                    ORDER BY executed_at DESC
                    LIMIT ?
                    """,
                    (from_broadcaster.lower(), limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT from_broadcaster_id, from_broadcaster_login,
                           to_broadcaster_id, to_broadcaster_login,
                           viewer_count, stream_duration_sec, executed_at,
                           success, error_message, target_stream_started_at,
                           candidates_count
                    FROM twitch_raid_history
                    ORDER BY executed_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()

        return [dict(row) for row in rows]
