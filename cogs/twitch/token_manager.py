"""
Token manager for the Twitch chat bot with automatic refresh and persistence.
"""
import asyncio
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Awaitable, Callable, Optional, Tuple

import aiohttp


log = logging.getLogger("TwitchStreams.TokenManager")


class TwitchBotTokenManager:
    """
    Manages Twitch bot tokens with automatic refresh and persistence.

    Features:
    - Automatic refresh ahead of expiry
    - Persistence via Windows Credential Manager (keyring)
    - Fallback to environment variables or token file
    - Token validation and bot id lookup
    """

    def __init__(self, client_id: str, client_secret: str, *, keyring_service: str = "DeadlockBot"):
        self.client_id = client_id
        self.client_secret = client_secret
        self.keyring_service = keyring_service

        self.access_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.expires_at: Optional[datetime] = None
        self.bot_id: Optional[str] = None

        self._lock = asyncio.Lock()
        self._refresh_task: Optional[asyncio.Task] = None
        self._on_refresh: Optional[Callable[[str, Optional[str], Optional[datetime]], Awaitable[None]]] = None

    def set_refresh_callback(
        self,
        callback: Callable[[str, Optional[str], Optional[datetime]], Awaitable[None]],
    ) -> None:
        """Register a callback that is invoked after successful refreshes."""
        self._on_refresh = callback

    async def initialize(self, access_token: Optional[str] = None, refresh_token: Optional[str] = None) -> bool:
        """
        Load tokens, validate them and start the auto-refresh loop.

        Returns:
            True if initialisation succeeded, False otherwise.
        """
        async with self._lock:
            if access_token:
                self.access_token = access_token
            if refresh_token:
                self.refresh_token = refresh_token

            if not self.access_token:
                loaded_access, loaded_refresh = await self._load_tokens()
                self.access_token = loaded_access
                self.refresh_token = self.refresh_token or loaded_refresh

            if not self.access_token:
                log.error("No Twitch bot access token available. Chat bot cannot start.")
                return False

            is_valid = await self._validate_and_fetch_info()
            if not is_valid:
                if self.refresh_token:
                    log.info("Access token invalid, attempting refresh.")
                    refreshed = await self._refresh_access_token()
                    if not refreshed:
                        log.error("Token refresh failed.")
                        return False
                else:
                    log.error("Token invalid and no refresh token available.")
                    return False

            if self._refresh_task is None or self._refresh_task.done():
                self._refresh_task = asyncio.create_task(self._auto_refresh_loop())

            # Sicherstellen, dass die Tokens persistiert werden (z.B. falls sie aus ENV geladen wurden)
            await self._save_tokens()

            log.info("Token manager initialised. Bot id: %s", self.bot_id or "unknown")
            return True

    async def get_valid_token(self, force_refresh: bool = False) -> Tuple[str, Optional[str]]:
        """
        Return a valid access token (auto-refreshing if needed).

        Args:
            force_refresh: If True, triggers a refresh even if the token is not expired.

        Returns:
            (access_token, bot_id)
        """
        async with self._lock:
            should_refresh = force_refresh
            if not should_refresh and self.expires_at:
                if datetime.now() >= self.expires_at - timedelta(minutes=5):
                    log.info("Access token close to expiry; refreshing.")
                    should_refresh = True
            
            if should_refresh:
                await self._refresh_access_token()

            if not self.access_token:
                raise RuntimeError("No valid Twitch bot token available.")

            return self.access_token, self.bot_id

    async def _validate_and_fetch_info(self) -> bool:
        """Validate the access token and fetch bot metadata."""
        if not self.access_token:
            return False

        try:
            token = self.access_token.replace("oauth:", "").strip()
            async with aiohttp.ClientSession() as session:
                headers = {"Authorization": f"OAuth {token}"}
                async with session.get("https://id.twitch.tv/oauth2/validate", headers=headers) as resp:
                    if resp.status != 200:
                        log.warning("Token validation failed: HTTP %s", resp.status)
                        return False

                    data = await resp.json()
                    self.bot_id = data.get("user_id") or self.bot_id
                    
                    scopes = data.get("scopes", [])
                    log.info("Bot token validated. ID: %s, Scopes: %s", self.bot_id, scopes)

                    expires_in = data.get("expires_in", 0)
                    if expires_in:
                        self.expires_at = datetime.now() + timedelta(seconds=int(expires_in))
                        log.info("Bot token valid until %s", self.expires_at.strftime("%Y-%m-%d %H:%M:%S"))

                if self.bot_id:
                    return True

                # Fallback to Helix users for the bot id
                headers_helix = {
                    "Client-ID": self.client_id,
                    "Authorization": f"Bearer {token}",
                }
                async with aiohttp.ClientSession() as session:
                    async with session.get("https://api.twitch.tv/helix/users", headers=headers_helix) as user_resp:
                        if user_resp.status == 200:
                            user_data = await user_resp.json()
                            if user_data.get("data"):
                                self.bot_id = user_data["data"][0].get("id") or self.bot_id
                                return True
                        else:
                            log.warning("Failed to fetch bot id via Helix: HTTP %s", user_resp.status)
        except Exception as exc:
            log.error("Token validation error: %s", exc)

        return False

    async def _refresh_access_token(self) -> bool:
        """Refresh the access token using the refresh token."""
        if not self.refresh_token:
            log.error("No refresh token available; cannot refresh Twitch bot token.")
            return False

        try:
            url = "https://id.twitch.tv/oauth2/token"
            params = {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token.replace("oauth:", "").strip(),
            }

            async with aiohttp.ClientSession() as session:
                async with session.post(url, params=params) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        log.error("Token refresh failed: HTTP %s: %s", resp.status, error_text)
                        return False

                    data = await resp.json()
                    self.access_token = data.get("access_token")
                    self.refresh_token = data.get("refresh_token", self.refresh_token)

                    expires_in = data.get("expires_in", 0)
                    if expires_in:
                        self.expires_at = datetime.now() + timedelta(seconds=int(expires_in))

                    await self._save_tokens()
                    if self._on_refresh and self.access_token:
                        try:
                            await self._on_refresh(self.access_token, self.refresh_token, self.expires_at)
                        except Exception as exc:
                            log.debug("Refresh callback failed: %s", exc)
                    log.info(
                        "Twitch bot token refreshed; valid until %s",
                        self.expires_at.strftime("%Y-%m-%d %H:%M:%S") if self.expires_at else "unknown",
                    )
                    return True
        except Exception as exc:
            log.error("Token refresh exception: %s", exc)
            return False

    async def _auto_refresh_loop(self):
        """Background task refreshing the token ahead of expiry."""
        while True:
            try:
                await asyncio.sleep(1800)  # 30 minutes

                if self.expires_at:
                    time_until_expiry = (self.expires_at - datetime.now()).total_seconds()
                    if time_until_expiry < 3600:
                        log.info("Auto-refresh: bot token expires soon; refreshing now.")
                        async with self._lock:
                            await self._refresh_access_token()
                    else:
                        log.debug("Auto-refresh: bot token valid for another %.1fh", time_until_expiry / 3600)
            except Exception as exc:
                log.error("Auto-refresh loop error: %s", exc)
                await asyncio.sleep(300)

    async def _load_tokens(self) -> Tuple[Optional[str], Optional[str]]:
        """Load tokens from environment, token file or keyring."""
        access = (os.getenv("TWITCH_BOT_TOKEN") or "").strip()
        refresh = (os.getenv("TWITCH_BOT_REFRESH_TOKEN") or "").strip()

        if access:
            if not refresh:
                log.warning("Access token found but no refresh token; automatic refresh not possible.")
            return access, refresh or None

        token_file = (os.getenv("TWITCH_BOT_TOKEN_FILE") or "").strip()
        if token_file:
            try:
                candidate = Path(token_file).read_text(encoding="utf-8").strip()
                if candidate:
                    return candidate, refresh or None
                log.warning("TWITCH_BOT_TOKEN_FILE is set (%s) but empty.", token_file)
            except Exception as exc:
                log.warning("Could not read TWITCH_BOT_TOKEN_FILE (%s): %s", token_file, exc)

        try:
            import keyring  # type: ignore

            # Wir suchen primär nach dem Format ZWECK@DeadlockBot
            # Prevent double-prefixing if self.keyring_service already contains the prefix
            service_access = self.keyring_service if self.keyring_service.startswith("TWITCH_BOT_TOKEN@") else f"TWITCH_BOT_TOKEN@{self.keyring_service}"
            access_keyring = keyring.get_password(service_access, "TWITCH_BOT_TOKEN")
            if not access_keyring:
                access_keyring = keyring.get_password(self.keyring_service, "TWITCH_BOT_TOKEN")

            service_refresh = self.keyring_service if self.keyring_service.startswith("TWITCH_BOT_REFRESH_TOKEN@") else f"TWITCH_BOT_REFRESH_TOKEN@{self.keyring_service}"
            refresh_keyring = keyring.get_password(service_refresh, "TWITCH_BOT_REFRESH_TOKEN")
            if not refresh_keyring:
                refresh_keyring = keyring.get_password(self.keyring_service, "TWITCH_BOT_REFRESH_TOKEN")

            if access_keyring:
                log.info("Loaded Twitch bot tokens from Windows Credential Manager.")
                return access_keyring, refresh_keyring or refresh or None
        except ImportError:
            log.debug("keyring not available; skipping credential manager.")
        except Exception as exc:
            log.debug("keyring lookup failed: %s", exc)

        return None, None

    async def _save_tokens(self):
        """Persist tokens to Windows Credential Manager (if available)."""
        try:
            import keyring  # type: ignore
        except Exception as exc:
            log.debug("keyring not available; cannot persist Twitch bot tokens: %s", exc)
            return

        saved_types = []
        try:
            if self.access_token:
                service_access = self.keyring_service if self.keyring_service.startswith("TWITCH_BOT_TOKEN@") else f"TWITCH_BOT_TOKEN@{self.keyring_service}"
                await asyncio.to_thread(
                    keyring.set_password,
                    service_access,
                    "TWITCH_BOT_TOKEN",
                    self.access_token,
                )
                saved_types.append("ACCESS_TOKEN")

            if self.refresh_token:
                service_refresh = self.keyring_service if self.keyring_service.startswith("TWITCH_BOT_REFRESH_TOKEN@") else f"TWITCH_BOT_REFRESH_TOKEN@{self.keyring_service}"
                await asyncio.to_thread(
                    keyring.set_password,
                    service_refresh,
                    "TWITCH_BOT_REFRESH_TOKEN",
                    self.refresh_token,
                )
                saved_types.append("REFRESH_TOKEN")

            if saved_types:
                log.info("Twitch Bot Tokens (%s) im Windows Credential Manager gespeichert (Dienst: %s).", "+".join(saved_types), self.keyring_service)
        except Exception as exc:
            log.error("Could not persist Twitch bot tokens: %s", exc)

    async def cleanup(self):
        """Stop the background auto-refresh task."""
        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                log.debug("Auto-refresh task cancelled during cleanup")


async def generate_oauth_tokens(client_id: str, client_secret: str, authorization_code: str, redirect_uri: str) -> dict:
    """
    Exchange an OAuth authorization code for access and refresh tokens.

    Returns:
        {
            "access_token": str,
            "refresh_token": str,
            "expires_in": int,
            "token_type": "bearer"
        }
    """
    url = "https://id.twitch.tv/oauth2/token"
    params = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": authorization_code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(url, params=params) as resp:
            if resp.status != 200:
                error = await resp.text()
                raise Exception(f"OAuth token exchange failed: {error}")

            return await resp.json()
