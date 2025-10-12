from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import discord
from discord.ext import commands

from .bot_service import FriendPresence, GuardCodeManager, SteamBotConfig, SteamBotService

log = logging.getLogger("SteamBotCog")

DEFAULT_CHANNEL_ID = 1374364800817303632
DEFAULT_REFRESH_TOKEN_PATH = Path(__file__).resolve().parent / ".steam-data" / "refresh.token"


async def _announce_setup_failure(bot: commands.Bot, channel_id: int, reason: str) -> None:
    """Post a setup failure message into the configured status channel."""

    await bot.wait_until_ready()
    channel = bot.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        try:
            fetched = await bot.fetch_channel(channel_id)
        except discord.HTTPException:
            log.warning("Unable to announce Steam bot failure – channel %s not found", channel_id)
            return
        if not isinstance(fetched, discord.TextChannel):
            log.warning("Steam bot status channel %s is not a text channel", channel_id)
            return
        channel = fetched
    try:
        await channel.send(
            "⚠️ Steam-Bot konnte nicht gestartet werden: "
            f"{reason}\nBitte stelle sicher, dass die Steam-Abhängigkeiten installiert sind."
        )
    except discord.HTTPException:
        log.exception("Failed to post Steam bot setup failure message")


def _env_optional(key: str) -> Optional[str]:
    value = os.getenv(key)
    if value:
        value = value.strip()
        if value:
            return value
    return None


def _env_first(*keys: str) -> Optional[str]:
    for key in keys:
        value = _env_optional(key)
        if value:
            return value
    return None


class SteamBotCog(commands.Cog):
    """Discord integration for the Steam bot service."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.guard_codes = GuardCodeManager()
        self.config = self._build_config()
        self.channel_id = self.resolve_channel_id_from_env()
        self.service = SteamBotService(self.config, self.guard_codes)
        self.service.register_status_callback(self._handle_status_update)
        self.service.register_connection_callback(self._handle_connection_change)
        self._status_message_id: Optional[int] = None
        self._status_lock = asyncio.Lock()
        self._last_status_payload: Optional[str] = None
        self._online_announced = False

    # ------------------------------------------------------------------
    # Cog lifecycle
    # ------------------------------------------------------------------
    async def cog_load(self) -> None:  # pragma: no cover - lifecycle hook
        await self.service.start()
        log.info("Steam bot service started")

    async def cog_unload(self) -> None:  # pragma: no cover - lifecycle hook
        await self.service.stop()
        log.info("Steam bot service stopped")

    # ------------------------------------------------------------------
    # Status updates
    # ------------------------------------------------------------------
    async def _handle_status_update(self, snapshot: List[FriendPresence]) -> None:
        if not self.bot.is_ready():  # pragma: no cover - runtime guard
            return
        channel = await self._fetch_channel()
        if channel is None:
            log.warning("Steam status channel %s not found", self.channel_id)
            return

        payload = self._format_status_message(snapshot)
        async with self._status_lock:
            if self._last_status_payload == payload:
                return
            self._last_status_payload = payload
            try:
                if self._status_message_id:
                    message = await channel.fetch_message(self._status_message_id)
                    await message.edit(content=payload)
                else:
                    message = await channel.send(payload)
                    self._status_message_id = message.id
            except discord.NotFound:
                message = await channel.send(payload)
                self._status_message_id = message.id
            except discord.HTTPException as exc:
                log.exception("Failed to publish Steam status update: %s", exc)

    async def _handle_connection_change(self, online: bool) -> None:
        if online:
            await self.bot.wait_until_ready()
            if self._online_announced:
                return
            channel = await self._fetch_channel()
            if channel is None:
                log.warning("Steam status channel %s not found", self.channel_id)
                return
            try:
                await channel.send("✅ Steam-Bot ist online.")
                self._online_announced = True
            except discord.HTTPException as exc:
                log.exception("Failed to announce Steam bot availability: %s", exc)
        else:
            self._online_announced = False

    def _format_status_message(self, snapshot: List[FriendPresence]) -> str:
        timestamp = datetime.now(tz=timezone.utc).strftime("%H:%M:%S UTC")
        if not snapshot:
            return (
                f"🛡️ **Steam Deadlock-Status ({timestamp})**\n"
                "Aktuell spielt niemand Deadlock."
            )

        lines = [f"⚔️ **Steam Deadlock-Status ({timestamp})**", ""]
        for presence in snapshot:
            profile_url = f"https://steamcommunity.com/profiles/{presence.steam_id}"
            persona = presence.persona_state or "Unknown"
            detail = presence.rich_presence_text
            if presence.app_name and presence.app_name.lower() != "deadlock":
                detail = f"{presence.app_name} – {detail}" if detail else presence.app_name
            if detail:
                lines.append(f"• [{presence.name}]({profile_url}) — {persona} — {detail}")
            else:
                lines.append(f"• [{presence.name}]({profile_url}) — {persona}")
        payload = "\n".join(lines)
        if len(payload) > 1800:
            payload = payload[:1797] + "…"
        return payload

    async def _fetch_channel(self) -> Optional[discord.TextChannel]:
        channel = self.bot.get_channel(self.channel_id)
        if isinstance(channel, discord.TextChannel):
            return channel
        try:
            fetched = await self.bot.fetch_channel(self.channel_id)
        except discord.HTTPException:
            return None
        return fetched if isinstance(fetched, discord.TextChannel) else None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def submit_guard_code(self, code: str) -> bool:
        """Expose guard-code submission for external cogs."""

        return self.guard_codes.submit(code)

    def _build_config(self) -> SteamBotConfig:
        username = _env_first(
            "STEAM_ACCOUNT_USERNAME",
            "STEAM_USERNAME",
            "STEAM_ACCOUNT",
            "STEAM_BOT_USERNAME",
        )
        password = _env_first(
            "STEAM_ACCOUNT_PASSWORD",
            "STEAM_PASSWORD",
            "STEAM_BOT_PASSWORD",
        )
        refresh_token_path_env = _env_first(
            "STEAM_REFRESH_TOKEN_PATH",
            "STEAM_REFRESH_TOKEN_FILE",
            "STEAM_BOT_REFRESH_TOKEN_PATH",
            "STEAM_BOT_REFRESH_TOKEN_FILE",
        )
        if refresh_token_path_env:
            refresh_token_path = Path(os.path.expanduser(os.path.expandvars(refresh_token_path_env)))
        else:
            refresh_token_path = DEFAULT_REFRESH_TOKEN_PATH

        refresh_token = _env_first(
            "STEAM_REFRESH_TOKEN",
            "STEAM_TOKEN",
            "STEAM_BOT_REFRESH_TOKEN",
        )

        if not username:
            username = _env_first(
                "STEAM_ACCOUNT_NAME",
                "STEAM_USERNAME",
                "STEAM_ACCOUNT_USERNAME",
                "STEAM_ACCOUNT",
            )

        if not username and not password:
            has_refresh_candidate = False
            if refresh_token and refresh_token.strip():
                has_refresh_candidate = True
            else:
                try:
                    has_refresh_candidate = refresh_token_path.is_file()
                except OSError:
                    has_refresh_candidate = False
            if not has_refresh_candidate:
                raise RuntimeError(
                    "Steam credentials missing (STEAM_ACCOUNT_USERNAME/STEAM_USERNAME and "
                    "STEAM_ACCOUNT_PASSWORD/STEAM_PASSWORD) and no refresh token available"
                )

        account_name = _env_first(
            "STEAM_ACCOUNT_NAME",
            "STEAM_USERNAME",
            "STEAM_ACCOUNT_USERNAME",
            "STEAM_ACCOUNT",
            "STEAM_BOT_USERNAME",
        )

        return SteamBotConfig(
            username=username,
            password=password,
            shared_secret=_env_first(
                "STEAM_SHARED_SECRET",
                "STEAM_TOTP_SECRET",
                "STEAM_BOT_SHARED_SECRET",
            ),
            identity_secret=_env_optional("STEAM_IDENTITY_SECRET")
            or _env_optional("STEAM_BOT_IDENTITY_SECRET"),
            refresh_token=refresh_token,
            refresh_token_path=str(refresh_token_path) if refresh_token_path else None,
            account_name=account_name or username,
            web_api_key=_env_first(
                "STEAM_WEB_API_KEY",
                "STEAM_API_KEY",
                "STEAM_BOT_WEB_API_KEY",
            ),
            deadlock_app_id=_env_first("DEADLOCK_APP_ID", "DEADLOCK_APPID") or "1422450",
        )

    @staticmethod
    def resolve_channel_id_from_env() -> int:
        channel_env = _env_first("STEAM_STATUS_CHANNEL_ID", "DEADLOCK_PRESENCE_CHANNEL_ID")
        if channel_env:
            try:
                return int(channel_env)
            except ValueError:
                log.warning("Invalid STEAM_STATUS_CHANNEL_ID: %s", channel_env)
        return DEFAULT_CHANNEL_ID


async def setup(bot: commands.Bot) -> None:
    try:
        await bot.add_cog(SteamBotCog(bot))
    except RuntimeError as exc:
        log.error("SteamBotCog konnte nicht geladen werden: %s", exc)
        channel_id = SteamBotCog.resolve_channel_id_from_env()
        bot.loop.create_task(_announce_setup_failure(bot, channel_id, str(exc)))
