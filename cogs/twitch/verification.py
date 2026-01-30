import logging
import os
import re
import secrets
from typing import Dict, List, Optional, Set, Tuple

import aiohttp

from .twitch_api import TwitchAPI

log = logging.getLogger("TwitchVerification")

# Regex for Discord invites
DISCORD_INVITE_REGEX = re.compile(
    r"(?:https?://)?(?:www\.)?(?:discord\.(?:gg|io|me|li)|discordapp\.com/invite)/([a-zA-Z0-9-]+)",
    re.IGNORECASE,
)

WEB_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"

# Minimal GraphQL query mirroring the About tab (social links + description)
ABOUT_PANEL_QUERY = """
query ChannelRoot_AboutPanel($channelLogin: String!, $skipSchedule: Boolean!, $includeIsDJ: Boolean!) {
  currentUser { id login }
  user(login: $channelLogin) {
    id
    description
    displayName
    channel {
      id
      socialMedias {
        id
        name
        title
        url
      }
      schedule @skip(if: $skipSchedule) { id }
    }
  }
}
""".strip()


def _pick_invite_code(found_codes: List[str], valid_invite_codes: Set[str]) -> Optional[Tuple[str, bool]]:
    """
    Return the first matching invite code.
    If valid_invite_codes is empty, accept the first found code (so we can still auto-detect).
    """
    if not found_codes:
        return None

    if not valid_invite_codes:
        return found_codes[0], False

    for code in found_codes:
        if code in valid_invite_codes:
            return code, True
    return None


def _match_codes(text: str, valid_invite_codes: Set[str], source: str) -> Optional[Tuple[bool, str]]:
    codes = DISCORD_INVITE_REGEX.findall(text or "")
    picked = _pick_invite_code(codes, valid_invite_codes)
    if not picked:
        return None
    code, was_exact = picked
    if valid_invite_codes and not was_exact:
        return None
    tag = "(unbestaetigt)" if not valid_invite_codes else ""
    return True, f"Discord-Link {tag} in {source} gefunden (Code: {code})"


async def _fetch_integrity_token(
    session: aiohttp.ClientSession,
    *,
    client_id: str,
    device_id: str,
    user_agent: str,
) -> Optional[str]:
    headers = {
        "Client-ID": client_id,
        "User-Agent": user_agent,
        "X-Device-Id": device_id,
        "Device-ID": device_id,
        "Origin": "https://www.twitch.tv",
        "Referer": "https://www.twitch.tv/",
    }
    try:
        async with session.post(
            "https://gql.twitch.tv/integrity",
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=8),
        ) as resp:
            if resp.status != 200:
                log.debug("integrity token request failed: %s", resp.status)
                return None
            data = await resp.json()
            return data.get("token")
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("integrity token fetch error: %s", exc)
        return None


async def _fetch_about_panel(
    api: TwitchAPI,
    twitch_login: str,
    *,
    bearer_token: Optional[str],
) -> Tuple[Optional[Dict], Optional[str]]:
    """
    Try to call the same GraphQL About query the web client uses.
    Returns (data, error_message).
    """
    session = api.get_http_session()
    client_id = getattr(api, "client_id", None) or WEB_CLIENT_ID
    device_id = secrets.token_hex(16)
    user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) TwitchBot/1.0"
    )

    integrity = await _fetch_integrity_token(
        session, client_id=client_id, device_id=device_id, user_agent=user_agent
    )

    headers = {
        "Client-ID": client_id,
        "User-Agent": user_agent,
        "X-Device-Id": device_id,
        "Device-ID": device_id,
        "Origin": "https://www.twitch.tv",
        "Referer": "https://www.twitch.tv/",
    }
    if integrity:
        headers["Client-Integrity"] = integrity
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"

    payload = {
        "operationName": "ChannelRoot_AboutPanel",
        "query": ABOUT_PANEL_QUERY,
        "variables": {
            "channelLogin": twitch_login,
            "skipSchedule": True,
            "includeIsDJ": False,
        },
    }

    try:
        async with session.post(
            "https://gql.twitch.tv/gql",
            headers=headers,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=12),
        ) as resp:
            if resp.status != 200:
                return None, f"GraphQL status {resp.status}"
            data = await resp.json()
    except Exception as exc:  # pragma: no cover - defensive
        return None, f"GraphQL request failed: {exc}"

    # Check for integrity errors explicitly
    for err in data.get("errors") or []:
        if "integrity" in (err.get("message") or "").lower():
            return None, "Integrity check blocked social links"

    return data.get("data"), None


async def _scrape_with_playwright(
    twitch_login: str,
    *,
    user_agent: str,
) -> Tuple[Optional[List[str]], Optional[str]]:
    """
    Headless fallback that renders the About page like a real browser.
    Only used when playwright is installed/enabled.
    """
    try:
        from playwright.async_api import async_playwright  # type: ignore
    except Exception as exc:  # pragma: no cover - optional dependency
        return None, f"playwright not available: {exc}"

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page(user_agent=user_agent, viewport={"width": 1400, "height": 900})
            await page.goto(f"https://www.twitch.tv/{twitch_login}/about", wait_until="networkidle", timeout=20000)
            anchors = await page.eval_on_selector_all(
                "a[href*='discord.gg'], a[href*='discordapp.com/invite'], a[href*='discord.com/invite']",
                "els => els.map(el => el.href)",
            )
            await browser.close()
            urls = [u for u in anchors if isinstance(u, str)]
            return urls, None
    except Exception as exc:  # pragma: no cover - defensive
        return None, f"playwright scrape failed: {exc}"


async def check_streamer_verification(
    api: TwitchAPI,
    twitch_login: str,
    valid_invite_codes: Set[str],
) -> tuple[bool, str]:
    """
    Prueft automatisiert, ob ein Streamer den Discord-Link hinterlegt hat.

    Returns: (success, reason)
    """
    twitch_login = twitch_login.lower().strip()

    # 1) Bio (Helix)
    try:
        user_info = await api.get_user_info(twitch_login)
        if user_info:
            description = user_info.get("description", "")
            match = _match_codes(description, valid_invite_codes, "der Twitch-Bio")
            if match:
                return match
    except Exception as exc:
        log.warning("Fehler beim Abrufen der Twitch-Bio fuer %s: %s", twitch_login, exc)

    # 2) GraphQL About-Panel (naeher am echten Web-Aufruf)
    bearer: Optional[str] = None
    try:
        if getattr(api, "_token", None):
            bearer = api._token
        else:
            try:
                await api._ensure_token()
                bearer = api._token
            except Exception as exc:  # pragma: no cover - optional
                log.debug("Bearer token unavailable for GQL check: %s", exc)

        gql_data, gql_err = await _fetch_about_panel(api, twitch_login, bearer_token=bearer)
        if gql_data and gql_data.get("user"):
            user_node = gql_data["user"]
            match = _match_codes(user_node.get("description", ""), valid_invite_codes, "der Twitch-Bio (GQL)")
            if match:
                return match

            channel = (user_node or {}).get("channel") or {}
            socials = channel.get("socialMedias") or []
            for entry in socials:
                url = (entry or {}).get("url") or ""
                match = _match_codes(url, valid_invite_codes, "den Social Links")
                if match:
                    return match

        if gql_err:
            log.debug("GraphQL social scrape failed: %s", gql_err)
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("GraphQL about scrape exception: %s", exc, exc_info=True)

    # 3) Fallback: HTML-Scrape
    try:
        session = api.get_http_session()
        url = f"https://www.twitch.tv/{twitch_login}/about"
        async with session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=12),
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) TwitchBot/1.0",
                "Accept-Language": "de,en;q=0.9",
            },
        ) as resp:
            if resp.status == 200:
                html = await resp.text()
                match = _match_codes(html, valid_invite_codes, "der Twitch-Profilseite")
                if match:
                    return match
            else:
                log.debug("Twitch-Profilseite fuer %s nicht erreichbar: %s", twitch_login, resp.status)
    except Exception as exc:
        log.warning("Fehler beim Scrapen der Twitch-Seite fuer %s: %s", twitch_login, exc)

    # 4) Optional: Headless Browser (nur wenn explizit aktiviert)
    if os.getenv("TWITCH_BROWSER_SCRAPE", "0").lower() in {"1", "true", "yes"}:
        urls, err = await _scrape_with_playwright(
            twitch_login,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) TwitchBot/1.0",
        )
        if urls:
            combined = " ".join(urls)
            match = _match_codes(combined, valid_invite_codes, "dem gerenderten Profil (Browser)")
            if match:
                return match
        if err:
            log.debug("Browser scrape skipped/failed: %s", err)

    return False, "Kein gueltiger Discord-Link in Bio oder auf der Profilseite gefunden."
