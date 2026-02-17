"""Administrative helpers for the Twitch cog."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional, Tuple

import discord

from .. import storage
from ..constants import log


class TwitchAdminMixin:
    """Helper methods used by the admin dashboard without Discord commands."""

    async def _cmd_set_channel(
        self, guild: discord.Guild, channel: Optional[discord.TextChannel] = None
    ) -> str:
        """Set the target channel for Twitch notifications."""
        channel = channel or getattr(guild, "system_channel", None)
        if channel is None:
            return "Kein gültiger Kanal angegeben."

        try:
            self._set_channel(guild.id, channel.id)
        except Exception:
            log.exception("Konnte Twitch-Channel speichern")
            return "Konnte Kanal nicht speichern."
        return f"Live-Posts gehen jetzt in {channel.mention}"

    async def _cmd_add_wrapper(self, login: str, require_discord_link: Optional[bool] = False) -> str:
        """Wrapper kept for backwards compatibility with removed Discord command."""
        try:
            return await self._cmd_add(login, bool(require_discord_link))
        except Exception:
            log.exception("twitch add fehlgeschlagen")
            return "Fehler beim Hinzufügen."

    async def _cmd_remove_wrapper(self, login: str) -> str:
        """Wrapper kept for backwards compatibility with removed Discord command."""
        try:
            return await self._cmd_remove(login)
        except Exception:
            log.exception("twitch remove fehlgeschlagen")
            return "Fehler beim Entfernen."

    async def _cmd_list_streamers(self) -> Tuple[str, Iterable[dict]]:
        """Return a formatted list of streamers and the raw rows."""
        try:
            with storage.get_conn() as c:
                rows = c.execute(
                    "SELECT twitch_login, manual_verified_permanent, manual_verified_until FROM twitch_streamers ORDER BY twitch_login"
                ).fetchall()
        except Exception:
            log.exception("Konnte Streamer-Liste aus DB lesen")
            return "Fehler beim Lesen der Streamer-Liste.", []

        if not rows:
            return "Keine Streamer gespeichert.", []

        def _fmt(row: dict) -> str:
            until = row.get("manual_verified_until")
            perm = bool(row.get("manual_verified_permanent"))
            tail = " (permanent verifiziert)" if perm else (f" (verifiziert bis {until})" if until else "")
            return f"- {row.get('twitch_login','?')}{tail}"

        try:
            formatted = [_fmt(dict(r)) for r in rows]
        except Exception:
            log.exception("Fehler beim Formatieren der Streamer-Liste")
            return "Fehler beim Anzeigen der Liste.", []
        return "\n".join(formatted)[:1900], [dict(r) for r in rows]

    async def _cmd_forcecheck(self) -> str:
        """Trigger a manual check of the Twitch state."""
        try:
            await self._tick()
        except Exception:
            log.exception("Forcecheck fehlgeschlagen")
            return "Fehler beim Forcecheck."
        return "Prüfung durchgeführt."

    async def _cmd_invites(self, guild: discord.Guild) -> Tuple[str, Iterable[str]]:
        """Return the active invite URLs for a guild."""
        try:
            await self._refresh_guild_invites(guild)
            codes = sorted(self._invite_codes.get(guild.id, set()))
        except Exception:
            log.exception("Konnte Einladungen nicht abrufen")
            return "Fehler beim Abrufen der Einladungen.", []

        if not codes:
            return "Keine aktiven Einladungen gefunden.", []

        urls = [f"https://discord.gg/{code}" for code in codes]
        return "Aktive Einladungen:", urls

    async def _cmd_refresh_invites(self, guild: discord.Guild) -> str:
        """Manually refresh invite codes from Discord API and cache in DB."""
        try:
            await self._refresh_guild_invites(guild)
            count = len(self._invite_codes.get(guild.id, set()))
            return f"✅ {count} Invite-Codes aktualisiert und in DB gespeichert."
        except Exception:
            log.exception("Invite-Refresh fehlgeschlagen")
            return "❌ Fehler beim Aktualisieren der Invite-Codes."

    async def _get_valid_invite_codes(self, guild_id: Optional[int] = None) -> set[str]:
        """Get valid invite codes from DB cache (falls back to API if needed)."""
        # Wenn keine Guild-ID angegeben, nehme die erste verfügbare
        if guild_id is None:
            guilds = getattr(self.bot, 'guilds', [])
            if not guilds:
                return set()
            guild_id = guilds[0].id
        
        # Zuerst: Versuche aus DB zu laden (schnell, kein API-Call)
        try:
            with storage.get_conn() as conn:
                rows = conn.execute(
                    "SELECT invite_code FROM discord_invite_codes WHERE guild_id = ?",
                    (guild_id,)
                ).fetchall()
                if rows:
                    codes = {row[0] for row in rows}
                    log.debug("Invite-Codes aus DB geladen für Guild %s: %s Codes", guild_id, len(codes))
                    # Cache auch im RAM für schnelleren Zugriff
                    self._invite_codes[guild_id] = codes
                    return codes
        except Exception:
            log.debug("Konnte Invite-Codes nicht aus DB laden für Guild %s", guild_id, exc_info=True)
        
        # Fallback: RAM-Cache
        cached = self._invite_codes.get(guild_id)
        if cached:
            log.debug("Invite-Codes aus RAM-Cache für Guild %s", guild_id)
            return cached
        
        # Letzter Fallback: Frisch von Discord API abrufen (nur wenn wirklich nötig)
        guild = self.bot.get_guild(guild_id)
        if not guild:
            log.warning("Guild %s nicht gefunden für Invite-Abruf", guild_id)
            return set()
        
        log.info("Invite-Codes nicht gecached - rufe frisch von Discord API ab für Guild %s", guild_id)
        try:
            await self._refresh_guild_invites(guild)
            return self._invite_codes.get(guild_id, set())
        except Exception:
            log.exception("Konnte Invite-Codes nicht abrufen für Guild %s", guild_id)
            return set()

    # -------------------------------------------------------
    # Admin-Commands: Add/Remove Helpers
    # -------------------------------------------------------
    @staticmethod
    def _parse_db_datetime(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    @classmethod
    def _is_partner_verified(cls, row: Dict[str, Any], now_utc: datetime) -> bool:
        try:
            if bool(row.get("manual_verified_permanent")):
                return True
        except Exception as exc:
            log.debug("Partner-Flag konnte nicht gelesen werden: %s", exc)

        until_raw = row.get("manual_verified_until")
        until_dt = cls._parse_db_datetime(str(until_raw)) if until_raw else None
        if until_dt and until_dt >= now_utc:
            return True
        return False

    async def _cmd_add(self, login: str, require_link: bool) -> str:
        if self.api is None:
            return "API nicht konfiguriert"

        normalized = self._normalize_login(login)
        if not normalized:
            return "Ungültiger Twitch-Login"

        users = await self.api.get_users([normalized])
        user = users.get(normalized)
        if not user:
            return "Unbekannter Twitch-Login"

        try:
            with storage.get_conn() as c:
                c.execute(
                    "INSERT OR IGNORE INTO twitch_streamers "
                    "(twitch_login, twitch_user_id, require_discord_link, next_link_check_at) "
                    "VALUES (?, ?, ?, datetime('now','+30 days'))",
                    (user["login"].lower(), user["id"], int(require_link)),
                )
                # WICHTIG: Auch bei UPDATE die user_id setzen (falls sie vorher NULL war)
                c.execute(
                    "UPDATE twitch_streamers "
                    "SET manual_verified_permanent=0, manual_verified_until=NULL, manual_verified_at=NULL, "
                    "    twitch_user_id=?, is_monitored_only=0 "
                    "WHERE twitch_login=?",
                    (user["id"], normalized),
                )
                # Historische Kategorie-Daten übernehmen, damit der Partner-Dashboard
                # nicht bei 0 startet. Kopiert alle Einträge aus twitch_stats_category,
                # die noch nicht in twitch_stats_tracked vorhanden sind.
                copied = c.execute(
                    """
                    INSERT INTO twitch_stats_tracked
                        (ts_utc, streamer, viewer_count, is_partner, game_name, stream_title, tags)
                    SELECT c.ts_utc, c.streamer, c.viewer_count, c.is_partner,
                           c.game_name, c.stream_title, c.tags
                    FROM twitch_stats_category c
                    WHERE LOWER(c.streamer) = ?
                      AND NOT EXISTS (
                          SELECT 1 FROM twitch_stats_tracked t
                          WHERE LOWER(t.streamer) = LOWER(c.streamer)
                            AND t.ts_utc = c.ts_utc
                      )
                    """,
                    (normalized,),
                ).rowcount
        except Exception:
            log.exception("DB-Fehler beim Hinzufügen von %s", normalized)
            return "Datenbankfehler beim Hinzufügen."

        suffix = f" ({copied} historische Datenpunkte übernommen)" if copied else ""
        return f"{user['display_name']} hinzugefügt{suffix}"

    async def _cmd_remove(self, login: str) -> str:
        normalized = self._normalize_login(login)
        if not normalized:
            return "Ungültiger Twitch-Login"

        deleted = 0
        try:
            with storage.get_conn() as c:
                cur = c.execute("DELETE FROM twitch_streamers WHERE twitch_login=?", (normalized,))
                deleted = cur.rowcount or 0
                c.execute("DELETE FROM twitch_live_state WHERE streamer_login=?", (normalized,))
        except Exception:
            log.exception("DB-Fehler beim Entfernen von %s", normalized)
            return "Datenbankfehler beim Entfernen."

        if deleted:
            return f"{normalized} entfernt"
        return f"{normalized} war nicht gespeichert"
