# =========================================
# cogs/twitch/__init__.py
# =========================================
"""Package entry point for the Twitch stream monitor cog."""

import inspect
import logging
from functools import wraps
from typing import Optional

from discord.ext import commands

from .cog import TwitchStreamCog

log = logging.getLogger("TwitchStreams")


async def setup(bot):
    """Add the Twitch stream cog to the master bot."""

    existing = bot.get_command("twl")
    if existing is not None:
        # Entfernt alte/stale Command-Objekte (z. B. nach fehlgeschlagenem Reload),
        # bevor das Cog hinzugefügt wird – sonst schlägt discord.py mit
        # CommandRegistrationError fehl.
        bot.remove_command(existing.name)
        log.info("Removed pre-existing !twl command before adding Twitch cog")

    cog = TwitchStreamCog(bot)
    await bot.add_cog(cog)

    @wraps(cog.twitch_leaderboard)
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

        try:
            signature = inspect.signature(leaderboard_cb)
        except (TypeError, ValueError):
            params = None
        else:
            params = list(signature.parameters.values())

        accepts_ctx = True
        accepts_filters = False
        if params is not None:
            accepts_ctx = any(
                p.kind
                in (
                    inspect.Parameter.POSITIONAL_ONLY,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    inspect.Parameter.VAR_POSITIONAL,
                )
                for p in params
            )
            accepts_filters = any(
                p.name == "filters"
                or p.kind in (inspect.Parameter.VAR_KEYWORD,)
                for p in params
            )

        call_args = []
        if accepts_ctx:
            call_args.append(ctx)

        call_kwargs = {}
        if accepts_filters:
            call_kwargs["filters"] = filters
        elif filters.strip():
            await ctx.reply("Diese Version des Befehls unterstützt keine Filterargumente.")
            return

        try:
            await leaderboard_cb(*call_args, **call_kwargs)
            return
        except TypeError:
            log.warning("Signature mismatch when calling twitch_leaderboard; attempting fallbacks", exc_info=True)

        # Fallback 1: force ctx positional if it wasn't passed
        if call_args != [ctx]:
            try:
                await leaderboard_cb(ctx, **call_kwargs)
                return
            except TypeError:
                pass

        # Fallback 2: try positional ctx + filters
        if filters:
            try:
                await leaderboard_cb(ctx, filters)
                return
            except TypeError:
                pass
            try:
                await leaderboard_cb(filters=filters)
                return
            except TypeError:
                pass

        # Final attempt: call without arguments
        try:
            await leaderboard_cb()
            return
        except TypeError:
            pass

        await ctx.reply("Twitch-Statistiken konnten nicht geladen werden (Kompatibilitätsproblem).")

    prefix_command = commands.Command(
        _twl_proxy,
        name="twl",
        help=cog.twitch_leaderboard.__doc__,
    )

    # Der Command gehört logisch zum Cog, wird aber bewusst außerhalb von add_cog registriert,
    # um Doppel-Registrierungen des Decorators zu vermeiden.
    set_cog = getattr(prefix_command, "_set_cog", None)
    if callable(set_cog):
        set_cog(cog)
    else:  # Fallback für discord.py-Versionen ohne _set_cog (z. B. neuere Py-Cord Builds)
        try:
            setattr(prefix_command, "cog", cog)  # type: ignore[attr-defined]
        except AttributeError:
            log.debug("Prefix command binding API unavailable; continuing without binding")
    bot.add_command(prefix_command)
    cog.set_prefix_command(prefix_command)
    log.info("Registered !twl prefix command via setup hook")
