# cogs/twitch/raid_manager.py
"""
Raid Bot Manager für automatische Twitch Raids zwischen Partnern.

Verwaltet:
- OAuth User Access Tokens für Streamer
- Automatische Raids beim Offline-Gehen
- Partner-Auswahl (kürzeste Stream-Zeit)
- Raid-Metadaten und History
"""
import asyncio
import logging
import time
import secrets
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlencode

import aiohttp

from .storage import get_conn

TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
TWITCH_AUTHORIZE_URL = "https://id.twitch.tv/oauth2/authorize"
TWITCH_API_BASE = "https://api.twitch.tv/helix"

# Erforderliche Scopes für Raid-Funktionalität
RAID_SCOPES = ["channel:manage:raids"]

log = logging.getLogger("TwitchStreams.RaidManager")


class RaidAuthManager:
    """Verwaltet OAuth User Access Tokens für Raid-Autorisierung."""

    def __init__(self, client_id: str, client_secret: str, redirect_uri: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self._state_tokens: Dict[str, str] = {}  # state -> twitch_login (für OAuth-Flow)

    def generate_auth_url(self, twitch_login: str) -> str:
        """Generiert eine OAuth-URL für Streamer-Autorisierung."""
        state = secrets.token_urlsafe(32)
        self._state_tokens[state] = twitch_login

        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": " ".join(RAID_SCOPES),
            "state": state,
            "force_verify": "true",  # Immer erneut authorisieren lassen
        }
        return f"{TWITCH_AUTHORIZE_URL}?{urlencode(params)}"

    def verify_state(self, state: str) -> Optional[str]:
        """Verifiziert State-Token und gibt den zugehörigen Login zurück."""
        return self._state_tokens.pop(state, None)

    async def exchange_code_for_token(
        self, code: str, session: aiohttp.ClientSession
    ) -> Dict:
        """Tauscht Authorization Code gegen User Access Token."""
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": self.redirect_uri,
        }

        async with session.post(TWITCH_TOKEN_URL, data=data) as r:
            if r.status != 200:
                txt = await r.text()
                log.error("Token exchange failed: HTTP %s: %s", r.status, txt[:300])
                r.raise_for_status()
            return await r.json()

    async def refresh_token(
        self, refresh_token: str, session: aiohttp.ClientSession
    ) -> Dict:
        """Erneuert einen abgelaufenen User Access Token."""
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }

        async with session.post(TWITCH_TOKEN_URL, data=data) as r:
            if r.status != 200:
                txt = await r.text()
                log.error("Token refresh failed: HTTP %s: %s", r.status, txt[:300])
                r.raise_for_status()
            return await r.json()

    def save_auth(
        self,
        twitch_user_id: str,
        twitch_login: str,
        access_token: str,
        refresh_token: str,
        expires_in: int,
        scopes: List[str],
    ) -> None:
        """Speichert OAuth-Tokens in der Datenbank."""
        expires_at = datetime.now(timezone.utc).timestamp() + expires_in
        expires_at_iso = datetime.fromtimestamp(expires_at, timezone.utc).isoformat()

        with get_conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO twitch_raid_auth
                (twitch_user_id, twitch_login, access_token, refresh_token,
                 token_expires_at, scopes, authorized_at, raid_enabled)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    twitch_user_id,
                    twitch_login,
                    access_token,
                    refresh_token,
                    expires_at_iso,
                    " ".join(scopes),
                ),
            )
            conn.commit()
        log.info("Saved raid auth for %s (user_id=%s)", twitch_login, twitch_user_id)

    async def get_valid_token(
        self, twitch_user_id: str, session: aiohttp.ClientSession
    ) -> Optional[str]:
        """
        Holt ein gültiges Access Token für den Streamer.
        Erneuert es automatisch, falls abgelaufen.
        """
        with get_conn() as conn:
            row = conn.execute(
                """
                SELECT access_token, refresh_token, token_expires_at, twitch_login
                FROM twitch_raid_auth
                WHERE twitch_user_id = ? AND raid_enabled = 1
                """,
                (twitch_user_id,),
            ).fetchone()

        if not row:
            return None

        access_token, refresh_token, expires_at_iso, twitch_login = row
        expires_at = datetime.fromisoformat(expires_at_iso).timestamp()

        # Token noch gültig?
        if time.time() < expires_at - 300:  # 5 Minuten Puffer
            return access_token

        # Token abgelaufen -> refresh
        log.info("Refreshing token for %s", twitch_login)
        try:
            token_data = await self.refresh_token(refresh_token, session)
            new_access_token = token_data["access_token"]
            new_refresh_token = token_data.get("refresh_token", refresh_token)
            expires_in = token_data.get("expires_in", 3600)

            # Token in DB aktualisieren
            new_expires_at = datetime.now(timezone.utc).timestamp() + expires_in
            new_expires_at_iso = datetime.fromtimestamp(
                new_expires_at, timezone.utc
            ).isoformat()

            with get_conn() as conn:
                conn.execute(
                    """
                    UPDATE twitch_raid_auth
                    SET access_token = ?, refresh_token = ?,
                        token_expires_at = ?, last_refreshed_at = CURRENT_TIMESTAMP
                    WHERE twitch_user_id = ?
                    """,
                    (new_access_token, new_refresh_token, new_expires_at_iso, twitch_user_id),
                )
                conn.commit()

            return new_access_token
        except Exception:
            log.exception("Failed to refresh token for %s", twitch_login)
            return None

    def revoke_auth(self, twitch_user_id: str) -> None:
        """Entfernt die Raid-Autorisierung für einen Streamer."""
        with get_conn() as conn:
            conn.execute(
                "DELETE FROM twitch_raid_auth WHERE twitch_user_id = ?",
                (twitch_user_id,),
            )
            conn.commit()
        log.info("Revoked raid auth for user_id=%s", twitch_user_id)

    def set_raid_enabled(self, twitch_user_id: str, enabled: bool) -> None:
        """Aktiviert/Deaktiviert Auto-Raid für einen Streamer."""
        with get_conn() as conn:
            conn.execute(
                "UPDATE twitch_raid_auth SET raid_enabled = ? WHERE twitch_user_id = ?",
                (1 if enabled else 0, twitch_user_id),
            )
            conn.commit()
        log.info("Set raid_enabled=%s for user_id=%s", enabled, twitch_user_id)


class RaidExecutor:
    """Führt Raids aus und speichert Metadaten."""

    def __init__(self, client_id: str, auth_manager: RaidAuthManager):
        self.client_id = client_id
        self.auth_manager = auth_manager

    async def start_raid(
        self,
        from_broadcaster_id: str,
        from_broadcaster_login: str,
        to_broadcaster_id: str,
        to_broadcaster_login: str,
        viewer_count: int,
        stream_duration_sec: int,
        target_stream_started_at: str,
        candidates_count: int,
        session: aiohttp.ClientSession,
    ) -> Tuple[bool, Optional[str]]:
        """
        Startet einen Raid von from_broadcaster zu to_broadcaster.

        Returns:
            (success, error_message)
        """
        # Access Token holen
        access_token = await self.auth_manager.get_valid_token(
            from_broadcaster_id, session
        )
        if not access_token:
            error_msg = f"No valid token for {from_broadcaster_login}"
            log.warning(error_msg)
            self._save_raid_history(
                from_broadcaster_id,
                from_broadcaster_login,
                to_broadcaster_id,
                to_broadcaster_login,
                viewer_count,
                stream_duration_sec,
                target_stream_started_at,
                candidates_count,
                success=False,
                error_message=error_msg,
            )
            return False, error_msg

        # Raid über Twitch API starten
        url = f"{TWITCH_API_BASE}/raids"
        params = {
            "from_broadcaster_id": from_broadcaster_id,
            "to_broadcaster_id": to_broadcaster_id,
        }
        headers = {
            "Client-ID": self.client_id,
            "Authorization": f"Bearer {access_token}",
        }

        try:
            async with session.post(url, headers=headers, params=params) as r:
                if r.status != 200:
                    txt = await r.text()
                    error_msg = f"Raid API failed: HTTP {r.status}: {txt[:200]}"
                    log.error(error_msg)
                    self._save_raid_history(
                        from_broadcaster_id,
                        from_broadcaster_login,
                        to_broadcaster_id,
                        to_broadcaster_login,
                        viewer_count,
                        stream_duration_sec,
                        target_stream_started_at,
                        candidates_count,
                        success=False,
                        error_message=error_msg,
                    )
                    return False, error_msg

                # Erfolg!
                log.info(
                    "Raid successful: %s -> %s (%d viewers, %d candidates)",
                    from_broadcaster_login,
                    to_broadcaster_login,
                    viewer_count,
                    candidates_count,
                )
                self._save_raid_history(
                    from_broadcaster_id,
                    from_broadcaster_login,
                    to_broadcaster_id,
                    to_broadcaster_login,
                    viewer_count,
                    stream_duration_sec,
                    target_stream_started_at,
                    candidates_count,
                    success=True,
                    error_message=None,
                )
                return True, None

        except Exception as e:
            error_msg = f"Exception during raid: {e}"
            log.exception("Raid exception: %s -> %s", from_broadcaster_login, to_broadcaster_login)
            self._save_raid_history(
                from_broadcaster_id,
                from_broadcaster_login,
                to_broadcaster_id,
                to_broadcaster_login,
                viewer_count,
                stream_duration_sec,
                target_stream_started_at,
                candidates_count,
                success=False,
                error_message=error_msg,
            )
            return False, error_msg

    def _save_raid_history(
        self,
        from_broadcaster_id: str,
        from_broadcaster_login: str,
        to_broadcaster_id: str,
        to_broadcaster_login: str,
        viewer_count: int,
        stream_duration_sec: int,
        target_stream_started_at: str,
        candidates_count: int,
        success: bool,
        error_message: Optional[str],
    ) -> None:
        """Speichert Raid-Metadaten in der Datenbank."""
        reason = "auto_raid_on_offline"
        with get_conn() as conn:
            conn.execute(
                """
                INSERT INTO twitch_raid_history
                (from_broadcaster_id, from_broadcaster_login, to_broadcaster_id,
                 to_broadcaster_login, viewer_count, stream_duration_sec, reason,
                 success, error_message, target_stream_started_at, candidates_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    from_broadcaster_id,
                    from_broadcaster_login,
                    to_broadcaster_id,
                    to_broadcaster_login,
                    viewer_count,
                    stream_duration_sec,
                    reason,
                    1 if success else 0,
                    error_message,
                    target_stream_started_at,
                    candidates_count,
                ),
            )
            conn.commit()


class RaidBot:
    """
    Hauptklasse für automatische Raid-Verwaltung.

    - Erkennt, wenn ein Partner offline geht
    - Wählt Partner nach Fairness aus (wer weniger Raids bekommen hat)
    - Führt den Raid aus und loggt Metadaten (gesendete + empfangene Raids)
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        session: aiohttp.ClientSession,
    ):
        self.auth_manager = RaidAuthManager(client_id, client_secret, redirect_uri)
        self.raid_executor = RaidExecutor(client_id, self.auth_manager)
        self.session = session
        self.chat_bot = None  # Wird später gesetzt

    def set_chat_bot(self, chat_bot):
        """Setzt den Twitch Chat Bot für Recruitment-Nachrichten."""
        self.chat_bot = chat_bot

    async def _send_recruitment_message(
        self,
        from_broadcaster_login: str,
        to_broadcaster_login: str,
        target_stream_data: Optional[Dict] = None,
    ):
        """
        Sendet eine Einladungs-Nachricht im Chat des geraideten Nicht-Partners.

        Diese Nachricht wird nur gesendet, wenn ein deutscher Deadlock-Streamer
        (kein Partner) geraidet wird, um ihn zur Community einzuladen.

        Zeigt dem Streamer seine aktuellen Stats als Teaser.
        """
        if not self.chat_bot:
            log.debug("Chat bot not available for recruitment message")
            return

        try:
            # Nachricht mit Discord-Link (ENV-Variable oder hardcoded)
            discord_invite = "discord.gg/deadlock-de"  # TODO: Aus ENV holen

            # Stream-Insights als Teaser berechnen
            insights = ""
            if target_stream_data:
                viewer_count = target_stream_data.get("viewer_count", 0)
                started_at = target_stream_data.get("started_at")

                if started_at:
                    from datetime import datetime, timezone
                    try:
                        started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                        now = datetime.now(timezone.utc)
                        hours = (now - started).total_seconds() / 3600

                        if hours < 1:
                            minutes = int((now - started).total_seconds() / 60)
                            time_str = f"{minutes} Minuten"
                        else:
                            time_str = f"{hours:.1f} Stunden"

                        insights = f"Wir tracken alle deutschen Deadlock-Streamer – du streamst gerade seit {time_str} mit {viewer_count} Viewern. "
                    except Exception:
                        pass

            message = (
                f"Hey @{to_broadcaster_login}! 👋 Dieser RAID kommt von der deutschen Deadlock Community! "
                f"{insights}"
                f"{from_broadcaster_login} ist bei uns Partner und bekommt automatisch Raids von anderen Partnern. "
                f"Als Partner kriegst du nicht nur Raids, sondern auch Zugriff auf detaillierte Stream-Stats und Analytics. "
                f"Bock drauf? Schau auf unserem Discord vorbei: {discord_invite} "
                f"Komplett kostenfrei, keine Verpflichtungen – nur gegenseitige Unterstützung! 🎮"
            )

            # Sende Nachricht im Chat des geraideten Streamers
            channel = await self.chat_bot.fetch_channel(to_broadcaster_login)
            if channel:
                await channel.send(message)
                log.info(
                    "Sent recruitment message in %s's chat (raided by %s)",
                    to_broadcaster_login,
                    from_broadcaster_login,
                )
            else:
                log.warning("Could not join channel %s for recruitment message", to_broadcaster_login)

        except Exception:
            log.exception(
                "Failed to send recruitment message to %s (raided by %s)",
                to_broadcaster_login,
                from_broadcaster_login,
            )

    def _select_fairest_candidate(
        self, candidates: List[Dict], from_broadcaster_id: str
    ) -> Optional[Dict]:
        """
        Wählt den fairsten Raid-Kandidaten aus.

        Fairness-Kriterien:
        1. Wer weniger Raids bekommen hat (Hauptkriterium)
        2. Wer kürzer live ist (Tiebreaker)

        Returns:
            Der fairste Kandidat oder None
        """
        if not candidates:
            return None

        # Raid-Statistiken für alle Kandidaten holen
        candidate_stats = []

        with get_conn() as conn:
            for candidate in candidates:
                user_id = candidate.get("user_id")
                user_login = candidate.get("user_login", "")
                started_at = candidate.get("started_at", "9999-99-99")

                # Anzahl empfangener Raids
                received = conn.execute(
                    """
                    SELECT COUNT(*) FROM twitch_raid_history
                    WHERE to_broadcaster_id = ? AND success = 1
                    """,
                    (user_id,),
                ).fetchone()
                received_count = received[0] if received else 0

                # Anzahl gesendeter Raids (für spätere Analysen)
                sent = conn.execute(
                    """
                    SELECT COUNT(*) FROM twitch_raid_history
                    WHERE from_broadcaster_id = ? AND success = 1
                    """,
                    (user_id,),
                ).fetchone()
                sent_count = sent[0] if sent else 0

                candidate_stats.append({
                    "candidate": candidate,
                    "user_id": user_id,
                    "user_login": user_login,
                    "started_at": started_at,
                    "received_raids": received_count,
                    "sent_raids": sent_count,
                })

        # Sortieren nach Fairness:
        # 1. Weniger empfangene Raids = höhere Priorität
        # 2. Bei Gleichstand: kürzere Stream-Zeit
        candidate_stats.sort(key=lambda x: (x["received_raids"], x["started_at"]))

        selected = candidate_stats[0]
        log.info(
            "Raid target selection: %s (received: %d raids, sent: %d raids, started: %s) from %d candidates",
            selected["user_login"],
            selected["received_raids"],
            selected["sent_raids"],
            selected["started_at"][:16],
            len(candidates),
        )

        return selected["candidate"]

    async def handle_streamer_offline(
        self,
        broadcaster_id: str,
        broadcaster_login: str,
        viewer_count: int,
        stream_duration_sec: int,
        online_partners: List[Dict],
        api=None,
        category_id: Optional[str] = None,
    ) -> Optional[str]:
        """
        Wird aufgerufen, wenn ein Streamer offline geht.
        Versucht automatisch zu raiden, falls möglich.

        Args:
            broadcaster_id: Twitch User ID des Offline-Gehenden
            broadcaster_login: Twitch Login des Offline-Gehenden
            viewer_count: Letzte Viewer-Anzahl
            stream_duration_sec: Stream-Dauer in Sekunden
            online_partners: Liste von Online-Partnern (Stream-Daten)

        Returns:
            Login des geraideten Streamers, oder None
        """
        # Prüfen, ob Streamer Auto-Raid aktiviert hat
        with get_conn() as conn:
            row = conn.execute(
                """
                SELECT s.raid_bot_enabled, a.raid_enabled
                FROM twitch_streamers s
                LEFT JOIN twitch_raid_auth a ON s.twitch_user_id = a.twitch_user_id
                WHERE s.twitch_user_id = ?
                """,
                (broadcaster_id,),
            ).fetchone()

        if not row:
            log.debug("Streamer %s not found in DB", broadcaster_login)
            return None

        raid_bot_enabled, raid_auth_enabled = row
        if not raid_bot_enabled:
            log.debug("Raid bot disabled for %s (setting)", broadcaster_login)
            return None
        if not raid_auth_enabled:
            log.debug("Raid bot disabled for %s (no auth)", broadcaster_login)
            return None

        # Kandidaten filtern (nur andere Streamer, nicht sich selbst)
        candidates = [
            s for s in online_partners
            if s.get("user_id") != broadcaster_id
        ]

        is_partner_raid = False
        target = None
        target_id = None
        target_login = None
        target_started_at = ""

        if candidates:
            # Partner vorhanden -> Fairness-basierte Auswahl
            is_partner_raid = True
            target = self._select_fairest_candidate(candidates, broadcaster_id)
            if not target:
                log.warning("Could not select raid target for %s", broadcaster_login)
                return None

            target_id = target["user_id"]
            target_login = target["user_login"]
            target_started_at = target.get("started_at", "")

            log.info(
                "Executing partner raid: %s -> %s (%d partner candidates)",
                broadcaster_login,
                target_login,
                len(candidates),
            )
        else:
            # Keine Partner online -> Fallback auf deutsche Deadlock-Streamer
            log.info("No partners online for %s, trying Deadlock-DE fallback", broadcaster_login)

            if not api or not category_id:
                log.warning("Cannot fallback to Deadlock-DE (no API or category_id)")
                return None

            try:
                # Hole deutsche Deadlock-Streamer
                de_streams = await api.get_streams_by_category(
                    category_id,
                    language="de",
                    limit=50
                )

                # Filtere eigenen Stream raus
                de_streams = [
                    s for s in de_streams
                    if s.get("user_id") != broadcaster_id
                ]

                if not de_streams:
                    log.info("No German Deadlock streamers found for fallback raid")
                    return None

                # Sortiere nach kürzester Stream-Zeit
                de_streams.sort(key=lambda s: s.get("started_at", "9999-99-99"))
                target = de_streams[0]

                target_id = target["user_id"]
                target_login = target["user_login"]
                target_started_at = target.get("started_at", "")

                log.info(
                    "Executing Deadlock-DE fallback raid: %s -> %s (non-partner, %d DE streamers found)",
                    broadcaster_login,
                    target_login,
                    len(de_streams),
                )
            except Exception:
                log.exception("Failed to get Deadlock-DE streams for fallback raid")
                return None

        # Raid ausführen
        success, error = await self.raid_executor.start_raid(
            from_broadcaster_id=broadcaster_id,
            from_broadcaster_login=broadcaster_login,
            to_broadcaster_id=target_id,
            to_broadcaster_login=target_login,
            viewer_count=viewer_count,
            stream_duration_sec=stream_duration_sec,
            target_stream_started_at=target_started_at,
            candidates_count=len(candidates) if is_partner_raid else len(de_streams) if 'de_streams' in locals() else 0,
            session=self.session,
        )

        # Bei Nicht-Partner-Raid: Chat-Nachricht senden
        if success and not is_partner_raid:
            await self._send_recruitment_message(
                from_broadcaster_login=broadcaster_login,
                to_broadcaster_login=target_login,
                target_stream_data=target,
            )

        return target_login if success else None
