"""Compatibility wrapper for legacy imports of Steam friend-request helpers.

The actual implementation lives in :mod:`cogs.steam.friend_requests`. This module
simply re-exports the public helpers so existing ``service.*`` imports keep
working while the single source of truth remains within the cogs package.
"""

from __future__ import annotations

from cogs.steam.friend_requests import (
    queue_friend_request,
    queue_friend_requests,
)

__all__ = ["queue_friend_request", "queue_friend_requests"]
