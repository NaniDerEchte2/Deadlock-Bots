# cogs/twitch/legacy_token_analytics.py
"""
LegacyTokenAnalyticsMixin – Übergangslösung für Streamer die noch re-authen müssen.

Streamer mit needs_reauth=1 haben noch die neuen Scopes (bits:read, channel:read:hype_train,
channel:read:subscriptions, channel:read:ads) nicht autorisiert. Für diese werden
legacy_access_token Felder für Analytics-EventSubs verwendet, bis sie re-authen.
"""
from __future__ import annotations

from typing import Optional

from ..constants import log
from .. import storage


def _mask_log_identifier(
    value: object, *, visible_prefix: int = 3, visible_suffix: int = 2
) -> str:
    text = str(value or "").strip()
    if not text:
        return "<empty>"
    if len(text) <= visible_prefix + visible_suffix:
        return "***"
    return f"{text[:visible_prefix]}...{text[-visible_suffix:]}"


class LegacyTokenAnalyticsMixin:
    """
    Stellt Hilfsmethoden bereit, die bei Streamern mit needs_reauth=1 den
    Übergang in den Re-Auth-Flow steuern.

    Hinweis: Klartext-Token-Fallbacks sind deaktiviert (ENC-only Read).
    """

    def _clear_legacy_snapshot_for_user(
        self,
        twitch_user_id: str,
        *,
        only_when_reauth_pending: bool = True,
        reason: str = "",
    ) -> bool:
        """
        Entfernt legacy_* Snapshot-Felder für genau einen Streamer.

        Standardmäßig nur, wenn needs_reauth=1 ist, damit fully-authed User
        nicht versehentlich betroffen sind.
        """
        user_id = str(twitch_user_id or "").strip()
        if not user_id:
            return False
        try:
            if only_when_reauth_pending:
                query = """
                    UPDATE twitch_raid_auth
                       SET legacy_access_token  = NULL,
                           legacy_refresh_token = NULL,
                           legacy_scopes        = NULL,
                           legacy_saved_at      = NULL
                     WHERE twitch_user_id = ?
                       AND needs_reauth = 1
                       AND (
                           legacy_access_token IS NOT NULL
                        OR legacy_refresh_token IS NOT NULL
                        OR legacy_scopes IS NOT NULL
                        OR legacy_saved_at IS NOT NULL
                       )
                """
            else:
                query = """
                    UPDATE twitch_raid_auth
                       SET legacy_access_token  = NULL,
                           legacy_refresh_token = NULL,
                           legacy_scopes        = NULL,
                           legacy_saved_at      = NULL
                     WHERE twitch_user_id = ?
                       AND (
                           legacy_access_token IS NOT NULL
                        OR legacy_refresh_token IS NOT NULL
                        OR legacy_scopes IS NOT NULL
                        OR legacy_saved_at IS NOT NULL
                       )
                """

            with storage.get_conn() as conn:
                conn.execute(query, (user_id,))
                changed = int(conn.execute("SELECT changes()").fetchone()[0] or 0)
            if changed:
                suffix = f" ({reason})" if reason else ""
                log.info(
                    "LegacyAuth: legacy_* snapshot removed for user_id=%s%s",
                    _mask_log_identifier(user_id),
                    suffix,
                )
            return bool(changed)
        except Exception:
            log.debug(
                "LegacyAuth: could not remove legacy_* snapshot for user_id=%s",
                _mask_log_identifier(user_id),
                exc_info=True,
            )
            return False

    async def _resolve_broadcaster_token_with_legacy(
        self, twitch_user_id: str
    ) -> Optional[str]:
        """
        Gibt den Token zurück, der für broadcaster-spezifische EventSub-Subscriptions
        genutzt werden soll:
        - needs_reauth=0 → neuer access_token (volle Scopes), bevorzugt via get_valid_token (mit Refresh)
        - needs_reauth=1 → kein Token (Klartext-Legacy wird nicht mehr geladen)
        """
        def _sanitize_token(raw_value: object) -> Optional[str]:
            token = str(raw_value or "").strip()
            if not token:
                return None
            if token.lower().startswith("oauth:"):
                token = token[6:]
            return token or None

        async def _resolve_current_access_token() -> Optional[str]:
            raid_bot = getattr(self, "_raid_bot", None)
            auth_manager = getattr(raid_bot, "auth_manager", None) if raid_bot else None
            session = getattr(raid_bot, "session", None) if raid_bot else None
            if auth_manager and session and not getattr(session, "closed", False):
                try:
                    refreshed_token = await auth_manager.get_valid_token(str(twitch_user_id), session)
                    clean = _sanitize_token(refreshed_token)
                    if clean:
                        return clean
                except Exception:
                    log.debug(
                        "LegacyToken: get_valid_token fehlgeschlagen (ENC-only, kein DB-Klartext-Fallback)",
                        exc_info=True,
                    )
            return None

        try:
            with storage.get_conn() as conn:
                row = conn.execute(
                    "SELECT needs_reauth, "
                    "CASE WHEN legacy_access_token IS NOT NULL AND TRIM(legacy_access_token) <> '' THEN 1 ELSE 0 END AS legacy_present "
                    "FROM twitch_raid_auth WHERE twitch_user_id=?",
                    (twitch_user_id,),
                ).fetchone()
            if not row:
                return None
            needs_reauth = row["needs_reauth"] if hasattr(row, "keys") else row[0]
            legacy_present = bool(row["legacy_present"] if hasattr(row, "keys") else row[1])
            if needs_reauth == 0:
                if legacy_present:
                    self._clear_legacy_snapshot_for_user(
                        str(twitch_user_id),
                        only_when_reauth_pending=False,
                        reason="fully_authed_cleanup",
                    )
                return await _resolve_current_access_token()

            if legacy_present:
                log.info(
                    "LegacyAuth: needs_reauth=1 for user_id=%s - legacy plaintext fallback disabled; re-auth required",
                    _mask_log_identifier(twitch_user_id),
                )
                return None

            log.info(
                "LegacyAuth: needs_reauth=1 but no legacy access grant exists (user_id=%s) - "
                "skip broadcaster EventSubs until re-auth",
                _mask_log_identifier(twitch_user_id),
            )
            return None
        except Exception:
            log.debug(
                "LegacyToken: Konnte Token nicht laden", exc_info=True
            )
            return None

    async def _is_fully_authed(self, twitch_user_id: str) -> bool:
        """
        True = neuer Token vorhanden (needs_reauth=0) → voller Bot-Betrieb.
        False = nur Legacy-Token oder kein Token → eingeschränkter Betrieb.
        """
        try:
            with storage.get_conn() as conn:
                row = conn.execute(
                    "SELECT needs_reauth FROM twitch_raid_auth WHERE twitch_user_id=?",
                    (twitch_user_id,),
                ).fetchone()
            if not row:
                return False
            needs_reauth = row["needs_reauth"] if hasattr(row, "keys") else row[0]
            return needs_reauth == 0
        except Exception:
            log.debug(
                "LegacyToken: _is_fully_authed-Check fehlgeschlagen",
                exc_info=True,
            )
            return False

    async def _get_pending_reauth_count(self) -> int:
        """Anzahl Streamer die noch re-authen müssen (needs_reauth=1)."""
        try:
            with storage.get_conn() as conn:
                return conn.execute(
                    "SELECT COUNT(*) FROM twitch_raid_auth WHERE needs_reauth=1"
                ).fetchone()[0]
        except Exception:
            log.debug("LegacyToken: Konnte pending reauth count nicht lesen", exc_info=True)
            return 0
