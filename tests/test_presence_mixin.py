from __future__ import annotations

import unittest

from discord.ext import commands

from bot_core.presence import PresenceMixin


class _PlainVoiceCog(commands.Cog):
    def __init__(self) -> None:
        self.calls = 0

    async def on_voice_state_update(self, member, before, after) -> None:
        self.calls += 1


class _ListenerVoiceCog(commands.Cog):
    def __init__(self) -> None:
        self.calls = 0

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after) -> None:
        self.calls += 1


class _PresenceTestBot(PresenceMixin):
    def __init__(self, *cogs: commands.Cog) -> None:
        self.cogs = {type(cog).__name__: cog for cog in cogs}
        self.extensions = {}


class PresenceMixinVoiceRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_router_skips_cog_listeners_already_registered_with_discord(self) -> None:
        plain = _PlainVoiceCog()
        listener = _ListenerVoiceCog()
        bot = _PresenceTestBot(plain, listener)

        await bot.on_voice_state_update(object(), object(), object())

        self.assertEqual(plain.calls, 1)
        self.assertEqual(listener.calls, 0)


if __name__ == "__main__":
    unittest.main()
