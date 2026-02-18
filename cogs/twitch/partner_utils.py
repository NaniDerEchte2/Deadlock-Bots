"""
Zentrale Utilities für Partner-Status-Checks.

Klare Trennung zwischen:
- PARTNER: Vollständige Features (IRC, Raids, Analytics, Chat Bot)
- MONITORED-ONLY: Nur Stats-Tracking (keine Chat-Features)
"""
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set

from .storage import get_conn

log = logging.getLogger("TwitchStreams.PartnerUtils")


def _parse_db_datetime(value: Optional[str]) -> Optional[datetime]:
    """Parse ISO datetime from DB."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def is_partner(row: Dict[str, any], now_utc: Optional[datetime] = None) -> bool:
    """
    Prüft ob ein Streamer ein verifizierter Partner ist.

    Partner-Kriterien:
    - manual_verified_permanent = 1 ODER
    - manual_verified_until ist gesetzt und nicht abgelaufen ODER
    - manual_verified_at ist gesetzt (für Legacy-Kompatibilität)

    UND:
    - manual_partner_opt_out != 1
    - is_monitored_only != 1
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    # Opt-out prüfen
    if bool(row.get("manual_partner_opt_out")):
        return False

    # Monitored-Only ausschließen
    if bool(row.get("is_monitored_only")):
        return False

    # Permanente Verifizierung
    if bool(row.get("manual_verified_permanent")):
        return True

    # Temporäre Verifizierung (mit Ablaufdatum)
    until_raw = row.get("manual_verified_until")
    if until_raw:
        until_dt = _parse_db_datetime(str(until_raw))
        if until_dt and until_dt >= now_utc:
            return True

    # Legacy: manual_verified_at vorhanden (alte Partner ohne explizites Ablaufdatum)
    if row.get("manual_verified_at"):
        return True

    return False


def get_all_partners(include_archived: bool = False) -> List[Dict]:
    """
    Gibt alle verifizierten Partner zurück.

    Args:
        include_archived: Wenn False, werden archivierte Partner ausgeschlossen

    Returns:
        Liste von Streamer-Dicts mit allen relevanten Feldern
    """
    with get_conn() as conn:
        query = """
            SELECT twitch_login,
                   twitch_user_id,
                   manual_verified_permanent,
                   manual_verified_until,
                   manual_verified_at,
                   manual_partner_opt_out,
                   is_monitored_only,
                   archived_at,
                   is_on_discord,
                   discord_user_id,
                   discord_display_name
              FROM twitch_streamers_partner_state
             WHERE is_partner = 1
        """

        if not include_archived:
            query += " AND is_partner_active = 1"

        query += " ORDER BY twitch_login"

        rows = conn.execute(query).fetchall()
        return [dict(row) for row in rows]


def get_live_partners() -> List[Dict]:
    """
    Gibt alle aktuell live Partner zurück.

    Returns:
        Liste von Dicts mit Streamer-Infos und Live-Status
    """
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT s.twitch_login,
                   s.twitch_user_id,
                   s.manual_verified_permanent,
                   s.manual_verified_until,
                   s.manual_verified_at,
                   s.manual_partner_opt_out,
                   s.is_monitored_only,
                   s.archived_at,
                   l.is_live,
                   l.active_session_id,
                   l.stream_title,
                   l.game_name,
                   l.viewer_count
              FROM twitch_streamers_partner_state s
              JOIN twitch_live_state l ON l.twitch_user_id = s.twitch_user_id
             WHERE l.is_live = 1
               AND s.is_partner_active = 1
             ORDER BY s.twitch_login
        """).fetchall()

        return [dict(row) for row in rows]


def get_monitored_only() -> Set[str]:
    """
    Gibt alle Monitored-Only Streamer zurück (nur Logins).

    Returns:
        Set von lowercase Logins
    """
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT twitch_login
              FROM twitch_streamers
             WHERE COALESCE(is_monitored_only, 0) = 1
        """).fetchall()

        return {str(row[0]).lower() for row in rows if row[0]}


def is_partner_channel_for_chat_tracking(login: str) -> bool:
    """
    Prüft ob ein Channel ein Partner ist und Chat-Tracking aktiviert haben sollte.

    Diese Funktion wird von IRC/Chat-Bots verwendet um zu entscheiden,
    ob ein Channel gejoined werden soll.

    Args:
        login: Twitch Login (case-insensitive)

    Returns:
        True wenn Partner, False wenn Monitored-Only oder nicht vorhanden
    """
    login_lower = str(login).lower().lstrip("#")

    with get_conn() as conn:
        row = conn.execute("""
            SELECT is_partner_active
              FROM twitch_streamers_partner_state
             WHERE LOWER(twitch_login) = ?
        """, (login_lower,)).fetchone()

        if not row:
            return False

        return bool(row["is_partner_active"] if hasattr(row, "keys") else row[0])


def get_partner_stats() -> Dict:
    """
    Gibt Statistiken über Partner und Monitored-Only Streamer zurück.

    Returns:
        Dict mit Stats
    """
    with get_conn() as conn:
        # Partner Count
        partner_count = conn.execute("""
            SELECT COUNT(*)
              FROM twitch_streamers_partner_state
             WHERE is_partner_active = 1
        """).fetchone()[0]

        # Monitored-Only Count
        monitored_count = conn.execute("""
            SELECT COUNT(*)
              FROM twitch_streamers
             WHERE COALESCE(is_monitored_only, 0) = 1
        """).fetchone()[0]

        # Live Partner Count
        live_partners = conn.execute("""
            SELECT COUNT(*)
              FROM twitch_streamers_partner_state s
              JOIN twitch_live_state l ON l.twitch_user_id = s.twitch_user_id
             WHERE l.is_live = 1
               AND s.is_partner_active = 1
        """).fetchone()[0]

        # Archived Partner Count
        archived_count = conn.execute("""
            SELECT COUNT(*)
              FROM twitch_streamers_partner_state
             WHERE is_partner = 1
               AND archived_at IS NOT NULL
        """).fetchone()[0]

        return {
            "total_partners": partner_count,
            "live_partners": live_partners,
            "archived_partners": archived_count,
            "monitored_only": monitored_count,
        }
