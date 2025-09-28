# filename: cogs/live_match/live_match_worker.py
# ------------------------------------------------------------
# LiveMatchWorker v2 (v3-ready Telemetrie)
#
# Aufgabe:
#   - Liest gewünschten Suffix je Channel aus live_lane_state
#   - Benennt Channel debounced (max. 1 Rename / 5 Min / Channel)
#   - Nur Delta-Umbenennungen (wenn sich Suffix wirklich ändert)
#   - Entfernt alte Suffixe stabil und hängt neuen korrekt an
#   - Schreibt Telemetrie in live_worker_actions_v3 (für v2/v3-Analysen)
#
# DB-Erwartung (wird automatisch angelegt):
#   live_lane_state(channel_id BIGINT PRIMARY KEY, is_active INT, last_update INT, suffix TEXT, reason TEXT)
#   live_worker_actions_v3(id PK, ts INT, channel_id BIGINT, old_name TEXT, new_name TEXT,
#                          desired_suffix TEXT, applied INT, reason TEXT)
#
# Hinweise:
#   - Keine ENV-Abhängigkeiten hier; feste, konservative Defaults.
#   - Keine "empty except" – Fehler werden protokolliert und in Telemetrie festgehalten.
# ------------------------------------------------------------

import re
import time
import logging
from typing import Dict, Optional

import discord
from discord.ext import commands, tasks

from service import db  # Sync-Wrapper mit execute/query_all/executemany

log = logging.getLogger("LiveMatchWorker")

# Festwerte (bewährt & konservativ)
TICK_SEC = 20                                   # Poll-Intervall Worker
PER_CHANNEL_RENAME_COOLDOWN_SEC = 300           # 5 Minuten Cooldown pro Channel

# Erkennung & Entfernen des von uns gesetzten Suffix-Teils am Channel-Namen:
# Beispiele: "• 3/6 Im Match", "• 4/6 Im Spiel", "• 1/6 Lobby/Queue"
SUFFIX_RX = re.compile(
    r"\s+•\s+\d+/\d+\s+(im\s+match|im\s+spiel|lobby/queue)",
    re.IGNORECASE
)

def _ensure_metrics_schema() -> None:
    """Legt die Telemetrie-Tabelle für den Worker an (idempotent)."""
    db.execute("""
        CREATE TABLE IF NOT EXISTS live_worker_actions_v3 (
          id             INTEGER PRIMARY KEY AUTOINCREMENT,
          ts             INTEGER NOT NULL,
          channel_id     INTEGER NOT NULL,
          old_name       TEXT,
          new_name       TEXT,
          desired_suffix TEXT,
          applied        INTEGER NOT NULL,  -- 1=rename durchgeführt, 0=übersprungen/Fehler
          reason         TEXT
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_lwa3_ts ON live_worker_actions_v3(ts)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_lwa3_channel ON live_worker_actions_v3(channel_id)")


class LiveMatchWorker(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._started = False
        # pro Channel: {last_applied: str, pending: str, last_rename_ts: float}
        self._state: Dict[int, Dict[str, Optional[str | float]]] = {}

    async def cog_load(self):
        db.connect()
        _ensure_metrics_schema()
        if not self._started:
            self.tick.start()
            self._started = True
        log.info(
            "LiveMatchWorker gestartet (Tick=%ss, Cooldown=%ss)",
            TICK_SEC, PER_CHANNEL_RENAME_COOLDOWN_SEC
        )

    async def cog_unload(self):
        if self._started:
            try:
                self.tick.cancel()
            except Exception as e:
                log.debug("Tick cancel beim Unload fehlgeschlagen (ignoriert): %r", e)

    @tasks.loop(seconds=TICK_SEC)
    async def tick(self):
        # Wir lesen nur die minimal nötigen Spalten
        rows = db.query_all("SELECT channel_id, is_active, suffix FROM live_lane_state")
        now = time.time()
        unix_now = int(now)

        for r in rows:
            try:
                channel_id = int(r["channel_id"])
                desired_suffix = (r["suffix"] or "").strip()
                is_active = int(r["is_active"] or 0)
            except Exception as e:
                msg = f"Ungültiger Datensatz in live_lane_state: {e!r}"
                log.debug(msg)
                self._telemetry(unix_now, None, None, None, applied=0, reason=msg)
                continue

            ch = self.bot.get_channel(channel_id)
            if not isinstance(ch, discord.VoiceChannel):
                # Telemetrie, falls Channel nicht mehr existiert/typfalsch
                self._telemetry(unix_now, None, None, desired_suffix, applied=0,
                                reason=f"channel_not_found_or_not_voice:{channel_id}")
                continue

            # State initialisieren: last_applied = was gerade im Namen steht
            st = self._state.get(ch.id)
            if st is None:
                current_suffix = self._extract_suffix(ch.name) or ""
                st = {
                    "last_applied": current_suffix,
                    "pending": desired_suffix,
                    "last_rename_ts": 0.0,  # erlaubt sofortige erste Anpassung
                }
                self._state[ch.id] = st
            else:
                # pending aktualisieren, wenn sich der Ziel-Suffix geändert hat
                if (st.get("pending") or "") != desired_suffix:
                    st["pending"] = desired_suffix

            # Falls Channel als inaktiv markiert ist, wollen wir i.d.R. KEIN Suffix anzeigen.
            if is_active == 0:
                st["pending"] = ""  # Ziel ist „kein Suffix“

            # Cooldown/Delta prüfen
            last_ts = float(st.get("last_rename_ts") or 0.0)
            due = (now - last_ts) >= PER_CHANNEL_RENAME_COOLDOWN_SEC
            want_change = (st.get("pending", "") != st.get("last_applied", ""))

            if not want_change:
                # Nichts zu tun – Telemetrie (sparsam, nur wenn last_applied != extract_base_mismatch)
                continue

            if not due:
                self._telemetry(
                    unix_now, ch.name, None, st.get("pending", ""), applied=0,
                    reason=f"cooldown_active:{int(PER_CHANNEL_RENAME_COOLDOWN_SEC - (now - last_ts))}s_left"
                )
                continue

            # Zielnamen bauen
            base = self._base_name(ch.name)
            target_suffix = st.get("pending", "") or ""
            desired_name = base if not target_suffix else f"{base} {target_suffix}"

            if desired_name == ch.name:
                # Name entspricht bereits Zielzustand
                st["last_applied"] = target_suffix
                continue

            # Rename versuchen
            try:
                await ch.edit(name=desired_name, reason="LiveMatchWorker (debounced rename)")
                st["last_applied"] = target_suffix
                st["last_rename_ts"] = now
                self._telemetry(
                    unix_now, ch.name, desired_name, target_suffix, applied=1, reason="ok"
                )
                log.info("Channel umbenannt: %s -> %s", ch.name, desired_name)
            except discord.Forbidden:
                msg = "permission_denied"
                self._telemetry(unix_now, ch.name, desired_name, target_suffix, applied=0, reason=msg)
                log.warning("Keine Berechtigung für Channel-Umbenennung (%s).", ch.id)
            except discord.HTTPException as e:
                msg = f"http_error:{e}"
                self._telemetry(unix_now, ch.name, desired_name, target_suffix, applied=0, reason=msg)
                log.warning("HTTP-Fehler beim Umbenennen (%s): %s", ch.id, e)
            except Exception as e:
                msg = f"unexpected_error:{e!r}"
                self._telemetry(unix_now, ch.name, desired_name, target_suffix, applied=0, reason=msg)
                log.error("Unerwarteter Fehler beim Umbenennen (%s): %r", ch.id, e)

    @tick.before_loop
    async def _before_tick(self):
        await self.bot.wait_until_ready()

    # ---------------- Hilfsfunktionen ----------------------------------------

    def _base_name(self, name: str) -> str:
        """Entfernt unseren bekannten Suffix-Anteil (• n/cap Im ...) zuverlässig."""
        return SUFFIX_RX.sub("", name).strip()

    def _extract_suffix(self, name: str) -> str:
        """Extrahiert existierenden Suffix („• n/cap Im Match|Im Spiel|Lobby/Queue“) aus dem Namen."""
        m = re.search(r"(•\s+\d+/\d+\s+(Im Match|Im Spiel|Lobby/Queue))", name, flags=re.IGNORECASE)
        return m.group(1) if m else ""

    def _telemetry(self, ts: int, old_name: Optional[str], new_name: Optional[str],
                   desired_suffix: Optional[str], *, applied: int, reason: str) -> None:
        """Schreibt einen Telemetrie-Eintrag (idempotent ungefährlich)."""
        try:
            db.execute(
                """
                INSERT INTO live_worker_actions_v3(ts, channel_id, old_name, new_name, desired_suffix, applied, reason)
                VALUES(?,?,?,?,?,?,?)
                """,
                (int(ts), 0 if new_name is None and old_name is None else 0,  # channel_id ist optional; 0 falls unbekannt
                 old_name, new_name, desired_suffix, int(applied), reason)
            )
        except Exception as e:
            # Telemetrie-Fehler nicht fatal machen, aber loggen
            log.debug("Telemetrie konnte nicht geschrieben werden: %r", e)


async def setup(bot: commands.Bot):
    await bot.add_cog(LiveMatchWorker(bot))
