from __future__ import annotations

import logging
import os
from dataclasses import dataclass

_TRUE_VALUES = {"1", "true", "yes", "y", "on"}
_FALSE_VALUES = {"0", "false", "no", "n", "off"}
_ALLOWED_RUNTIME_ROLES = {"master", "twitch_worker", "dashboard"}
_LEGACY_ROLE_MAP = {
    "bot": "twitch_worker",
    "dashboard": "dashboard",
}


def _parse_bool(raw: str | None, *, default: bool, env_name: str) -> bool:
    if raw is None:
        return default

    normalized = raw.strip().lower()
    if not normalized:
        return default
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False

    logging.warning("Invalid %s=%r, using default=%s", env_name, raw, default)
    return default


def _legacy_runtime_role() -> str:
    raw_role = (os.getenv("TWITCH_SPLIT_RUNTIME_ROLE") or "").strip().lower()
    mapped_role = _LEGACY_ROLE_MAP.get(raw_role, "")
    if not mapped_role:
        return ""

    enforced = _parse_bool(
        os.getenv("TWITCH_SPLIT_RUNTIME_ENFORCE"),
        default=False,
        env_name="TWITCH_SPLIT_RUNTIME_ENFORCE",
    )
    if not enforced:
        return ""
    return mapped_role


def resolve_runtime_role() -> str:
    raw_role = (os.getenv("RUNTIME_ROLE") or "").strip().lower()
    if raw_role:
        if raw_role in _ALLOWED_RUNTIME_ROLES:
            return raw_role
        logging.warning("Invalid RUNTIME_ROLE=%r, falling back to master", raw_role)
        return "master"

    legacy = _legacy_runtime_role()
    if legacy:
        return legacy
    return "master"


def resolve_discord_gateway_enabled(role: str) -> bool:
    default = role == "master"
    return _parse_bool(
        os.getenv("DISCORD_GATEWAY_ENABLED"),
        default=default,
        env_name="DISCORD_GATEWAY_ENABLED",
    )


@dataclass(frozen=True, slots=True)
class RuntimeMode:
    role: str
    discord_gateway_enabled: bool


def resolve_runtime_mode() -> RuntimeMode:
    role = resolve_runtime_role()
    discord_gateway_enabled = resolve_discord_gateway_enabled(role)
    return RuntimeMode(role=role, discord_gateway_enabled=discord_gateway_enabled)


def split_runtime_role_for_cogs(mode: RuntimeMode | None = None) -> str:
    active_mode = mode or resolve_runtime_mode()
    if active_mode.role == "twitch_worker":
        return "bot"
    if active_mode.role == "dashboard":
        return "dashboard"
    return ""


def ensure_gateway_start_allowed(mode: RuntimeMode | None = None) -> RuntimeMode:
    active_mode = mode or resolve_runtime_mode()
    if active_mode.discord_gateway_enabled and active_mode.role != "master":
        raise RuntimeError(
            "Invalid runtime configuration: "
            f"RUNTIME_ROLE={active_mode.role} cannot run Discord Gateway with "
            "DISCORD_GATEWAY_ENABLED=true."
        )
    return active_mode


__all__ = [
    "RuntimeMode",
    "ensure_gateway_start_allowed",
    "resolve_discord_gateway_enabled",
    "resolve_runtime_mode",
    "resolve_runtime_role",
    "split_runtime_role_for_cogs",
]

