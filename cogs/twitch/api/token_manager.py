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


def _exc_name(exc: BaseException) -> str:
    """Return exception class name for safe logging without secret-bearing payloads."""
    return exc.__class__.__name__


class TwitchBotTokenManager:
    """
    Manages Twitch bot tokens with automatic refresh and persistence.

    Features:
    - Automatic refresh ahead of expiry
    - Persistence via Windows Credential Manager (keyring)
    - Fallback to environment variables or token file
    - Token validation and bot id lookup
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        keyring_service: str = "DeadlockBot",
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.keyring_service = keyring_service

        self.access_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.expires_at: Optional[datetime] = None
        self.bot_id: Optional[str] = None

        self._lock = asyncio.Lock()
        self._refresh_task: Optional[asyncio.Task] = None
        self._on_refresh: Optional[
            Callable[[str, Optional[str], Optional[datetime]], Awaitable[None]]
        ] = None

    def set_refresh_callback(
        self,
        callback: Callable[[str, Optional[str], Optional[datetime]], Awaitable[None]],
    ) -> None:
        """Register a callback that is invoked after successful refreshes."""
        self._on_refresh = callback

    async def initialize(
        self, access_token: Optional[str] = None, refresh_token: Optional[str] = None
    ) -> bool:
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
                log.error(
                    "No Twitch bot access token available. Chat bot cannot start."
                )
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

            log.info("Auth manager initialised. Bot id: %s", self.bot_id or "unknown")
            return True

    async def get_valid_token(
        self, force_refresh: bool = False
    ) -> Tuple[str, Optional[str]]:
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

            # oauth:-Prefix entfernen – Helix-API erwartet reines Bearer-Token
            clean_token = self.access_token.replace("oauth:", "").strip()
            return clean_token, self.bot_id

    async def _validate_and_fetch_info(self) -> bool:
        """Validate the access token and fetch bot metadata."""
        if not self.access_token:
            return False

        try:
            token = self.access_token.replace("oauth:", "").strip()
            async with aiohttp.ClientSession() as session:
                headers = {"Authorization": f"OAuth {token}"}
                async with session.get(
                    "https://id.twitch.tv/oauth2/validate", headers=headers
                ) as resp:
                    if resp.status != 200:
                        # 401 beim Validate ist normal wenn das Token zwischen zwei
                        # Auto-Refresh-Zyklen abläuft – der Caller refresht dann.
                        lvl = logging.DEBUG if resp.status == 401 else logging.WARNING
                        log.log(lvl, "Token validation failed: HTTP %s", resp.status)
                        return False

                    data = await resp.json()
                    self.bot_id = data.get("user_id") or self.bot_id

                    scopes = data.get("scopes", [])
                    log.info(
                        "Bot auth validated. ID: %s, scope_count=%d",
                        self.bot_id,
                        len(scopes),
                    )

                    expires_in = data.get("expires_in", 0)
                    if expires_in:
                        self.expires_at = datetime.now() + timedelta(
                            seconds=int(expires_in)
                        )
                        log.info(
                            "Bot auth valid until %s",
                            self.expires_at.strftime("%Y-%m-%d %H:%M:%S"),
                        )  # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure

                if self.bot_id:
                    return True

                # Fallback to Helix users for the bot id
                headers_helix = {
                    "Client-ID": self.client_id,
                    "Authorization": f"Bearer {token}",
                }
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        "https://api.twitch.tv/helix/users", headers=headers_helix
                    ) as user_resp:
                        if user_resp.status == 200:
                            user_data = await user_resp.json()
                            if user_data.get("data"):
                                self.bot_id = (
                                    user_data["data"][0].get("id") or self.bot_id
                                )
                                return True
                        else:
                            log.warning(
                                "Failed to fetch bot id via Helix: HTTP %s",
                                user_resp.status,
                            )
        except Exception as exc:
            log.error(
                "Auth validation failed (%s).", _exc_name(exc)
            )  # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure

        return False

    async def _refresh_access_token(self) -> bool:
        """Refresh the access token using the refresh token."""
        if not self.refresh_token:
            log.error("No refresh token available; cannot refresh Twitch bot token.")
            return False

        try:
            url = "https://id.twitch.tv/oauth2/token"
            form_data = {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token.replace("oauth:", "").strip(),
            }

            async with aiohttp.ClientSession() as session:
                async with session.post(url, data=form_data) as resp:
                    if resp.status != 200:
                        log.error(
                            "Auth refresh failed: HTTP %s", resp.status
                        )  # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
                        return False

                    data = await resp.json()
                    self.access_token = data.get("access_token")
                    self.refresh_token = data.get("refresh_token", self.refresh_token)

                    expires_in = data.get("expires_in", 0)
                    if expires_in:
                        self.expires_at = datetime.now() + timedelta(
                            seconds=int(expires_in)
                        )

                    await self._save_tokens()
                    if self._on_refresh and self.access_token:
                        try:
                            await self._on_refresh(
                                self.access_token, self.refresh_token, self.expires_at
                            )
                        except Exception as exc:
                            log.debug("Refresh callback failed (%s).", _exc_name(exc))
                    log.info(
                        "Bot auth refreshed; valid until %s",
                        self.expires_at.strftime("%Y-%m-%d %H:%M:%S")
                        if self.expires_at
                        else "unknown",
                    )  # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
                    return True
        except Exception as exc:
            log.error(
                "Auth refresh exception (%s).", _exc_name(exc)
            )  # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
            return False

    async def _auto_refresh_loop(self):
        """Background task refreshing the token ahead of expiry."""
        while True:
            try:
                await asyncio.sleep(1800)  # 30 minutes

                if self.expires_at:
                    time_until_expiry = (
                        self.expires_at - datetime.now()
                    ).total_seconds()
                    if time_until_expiry < 3600:
                        log.info(
                            "Auto-refresh: bot token expires soon; refreshing now."
                        )
                        async with self._lock:
                            await self._refresh_access_token()
                    else:
                        log.debug(
                            "Auto-refresh: bot token valid for another %.1fh",
                            time_until_expiry / 3600,
                        )
            except Exception as exc:
                log.error("Auto-refresh loop error (%s).", _exc_name(exc))
                await asyncio.sleep(300)

    async def _load_tokens(self) -> Tuple[Optional[str], Optional[str]]:
        """Load tokens from environment, token file or keyring."""
        access = (os.getenv("TWITCH_BOT_TOKEN") or "").strip()
        refresh = (os.getenv("TWITCH_BOT_REFRESH_TOKEN") or "").strip()

        if access:
            if not refresh:
                log.warning(
                    "Access token found but no refresh token; automatic refresh not possible."
                )
            return access, refresh or None

        token_file = (os.getenv("TWITCH_BOT_TOKEN_FILE") or "").strip()
        if token_file:
            try:
                candidate = Path(token_file).read_text(encoding="utf-8").strip()
                if candidate:
                    return candidate, refresh or None
                log.warning(
                    "Configured bot auth file is empty."
                )  # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
            except Exception as exc:
                log.warning(
                    "Configured bot auth file could not be read (%s).", _exc_name(exc)
                )  # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure

        try:
            import keyring  # type: ignore

            # Neues Format: service=DeadlockBot, username=TWITCH_BOT_TOKEN
            access_keyring = keyring.get_password(
                self.keyring_service, "TWITCH_BOT_TOKEN"
            )
            # Fallback: altes Format (service=TWITCH_BOT_TOKEN@DeadlockBot)
            if not access_keyring:
                access_keyring = keyring.get_password(
                    f"TWITCH_BOT_TOKEN@{self.keyring_service}", "TWITCH_BOT_TOKEN"
                )

            refresh_keyring = keyring.get_password(
                self.keyring_service, "TWITCH_BOT_REFRESH_TOKEN"
            )
            if not refresh_keyring:
                refresh_keyring = keyring.get_password(
                    f"TWITCH_BOT_REFRESH_TOKEN@{self.keyring_service}",
                    "TWITCH_BOT_REFRESH_TOKEN",
                )

            if access_keyring:
                log.info("Loaded Twitch bot tokens from Windows Credential Manager.")
                return access_keyring, refresh_keyring or refresh or None
        except ImportError:
            log.debug("keyring not available; skipping credential manager.")
        except Exception as exc:
            log.debug("keyring lookup failed (%s).", _exc_name(exc))

        return None, None

    async def _save_tokens(self):
        """Persist tokens to Windows Credential Manager (if available)."""
        try:
            import keyring  # type: ignore
        except Exception as exc:
            log.debug(
                "keyring unavailable; bot auth not persisted (%s).", _exc_name(exc)
            )  # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
            return

        saved_types = []
        try:
            if self.access_token:
                await asyncio.to_thread(
                    keyring.set_password,
                    self.keyring_service,
                    "TWITCH_BOT_TOKEN",
                    self.access_token,
                )
                saved_types.append("ACCESS_TOKEN")

            if self.refresh_token:
                await asyncio.to_thread(
                    keyring.set_password,
                    self.keyring_service,
                    "TWITCH_BOT_REFRESH_TOKEN",
                    self.refresh_token,
                )
                saved_types.append("REFRESH_TOKEN")

            if saved_types:
                log.info(
                    "Bot auth saved in Windows vault (types: %s, service: %s).",
                    "+".join(saved_types),
                    self.keyring_service,
                )  # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
        except Exception as exc:
            log.error(
                "Could not persist bot auth in Windows vault (%s).", _exc_name(exc)
            )  # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure

    async def cleanup(self):
        """Stop the background auto-refresh task."""
        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                log.debug("Auto-refresh task cancelled during cleanup")


async def generate_oauth_tokens(
    client_id: str, client_secret: str, authorization_code: str, redirect_uri: str
) -> dict:
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
    form_data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": authorization_code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(url, data=form_data) as resp:
            if resp.status != 200:
                error = await resp.text()
                raise Exception(f"OAuth token exchange failed: {error}")

            return await resp.json()
