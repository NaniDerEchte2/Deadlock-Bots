from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("StreamerOnboarding.TwitchIntegration")

_BLOCKING_TOKEN_BLACKLIST_THRESHOLD = 1


class TwitchPartnerIntegrationUnavailable(RuntimeError):
    """Raised when the external Deadlock-Twitch-Bot integration cannot be used."""


@dataclass(frozen=True, slots=True)
class TwitchPartnerAuthState:
    twitch_login: str | None
    twitch_user_id: str | None
    authorized: bool


@dataclass(frozen=True, slots=True)
class _ExternalModules:
    repo_path: Path
    get_conn: Any
    raid_auth_manager_cls: Any
    default_redirect_uri: str


_EXTERNAL_MODULES: _ExternalModules | None = None
_AUTH_MANAGER: Any | None = None


def _normalize_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "y", "on", "t"}


def _normalize_login(value: object) -> str:
    return str(value or "").strip().lower()


def _row_value(row: Any, key: str, index: int) -> Any:
    if row is None:
        return None
    if hasattr(row, "keys"):
        return row[key]
    return row[index]


def _candidate_repo_paths() -> list[Path]:
    candidates: list[Path] = []

    configured = (os.getenv("DEADLOCK_TWITCH_BOT_DIR") or "").strip()
    if configured:
        candidates.append(Path(configured).expanduser())

    base = Path(__file__).resolve()
    candidates.extend(
        [
            base.parents[3] / "Deadlock-Twitch-Bot",
            base.parents[2].parent / "Deadlock-Twitch-Bot",
        ]
    )

    unique: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except Exception:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(resolved)
    return unique


def _resolve_repo_path() -> Path:
    for candidate in _candidate_repo_paths():
        if candidate.is_dir():
            return candidate
    searched = ", ".join(str(path) for path in _candidate_repo_paths()) or "<none>"
    raise TwitchPartnerIntegrationUnavailable(
        "Deadlock-Twitch-Bot wurde nicht gefunden. "
        f"Gepruefte Pfade: {searched}"
    )


def _load_external_modules() -> _ExternalModules:
    global _EXTERNAL_MODULES
    if _EXTERNAL_MODULES is not None:
        return _EXTERNAL_MODULES

    repo_path = _resolve_repo_path()
    repo_path_str = str(repo_path)
    if repo_path_str not in sys.path:
        sys.path.insert(0, repo_path_str)

    try:
        from bot.core.constants import TWITCH_RAID_REDIRECT_URI
        from bot.raid.auth import RaidAuthManager
        from bot.storage import get_conn
    except Exception as exc:
        raise TwitchPartnerIntegrationUnavailable(
            "Deadlock-Twitch-Bot konnte nicht geladen werden."
        ) from exc

    _EXTERNAL_MODULES = _ExternalModules(
        repo_path=repo_path,
        get_conn=get_conn,
        raid_auth_manager_cls=RaidAuthManager,
        default_redirect_uri=str(TWITCH_RAID_REDIRECT_URI or "").strip(),
    )
    return _EXTERNAL_MODULES


def _auth_manager() -> Any:
    global _AUTH_MANAGER
    if _AUTH_MANAGER is not None:
        return _AUTH_MANAGER

    modules = _load_external_modules()
    client_id = (os.getenv("TWITCH_CLIENT_ID") or "").strip()
    client_secret = (os.getenv("TWITCH_CLIENT_SECRET") or "").strip()
    redirect_uri = (os.getenv("TWITCH_RAID_REDIRECT_URI") or modules.default_redirect_uri).strip()

    if not client_id or not client_secret or not redirect_uri:
        raise TwitchPartnerIntegrationUnavailable(
            "Twitch OAuth ist nicht konfiguriert "
            "(TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET / TWITCH_RAID_REDIRECT_URI)."
        )

    try:
        _AUTH_MANAGER = modules.raid_auth_manager_cls(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
        )
    except TypeError:
        _AUTH_MANAGER = modules.raid_auth_manager_cls(client_id, client_secret, redirect_uri)
    except Exception as exc:
        raise TwitchPartnerIntegrationUnavailable(
            "RaidAuthManager aus Deadlock-Twitch-Bot konnte nicht initialisiert werden."
        ) from exc
    return _AUTH_MANAGER


def generate_discord_auth_url(discord_user_id: int) -> str:
    auth_url = str(_auth_manager().generate_discord_button_url(f"discord:{discord_user_id}") or "")
    auth_url = auth_url.strip()
    if not auth_url:
        raise TwitchPartnerIntegrationUnavailable(
            "Deadlock-Twitch-Bot hat keinen gueltigen Auth-Link geliefert."
        )
    return auth_url


def get_auth_state(discord_user_id: int) -> TwitchPartnerAuthState:
    modules = _load_external_modules()
    manager = _auth_manager()
    discord_id = str(discord_user_id)

    try:
        with modules.get_conn() as conn:
            streamer_row = conn.execute(
                """
                SELECT twitch_login, twitch_user_id
                FROM twitch_streamers
                WHERE discord_user_id = ?
                ORDER BY manual_verified_at DESC NULLS LAST, created_at DESC NULLS LAST
                LIMIT 1
                """,
                (discord_id,),
            ).fetchone()
            if streamer_row is None:
                return TwitchPartnerAuthState(
                    twitch_login=None,
                    twitch_user_id=None,
                    authorized=False,
                )

            twitch_login = _normalize_login(_row_value(streamer_row, "twitch_login", 0)) or None
            twitch_user_id = str(_row_value(streamer_row, "twitch_user_id", 1) or "").strip() or None

            authorized = bool(twitch_user_id and manager.has_enabled_auth(twitch_user_id))
            if not authorized and twitch_login:
                auth_row = conn.execute(
                    """
                    SELECT twitch_user_id, raid_enabled, authorized_at
                    FROM twitch_raid_auth
                    WHERE LOWER(twitch_login) = LOWER(?)
                    LIMIT 1
                    """,
                    (twitch_login,),
                ).fetchone()
                if auth_row is not None:
                    if not twitch_user_id:
                        twitch_user_id = (
                            str(_row_value(auth_row, "twitch_user_id", 0) or "").strip() or None
                        )
                    raid_enabled = _normalize_bool(_row_value(auth_row, "raid_enabled", 1))
                    authorized_at = str(_row_value(auth_row, "authorized_at", 2) or "").strip()
                    authorized = raid_enabled or bool(authorized_at)

            return TwitchPartnerAuthState(
                twitch_login=twitch_login,
                twitch_user_id=twitch_user_id,
                authorized=authorized,
            )
    except Exception as exc:
        raise TwitchPartnerIntegrationUnavailable(
            "Autorisierungsstatus aus Deadlock-Twitch-Bot konnte nicht gelesen werden."
        ) from exc


def check_onboarding_blocklist(
    *,
    discord_user_id: int | None = None,
    twitch_login: str | None = None,
) -> tuple[bool, str | None]:
    modules = _load_external_modules()
    normalized_login = _normalize_login(twitch_login)
    discord_id = str(discord_user_id) if discord_user_id is not None else ""
    candidate_logins: set[str] = set()
    candidate_user_ids: set[str] = set()
    if normalized_login:
        candidate_logins.add(normalized_login)

    try:
        with modules.get_conn() as conn:
            if normalized_login:
                login_row = conn.execute(
                    """
                    SELECT twitch_login, twitch_user_id, manual_partner_opt_out
                    FROM twitch_streamers
                    WHERE LOWER(twitch_login) = LOWER(?)
                    LIMIT 1
                    """,
                    (normalized_login,),
                ).fetchone()
                if login_row is not None:
                    login_value = _normalize_login(_row_value(login_row, "twitch_login", 0))
                    user_id_value = str(_row_value(login_row, "twitch_user_id", 1) or "").strip()
                    if login_value:
                        candidate_logins.add(login_value)
                    if user_id_value:
                        candidate_user_ids.add(user_id_value)
                    if _normalize_bool(_row_value(login_row, "manual_partner_opt_out", 2)):
                        return True, f"manual_partner_opt_out=1 fuer {login_value or normalized_login}"

            if discord_id:
                rows = conn.execute(
                    """
                    SELECT twitch_login, twitch_user_id, manual_partner_opt_out
                    FROM twitch_streamers
                    WHERE discord_user_id = ?
                    """,
                    (discord_id,),
                ).fetchall()
                for row in rows:
                    login_value = _normalize_login(_row_value(row, "twitch_login", 0))
                    user_id_value = str(_row_value(row, "twitch_user_id", 1) or "").strip()
                    if login_value:
                        candidate_logins.add(login_value)
                    if user_id_value:
                        candidate_user_ids.add(user_id_value)
                    if _normalize_bool(_row_value(row, "manual_partner_opt_out", 2)):
                        blocked_login = login_value or normalized_login or "unbekannt"
                        return True, f"manual_partner_opt_out=1 fuer {blocked_login}"

            for twitch_user_id in sorted(candidate_user_ids):
                blacklist_row = conn.execute(
                    """
                    SELECT error_count
                    FROM twitch_token_blacklist
                    WHERE twitch_user_id = ?
                    LIMIT 1
                    """,
                    (twitch_user_id,),
                ).fetchone()
                if blacklist_row is None:
                    continue
                error_count = _row_value(blacklist_row, "error_count", 0)
                try:
                    parsed_error_count = int(error_count or 0)
                except Exception:
                    parsed_error_count = _BLOCKING_TOKEN_BLACKLIST_THRESHOLD
                if parsed_error_count >= _BLOCKING_TOKEN_BLACKLIST_THRESHOLD:
                    return True, f"twitch_token_blacklist fuer {twitch_user_id}"

            for login_value in sorted(candidate_logins):
                legacy_row = conn.execute(
                    """
                    SELECT reason
                    FROM twitch_raid_blacklist
                    WHERE LOWER(target_login) = LOWER(?)
                    LIMIT 1
                    """,
                    (login_value,),
                ).fetchone()
                if legacy_row is None:
                    continue
                reason = str(_row_value(legacy_row, "reason", 0) or "").strip() or "kein Grund"
                return True, f"twitch_raid_blacklist fuer {login_value} ({reason})"
    except Exception as exc:
        raise TwitchPartnerIntegrationUnavailable(
            "Opt-out-/Blacklist-Status aus Deadlock-Twitch-Bot konnte nicht gelesen werden."
        ) from exc

    return False, None


__all__ = [
    "TwitchPartnerAuthState",
    "TwitchPartnerIntegrationUnavailable",
    "check_onboarding_blocklist",
    "generate_discord_auth_url",
    "get_auth_state",
]
