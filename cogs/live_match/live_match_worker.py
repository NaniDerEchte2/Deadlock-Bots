# filename: cogs/live_match/live_match_worker.py
# ------------------------------------------------------------
# LiveMatchWorker v2.2 (v3-ready Telemetrie) – robustes Suffix-Handling
#
# Änderungen in v2.2:
#   - Entfernt jetzt Suffix-Blöcke sowohl MIT "• n/c" als auch OHNE Zähler.
#   - Extrahiert den letzten Suffix-Block in beiden Formen (mit/ohne Bullet).
#   - Kanonisiert Suffixe aggressiver (case/whitespace, Bullet/Zähler egal).
#   - Optionaler Stale-Schutz: ignoriert veraltete live_lane_state-Einträge.
# ------------------------------------------------------------

import re
import time
import logging
from typing import Dict, Optional

import discord
from discord.ext import commands, tasks

from service import db  # Sync-Wrapper mit execute/query_all/executemany

log = logging.getLogger("LiveMatchWorker")

# Festwerte
TICK_SEC = 20                                   # Poll-Intervall Worker
PER_CHANNEL_RENAME_COOLDOWN_SEC = 310           # ~5 Minuten + 10 Sekunden Cooldown pro Channel
STALE_STATE_MAX_AGE_SEC = 600                   # 10 Minuten: älter = kein Rename

# Erlaubte Suffix-Varianten (kanonische Textteile)
_SUFFIX_TERMS = r"(?:im\s+match|im\s+spiel|in\s+der\s+lobby|lobby/queue)"

# Für die Extraktion des *letzten* vorhandenen Suffix-Blocks (mit/ohne Zähler).
EXTRACT_LAST_SUFFIX_RX = re.compile(
    rf"((?:•\s*\d+/\d+\s*)?{_SUFFIX_TERMS}(?:\s*\(\d+\s*DL\))?)",
    re.IGNORECASE,
)

SUFFIX_DISPLAY_RX = re.compile(
    rf"\s*(?:•\s*\d+/\d+\s*)?(?:{_SUFFIX_TERMS})(?:\s*\(\d+\s*DL\))?",
    re.IGNORECASE,
)

def _canon(s: Optional[str]) -> str:
    """Kanonische Form für Vergleich (Zähler/Bullet/Case/Whitespace egal)."""
    t = (s or "").strip().lower()
    # Bullet + Zähler entfernen
    t = re.sub(r"•\s*\d+/\d+\s*", "", t)
    # DL-Klammern entfernen
    t = re.sub(r"\(\s*\d+\s*dl\s*\)", "", t, flags=re.IGNORECASE)
    # Mehrfache Whitespaces normalisieren
    t = re.sub(r"\s+", " ", t)
    # Nur anerkannte Suffix-Terme stehen lassen
    m = re.search(_SUFFIX_TERMS, t, re.IGNORECASE)
    return m.group(0) if m else ""

def _base_name(name: str) -> str:
    """Entfernt *alle* erkannten Suffix-Blöcke (mit/ohne Zähler) zuverlässig."""
    return SUFFIX_DISPLAY_RX.sub("", name).strip()

def _extract_last_suffix_display(name: str) -> str:
    """Extrahiert den letzten Suffix-Block inklusive DL-Zusätzen für Display."""
    matches = EXTRACT_LAST_SUFFIX_RX.findall(name)
    if not matches:
        return ""
    return matches[-1].strip()

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
        # pro Channel: {last_applied_display, last_applied_canon, pending_display, pending_canon, last_rename_ts}
        self._state: Dict[int, Dict[str, Optional[str | float]]] = {}

    async def cog_load(self):
        db.connect()
        _ensure_metrics_schema()
        if not self._started:
            self.tick.start()
            self._started = True
        log.info(
            "LiveMatchWorker gestartet (Tick=%ss, Cooldown=%ss, Stale=%ss)",
            TICK_SEC, PER_CHANNEL_RENAME_COOLDOWN_SEC, STALE_STATE_MAX_AGE_SEC
        )

    async def cog_unload(self):
        if self._started:
            try:
                self.tick.cancel()
            except Exception as e:
                log.debug("Tick cancel beim Unload fehlgeschlagen (ignoriert): %r", e)

    @tasks.loop(seconds=TICK_SEC)
    async def tick(self):
        # Wir lesen jetzt auch last_update für Stale-Schutz
        rows = db.query_all("SELECT channel_id, is_active, suffix, last_update FROM live_lane_state")
        now = time.time()
        unix_now = int(now)

        for r in rows:
            try:
                channel_id = int(r["channel_id"])
                desired_suffix_raw = (r["suffix"] or "").strip()
                is_active = int(r["is_active"] or 0)
                last_update = int(r.get("last_update") or 0)
            except Exception as e:
                msg = f"Ungültiger Datensatz in live_lane_state: {e!r}"
                log.debug(msg)
                self._telemetry(unix_now, 0, None, None, None, applied=0, reason=msg)
                continue

            desired_display = desired_suffix_raw
            desired_canon = _canon(desired_suffix_raw)

            # Stale-Schutz: wenn live_lane_state zu alt, nicht umbenennen
            if last_update and (unix_now - last_update) > STALE_STATE_MAX_AGE_SEC:
                self._telemetry(
                    unix_now, channel_id, None, None, desired_display, applied=0,
                    reason=f"stale_state:{unix_now - last_update}s_old"
                )
                continue

            ch = self.bot.get_channel(channel_id)
            if not isinstance(ch, discord.VoiceChannel):
                self._telemetry(unix_now, channel_id, None, None, desired_display, applied=0,
                                reason="channel_not_found_or_not_voice")
                continue

            if is_active == 0:
                desired_display = ""
                desired_canon = ""

            # State initialisieren: last_applied = was gerade im Namen steht (letzter erkannter Suffix)
            st = self._state.get(ch.id)
            if st is None:
                current_display = _extract_last_suffix_display(ch.name)
                current_canon = _canon(current_display)
                st = {
                    "last_applied_display": current_display,
                    "last_applied_canon": current_canon,
                    "pending_display": desired_display,
                    "pending_canon": desired_canon,
                    "last_rename_ts": 0.0,  # erlaubt sofortige erste Anpassung
                }
                self._state[ch.id] = st
            else:
                if (
                    st.get("pending_display") != desired_display
                    or st.get("pending_canon") != desired_canon
                ):
                    st["pending_display"] = desired_display
                    st["pending_canon"] = desired_canon

            # Cooldown/Delta prüfen (Vergleich auf kanonischer Basis)
            last_ts = float(st.get("last_rename_ts") or 0.0)
            due = (now - last_ts) >= PER_CHANNEL_RENAME_COOLDOWN_SEC
            pending_display = (st.get("pending_display") or "").strip()
            pending_canon = st.get("pending_canon") or _canon(pending_display)
            last_display = (st.get("last_applied_display") or "").strip()
            last_canon = st.get("last_applied_canon") or _canon(last_display)
            want_change = (pending_display != last_display) or (pending_canon != last_canon)

            if not want_change:
                continue

            if not due:
                self._telemetry(
                    unix_now, ch.id, ch.name, None, pending_display, applied=0,
                    reason=f"cooldown_active:{int(PER_CHANNEL_RENAME_COOLDOWN_SEC - (now - last_ts))}s_left"
                )
                continue

            # Zielnamen bauen – vorher ALLE bestehenden Suffix-Blöcke (mit/ohne Zähler) wegschneiden
            base = _base_name(ch.name)
            target_suffix = pending_display
            desired_name = base if not target_suffix else f"{base} {target_suffix}".strip()

            if desired_name == ch.name:
                st["last_applied_display"] = target_suffix
                st["last_applied_canon"] = _canon(target_suffix)
                continue

            # Rename versuchen
            try:
                await ch.edit(name=desired_name, reason="LiveMatchWorker (debounced rename)")
                st["last_applied_display"] = target_suffix
                st["last_applied_canon"] = _canon(target_suffix)
                st["last_rename_ts"] = now
                self._telemetry(unix_now, ch.id, ch.name, desired_name, target_suffix, applied=1, reason="ok")
                log.info("Channel umbenannt: %s -> %s", ch.name, desired_name)
            except discord.Forbidden:
                msg = "permission_denied"
                self._telemetry(unix_now, ch.id, ch.name, desired_name, target_suffix, applied=0, reason=msg)
                log.warning("Keine Berechtigung für Channel-Umbenennung (%s).", ch.id)
            except discord.HTTPException as e:
                msg = f"http_error:{e}"
                self._telemetry(unix_now, ch.id, ch.name, desired_name, target_suffix, applied=0, reason=msg)
                log.warning("HTTP-Fehler beim Umbenennen (%s): %s", ch.id, e)
            except Exception as e:
                msg = f"unexpected_error:{e!r}"
                self._telemetry(unix_now, ch.id, ch.name, desired_name, target_suffix, applied=0, reason=msg)
                log.error("Unerwarteter Fehler beim Umbenennen (%s): %r", ch.id, e)

    @tick.before_loop
    async def _before_tick(self):
        await self.bot.wait_until_ready()

    # ---------------- Telemetrie ---------------------------------------------

    def _telemetry(
        self,
        ts: int,
        channel_id: int,
        old_name: Optional[str],
        new_name: Optional[str],
        desired_suffix: Optional[str],
        *,
        applied: int,
        reason: str,
    ) -> None:
        """Schreibt einen Telemetrie-Eintrag (idempotent ungefährlich)."""
        try:
            db.execute(
                """
                INSERT INTO live_worker_actions_v3(ts, channel_id, old_name, new_name, desired_suffix, applied, reason)
                VALUES(?,?,?,?,?,?,?)
                """,
                (int(ts), int(channel_id), old_name, new_name, desired_suffix, int(applied), reason)
            )
        except Exception as e:
            # Telemetrie-Fehler nicht fatal machen, aber loggen
            log.debug("Telemetrie konnte nicht geschrieben werden: %r", e)

async def setup(bot: commands.Bot):
    await bot.add_cog(LiveMatchWorker(bot))
