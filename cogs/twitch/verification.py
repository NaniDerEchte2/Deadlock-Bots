import re
import logging
import aiohttp
from typing import List, Optional, Set
from .twitch_api import TwitchAPI

log = logging.getLogger("TwitchVerification")

# Regex for Discord invites
DISCORD_INVITE_REGEX = re.compile(
    r"(?:https?://)?(?:www\.)?(?:discord\.(?:gg|io|me|li)|discordapp\.com/invite)/([a-zA-Z0-9-]+)",
    re.IGNORECASE
)

async def check_streamer_verification(
    api: TwitchAPI,
    twitch_login: str,
    valid_invite_codes: Set[str]
) -> tuple[bool, str]:
    """
    Prüft automatisiert, ob ein Streamer den Discord-Link hinterlegt hat.
    
    Returns: (success, reason)
    """
    twitch_login = twitch_login.lower().strip()
    
    # 1. Bio (Description) via API prüfen
    try:
        user_info = await api.get_user_info(twitch_login)
        if user_info:
            description = user_info.get("description", "")
            found_codes = DISCORD_INVITE_REGEX.findall(description)
            for code in found_codes:
                if code in valid_invite_codes:
                    return True, f"Discord-Link in der Twitch-Bio gefunden (Code: {code})"
    except Exception as e:
        log.warning("Fehler beim Abrufen der Twitch-Bio für %s: %s", twitch_login, e)

    # 2. Öffentliche Seite prüfen (Social Links / Panels)
    # Da Social Links nicht in der Helix API sind, laden wir die Seite und suchen im HTML.
    # Hinweis: Twitch ist eine SPA, aber einige Links sind oft im initialen HTML oder in JSON-Blobs.
    try:
        session = api.get_http_session()
        url = f"https://www.twitch.tv/{twitch_login}/about"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                html = await resp.text()
                
                # Suche nach Invite-Codes im HTML
                found_codes = DISCORD_INVITE_REGEX.findall(html)
                for code in found_codes:
                    if code in valid_invite_codes:
                        return True, f"Discord-Link auf der Twitch-Profilseite gefunden (Code: {code})"
            else:
                log.debug("Twitch-Profilseite für %s nicht erreichbar: %s", twitch_login, resp.status)
    except Exception as e:
        log.warning("Fehler beim Scrapen der Twitch-Seite für %s: %s", twitch_login, e)

    return False, "Kein gültiger Discord-Link in Bio oder auf der Profilseite gefunden."
