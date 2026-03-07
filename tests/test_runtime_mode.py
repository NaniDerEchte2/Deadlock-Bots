from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from bot_core.runtime_mode import RuntimeMode, ensure_gateway_start_allowed, resolve_runtime_mode


class RuntimeModeTests(unittest.TestCase):
    def test_master_defaults_gateway_enabled(self) -> None:
        with patch.dict(
            os.environ,
            {
                "RUNTIME_ROLE": "master",
            },
            clear=True,
        ):
            mode = resolve_runtime_mode()

        self.assertEqual(mode.role, "master")
        self.assertTrue(mode.discord_gateway_enabled)

    def test_dashboard_defaults_gateway_disabled(self) -> None:
        with patch.dict(
            os.environ,
            {
                "RUNTIME_ROLE": "dashboard",
            },
            clear=True,
        ):
            mode = resolve_runtime_mode()

        self.assertEqual(mode.role, "dashboard")
        self.assertFalse(mode.discord_gateway_enabled)

    def test_dashboard_gateway_enable_is_rejected(self) -> None:
        with self.assertRaises(RuntimeError):
            ensure_gateway_start_allowed(
                RuntimeMode(role="dashboard", discord_gateway_enabled=True)
            )

    def test_invalid_runtime_role_falls_back_to_master(self) -> None:
        with patch.dict(
            os.environ,
            {
                "RUNTIME_ROLE": "sidecar",
            },
            clear=True,
        ):
            mode = resolve_runtime_mode()

        self.assertEqual(mode.role, "master")
        self.assertTrue(mode.discord_gateway_enabled)

    def test_master_gateway_enable_is_allowed(self) -> None:
        mode = ensure_gateway_start_allowed(
            RuntimeMode(role="master", discord_gateway_enabled=True)
        )

        self.assertEqual(mode.role, "master")
        self.assertTrue(mode.discord_gateway_enabled)


if __name__ == "__main__":
    unittest.main()
