"""Administrative command group for the Twitch cog."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

import discord
from discord.ext import commands

from . import storage
from .logger import log


class TwitchAdminMixin:
    """Hybrid command group /twitch [...] including helpers."""

    @commands.hybrid_group(name="twitch", with_app_command=True)
    @commands.has_guild_permissions(manage_guild=True)
    async def twitch_group(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await ctx.send("Subcommands: add, remove, list, channel, forcecheck, invites")

    @twitch_group.command(name="channel")
    @commands.has_guild_permissions(manage_guild=True)
    async def twitch_channel(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None):
        channel = channel or ctx.channel
        try:
            self._set_channel(ctx.guild.id, channel.id)
            await ctx.reply(f"Live-Posts gehen jetzt in {channel.mention}")
        except Exception:
            log.exception("Konnte Twitch-Channel speichern")
            await ctx.reply("Konnte Kanal nicht speichern.")

    @twitch_group.command(name="add")
    @commands.has_guild_permissions(manage_guild=True)
    async def twitch_add(self, ctx: commands.Context, login: str, require_discord_link: Optional[bool] = False):
        try:
            msg = await self._cmd_add(login, bool(require_discord_link))
        except Exception:
            log.exception("twitch add fehlgeschlagen")
            await ctx.reply("Fehler beim Hinzufügen.")
            return
        await ctx.reply(msg)

    @twitch_group.command(name="remove")
    @commands.has_guild_permissions(manage_guild=True)
    async def twitch_remove(self, ctx: commands.Context, login: str):
        try:
            msg = await self._cmd_remove(login)
        except Exception:
            log.exception("twitch remove fehlgeschlagen")
            await ctx.reply("Fehler beim Entfernen.")
            return
        await ctx.reply(msg)

    @twitch_group.command(name="list")
    @commands.has_guild_permissions(manage_guild=True)
    async def twitch_list(self, ctx: commands.Context):
        try:
            with storage.get_conn() as c:
                rows = c.execute(
                    "SELECT twitch_login, manual_verified_permanent, manual_verified_until FROM twitch_streamers ORDER BY twitch_login"
                ).fetchall()
        except Exception:
            log.exception("Konnte Streamer-Liste aus DB lesen")
            await ctx.reply("Fehler beim Lesen der Streamer-Liste.")
            return

        if not rows:
            await ctx.reply("Keine Streamer gespeichert.")
            return

        def _fmt(row: dict) -> str:
            until = row.get("manual_verified_until")
            perm = bool(row.get("manual_verified_permanent"))
            tail = " (permanent verifiziert)" if perm else (f" (verifiziert bis {until})" if until else "")
            return f"- {row.get('twitch_login','?')}{tail}"

        try:
            lines = [_fmt(dict(r)) for r in rows]
            await ctx.reply("\n".join(lines)[:1900])
        except Exception:
            log.exception("Fehler beim Formatieren der Streamer-Liste")
            await ctx.reply("Fehler beim Anzeigen der Liste.")

    @twitch_group.command(name="forcecheck")
    @commands.has_guild_permissions(manage_guild=True)
    async def twitch_forcecheck(self, ctx: commands.Context):
        await ctx.reply("Prüfe jetzt…")
        try:
            await self._tick()
        except Exception:
            log.exception("Forcecheck fehlgeschlagen")
            await ctx.send("Fehler beim Forcecheck.")

    @twitch_group.command(name="invites")
    @commands.has_guild_permissions(manage_guild=True)
    async def twitch_invites(self, ctx: commands.Context):
        try:
            await self._refresh_guild_invites(ctx.guild)
            codes = sorted(self._invite_codes.get(ctx.guild.id, set()))
            if not codes:
                await ctx.reply("Keine aktiven Einladungen gefunden.")
            else:
                urls = [f"https://discord.gg/{code}" for code in codes]
                await ctx.reply("Aktive Einladungen:\n" + "\n".join(urls)[:1900])
        except Exception:
            log.exception("Konnte Einladungen nicht abrufen")
            await ctx.reply("Fehler beim Abrufen der Einladungen.")

    # -------------------------------------------------------
    # Admin-Commands: Add/Remove Helpers
    # -------------------------------------------------------
    @staticmethod
    def _parse_db_datetime(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    @classmethod
    def _is_partner_verified(cls, row: Dict[str, Any], now_utc: datetime) -> bool:
        try:
            if bool(row.get("manual_verified_permanent")):
                return True
        except Exception:
            pass

        until_raw = row.get("manual_verified_until")
        until_dt = cls._parse_db_datetime(str(until_raw)) if until_raw else None
        if until_dt and until_dt >= now_utc:
            return True
        return False

    async def _cmd_add(self, login: str, require_link: bool) -> str:
        if self.api is None:
            return "API nicht konfiguriert"

        normalized = self._normalize_login(login)
        if not normalized:
            return "Ungültiger Twitch-Login"

        users = await self.api.get_users([normalized])
        user = users.get(normalized)
        if not user:
            return "Unbekannter Twitch-Login"

        try:
            with storage.get_conn() as c:
                c.execute(
                    "INSERT OR IGNORE INTO twitch_streamers "
                    "(twitch_login, twitch_user_id, require_discord_link, next_link_check_at) "
                    "VALUES (?, ?, ?, datetime('now','+30 days'))",
                    (user["login"].lower(), user["id"], int(require_link)),
                )
                c.execute(
                    "UPDATE twitch_streamers "
                    "SET manual_verified_permanent=0, manual_verified_until=NULL, manual_verified_at=NULL "
                    "WHERE twitch_login=?",
                    (normalized,),
                )
        except Exception:
            log.exception("DB-Fehler beim Hinzufügen von %s", normalized)
            return "Datenbankfehler beim Hinzufügen."

        return f"{user['display_name']} hinzugefügt"

    async def _cmd_remove(self, login: str) -> str:
        normalized = self._normalize_login(login)
        if not normalized:
            return "Ungültiger Twitch-Login"

        deleted = 0
        try:
            with storage.get_conn() as c:
                cur = c.execute("DELETE FROM twitch_streamers WHERE twitch_login=?", (normalized,))
                deleted = cur.rowcount or 0
                c.execute("DELETE FROM twitch_live_state WHERE streamer_login=?", (normalized,))
        except Exception:
            log.exception("DB-Fehler beim Entfernen von %s", normalized)
            return "Datenbankfehler beim Entfernen."

        if deleted:
            return f"{normalized} entfernt"
        return f"{normalized} war nicht gespeichert"
