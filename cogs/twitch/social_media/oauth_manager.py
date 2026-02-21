"""
Social Media OAuth Manager - Handles OAuth flows for TikTok, YouTube, Instagram.

OAuth Flow:
1. generate_auth_url() - Create authorization URL with state token
2. User authorizes on platform
3. Platform redirects to callback with code
4. handle_callback() - Exchange code for tokens
5. save_encrypted_tokens() - Encrypt and store in DB

Platforms:
- TikTok: OAuth 2.0
- YouTube: OAuth 2.0 with PKCE
- Instagram: OAuth 2.0 (Meta/Facebook)
"""

import hashlib
import logging
import os
import secrets
from base64 import urlsafe_b64encode
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import aiohttp

from service.field_crypto import get_crypto

from ..storage import get_conn

log = logging.getLogger("TwitchStreams.OAuthManager")


def _sanitize_log_value(value: str | None) -> str:
    """Prevent CRLF log-forging via untrusted values."""
    if value is None:
        return "<none>"
    return str(value).replace("\r", "\\r").replace("\n", "\\n")


class SocialMediaOAuthManager:
    """Manages OAuth flows for social media platforms."""

    def __init__(self):
        """Initialize OAuth manager with crypto."""
        self.crypto = get_crypto()

    def generate_auth_url(
        self, platform: str, streamer_login: str | None, redirect_uri: str
    ) -> str:
        """
        Generate OAuth authorization URL.

        Args:
            platform: Platform name ('tiktok', 'youtube', 'instagram')
            streamer_login: Streamer login (None = bot-global)
            redirect_uri: OAuth callback URL

        Returns:
            Authorization URL for user to visit
        """
        # Generate CSRF state token
        state = secrets.token_urlsafe(32)

        # Generate PKCE verifier (TikTok v2 and YouTube both require PKCE)
        pkce_verifier = secrets.token_urlsafe(64) if platform in ("tiktok", "youtube") else None

        # Store state in DB (expires in 10 minutes)
        expires_at = datetime.now(UTC) + timedelta(minutes=10)

        with get_conn() as conn:
            conn.execute(
                """
                INSERT INTO oauth_state_tokens
                    (state_token, platform, streamer_login, redirect_uri, pkce_verifier, expires_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    state,
                    platform,
                    streamer_login,
                    redirect_uri,
                    pkce_verifier,
                    expires_at.isoformat(),
                ),
            )

        # Generate platform-specific URL
        if platform == "tiktok":
            return self._tiktok_auth_url(state, redirect_uri, pkce_verifier)
        elif platform == "youtube":
            return self._youtube_auth_url(state, redirect_uri, pkce_verifier)
        elif platform == "instagram":
            return self._instagram_auth_url(state, redirect_uri)
        else:
            raise ValueError(f"Unknown platform: {platform}")

    def _tiktok_auth_url(self, state: str, redirect_uri: str, verifier: str) -> str:
        """Generate TikTok OAuth URL with PKCE (required by TikTok API v2)."""
        client_key = os.environ.get("TIKTOK_CLIENT_KEY", "")
        if not client_key:
            raise ValueError(
                "TIKTOK_CLIENT_KEY nicht konfiguriert. "
                "Bitte im Windows-Vault unter 'DeadlockBot' / 'TIKTOK_CLIENT_KEY' eintragen."
            )

        # PKCE challenge (S256)
        challenge = (
            urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        )

        scopes = "user.info.basic,video.upload,video.publish"

        params = {
            "client_key": client_key,
            "scope": scopes,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }

        return f"https://www.tiktok.com/v2/auth/authorize/?{urlencode(params)}"

    def _youtube_auth_url(self, state: str, redirect_uri: str, verifier: str) -> str:
        """Generate YouTube OAuth URL with PKCE."""
        client_id = os.environ.get("YOUTUBE_CLIENT_ID", "")
        if not client_id:
            raise ValueError(
                "YOUTUBE_CLIENT_ID nicht konfiguriert. "
                "Bitte im Windows-Vault unter 'DeadlockBot' / 'YOUTUBE_CLIENT_ID' eintragen."
            )

        # PKCE challenge
        challenge = (
            urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        )

        scopes = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly"

        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": scopes,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "access_type": "offline",  # Request refresh token
            "prompt": "consent",  # Force consent screen to get refresh token
        }

        return f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"

    def _instagram_auth_url(self, state: str, redirect_uri: str) -> str:
        """Generate Instagram OAuth URL (via Facebook/Meta)."""
        client_id = os.environ.get("INSTAGRAM_CLIENT_ID", "")
        if not client_id:
            raise ValueError(
                "INSTAGRAM_CLIENT_ID nicht konfiguriert. "
                "Bitte im Windows-Vault unter 'DeadlockBot' / 'INSTAGRAM_CLIENT_ID' eintragen."
            )
        scopes = "instagram_basic,instagram_content_publish"

        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": scopes,
            "response_type": "code",
            "state": state,
        }

        return f"https://api.instagram.com/oauth/authorize?{urlencode(params)}"

    async def handle_callback(self, code: str, state: str) -> dict:
        """
        Handle OAuth callback, exchange code for tokens.

        Args:
            code: Authorization code from platform
            state: State token for CSRF protection

        Returns:
            Dict with platform and streamer_login

        Raises:
            ValueError: If state invalid or expired
        """
        # Verify and consume state token
        with get_conn() as conn:
            state_row = conn.execute(
                """
                SELECT platform, streamer_login, redirect_uri, pkce_verifier
                FROM oauth_state_tokens
                WHERE state_token = ?
                  AND expires_at > ?
                """,
                (state, datetime.now(UTC).isoformat()),
            ).fetchone()

            if not state_row:
                raise ValueError("Invalid or expired state token")

            # Delete state (one-time use)
            conn.execute("DELETE FROM oauth_state_tokens WHERE state_token = ?", (state,))

        platform = state_row["platform"]
        streamer_login = state_row["streamer_login"]
        redirect_uri = state_row["redirect_uri"]
        pkce_verifier = state_row["pkce_verifier"]

        # Exchange code for tokens
        if platform == "tiktok":
            tokens = await self._tiktok_exchange_code(code, redirect_uri, pkce_verifier)
        elif platform == "youtube":
            tokens = await self._youtube_exchange_code(code, redirect_uri, pkce_verifier)
        elif platform == "instagram":
            tokens = await self._instagram_exchange_code(code, redirect_uri)
        else:
            raise ValueError(f"Unknown platform: {platform}")

        # Save encrypted tokens
        await self.save_encrypted_tokens(platform, streamer_login, tokens)

        return {
            "platform": platform,
            "streamer_login": streamer_login,
        }

    async def _tiktok_exchange_code(self, code: str, redirect_uri: str, verifier: str) -> dict:
        """Exchange TikTok authorization code for tokens (with PKCE code_verifier)."""
        client_key = os.environ.get("TIKTOK_CLIENT_KEY", "")
        client_secret = os.environ.get("TIKTOK_CLIENT_SECRET", "")

        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://open.tiktokapis.com/v2/oauth/token/",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={
                    "client_key": client_key,
                    "client_secret": client_secret,
                    "code": code,
                    "grant_type": "authorization_code",
                    "redirect_uri": redirect_uri,
                    "code_verifier": verifier,
                },
            ) as resp:
                data = await resp.json()

                if resp.status != 200 or "error" in data:
                    raise ValueError(f"TikTok token exchange failed: {data}")

                return {
                    "access_token": data["data"]["access_token"],
                    "refresh_token": data["data"]["refresh_token"],
                    "expires_at": datetime.now(UTC) + timedelta(seconds=data["data"]["expires_in"]),
                    "scopes": data["data"]["scope"],
                    "user_id": data["data"].get("open_id"),
                    "client_id": client_key,
                    "client_secret": client_secret,
                }

    async def _youtube_exchange_code(self, code: str, redirect_uri: str, verifier: str) -> dict:
        """Exchange YouTube authorization code for tokens (with PKCE)."""
        client_id = os.environ.get("YOUTUBE_CLIENT_ID", "")
        client_secret = os.environ.get("YOUTUBE_CLIENT_SECRET", "")

        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "code": code,
                    "code_verifier": verifier,
                    "grant_type": "authorization_code",
                    "redirect_uri": redirect_uri,
                },
            ) as resp:
                data = await resp.json()

                if resp.status != 200 or "error" in data:
                    raise ValueError(f"YouTube token exchange failed: {data}")

                return {
                    "access_token": data["access_token"],
                    "refresh_token": data.get("refresh_token"),  # Only on first auth
                    "expires_at": datetime.now(UTC) + timedelta(seconds=data["expires_in"]),
                    "scopes": data["scope"],
                    "client_id": client_id,
                    "client_secret": client_secret,
                }

    async def _instagram_exchange_code(self, code: str, redirect_uri: str) -> dict:
        """Exchange Instagram authorization code for tokens."""
        client_id = os.environ.get("INSTAGRAM_CLIENT_ID", "")
        client_secret = os.environ.get("INSTAGRAM_CLIENT_SECRET", "")

        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.instagram.com/oauth/access_token",
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "grant_type": "authorization_code",
                    "redirect_uri": redirect_uri,
                    "code": code,
                },
            ) as resp:
                data = await resp.json()

                if resp.status != 200 or "error_message" in data:
                    raise ValueError(f"Instagram token exchange failed: {data}")

                return {
                    "access_token": data["access_token"],
                    "user_id": data["user_id"],
                    "expires_at": datetime.now(UTC) + timedelta(days=60),  # Long-lived token
                    "client_id": client_id,
                    "client_secret": client_secret,
                }

    async def save_encrypted_tokens(self, platform: str, streamer_login: str | None, tokens: dict):
        """
        Save tokens with encryption.

        Args:
            platform: Platform name
            streamer_login: Streamer login (None = bot-global)
            tokens: Token data from exchange
        """
        with get_conn() as conn:
            # Build row identifier for AAD
            row_id = f"{platform}|{streamer_login or 'global'}"

            # Encrypt access token
            aad_access = f"social_media_platform_auth|access_token|{row_id}|1"
            access_enc = self.crypto.encrypt_field(tokens["access_token"], aad_access, kid="v1")

            # Encrypt refresh token (if exists)
            refresh_enc = None
            if tokens.get("refresh_token"):
                aad_refresh = f"social_media_platform_auth|refresh_token|{row_id}|1"
                refresh_enc = self.crypto.encrypt_field(
                    tokens["refresh_token"], aad_refresh, kid="v1"
                )

            # Encrypt client secret (if exists)
            secret_enc = None
            if tokens.get("client_secret"):
                aad_secret = f"social_media_platform_auth|client_secret|{row_id}|1"
                secret_enc = self.crypto.encrypt_field(
                    tokens["client_secret"], aad_secret, kid="v1"
                )

            # Save to database
            conn.execute(
                """
                INSERT INTO social_media_platform_auth
                    (platform, streamer_login, access_token_enc, refresh_token_enc,
                     client_id, client_secret_enc, token_expires_at, scopes,
                     platform_user_id, platform_username, enc_version, enc_kid)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'v1')
                ON CONFLICT(platform, streamer_login) DO UPDATE SET
                    access_token_enc = excluded.access_token_enc,
                    refresh_token_enc = excluded.refresh_token_enc,
                    client_secret_enc = excluded.client_secret_enc,
                    token_expires_at = excluded.token_expires_at,
                    scopes = excluded.scopes,
                    platform_user_id = excluded.platform_user_id,
                    platform_username = excluded.platform_username,
                    last_refreshed_at = CURRENT_TIMESTAMP
                """,
                (
                    platform,
                    streamer_login,
                    access_enc,
                    refresh_enc,
                    tokens.get("client_id"),
                    secret_enc,
                    tokens["expires_at"].isoformat()
                    if isinstance(tokens["expires_at"], datetime)
                    else tokens["expires_at"],
                    tokens.get("scopes"),
                    tokens.get("user_id"),
                    tokens.get("username"),
                ),
            )

            safe_platform = _sanitize_log_value(platform)
            safe_streamer = _sanitize_log_value(streamer_login)
            log.info(
                "Saved encrypted auth data for platform=%s, streamer=%s",
                safe_platform,
                safe_streamer,
            )

    async def refresh_token(
        self, platform: str, refresh_token: str, client_id: str, client_secret: str
    ) -> dict:
        """
        Refresh an access token.

        Args:
            platform: Platform name
            refresh_token: Refresh token
            client_id: OAuth client ID
            client_secret: OAuth client secret

        Returns:
            New token data
        """
        if platform == "tiktok":
            return await self._refresh_tiktok_token(refresh_token, client_id, client_secret)
        elif platform == "youtube":
            return await self._refresh_youtube_token(refresh_token, client_id, client_secret)
        else:
            raise ValueError(f"Token refresh not supported for platform: {platform}")

    async def _refresh_tiktok_token(
        self, refresh_token: str, client_key: str, client_secret: str
    ) -> dict:
        """Refresh TikTok access token."""
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://open.tiktokapis.com/v2/oauth/token/",
                json={
                    "client_key": client_key,
                    "client_secret": client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
            ) as resp:
                data = await resp.json()

                if resp.status != 200 or "error" in data:
                    raise ValueError(f"TikTok token refresh failed: {data}")

                return {
                    "access_token": data["data"]["access_token"],
                    "refresh_token": data["data"]["refresh_token"],
                    "expires_at": datetime.now(UTC) + timedelta(seconds=data["data"]["expires_in"]),
                }

    async def _refresh_youtube_token(
        self, refresh_token: str, client_id: str, client_secret: str
    ) -> dict:
        """Refresh YouTube access token."""
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
            ) as resp:
                data = await resp.json()

                if resp.status != 200 or "error" in data:
                    raise ValueError(f"YouTube token refresh failed: {data}")

                return {
                    "access_token": data["access_token"],
                    "expires_at": datetime.now(UTC) + timedelta(seconds=data["expires_in"]),
                }
