# cogs/live_match_worker.py
# ------------------------------------------------------------
# LiveMatchWorker – benennt Voice-Channels gemäß live_lane_state
#
# Änderungen:
# - Pro Channel max. 1 Rename / 5 min (KEIN ENV, fixer Wert).
# - Coalescing: innerhalb des Fensters nur den LETZTEN gewünschten Suffix anwenden.
# - Rename nur, wenn sich das Suffix GEÄNDERT hat (Delta).
# - Robust: entfernt alte Suffixe, baut Zielnamen sauber neu auf.
# ------------------------------------------------------------

import re
import time
import logging
from typing import Dict, Optional

import discord
from discord.ext import commands, tasks

from service import db

log = logging.getLogger("LiveMatchWorker")

# Feste Parameter (keine ENV)
TICK_SEC = 20                                  # Worker-Poll
PER_CHANNEL_RENAME_COOLDOWN_SEC = 300          # 5 Minuten

# Entfernt bekannte Suffixe: "• n/cap Im Match|Im Spiel|Lobby/Queue"
SUFFIX_RX = re.compile(
    r"\s+•\s+\d+/\d+\s+(im\s+match|im\s+spiel|lobby/queue)",
    re.IGNORECASE
)

class LiveMatchWorker(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._started = False
        # pro Channel: {last_applied, pending, last_rename_ts}
        self._state: Dict[int, Dict[str, Optional[str | float]]] = {}

    async def cog_load(self):
        db.connect()
        if not self._started:
            self.tick.start()
            self._started = True
        log.info(
            "LiveMatchWorker gestartet (Tick=%ss, per-channel cooldown=%ss)",
            TICK_SEC, PER_CHANNEL_RENAME_COOLDOWN_SEC
        )

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
        now = time.time()

        for r in rows:
            ch = self.bot.get_channel(int(r["channel_id"]))
            if not isinstance(ch, discord.VoiceChannel):
                continue

            desired_suffix = (r.get("suffix") or "").strip()
            st = self._state.get(ch.id)
            if st is None:
                # initialisieren – last_applied = tatsächlich am Namen vorhandenes Suffix
                current_suffix = self._extract_suffix(ch.name)
                st = {
                    "last_applied": current_suffix or "",
                    "pending": desired_suffix,
                    "last_rename_ts": 0.0,  # erlaubt sofort, wenn nötig
                }
                self._state[ch.id] = st
            else:
                # Pending aktualisieren, wenn sich der Wunsch ändert
                if st.get("pending", "") != desired_suffix:
                    st["pending"] = desired_suffix

            # Prüfen, ob ein Rename fällig ist
            due = (now - float(st.get("last_rename_ts") or 0.0)) >= PER_CHANNEL_RENAME_COOLDOWN_SEC
            want_change = (st.get("pending", "") != st.get("last_applied", ""))

            if not due or not want_change:
                continue  # nichts zu tun oder noch im Cooldown

            # Zielnamen berechnen – nur wenn er sich wirklich unterscheidet
            base = self._base_name(ch.name)
            pending_suffix = st.get("pending", "") or ""
            desired_name = base if not pending_suffix else f"{base} {pending_suffix}"

            if desired_name == ch.name:
                # Name entspricht schon dem Ziel → Buchhaltung updaten, kein Patch "verbraten"
                st["last_applied"] = pending_suffix
                continue

            # Versuch zu patchen
            try:
                await ch.edit(name=desired_name, reason="LiveMatchWorker (debounced)")
                st["last_applied"] = pending_suffix
                st["last_rename_ts"] = now
                log.info("Umbenannt: %s -> %s", base, desired_name)
            except discord.HTTPException as e:
                # Bei Rate-Limit o.ä. NICHT schütten – wir versuchen es im nächsten Due-Fenster erneut
                log.warning("Rename fehlgeschlagen (%s): %s", ch.id, e)

    # --------- Hilfen ---------------------------------------------------------
    def _base_name(self, name: str) -> str:
        """Entfernt unseren Suffix-Teil zuverlässig."""
        return SUFFIX_RX.sub("", name).strip()

    def _extract_suffix(self, name: str) -> str:
        """Liest existierenden Suffix („• n/cap …“) aus dem Namen (für Initialisierung)."""
        m = re.search(r"(•\s+\d+/\d+\s+(Im Match|Im Spiel|Lobby/Queue))", name, flags=re.IGNORECASE)
        return m.group(1) if m else ""

async def setup(bot: commands.Bot):
    await bot.add_cog(LiveMatchWorker(bot))
