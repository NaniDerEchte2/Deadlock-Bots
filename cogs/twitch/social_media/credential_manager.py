"""
Social Media Credential Manager - Loads encrypted credentials from DB.

Manages OAuth credentials for social media platforms:
- TikTok
- YouTube
- Instagram

Features:
- Loads encrypted tokens from database
- Decrypts using AES-256-GCM
- Per-streamer credential support (with fallback to global)
- Automatic token refresh check
"""

import logging
from datetime import datetime, timezone
from typing import Dict, Optional

from ..storage import get_conn
from service.field_crypto import get_crypto, DecryptFailed

log = logging.getLogger("TwitchStreams.CredentialManager")


class SocialMediaCredentialManager:
    """Manages encrypted social media platform credentials."""

    def __init__(self):
        """Initialize credential manager with crypto."""
        self.crypto = get_crypto()

    def get_credentials(
        self,
        platform: str,
        streamer_login: Optional[str] = None
    ) -> Optional[Dict]:
        """
        Fetch and decrypt credentials for a platform.

        Args:
            platform: Platform name ('tiktok', 'youtube', 'instagram')
            streamer_login: Streamer login (None = bot-global)

        Returns:
            Dict with decrypted credentials or None if not found

        Example:
            creds = manager.get_credentials("tiktok", "earlysalty")
            if creds:
                access_token = creds["access_token"]
                refresh_token = creds["refresh_token"]
        """
        with get_conn() as conn:
            # Try streamer-specific first, then fall back to global
            # ORDER BY prioritizes streamer-specific over global
            row = conn.execute(
                """
                SELECT id, platform, streamer_login,
                       access_token_enc, refresh_token_enc, client_id, client_secret_enc,
                       token_expires_at, scopes, platform_user_id, platform_username,
                       enc_version, enc_kid
                FROM social_media_platform_auth
                WHERE platform = ?
                  AND (streamer_login = ? OR (streamer_login IS NULL AND ? IS NULL))
                  AND enabled = 1
                ORDER BY streamer_login IS NOT NULL DESC
                LIMIT 1
                """,
                (platform, streamer_login, streamer_login)
            ).fetchone()

            if not row:
                log.debug(
                    "No credentials found for platform=%s, streamer=%s",
                    platform, streamer_login
                )
                return None

            # Build AAD (Associated Authenticated Data)
            # Format: table|column|row_id|version
            row_id = f"{row['platform']}|{row['streamer_login'] or 'global'}"

            try:
                # Decrypt access token
                aad_access = f"social_media_platform_auth|access_token|{row_id}|{row['enc_version']}"
                access_token = self.crypto.decrypt_field(
                    row["access_token_enc"],
                    aad_access
                )

                # Decrypt refresh token (if exists)
                refresh_token = None
                if row["refresh_token_enc"]:
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

                # Return decrypted credentials
                return {
                    "id": row["id"],
                    "platform": row["platform"],
                    "streamer_login": row["streamer_login"],
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                    "client_id": row["client_id"],  # Not encrypted (public)
                    "client_secret": client_secret,
                    "expires_at": row["token_expires_at"],
                    "scopes": row["scopes"],
                    "platform_user_id": row["platform_user_id"],
                    "platform_username": row["platform_username"],
                }

            except DecryptFailed as e:
                log.error(
                    "Failed to decrypt credentials for platform=%s, streamer=%s: %s",
                    platform, streamer_login, e
                )
                return None
            except Exception as e:
                log.exception(
                    "Unexpected error loading credentials for platform=%s, streamer=%s",
                    platform, streamer_login
                )
                return None

    def is_token_expired(self, credentials: Dict) -> bool:
        """
        Check if token is expired or near expiry.

        Args:
            credentials: Credentials dict from get_credentials()

        Returns:
            True if expired or expires within 1 hour
        """
        if not credentials or not credentials.get("expires_at"):
            return True

        try:
            expires_at = datetime.fromisoformat(credentials["expires_at"].replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)

            # Consider expired if less than 1 hour remaining
            time_remaining = (expires_at - now).total_seconds()
            return time_remaining < 3600  # 1 hour
        except Exception as e:
            log.error("Failed to parse expiry time: %s", e)
            return True

    def get_all_platforms_status(
        self,
        streamer_login: Optional[str] = None
    ) -> Dict[str, Dict]:
        """
        Get connection status for all platforms.

        Args:
            streamer_login: Streamer login (None = bot-global)

        Returns:
            Dict mapping platform -> status info
        """
        platforms = {}

        for platform in ["tiktok", "youtube", "instagram"]:
            creds = self.get_credentials(platform, streamer_login)

            if creds:
                platforms[platform] = {
                    "connected": True,
                    "username": creds.get("platform_username"),
                    "user_id": creds.get("platform_user_id"),
                    "expires_at": creds.get("expires_at"),
                    "expired": self.is_token_expired(creds),
                    "scopes": creds.get("scopes"),
                }
            else:
                platforms[platform] = {
                    "connected": False,
                    "username": None,
                    "user_id": None,
                    "expires_at": None,
                    "expired": False,
                    "scopes": None,
                }

        return platforms
