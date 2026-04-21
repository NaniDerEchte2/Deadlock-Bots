from __future__ import annotations

import asyncio
import importlib
import sys
import types
import unittest
from importlib.util import find_spec


class _Settings(types.SimpleNamespace):
    def __getattr__(self, _name: str) -> object:
        return None


class _FakeResponse:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []
        self.modal = None

    async def send_message(self, content: str, *, ephemeral: bool = False) -> None:
        self.messages.append({"content": content, "ephemeral": ephemeral})

    async def send_modal(self, modal: object) -> None:
        self.modal = modal


class _FakeClient:
    def __init__(self, steam_link_cog: object | None = None) -> None:
        self._steam_link_cog = steam_link_cog

    def get_cog(self, name: str) -> object | None:
        if name == "SteamLink":
            return self._steam_link_cog
        return None


class _FakeInteraction:
    def __init__(self, *, user_id: int = 123456789, steam_link_cog: object | None = None) -> None:
        self.user = types.SimpleNamespace(id=user_id)
        self.client = _FakeClient(steam_link_cog=steam_link_cog)
        self.response = _FakeResponse()


@unittest.skipUnless(find_spec("discord") is not None, "discord.py is required for this integration test")
class OnboardingAccountLinkViewTest(unittest.TestCase):
    def setUp(self) -> None:
        self._original_modules: dict[str, object] = {}
        for name in (
            "service",
            "service.config",
            "cogs.steam",
            "cogs.steam.steam_link_oauth",
            "cogs.onboarding",
        ):
            self._original_modules[name] = sys.modules.get(name)

        service_pkg = types.ModuleType("service")
        config_mod = types.ModuleType("service.config")
        config_mod.settings = _Settings(
            guild_id=1,
            verified_role_id=2,
            content_creator_role_id=3,
            public_base_url="https://legacy.example.test",
        )
        service_pkg.config = config_mod
        sys.modules["service"] = service_pkg
        sys.modules["service.config"] = config_mod

        steam_pkg = types.ModuleType("cogs.steam")
        oauth_mod = types.ModuleType("cogs.steam.steam_link_oauth")
        oauth_mod.FRIEND_CODE_LINKING_ENABLED = True
        oauth_mod.start_urls_for = lambda uid: {
            "discord_start": f"https://link.example.test/discord/login?uid={int(uid)}",
            "steam_openid_start": f"https://link.example.test/steam/login?launch=signed-{int(uid)}",
        }

        class _FakeSteamFriendCodeModal:
            def __init__(self, *, user_id: int, link_cog: object) -> None:
                self.user_id = user_id
                self.link_cog = link_cog

        oauth_mod.SteamFriendCodeModal = _FakeSteamFriendCodeModal
        steam_pkg.steam_link_oauth = oauth_mod
        sys.modules["cogs.steam"] = steam_pkg
        sys.modules["cogs.steam.steam_link_oauth"] = oauth_mod

        sys.modules.pop("cogs.onboarding", None)
        self.mod = importlib.import_module("cogs.onboarding")

    def tearDown(self) -> None:
        sys.modules.pop("cogs.onboarding", None)
        for name, original in self._original_modules.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original

    def test_onboarding_uses_delegated_steam_launch_url(self) -> None:
        view = self.mod.OnboardingAccountLinkView(cog=object(), step_index=7, user_id=123456789)
        button = next(child for child in view.children if getattr(child, "url", None))

        self.assertEqual(
            button.url,
            "https://link.example.test/steam/login?launch=signed-123456789",
        )
        self.assertNotIn("uid=", str(button.url))

    def test_onboarding_adds_friend_code_button_when_supported(self) -> None:
        view = self.mod.OnboardingAccountLinkView(cog=object(), step_index=7, user_id=123456789)

        labels = [getattr(child, "label", "") for child in view.children]
        self.assertIn("Freundescode", labels)

    def test_friend_code_button_uses_existing_steam_link_cog(self) -> None:
        async def _run() -> None:
            view = self.mod.OnboardingAccountLinkView(cog=object(), step_index=7, user_id=123456789)
            friend_button = next(
                child for child in view.children if getattr(child, "label", "") == "Freundescode"
            )
            steam_link_cog = object()
            interaction = _FakeInteraction(steam_link_cog=steam_link_cog)

            await friend_button.callback(interaction)

            self.assertIsNotNone(interaction.response.modal)
            self.assertEqual(interaction.response.modal.user_id, 123456789)
            self.assertIs(interaction.response.modal.link_cog, steam_link_cog)

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main(verbosity=2)
