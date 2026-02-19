"""Steam token storage helper (Windows Credential Manager with file fallback)."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Optional, Tuple

log = logging.getLogger(__name__)

KEYRING_SERVICE = (os.getenv("STEAM_TOKEN_KEYRING_SERVICE") or "DeadlockBot").strip() or "DeadlockBot"

TOKEN_REFRESH_KEY = "STEAM_REFRESH_TOKEN"
TOKEN_MACHINE_KEY = "STEAM_MACHINE_AUTH_TOKEN"
TOKEN_REFRESH_SAVED_AT_KEY = "STEAM_REFRESH_TOKEN_SAVED_AT"
TOKEN_MACHINE_SAVED_AT_KEY = "STEAM_MACHINE_AUTH_TOKEN_SAVED_AT"

_FALSE_VALUES = {"0", "false", "no", "off"}


def _flag_enabled(name: str, default: bool = True) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw not in _FALSE_VALUES


def _vault_enabled() -> bool:
    return os.name == "nt" and _flag_enabled("STEAM_USE_WINDOWS_VAULT", True)


def _keyring_targets(secret_key: str) -> Tuple[Tuple[str, str], Tuple[str, str]]:
    return (
        (KEYRING_SERVICE, secret_key),
        (f"{secret_key}@{KEYRING_SERVICE}", secret_key),
    )


@lru_cache(maxsize=1)
def _get_keyring():
    if not _vault_enabled():
        return None

    try:
        import keyring  # type: ignore
        return keyring
    except Exception:
        log.debug(
            "Steam vault backend unavailable; using file token storage.",
            exc_info=log.isEnabledFor(logging.DEBUG),
        )
        return None


def token_storage_mode() -> str:
    return "windows_vault" if _get_keyring() is not None else "file"


def _presence_data_dir() -> Path:
    configured = (os.getenv("STEAM_PRESENCE_DATA_DIR") or "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parent / "steam_presence" / ".steam-data"


def refresh_token_path() -> Path:
    return _presence_data_dir() / "refresh.token"


def machine_auth_token_path() -> Path:
    return _presence_data_dir() / "machine_auth_token.txt"


def _normalize_token(value: Optional[str]) -> str:
    return str(value).strip() if value else ""


def _read_token_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""
    except Exception:
        log.exception("Failed to read Steam token file", extra={"path": str(path)})
        return ""


def _write_token_file(path: Path, value: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{value}\n", encoding="utf-8")
    except Exception:
        log.exception("Failed to write Steam token file", extra={"path": str(path)})


def _remove_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except Exception:
        log.exception("Failed to remove Steam token file", extra={"path": str(path)})


def _file_mtime_iso(path: Path) -> Optional[str]:
    try:
        ts = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except Exception:
        return None
    return ts.isoformat()


def _parse_iso_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _vault_get(secret_key: str) -> Optional[str]:
    keyring = _get_keyring()
    if keyring is None:
        return None

    for service_name, user_name in _keyring_targets(secret_key):
        try:
            value = keyring.get_password(service_name, user_name)
        except Exception:
            continue
        if value:
            norm = str(value).strip()
            if norm:
                return norm
    return None


def _vault_set(secret_key: str, value: str) -> bool:
    keyring = _get_keyring()
    if keyring is None:
        return False

    wrote_any = False
    for service_name, user_name in _keyring_targets(secret_key):
        try:
            keyring.set_password(service_name, user_name, value)
            wrote_any = True
        except Exception:
            continue
    return wrote_any


def _vault_delete(secret_key: str) -> bool:
    keyring = _get_keyring()
    if keyring is None:
        return False

    for service_name, user_name in _keyring_targets(secret_key):
        try:
            keyring.delete_password(service_name, user_name)
        except Exception:
            continue
    return True


def _read_token(
    *,
    token_key: str,
    saved_at_key: str,
    file_path: Path,
) -> str:
    token = _vault_get(token_key)
    if token:
        return token

    token = _read_token_file(file_path)
    if not token:
        return ""

    # One-way migration: file -> vault (if available), then remove file.
    if _get_keyring() is not None and _vault_set(token_key, token):
        saved_at = _file_mtime_iso(file_path) or _now_iso()
        _vault_set(saved_at_key, saved_at)
        _remove_file(file_path)
        log.info("Migrated Steam credential from file storage to Windows vault.")

    return token


def _write_token(
    *,
    value: Optional[str],
    token_key: str,
    saved_at_key: str,
    file_path: Path,
    saved_at_iso: Optional[str] = None,
) -> str:
    token = _normalize_token(value)

    if _get_keyring() is not None:
        if token:
            if _vault_set(token_key, token):
                _vault_set(saved_at_key, saved_at_iso or _now_iso())
                _remove_file(file_path)
                return "windows_vault"
        else:
            _vault_delete(token_key)
            _vault_delete(saved_at_key)
            _remove_file(file_path)
            return "windows_vault"

    if token:
        _write_token_file(file_path, token)
    else:
        _remove_file(file_path)
    return "file"


def read_refresh_token(file_path: Optional[Path] = None) -> str:
    return _read_token(
        token_key=TOKEN_REFRESH_KEY,
        saved_at_key=TOKEN_REFRESH_SAVED_AT_KEY,
        file_path=file_path or refresh_token_path(),
    )


def read_machine_auth_token(file_path: Optional[Path] = None) -> str:
    return _read_token(
        token_key=TOKEN_MACHINE_KEY,
        saved_at_key=TOKEN_MACHINE_SAVED_AT_KEY,
        file_path=file_path or machine_auth_token_path(),
    )


def write_refresh_token(
    value: Optional[str],
    file_path: Optional[Path] = None,
    *,
    saved_at_iso: Optional[str] = None,
) -> str:
    return _write_token(
        value=value,
        token_key=TOKEN_REFRESH_KEY,
        saved_at_key=TOKEN_REFRESH_SAVED_AT_KEY,
        file_path=file_path or refresh_token_path(),
        saved_at_iso=saved_at_iso,
    )


def write_machine_auth_token(
    value: Optional[str],
    file_path: Optional[Path] = None,
    *,
    saved_at_iso: Optional[str] = None,
) -> str:
    return _write_token(
        value=value,
        token_key=TOKEN_MACHINE_KEY,
        saved_at_key=TOKEN_MACHINE_SAVED_AT_KEY,
        file_path=file_path or machine_auth_token_path(),
        saved_at_iso=saved_at_iso,
    )


def refresh_token_exists(file_path: Optional[Path] = None) -> bool:
    if _vault_get(TOKEN_REFRESH_KEY):
        return True
    return bool(_read_token_file(file_path or refresh_token_path()))


def machine_auth_token_exists(file_path: Optional[Path] = None) -> bool:
    if _vault_get(TOKEN_MACHINE_KEY):
        return True
    return bool(_read_token_file(file_path or machine_auth_token_path()))


def get_refresh_token_age_days(file_path: Optional[Path] = None) -> Optional[int]:
    saved_at = _parse_iso_timestamp(_vault_get(TOKEN_REFRESH_SAVED_AT_KEY))
    if saved_at is not None:
        return max(0, (datetime.now(timezone.utc) - saved_at).days)

    if _vault_get(TOKEN_REFRESH_KEY):
        return 0

    path = file_path or refresh_token_path()
    token = _read_token_file(path)
    if not token:
        return None
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except Exception:
        return None
    return max(0, (datetime.now(timezone.utc) - mtime).days)


def clear_tokens(
    *,
    refresh_file_path: Optional[Path] = None,
    machine_file_path: Optional[Path] = None,
) -> list[str]:
    removed: list[str] = []
    refresh_path = refresh_file_path or refresh_token_path()
    machine_path = machine_file_path or machine_auth_token_path()

    if refresh_token_exists(refresh_path):
        removed.append("refresh_token")
    if machine_auth_token_exists(machine_path):
        removed.append("machine_auth")

    write_refresh_token("", refresh_path)
    write_machine_auth_token("", machine_path)
    return removed


__all__ = [
    "clear_tokens",
    "get_refresh_token_age_days",
    "machine_auth_token_exists",
    "machine_auth_token_path",
    "read_machine_auth_token",
    "read_refresh_token",
    "refresh_token_exists",
    "refresh_token_path",
    "token_storage_mode",
    "write_machine_auth_token",
    "write_refresh_token",
]
