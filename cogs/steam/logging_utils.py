"""Utilities for safely logging user-controlled data."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_CONTROL_REPLACEMENTS = {
    ord("\r"): "\\r",
    ord("\n"): "\\n",
}


def _sanitize_str(value: str) -> str:
    sanitized_chars = []
    for ch in value:
        code_point = ord(ch)
        if code_point in _CONTROL_REPLACEMENTS:
            sanitized_chars.append(_CONTROL_REPLACEMENTS[code_point])
        elif code_point < 32:
            sanitized_chars.append("?")
        else:
            sanitized_chars.append(ch)
    return "".join(sanitized_chars)


def sanitize_log_value(value: Any) -> Any:
    """Recursively sanitise values for safe logging."""
    if isinstance(value, str):
        return _sanitize_str(value)
    if isinstance(value, list):
        return [sanitize_log_value(v) for v in value]
    if isinstance(value, tuple):
        return tuple(sanitize_log_value(v) for v in value)
    if isinstance(value, set):
        return {sanitize_log_value(v) for v in value}
    if isinstance(value, dict):
        return {
            _sanitize_str(k) if isinstance(k, str) else k: sanitize_log_value(v)
            for k, v in value.items()
        }
    return value


def safe_log_extra(data: Mapping[str, Any]) -> dict:
    """Return a dict that is safe to use with logging's ``extra`` parameter."""
    return {_sanitize_str(str(key)): sanitize_log_value(value) for key, value in data.items()}


__all__ = ["sanitize_log_value", "safe_log_extra"]
