"""Shared helpers for Steam-related cogs."""

from .friend_requests import queue_friend_request, queue_friend_requests
from .schnelllink import (
    SCHNELL_LINK_CUSTOM_ID,
    SchnellLink,
    SchnellLinkButton,
    respond_with_schnelllink,
)

# Backwards compatibility with older quick-invite imports
QUICK_INVITE_CUSTOM_ID = SCHNELL_LINK_CUSTOM_ID
QuickInvite = SchnellLink
QuickInviteButton = SchnellLinkButton
respond_with_quick_invite = respond_with_schnelllink

__all__ = [
    "queue_friend_request",
    "queue_friend_requests",
    "SCHNELL_LINK_CUSTOM_ID",
    "SchnellLink",
    "SchnellLinkButton",
    "respond_with_schnelllink",
    "QUICK_INVITE_CUSTOM_ID",
    "QuickInvite",
    "QuickInviteButton",
    "respond_with_quick_invite",
]
