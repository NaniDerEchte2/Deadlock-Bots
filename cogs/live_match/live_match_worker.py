# ------------------------------------------------------------
# LiveMatchWorker – benennt Voice-Channels gemäß live_lane_state
# DB (shared/db.py):
#   live_lane_state.channel_id, is_active, suffix
#
# Suffix: "• n/cap im Match" (falls aktiv), sonst Basisname ohne Match-Suffix.
# ------------------------------------------------------------

import os
import re
import time
import logging

import discord
from discord.ext import commands, tasks

from shared import db

log = logging.getLogger("LiveMatchWorker")

TICK_SEC               = int(os.getenv("LMW_TICK_SEC", "30"))
NAME_EDIT_COOLDOWN_SEC = int(os.getenv("LMW_NAME_COOLDOWN_SEC", "90"))

MATCH_SUFFIX_RX = re.compile(r"\s+•\s+\d+/\d+\s+im\s+match", re.IGNORECASE)


class LiveMatchWorker(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._last_patch_ts: dict[int, float] = {}
        self._started = False

    async def cog_load(self):
        db.connect()
        if not self._started:
            self.tick.start()
            self._started = True
        log.info("LiveMatchWorker gestartet (Tick=%ss)", TICK_SEC)

    async def cog_unload(self):
        try:
            if self._started:
                self.tick.cancel()
        except Exception:
            pass

    @tasks.loop(seconds=TICK_SEC)
    async def tick(self):
        await self.bot.wait_until_ready()
        rows = db.query_all("SELECT channel_id, is_active, suffix FROM live_lane_state")
        for r in rows:
            ch = self.bot.get_channel(int(r["channel_id"]))
            if not isinstance(ch, discord.VoiceChannel):
                continue

            base = MATCH_SUFFIX_RX.sub("", ch.name).strip()
            desired = base
            if int(r["is_active"] or 0):
                suf = str(r["suffix"] or "").strip()
                if suf:
                    desired = f"{base} {suf}"

            await self._safe_rename(ch, desired)

    async def _safe_rename(self, ch: discord.VoiceChannel, desired: str):
        if not desired or desired == ch.name:
            return
        last = self._last_patch_ts.get(ch.id, 0.0)
        if time.time() - last < NAME_EDIT_COOLDOWN_SEC:
            return
        try:
            await ch.edit(name=desired, reason="LiveMatchWorker")
            self._last_patch_ts[ch.id] = time.time()
        except discord.HTTPException:
            # kurzer Retry
            await discord.utils.sleep_until(discord.utils.utcnow() + discord.utils.timedelta(seconds=1.2))
            try:
                await ch.edit(name=desired, reason="LiveMatchWorker (retry)")
                self._last_patch_ts[ch.id] = time.time()
            except Exception as e:
                log.info("Rename fehlgeschlagen (%s): %s", ch.id, e)


async def setup(bot: commands.Bot):
    await bot.add_cog(LiveMatchWorker(bot))
