"""Token Error Handler für Twitch OAuth Refresh-Fehler.

Verwaltet:
- Blacklist für ungültige Refresh-Tokens
- Discord-Benachrichtigungen bei Token-Problemen
- Verhindert endlose Refresh-Versuche
"""
import asyncio
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

import discord

from ..storage import get_conn

log = logging.getLogger("TwitchStreams.TokenErrorHandler")

# Kanal-ID für Token-Fehler-Benachrichtigungen (Admin)
TOKEN_ERROR_CHANNEL_ID = 1374364800817303632

# Grace-Period: Wie viele Tage der User Zeit hat bevor die Rolle entfernt wird
GRACE_PERIOD_DAYS = 7


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
        self._migrate_db()

    @staticmethod
    def _migrate_db() -> None:
        """Fügt neue Spalten zur twitch_token_blacklist hinzu (idempotent)."""
        new_cols = [
            ("grace_expires_at", "TEXT"),
            ("user_dm_sent", "INTEGER DEFAULT 0"),
            ("reminder_sent", "INTEGER DEFAULT 0"),
            ("role_removed", "INTEGER DEFAULT 0"),
        ]
        try:
            with get_conn() as conn:
                existing = {row[1] for row in conn.execute("PRAGMA table_info(twitch_token_blacklist)")}
                for col_name, col_type in new_cols:
                    if col_name not in existing:
                        conn.execute(
                            f"ALTER TABLE twitch_token_blacklist ADD COLUMN {col_name} {col_type}"
                        )
                conn.commit()
        except Exception:
            log.warning("DB migration for twitch_token_blacklist failed (non-critical)", exc_info=True)

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

    # Anzahl aufeinanderfolgender Fehler, bevor der Raid-Bot wirklich deaktiviert wird
    BLACKLIST_DISABLE_THRESHOLD = 3
    CONSECUTIVE_FAILURE_WINDOW_HOURS = 12

    def is_token_blacklisted(self, twitch_user_id: str) -> bool:
        """
        Prüft, ob ein Token endgültig gesperrt ist (>= BLACKLIST_DISABLE_THRESHOLD Fehler).

        Args:
            twitch_user_id: Twitch User ID

        Returns:
            True wenn Token dauerhaft blacklisted ist
        """
        try:
            with get_conn() as conn:
                row = conn.execute(
                    "SELECT error_count FROM twitch_token_blacklist WHERE twitch_user_id = ?",
                    (twitch_user_id,),
                ).fetchone()
                if not row:
                    return False
                return int(row[0]) >= self.BLACKLIST_DISABLE_THRESHOLD
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
                    "SELECT error_count, last_error_at FROM twitch_token_blacklist WHERE twitch_user_id = ?",
                    (twitch_user_id,),
                ).fetchone()

                if existing:
                    prior_count = int(existing[0] or 0)
                    last_error_raw = existing[1]
                    reset_counter = False

                    if last_error_raw:
                        try:
                            last_error_dt = datetime.fromisoformat(str(last_error_raw).replace("Z", "+00:00"))
                            if last_error_dt.tzinfo is None:
                                last_error_dt = last_error_dt.replace(tzinfo=timezone.utc)
                            reset_counter = (datetime.now(timezone.utc) - last_error_dt) > timedelta(
                                hours=self.CONSECUTIVE_FAILURE_WINDOW_HOURS
                            )
                        except Exception:
                            reset_counter = False

                    if reset_counter:
                        new_count = 1
                        conn.execute(
                            """
                            UPDATE twitch_token_blacklist
                            SET error_count = ?, first_error_at = ?, last_error_at = ?,
                                error_message = ?, notified = 0
                            WHERE twitch_user_id = ?
                            """,
                            (new_count, now, now, error_message, twitch_user_id),
                        )
                        log.info(
                            "Reset token failure counter for %s after %dh without errors",
                            twitch_login,
                            self.CONSECUTIVE_FAILURE_WINDOW_HOURS,
                        )
                    else:
                        # Erhöhe Counter innerhalb des Consecutive-Fensters
                        new_count = max(1, prior_count + 1)
                        conn.execute(
                            """
                            UPDATE twitch_token_blacklist
                            SET error_count = ?, last_error_at = ?, error_message = ?
                            WHERE twitch_user_id = ?
                            """,
                            (new_count, now, error_message, twitch_user_id),
                        )
                else:
                    # Neuer Eintrag – Grace-Period ab jetzt
                    grace_expires = (
                        datetime.now(timezone.utc) + timedelta(days=GRACE_PERIOD_DAYS)
                    ).isoformat()
                    conn.execute(
                        """
                        INSERT INTO twitch_token_blacklist
                        (twitch_user_id, twitch_login, error_message, first_error_at, last_error_at,
                         grace_expires_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (twitch_user_id, twitch_login, error_message, now, now, grace_expires),
                    )
                
                conn.commit()

            # error_count nach dem Commit neu lesen (könnte direkt aus dem UPSERT stammen)
            try:
                with get_conn() as conn:
                    cnt_row = conn.execute(
                        "SELECT error_count FROM twitch_token_blacklist WHERE twitch_user_id = ?",
                        (twitch_user_id,),
                    ).fetchone()
                current_count = int(cnt_row[0]) if cnt_row else 1
            except Exception:
                current_count = 1

            log.warning(
                "Blocked auto-refresh for %s (ID: %s) after auth failure. "
                "Consecutive failures: %d/%d",
                twitch_login,
                twitch_user_id,
                current_count,
                self.BLACKLIST_DISABLE_THRESHOLD,
            )

            # Raid-Bot erst nach BLACKLIST_DISABLE_THRESHOLD aufeinanderfolgenden Fehlern deaktivieren
            if current_count >= self.BLACKLIST_DISABLE_THRESHOLD:
                self._disable_raid_bot(twitch_user_id)
            else:
                log.info(
                    "Token error for %s (count %d/%d) – will retry before disabling",
                    twitch_login,
                    current_count,
                    self.BLACKLIST_DISABLE_THRESHOLD,
                )
            
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
            # Rolle wird NICHT sofort entfernt – User hat %d Tage Grace-Period
            # Stelle sicher dass grace_expires_at gesetzt ist (wurde beim ersten Blacklist-Eintrag gesetzt)
            try:
                with get_conn() as conn:
                    row = conn.execute(
                        "SELECT grace_expires_at FROM twitch_token_blacklist WHERE twitch_user_id = ?",
                        (twitch_user_id,),
                    ).fetchone()
                    if row and not row[0]:
                        grace_expires = (
                            datetime.now(timezone.utc) + timedelta(days=GRACE_PERIOD_DAYS)
                        ).isoformat()
                        conn.execute(
                            "UPDATE twitch_token_blacklist SET grace_expires_at = ? WHERE twitch_user_id = ?",
                            (grace_expires, twitch_user_id),
                        )
                        conn.commit()
            except Exception:
                log.warning("Could not ensure grace_expires_at for %s", twitch_user_id, exc_info=True)
        except Exception:
            log.error("Error disabling raid bot", exc_info=True)

    # Minimale Wartezeit zwischen zwei Refresh-Versuchen nach einem Fehler (in Stunden)
    RETRY_COOLDOWN_HOURS = 2

    def has_recent_failure(self, twitch_user_id: str) -> bool:
        """
        Gibt True zurück, wenn in den letzten RETRY_COOLDOWN_HOURS ein Fehler aufgetreten ist.
        Verhindert, dass der Maintenance-Loop oder on-demand Refreshes zu schnell wiederholt werden.
        Nur relevant wenn error_count < BLACKLIST_DISABLE_THRESHOLD (vollständig blacklisted
        wird bereits via is_token_blacklisted() abgefangen).
        """
        try:
            with get_conn() as conn:
                row = conn.execute(
                    "SELECT error_count, last_error_at FROM twitch_token_blacklist WHERE twitch_user_id = ?",
                    (twitch_user_id,),
                ).fetchone()
                if not row or not row[1]:
                    return False
                error_count = int(row[0] or 0)
                # Vollständig blacklisted – wird separat via is_token_blacklisted() behandelt
                if error_count >= self.BLACKLIST_DISABLE_THRESHOLD:
                    return False
                last_error_raw = str(row[1])
                last_error_dt = datetime.fromisoformat(last_error_raw.replace("Z", "+00:00"))
                if last_error_dt.tzinfo is None:
                    last_error_dt = last_error_dt.replace(tzinfo=timezone.utc)
                return (datetime.now(timezone.utc) - last_error_dt) < timedelta(hours=self.RETRY_COOLDOWN_HOURS)
        except Exception:
            log.error("Error in has_recent_failure for %s", twitch_user_id, exc_info=True)
            return False

    def clear_failure_count(self, twitch_user_id: str) -> None:
        """
        Setzt den Fehler-Counter zurück (z.B. nach erfolgreichem Refresh).
        Löscht den Blacklist-Eintrag komplett, falls vorhanden.
        """
        try:
            with get_conn() as conn:
                conn.execute(
                    "DELETE FROM twitch_token_blacklist WHERE twitch_user_id = ?",
                    (twitch_user_id,),
                )
                conn.commit()
        except Exception:
            log.error("Error clearing failure count for %s", twitch_user_id, exc_info=True)

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

            # User-DM senden
            await self._send_user_dm_token_error(twitch_user_id, twitch_login, error_message)

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

    async def _send_user_dm_token_error(
        self,
        twitch_user_id: str,
        twitch_login: str,
        error_message: str,
        *,
        is_reminder: bool = False,
    ) -> bool:
        """Sendet dem Streamer eine DM über den Token-Fehler. Gibt True zurück wenn erfolgreich."""
        if not self.discord_bot:
            return False

        discord_user_id = self._get_discord_user_id(twitch_user_id, twitch_login)
        if not discord_user_id:
            log.debug("No discord_user_id found for %s, cannot send DM", twitch_login)
            return False

        try:
            user = await self.discord_bot.fetch_user(int(discord_user_id))
        except Exception:
            log.debug("Could not fetch Discord user %s for DM", discord_user_id)
            return False

        grace_dt = datetime.now(timezone.utc) + timedelta(days=GRACE_PERIOD_DAYS)
        deadline_ts = int(grace_dt.timestamp())

        if is_reminder:
            embed = discord.Embed(
                title="⚠️ Twitch Bot – Autorisierung weiterhin fehlgeschlagen",
                description=f"Die Autorisierung für den Twitch Bot ist für **{twitch_login}** seit {GRACE_PERIOD_DAYS} Tagen nicht erneuert worden.",
                color=discord.Color.dark_red(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(
                name="Streamer",
                value=f"[{twitch_login}](https://twitch.tv/{twitch_login})",
                inline=True,
            )
            embed.add_field(
                name="Status",
                value="❌ Streamer-Rolle entzogen",
                inline=True,
            )
            embed.add_field(
                name="Lösung",
                value=(
                    "Klicke auf den Button unten, um einen neuen Auth-Link zu erhalten.\n"
                    "Nach erfolgreicher Autorisierung wird die Streamer-Rolle automatisch wiederhergestellt.\n\n"
                    "Alternativ: `/twitch raid auth` auf dem Discord-Server nutzen."
                ),
                inline=False,
            )
            embed.add_field(
                name="Hinweis",
                value=(
                    "Bei Problemen oder Fragen bitte auf dem Server melden.\n"
                    "Die Autorisierung ist erforderlich, um weiterhin am Partnerprogramm teilzunehmen."
                ),
                inline=False,
            )
        else:
            embed = discord.Embed(
                title="⚠️ Twitch Bot – Autorisierung fehlgeschlagen",
                description=f"Die Autorisierung für den Twitch Bot ist für **{twitch_login}** fehlgeschlagen und muss erneuert werden.",
                color=discord.Color.orange(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(
                name="Streamer",
                value=f"[{twitch_login}](https://twitch.tv/{twitch_login})",
                inline=True,
            )
            embed.add_field(
                name="Mögliche Ursachen",
                value=(
                    "· Du hast das Passwort geändert\n"
                    "· Du hast 2FA aktiviert oder geändert\n"
                    "· Du bist Twitch Affiliate oder Partner geworden\n"
                    "· Bot in den Twitch-Einstellungen deautorisiert\n"
                    "· Sicherheitsänderung im Twitch-Profil geändert\n"
                    "· Du hast deine Email Adresse geändert."
                ),
                inline=False,
            )
            embed.add_field(
                name="Lösung",
                value=(
                    "Klicke auf den Button unten, um einen neuen Auth-Link zu erhalten.\n"
                    "Nach erfolgreicher Autorisierung wird der Bot automatisch wieder aktiviert.\n\n"
                ),
                inline=False,
            )
            embed.add_field(
                name="Das passiert bei Nichtbehebung:",
                value=(
                    f"Wird die Autorisierung bis <t:{deadline_ts}:F> nicht erneuert, "
                    f"wird die Streamer-Partnerschaft vorübergehend entzogen.\n"
                    "Sie kann jederzeit durch eine erneute Autorisierung erneuert werden."
                ),
                inline=False,
            )
            embed.add_field(
                name="Hinweis",
                value=(
                    "Bei Problemen oder Fragen bitte dich sofort bei @EarlySalty melden :).\n"
                    "Die Autorisierung ist erforderlich, um weiterhin am Partnerprogramm teilzunehmen "
                    "und alle Features zu nutzen (Auto-Raid, Chat-Schutz, Analytics)."
                ),
                inline=False,
            )

        embed.set_footer(text="Twitch Raid Bot • Token Error Handler")

        # Auth-Button anhängen (persistent, funktioniert auch in DMs)
        try:
            from ..raid.views import RaidAuthGenerateView
            view = RaidAuthGenerateView(twitch_login=twitch_login, button_label="🔗 Auth-Link erzeugen")
        except Exception:
            view = None

        try:
            await user.send(embed=embed, view=view)
            log.info("Sent token error DM to Discord user %s (%s)", discord_user_id, twitch_login)
            # user_dm_sent in DB markieren
            try:
                with get_conn() as conn:
                    conn.execute(
                        "UPDATE twitch_token_blacklist SET user_dm_sent = 1 WHERE twitch_user_id = ?",
                        (twitch_user_id,),
                    )
                    conn.commit()
            except Exception:
                pass
            return True
        except discord.Forbidden:
            log.info("Cannot DM Discord user %s (DMs closed), skipping", discord_user_id)
            return False
        except Exception:
            log.warning("Failed to send token error DM to %s", discord_user_id, exc_info=True)
            return False

    def _get_discord_user_id(self, twitch_user_id: str, twitch_login: str) -> Optional[str]:
        """Holt die Discord User ID eines Streamers aus der DB."""
        try:
            with get_conn() as conn:
                row = conn.execute(
                    """
                    SELECT discord_user_id FROM twitch_streamers
                    WHERE twitch_user_id = ?
                       OR (? <> '' AND LOWER(twitch_login) = LOWER(?))
                    LIMIT 1
                    """,
                    (twitch_user_id, twitch_login, twitch_login),
                ).fetchone()
                if row:
                    val = str(row[0] if not hasattr(row, "keys") else row["discord_user_id"] or "").strip()
                    return val if val.isdigit() else None
        except Exception:
            log.warning("Could not fetch discord_user_id for %s", twitch_login, exc_info=True)
        return None

    async def check_grace_periods(self) -> None:
        """
        Prüft abgelaufene Grace-Periods (stündlich aufrufen).
        - Sendet Erinnerungs-DM an User + Admin-Channel-Notification
        - Entfernt die Streamer-Rolle nach Ablauf der Frist
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            with get_conn() as conn:
                expired = conn.execute(
                    """
                    SELECT twitch_user_id, twitch_login, error_message,
                           reminder_sent, role_removed, grace_expires_at
                    FROM twitch_token_blacklist
                    WHERE error_count >= ?
                      AND grace_expires_at IS NOT NULL
                      AND grace_expires_at <= ?
                      AND role_removed = 0
                    """,
                    (self.BLACKLIST_DISABLE_THRESHOLD, now_iso),
                ).fetchall()
        except Exception:
            log.error("check_grace_periods: DB query failed", exc_info=True)
            return

        for row in expired:
            uid, login, err_msg, reminder_sent, role_removed, grace_expires_at = row
            discord_user_id = self._get_discord_user_id(uid, login)

            # 1. Erinnerungs-DM an User
            if not reminder_sent:
                await self._send_user_dm_token_error(uid, login, err_msg or "", is_reminder=True)
                # Admin-Channel benachrichtigen damit Admin selbst auch schreiben kann
                await self._notify_admin_grace_expired(uid, login, discord_user_id)
                try:
                    with get_conn() as conn:
                        conn.execute(
                            "UPDATE twitch_token_blacklist SET reminder_sent = 1 WHERE twitch_user_id = ?",
                            (uid,),
                        )
                        conn.commit()
                except Exception:
                    log.warning("Could not set reminder_sent for %s", login, exc_info=True)

            # 2. Streamer-Rolle entfernen
            if discord_user_id:
                self.schedule_streamer_role_sync(
                    discord_user_id,
                    should_have_role=False,
                    reason=f"Twitch-Token seit {GRACE_PERIOD_DAYS} Tagen ungültig – Grace-Period abgelaufen",
                )
            try:
                with get_conn() as conn:
                    conn.execute(
                        "UPDATE twitch_token_blacklist SET role_removed = 1 WHERE twitch_user_id = ?",
                        (uid,),
                    )
                    conn.commit()
            except Exception:
                log.warning("Could not set role_removed for %s", login, exc_info=True)

            log.info(
                "Grace period expired for %s (id=%s) – role removed, reminder sent",
                login, uid,
            )

    async def _notify_admin_grace_expired(
        self,
        twitch_user_id: str,
        twitch_login: str,
        discord_user_id: Optional[str],
    ) -> None:
        """Benachrichtigt den Admin-Channel wenn eine Grace-Period abgelaufen ist."""
        if not self.discord_bot:
            return
        try:
            channel = self.discord_bot.get_channel(TOKEN_ERROR_CHANNEL_ID)
            if not channel:
                return

            mention = f"<@{discord_user_id}>" if discord_user_id else f"`{twitch_login}`"
            embed = discord.Embed(
                title="🚨 Grace-Period abgelaufen – Streamer-Rolle entzogen",
                description=(
                    f"Der Streamer **{twitch_login}** hat seinen Token innerhalb von "
                    f"**{GRACE_PERIOD_DAYS} Tagen** nicht erneuert.\n"
                    f"Die Streamer-Rolle wurde automatisch entzogen."
                ),
                color=discord.Color.dark_red(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="Streamer", value=f"[{twitch_login}](https://twitch.tv/{twitch_login})", inline=True)
            embed.add_field(name="Discord", value=mention, inline=True)
            embed.add_field(name="User ID", value=f"`{twitch_user_id}`", inline=True)
            embed.add_field(
                name="Nächste Schritte",
                value=(
                    f"Bitte kontaktiere {mention} direkt.\n"
                    f"Der User kann sich über `/twitch raid auth` neu autorisieren um die Rolle zurückzubekommen."
                ),
                inline=False,
            )
            embed.set_footer(text="Twitch Raid Bot • Grace-Period Handler")
            await channel.send(embed=embed)
        except Exception:
            log.warning("Failed to send grace-expired admin notification for %s", twitch_login, exc_info=True)

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
