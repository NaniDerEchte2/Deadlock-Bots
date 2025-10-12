"""Discord cog to bridge Steam Deadlock presence information.

This module ports the former stand-alone ``deadlock_presence_bot.py`` script
into a Discord cog so it can run together with the main bot instance.  The
behaviour stays the same: the cog logs into Steam, watches friends that are
currently playing Deadlock and mirrors the information into a Discord embed
inside a configured channel.

Environment variables
---------------------
The cog relies on the following environment variables (identical to the
stand-alone script):

``STEAM_USERNAME`` / ``STEAM_PASSWORD``
    Credentials of the Steam account that should be used to watch friends.

``STEAM_TOTP_SECRET`` (optional)
    Base32 secret for generating Steam Guard codes.  If missing and the
    account requires 2FA the cog will log an error and remain inactive.

``DEADLOCK_PRESENCE_CHANNEL_ID`` (optional)
    ID of the Discord text channel where the presence embed should be posted.
    If omitted the channel defaults to ``1374364800817303632``.

``DEADLOCK_PRESENCE_POLL_SECONDS`` (optional)
    Polling interval for a fallback refresh.  Defaults to 20 seconds.

Usage
-----
Simply drop the file into ``cogs/steam`` (done by this commit) and the
auto-discovery inside ``main_bot.py`` will load the cog automatically.  It can
also be reloaded manually via ``!reload steam.deadlock_presence``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

import discord
from discord.ext import commands

import pyotp
from steam.client import SteamClient
from steam.enums import EResult
from steam.enums.emsg import EMsg

LOGGER = logging.getLogger(__name__)

# Steam constants -----------------------------------------------------------
DEADLOCK_APPID = 1422450


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class FriendPresence:
    steamid: int
    name: str
    hero_or_status: str
    since: datetime
    display_line: str


@dataclass
class PresenceSnapshot:
    friends: Dict[int, FriendPresence] = field(default_factory=dict)

    def to_sorted_list(self) -> List[FriendPresence]:
        return sorted(self.friends.values(), key=lambda f: (f.since, f.name.lower()))


# ---------------------------------------------------------------------------
# Steam watcher thread (unchanged core logic from stand-alone script)
# ---------------------------------------------------------------------------


class SteamPresenceWatcher(threading.Thread):
    """Background thread that keeps a SteamClient connection alive."""

    def __init__(
        self,
        *,
        out_queue: "queue.Queue[PresenceSnapshot | None]",
        username: str,
        password: str,
        totp_secret: Optional[str],
        poll_seconds: int = 20,
    ) -> None:
        super().__init__(daemon=True)
        self.out_queue = out_queue
        self.username = username
        self.password = password
        self.totp_secret = totp_secret
        self.poll_seconds = poll_seconds

        self.client = SteamClient()
        self.running = threading.Event()
        self.running.set()

        self.current_snapshot = PresenceSnapshot()
        self.first_seen: Dict[int, datetime] = {}

        self._last_emit: float = 0.0
        self._emit_min_interval = 5.0

        self.client.on(EMsg.ClientPersonaState, self._on_persona_state)

    # -- life-cycle -----------------------------------------------------

    def stop(self) -> None:
        self.running.clear()
        try:
            if getattr(self.client, "connected", False):
                self.client.logout()
        except Exception as exc:  # pragma: no cover - best effort only
            LOGGER.debug("Steam logout failed: %s", exc)
        finally:
            # unblock queue consumers
            try:
                self.out_queue.put_nowait(None)
            except Exception:
                pass

    # -- helpers --------------------------------------------------------

    def _otp_code(self) -> Optional[str]:
        if not self.totp_secret:
            return None
        try:
            return pyotp.TOTP(self.totp_secret).now()
        except Exception as exc:
            LOGGER.warning("Failed to generate Steam TOTP code: %s", exc)
            return None

    def login(self) -> bool:
        LOGGER.info("[Steam] Logging in as %s", self.username)
        res = self.client.login(
            username=self.username,
            password=self.password,
            two_factor_code=self._otp_code(),
        )

        if res == EResult.AccountLoginDeniedNeedTwoFactor:
            LOGGER.error(
                "Steam login requires two-factor authentication but no valid TOTP secret is available."
            )
            return False

        if res == EResult.AccountLogonDenied:
            LOGGER.error("Steam Guard e-mail code required – interactive input is unavailable in cog mode")
            return False

        if res != EResult.OK:
            LOGGER.error("Steam login failed: %s", res)
            return False

        LOGGER.info("[Steam] Logged in as %s", self.client.user.name)
        return True

    # -- event/poll handling --------------------------------------------

    def _on_persona_state(self, _msg) -> None:
        if self._rebuild_snapshot():
            self._maybe_emit_snapshot()

    def _poll_loop(self) -> None:
        while self.running.is_set():
            try:
                if self._rebuild_snapshot():
                    self._maybe_emit_snapshot()
            except Exception as exc:
                LOGGER.exception("[Steam] Polling error: %s", exc)
            time.sleep(self.poll_seconds)

    # -- snapshot construction ------------------------------------------

    def _rebuild_snapshot(self) -> bool:
        new_snapshot = PresenceSnapshot()
        try:
            for friend in self.client.friends:
                appid = friend.get_ps("game_played_app_id")
                game_name = friend.get_ps("game_name")
                if appid != DEADLOCK_APPID and (not game_name or "Deadlock" not in (game_name or "")):
                    continue

                steamid = int(friend.steam_id)
                name = friend.name or str(steamid)
                rp = getattr(friend, "rich_presence", None) or {}
                hero = (
                    rp.get("hero")
                    or rp.get("character")
                    or rp.get("status")
                    or rp.get("steam_display")
                    or "Im Spiel"
                )

                display_source = str(hero)
                if "Deadlock:" in display_source:
                    display_source = display_source.split("Deadlock:", 1)[1].strip()

                now = datetime.utcnow()
                since = self.first_seen.get(steamid)
                if not since:
                    since = now
                    self.first_seen[steamid] = now

                minutes = max(0, int((now - since).total_seconds() // 60))
                display_line = f"Deadlock: {display_source} ({minutes}. Min.)"

                new_snapshot.friends[steamid] = FriendPresence(
                    steamid=steamid,
                    name=name,
                    hero_or_status=display_source,
                    since=since,
                    display_line=display_line,
                )
        except Exception as exc:
            LOGGER.exception("[Steam] Snapshot rebuild failed: %s", exc)

        changed = self._snapshot_changed(self.current_snapshot, new_snapshot)
        if changed:
            self.current_snapshot = new_snapshot
        return changed

    @staticmethod
    def _snapshot_changed(a: PresenceSnapshot, b: PresenceSnapshot) -> bool:
        if set(a.friends.keys()) != set(b.friends.keys()):
            return True
        for key, fb in b.friends.items():
            fa = a.friends.get(key)
            if not fa:
                return True
            if fa.display_line != fb.display_line or fa.name != fb.name:
                return True
        return False

    def _maybe_emit_snapshot(self) -> None:
        now = time.time()
        if now - self._last_emit < self._emit_min_interval:
            return
        self._last_emit = now
        try:
            self.out_queue.put_nowait(self.current_snapshot)
        except Exception:
            LOGGER.debug("Failed to queue snapshot (queue full?)")

    # -- thread entry ----------------------------------------------------

    def run(self) -> None:  # pragma: no cover - threaded code
        if not self.login():
            LOGGER.error("Steam presence watcher did not start due to login failure")
            try:
                self.out_queue.put_nowait(None)
            except Exception:
                pass
            return

        self._rebuild_snapshot()
        self._maybe_emit_snapshot()

        poller = threading.Thread(target=self._poll_loop, daemon=True)
        poller.start()

        try:
            self.client.run_forever()
        except Exception as exc:
            LOGGER.exception("Steam client error: %s", exc)
        finally:
            self.running.clear()
            try:
                self.client.logout()
            except Exception:
                pass
            try:
                self.out_queue.put_nowait(None)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Discord Cog
# ---------------------------------------------------------------------------


class DeadlockPresence(commands.Cog):
    """Cog that mirrors Steam Deadlock presence into a Discord channel."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.channel_id = int(os.getenv("DEADLOCK_PRESENCE_CHANNEL_ID", "1374364800817303632"))
        self.poll_seconds = int(os.getenv("DEADLOCK_PRESENCE_POLL_SECONDS", "20"))

        self._steam_username = os.getenv("STEAM_USERNAME", "").strip()
        self._steam_password = os.getenv("STEAM_PASSWORD", "").strip()
        self._steam_totp_secret = os.getenv("STEAM_TOTP_SECRET", "").strip() or None

        self._queue: "queue.Queue[PresenceSnapshot | None]" = queue.Queue()
        self._watcher: Optional[SteamPresenceWatcher] = None
        self._consumer_task: Optional[asyncio.Task[None]] = None
        self._message_id: Optional[int] = None
        self._channel: Optional[discord.TextChannel] = None
        self._publish_lock = asyncio.Lock()

        self._enabled = bool(self._steam_username and self._steam_password)
        if not self._enabled:
            LOGGER.warning(
                "Steam Deadlock presence disabled: STEAM_USERNAME/STEAM_PASSWORD missing"
            )

    # life-cycle --------------------------------------------------------

    async def cog_load(self) -> None:
        if not self._enabled:
            return

        self._watcher = SteamPresenceWatcher(
            out_queue=self._queue,
            username=self._steam_username,
            password=self._steam_password,
            totp_secret=self._steam_totp_secret,
            poll_seconds=self.poll_seconds,
        )
        self._watcher.start()
        self._consumer_task = asyncio.create_task(self._consume_queue())

    async def cog_unload(self) -> None:
        if self._consumer_task:
            self._consumer_task.cancel()
            try:
                await self._consumer_task
            except asyncio.CancelledError:
                pass
            self._consumer_task = None

        if self._watcher:
            self._watcher.stop()
            self._watcher.join(timeout=5)
            self._watcher = None

    # Discord events ----------------------------------------------------

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if not self._enabled:
            return
        if self._channel is not None:
            return
        channel = self.bot.get_channel(self.channel_id)
        if isinstance(channel, discord.TextChannel):
            self._channel = channel
            LOGGER.info("Deadlock presence channel resolved: #%s", channel)
        else:
            LOGGER.error(
                "Deadlock presence channel %s not found or not a text channel", self.channel_id
            )

    # internal helpers --------------------------------------------------

    async def _consume_queue(self) -> None:  # pragma: no cover - runs forever
        loop = asyncio.get_running_loop()
        while True:
            try:
                snapshot = await loop.run_in_executor(None, self._queue.get)
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(1)
                continue

            if snapshot is None:
                if not self._watcher or not self._watcher.running.is_set():
                    break
                await asyncio.sleep(0)
                continue

            try:
                await self._publish_snapshot(snapshot)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                LOGGER.exception("Failed to publish Steam snapshot: %s", exc)

    async def _publish_snapshot(self, snapshot: PresenceSnapshot) -> None:
        if not self._channel:
            # channel not ready yet, retry later
            return

        async with self._publish_lock:
            friends = snapshot.to_sorted_list()
            title = "🟢 Deadlock – wer spielt gerade?"
            ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

            if not friends:
                description = "_Niemand aus der Freundesliste spielt gerade Deadlock._"
            else:
                lines = []
                for entry in friends:
                    name = discord.utils.escape_markdown(entry.name)
                    lines.append(f"**{name}**\n{entry.display_line}")
                description = "\n\n".join(lines)

            embed = discord.Embed(title=title, description=description)
            embed.set_footer(text=f"Aktualisiert: {ts}")

            try:
                if self._message_id:
                    message = await self._channel.fetch_message(self._message_id)
                    await message.edit(embed=embed)
                else:
                    message = await self._channel.send(embed=embed)
                    self._message_id = message.id
            except discord.HTTPException as exc:
                LOGGER.warning("Discord HTTP error while publishing presence: %s", exc)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(DeadlockPresence(bot))

