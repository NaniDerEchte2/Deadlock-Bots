import asyncio
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Tuple

log = logging.getLogger("TwitchStreams.ChatBot")

_KEYRING_SERVICE = "DeadlockBot"


def _read_keyring_secret(key: str) -> Optional[str]:
    """Read a secret from Windows Credential Manager."""
    try:
        import keyring  # type: ignore
    except Exception:
        return None

    for service in (_KEYRING_SERVICE, f"{key}@{_KEYRING_SERVICE}"):
        try:
            val = keyring.get_password(service, key)
            if val:
                return val
        except Exception:
            continue
    return None


async def _save_bot_tokens_to_keyring(
    *, access_token: str, refresh_token: Optional[str]
) -> None:
    """Persist access/refresh tokens to Windows Credential Manager."""
    try:
        import keyring  # type: ignore
    except Exception:
        log.debug("keyring nicht verfügbar – Tokens können nicht persistiert werden.")
        return

    async def _save_one(service: str, name: str, value: str) -> None:
        await asyncio.to_thread(keyring.set_password, service, name, value)

    tasks = []
    saved_types = []
    if access_token:
        tasks.append(_save_one(_KEYRING_SERVICE, "TWITCH_BOT_TOKEN", access_token))
        saved_types.append("ACCESS_TOKEN")
    if refresh_token:
        tasks.append(
            _save_one(_KEYRING_SERVICE, "TWITCH_BOT_REFRESH_TOKEN", refresh_token)
        )
        saved_types.append("REFRESH_TOKEN")

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
        log.info(
            "Bot auth saved in Windows vault (types: %s, service: %s).",
            "+".join(saved_types),
            _KEYRING_SERVICE,
        )


def load_bot_tokens(
    *, log_missing: bool = True
) -> Tuple[Optional[str], Optional[str], Optional[int]]:
    """
    Load the Twitch bot OAuth token and refresh token from env/file/Windows keyring.

    Returns:
        (access_token, refresh_token, expiry_ts_utc)
    """
    raw_env = os.getenv("TWITCH_BOT_TOKEN", "") or ""
    raw_refresh = os.getenv("TWITCH_BOT_REFRESH_TOKEN", "") or ""
    token = raw_env.strip()
    refresh = raw_refresh.strip() or None
    expiry_ts: Optional[int] = None

    if token:
        return token, refresh, expiry_ts

    token_file = (os.getenv("TWITCH_BOT_TOKEN_FILE") or "").strip()
    if token_file:
        try:
            candidate = Path(token_file).read_text(encoding="utf-8").strip()
            if candidate:
                return candidate, refresh, expiry_ts
            if log_missing:
                log.warning(
                    "Konfigurierte Bot-Auth-Datei ist leer."
                )  # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
        except Exception as exc:  # pragma: no cover - defensive logging
            if log_missing:
                log.warning(
                    "Konfigurierte Bot-Auth-Datei konnte nicht gelesen werden (%s).",
                    exc.__class__.__name__,
                )  # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure

    keyring_token = _read_keyring_secret("TWITCH_BOT_TOKEN")
    keyring_refresh = _read_keyring_secret("TWITCH_BOT_REFRESH_TOKEN")
    if keyring_token:
        return keyring_token, keyring_refresh or refresh, expiry_ts

    if log_missing:
        log.warning(
            "TWITCH_BOT_TOKEN nicht gesetzt. Twitch Chat Bot wird nicht gestartet. "
            "Bitte setze ein OAuth-Token für den Bot-Account."
        )
    return None, None, None


def load_bot_token(*, log_missing: bool = True) -> Optional[str]:
    token, _, _ = load_bot_tokens(log_missing=log_missing)
    return token


class TokenPersistenceMixin:
    async def _persist_bot_tokens(
        self,
        *,
        access_token: str,
        refresh_token: Optional[str],
        expires_in: Optional[int],
        scopes: Optional[list] = None,
        user_id: Optional[str] = None,
    ) -> None:
        """Persist bot tokens in Windows Credential Manager (keyring)."""
        if not access_token:
            return

        if getattr(self, "_token_manager", None):
            self._token_manager.access_token = access_token
            if refresh_token:
                self._token_manager.refresh_token = refresh_token
            if user_id:
                self._token_manager.bot_id = str(user_id)
            if expires_in:
                self._token_manager.expires_at = datetime.now() + timedelta(
                    seconds=int(expires_in)
                )
            await self._token_manager._save_tokens()
            return

        await _save_bot_tokens_to_keyring(
            access_token=access_token,
            refresh_token=refresh_token,
        )
