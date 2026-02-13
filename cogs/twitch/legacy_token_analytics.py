# cogs/twitch/legacy_token_analytics.py
"""
LegacyTokenAnalyticsMixin – Übergangslösung für Streamer die noch re-authen müssen.

Streamer mit needs_reauth=1 haben noch die neuen Scopes (bits:read, channel:read:hype_train,
channel:read:subscriptions, channel:read:ads) nicht autorisiert. Für diese werden die
legacy_access_token Felder für Analytics-EventSubs verwendet, bis sie re-authen.
"""
from __future__ import annotations

from typing import Optional

from .logger import log
from . import storage


class LegacyTokenAnalyticsMixin:
    """
    Stellt Hilfsmethoden bereit, die bei Streamern mit needs_reauth=1 den
    legacy_access_token statt des (ungültigen/unzureichenden) access_token verwenden.
    """

    async def _resolve_broadcaster_token_with_legacy(
        self, twitch_user_id: str
    ) -> Optional[str]:
        """
        Gibt den Token zurück, der für broadcaster-spezifische EventSub-Subscriptions
        genutzt werden soll:
        - needs_reauth=0 → neuer access_token (volle Scopes)
        - needs_reauth=1 → legacy_access_token (alte Scopes für Analytics-Übergang)
        """
        try:
            with storage.get_conn() as conn:
                row = conn.execute(
                    "SELECT access_token, legacy_access_token, needs_reauth "
                    "FROM twitch_raid_auth WHERE twitch_user_id=?",
                    (twitch_user_id,),
                ).fetchone()
            if not row:
                return None
            needs_reauth = row["needs_reauth"] if hasattr(row, "keys") else row[2]
            if needs_reauth == 0:
                token = row["access_token"] if hasattr(row, "keys") else row[0]
            else:
                token = row["legacy_access_token"] if hasattr(row, "keys") else row[1]
                if token:
                    log.debug(
                        "LegacyToken: Nutze legacy_access_token (needs_reauth=1)",
                    )
            if not token:
                return None
            token = str(token).strip()
            if token.lower().startswith("oauth:"):
                token = token[6:]
            return token or None
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
