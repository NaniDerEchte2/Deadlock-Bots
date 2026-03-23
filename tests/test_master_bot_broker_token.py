from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bot_core.master_bot import MasterBot


class MasterBotBrokerTokenTests(unittest.TestCase):
    def test_master_broker_token_accepts_shared_internal_api_token(self) -> None:
        with patch.dict(
            os.environ,
            {
                "MASTER_BROKER_TOKEN": "",
                "MAIN_BOT_INTERNAL_TOKEN": "",
                "TWITCH_INTERNAL_API_TOKEN": "shared-internal-token",
            },
            clear=False,
        ):
            self.assertEqual(MasterBot._master_broker_token(), "shared-internal-token")


class MasterBotLiveBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_master_broker_view_resolver_loads_live_bridge_extension(self) -> None:
        loaded_extensions: list[str] = []

        async def _load_extension(name: str) -> None:
            loaded_extensions.append(name)
            setattr(fake_bot, "resolve_master_broker_view_spec", lambda spec: spec)

        fake_bot = SimpleNamespace(
            runtime_mode=SimpleNamespace(role="master"),
            master_broker=object(),
            extensions={},
            cogs_list=["cogs.twitch.live_bridge", "cogs.misc"],
            cog_status={},
            load_extension=_load_extension,
        )

        loaded = await MasterBot._ensure_master_broker_view_resolver(fake_bot)  # type: ignore[arg-type]

        self.assertTrue(loaded)
        self.assertEqual(loaded_extensions, ["cogs.twitch.live_bridge"])
        self.assertEqual(fake_bot.cogs_list, ["cogs.misc"])
        self.assertEqual(fake_bot.cog_status["cogs.twitch.live_bridge"], "loaded")
        self.assertTrue(callable(fake_bot.resolve_master_broker_view_spec))


if __name__ == "__main__":
    unittest.main()
