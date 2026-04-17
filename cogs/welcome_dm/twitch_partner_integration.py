from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("StreamerOnboarding.TwitchIntegration")


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
    raid_auth_manager_cls: Any
    raid_integration_state_resolver_cls: Any
    default_redirect_uri: str


_EXTERNAL_MODULES: _ExternalModules | None = None
_AUTH_MANAGER: Any | None = None


def _normalize_login(value: object) -> str:
    return str(value or "").strip().lower()


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
        f"Deadlock-Twitch-Bot wurde nicht gefunden. Gepruefte Pfade: {searched}"
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
        from bot.raid.integration_state import RaidIntegrationStateResolver
    except Exception as exc:
        raise TwitchPartnerIntegrationUnavailable(
            "Deadlock-Twitch-Bot konnte nicht geladen werden."
        ) from exc

    _EXTERNAL_MODULES = _ExternalModules(
        repo_path=repo_path,
        raid_auth_manager_cls=RaidAuthManager,
        raid_integration_state_resolver_cls=RaidIntegrationStateResolver,
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
    auth_url = str(
        _auth_manager().generate_discord_button_url(
            f"discord:{discord_user_id}",
            discord_user_id=discord_user_id,
        )
        or ""
    )
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
        resolver = modules.raid_integration_state_resolver_cls(
            auth_manager=manager,
            token_error_handler=getattr(manager, "token_error_handler", None),
        )
        state = resolver.resolve_auth_state(discord_id)
        return TwitchPartnerAuthState(
            twitch_login=_normalize_login(state.twitch_login) or None,
            twitch_user_id=str(state.twitch_user_id or "").strip() or None,
            authorized=bool(state.authorized),
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
    normalized_login = _normalize_login(twitch_login) or None
    normalized_discord_id = str(discord_user_id) if discord_user_id is not None else None

    try:
        resolver = modules.raid_integration_state_resolver_cls(
            auth_manager=_AUTH_MANAGER,
            token_error_handler=getattr(_AUTH_MANAGER, "token_error_handler", None),
        )
        state = resolver.resolve_block_state(
            discord_user_id=normalized_discord_id,
            twitch_login=normalized_login,
        )
        if state.partner_opt_out:
            blocked_login = _normalize_login(state.twitch_login) or normalized_login or "unbekannt"
            return True, f"manual_partner_opt_out=1 fuer {blocked_login}"
        if state.token_blacklisted:
            blocked_user_id = str(state.twitch_user_id or "").strip() or "unbekannt"
            return True, f"twitch_token_blacklist fuer {blocked_user_id}"
        if state.raid_blacklisted:
            blocked_login = _normalize_login(state.twitch_login) or normalized_login or "unbekannt"
            return True, f"twitch_raid_blacklist fuer {blocked_login}"
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
