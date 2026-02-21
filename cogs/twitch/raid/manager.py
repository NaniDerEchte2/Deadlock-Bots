# cogs/twitch/raid_manager.py
"""
Raid Bot Manager für automatische Twitch Raids zwischen Partnern.

Verwaltet:
- OAuth User Access Tokens für Streamer
- Automatische Raids beim Offline-Gehen
- Partner-Auswahl (niedrigste Viewer, optional niedrigste Follower)
- Raid-Metadaten und History
"""

import asyncio
import json
import logging
import os
import secrets
import time
from datetime import UTC, datetime
from urllib.parse import urlencode

import aiohttp
import discord

from ..api.token_error_handler import TokenErrorHandler
from ..storage import backfill_tracked_stats_from_category, get_conn

TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"  # noqa: S105
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
    "clips:edit",
    "channel:read:ads",
    "bits:read",
    "channel:read:hype_train",
    "moderator:read:chatters",
    "moderator:manage:shoutouts",
    "channel:read:redemptions",
]

RAID_TARGET_COOLDOWN_DAYS = 7  # Avoid repeating the same raid target if alternatives exist
RECRUIT_DISCORD_INVITE = (
    os.getenv("RECRUIT_DISCORD_INVITE") or ""
).strip() or "Discord: Server hinzufügen & Code eingeben: z5TfVHuQq2"
RECRUIT_DISCORD_INVITE_DIRECT = (
    os.getenv("RECRUIT_DISCORD_INVITE_DIRECT") or ""
).strip() or "https://discord.gg/z5TfVHuQq2"

_recruit_direct_invite_threshold_raw = (
    os.getenv("RECRUIT_DIRECT_INVITE_MAX_FOLLOWERS") or "120"
).strip()
try:
    RECRUIT_DIRECT_INVITE_MAX_FOLLOWERS = max(0, int(_recruit_direct_invite_threshold_raw))
except ValueError:
    RECRUIT_DIRECT_INVITE_MAX_FOLLOWERS = 120


def _parse_env_int(name: str, default: int = 0) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


STREAMER_ROLE_ID = _parse_env_int("STREAMER_ROLE_ID", 1313624729466441769)
STREAMER_GUILD_ID = _parse_env_int("STREAMER_GUILD_ID", 0)
FALLBACK_MAIN_GUILD_ID = _parse_env_int("MAIN_GUILD_ID", 0)

log = logging.getLogger("TwitchStreams.RaidManager")


def _mask_log_identifier(value: object, *, visible_prefix: int = 3, visible_suffix: int = 2) -> str:
    text = str(value or "").strip()
    if not text:
        return "<empty>"
    if len(text) <= visible_prefix + visible_suffix:
        return "***"
    return f"{text[:visible_prefix]}...{text[-visible_suffix:]}"


class RaidAuthManager:
    """Verwaltet OAuth User Access Tokens für Raid-Autorisierung."""

    def __init__(self, client_id: str, client_secret: str, redirect_uri: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self._state_tokens: dict[str, tuple[str, float]] = {}  # state -> (twitch_login, timestamp)
        self._pending_auth_urls: dict[str, str] = {}  # state -> full_twitch_auth_url
        self._lock = asyncio.Lock()
        self.token_error_handler = TokenErrorHandler()

        # Basis-URL für Short-Redirect ableiten (z.B. https://raid.earlysalty.com)
        _parts = redirect_uri.split("/twitch/", 1)
        self._base_url: str = _parts[0] if len(_parts) > 1 else ""

    # ------------------------------------------------------------------
    # Encryption helpers (field-level AES-256-GCM)
    # ------------------------------------------------------------------

    @staticmethod
    def _get_crypto_optional():
        """Gibt FieldCrypto zurück oder None wenn nicht verfügbar."""
        try:
            from service.field_crypto import get_crypto  # noqa: PLC0415

            return get_crypto()
        except Exception as exc:
            log.warning("field_crypto nicht verfügbar: %s", exc)
            return None

    def _try_decrypt(self, blob, aad: str, context: str):
        """Entschlüsselt ein enc-Blob; gibt None zurück wenn nicht möglich."""
        if not blob:
            return None
        crypto = self._get_crypto_optional()
        if not crypto:
            return None
        try:
            return crypto.decrypt_field(bytes(blob), aad)
        except Exception:
            log.warning(
                "Secure field decode failed [%s]; encrypted value will be ignored.",
                context,
            )
            log.debug("Secure field decode traceback [%s]", context, exc_info=True)
            return None

    def _try_encrypt(self, plaintext, aad: str, context: str):
        """Verschlüsselt Plaintext zu enc-Blob; gibt None zurück wenn nicht möglich."""
        if not plaintext:
            return None
        crypto = self._get_crypto_optional()
        if not crypto:
            return None
        try:
            return crypto.encrypt_field(str(plaintext), aad)
        except Exception:
            log.error(
                "Secure field encode failed [%s]; fallback remains active.",
                context,
            )
            log.debug("Secure field encode traceback [%s]", context, exc_info=True)
            return None

    def _resolve_token(self, enc_blob, aad: str, field_name: str, user_id: str):
        """
        Liest Token ausschließlich aus der verschlüsselten Spalte.

        Gibt None zurück, wenn enc fehlt oder nicht entschlüsselbar ist.
        """
        ctx = f"{field_name}|user={_mask_log_identifier(user_id)}"
        if enc_blob is None:
            log.error(
                "Encrypted field missing (enc=NULL) [%s] - plaintext fallback is disabled.",
                ctx,
            )
            return None

        decrypted = self._try_decrypt(enc_blob, aad, ctx)
        if decrypted is not None:
            log.debug("Encrypted field loaded [%s]", ctx)
            return decrypted

        log.error(
            "Encrypted field unreadable [%s] - plaintext fallback is disabled.",
            ctx,
        )
        return None

    def _write_token_refresh(
        self,
        conn,
        user_id: str,
        access_token: str,
        refresh_token: str,
        expires_at_iso: str,
    ) -> None:
        """
        Schreibt refreshte Tokens verschlüsselt in die DB.
        Klartext-Spalten werden zur Sicherheit nur noch mit 'ENC' überschrieben.
        """
        access_enc = self._try_encrypt(
            access_token,
            f"twitch_raid_auth|access_token|{user_id}|1",
            f"access_token|user={_mask_log_identifier(user_id)}",
        )
        refresh_enc = self._try_encrypt(
            refresh_token,
            f"twitch_raid_auth|refresh_token|{user_id}|1",
            f"refresh_token|user={_mask_log_identifier(user_id)}",
        )

        if access_enc is None or refresh_enc is None:
            log.error(
                "Refresh for user_id=%s: encrypted write failed; tokens NOT updated to avoid lockout.",
                _mask_log_identifier(user_id),
            )
            return

        conn.execute(
            """
            UPDATE twitch_raid_auth
               SET access_token = 'ENC', refresh_token = 'ENC',
                   access_token_enc = ?, refresh_token_enc = ?,
                   enc_version = 1, enc_kid = 'v1',
                   token_expires_at = ?, last_refreshed_at = CURRENT_TIMESTAMP
             WHERE twitch_user_id = ?
            """,
            (
                access_enc,
                refresh_enc,
                expires_at_iso,
                user_id,
            ),
        )

    # ------------------------------------------------------------------

    def generate_auth_url(self, twitch_login: str) -> str:
        """Generiert eine OAuth-URL für Streamer-Autorisierung."""
        state = secrets.token_urlsafe(16)
        self._state_tokens[state] = (twitch_login, time.time())

        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": " ".join(RAID_SCOPES),
            "state": state,
            "force_verify": "true",
        }
        return f"{TWITCH_AUTHORIZE_URL}?{urlencode(params)}"

    def generate_discord_button_url(self, twitch_login: str) -> str:
        """Generiert einen kurzen Redirect-URL für Discord-Buttons (max 512 Zeichen).

        Discord-Button-URLs dürfen max. 512 Zeichen lang sein.  Der volle Twitch-
        OAuth-URL überschreitet dieses Limit.  Wir speichern den vollen URL im
        _pending_auth_urls-Dict und geben einen kurzen /twitch/raid/go?state=…
        Redirect-Link zurück, den der Webserver dann weiterleitet.
        """
        state = secrets.token_urlsafe(16)
        ts = time.time()
        self._state_tokens[state] = (twitch_login, ts)

        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": " ".join(RAID_SCOPES),
            "state": state,
            "force_verify": "true",
        }
        full_url = f"{TWITCH_AUTHORIZE_URL}?{urlencode(params)}"
        self._pending_auth_urls[state] = full_url

        if self._base_url:
            return f"{self._base_url}/twitch/raid/go?state={state}"
        # Fallback: vollen URL trotzdem zurückgeben (kann bei sehr langen URLs fehlschlagen)
        return full_url

    def get_pending_auth_url(self, state: str) -> str | None:
        """Gibt den gespeicherten vollen OAuth-URL für einen State zurück (einmalig)."""
        # Wir löschen den Eintrag NICHT hier – verify_state macht das beim echten Callback.
        entry = self._pending_auth_urls.get(state)
        if not entry:
            return None
        # Prüfen ob der State noch gültig ist (10 Min TTL)
        token_data = self._state_tokens.get(state)
        if not token_data or time.time() - token_data[1] > 600:
            self._pending_auth_urls.pop(state, None)
            return None
        return entry

    def verify_state(self, state: str) -> str | None:
        """Verifiziert State-Token und gibt den zugehörigen Login zurück (max 10 Min alt)."""
        self._pending_auth_urls.pop(state, None)  # Cleanup Short-URL Eintrag
        data = self._state_tokens.pop(state, None)
        if not data:
            return None

        login, timestamp = data
        if time.time() - timestamp > 600:  # 10 Minuten TTL
            log.warning("OAuth state for %s expired", login)
            return None

        return login

    def cleanup_states(self) -> None:
        """Entfernt abgelaufene State-Tokens aus dem Speicher."""
        now = time.time()
        expired = [s for s, (_, ts) in self._state_tokens.items() if now - ts > 600]
        for s in expired:
            del self._state_tokens[s]
            self._pending_auth_urls.pop(s, None)
        if expired:
            log.debug("Cleaned up %d expired auth states", len(expired))

    async def exchange_code_for_token(self, code: str, session: aiohttp.ClientSession) -> dict:
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
                try:
                    err_body = await r.text()
                except Exception:
                    err_body = "<unreadable>"
                log.error(
                    "OAuth exchange failed with HTTP %s: %s",
                    r.status,
                    err_body[:500],
                )
                r.raise_for_status()
            return await r.json()

    async def refresh_token(
        self,
        refresh_token: str,
        session: aiohttp.ClientSession,
        twitch_user_id: str = None,
        twitch_login: str = None,
    ) -> dict:
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
                    "OAuth refresh request failed for %s with HTTP status %s",
                    twitch_login or "unknown",
                    r.status,
                )

                # Nur echte "invalid refresh token"/"invalid_grant" Antworten dürfen
                # den Blacklist-Counter erhöhen. Andere 400er sind oft Request-/Config-
                # Probleme und sollten keinen Streamer sperren.
                response_lc = txt.lower()
                parsed_message = ""
                parsed_error = ""
                try:
                    payload = json.loads(txt)
                    if isinstance(payload, dict):
                        parsed_message = str(payload.get("message", "")).lower()
                        parsed_error = str(payload.get("error", "")).lower()
                except Exception:
                    log.debug("OAuth refresh error payload was not valid JSON", exc_info=True)

                is_invalid_refresh_grant = r.status == 400 and (
                    "invalid refresh token" in response_lc
                    or "invalid refresh token" in parsed_message
                    or "invalid_grant" in response_lc
                    or "invalid_grant" in parsed_message
                    or "invalid_grant" in parsed_error
                )

                if is_invalid_refresh_grant and twitch_user_id and twitch_login:
                    log.warning(
                        "Invalid OAuth refresh grant for %s (ID: %s) - adding to blacklist",
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
                    if hasattr(self, "_discord_bot") and self._discord_bot:
                        asyncio.create_task(
                            self.token_error_handler.notify_token_error(
                                twitch_user_id=twitch_user_id,
                                twitch_login=twitch_login,
                                error_message=error_msg,
                            )
                        )
                elif r.status == 400 and twitch_login:
                    log.warning(
                        "OAuth refresh for %s returned HTTP 400 but not an invalid refresh grant; skipping blacklist increment",
                        twitch_login,
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
                SELECT twitch_user_id, twitch_login,
                       refresh_token_enc,
                       enc_version, token_expires_at
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
            _enc_v = row["enc_version"] or 1
            refresh_tok = self._resolve_token(
                row["refresh_token_enc"],
                f"twitch_raid_auth|refresh_token|{user_id}|{_enc_v}",
                "refresh_token",
                str(user_id),
            )
            expires_iso = row["token_expires_at"]

            # Sicherheits-Check: Falls doch auf Blacklist, überspringen
            if self.token_error_handler.is_token_blacklisted(user_id):
                continue

            # Cooldown: Nicht zu schnell nach einem Fehler erneut versuchen (min. 2h Abstand)
            if self.token_error_handler.has_recent_failure(user_id):
                log.debug(
                    "Skipping OAuth refresh for broadcaster=%s (recent failure, cooldown active)",
                    _mask_log_identifier(login),
                )
                continue

            # Bug Fix: NULL refresh_token → kein Refresh möglich, überspringen
            if not refresh_tok:
                log.warning(
                    "No refresh grant stored for broadcaster=%s (user_id=%s); skipping",
                    _mask_log_identifier(login),
                    _mask_log_identifier(user_id),
                )
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
                            (user_id,),
                        ).fetchone()

                    if current:
                        curr_iso = current[0]
                        if not curr_iso:
                            log.warning(
                                "Missing expiry timestamp in double-check for broadcaster=%s; skipping",
                                _mask_log_identifier(login),
                            )
                            continue
                        curr_ts = datetime.fromisoformat(
                            curr_iso.replace("Z", "+00:00")
                        ).timestamp()
                        # Bug Fix: time.time() statt veraltetes now_ts verwenden
                        if time.time() < curr_ts - 7200:
                            continue  # Wurde bereits refresht
                except Exception as exc:
                    # Bug Fix: Bei Exception im Double-Check SKIP statt Fallthrough zum Refresh
                    log.warning(
                        "Double-check failed for %s, skipping refresh to be safe: %s",
                        login,
                        exc,
                    )
                    continue

                log.debug("Auto-refreshing OAuth grant for %s (background maintenance)", login)
                try:
                    token_data = await self.refresh_token(
                        refresh_tok, session, twitch_user_id=user_id, twitch_login=login
                    )
                    new_access = token_data["access_token"]
                    new_refresh = token_data.get("refresh_token") or refresh_tok
                    expires_in = token_data.get("expires_in", 3600)

                    new_expires_at = datetime.now(UTC).timestamp() + expires_in
                    new_expires_iso = datetime.fromtimestamp(new_expires_at, UTC).isoformat()

                    with get_conn() as conn:
                        self._write_token_refresh(
                            conn, user_id, new_access, new_refresh, new_expires_iso
                        )
                        conn.commit()
                    # Erfolgreicher Refresh → eventuelle Fehler-Counter zurücksetzen
                    self.token_error_handler.clear_failure_count(user_id)
                    refreshed_count += 1
                    # Kleines Delay um API Spikes zu vermeiden
                    await asyncio.sleep(0.5)

                except Exception as exc:
                    if isinstance(exc, RuntimeError) and "Session is closed" in str(exc):
                        log.warning(
                            "Background refresh aborted for %s: shared HTTP session is closed",
                            login,
                        )
                        raise
                    log.error("Background refresh failed for %s: %s", login, exc)

        if refreshed_count > 0:
            log.debug("Maintenance: Refreshed %d user tokens", refreshed_count)

        return refreshed_count

    async def snapshot_and_flag_reauth(self) -> int:
        """Setzt needs_reauth=1 für alle und löscht Klartext-Tokens.
        Gibt Anzahl betroffener Zeilen zurück."""
        with get_conn() as conn:
            conn.execute("""
                UPDATE twitch_raid_auth SET
                    legacy_access_token  = NULL,
                    legacy_refresh_token = NULL,
                    legacy_scopes        = scopes,
                    legacy_saved_at      = CURRENT_TIMESTAMP,
                    needs_reauth         = 1,
                    access_token         = 'ENC',
                    refresh_token        = 'ENC'
                WHERE access_token <> 'ENC'
            """)
            count = conn.execute("SELECT changes()").fetchone()[0]
        log.info(
            "snapshot_and_flag_reauth: %d Tokens auf needs_reauth=1 gesetzt und Klartext gelöscht",
            count,
        )
        return count

    def clear_legacy_tokens_for_fully_authed(self) -> int:
        """
        Entfernt legacy_* Snapshots für Streamer mit needs_reauth=0.
        Diese Daten sind nach erfolgreichem Re-Auth nicht mehr nötig.
        """
        with get_conn() as conn:
            conn.execute(
                """
                UPDATE twitch_raid_auth
                   SET legacy_access_token  = NULL,
                       legacy_refresh_token = NULL,
                       legacy_scopes        = NULL,
                       legacy_saved_at      = NULL
                 WHERE needs_reauth = 0
                   AND (
                       legacy_access_token IS NOT NULL
                    OR legacy_refresh_token IS NOT NULL
                    OR legacy_scopes IS NOT NULL
                    OR legacy_saved_at IS NOT NULL
                   )
                """
            )
            count = conn.execute("SELECT changes()").fetchone()[0]
        if count:
            log.info(
                "legacy_* Snapshots für %d fully-authed Streamer entfernt (needs_reauth=0)",
                count,
            )
        return int(count or 0)

    def save_auth(
        self,
        twitch_user_id: str,
        twitch_login: str,
        access_token: str,
        refresh_token: str,
        expires_in: int,
        scopes: list[str],
    ) -> None:
        """Speichert OAuth-Tokens verschlüsselt in der Datenbank."""
        now = datetime.now(UTC)
        expires_at = now.timestamp() + expires_in
        expires_at_iso = datetime.fromtimestamp(expires_at, UTC).isoformat()
        authorized_at = now.isoformat()

        access_enc = self._try_encrypt(
            access_token,
            f"twitch_raid_auth|access_token|{twitch_user_id}|1",
            f"access_token|user={_mask_log_identifier(twitch_user_id)}",
        )
        refresh_enc = self._try_encrypt(
            refresh_token,
            f"twitch_raid_auth|refresh_token|{twitch_user_id}|1",
            f"refresh_token|user={_mask_log_identifier(twitch_user_id)}",
        )

        if access_enc is None or refresh_enc is None:
            log.error(
                "save_auth für user_id=%s: enc-Verschlüsselung fehlgeschlagen – Speicherung abgebrochen (Sicherheits-Policy).",
                _mask_log_identifier(twitch_user_id),
            )
            return

        with get_conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO twitch_raid_auth
                (twitch_user_id, twitch_login, access_token, refresh_token,
                 access_token_enc, refresh_token_enc, enc_version, enc_kid,
                 token_expires_at, scopes, authorized_at, raid_enabled)
                VALUES (?, ?, 'ENC', 'ENC', ?, ?, 1, 'v1', ?, ?, ?, 1)
                """,
                (
                    twitch_user_id,
                    twitch_login,
                    access_enc,
                    refresh_enc,
                    expires_at_iso,
                    " ".join(scopes),
                    authorized_at,
                ),
            )
            # Aktivieren, damit Auto-Raid unmittelbar nach OAuth freigeschaltet ist
            conn.execute(
                """
                UPDATE twitch_streamers
                   SET twitch_login = ?,
                       twitch_user_id = ?,
                       raid_bot_enabled = 1,
                       manual_verified_permanent = 1,
                       manual_verified_until = NULL,
                       manual_verified_at = COALESCE(manual_verified_at, CURRENT_TIMESTAMP),
                       manual_partner_opt_out = 0,
                       is_monitored_only = 0,
                       is_on_discord = CASE
                           WHEN COALESCE(discord_user_id, '') <> '' THEN 1
                           ELSE is_on_discord
                       END
                 WHERE twitch_user_id = ?
                    OR lower(twitch_login) = lower(?)
                """,
                (twitch_login, twitch_user_id, twitch_user_id, twitch_login),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO twitch_streamers
                    (twitch_login, twitch_user_id, raid_bot_enabled, manual_verified_permanent,
                     manual_verified_until, manual_verified_at, manual_partner_opt_out)
                VALUES (?, ?, 1, 1, NULL, CURRENT_TIMESTAMP, 0)
                """,
                (twitch_login, twitch_user_id),
            )
            # Re-Auth abgeschlossen: needs_reauth zurücksetzen
            conn.execute(
                """
                UPDATE twitch_raid_auth
                   SET needs_reauth = 0,
                       legacy_access_token = NULL,
                       legacy_refresh_token = NULL,
                       legacy_scopes = NULL,
                       legacy_saved_at = NULL
                WHERE twitch_user_id = ?
                """,
                (twitch_user_id,),
            )
            copied = backfill_tracked_stats_from_category(conn, twitch_login)
            if copied:
                log.info(
                    "Backfilled %d category samples into tracked for %s during raid auth save",
                    copied,
                    twitch_login,
                )
            conn.commit()

        # Bei erfolgreicher Auth: Von Blacklist entfernen (falls vorhanden)
        self.token_error_handler.remove_from_blacklist(twitch_user_id)

        log.info("Saved raid auth for %s (user_id=%s)", twitch_login, twitch_user_id)

    async def get_tokens_for_user(
        self, twitch_user_id: str, session: aiohttp.ClientSession
    ) -> tuple[str, str] | None:
        """
        Holt Access- UND Refresh-Token für einen User.
        Erneuert den Token automatisch, falls abgelaufen.
        Wird bewusst auch genutzt, wenn raid_enabled=0 (Chat-Bot/Moderation).
        """
        # Blacklist check
        if self.token_error_handler.is_token_blacklisted(twitch_user_id):
            return None

        # Cooldown: Nicht zu schnell nach einem Fehler erneut versuchen
        if self.token_error_handler.has_recent_failure(twitch_user_id):
            log.debug(
                "Auth lookup cooldown active for user_id=%s",
                _mask_log_identifier(twitch_user_id),
            )
            return None

        with get_conn() as conn:
            row = conn.execute(
                """
                SELECT access_token_enc, refresh_token_enc,
                       enc_version, token_expires_at, twitch_login
                FROM twitch_raid_auth
                WHERE twitch_user_id = ?
                """,
                (twitch_user_id,),
            ).fetchone()

        if not row:
            return None

        _enc_v = row["enc_version"] or 1
        _uid = str(twitch_user_id)
        access_token = self._resolve_token(
            row["access_token_enc"],
            f"twitch_raid_auth|access_token|{_uid}|{_enc_v}",
            "access_token",
            _uid,
        )
        refresh_token = self._resolve_token(
            row["refresh_token_enc"],
            f"twitch_raid_auth|refresh_token|{_uid}|{_enc_v}",
            "refresh_token",
            _uid,
        )
        if not access_token or not refresh_token:
            log.warning(  # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
                "Missing encrypted tokens for user_id=%s during auth lookup",
                _mask_log_identifier(twitch_user_id),
            )
            return None
        expires_at_iso = row["token_expires_at"]
        twitch_login = row["twitch_login"]
        expires_at = datetime.fromisoformat(expires_at_iso.replace("Z", "+00:00")).timestamp()

        # Token noch gültig? (5 Minuten Puffer)
        if time.time() < expires_at - 300:
            return access_token, refresh_token

        # Token abgelaufen -> refresh
        async with self._lock:
            # Erneuter Check innerhalb des Locks (Double-Check Locking Pattern)
            with get_conn() as conn:
                row_check = conn.execute(
                    """SELECT token_expires_at,
                              access_token_enc,
                              refresh_token_enc,
                              enc_version
                       FROM twitch_raid_auth WHERE twitch_user_id = ?""",
                    (twitch_user_id,),
                ).fetchone()

            if row_check:
                _dc_uid = str(twitch_user_id)
                _dc_v = row_check["enc_version"] or 1
                curr_expires_iso = row_check["token_expires_at"]
                curr_access = self._resolve_token(
                    row_check["access_token_enc"],
                    f"twitch_raid_auth|access_token|{_dc_uid}|{_dc_v}",
                    "access_token.dc",
                    _dc_uid,
                )
                curr_refresh = self._resolve_token(
                    row_check["refresh_token_enc"],
                    f"twitch_raid_auth|refresh_token|{_dc_uid}|{_dc_v}",
                    "refresh_token.dc",
                    _dc_uid,
                )
                curr_expires = datetime.fromisoformat(
                    curr_expires_iso.replace("Z", "+00:00")
                ).timestamp()
                if time.time() < curr_expires - 300 and curr_access and curr_refresh:
                    return curr_access, curr_refresh

            log.info(
                "Refreshing OAuth grant for broadcaster=%s (auth lookup)",
                _mask_log_identifier(twitch_login),
            )
            try:
                token_data = await self.refresh_token(
                    refresh_token,
                    session,
                    twitch_user_id=twitch_user_id,
                    twitch_login=twitch_login,
                )
                new_access_token = token_data["access_token"]
                new_refresh_token = token_data.get("refresh_token") or refresh_token
                expires_in = token_data.get("expires_in", 3600)

                # Token in DB aktualisieren
                new_expires_at = datetime.now(UTC).timestamp() + expires_in
                new_expires_at_iso = datetime.fromtimestamp(new_expires_at, UTC).isoformat()

                with get_conn() as conn:
                    self._write_token_refresh(
                        conn,
                        twitch_user_id,
                        new_access_token,
                        new_refresh_token,
                        new_expires_at_iso,
                    )
                    conn.commit()

                self.token_error_handler.clear_failure_count(twitch_user_id)
                return new_access_token, new_refresh_token
            except Exception:
                log.exception("Failed to refresh OAuth grant for %s", twitch_login)
                return None

    async def get_valid_token(
        self, twitch_user_id: str, session: aiohttp.ClientSession
    ) -> str | None:
        """
        Holt ein gültiges Access Token für den Streamer.
        Erneuert es automatisch, falls abgelaufen.

        WICHTIG: Wenn der Token auf der Blacklist steht (ungültiger Refresh-Token),
        wird None zurückgegeben ohne Refresh-Versuch.
        """
        # SCHRITT 1: Blacklist-Check BEVOR wir überhaupt zur DB gehen
        if self.token_error_handler.is_token_blacklisted(twitch_user_id):
            log.warning(
                "OAuth grant for user_id=%s is blacklisted - skipping refresh attempt",
                twitch_user_id,
            )
            return None

        # Cooldown: Wenn kürzlich ein Fehler aufgetreten ist, nicht sofort nochmal versuchen
        if self.token_error_handler.has_recent_failure(twitch_user_id):
            log.debug(
                "OAuth grant for user_id=%s has recent failure, cooldown active - returning None",
                twitch_user_id,
            )
            return None

        # SCHRITT 2: Token aus DB holen
        with get_conn() as conn:
            row = conn.execute(
                """
                SELECT access_token_enc, refresh_token_enc,
                       enc_version, token_expires_at, twitch_login
                FROM twitch_raid_auth
                WHERE twitch_user_id = ? AND raid_enabled = 1
                """,
                (twitch_user_id,),
            ).fetchone()

        if not row:
            return None

        _enc_v = row["enc_version"] or 1
        _uid = str(twitch_user_id)
        access_token = self._resolve_token(
            row["access_token_enc"],
            f"twitch_raid_auth|access_token|{_uid}|{_enc_v}",
            "access_token",
            _uid,
        )
        refresh_token = self._resolve_token(
            row["refresh_token_enc"],
            f"twitch_raid_auth|refresh_token|{_uid}|{_enc_v}",
            "refresh_token",
            _uid,
        )
        if not access_token:
            log.warning(  # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
                "Missing encrypted access token for user_id=%s",
                _mask_log_identifier(twitch_user_id),
            )
            return None
        expires_at_iso = row["token_expires_at"]
        twitch_login = row["twitch_login"]
        expires_at = datetime.fromisoformat(expires_at_iso.replace("Z", "+00:00")).timestamp()

        # SCHRITT 3: Token noch gültig?
        if time.time() < expires_at - 300:  # 5 Minuten Puffer
            return access_token

        # SCHRITT 4: Token abgelaufen -> refresh (mit Blacklist-Protection)
        async with self._lock:
            # Double-Check Locking
            with get_conn() as conn:
                row_check = conn.execute(
                    """SELECT token_expires_at,
                              access_token_enc, enc_version
                       FROM twitch_raid_auth WHERE twitch_user_id = ?""",
                    (twitch_user_id,),
                ).fetchone()

            if row_check:
                _dc_uid = str(twitch_user_id)
                _dc_v = row_check["enc_version"] or 1
                curr_expires_iso = row_check["token_expires_at"]
                curr_access = self._resolve_token(
                    row_check["access_token_enc"],
                    f"twitch_raid_auth|access_token|{_dc_uid}|{_dc_v}",
                    "access_token.dc",
                    _dc_uid,
                )
                curr_expires = datetime.fromisoformat(
                    curr_expires_iso.replace("Z", "+00:00")
                ).timestamp()
                if time.time() < curr_expires - 300 and curr_access:
                    return curr_access

            if not refresh_token:
                log.warning(  # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
                    "Missing encrypted refresh token for user_id=%s - cannot refresh",
                    _mask_log_identifier(twitch_user_id),
                )
                return None

            log.info("Refreshing OAuth grant for %s", twitch_login)
            try:
                # Refresh mit User-Info für Blacklist-Tracking
                token_data = await self.refresh_token(
                    refresh_token,
                    session,
                    twitch_user_id=twitch_user_id,
                    twitch_login=twitch_login,
                )
                new_access_token = token_data["access_token"]
                new_refresh_token = token_data.get("refresh_token") or refresh_token
                expires_in = token_data.get("expires_in", 3600)

                # Token in DB aktualisieren
                new_expires_at = datetime.now(UTC).timestamp() + expires_in
                new_expires_at_iso = datetime.fromtimestamp(new_expires_at, UTC).isoformat()

                with get_conn() as conn:
                    self._write_token_refresh(
                        conn,
                        twitch_user_id,
                        new_access_token,
                        new_refresh_token,
                        new_expires_at_iso,
                    )
                    conn.commit()

                self.token_error_handler.clear_failure_count(twitch_user_id)
                return new_access_token
            except Exception:
                log.exception("Failed to refresh OAuth grant for %s", twitch_login)
                return None

    async def get_valid_token_for_login(
        self, twitch_login: str, session: aiohttp.ClientSession
    ) -> tuple[str, str] | None:
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
        login_hint = ""
        discord_user_id = ""
        with get_conn() as conn:
            auth_row = conn.execute(
                "SELECT twitch_login FROM twitch_raid_auth WHERE twitch_user_id = ?",
                (twitch_user_id,),
            ).fetchone()
            if auth_row:
                login_hint = str(
                    auth_row[0] if not hasattr(auth_row, "keys") else auth_row["twitch_login"] or ""
                )

            streamer_row = conn.execute(
                """
                SELECT discord_user_id
                FROM twitch_streamers
                WHERE twitch_user_id = ?
                   OR (? <> '' AND LOWER(twitch_login) = LOWER(?))
                LIMIT 1
                """,
                (twitch_user_id, login_hint, login_hint),
            ).fetchone()
            if streamer_row:
                discord_user_id = str(
                    streamer_row[0]
                    if not hasattr(streamer_row, "keys")
                    else streamer_row["discord_user_id"] or ""
                ).strip()

            conn.execute(
                "DELETE FROM twitch_raid_auth WHERE twitch_user_id = ?",
                (twitch_user_id,),
            )
            conn.execute(
                """
                UPDATE twitch_streamers
                   SET raid_bot_enabled = 0,
                       manual_verified_permanent = 0,
                       manual_verified_until = NULL,
                       manual_verified_at = NULL,
                       manual_partner_opt_out = 1
                 WHERE twitch_user_id = ?
                    OR (? <> '' AND LOWER(twitch_login) = LOWER(?))
                """,
                (twitch_user_id, login_hint, login_hint),
            )
            conn.commit()

        if discord_user_id and hasattr(self.token_error_handler, "schedule_streamer_role_sync"):
            self.token_error_handler.schedule_streamer_role_sync(
                discord_user_id,
                should_have_role=False,
                reason="Twitch-Bot Autorisierung entzogen",
            )
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
        reason: str = "auto_raid_on_offline",
    ) -> tuple[bool, str | None]:
        """
        Startet einen Raid von from_broadcaster zu to_broadcaster.

        Returns:
            (success, error_message)
        """
        # Access Token holen
        access_token = await self.auth_manager.get_valid_token(from_broadcaster_id, session)
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
                reason,
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
                        reason,
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
                    reason,
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
                reason,
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
        reason: str,
        success: bool,
        error_message: str | None,
    ) -> None:
        """Speichert Raid-Metadaten in der Datenbank."""
        history_reason = (reason or "").strip() or "auto_raid_on_offline"
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
                    history_reason,
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
    - Wählt Partner nach niedrigsten Viewern (Tie-Breaker: Follower, dann Stream-Zeit)
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
        self._session = session
        self.chat_bot = None  # Wird später gesetzt
        self._bot_id = None  # Wird bei set_chat_bot gesetzt als Fallback
        self._cog = None  # Referenz zum TwitchStreamCog für EventSub subscriptions

        # Pending Raids: {to_broadcaster_id: (from_broadcaster_login, target_stream_data, timestamp, is_partner_raid, viewer_count)}
        self._pending_raids: dict[str, tuple[str, dict | None, float, bool, int]] = {}
        # Unterdrückt den nächsten Offline-Auto-Raid, wenn kurz zuvor ein manueller/externer Raid erkannt wurde.
        self._manual_raid_suppression: dict[str, float] = {}

        # Cleanup-Task starten
        self._cleanup_task = asyncio.create_task(self._periodic_cleanup())

    @property
    def session(self) -> aiohttp.ClientSession | None:
        """Return an active HTTP session; refresh from cog/api if the cached one is closed."""
        if self._session is not None and not self._session.closed:
            return self._session

        cog = getattr(self, "_cog", None)
        api = getattr(cog, "api", None) if cog is not None else None
        if api is not None:
            try:
                refreshed = api.get_http_session()
                if refreshed is not None and not refreshed.closed:
                    if self._session is not refreshed:
                        log.warning(
                            "RaidBot detected closed HTTP session; switched to fresh TwitchAPI session"
                        )
                    self._session = refreshed
                    return refreshed
            except Exception:
                log.debug(
                    "RaidBot could not refresh HTTP session from TwitchAPI",
                    exc_info=True,
                )
        return None

    @session.setter
    def session(self, value: aiohttp.ClientSession | None) -> None:
        self._session = value

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
        2. Proaktiver Refresh von User-Tokens (alle 30min; intern expiry-gebremst)
        3. Cleanup alter pending raids (alle 2min)
        """
        state_cleanup_interval = 1800.0
        token_refresh_interval = 1800.0
        blacklist_cleanup_interval = 7 * 1800.0
        pending_raid_cleanup_interval = 120.0
        grace_period_check_interval = 3600.0  # stündlich

        last_state_cleanup = 0.0
        last_token_refresh = 0.0
        last_blacklist_cleanup = 0.0
        last_raid_cleanup = 0.0
        last_grace_period_check = 0.0
        while True:
            await asyncio.sleep(60)  # Loop-Tick (Wartungs-Tasks laufen in eigenen Intervallen)
            try:
                now = time.time()

                # 1. State Cleanup (alle 30min)
                if now - last_state_cleanup >= state_cleanup_interval:
                    self.auth_manager.cleanup_states()
                    last_state_cleanup = now

                # 2. Token Maintenance (alle 30min; refresh_all_tokens prüft intern Expiry)
                if now - last_token_refresh >= token_refresh_interval:
                    active_session = self.session
                    if active_session is None:
                        log.warning("Skipping token maintenance: no active HTTP session available")
                    else:
                        try:
                            await self.auth_manager.refresh_all_tokens(active_session)
                        except RuntimeError as exc:
                            if "Session is closed" in str(exc):
                                self.session = None
                                log.warning(
                                    "Token maintenance deferred: shared HTTP session closed; retrying next tick"
                                )
                            else:
                                raise
                        else:
                            self.auth_manager.clear_legacy_tokens_for_fully_authed()
                            last_token_refresh = now

                # Token Blacklist Cleanup (alle 3.5h)
                if now - last_blacklist_cleanup >= blacklist_cleanup_interval:
                    self.auth_manager.token_error_handler.cleanup_old_entries(days=30)
                    last_blacklist_cleanup = now

                # Grace-Period Check (stündlich): Erinnerung + Rolle entfernen bei Ablauf
                if now - last_grace_period_check >= grace_period_check_interval:
                    await self.auth_manager.token_error_handler.check_grace_periods()
                    last_grace_period_check = now

                # 3. Pending Raids Cleanup (alle 2min)
                if now - last_raid_cleanup >= pending_raid_cleanup_interval:
                    self._cleanup_stale_pending_raids()
                    self._cleanup_expired_manual_raid_suppressions()
                    last_raid_cleanup = now

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
        log.debug("Discord bot set for token error notifications")

    def set_cog(self, cog):
        """
        Setzt die Cog-Referenz für dynamische EventSub subscriptions.

        Args:
            cog: TwitchStreamCog Instanz
        """
        self._cog = cog
        log.debug("Cog reference set for dynamic EventSub subscriptions")

    def mark_manual_raid_started(self, broadcaster_id: str, ttl_seconds: float = 300.0) -> None:
        """Unterdrückt den nächsten Offline-Auto-Raid für einen Streamer (z.B. nach !raid/!traid)."""
        broadcaster_key = str(broadcaster_id or "").strip()
        if not broadcaster_key:
            return
        ttl = max(30.0, float(ttl_seconds or 0.0))
        self._manual_raid_suppression[broadcaster_key] = time.time() + ttl

    def is_offline_auto_raid_suppressed(self, broadcaster_id: str) -> bool:
        """True, wenn für den Streamer aktuell eine manuelle-Raid-Sperre aktiv ist."""
        broadcaster_key = str(broadcaster_id or "").strip()
        if not broadcaster_key:
            return False
        now = time.time()
        until = self._manual_raid_suppression.get(broadcaster_key)
        if until is None:
            return False
        if now <= until:
            return True
        self._manual_raid_suppression.pop(broadcaster_key, None)
        return False

    def _resolve_streamer_id_by_login(self, broadcaster_login: str) -> str | None:
        """Best-effort: löst eine Twitch-User-ID aus twitch_streamers über den Login auf."""
        login_key = str(broadcaster_login or "").strip().lower()
        if not login_key:
            return None
        try:
            with get_conn() as conn:
                row = conn.execute(
                    "SELECT twitch_user_id FROM twitch_streamers WHERE LOWER(twitch_login) = ?",
                    (login_key,),
                ).fetchone()
            if not row:
                return None
            resolved = row["twitch_user_id"] if hasattr(row, "keys") else row[0]
            resolved_key = str(resolved or "").strip()
            return resolved_key or None
        except Exception:
            log.debug(
                "Konnte broadcaster_id nicht über Login auflösen: %s",
                login_key,
                exc_info=True,
            )
            return None

    def _cleanup_expired_manual_raid_suppressions(self) -> None:
        """Entfernt abgelaufene Einträge aus dem Manual-Raid-Suppression-Cache."""
        now = time.time()
        expired = [
            broadcaster_id
            for broadcaster_id, until in self._manual_raid_suppression.items()
            if now > float(until or 0.0)
        ]
        for broadcaster_id in expired:
            self._manual_raid_suppression.pop(broadcaster_id, None)
        if expired:
            log.debug("Cleaned up %d expired manual raid suppressions", len(expired))

    @staticmethod
    def _normalize_discord_user_id(raw: str | None) -> str | None:
        candidate = str(raw or "").strip()
        if candidate and candidate.isdigit():
            return candidate
        return None

    def _iter_role_guild_candidates(
        self, discord_bot: discord.Client | None
    ) -> list[discord.Guild]:
        if discord_bot is None:
            return []

        candidates: list[discord.Guild] = []
        seen: set[int] = set()
        for guild_id in (STREAMER_GUILD_ID, FALLBACK_MAIN_GUILD_ID):
            if guild_id and guild_id not in seen:
                seen.add(guild_id)
                guild = discord_bot.get_guild(guild_id)
                if guild is not None:
                    candidates.append(guild)

        if not candidates:
            candidates.extend(getattr(discord_bot, "guilds", []))
        return candidates

    async def _resolve_discord_display_name(self, discord_user_id: str | None) -> str | None:
        normalized_id = self._normalize_discord_user_id(discord_user_id)
        if not normalized_id:
            return None

        discord_bot = getattr(self.auth_manager, "_discord_bot", None)
        if discord_bot is None:
            return None

        user_id_int = int(normalized_id)
        user = discord_bot.get_user(user_id_int)
        if user is None:
            try:
                user = await discord_bot.fetch_user(user_id_int)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return None

        if user is None:
            return None
        return (
            str(
                getattr(user, "global_name", None)
                or getattr(user, "display_name", None)
                or getattr(user, "name", None)
                or ""
            ).strip()
            or None
        )

    async def _apply_streamer_role(
        self,
        discord_user_id: str | None,
        *,
        should_have_role: bool,
        reason: str,
    ) -> None:
        if STREAMER_ROLE_ID <= 0:
            return

        normalized_id = self._normalize_discord_user_id(discord_user_id)
        if not normalized_id:
            return

        discord_bot = getattr(self.auth_manager, "_discord_bot", None)
        if discord_bot is None:
            return

        user_id_int = int(normalized_id)
        for guild in self._iter_role_guild_candidates(discord_bot):
            role = guild.get_role(STREAMER_ROLE_ID)
            if role is None:
                continue

            member = guild.get_member(user_id_int)
            if member is None:
                try:
                    member = await guild.fetch_member(user_id_int)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    member = None

            if member is None:
                continue

            try:
                has_role = role in member.roles
                if should_have_role and not has_role:
                    await member.add_roles(role, reason=reason)
                    log.info(
                        "Streamer role granted to %s in guild %s",
                        normalized_id,
                        guild.id,
                    )
                elif (not should_have_role) and has_role:
                    await member.remove_roles(role, reason=reason)
                    log.info(
                        "Streamer role removed from %s in guild %s",
                        normalized_id,
                        guild.id,
                    )
            except discord.Forbidden:
                log.warning("Missing permission to sync streamer role in guild %s", guild.id)
            except discord.HTTPException:
                log.warning(
                    "Discord API error while syncing streamer role in guild %s",
                    guild.id,
                )

    async def _sync_partner_state_after_auth(
        self,
        twitch_user_id: str,
        twitch_login: str,
        *,
        state_discord_user_id: str | None = None,
    ) -> str | None:
        provided_discord_id = self._normalize_discord_user_id(state_discord_user_id)
        existing_discord_id: str | None = None
        existing_display_name: str | None = None

        with get_conn() as conn:
            row = conn.execute(
                """
                SELECT discord_user_id, discord_display_name
                FROM twitch_streamers
                WHERE twitch_user_id = ?
                   OR lower(twitch_login) = lower(?)
                LIMIT 1
                """,
                (twitch_user_id, twitch_login),
            ).fetchone()
            if row:
                existing_discord_id = self._normalize_discord_user_id(
                    row[0] if not hasattr(row, "keys") else row["discord_user_id"]
                )
                existing_display_name = (
                    str(
                        row[1] if not hasattr(row, "keys") else row["discord_display_name"] or ""
                    ).strip()
                    or None
                )

        final_discord_id = provided_discord_id or existing_discord_id
        final_display_name = existing_display_name or await self._resolve_discord_display_name(
            final_discord_id
        )

        is_on_discord_value = 1 if final_discord_id else 0
        with get_conn() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO twitch_streamers
                    (twitch_login, twitch_user_id, discord_user_id, discord_display_name,
                     is_on_discord, manual_verified_permanent, manual_verified_until,
                     manual_verified_at, manual_partner_opt_out, raid_bot_enabled)
                VALUES (?, ?, ?, ?, ?, 1, NULL, CURRENT_TIMESTAMP, 0, 1)
                """,
                (
                    twitch_login,
                    twitch_user_id,
                    final_discord_id,
                    final_display_name,
                    is_on_discord_value,
                ),
            )
            conn.execute(
                """
                UPDATE twitch_streamers
                   SET twitch_login = ?,
                       twitch_user_id = ?,
                       discord_user_id = ?,
                       discord_display_name = ?,
                       is_on_discord = ?,
                       manual_verified_permanent = 1,
                       manual_verified_until = NULL,
                       manual_verified_at = COALESCE(manual_verified_at, CURRENT_TIMESTAMP),
                       manual_partner_opt_out = 0,
                       is_monitored_only = 0,
                       raid_bot_enabled = 1
                 WHERE twitch_user_id = ?
                    OR lower(twitch_login) = lower(?)
                """,
                (
                    twitch_login,
                    twitch_user_id,
                    final_discord_id,
                    final_display_name,
                    is_on_discord_value,
                    twitch_user_id,
                    twitch_login,
                ),
            )
            copied = backfill_tracked_stats_from_category(conn, twitch_login)
            if copied:
                log.info(
                    "Backfilled %d category samples into tracked for %s during partner sync",
                    copied,
                    twitch_login,
                )
            conn.commit()

        if final_discord_id:
            await self._apply_streamer_role(
                final_discord_id,
                should_have_role=True,
                reason="Twitch-Bot erfolgreich autorisiert",
            )
        return final_discord_id

    async def complete_setup_for_streamer(
        self,
        twitch_user_id: str,
        twitch_login: str,
        state_discord_user_id: str | None = None,
    ):
        """
        Führt Aktionen nach erfolgreicher OAuth-Autorisierung aus:
        1. Bot als Moderator setzen
        2. Bestätigungsnachricht im Chat senden
        """
        log.info("Completing setup for streamer %s (%s)", twitch_login, twitch_user_id)

        try:
            await self._sync_partner_state_after_auth(
                twitch_user_id,
                twitch_login,
                state_discord_user_id=state_discord_user_id,
            )
        except Exception:
            log.exception(
                "Failed to sync partner state after auth for %s (%s)",
                twitch_login,
                twitch_user_id,
            )

        # 1. Tokens holen
        tokens = await self.auth_manager.get_tokens_for_user(twitch_user_id, self.session)
        if not tokens:
            log.warning("Could not load OAuth grant for %s to complete setup", twitch_login)
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
            log.warning(
                "complete_setup: Keine Bot-ID verfügbar für %s (chat_bot=%s). Setze TWITCH_BOT_USER_ID ENV.",
                twitch_login,
                "None" if not self.chat_bot else "set",
            )
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
                        log.info(
                            "Bot (ID: %s) is now moderator in %s's channel (ID: %s)",
                            bot_id,
                            twitch_login,
                            twitch_user_id,
                        )
                    elif r.status == 422:
                        log.info(
                            "Bot (ID: %s) is already moderator in %s's channel",
                            bot_id,
                            twitch_login,
                        )
                    else:
                        txt = await r.text()
                        if r.status == 400 and "already a mod" in txt.lower():
                            log.info(
                                "Bot (ID: %s) is already moderator in %s's channel (HTTP 400 variant)",
                                bot_id,
                                twitch_login,
                            )
                        else:
                            log.warning(
                                "Failed to add bot as moderator in %s: HTTP %s (used broadcaster grant)",
                                _mask_log_identifier(twitch_login),
                                r.status,
                            )
            except Exception:
                log.exception("Error adding bot as moderator for %s", twitch_login)

        # 3. Bestätigungsnachricht senden
        if self.chat_bot:
            try:
                # Sicherstellen, dass der Bot im Channel ist
                await self.chat_bot.join(twitch_login, channel_id=twitch_user_id)
                await asyncio.sleep(
                    2
                )  # Etwas mehr Zeit geben, damit der Mod-Status im Chat "ankommt"

                # Nachricht im Stil des Screenshots
                message = "Deadlock Chatbot Guard verbunden! 🎮"
                commands_public = (
                    "Commands für alle: "
                    "!ping (Bot-Status) | "
                    "!clip [beschreibung] (Clip erstellen) | "
                    "!raid_history (letzte Raids)"
                )
                commands_mod = (
                    "Mod-Commands: "
                    "!raid / !traid (Raid starten) | "
                    "!raid_status (Bot-Status) | "
                    "!uban / !unban (letzten Auto-Ban aufheben) | "
                    "!silentban / !silentraid (Benachrichtigungen an/aus)"
                )

                # Sende Nachrichten (EventSub kompatibel via ChatBot Methode)
                if hasattr(self.chat_bot, "_send_chat_message"):
                    # Mock Channel-Objekt für die interne Methode
                    class MockChannel:
                        def __init__(self, login, uid):
                            self.name = login
                            self.id = uid

                    mock_ch = MockChannel(twitch_login, twitch_user_id)
                    await self.chat_bot._send_chat_message(mock_ch, message)
                    await asyncio.sleep(1)
                    await self.chat_bot._send_chat_message(mock_ch, commands_public)
                    await asyncio.sleep(1)
                    await self.chat_bot._send_chat_message(mock_ch, commands_mod)
                elif hasattr(self.chat_bot, "send_message") and bot_id:
                    await self.chat_bot.send_message(str(twitch_user_id), str(bot_id), message)
                    await asyncio.sleep(1)
                    await self.chat_bot.send_message(
                        str(twitch_user_id), str(bot_id), commands_public
                    )
                    await asyncio.sleep(1)
                    await self.chat_bot.send_message(str(twitch_user_id), str(bot_id), commands_mod)

                log.info("Sent auth success message to %s", twitch_login)
            except Exception:
                log.exception("Error sending auth success message to %s", twitch_login)

    def _cleanup_stale_pending_raids(self):
        """
        Entfernt pending raids, die älter als 5 Minuten sind (wahrscheinlich fehlgeschlagen).
        """
        now = time.time()
        timeout = 300  # 5 Minuten
        stale = [
            to_id
            for to_id, (_, _, timestamp, _, _) in self._pending_raids.items()
            if now - timestamp > timeout
        ]
        for to_id in stale:
            from_login, _, timestamp, _, _ = self._pending_raids.pop(to_id)
            age = now - timestamp
            log.warning(
                "Pending raid timed out after %.0fs: %s -> (ID: %s). EventSub event never arrived.",
                age,
                from_login,
                to_id,
            )

    async def _register_pending_raid(
        self,
        from_broadcaster_login: str,
        to_broadcaster_id: str,
        to_broadcaster_login: str,
        target_stream_data: dict | None = None,
        is_partner_raid: bool = False,
        viewer_count: int = 0,
    ):
        """
        Registriert einen Raid, der auf EventSub Bestätigung wartet.

        Wird aufgerufen nach erfolgreichem API-Call, bevor der Raid tatsächlich beim Ziel ankommt.
        Erstellt dynamisch eine channel.raid EventSub subscription für das Ziel.

        Args:
            from_broadcaster_login: Login des Raiding-Streamers
            to_broadcaster_id: User-ID des Raid-Ziels
            to_broadcaster_login: Login des Raid-Ziels
            target_stream_data: Stream-Daten des Ziels (optional)
            is_partner_raid: True wenn es ein Partner-Raid ist (für Partner-Message)
            viewer_count: Viewer-Count beim Raid-Start (für Partner-Message)
        """
        self._pending_raids[to_broadcaster_id] = (
            from_broadcaster_login,
            target_stream_data,
            time.time(),
            is_partner_raid,
            viewer_count,
        )
        log.info(
            "Pending raid registered: %s -> %s (ID: %s). Creating EventSub subscription...",
            from_broadcaster_login,
            to_broadcaster_login,
            to_broadcaster_id,
        )

        # Dynamische EventSub subscription erstellen
        if self._cog and hasattr(self._cog, "subscribe_raid_target_dynamic"):
            try:
                success = await self._cog.subscribe_raid_target_dynamic(
                    to_broadcaster_id, to_broadcaster_login
                )
                if success:
                    log.info(
                        "EventSub channel.raid subscription created for %s",
                        to_broadcaster_login,
                    )
                else:
                    log.warning(
                        "Failed to create EventSub subscription for %s - raid message may not be sent",
                        to_broadcaster_login,
                    )
            except Exception:
                log.exception(
                    "Error creating dynamic EventSub subscription for %s",
                    to_broadcaster_login,
                )
        else:
            log.warning(
                "Cog reference not set - cannot create dynamic EventSub subscription for %s",
                to_broadcaster_login,
            )

    async def on_raid_arrival(
        self,
        to_broadcaster_id: str,
        to_broadcaster_login: str,
        from_broadcaster_login: str,
        viewer_count: int,
        from_broadcaster_id: str | None = None,
    ):
        """
        Wird aufgerufen, wenn ein channel.raid EventSub Event eintrifft.

        Sendet entweder:
        - Partner-Message (bei Partner-Raids)
        - Recruitment-Message (bei Non-Partner-Raids)
        """
        pending = self._pending_raids.pop(to_broadcaster_id, None)
        if not pending:
            from_broadcaster_key = str(from_broadcaster_id or "").strip()
            if not from_broadcaster_key:
                from_broadcaster_key = (
                    self._resolve_streamer_id_by_login(from_broadcaster_login) or ""
                )
            if from_broadcaster_key:
                self.mark_manual_raid_started(from_broadcaster_key, ttl_seconds=180.0)
                log.info(
                    "External/manual raid detected via EventSub: %s -> %s. "
                    "Suppressing next offline auto-raid for broadcaster_id=%s (ttl=180s/3min)",
                    from_broadcaster_login,
                    to_broadcaster_login,
                    from_broadcaster_key,
                )
            log.debug(
                "Raid arrival ignored (not pending): %s -> %s",
                from_broadcaster_login,
                to_broadcaster_login,
            )
            return

        (
            expected_from,
            target_stream_data,
            _,
            is_partner_raid,
            registered_viewer_count,
        ) = pending

        # Verify it's the same raid we started
        if expected_from.lower() != from_broadcaster_login.lower():
            log.warning(
                "Raid arrival mismatch: expected from %s, got from %s",
                expected_from,
                from_broadcaster_login,
            )
            return

        log.info(
            "✅ Raid arrival confirmed: %s -> %s (%d viewers, partner_raid=%s)",
            from_broadcaster_login,
            to_broadcaster_login,
            viewer_count,
            is_partner_raid,
        )

        # silent_raid Check: Streamer kann Raid-Nachrichten im Chat unterdrücken
        silent_raid = False
        try:
            with get_conn() as conn:
                _sr_row = conn.execute(
                    "SELECT silent_raid FROM twitch_streamers WHERE LOWER(twitch_login) = ?",
                    (to_broadcaster_login.lower(),),
                ).fetchone()
                silent_raid = bool(int((_sr_row[0] if _sr_row else 0) or 0))
        except Exception:
            log.debug(
                "Raid arrival: silent_raid lookup failed for %s",
                to_broadcaster_login,
                exc_info=True,
            )

        if silent_raid:
            log.info(
                "Raid message suppressed (silent_raid): %s -> %s",
                from_broadcaster_login,
                to_broadcaster_login,
            )
            return

        # Partner-Raid: Sende Partner-Message
        if is_partner_raid:
            await self._send_partner_raid_message(
                from_broadcaster_login=from_broadcaster_login,
                to_broadcaster_login=to_broadcaster_login,
                to_broadcaster_id=to_broadcaster_id,
                viewer_count=viewer_count,
            )
        # Non-Partner-Raid: Sende Recruitment-Message
        else:
            await self._send_recruitment_message_now(
                from_broadcaster_login=from_broadcaster_login,
                to_broadcaster_login=to_broadcaster_login,
                target_stream_data=target_stream_data,
            )

    async def _send_partner_raid_message(
        self,
        from_broadcaster_login: str,
        to_broadcaster_login: str,
        to_broadcaster_id: str,
        viewer_count: int,
    ):
        """
        Sendet eine Bestätigungs-Nachricht im Chat des geraideten Partners.

        Diese Nachricht zeigt dem Partner-Streamer, dass der Raid durch
        das Deadlock Streamer-Netzwerk kam, um den Mehrwert zu verdeutlichen.

        Wird aufgerufen NACH dem EventSub channel.raid Event, d.h. der Raid
        ist bereits beim Ziel angekommen.
        """
        if not self.chat_bot:
            log.debug("Chat bot not available for partner raid message")
            return

        try:
            # Erfolgreiche Netzwerk-Raids für dieses Ziel zählen (inkl. aktuellem Raid)
            received_raid_count = self._get_received_network_raid_count(to_broadcaster_id)
            if received_raid_count <= 0:
                received_raid_count = 1

            viewer_word = "Viewer" if viewer_count == 1 else "Viewern"

            # 1. Channel beitreten (falls noch nicht joined)
            await self.chat_bot.join(to_broadcaster_login, channel_id=to_broadcaster_id)

            # 2. Kurze Verzögerung, damit der Bot bereit ist und der Raid-Alert durch ist
            await asyncio.sleep(5.0)

            # 3. Nachricht vorbereiten
            message = (
                f"Hey @{to_broadcaster_login}! 🎮 "
                f"@{from_broadcaster_login} hat dich gerade mit {viewer_count} {viewer_word} geraidet. "
                f"Das ist dein Raid Nr. {received_raid_count} aus dem Deadlock Streamer-Netzwerk. ❤️"
            )

            # 4. Nachricht senden
            if hasattr(self.chat_bot, "_send_chat_message"):

                class MockChannel:
                    def __init__(self, login, uid):
                        self.name = login
                        self.id = uid

                success = await self.chat_bot._send_chat_message(
                    MockChannel(to_broadcaster_login, to_broadcaster_id),
                    message,
                    source="partner_raid",
                )

                if success:
                    log.info(
                        "✅ Sent partner raid message to %s (raided by %s with %d viewers, network_raid_no=%d)",
                        to_broadcaster_login,
                        from_broadcaster_login,
                        viewer_count,
                        received_raid_count,
                    )
                else:
                    log.warning(
                        "Failed to send partner raid message to %s",
                        to_broadcaster_login,
                    )
            else:
                log.debug(
                    "Chat bot does not have _send_chat_message method, skipping partner raid message to %s",
                    to_broadcaster_login,
                )
        except Exception:
            log.exception(
                "Failed to send partner raid message to %s (raided by %s)",
                to_broadcaster_login,
                from_broadcaster_login,
            )

    def _get_received_network_raid_count(self, to_broadcaster_id: str) -> int:
        """
        Anzahl erfolgreicher, vom Raid-Bot geloggter Raids auf dieses Ziel.

        Die History enthält nur Raids, die von unseren Streamern über den Bot
        ausgeführt wurden; damit entspricht der Wert den erhaltenen Netzwerk-Raids.
        """
        target_id = str(to_broadcaster_id or "").strip()
        if not target_id:
            return 0

        try:
            with get_conn() as conn:
                row = conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM twitch_raid_history
                    WHERE to_broadcaster_id = ?
                      AND success = 1
                    """,
                    (target_id,),
                ).fetchone()
            return int(row[0]) if row and row[0] is not None else 0
        except Exception:
            log.debug(
                "Could not count received network raids for %s",
                target_id,
                exc_info=True,
            )
            return 0

    @staticmethod
    def _parse_nonnegative_int(value: object) -> int | None:
        try:
            if value is None:
                return None
            parsed = int(value)
            return parsed if parsed >= 0 else None
        except (TypeError, ValueError):
            return None

    async def _resolve_recruitment_followers_total(
        self,
        *,
        login: str,
        target_id: str | None,
        target_stream_data: dict | None,
    ) -> int | None:
        cached_total = self._parse_nonnegative_int(
            (target_stream_data or {}).get("followers_total")
        )
        if cached_total is not None:
            return cached_total

        resolved_target_id = str(target_id or "").strip()
        if not resolved_target_id or not self.session:
            return None

        try:
            from ..api.twitch_api import TwitchAPI
        except Exception:
            return None

        user_token: str | None = None
        try:
            user_token = await self.auth_manager.get_valid_token(resolved_target_id, self.session)
        except Exception:
            user_token = None

        try:
            api = TwitchAPI(
                self.auth_manager.client_id,
                self.auth_manager.client_secret,
                session=self.session,
            )
            followers_total = await api.get_followers_total(
                resolved_target_id, user_token=user_token
            )
        except Exception:
            log.debug("Follower-Check fehlgeschlagen fuer %s", login, exc_info=True)
            return None

        parsed_total = self._parse_nonnegative_int(followers_total)
        if parsed_total is not None and isinstance(target_stream_data, dict):
            target_stream_data["followers_total"] = parsed_total
        return parsed_total

    async def _send_recruitment_message_now(
        self,
        from_broadcaster_login: str,
        to_broadcaster_login: str,
        target_stream_data: dict | None = None,
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
                users = await self.chat_bot.fetch_users(logins=[to_broadcaster_login])
                if users:
                    target_id = str(users[0].id)

            if not target_id:
                log.warning(
                    "Could not resolve user ID for recruitment message to %s",
                    to_broadcaster_login,
                )
                return

            await self.chat_bot.join(to_broadcaster_login, channel_id=target_id)
        except Exception:
            log.debug("Konnte Channel %s nicht vorab beitreten", to_broadcaster_login)

        # Follow-Status prüfen (Auto-Follow per API ist bei Twitch nicht mehr möglich).
        if target_id and hasattr(self.chat_bot, "follow_channel"):
            await self.chat_bot.follow_channel(target_id)

        # 2. 15 Sekunden warten, damit der Streamer den Raid-Alert verarbeiten kann
        log.info(
            "Warte 15s vor Senden der Recruitment-Message an %s...",
            to_broadcaster_login,
        )
        await asyncio.sleep(15.0)

        try:
            # 2. Anti-Spam Check: Haben wir diesen Streamer schon "kürzlich" geraidet?
            # Wir prüfen, ob es mehr als 1 erfolgreichen Raid in den letzten 24 Stunden gab.
            with get_conn() as conn:
                raid_check = conn.execute(
                    """
                    SELECT COUNT(*) FROM twitch_raid_history
                    WHERE to_broadcaster_id = ?
                      AND success = 1
                      AND executed_at > datetime('now', '-1 day')
                    """,
                    (target_id,),
                ).fetchone()
                recent_raids = raid_check[0] if raid_check else 0

            if recent_raids > 1:
                log.info(
                    "Skipping recruitment message to %s (Anti-Spam: %d raids in last 24 hours)",
                    to_broadcaster_login,
                    recent_raids,
                )
                return

            # 3. Bestimme die Anzahl der bisherigen Netzwerk-Raids für diesen Streamer
            total_raids = self._get_received_network_raid_count(target_id)

            # 4. Nachricht vorbereiten (mit Stats Teaser)
            followers_total = await self._resolve_recruitment_followers_total(
                login=to_broadcaster_login,
                target_id=target_id,
                target_stream_data=target_stream_data,
            )
            use_direct_invite = (
                followers_total is not None
                and followers_total <= RECRUIT_DIRECT_INVITE_MAX_FOLLOWERS
            )
            discord_invite = (
                RECRUIT_DISCORD_INVITE_DIRECT if use_direct_invite else RECRUIT_DISCORD_INVITE
            )

            stats_teaser = ""
            try:
                with get_conn() as conn:
                    stats = conn.execute(
                        """
                        SELECT
                            ROUND(AVG(viewer_count)) as avg_viewers,
                            MAX(viewer_count) as peak_viewers
                        FROM twitch_stats_category
                        WHERE streamer = ?
                          AND viewer_count > 0
                        """,
                        (to_broadcaster_login.lower(),),
                    ).fetchone()

                if stats and stats[0]:
                    avg_viewers = int(stats[0])
                    peak_viewers = int(stats[1]) if stats[1] else 0
                    if peak_viewers > 0:
                        stats_teaser = f"Übrigens: Du hattest im Schnitt {avg_viewers} Viewer bei Deadlock, dein Peak war {peak_viewers}. "
            except Exception:
                log.debug("Could not fetch stats for %s", to_broadcaster_login, exc_info=True)

            # Nachrichtenauswahl basierend auf Raid-Anzahl
            if total_raids <= 1:
                message = (
                    f"Hey @{to_broadcaster_login}! Ich bin der Bot der deutschen Deadlock Community . "
                    f"Ich manage hier die Raids bei Twitch Deadlock.. "
                    f"Du wurdest gerade von @{from_broadcaster_login} geraidet, einem unserer Partner! <3 "
                    f"Falls du bock hast kannst auch Teil der Community werden und Support erhalten – "
                    f"schau gerne mal auf unserem Discord vorbei: {discord_invite} "
                    f"Dir noch einen wunderschönen Stream <3"
                )
            elif total_raids == 2:
                message = (
                    f"Hey @{to_broadcaster_login}! Na, schon der 2. Raid von uns! ❤️ "
                    f"@{from_broadcaster_login} bringt dir gerade Verstärkung aus dem Netzwerk vorbei. "
                    f"{stats_teaser}"
                    f"Unser Partner-Netzwerk wächst ständig und wir würden freuen uns über ein neues Gesicht freuen :). "
                    f"Schau mal rein: {discord_invite} 🎮"
                )
            elif total_raids == 3:
                message = (
                    f"Hey @{to_broadcaster_login}! Aller guten Dinge sind 3! Das ist schon der 3. Raid aus der Community für dich. ❤️ "
                    f"Hast du schon über eine Partnerschaft nachgedacht? Gemeinsam wachsen wir viel schneller! "
                    f"Join uns: {discord_invite} 🎮"
                )
            else:  # 4. Raid und mehr
                message = (
                    f"Hey @{to_broadcaster_login}! So langsam wird es Zeit für eine Partnerschaft, oder? 😉 "
                    f"Das ist schon der {total_raids}. Raid von uns (diesmal von @{from_broadcaster_login})! "
                    f"{stats_teaser}"
                    f"Komm in unser Netzwerk und profitiere von gegenseitigen Raids, Zugang zu der größten deutschen Deadlock Community und viel mehr. Schau doch gerne mal vorbei: {discord_invite} 🎮"
                )

            # 5. Sende Nachricht via Bot
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
                        message,
                        source="recruitment",
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

    def _get_recent_raid_targets(self, from_broadcaster_id: str, days: int) -> set[str]:
        if not from_broadcaster_id or days <= 0:
            return set()
        cutoff = f"-{int(days)} days"
        try:
            with get_conn() as conn:
                rows = conn.execute(
                    """
                    SELECT DISTINCT to_broadcaster_id
                    FROM twitch_raid_history
                    WHERE from_broadcaster_id = ?
                      AND success = 1
                      AND executed_at >= datetime('now', ?)
                    """,
                    (from_broadcaster_id, cutoff),
                ).fetchall()
            return {str(row[0]) for row in rows if row and row[0]}
        except Exception:
            log.debug(
                "Failed to load recent raid targets for %s",
                from_broadcaster_id,
                exc_info=True,
            )
            return set()

    async def _attach_followers_totals(self, candidates: list[dict]) -> None:
        if not candidates or not self.session:
            return
        try:
            from ..api.twitch_api import TwitchAPI
        except Exception:
            return

        api = TwitchAPI(
            self.auth_manager.client_id,
            self.auth_manager.client_secret,
            session=self.session,
        )

        for candidate in candidates:
            if candidate.get("followers_total") is not None:
                continue
            user_id = str(candidate.get("user_id") or "").strip()
            if not user_id:
                continue
            try:
                token = await self.auth_manager.get_valid_token(user_id, self.session)
            except Exception:
                token = None
            if not token:
                continue
            try:
                followers = await api.get_followers_total(user_id, user_token=token)
            except Exception:
                continue
            if followers is not None:
                candidate["followers_total"] = int(followers)

    async def _select_fairest_candidate(
        self, candidates: list[dict], from_broadcaster_id: str
    ) -> dict | None:
        """
        Wählt den Raid-Kandidaten mit den wenigsten Viewern.
        Bei Gleichstand: Wenigste Follower (wenn verfügbar), danach kürzeste Stream-Zeit.
        Ziele der letzten Tage werden vermieden, sofern Alternativen existieren.
        """
        if not candidates:
            return None

        recent_targets = self._get_recent_raid_targets(
            from_broadcaster_id, RAID_TARGET_COOLDOWN_DAYS
        )
        if recent_targets:
            filtered = [c for c in candidates if str(c.get("user_id") or "") not in recent_targets]
        else:
            filtered = []

        pool = filtered or candidates

        await self._attach_followers_totals(pool)

        def _safe_int(value: object, default: int) -> int:
            try:
                if value is None:
                    return default
                return int(value)
            except (TypeError, ValueError):
                return default

        def _sort_key(candidate: dict) -> tuple[int, int, str]:
            viewers = _safe_int(candidate.get("viewer_count"), 10**9)
            followers = _safe_int(candidate.get("followers_total"), 10**9)
            started_at = candidate.get("started_at") or "9999-99-99"
            return (viewers, followers, started_at)

        pool.sort(key=_sort_key)

        selected = pool[0]
        log.info(
            "Raid target selection (min viewers): %s (viewers=%s, followers=%s, recent_filtered=%d) from %d candidates",
            selected.get("user_login"),
            selected.get("viewer_count"),
            selected.get("followers_total"),
            max(0, len(candidates) - len(pool)),
            len(candidates),
        )

        return selected

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

    def _is_retryable_raid_error(self, error: str | None) -> bool:
        """Return True for raid target errors where we should try another target."""
        if not error:
            return False
        msg = error.lower()
        retryable_markers = (
            "cannot be raided",
            "does not allow you to raid",
            "do not allow you to raid",
            "not allow you to raid",
            "settings do not allow you to raid",
            "not accepting raids",
            "does not allow raids",
            "raids are disabled",
        )
        return any(marker in msg for marker in retryable_markers)

    async def handle_streamer_offline(
        self,
        broadcaster_id: str,
        broadcaster_login: str,
        viewer_count: int,
        stream_duration_sec: int,
        online_partners: list[dict],
        api=None,
        category_id: str | None = None,
    ) -> str | None:
        """
        Wird aufgerufen, wenn ein Streamer offline geht.
        Versucht automatisch zu raiden, falls möglich.

        Features:
        - Auto-Retry bei Fehlern (z.B. Ziel hat Raids deaktiviert)
        - Blacklist-Management für nicht raidbare Kanäle
        """
        # Prüfen, ob Auto-Raid durch manuellen Raid unterdrückt ist
        if self.is_offline_auto_raid_suppressed(broadcaster_id):
            log.info(
                "Auto-raid suppressed for %s (manual raid detected recently)",
                broadcaster_login,
            )
            return None

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
                and bool(s.get("raid_enabled", True))
                and not self._is_blacklisted(s.get("user_id"), s.get("user_login"))
            ]

            if partner_candidates:
                # Partner vorhanden -> Auswahl nach niedrigsten Viewern
                is_partner_raid = True
                target = await self._select_fairest_candidate(partner_candidates, broadcaster_id)
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
                    target = await self._select_fairest_candidate(
                        fallback_candidates, broadcaster_id
                    )
                    candidates_count = len(fallback_candidates)

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
                reason="auto_raid_on_offline",
            )

            if success:
                # Pending Raid registrieren (Nachricht wird erst nach EventSub gesendet)
                # Funktioniert für Partner-Raids UND Non-Partner-Raids
                await self._register_pending_raid(
                    from_broadcaster_login=broadcaster_login,
                    to_broadcaster_id=target_id,
                    to_broadcaster_login=target_login,
                    target_stream_data=target,
                    is_partner_raid=is_partner_raid,
                    viewer_count=viewer_count,
                )
                return target_login

            # Fehler-Behandlung
            exclude_ids.add(target_id)  # Diesen Kandidaten nicht nochmal versuchen

            # Check auf "Cannot be raided"/Raid-Settings (HTTP 400)
            if self._is_retryable_raid_error(error):
                if is_partner_raid:
                    log.warning(
                        "Raid failed: Partner target %s does not allow raids. Skipping without blacklist.",
                        target_login,
                    )
                else:
                    log.warning(
                        "Raid failed: Target %s does not allow raids. Blacklisting and retrying.",
                        target_login,
                    )
                    self._add_to_blacklist(target_id, target_login, error)
                continue  # Nächster Versuch

            # Bei anderen Fehlern (z.B. API Down, Auth Error) brechen wir ab
            log.error("Raid failed with non-retriable error: %s. Aborting.", error)
            return None

        return None
