# cogs/live_match_worker.py
import os, re, time, logging
import discord
from discord.ext import commands, tasks
from shared import db

log = logging.getLogger("LiveMatchWorker")

NAME_EDIT_COOLDOWN_SEC = int(os.getenv("NAME_EDIT_COOLDOWN_SEC", "120"))
TIMER_TICK_SEC = int(os.getenv("TIMER_TICK_SEC", "60"))
RENAME_STEP_MIN = int(os.getenv("RENAME_STEP_MIN", "5"))  # alle 5 Minuten Namen aktualisieren

SUFFIX_PATTERN = re.compile(r"(?:\s*•\s*Im Match\s*\(Min\s*\d+\))$", re.IGNORECASE)

class LiveMatchWorker(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._last_edit: dict[int, int] = {}  # channel_id -> ts
        self.tick.start()

    def cog_unload(self):
        self.tick.cancel()

    def _strip_suffix(self, name: str) -> str:
        return re.sub(SUFFIX_PATTERN, "", name).strip()

    def _can_edit(self, channel_id: int) -> bool:
        last = self._last_edit.get(channel_id, 0)
        return (time.time() - last) >= NAME_EDIT_COOLDOWN_SEC

    async def _rename(self, ch: discord.VoiceChannel, base: str, minutes: int):
        if not self._can_edit(ch.id):
            return
        new = f"{base} • Im Match (Min {minutes})"
        if ch.name == new:
            return
        try:
            await ch.edit(name=new, reason="LiveMatch status update")
            self._last_edit[ch.id] = int(time.time())
        except Exception as e:
            log.warning("Rename failed %s: %s", ch.id, e)

    async def _remove_suffix(self, ch: discord.VoiceChannel):
        base = self._strip_suffix(ch.name)
        if base != ch.name and self._can_edit(ch.id):
            try:
                await ch.edit(name=base, reason="LiveMatch clear")
                self._last_edit[ch.id] = int(time.time())
            except Exception as e:
                log.warning("Clear failed %s: %s", ch.id, e)

    @tasks.loop(seconds=TIMER_TICK_SEC)
    async def tick(self):
        now = int(time.time())
        # alle Lanes aus DB holen, die existieren (wir editieren nur, wenn der Channel noch da ist)
        for row in db.query_all("SELECT channel_id,is_active,started_at,last_update,minutes FROM live_lane_state"):
            ch = self.bot.get_channel(int(row["channel_id"]))
            if not isinstance(ch, discord.VoiceChannel):
                continue
            is_active = int(row["is_active"]) == 1
            minutes = int(row["minutes"] or 0)

            if is_active:
                # Minuten hochzählen
                minutes += int(TIMER_TICK_SEC // 60)
                # nur alle RENAME_STEP_MIN Minuten umbenennen (Rate-Limit)
                rename_now = (minutes % RENAME_STEP_MIN == 0)
                db.execute("UPDATE live_lane_state SET minutes=?, last_update=? WHERE channel_id=?",
                           (minutes, now, ch.id))
                if rename_now:
                    await self._rename(ch, self._strip_suffix(ch.name), minutes)
            else:
                # inaktiv → Suffix entfernen + Minuten resetten
                if minutes != 0:
                    db.execute("UPDATE live_lane_state SET minutes=0, last_update=? WHERE channel_id=?", (now, ch.id))
                await self._remove_suffix(ch)

async def setup(bot: commands.Bot):
    await bot.add_cog(LiveMatchWorker(bot))
