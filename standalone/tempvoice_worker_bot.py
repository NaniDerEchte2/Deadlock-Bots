# filename: service/tempvoice_worker_bot.py
# ------------------------------------------------------------
# TempVoice Worker Bot (Ops-only Default)
#
# - Startet einen einfachen JSON-Line TCP-Server für Voice-/Channel-Operationen.
# - Standardmäßig KEINE Auto-Renames (LIVE_MATCH_ENABLE=0).
# - Rename-OPS über Socket geblockt (SOCKET_RENAME_ALLOW=0).
# - Kompatibel mit LiveMatchWorker-Cog, ohne Überschneidungen.
# ------------------------------------------------------------

import os
import re
import json
import asyncio
import logging
from typing import Any, Dict, Optional

import discord

# ===== Konfiguration per ENV =====
DISCORD_TOKEN         = os.getenv("TEMPVOICE_TOKEN") or os.getenv("DISCORD_TOKEN")  # Worker-Token
SOCKET_HOST           = os.getenv("TEMPVOICE_SOCKET_HOST", "127.0.0.1")
SOCKET_PORT           = int(os.getenv("TEMPVOICE_SOCKET_PORT", "8766"))

# --- WICHTIG: Renamer standardmäßig AUS (Ops-only) ---
LIVE_MATCH_ENABLE     = (os.getenv("LIVE_MATCH_ENABLE", "0") == "1")
# Rename-OPS via Socket standardmäßig blockieren
SOCKET_RENAME_ALLOW   = (os.getenv("SOCKET_RENAME_ALLOW", "0") == "1")

# (Optional) DB für Auto-Renamer, falls LIVE_MATCH_ENABLE=1
DB_PATH               = os.getenv("DEADLOCK_DB_PATH", "service/deadlock.sqlite3")
LIVE_STATE_TABLE      = "live_lane_state"

# Discord Intents (Voice/Channels)
intents = discord.Intents.none()
intents.guilds = True
intents.voice_states = True
intents.members = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s - TempVoice - %(levelname)s - %(message)s")
logger = logging.getLogger("TempVoiceWorker")


# ====== Util: Suffix-Cleanup / Name-Builder ==================================

# Erkennung gängiger Match/Lobby-Suffixe mit Zähler, analog zum Worker-Cog
_SUFFIX_VARIANTS = r"(?:im\s+match|im\s+spiel|in\s+der\s+lobby|lobby/queue)"
SUFFIX_RX = re.compile(rf"(?:\s*•\s*\d+/\d+(?:\s*\(max\s*\d+\))?\s*(?:{_SUFFIX_VARIANTS}))+",
                       re.IGNORECASE)
EXTRACT_LAST_SUFFIX_RX = re.compile(rf"(•\s*\d+/\d+(?:\s*\(max\s*\d+\))?\s*(?:{_SUFFIX_VARIANTS}))",
                                    re.IGNORECASE)

def _normalize_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

def _base_name_cleanup(name: str) -> str:
    """Entfernt ALLE erkannten Suffix-Blöcke (auch gestapelte)."""
    return _normalize_spaces(SUFFIX_RX.sub("", name))

def _build_name(base: str, suffix: Optional[str]) -> str:
    base = _normalize_spaces(base)
    suf  = _normalize_spaces(suffix or "")
    return base if not suf else f"{base} {suf}"


# ====== Discord Client ========================================================

class TempVoiceWorker(discord.Client):
    def __init__(self) -> None:
        super().__init__(intents=intents)
        self.socket_server: Optional[asyncio.base_events.Server] = None
        self._renamer_task: Optional[asyncio.Task] = None

        # Lazy DB handle (nur wenn Renamer aktiv ist)
        self._db = None

    # ---- Socket Server (JSON line) ------------------------------------------

    async def start_socket_server(self) -> None:
        loop = asyncio.get_running_loop()
        self.socket_server = await asyncio.start_server(self._handle_conn, SOCKET_HOST, SOCKET_PORT)
        addr = ", ".join(str(sock.getsockname()) for sock in self.socket_server.sockets or [])
        logger.info("Socket-Server lauscht auf %s", addr)

        async with self.socket_server:
            await self.socket_server.serve_forever()

    async def _handle_conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="ignore").strip()
                if not line:
                    continue

                try:
                    payload = json.loads(line)
                except Exception as e:
                    await self._send_json(writer, {"ok": False, "error": f"invalid_json: {e}"})
                    continue

                try:
                    resp = await self._handle_op(payload)
                except Exception as e:
                    logger.exception("op failed")
                    resp = {"ok": False, "error": f"internal_error:{e!r}"}

                await self._send_json(writer, resp)
        except Exception as e:
            logger.debug("Socket connection error from %s: %r", peer, e)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _send_json(self, writer: asyncio.StreamWriter, obj: Dict[str, Any]) -> None:
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        writer.write(data)
        await writer.drain()

    # ---- OP Handler ----------------------------------------------------------

    async def _handle_op(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Erwartet JSON wie:
        {"op": "edit_channel", "channel_id": 123, ...}
        """
        op = str(payload.get("op") or "").lower().strip()
        if not op:
            return {"ok": False, "error": "missing_op"}

        # Hilfsfunktionen
        async def _get_channel(cid: int) -> Optional[discord.abc.GuildChannel]:
            for g in self.guilds:
                ch = g.get_channel(cid)
                if ch:
                    return ch
            return None

        async def _get_member(guild_id: int, user_id: int) -> Optional[discord.Member]:
            g = self.get_guild(guild_id)
            if not g:
                return None
            try:
                return await g.fetch_member(user_id)
            except Exception:
                return g.get_member(user_id)

        # --- OPS ---
        if op == "edit_channel":
            channel_id = int(payload.get("channel_id"))
            reason     = str(payload.get("reason") or "TempVoice edit")
            overwrites = payload.get("overwrites")  # optional, ignorieren wir hier

            ch = await _get_channel(channel_id)
            if not isinstance(ch, (discord.VoiceChannel, discord.StageChannel)):
                return {"ok": False, "error": f"channel_not_found_or_not_voice:{channel_id}"}

            kwargs: Dict[str, Any] = {}
            if "name" in payload:
                # Cleanup auf Basename + optionaler externer Suffix (wir gehen nicht von match-suffix aus)
                kwargs["name"] = _normalize_spaces(payload["name"])
            if "user_limit" in payload:
                kwargs["user_limit"] = int(payload["user_limit"]) if payload["user_limit"] is not None else 0
            if "bitrate" in payload:
                kwargs["bitrate"] = int(payload["bitrate"])

            try:
                await ch.edit(reason=reason, **kwargs)
                return {"ok": True, "applied": kwargs}
            except discord.Forbidden:
                return {"ok": False, "error": "forbidden"}
            except discord.HTTPException as e:
                return {"ok": False, "error": f"http:{e}"}

        if op == "set_permissions":
            channel_id = int(payload.get("channel_id"))
            target_id  = int(payload.get("target_id"))
            allow      = int(payload.get("allow") or 0)
            deny       = int(payload.get("deny") or 0)
            reason     = str(payload.get("reason") or "TempVoice perms")

            ch = await _get_channel(channel_id)
            if not isinstance(ch, (discord.VoiceChannel, discord.StageChannel, discord.CategoryChannel)):
                return {"ok": False, "error": f"channel_not_voice/category:{channel_id}"}

            # Ziel kann Rolle oder Member sein
            target: Optional[discord.abc.Snowflake] = None
            if isinstance(ch, discord.CategoryChannel):
                guild = ch.guild
            else:
                guild = ch.guild

            role = guild.get_role(target_id)
            member = guild.get_member(target_id)

            target = role or member
            if not target:
                return {"ok": False, "error": "target_not_found"}

            perms = discord.PermissionOverwrite()
            # Grob: Discord nutzt hier Flags, wir setzen nur rudimentär falls nötig
            # (Erweiterbar nach Bedarf)
            if allow or deny:
                # Platzhalter: Hier könnte man bitmaskenbasierte Zuordnung ergänzen.
                pass

            try:
                await ch.set_permissions(target, overwrite=perms, reason=reason)
                return {"ok": True}
            except discord.Forbidden:
                return {"ok": False, "error": "forbidden"}
            except discord.HTTPException as e:
                return {"ok": False, "error": f"http:{e}"}

        if op == "create_voice":
            guild_id   = int(payload.get("guild_id"))
            name       = _normalize_spaces(str(payload.get("name") or "Temp Voice"))
            category_id= payload.get("category_id")
            reason     = str(payload.get("reason") or "TempVoice create")

            g = self.get_guild(guild_id)
            if not g:
                return {"ok": False, "error": f"guild_not_found:{guild_id}"}
            cat = g.get_channel(int(category_id)) if category_id else None
            try:
                ch = await g.create_voice_channel(name=name, category=cat if isinstance(cat, discord.CategoryChannel) else None, reason=reason)
                return {"ok": True, "channel_id": ch.id}
            except discord.Forbidden:
                return {"ok": False, "error": "forbidden"}
            except discord.HTTPException as e:
                return {"ok": False, "error": f"http:{e}"}

        if op == "delete_channel":
            channel_id = int(payload.get("channel_id"))
            reason     = str(payload.get("reason") or "TempVoice delete")
            ch = await _get_channel(channel_id)
            if not ch:
                return {"ok": False, "error": f"channel_not_found:{channel_id}"}
            try:
                await ch.delete(reason=reason)
                return {"ok": True}
            except discord.Forbidden:
                return {"ok": False, "error": "forbidden"}
            except discord.HTTPException as e:
                return {"ok": False, "error": f"http:{e}"}

        if op == "move_member":
            guild_id   = int(payload.get("guild_id"))
            user_id    = int(payload.get("user_id"))
            channel_id = int(payload.get("channel_id"))
            reason     = str(payload.get("reason") or "TempVoice move")

            member = await _get_member(guild_id, user_id)
            if not member:
                return {"ok": False, "error": "member_not_found"}

            ch = await _get_channel(channel_id)
            if not isinstance(ch, (discord.VoiceChannel, discord.StageChannel)):
                return {"ok": False, "error": "target_not_voice"}

            try:
                await member.move_to(ch, reason=reason)
                return {"ok": True}
            except discord.Forbidden:
                return {"ok": False, "error": "forbidden"}
            except discord.HTTPException as e:
                return {"ok": False, "error": f"http:{e}"}

        # --- RENAME-OPS: per Default blockiert ---
        if op == "rename_match_suffix":
            if not SOCKET_RENAME_ALLOW:
                return {"ok": False, "error": "rename_via_socket_disabled (set SOCKET_RENAME_ALLOW=1 to allow)"}
            channel_id = int(payload.get("channel_id"))
            ch = await _get_channel(channel_id)
            if not isinstance(ch, (discord.VoiceChannel, discord.StageChannel)):
                return {"ok": False, "error": "target_not_voice"}
            base = _base_name_cleanup(ch.name)
            suffix = _normalize_spaces(str(payload.get("suffix") or ""))
            desired = _build_name(base, suffix)
            return await self._safe_rename(ch, desired, reason=str(payload.get("reason") or "TempVoice rename"))

        if op == "clear_match_suffix":
            if not SOCKET_RENAME_ALLOW:
                return {"ok": False, "error": "clear_via_socket_disabled (set SOCKET_RENAME_ALLOW=1 to allow)"}
            channel_id = int(payload.get("channel_id"))
            ch = await _get_channel(channel_id)
            if not isinstance(ch, (discord.VoiceChannel, discord.StageChannel)):
                return {"ok": False, "error": "target_not_voice"}
            base = _base_name_cleanup(ch.name)
            desired = _build_name(base, "")
            return await self._safe_rename(ch, desired, reason=str(payload.get("reason") or "TempVoice clear"))

        return {"ok": False, "error": f"unknown_op:{op}"}

    # ---- Safe Rename ---------------------------------------------------------

    async def _safe_rename(self, channel: discord.abc.GuildChannel, desired: str, reason: str) -> Dict[str, Any]:
        desired = desired[:100]  # Discord-Limit
        if getattr(channel, "name", "") == desired:
            return {"ok": True, "skipped": "noop", "name": desired}
        try:
            await channel.edit(name=desired, reason=reason)
            return {"ok": True, "name": desired}
        except discord.Forbidden:
            return {"ok": False, "error": "forbidden"}
        except discord.HTTPException as e:
            return {"ok": False, "error": f"http:{e}"}

    # ---- Auto-Renamer (nur wenn LIVE_MATCH_ENABLE=1) -------------------------

    async def _open_db(self):
        if self._db is not None:
            return self._db
        import sqlite3
        self._db = sqlite3.connect(DB_PATH)
        self._db.row_factory = sqlite3.Row
        return self._db

    async def live_match_runner(self):
        """Liest periodisch live_lane_state und setzt den Namen. Nur aktiv, wenn LIVE_MATCH_ENABLE=1."""
        logger.info("LiveMatch-Renamer aktiv – liest %s.%s", DB_PATH, LIVE_STATE_TABLE)
        import time as _t
        while not self.is_closed():
            try:
                db = await self._open_db()
                cur = db.execute(f"SELECT channel_id, suffix FROM {LIVE_STATE_TABLE} WHERE is_active=1")
                rows = cur.fetchall()
                # Map Channels
                for r in rows:
                    cid = int(r["channel_id"])
                    suffix = (r["suffix"] or "").strip()
                    ch = None
                    for g in self.guilds:
                        ch = g.get_channel(cid) or ch
                    if not isinstance(ch, (discord.VoiceChannel, discord.StageChannel)):
                        continue
                    base = _base_name_cleanup(ch.name)
                    desired = _build_name(base, suffix)
                    await self._safe_rename(ch, desired, reason="TempVoice (auto)")
            except Exception as e:
                logger.warning("Renamer-Loop error: %r", e)
            await asyncio.sleep(20)  # etwas konservativer als der Cog

    # ---- Lifecycle -----------------------------------------------------------

    async def setup_hook(self) -> None:
        # Socket-Server separat im Hintergrund starten
        asyncio.create_task(self.start_socket_server())

    async def on_ready(self):
        logger.info("Bot eingeloggt als %s (ID: %s)", self.user, self.user.id if self.user else "?")
        if LIVE_MATCH_ENABLE:
            logger.info("LiveMatch-Renamer ist AKTIV (LIVE_MATCH_ENABLE=1) – nicht parallel zum Cog benutzen!")
            if not self._renamer_task or self._renamer_task.done():
                self._renamer_task = asyncio.create_task(self.live_match_runner())
        else:
            logger.info("Ops-only Modus: LiveMatch-Renamer ist AUS (LIVE_MATCH_ENABLE!=1) – Umbenennungen macht der Cog.")


# ====== Main =================================================================

def main() -> None:
    if not DISCORD_TOKEN:
        logger.error("Kein Token gefunden. Setze TEMPVOICE_TOKEN oder DISCORD_TOKEN.")
        raise SystemExit(1)
    bot = TempVoiceWorker()
    bot.run(DISCORD_TOKEN, log_handler=None)

if __name__ == "__main__":
    main()
