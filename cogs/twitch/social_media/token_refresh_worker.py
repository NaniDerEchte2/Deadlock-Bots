"""
Social Media Token Refresh Worker - Auto-refresh tokens before expiry.

Background worker that:
1. Runs every 5 minutes
2. Checks for tokens expiring within 1 hour
3. Refreshes them automatically
4. Updates encrypted storage
5. Logs failures for manual intervention

Pattern: Similar to TwitchBotTokenManager
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from discord.ext import commands

from ..storage import get_conn
from service.field_crypto import get_crypto
from .oauth_manager import SocialMediaOAuthManager

log = logging.getLogger("TwitchStreams.TokenRefreshWorker")


def _sanitize_log_value(value):
    """Prevent CRLF log-forging via untrusted values."""
    if value is None:
        return "<none>"
    return str(value).replace("\r", "\\r").replace("\n", "\\n")


class SocialMediaTokenRefreshWorker(commands.Cog):
    """Background worker for automatic token refresh."""

    def __init__(self, bot):
        """Initialize worker."""
        self.bot = bot
        self.enabled = True
        self.interval_seconds = 5 * 60  # 5 minutes
        self.refresh_threshold_hours = 1  # Refresh if expires within 1 hour

        self.crypto = get_crypto()
        self.oauth_manager = SocialMediaOAuthManager()

        # Start background task
        self._task = bot.loop.create_task(self._refresh_loop())
        log.info(
            "Token refresh worker started (interval=%ss, threshold=%sh)",
            self.interval_seconds,
            self.refresh_threshold_hours
        )

    def cog_unload(self):
        """Cleanup on cog unload."""
        if self._task:
            self._task.cancel()
        log.info("Token refresh worker stopped")

    async def _refresh_loop(self):
        """Main refresh loop."""
        await self.bot.wait_until_ready()
        await asyncio.sleep(60)  # Initial delay

        while not self.bot.is_closed() and self.enabled:
            try:
                await self._refresh_expiring_tokens()
            except Exception:
                log.exception("Token refresh run failed")

            await asyncio.sleep(self.interval_seconds)

    async def _refresh_expiring_tokens(self):
        """Refresh tokens expiring within threshold."""
        threshold = datetime.now(timezone.utc) + timedelta(hours=self.refresh_threshold_hours)

        # Find tokens expiring soon
        with get_conn() as conn:
            expiring = conn.execute(
                """
                SELECT id, platform, streamer_login,
                       refresh_token_enc, client_id, client_secret_enc,
                       token_expires_at, enc_version
                FROM social_media_platform_auth
                WHERE enabled = 1
                  AND refresh_token_enc IS NOT NULL
                  AND token_expires_at IS NOT NULL
                  AND token_expires_at < ?
                ORDER BY token_expires_at ASC
                """,
                (threshold.isoformat(),)
            ).fetchall()

        if not expiring:
            log.debug("No tokens expiring within %sh", self.refresh_threshold_hours)
            return

        log.info("Found %s tokens expiring within %sh", len(expiring), self.refresh_threshold_hours)

        # Refresh each token
        for row in expiring:
            try:
                await self._refresh_platform_token(row)
            except Exception:
                safe_platform = _sanitize_log_value(row["platform"])
                safe_streamer = _sanitize_log_value(row["streamer_login"])
                log.exception(
                    "Failed to refresh OAuth credentials for platform=%s, streamer=%s",
                    safe_platform,
                    safe_streamer
                )

    async def _refresh_platform_token(self, row: dict):
        """
        Refresh a single platform token.

        Args:
            row: Database row with encrypted tokens
        """
        platform = row["platform"]
        streamer_login = row["streamer_login"]
        safe_platform = _sanitize_log_value(platform)
        safe_streamer = _sanitize_log_value(streamer_login)
        row_id = f"{platform}|{streamer_login or 'global'}"

        log.info("Refreshing OAuth credentials for platform=%s, streamer=%s", safe_platform, safe_streamer)

        # Decrypt refresh token
        aad_refresh = f"social_media_platform_auth|refresh_token|{row_id}|{row['enc_version']}"
        refresh_token = self.crypto.decrypt_field(
            row["refresh_token_enc"],
            aad_refresh
        )

        # Decrypt client secret (if exists)
        client_secret = None
        if row["client_secret_enc"]:
            aad_secret = f"social_media_platform_auth|client_secret|{row_id}|{row['enc_version']}"
            client_secret = self.crypto.decrypt_field(
                row["client_secret_enc"],
                aad_secret
            )

        # Refresh token via OAuth manager
        try:
            new_tokens = await self.oauth_manager.refresh_token(
                platform=platform,
                refresh_token=refresh_token,
                client_id=row["client_id"],
                client_secret=client_secret or ""
            )
        except Exception:
            log.error(
                "OAuth credential refresh failed for platform=%s, streamer=%s",
                safe_platform,
                safe_streamer
            )
            # TODO: Send notification to user for re-auth
            return

        # Save new tokens (encrypted)
        await self._save_refreshed_tokens(
            platform=platform,
            streamer_login=streamer_login,
            row_id=row_id,
            new_tokens=new_tokens
        )

        log.info(
            "OAuth credentials refreshed successfully for platform=%s, streamer=%s",
            safe_platform,
            safe_streamer
        )

    async def _save_refreshed_tokens(
        self,
        platform: str,
        streamer_login: str,
        row_id: str,
        new_tokens: dict
    ):
        """Save refreshed tokens to database."""
        # Encrypt new access token
        aad_access = f"social_media_platform_auth|access_token|{row_id}|1"
        access_enc = self.crypto.encrypt_field(
            new_tokens["access_token"],
            aad_access,
            kid="v1"
        )

        # Encrypt new refresh token (if provided)
        refresh_enc = None
        if new_tokens.get("refresh_token"):
            aad_refresh = f"social_media_platform_auth|refresh_token|{row_id}|1"
            refresh_enc = self.crypto.encrypt_field(
                new_tokens["refresh_token"],
                aad_refresh,
                kid="v1"
            )

        # Update database
        with get_conn() as conn:
            if refresh_enc:
                # Update both access and refresh tokens
                conn.execute(
                    """
                    UPDATE social_media_platform_auth
                    SET access_token_enc = ?,
                        refresh_token_enc = ?,
                        token_expires_at = ?,
                        last_refreshed_at = CURRENT_TIMESTAMP
                    WHERE platform = ? AND (streamer_login = ? OR (streamer_login IS NULL AND ? IS NULL))
                    """,
                    (
                        access_enc,
                        refresh_enc,
                        new_tokens["expires_at"].isoformat() if isinstance(new_tokens["expires_at"], datetime) else new_tokens["expires_at"],
                        platform,
                        streamer_login,
                        streamer_login
                    )
                )
            else:
                # Update only access token (keep existing refresh token)
                conn.execute(
                    """
                    UPDATE social_media_platform_auth
                    SET access_token_enc = ?,
                        token_expires_at = ?,
                        last_refreshed_at = CURRENT_TIMESTAMP
                    WHERE platform = ? AND (streamer_login = ? OR (streamer_login IS NULL AND ? IS NULL))
                    """,
                    (
                        access_enc,
                        new_tokens["expires_at"].isoformat() if isinstance(new_tokens["expires_at"], datetime) else new_tokens["expires_at"],
                        platform,
                        streamer_login,
                        streamer_login
                    )
                )


async def setup(bot):
    """Setup function for Discord.py cog."""
    await bot.add_cog(SocialMediaTokenRefreshWorker(bot))
