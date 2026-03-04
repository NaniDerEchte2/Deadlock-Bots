from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from bot_core.cog_loader import CogLoaderMixin


class _DummyLoader(CogLoaderMixin):
    def __init__(self) -> None:
        self.blocked_namespaces: set[str] = set()


class CogLoaderSplitRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.loader = _DummyLoader()

    def test_split_role_is_ignored_without_enforce_flag(self) -> None:
        with patch.dict(
            os.environ,
            {"TWITCH_SPLIT_RUNTIME_ROLE": "bot"},
            clear=True,
        ):
            self.assertFalse(self.loader._should_exclude("cogs.ai_onboarding"))
            self.assertFalse(self.loader._should_exclude("cogs.twitch"))

    def test_split_role_bot_enforced_only_allows_twitch_bridge(self) -> None:
        with patch.dict(
            os.environ,
            {
                "TWITCH_SPLIT_RUNTIME_ROLE": "bot",
                "TWITCH_SPLIT_RUNTIME_ENFORCE": "1",
            },
            clear=True,
        ):
            self.assertFalse(self.loader._should_exclude("cogs.twitch"))
            self.assertTrue(self.loader._should_exclude("cogs.ai_onboarding"))

    def test_split_role_dashboard_enforced_excludes_all_discord_cogs(self) -> None:
        with patch.dict(
            os.environ,
            {
                "TWITCH_SPLIT_RUNTIME_ROLE": "dashboard",
                "TWITCH_SPLIT_RUNTIME_ENFORCE": "true",
            },
            clear=True,
        ):
            self.assertTrue(self.loader._should_exclude("cogs.twitch"))
            self.assertTrue(self.loader._should_exclude("cogs.ai_onboarding"))

    def test_cog_exclude_env_excludes_specific_cog(self) -> None:
        with patch.dict(
            os.environ,
            {"COG_EXCLUDE": "cogs.twitch"},
            clear=True,
        ):
            self.assertTrue(self.loader._should_exclude("cogs.twitch"))
            self.assertFalse(self.loader._should_exclude("cogs.ai_onboarding"))

    def test_cog_exclude_env_supports_short_names_and_multiple_values(self) -> None:
        with patch.dict(
            os.environ,
            {"COG_EXCLUDE": "twitch, cogs.voice"},
            clear=True,
        ):
            self.assertTrue(self.loader._should_exclude("cogs.twitch"))
            self.assertTrue(self.loader._should_exclude("cogs.voice"))
            self.assertFalse(self.loader._should_exclude("cogs.ai_onboarding"))


if __name__ == "__main__":
    unittest.main()
