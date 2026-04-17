from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from cogs.deadlock_voice_status import DeadlockVoiceStatus


class _DummyBot:
    def __init__(self) -> None:
        self.queued_renames: list[tuple[int, str, str]] = []

    async def queue_channel_rename(self, channel_id: int, new_name: str, reason: str) -> None:
        self.queued_renames.append((channel_id, new_name, reason))


class _DummyChannel:
    def __init__(self, channel_id: int, name: str) -> None:
        self.id = channel_id
        self.name = name


class DeadlockVoiceStatusRenameTests(unittest.IsolatedAsyncioTestCase):
    async def test_match_over_45_minutes_still_queues_rename_after_cooldown(self) -> None:
        bot = _DummyBot()
        channel = _DummyChannel(123, "Archon 2")

        with patch.dict(os.environ, {"DEADLOCK_VS_TRACE": "0"}, clear=False):
            cog = DeadlockVoiceStatus(bot)

        await cog._apply_channel_name(
            channel,
            "Archon 2",
            "im Match Min 93 (2/6)",
            "match",
            "93",
            None,
            2,
            6,
            90,
            "server-1",
            debug_payload={},
        )

        self.assertEqual(
            bot.queued_renames,
            [(123, "Archon 2 - im Match Min 93 (2/6)", "Deadlock Voice Status Update")],
        )
        rename_trace = cog.last_observation[123]["rename"]
        self.assertEqual(rename_trace["result"], "queued")
        self.assertEqual(rename_trace["effective_cooldown"], 600)


if __name__ == "__main__":
    unittest.main()
