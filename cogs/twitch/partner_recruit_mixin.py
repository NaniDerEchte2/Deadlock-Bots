"""Mixin für automatische Erkennung und Ansprache frequenter Deadlock-Streamer.

Logik:
  1. Alle 30 Min werden Streamer aus twitch_stats_category geprüft, die regelmäßig
     Deadlock streamen aber noch keine Partner sind.
  2. Qualifizierende Kandidaten (5+ Tage in 14 Tagen, avg ≥ 2h/Tag) werden erkannt.
  3. Wenn ein Kandidat gerade live ist, wird ein freundliches Partner-Angebot
     im Chat gesendet.
  4. Jeder Kontaktversuch wird in twitch_partner_outreach geloggt;
     ein 60-Tage-Cooldown verhindert erneute Kontaktaufnahme.
"""

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from .storage import get_conn

log = logging.getLogger("TwitchStreams.PartnerRecruit")

# --- Konfiguration -----------------------------------------------------------
RECRUIT_LOOKBACK_DAYS = 14            # Zeitraum für die Erkennung
RECRUIT_MIN_DAYS = 5                  # Mindestanzahl Streaming-Tage im Zeitraum
RECRUIT_MIN_AVG_SAMPLES_PER_DAY = 96  # ≈ 2h bei 75s-Sample-Intervall
RECRUIT_COOLDOWN_DAYS = 60            # Pause zwischen Kontaktversuchen
RECRUIT_CHECK_INTERVAL_SECONDS = 1800 # Prüfzyklus (30 min)
RECRUIT_DISCORD_INVITE = "discord.gg/z5TfVHuQq2"

# Twitch-Chat-Limit: 500 Zeichen. Nachricht bleibt bei ~300 Zeichen.
_OUTREACH_MSG = (
    "Hey @{login}! Wir haben gesehen, du streamst Deadlock regelmäßiger. "
    "{days} Tage in 2 Wochen, das ist sportlich :). "
    "Da wir immer auf der Suche nach Talenten sind, und du regelmäßiger streamst haben wir gedacht, "
    "laden wir dich zu unserem Streamer-Partner-Programm ein. "
    "Das kannst du von uns erwarten: Anti-Spam-Schutz & Support für deinen Kanal, "
    "Go-Live Ankündigungen auf unserem Discord Server, Auto-Raids. "
    "Falls du bock hast schau gerne hier mal vorbei: {invite} <3"
)


class TwitchPartnerRecruitMixin:
    """Erkennt frequente Deadlock-Streamer und sendet ihnen ein Partner-Angebot."""

    # ------------------------------------------------------------------
    # Hauptentry-Point (wird aus monitoring._tick() aufgerufen)
    # ------------------------------------------------------------------
    async def _run_partner_recruit(self, category_streams: List[dict]) -> None:
        """Prüft auf neue Kandidaten und sendet ggf. eine Outreach-Nachricht.

        Wird jeden Tick aufgerufen, aber intern auf 30 Min rate-limitiert.
        Sendet pro Durchlauf höchstens eine Nachricht (Rate-Limit-Schutz).
        """
        last_run = getattr(self, "_last_recruit_check", 0.0)
        if time.time() - last_run < RECRUIT_CHECK_INTERVAL_SECONDS:
            return
        setattr(self, "_last_recruit_check", time.time())

        candidates = self._detect_recruit_candidates()
        if not candidates:
            log.debug("PartnerRecruit: Keine Kandidaten gefunden")
            return

        # Wer ist gerade live? (aus dem aktuellen category_streams-Snapshot)
        live_by_login: Dict[str, dict] = {}
        for stream in category_streams:
            login = (stream.get("user_login") or "").lower()
            if login:
                live_by_login[login] = stream

        for candidate in candidates:
            login = candidate["streamer"]
            if login not in live_by_login:
                log.debug("PartnerRecruit: %s nicht live, überspringe", login)
                continue

            user_id = live_by_login[login].get("user_id")
            if not user_id:
                log.debug("PartnerRecruit: Kein user_id für %s", login)
                continue

            await self._send_partner_outreach(login, str(user_id), candidate["distinct_days"])
            # Nur eine Nachricht pro Zyklus (Twitch Rate-Limit-Schutz)
            break

    # ------------------------------------------------------------------
    # Erkennung
    # ------------------------------------------------------------------
    def _detect_recruit_candidates(self) -> List[dict]:
        """SQL-Query: Streamer mit 5+ Tagen, avg ≥ 2h/Tag, keine Partner, kein aktiver Cooldown."""
        try:
            with get_conn() as conn:
                rows = conn.execute(
                    """
                    SELECT
                        streamer,
                        COUNT(DISTINCT DATE(ts_utc))                              AS distinct_days,
                        CAST(COUNT(*) AS REAL) / COUNT(DISTINCT DATE(ts_utc))      AS avg_samples_per_day
                    FROM twitch_stats_category
                    WHERE ts_utc > datetime('now', ?)
                      AND LOWER(streamer) NOT IN (
                            SELECT LOWER(twitch_login) FROM twitch_streamers
                          )
                      AND LOWER(streamer) NOT IN (
                            SELECT streamer_login FROM twitch_partner_outreach
                             WHERE cooldown_until > datetime('now')
                          )
                      AND LOWER(streamer) NOT IN (
                            SELECT LOWER(target_login) FROM twitch_raid_blacklist
                          )
                    GROUP BY streamer
                    HAVING COUNT(DISTINCT DATE(ts_utc)) >= ?
                      AND CAST(COUNT(*) AS REAL) / COUNT(DISTINCT DATE(ts_utc)) >= ?
                    ORDER BY distinct_days DESC
                    """,
                    (
                        f"-{RECRUIT_LOOKBACK_DAYS} days",
                        RECRUIT_MIN_DAYS,
                        RECRUIT_MIN_AVG_SAMPLES_PER_DAY,
                    ),
                ).fetchall()

            return [
                {
                    "streamer": str(row["streamer"] if hasattr(row, "keys") else row[0]).lower(),
                    "distinct_days": int(row["distinct_days"] if hasattr(row, "keys") else row[1]),
                }
                for row in rows
            ]
        except Exception:
            log.debug("PartnerRecruit: Kandidaten-Query fehlgeschlagen", exc_info=True)
            return []

    # ------------------------------------------------------------------
    # Outreach senden
    # ------------------------------------------------------------------
    async def _send_partner_outreach(self, login: str, user_id: str, distinct_days: int) -> None:
        """Folgt dem Channel, sendet die Nachricht und logt den Versuch."""
        chat_bot = getattr(self, "_twitch_chat_bot", None)
        if not chat_bot:
            log.debug("PartnerRecruit: Chat-Bot nicht verfügbar")
            return

        # 1. Channel folgen (damit "Follower only" Chats funktionieren)
        if hasattr(chat_bot, "follow_channel"):
            await chat_bot.follow_channel(user_id)

        # 2. Channel beitreten
        try:
            await chat_bot.join(login, channel_id=user_id)
        except Exception:
            log.debug("PartnerRecruit: join(%s) fehlgeschlagen", login, exc_info=True)

        # Kurze Pause: Follow + Join brauchen einen Moment
        await asyncio.sleep(2)

        # 3. Nachricht zusammenbauen
        message = _OUTREACH_MSG.format(
            login=login,
            days=distinct_days,
            invite=RECRUIT_DISCORD_INVITE,
        )

        # 4. Senden via _send_chat_message (gleiche Methode wie raid_manager)
        success = False
        if hasattr(chat_bot, "_send_chat_message"):

            class _MockChannel:
                __slots__ = ("name", "id")

                def __init__(self, n: str, uid: str):
                    self.name = n
                    self.id = uid

            success = await chat_bot._send_chat_message(_MockChannel(login, user_id), message)

        # 5. Versuch loggen (Cooldown setzen auch bei Fehler)
        self._record_outreach(login, user_id, success)

        if success:
            log.info("PartnerRecruit: Outreach gesendet an %s (%d Tage aktiv)", login, distinct_days)
        else:
            log.warning("PartnerRecruit: Outreach an %s fehlgeschlagen", login)

    # ------------------------------------------------------------------
    # Persistenz
    # ------------------------------------------------------------------
    def _record_outreach(self, login: str, user_id: str, success: bool) -> None:
        """Speichert den Outreach-Versuch mit 60-Tage-Cooldown."""
        now = datetime.now(timezone.utc)
        cooldown_until = (now + timedelta(days=RECRUIT_COOLDOWN_DAYS)).isoformat(timespec="seconds")
        status = "sent" if success else "failed"

        try:
            with get_conn() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO twitch_partner_outreach
                        (streamer_login, streamer_user_id, detected_at, contacted_at, status, cooldown_until)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        login.lower(),
                        user_id,
                        now.isoformat(timespec="seconds"),
                        now.isoformat(timespec="seconds"),
                        status,
                        cooldown_until,
                    ),
                )
                conn.commit()
        except Exception:
            log.debug("PartnerRecruit: Outreach nicht loggbar für %s", login, exc_info=True)
