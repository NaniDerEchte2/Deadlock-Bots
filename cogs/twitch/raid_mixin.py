# cogs/twitch/raid_mixin.py
"""Mixin für Auto-Raid-Integration in TwitchStreamCog."""

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

from .storage import get_conn

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
                row = conn.execute(
                    "SELECT raid_bot_enabled FROM twitch_streamers WHERE twitch_user_id = ?",
                    (twitch_user_id,),
                ).fetchone()
            if not row or not row[0]:
                log.debug("Auto-Raid übersprungen für %s: nicht aktiviert", login)
                return
        except Exception:
            log.debug("Auto-Raid für %s übersprungen (DB-Check fehlgeschlagen)", login, exc_info=True)
            return

        auth_mgr = getattr(self._raid_bot, "auth_manager", None)
        if not auth_mgr or not auth_mgr.has_enabled_auth(twitch_user_id):
            log.debug("Auto-Raid übersprungen für %s: kein aktiver OAuth-Grant", login)
            return

        # Stream-Dauer berechnen
        started_at_str = previous_state.get("last_started_at")
        stream_duration_sec = 0
        if started_at_str:
            try:
                started_at = datetime.fromisoformat(started_at_str.replace("Z", "+00:00"))
                now = datetime.now(timezone.utc)
                stream_duration_sec = int((now - started_at).total_seconds())
            except Exception:
                log.debug("Konnte Stream-Dauer für %s nicht berechnen", login, exc_info=True)

        # Viewer-Count
        viewer_count = int(previous_state.get("last_viewer_count", 0))

        # Online-Partner finden (nur verifizierte Partner, die gerade live sind)
        online_partners = []
        with get_conn() as conn:
            partners = conn.execute(
                """
                SELECT DISTINCT s.twitch_login, s.twitch_user_id
                FROM twitch_streamers s
                WHERE (s.manual_verified_permanent = 1
                       OR s.manual_verified_until IS NOT NULL
                       OR s.manual_verified_at IS NOT NULL)
                  AND s.manual_partner_opt_out = 0
                  AND s.twitch_user_id != ?
                """,
                (twitch_user_id,),
            ).fetchall()

        # Nur Partner, die gerade live sind
        for partner_login, partner_user_id in partners:
            partner_login_lower = partner_login.lower()
            stream_data = streams_by_login.get(partner_login_lower)
            if stream_data:
                # Stream-Daten mit user_id anreichern
                stream_data["user_id"] = partner_user_id
                online_partners.append(stream_data)

        log.info(
            "Auto-Raid triggered für %s (offline): %d Online-Partner gefunden, "
            "Stream-Dauer: %d Sek, Viewer: %d",
            login,
            len(online_partners),
            stream_duration_sec,
            viewer_count,
        )

        # Raid ausführen (mit Fallback auf DE-Deadlock-Streamer)
        try:
            target_login = await self._raid_bot.handle_streamer_offline(
                broadcaster_id=twitch_user_id,
                broadcaster_login=login,
                viewer_count=viewer_count,
                stream_duration_sec=stream_duration_sec,
                online_partners=online_partners,
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
