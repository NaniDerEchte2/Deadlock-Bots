# cogs/twitch/raid_manager.py
"""
Raid Bot Manager für automatische Twitch Raids zwischen Partnern.

Verwaltet:
- OAuth User Access Tokens für Streamer
- Automatische Raids beim Offline-Gehen
- Partner-Auswahl (kürzeste Stream-Zeit)
- Raid-Metadaten und History
"""
import logging
import time
import secrets
import asyncio
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlencode

import aiohttp

from .storage import get_conn
from .token_error_handler import TokenErrorHandler

TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
TWITCH_AUTHORIZE_URL = "https://id.twitch.tv/oauth2/authorize"
TWITCH_API_BASE = "https://api.twitch.tv/helix"

# Erforderliche Scopes für Raid-Funktionalität + Zusatz-Metriken (Follower/Chat)
# Hinweis: Re-Auth notwendig, falls bisher nur channel:manage:raids erteilt war.
RAID_SCOPES = [
    "channel:manage:raids",
    "moderator:read:followers",
    "moderator:manage:banned_users",
    "moderator:manage:chat_messages",
    "channel:read:subscriptions",
    "analytics:read:games",
    "channel:manage:moderators",
    "channel:bot",
    "chat:read",
    "chat:edit",
]

log = logging.getLogger("TwitchStreams.RaidManager")


class RaidAuthManager:
    """Verwaltet OAuth User Access Tokens für Raid-Autorisierung."""

    def __init__(self, client_id: str, client_secret: str, redirect_uri: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self._state_tokens: Dict[str, Tuple[str, float]] = {}  # state -> (twitch_login, timestamp)
        self._lock = asyncio.Lock()
        self.token_error_handler = TokenErrorHandler()

    def generate_auth_url(self, twitch_login: str) -> str:
        """Generiert eine OAuth-URL für Streamer-Autorisierung."""
        # State kürzen auf 16 chars um URL-Länge für Discord Buttons (max 512) zu sparen
        state = secrets.token_urlsafe(16)
        self._state_tokens[state] = (twitch_login, time.time())

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
        """Verifiziert State-Token und gibt den zugehörigen Login zurück (max 10 Min alt)."""
        data = self._state_tokens.pop(state, None)
        if not data:
            return None
        
        login, timestamp = data
        if time.time() - timestamp > 600:  # 10 Minuten TTL
            log.warning("State token for %s expired", login)
            return None
            
        return login

    def cleanup_states(self) -> None:
        """Entfernt abgelaufene State-Tokens aus dem Speicher."""
        now = time.time()
        expired = [s for s, (_, ts) in self._state_tokens.items() if now - ts > 600]
        for s in expired:
            del self._state_tokens[s]
        if expired:
            log.debug("Cleaned up %d expired auth states", len(expired))

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
        self, refresh_token: str, session: aiohttp.ClientSession, twitch_user_id: str = None, twitch_login: str = None
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
                error_msg = f"HTTP {r.status}: {txt[:300]}"
                log.error(
                    "Token refresh failed for refresh_token starting with '%s...': %s",
                    refresh_token[:8],
                    error_msg,
                )
                
                # Bei "Invalid refresh token" (HTTP 400): Blacklist + Benachrichtigung
                if r.status == 400 and "invalid" in txt.lower() and twitch_user_id and twitch_login:
                    log.warning(
                        "Invalid refresh token detected for %s (ID: %s) - adding to blacklist",
                        twitch_login,
                        twitch_user_id,
                    )
                    
                    # Zur Blacklist hinzufügen
                    self.token_error_handler.add_to_blacklist(
                        twitch_user_id=twitch_user_id,
                        twitch_login=twitch_login,
                        error_message=error_msg,
                    )
                    
                    # Discord-Benachrichtigung senden (async, fire-and-forget)
                    if hasattr(self, '_discord_bot') and self._discord_bot:
                        asyncio.create_task(
                            self.token_error_handler.notify_token_error(
                                twitch_user_id=twitch_user_id,
                                twitch_login=twitch_login,
                                error_message=error_msg,
                            )
                        )

                r.raise_for_status()
            return await r.json()

    async def refresh_all_tokens(self, session: aiohttp.ClientSession) -> int:
        """
        Refreshes tokens for all authorized users if they are close to expiry (< 2 hours).
        Returns the number of refreshed tokens.
        """
        refreshed_count = 0
        with get_conn() as conn:
            # Hole alle User mit raid_enabled=1
            rows = conn.execute(
                """
                SELECT twitch_user_id, twitch_login, refresh_token, token_expires_at
                FROM twitch_raid_auth
                WHERE raid_enabled = 1
                """
            ).fetchall()

        if not rows:
            return 0

        now_ts = time.time()
        
        # Parallelisierung möglich, aber hier sequenziell zur Sicherheit (Rate Limits)
        for row in rows:
            user_id = row["twitch_user_id"]
            login = row["twitch_login"]
            refresh_tok = row["refresh_token"]
            expires_iso = row["token_expires_at"]

            # Sicherheits-Check: Falls doch auf Blacklist, überspringen
            if self.token_error_handler.is_token_blacklisted(user_id):
                continue

            try:
                expires_dt = datetime.fromisoformat(expires_iso.replace("Z", "+00:00"))
                expires_ts = expires_dt.timestamp()
            except Exception:
                log.warning("Invalid expiry date for %s, forcing refresh", login)
                expires_ts = 0

            # Refresh wenn weniger als 2 Stunden (7200s) gültig
            if now_ts < expires_ts - 7200:
                continue

            async with self._lock:
                try:
                    # Double-Check im Lock, falls parallel ein Raid lief und refresht hat
                    with get_conn() as conn:
                        current = conn.execute(
                            "SELECT token_expires_at FROM twitch_raid_auth WHERE twitch_user_id = ?",
                            (user_id,)
                        ).fetchone()
                    
                    if current:
                        curr_iso = current[0]
                        curr_ts = datetime.fromisoformat(curr_iso.replace("Z", "+00:00")).timestamp()
                        if now_ts < curr_ts - 7200:
                            continue # Wurde bereits refresht
                except Exception as exc:
                    log.debug("Konnte expires_at nicht parsen für %s", login, exc_info=exc)

                log.info("Auto-refreshing token for %s (background maintenance)", login)
                try:
                    token_data = await self.refresh_token(
                        refresh_tok, session, twitch_user_id=user_id, twitch_login=login
                    )
                    new_access = token_data["access_token"]
                    new_refresh = token_data.get("refresh_token", refresh_tok)
                    expires_in = token_data.get("expires_in", 3600)
                    
                    new_expires_at = datetime.now(timezone.utc).timestamp() + expires_in
                    new_expires_iso = datetime.fromtimestamp(new_expires_at, timezone.utc).isoformat()

                    with get_conn() as conn:
                        conn.execute(
                            """
                            UPDATE twitch_raid_auth
                            SET access_token = ?, refresh_token = ?,
                                token_expires_at = ?, last_refreshed_at = CURRENT_TIMESTAMP
                            WHERE twitch_user_id = ?
                            """,
                            (new_access, new_refresh, new_expires_iso, user_id),
                        )
                        conn.commit()
                    refreshed_count += 1
                    # Kleines Delay um API Spikes zu vermeiden
                    await asyncio.sleep(0.5)

                except Exception:
                    log.error("Background refresh failed for %s", login) # Log & Continue
                    
        if refreshed_count > 0:
            log.info("Maintenance: Refreshed %d user tokens", refreshed_count)
            
        return refreshed_count

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
        now = datetime.now(timezone.utc)
        expires_at = now.timestamp() + expires_in
        expires_at_iso = datetime.fromtimestamp(expires_at, timezone.utc).isoformat()
        authorized_at = now.isoformat()

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
                    authorized_at,
                ),
            )
            # Aktivieren, damit Auto-Raid unmittelbar nach OAuth freigeschaltet ist
            conn.execute(
                """
                UPDATE twitch_streamers
                   SET raid_bot_enabled = 1
                 WHERE twitch_user_id = ?
                    OR lower(twitch_login) = lower(?)
                """,
                (twitch_user_id, twitch_login),
            )
            conn.commit()

        # Bei erfolgreicher Auth: Von Blacklist entfernen (falls vorhanden)
        self.token_error_handler.remove_from_blacklist(twitch_user_id)

        log.info("Saved raid auth for %s (user_id=%s)", twitch_login, twitch_user_id)

    async def get_tokens_for_user(
        self, twitch_user_id: str, session: aiohttp.ClientSession
    ) -> Optional[Tuple[str, str]]:
        """
        Holt Access- UND Refresh-Token für einen User.
        Erneuert den Token automatisch, falls abgelaufen.
        Wird bewusst auch genutzt, wenn raid_enabled=0 (Chat-Bot/Moderation).
        """
        # Blacklist check
        if self.token_error_handler.is_token_blacklisted(twitch_user_id):
            return None

        with get_conn() as conn:
            row = conn.execute(
                """
                SELECT access_token, refresh_token, token_expires_at, twitch_login
                FROM twitch_raid_auth
                WHERE twitch_user_id = ?
                """,
                (twitch_user_id,),
            ).fetchone()

        if not row:
            return None

        access_token, refresh_token, expires_at_iso, twitch_login = row
        expires_at = datetime.fromisoformat(expires_at_iso).timestamp()

        # Token noch gültig? (5 Minuten Puffer)
        if time.time() < expires_at - 300:
            return access_token, refresh_token

        # Token abgelaufen -> refresh
        async with self._lock:
            # Erneuter Check innerhalb des Locks (Double-Check Locking Pattern)
            with get_conn() as conn:
                row_check = conn.execute(
                    "SELECT token_expires_at, access_token, refresh_token FROM twitch_raid_auth WHERE twitch_user_id = ?",
                    (twitch_user_id,),
                ).fetchone()
            
            if row_check:
                curr_expires_iso, curr_access, curr_refresh = row_check
                curr_expires = datetime.fromisoformat(curr_expires_iso).timestamp()
                if time.time() < curr_expires - 300:
                    return curr_access, curr_refresh

            log.info("Refreshing token for %s (get_tokens)", twitch_login)
            try:
                token_data = await self.refresh_token(
                    refresh_token,
                    session,
                    twitch_user_id=twitch_user_id,
                    twitch_login=twitch_login,
                )
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

                return new_access_token, new_refresh_token
            except Exception:
                log.exception("Failed to refresh token for %s", twitch_login)
                return None

    async def get_valid_token(
        self, twitch_user_id: str, session: aiohttp.ClientSession
    ) -> Optional[str]:
        """
        Holt ein gültiges Access Token für den Streamer.
        Erneuert es automatisch, falls abgelaufen.

        WICHTIG: Wenn der Token auf der Blacklist steht (ungültiger Refresh-Token),
        wird None zurückgegeben ohne Refresh-Versuch.
        """
        # SCHRITT 1: Blacklist-Check BEVOR wir überhaupt zur DB gehen
        if self.token_error_handler.is_token_blacklisted(twitch_user_id):
            log.warning(
                "Token for user_id=%s is blacklisted - skipping refresh attempt",
                twitch_user_id,
            )
            return None

        # SCHRITT 2: Token aus DB holen
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

        # SCHRITT 3: Token noch gültig?
        if time.time() < expires_at - 300:  # 5 Minuten Puffer
            return access_token

        # SCHRITT 4: Token abgelaufen -> refresh (mit Blacklist-Protection)
        async with self._lock:
            # Double-Check Locking
            with get_conn() as conn:
                row_check = conn.execute(
                    "SELECT token_expires_at, access_token FROM twitch_raid_auth WHERE twitch_user_id = ?",
                    (twitch_user_id,),
                ).fetchone()
            
            if row_check:
                curr_expires_iso, curr_access = row_check
                curr_expires = datetime.fromisoformat(curr_expires_iso).timestamp()
                if time.time() < curr_expires - 300:
                    return curr_access

            log.info("Refreshing token for %s", twitch_login)
            try:
                # Refresh mit User-Info für Blacklist-Tracking
                token_data = await self.refresh_token(
                    refresh_token,
                    session,
                    twitch_user_id=twitch_user_id,
                    twitch_login=twitch_login,
                )
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

    async def get_valid_token_for_login(
        self, twitch_login: str, session: aiohttp.ClientSession
    ) -> Optional[tuple[str, str]]:
        """
        Liefert (twitch_user_id, access_token) für einen Login, falls autorisiert.
        """
        login = (twitch_login or "").strip().lower()
        if not login:
            return None
        with get_conn() as conn:
            row = conn.execute(
                "SELECT twitch_user_id FROM twitch_streamers WHERE LOWER(twitch_login) = ?",
                (login,),
            ).fetchone()
        if not row:
            return None
        twitch_user_id = row[0] if not hasattr(row, "keys") else row["twitch_user_id"]
        token = await self.get_valid_token(str(twitch_user_id), session)
        if token:
            return str(twitch_user_id), token
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
            # Flag im Streamer-Datensatz spiegeln, damit der Auto-Raid-Check konsistent bleibt
            conn.execute(
                "UPDATE twitch_streamers SET raid_bot_enabled = ? WHERE twitch_user_id = ?",
                (1 if enabled else 0, twitch_user_id),
            )
            conn.commit()
        log.info("Set raid_enabled=%s for user_id=%s", enabled, twitch_user_id)

    def has_enabled_auth(self, twitch_user_id: str) -> bool:
        """
        True, wenn ein OAuth-Grant mit raid_enabled=1 für den Streamer existiert.
        Nutzt DB-Check, damit wir vor Auto-Raids kurzschließen können.
        """
        with get_conn() as conn:
            row = conn.execute(
                "SELECT raid_enabled FROM twitch_raid_auth WHERE twitch_user_id = ?",
                (twitch_user_id,),
            ).fetchone()
        return bool(row and row[0])

    def get_scopes(self, twitch_user_id: str) -> list[str]:
        """Liefert die gespeicherten OAuth-Scopes für einen Streamer (lowercased, unabhängig von raid_enabled)."""
        try:
            with get_conn() as conn:
                row = conn.execute(
                    "SELECT scopes FROM twitch_raid_auth WHERE twitch_user_id = ?",
                    (twitch_user_id,),
                ).fetchone()
            scopes_raw = (row[0] if row else "") or ""
            scopes = [s.strip().lower() for s in scopes_raw.split() if s.strip()]
            return scopes
        except Exception:
            log.debug("get_scopes failed for %s", twitch_user_id, exc_info=True)
            return []


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
        self._bot_id = None   # Wird bei set_chat_bot gesetzt als Fallback
        self._cleanup_counter = 0
        
        # Cleanup-Task starten
        self._cleanup_task = asyncio.create_task(self._periodic_cleanup())

    async def cleanup(self):
        """Stoppt Hintergrund-Tasks."""
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                log.debug("Cleanup task cancelled")

    async def _periodic_cleanup(self):
        """
        Periodische Wartung:
        1. Cleanup abgelaufener Auth-States (alle 30min)
        2. Proaktiver Refresh von User-Tokens (alle 60min)
        """
        while True:
            await asyncio.sleep(1800)  # Alle 30 Minuten
            try:
                # 1. State Cleanup
                self.auth_manager.cleanup_states()
                
                # 2. Token Maintenance (nur bei jedem 2. Durchlauf = alle 60min)
                # Einfache Implementierung: Wir machen es einfach alle 30min,
                # die refresh_all_tokens Methode prüft ja die Expiry (2h Buffer).
                # Das schadet nicht und hält die Tokens frisch.
                if self.session:
                    await self.auth_manager.refresh_all_tokens(self.session)

                # Token Blacklist Cleanup (alle ~3.5 Tage = 7 * 30min Zyklen)
                self._cleanup_counter += 1
                if self._cleanup_counter % 7 == 0:
                    self.auth_manager.token_error_handler.cleanup_old_entries(days=30)

            except Exception:
                log.exception("Error during periodic raid bot maintenance")

    def set_chat_bot(self, chat_bot):
        """Setzt den Twitch Chat Bot für Recruitment-Nachrichten."""
        self.chat_bot = chat_bot
        # Bot-ID speichern damit complete_setup auch ohne chat_bot funktioniert
        if chat_bot:
            bot_id = getattr(chat_bot, "bot_id_safe", None) or getattr(chat_bot, "bot_id", None)
            if bot_id and str(bot_id).strip():
                self._bot_id = str(bot_id).strip()

    def set_discord_bot(self, discord_bot):
        """
        Setzt die Discord Bot-Instanz für Token-Error-Benachrichtigungen.

        Args:
            discord_bot: Discord Client/Bot Instanz
        """
        self.auth_manager.token_error_handler.discord_bot = discord_bot
        self.auth_manager._discord_bot = discord_bot
        log.info("Discord bot set for token error notifications")

    async def complete_setup_for_streamer(self, twitch_user_id: str, twitch_login: str):
        """
        Führt Aktionen nach erfolgreicher OAuth-Autorisierung aus:
        1. Bot als Moderator setzen
        2. Bestätigungsnachricht im Chat senden
        """
        log.info("Completing setup for streamer %s (%s)", twitch_login, twitch_user_id)
        
        # 1. Tokens holen
        tokens = await self.auth_manager.get_tokens_for_user(twitch_user_id, self.session)
        if not tokens:
            log.warning("Could not get tokens for %s to complete setup", twitch_login)
            return

        access_token, _ = tokens
        # Bot-ID: aus chat_bot wenn verfügbar, sonst aus gespeichertem _bot_id Fallback
        bot_id = None
        if self.chat_bot:
            bot_id = getattr(self.chat_bot, "bot_id_safe", None)
            if bot_id is None:
                bot_id_raw = getattr(self.chat_bot, "bot_id", None)
                bot_id = str(bot_id_raw).strip() if bot_id_raw and str(bot_id_raw).strip() else None
        if not bot_id:
            bot_id = getattr(self, "_bot_id", None)
        if not bot_id:
            # Letzte Chance: Bot-ID aus ENV
            import os
            bot_id = os.getenv("TWITCH_BOT_USER_ID", "").strip() or None
        if not bot_id:
            log.warning("complete_setup: Keine Bot-ID verfügbar für %s (chat_bot=%s). Setze TWITCH_BOT_USER_ID ENV.", twitch_login, "None" if not self.chat_bot else "set")
            return
        
        # 2. Bot als Moderator setzen
        if bot_id:
            try:
                url = f"{TWITCH_API_BASE}/moderation/moderators"
                params = {
                    "broadcaster_id": twitch_user_id,
                    "user_id": bot_id,
                }
                headers = {
                    "Client-ID": self.auth_manager.client_id,
                    "Authorization": f"Bearer {access_token}",
                }
                async with self.session.post(url, headers=headers, params=params) as r:
                    if r.status in {200, 204}:
                        log.info("Bot (ID: %s) is now moderator in %s's channel (ID: %s)", bot_id, twitch_login, twitch_user_id)
                    elif r.status == 422:
                        log.info("Bot (ID: %s) is already moderator in %s's channel", bot_id, twitch_login)
                    else:
                        txt = await r.text()
                        log.warning("Failed to add bot as moderator in %s: HTTP %s: %s (used broadcaster token)", twitch_login, r.status, txt)
            except Exception:
                log.exception("Error adding bot as moderator for %s", twitch_login)

        # 3. Bestätigungsnachricht senden
        if self.chat_bot:
            try:
                # Sicherstellen, dass der Bot im Channel ist
                await self.chat_bot.join(twitch_login, channel_id=twitch_user_id)
                await asyncio.sleep(2) # Etwas mehr Zeit geben, damit der Mod-Status im Chat "ankommt"
                
                # Nachricht im Stil des Screenshots
                message = "Deadlock Chatbot verbunden! Hallo! 🎮 deadlock.de"
                
                # Sende Nachricht (EventSub kompatibel via ChatBot Methode)
                if hasattr(self.chat_bot, "_send_chat_message"):
                    # Mock Channel-Objekt für die interne Methode
                    class MockChannel:
                        def __init__(self, login, uid):
                            self.name = login
                            self.id = uid
                    
                    await self.chat_bot._send_chat_message(MockChannel(twitch_login, twitch_user_id), message)
                elif hasattr(self.chat_bot, "send_message") and bot_id:
                    await self.chat_bot.send_message(str(twitch_user_id), str(bot_id), message)
                
                log.info("Sent auth success message to %s", twitch_login)
            except Exception:
                log.exception("Error sending auth success message to %s", twitch_login)

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

        Zeigt dem Streamer minimale Stats als Teaser (Avg Viewer, Peak).
        """
        if not self.chat_bot:
            log.debug("Chat bot not available for recruitment message")
            return

        # 1. Sofort beitreten, damit wir bereit sind
        try:
            target_id = None
            if target_stream_data:
                target_id = target_stream_data.get("user_id")
            
            if not target_id:
                # Fallback: ID über Login-Namen auflösen
                users = await self.chat_bot.fetch_users(names=[to_broadcaster_login])
                if users:
                    target_id = str(users[0].id)

            if not target_id:
                log.warning("Could not resolve user ID for recruitment message to %s", to_broadcaster_login)
                return

            await self.chat_bot.join(to_broadcaster_login, channel_id=target_id)
        except Exception:
            log.debug("Konnte Channel %s nicht vorab beitreten", to_broadcaster_login)

        # 2. 15 Sekunden warten, damit der Streamer den Raid-Alert verarbeiten kann
        log.info("Warte 15s vor Senden der Recruitment-Message an %s...", to_broadcaster_login)
        await asyncio.sleep(25.0)

        try:
            # 2. Anti-Spam Check: Haben wir diesen Streamer schon "kürzlich" geraidet?
            # Wir prüfen, ob es mehr als 1 erfolgreichen Raid in den letzten 14 Tagen gab.
            with get_conn() as conn:
                raid_check = conn.execute(
                    """
                    SELECT COUNT(*) FROM twitch_raid_history
                    WHERE to_broadcaster_id = ?
                      AND success = 1
                      AND executed_at > datetime('now', '-14 days')
                    """,
                    (target_id,),
                ).fetchone()
                recent_raids = raid_check[0] if raid_check else 0
            
            if recent_raids > 1:
                log.info(
                    "Skipping recruitment message to %s (Anti-Spam: %d raids in last 14 days)", 
                    to_broadcaster_login, recent_raids
                )
                return

            # 3. Nachricht vorbereiten (mit Stats Teaser)
            discord_invite = "discord.gg/z5TfVHuQq2"  # TODO: Aus ENV holen

            stats_teaser = ""
            try:
                with get_conn() as conn:
                    stats = conn.execute(
                        """
                        SELECT
                            ROUND(AVG(last_viewer_count)) as avg_viewers,
                            MAX(last_viewer_count) as peak_viewers
                        FROM twitch_stream_history
                        WHERE twitch_user_id = ?
                          AND last_viewer_count > 0
                        """,
                        (target_id,),
                    ).fetchone()

                if stats and stats[0]:
                    avg_viewers = int(stats[0])
                    peak_viewers = int(stats[1]) if stats[1] else 0
                    if peak_viewers > 0:
                        stats_teaser = f"Übrigens: Du hattest im Schnitt {avg_viewers} Viewer bei Deadlock, dein Peak war {peak_viewers}. Weitere Details haben wir auch – "
            except Exception:
                log.debug("Could not fetch stats for %s", to_broadcaster_login, exc_info=True)

            message = (
                f"Hey @{to_broadcaster_login}! "
                f"Du wurdest gerade von @{from_broadcaster_login} geraidet, einem unserer Deadlock Streamer-Partner! <3 "
                f"{stats_teaser}"
                f"Du kannst auch Teil der Community werden und auch Support zu erhalten – "
                f"schau gerne mal auf unserem Discord vorbei: {discord_invite} "
                f"Win-Win für alle Deadlock-Streamer! 🎮"
            )

            # 4. Sende Nachricht via Bot
            # TwitchIO 3.x: Nutze _send_chat_message helper (MockChannel)
            # Diese Methode existiert im chat_bot und funktioniert mit EventSub
            try:
                if hasattr(self.chat_bot, "_send_chat_message"):
                    # Mock Channel-Objekt für die interne Methode
                    class MockChannel:
                        def __init__(self, login, uid):
                            self.name = login
                            self.id = uid
                    
                    success = await self.chat_bot._send_chat_message(
                        MockChannel(to_broadcaster_login, target_id),
                        message
                    )
                    
                    if success:
                        log.info(
                            "Sent recruitment message in %s's chat (raided by %s)",
                            to_broadcaster_login,
                            from_broadcaster_login,
                        )
                    else:
                        log.warning(
                            "Failed to send recruitment message to %s (returned False)",
                            to_broadcaster_login,
                        )
                else:
                    log.debug(
                        "Chat bot does not have _send_chat_message method, skipping recruitment message to %s",
                        to_broadcaster_login,
                    )
            except Exception:
                log.exception(
                    "Failed to send recruitment message to %s (raided by %s)",
                    to_broadcaster_login,
                    from_broadcaster_login,
                )

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

    def _is_blacklisted(self, target_id: str, target_login: str) -> bool:
        """Prüft, ob ein Ziel auf der Blacklist steht."""
        try:
            with get_conn() as conn:
                row = conn.execute(
                    """
                    SELECT 1 FROM twitch_raid_blacklist
                    WHERE (target_id IS NOT NULL AND target_id = ?)
                       OR lower(target_login) = lower(?)
                    """,
                    (target_id, target_login),
                ).fetchone()
                return bool(row)
        except Exception:
            log.error("Error checking blacklist", exc_info=True)
            return False

    def _add_to_blacklist(self, target_id: str, target_login: str, reason: str):
        """Fügt ein Ziel zur Blacklist hinzu."""
        try:
            with get_conn() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO twitch_raid_blacklist (target_id, target_login, reason)
                    VALUES (?, ?, ?)
                    """,
                    (target_id, target_login, reason),
                )
                conn.commit()
            log.info(
                "Added %s (ID: %s) to raid blacklist. Reason: %s",
                target_login,
                target_id,
                reason,
            )
        except Exception:
            log.error("Error adding to blacklist", exc_info=True)

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

        Features:
        - Auto-Retry bei Fehlern (z.B. Ziel hat Raids deaktiviert)
        - Blacklist-Management für nicht raidbare Kanäle
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

        # Retry-Loop Setup
        max_attempts = 3
        exclude_ids = {broadcaster_id}
        cached_de_streams = None  # Cache für Fallback-Streams um API zu schonen

        for attempt in range(max_attempts):
            target = None
            is_partner_raid = False
            candidates_count = 0

            # 1. Partner-Kandidaten filtern
            # Wir prüfen Blacklist und bereits versuchte IDs
            partner_candidates = [
                s
                for s in online_partners
                if s.get("user_id") not in exclude_ids
                and not self._is_blacklisted(s.get("user_id"), s.get("user_login"))
            ]

            if partner_candidates:
                # Partner vorhanden -> Fairness-basierte Auswahl
                is_partner_raid = True
                target = self._select_fairest_candidate(partner_candidates, broadcaster_id)
                candidates_count = len(partner_candidates)

            # 2. Fallback (Deadlock-DE), falls kein Partner gefunden
            if not target and api and category_id:
                if cached_de_streams is None:
                    try:
                        log.info(
                            "No partners online for %s, fetching Deadlock-DE fallback",
                            broadcaster_login,
                        )
                        cached_de_streams = await api.get_streams_by_category(
                            category_id, language="de", limit=50
                        )
                    except Exception:
                        log.exception("Failed to get Deadlock-DE streams for fallback raid")
                        cached_de_streams = []

                # Fallback-Kandidaten filtern
                fallback_candidates = [
                    s
                    for s in cached_de_streams
                    if s.get("user_id") not in exclude_ids
                    and not self._is_blacklisted(s.get("user_id"), s.get("user_login"))
                ]

                if fallback_candidates:
                    # Sortiere nach kürzester Stream-Zeit
                    fallback_candidates.sort(key=lambda s: s.get("started_at", "9999"))
                    target = fallback_candidates[0]
                    candidates_count = len(fallback_candidates)
                    log.info(
                        "Selected fallback target: %s (from %d candidates)",
                        target["user_login"],
                        candidates_count,
                    )

            if not target:
                log.info(
                    "No valid raid target found for %s (Attempt %d/%d)",
                    broadcaster_login,
                    attempt + 1,
                    max_attempts,
                )
                return None

            # 3. Raid ausführen
            target_id = target["user_id"]
            target_login = target["user_login"]
            target_started_at = target.get("started_at", "")

            log.info(
                "Executing raid attempt %d/%d: %s -> %s",
                attempt + 1,
                max_attempts,
                broadcaster_login,
                target_login,
            )

            success, error = await self.raid_executor.start_raid(
                from_broadcaster_id=broadcaster_id,
                from_broadcaster_login=broadcaster_login,
                to_broadcaster_id=target_id,
                to_broadcaster_login=target_login,
                viewer_count=viewer_count,
                stream_duration_sec=stream_duration_sec,
                target_stream_started_at=target_started_at,
                candidates_count=candidates_count,
                session=self.session,
            )

            if success:
                # Bei Nicht-Partner-Raid: Chat-Nachricht senden
                if not is_partner_raid:
                    await self._send_recruitment_message(
                        from_broadcaster_login=broadcaster_login,
                        to_broadcaster_login=target_login,
                        target_stream_data=target,
                    )
                return target_login

            # Fehler-Behandlung
            exclude_ids.add(target_id)  # Diesen Kandidaten nicht nochmal versuchen

            # Check auf "Cannot be raided" (HTTP 400)
            if error and "cannot be raided" in error:
                log.warning(
                    "Raid failed: Target %s does not allow raids. Blacklisting and retrying.",
                    target_login,
                )
                self._add_to_blacklist(target_id, target_login, error)
                continue  # Nächster Versuch

            # Bei anderen Fehlern (z.B. API Down, Auth Error) brechen wir ab
            log.error(
                "Raid failed with non-retriable error: %s. Aborting.", error
            )
            return None

        return None
