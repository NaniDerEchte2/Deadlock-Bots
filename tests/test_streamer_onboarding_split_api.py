import os
import unittest
from unittest.mock import patch

from cogs.welcome_dm import step_streamer


class StreamerOnboardingSplitApiTests(unittest.TestCase):
    def test_internal_api_auth_url_requires_base_url_and_token(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(step_streamer._split_internal_api_auth_url(12345))

    def test_internal_api_auth_url_normalizes_base_without_internal_suffix(self) -> None:
        with patch.dict(
            os.environ,
            {
                "TWITCH_INTERNAL_API_BASE_URL": "http://127.0.0.1:8766",
                "TWITCH_INTERNAL_API_TOKEN": "secret-token",
            },
            clear=True,
        ):
            result = step_streamer._split_internal_api_auth_url(12345)

        self.assertIsNotNone(result)
        assert result is not None
        url, headers = result
        self.assertEqual(
            url,
            "http://127.0.0.1:8766/internal/twitch/v1/raid/auth-url?login=discord%3A12345",
        )
        self.assertEqual(headers, {"X-Internal-Token": "secret-token"})

    def test_internal_api_auth_url_strips_existing_internal_base_path(self) -> None:
        with patch.dict(
            os.environ,
            {
                "TWITCH_INTERNAL_API_BASE_URL": "https://api.example/internal/twitch/v1",
                "TWITCH_INTERNAL_API_TOKEN": "secret-token",
            },
            clear=True,
        ):
            result = step_streamer._split_internal_api_auth_url(999)

        self.assertIsNotNone(result)
        assert result is not None
        url, headers = result
        self.assertEqual(
            url,
            "https://api.example/internal/twitch/v1/raid/auth-url?login=discord%3A999",
        )
        self.assertEqual(headers, {"X-Internal-Token": "secret-token"})

    def test_prefer_split_internal_api_when_configured_and_role_not_bot(self) -> None:
        with patch.dict(
            os.environ,
            {
                "TWITCH_INTERNAL_API_BASE_URL": "http://127.0.0.1:8766",
                "TWITCH_INTERNAL_API_TOKEN": "secret-token",
                "TWITCH_SPLIT_RUNTIME_ROLE": "dashboard",
                "TWITCH_SPLIT_RUNTIME_ENFORCE": "1",
            },
            clear=True,
        ):
            self.assertTrue(step_streamer._prefer_split_internal_raid_auth_api())

    def test_prefer_split_internal_api_disabled_for_enforced_bot_role(self) -> None:
        with patch.dict(
            os.environ,
            {
                "TWITCH_INTERNAL_API_BASE_URL": "http://127.0.0.1:8766",
                "TWITCH_INTERNAL_API_TOKEN": "secret-token",
                "TWITCH_SPLIT_RUNTIME_ROLE": "bot",
                "TWITCH_SPLIT_RUNTIME_ENFORCE": "1",
            },
            clear=True,
        ):
            self.assertFalse(step_streamer._prefer_split_internal_raid_auth_api())


if __name__ == "__main__":
    unittest.main()
