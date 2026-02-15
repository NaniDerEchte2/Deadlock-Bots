# =========================================
# cogs/twitch/__init__.py
# =========================================
"""Package entry point for the Twitch stream monitor cog."""

import logging
from typing import Optional

from discord.ext import commands



log = logging.getLogger("TwitchStreams")


async def setup(bot: commands.Bot):
    """Add the Twitch stream cog to the master bot, and register the !twl proxy command exactly once."""
    from .cog import TwitchStreamCog  # Local import to avoid self-import warnings during extension discovery

    # 1) Stale/alte Command-Objekte vorab entfernen
    existing = bot.get_command("twl")
    if existing is not None:
        bot.remove_command(existing.name)
        log.info("Removed pre-existing !twl command before adding Twitch cog")

    # 2) Cog hinzufügen – vorher prüfen ob bereits geladen (z.B. nach failedReload)
    existing_cog = bot.get_cog("TwitchStreamCog")
    if existing_cog is not None:
        log.warning("TwitchStreamCog ist bereits geladen – wird zuerst entfernt")
        await bot.remove_cog("TwitchStreamCog")

    cog = TwitchStreamCog(bot)
    await bot.add_cog(cog)

    # 3) Dünner Prefix-Proxy (!twl) → ruft IMMER die Cog-Methode auf (keine Doppel-Registrierung)
    async def _twl_proxy(ctx: commands.Context, *, filters: str = ""):
        active_cog: Optional[TwitchStreamCog] = bot.get_cog(cog.__cog_name__)  # type: ignore[assignment]
        if not isinstance(active_cog, TwitchStreamCog):
            await ctx.reply("Twitch-Statistiken sind derzeit nicht verfügbar.")
            return

        leaderboard_cb = getattr(active_cog, "twitch_leaderboard", None)
        if not callable(leaderboard_cb):
            await ctx.reply("Twitch-Statistiken sind derzeit nicht verfügbar.")
            log.error("twitch_leaderboard callable missing on active cog")
            return

        # Einheitlicher Call: wir geben Context + keyword-only 'filters' weiter
        try:
            await leaderboard_cb(ctx, filters=filters)
        except TypeError as e:
            # Fallbacks, falls ältere Signaturen aktiv sind
            log.warning("Signature mismatch when calling twitch_leaderboard: %s", e, exc_info=True)
            try:
                await leaderboard_cb(ctx)
            except TypeError:
                await ctx.reply("Twitch-Statistiken konnten nicht geladen werden (Kompatibilitätsproblem).")

    prefix_command = commands.Command(
        _twl_proxy,
        name="twl",
        help="Zeigt Twitch-Statistiken (Leaderboard) im Partner-Kanal an. Nutzung: !twl [samples=N] [avg=N] [partner=only|exclude|any] [limit=N]",
    )

    # Command bewusst NUR HIER registrieren (keine Decorators im Cog)
    bot.add_command(prefix_command)
    cog.set_prefix_command(prefix_command)
    log.debug("Registered !twl prefix command via setup hook")
