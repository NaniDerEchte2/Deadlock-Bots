"""
Helpers for creating resilient aiohttp connectors/sessions.

Motivation: We observed occasional DNS timeouts (aiodns/pycares) when the
bot connects to Discord or external APIs (Twitch). Using a connector with
explicit public DNS fallbacks and IPv4 preference improves the likelihood
of successful resolution on flaky networks.
"""

from __future__ import annotations

import logging
import os
import socket
from collections.abc import Iterable, Sequence

import aiohttp
from aiohttp import resolver as aiohttp_resolver

_log = logging.getLogger("http_client")

# Public resolvers used as fallback when system DNS is slow/unreliable.
_DEFAULT_DNS_SERVERS: Sequence[str] = ("1.1.1.1", "8.8.8.8", "9.9.9.9")


def _parse_env_dns() -> list[str]:
    """Parse comma/semicolon separated DNS servers from env, if provided."""
    raw = os.getenv("HTTP_DNS_SERVERS") or os.getenv("PREFERRED_DNS") or ""
    if not raw:
        return []
    parts = raw.replace(";", ",").split(",")
    return [p.strip() for p in parts if p.strip()]


def build_resilient_connector(
    *,
    dns_servers: Iterable[str] | None = None,
    ttl_dns_cache: int = 300,
    family: socket.AddressFamily = socket.AF_INET,
    limit: int = 500,
    limit_per_host: int = 0,
) -> aiohttp.TCPConnector:
    """
    Create a TCPConnector with sane DNS fallbacks and IPv4 preference.

    - Uses public resolvers unless custom ones are provided via env or args
    - Caches DNS results for ``ttl_dns_cache`` seconds to avoid repeated lookups
    - Prefers IPv4 (common cause of timeouts on some consumer networks)
    """
    nameservers = list(dns_servers or []) or _parse_env_dns() or list(_DEFAULT_DNS_SERVERS)
    resolver = None

    # Prefer async resolver with explicit nameservers; gracefully fall back.
    try:
        resolver = aiohttp_resolver.AsyncResolver(nameservers=nameservers, rotate=True)
    except Exception as exc:  # pragma: no cover - defensive fallback
        resolver = None
        _log.warning(
            "Falling back to default DNS resolver (nameservers=%s): %s",
            nameservers,
            exc,
        )

    try:
        return aiohttp.TCPConnector(
            resolver=resolver,
            ttl_dns_cache=max(0, ttl_dns_cache),
            family=family,
            limit=limit,
            limit_per_host=limit_per_host,
            enable_cleanup_closed=True,
        )
    except Exception as exc:  # pragma: no cover - defensive fallback
        _log.warning("TCPConnector init failed, using aiohttp defaults: %s", exc)
        return aiohttp.TCPConnector(enable_cleanup_closed=True)
