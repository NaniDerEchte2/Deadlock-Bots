import logging

try:
    from twitchio import eventsub
    from twitchio import web as twitchio_web
    from twitchio.ext import commands as twitchio_commands

    _ = (eventsub, twitchio_web, twitchio_commands)

    TWITCHIO_AVAILABLE = True
except ImportError:
    TWITCHIO_AVAILABLE = False
    eventsub = None
    twitchio_web = None
    twitchio_commands = None
    log = logging.getLogger("TwitchStreams.ChatBot")
    log.warning(
        "twitchio nicht installiert. Twitch Chat Bot wird nicht verfügbar sein. "
        "Installation: pip install twitchio"
    )

__all__ = ("TWITCHIO_AVAILABLE", "eventsub", "twitchio_web", "twitchio_commands")
