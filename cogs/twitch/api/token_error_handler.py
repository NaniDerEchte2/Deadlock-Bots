"""Token Error Handler für Twitch OAuth Refresh-Fehler.

Verwaltet:
- Blacklist für ungültige Refresh-Tokens
- Discord-Benachrichtigungen bei Token-Problemen
- Verhindert endlose Refresh-Versuche
"""
import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import discord

from ..storage import get_conn

log = logging.getLogger("TwitchStreams.TokenErrorHandler")

# Kanal-ID für Token-Fehler-Benachrichtigungen
TOKEN_ERROR_CHANNEL_ID = 1374364800817303632


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


class TokenErrorHandler:
    """Verwaltet Token-Fehler und verhindert endlose Refresh-Versuche."""

    def __init__(self, discord_bot: Optional[discord.Client] = None):
        """
        Args:
            discord_bot: Discord Bot-Instanz für Benachrichtigungen
        """
        self.discord_bot = discord_bot

    @staticmethod
    def _normalize_discord_user_id(raw: Optional[str]) -> Optional[str]:
        value = str(raw or "").strip()
        if value and value.isdigit():
            return value
        return None

    def _iter_role_guild_candidates(self) -> list[discord.Guild]:
        if not self.discord_bot:
            return []

        candidates: list[discord.Guild] = []
        seen: set[int] = set()
        for guild_id in (STREAMER_GUILD_ID, FALLBACK_MAIN_GUILD_ID):
            if guild_id and guild_id not in seen:
                seen.add(guild_id)
                guild = self.discord_bot.get_guild(guild_id)
                if guild is not None:
                    candidates.append(guild)

        if not candidates:
            candidates.extend(getattr(self.discord_bot, "guilds", []))
        return candidates

    async def _sync_streamer_role(
        self,
        discord_user_id: str,
        *,
        should_have_role: bool,
        reason: str,
    ) -> None:
        if not self.discord_bot or STREAMER_ROLE_ID <= 0:
            return

        normalized_id = self._normalize_discord_user_id(discord_user_id)
        if not normalized_id:
            return

        user_id_int = int(normalized_id)
        for guild in self._iter_role_guild_candidates():
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
                    log.info("Granted streamer role to Discord user %s in guild %s", normalized_id, guild.id)
                elif (not should_have_role) and has_role:
                    await member.remove_roles(role, reason=reason)
                    log.info("Removed streamer role from Discord user %s in guild %s", normalized_id, guild.id)
            except discord.Forbidden:
                log.warning("Missing permission to sync streamer role in guild %s", guild.id)
            except discord.HTTPException:
                log.warning("Discord API error while syncing streamer role in guild %s", guild.id)

    def schedule_streamer_role_sync(
        self,
        discord_user_id: Optional[str],
        *,
        should_have_role: bool,
        reason: str,
    ) -> None:
        normalized_id = self._normalize_discord_user_id(discord_user_id)
        if not normalized_id:
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        loop.create_task(
            self._sync_streamer_role(
                normalized_id,
                should_have_role=should_have_role,
                reason=reason,
            ),
            name="twitch.token_error.role_sync",
        )

    def is_token_blacklisted(self, twitch_user_id: str) -> bool:
        """
        Prüft, ob ein Token auf der Blacklist steht.
        
        Args:
            twitch_user_id: Twitch User ID
            
        Returns:
            True wenn Token blacklisted ist
        """
        try:
            with get_conn() as conn:
                row = conn.execute(
                    "SELECT 1 FROM twitch_token_blacklist WHERE twitch_user_id = ?",
                    (twitch_user_id,),
                ).fetchone()
                return bool(row)
        except Exception:
            log.error("Error checking token blacklist", exc_info=True)
            return False

    def add_to_blacklist(
        self,
        twitch_user_id: str,
        twitch_login: str,
        error_message: str,
    ):
        """
        Fügt einen Token zur Blacklist hinzu oder erhöht den Error-Counter.
        
        Args:
            twitch_user_id: Twitch User ID
            twitch_login: Twitch Login Name
            error_message: Fehlermeldung vom Token-Refresh
        """
        now = datetime.now(timezone.utc).isoformat()
        
        try:
            with get_conn() as conn:
                # Prüfe ob bereits vorhanden
                existing = conn.execute(
                    "SELECT error_count FROM twitch_token_blacklist WHERE twitch_user_id = ?",
                    (twitch_user_id,),
                ).fetchone()

                if existing:
                    # Erhöhe Counter
                    new_count = existing[0] + 1
                    conn.execute(
                        """
                        UPDATE twitch_token_blacklist
                        SET error_count = ?, last_error_at = ?, error_message = ?
                        WHERE twitch_user_id = ?
                        """,
                        (new_count, now, error_message, twitch_user_id),
                    )
                else:
                    # Neuer Eintrag
                    conn.execute(
                        """
                        INSERT INTO twitch_token_blacklist
                        (twitch_user_id, twitch_login, error_message, first_error_at, last_error_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (twitch_user_id, twitch_login, error_message, now, now),
                    )
                
                conn.commit()
                
            log.warning(
                "Blocked auto-refresh for %s (ID: %s) after auth failure.",
                twitch_login,
                twitch_user_id,
            )
            
            # Deaktiviere Auto-Raid für diesen Streamer
            self._disable_raid_bot(twitch_user_id)
            
        except Exception:
            log.error("Error adding to token blacklist", exc_info=True)

    def _disable_raid_bot(self, twitch_user_id: str):
        """Deaktiviert den Raid-Bot für einen Streamer mit Token-Fehler."""
        login_hint = ""
        discord_user_id = ""
        try:
            with get_conn() as conn:
                auth_row = conn.execute(
                    "SELECT twitch_login FROM twitch_raid_auth WHERE twitch_user_id = ?",
                    (twitch_user_id,),
                ).fetchone()
                if auth_row:
                    login_hint = str(
                        auth_row[0] if not hasattr(auth_row, "keys") else auth_row["twitch_login"] or ""
                    ).strip()

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
                        streamer_row[0] if not hasattr(streamer_row, "keys") else streamer_row["discord_user_id"] or ""
                    ).strip()

                conn.execute(
                    """
                    UPDATE twitch_raid_auth
                    SET raid_enabled = 0
                    WHERE twitch_user_id = ?
                    """,
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
            log.info("Disabled raid bot for user_id=%s due to token error", twitch_user_id)

            self.schedule_streamer_role_sync(
                discord_user_id,
                should_have_role=False,
                reason="Twitch-Bot Autorisierung ungültig",
            )
        except Exception:
            log.error("Error disabling raid bot", exc_info=True)

    def remove_from_blacklist(self, twitch_user_id: str):
        """
        Entfernt einen Token von der Blacklist (z.B. nach erfolgreicher Re-Autorisierung).
        
        Args:
            twitch_user_id: Twitch User ID
        """
        try:
            with get_conn() as conn:
                conn.execute(
                    "DELETE FROM twitch_token_blacklist WHERE twitch_user_id = ?",
                    (twitch_user_id,),
                )
                conn.commit()
            log.info("Removed user_id=%s from token blacklist", twitch_user_id)
        except Exception:
            log.error("Error removing from token blacklist", exc_info=True)

    async def notify_token_error(
        self,
        twitch_user_id: str,
        twitch_login: str,
        error_message: str,
    ):
        """
        Sendet eine Discord-Benachrichtigung über einen Token-Fehler.
        Wird nur einmal pro Streamer gesendet, um Spam zu vermeiden.
        
        Args:
            twitch_user_id: Twitch User ID
            twitch_login: Twitch Login Name
            error_message: Fehlermeldung vom Token-Refresh
        """
        if not self.discord_bot:
            log.warning("Discord bot not available, skipping notification")
            return

        # Prüfe ob bereits benachrichtigt
        try:
            with get_conn() as conn:
                row = conn.execute(
                    "SELECT notified FROM twitch_token_blacklist WHERE twitch_user_id = ?",
                    (twitch_user_id,),
                ).fetchone()
                
                if row and row[0] == 1:
                    log.debug("Already notified about auth error for %s", twitch_login)
                    return
        except Exception:
            log.error("Error checking notification status", exc_info=True)
            return

        try:
            channel = self.discord_bot.get_channel(TOKEN_ERROR_CHANNEL_ID)
            if not channel:
                log.warning("Auth error notification channel %s not found", TOKEN_ERROR_CHANNEL_ID)
                return

            # Erstelle Discord Embed
            embed = discord.Embed(
                title="⚠️ Twitch Token Error",
                description=f"Der Refresh-Token für **{twitch_login}** ist ungültig.",
                color=discord.Color.red(),
                timestamp=datetime.now(timezone.utc),
            )
            
            embed.add_field(
                name="Streamer",
                value=f"[{twitch_login}](https://twitch.tv/{twitch_login})",
                inline=True,
            )
            
            embed.add_field(
                name="User ID",
                value=f"`{twitch_user_id}`",
                inline=True,
            )
            
            embed.add_field(
                name="Fehler",
                value=f"```{error_message[:200]}```",
                inline=False,
            )
            
            embed.add_field(
                name="Aktion erforderlich",
                value=(
                    "Der Streamer muss sich **neu autorisieren**, damit der Raid-Bot wieder funktioniert.\n"
                    "➡️ Verwende `/twitch raid auth` um den Auth-Link zu erhalten."
                ),
                inline=False,
            )
            
            embed.add_field(
                name="Status",
                value="❌ Auto-Raid **deaktiviert** bis zur Re-Autorisierung",
                inline=False,
            )
            
            embed.set_footer(text="Twitch Raid Bot • Token Error Handler")

            await channel.send(embed=embed)
            
            # Markiere als benachrichtigt
            with get_conn() as conn:
                conn.execute(
                    """
                    UPDATE twitch_token_blacklist
                    SET notified = 1
                    WHERE twitch_user_id = ?
                    """,
                    (twitch_user_id,),
                )
                conn.commit()
                
            log.info("Sent auth error notification for %s to channel %s", twitch_login, TOKEN_ERROR_CHANNEL_ID)
            
        except Exception:
            log.error("Error sending token error notification", exc_info=True)

    def cleanup_old_entries(self, days: int = 30):
        """
        Entfernt alte Blacklist-Einträge.
        
        Args:
            days: Einträge älter als diese Anzahl Tage werden gelöscht
        """
        try:
            cutoff = datetime.now(timezone.utc).timestamp() - (days * 86400)
            cutoff_iso = datetime.fromtimestamp(cutoff, timezone.utc).isoformat()
            
            with get_conn() as conn:
                result = conn.execute(
                    """
                    DELETE FROM twitch_token_blacklist
                    WHERE last_error_at < ?
                    """,
                    (cutoff_iso,),
                )
                deleted = result.rowcount
                conn.commit()
                
            if deleted > 0:
                log.info("Cleaned up %d old token blacklist entries (>%d days)", deleted, days)
                
        except Exception:
            log.error("Error cleaning up token blacklist", exc_info=True)
