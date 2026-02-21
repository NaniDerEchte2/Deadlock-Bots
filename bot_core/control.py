from __future__ import annotations

import datetime as _dt
import logging
import os
from typing import TYPE_CHECKING

import discord
import pytz
from discord.ext import commands

if TYPE_CHECKING:
    from bot_core.lifecycle import BotLifecycle
    from bot_core.master_bot import MasterBot

__all__ = ["MasterControlCog", "is_bot_owner"]


def is_bot_owner():
    async def predicate(ctx):
        return ctx.author.id == ctx.bot.owner_id

    return commands.check(predicate)


class MasterControlCog(commands.Cog):
    """Master control commands for bot management"""

    def __init__(self, bot: MasterBot):
        self.bot = bot
        self.lifecycle: BotLifecycle | None = getattr(bot, "lifecycle", None)

    @commands.group(name="master", invoke_without_command=True, aliases=["m"])
    @is_bot_owner()
    async def master_control(self, ctx):
        p = os.getenv("COMMAND_PREFIX", "!")
        embed = discord.Embed(
            title="🤖 Master Bot Kontrolle",
            description="Verwalte alle Bot-Cogs und Systeme",
            color=0x0099FF,
        )
        embed.add_field(
            name="📋 Master Commands",
            value=(
                f"`{p}master status` - Bot Status\n"
                f"`{p}master reload [cog]` - Cog neu laden\n"
                f"`{p}master reloadall` - Alle Cogs neu laden + Auto-Discovery\n"
                f"`{p}master reloadsteam` - Alle Steam-Cogs neu laden (Ordner)\n"
                f"`{p}master discover` - Neue Cogs entdecken (ohne laden)\n"
                f"`{p}master unload <muster>` - Cogs mit Muster entladen\n"
                f"`{p}master unloadtree <prefix>` - ganzen Cog-Ordner entladen\n"
                f"`{p}master restart` - Bot sauber neu starten\n"
                f"`{p}master shutdown` - Bot beenden"
            ),
            inline=False,
        )
        await ctx.send(embed=embed)

    def _format_timestamp(self, ts: float | None) -> str:
        if not ts:
            return "—"
        tz = self.bot.startup_time.tzinfo or pytz.timezone("Europe/Berlin")
        return _dt.datetime.fromtimestamp(ts, tz=tz).strftime("%d.%m.%Y %H:%M:%S")

    @master_control.command(name="status", aliases=["s"])
    async def master_status(self, ctx):
        embed = discord.Embed(
            title="📊 Master Bot Status",
            description=f"Bot läuft seit: {self.bot.startup_time.strftime('%d.%m.%Y %H:%M:%S')}",
            color=0x00FF00,
        )
        embed.add_field(
            name="🔧 System",
            value=(
                f"Guilds: {len(self.bot.guilds)}\n"
                f"Users: {len(set(self.bot.get_all_members()))}\n"
                f"Commands: {len(self.bot.commands)}"
            ),
            inline=True,
        )

        # NEU: echte Runtime-Extensions
        active = self.bot.active_cogs()
        discovered = set(self.bot.cogs_list)
        inactive = sorted(list(discovered - set(active)))

        if active:
            short = [f"✅ {a.split('.')[-1]}" for a in active]
            embed.add_field(
                name=f"📦 Loaded Cogs ({len(active)})",
                value="\n".join(short),
                inline=True,
            )

        if inactive:
            short_inactive = [f"• {a.split('.')[-1]}" for a in inactive]
            embed.add_field(
                name=f"🗂️ Inaktiv/Entdeckt ({len(inactive)})",
                value="\n".join(short_inactive),
                inline=True,
            )

        # Optional: zeig fehlerhafte Ladeversuche aus letzter Runde
        errs = [
            k
            for k, v in self.bot.cog_status.items()
            if isinstance(v, str) and v.startswith("error:")
        ]
        if errs:
            err_short = [f"❌ {e.split('.')[-1]}" for e in errs]
            embed.add_field(
                name="⚠️ Fehlerhafte Cogs (letzter Versuch)",
                value="\n".join(err_short),
                inline=False,
            )

        await ctx.send(embed=embed)

    @master_control.command(name="reload", aliases=["rl"])
    async def master_reload(self, ctx, cog_name: str = None):
        if not cog_name:
            await ctx.send(
                "❌ Bitte Cog-Namen angeben! Verfügbar:\n"
                + "\n".join([c.split(".")[-1] for c in self.bot.cogs_list])
            )
            return

        self.bot.auto_discover_cogs()
        target, collisions = self.bot.resolve_cog_identifier(cog_name)
        if not target:
            if collisions:
                options = "\n".join(f"• {c}" for c in collisions[:10])
                if len(collisions) > 10:
                    options += "\n…"
                await ctx.send(
                    f"❌ Mehrdeutiger Cog-Name `{cog_name}`. Bitte präzisieren:\n{options}"
                )
            else:
                await ctx.send(f"❌ Cog `{cog_name}` nicht gefunden!")
            return

        ok, msg = await self.bot.reload_cog(target)
        embed = discord.Embed(
            title="🔄 Cog Reload", description=msg, color=0x00FF00 if ok else 0xFF0000
        )
        await ctx.send(embed=embed)

    @master_control.command(name="reloadall", aliases=["rla"])
    async def master_reload_all(self, ctx):
        embed = discord.Embed(
            title="🔄 Alle Cogs neu laden (Auto-Discovery)",
            description="Entdecke neue Cogs und lade alle neu...",
            color=0xFFAA00,
        )
        msg = await ctx.send(embed=embed)

        ok, result = await self.bot.reload_all_cogs_with_discovery()
        await self.bot.update_presence()

        if ok:
            summary = result
            final = discord.Embed(
                title="🔄 Auto-Reload Abgeschlossen",
                description=f"**{summary['loaded']}/{summary['discovered']}** Cogs erfolgreich geladen",
                color=0x00FF00 if summary["loaded"] == summary["discovered"] else 0xFFAA00,
            )
            if summary["new_cogs"] > 0:
                final.add_field(
                    name="🆕 Neue Cogs",
                    value=f"{summary['new_cogs']} neue Cogs automatisch entdeckt!",
                    inline=False,
                )
            final.add_field(
                name="📊 Summary",
                value=(
                    f"Entladen: {summary['unloaded']}\n"
                    f"Entdeckt: {summary['discovered']}\n"
                    f"Geladen: {summary['loaded']}\n"
                    f"Neu: {summary['new_cogs']}"
                ),
                inline=True,
            )
            loaded_cogs = [n.split(".")[-1] for n in self.bot.active_cogs()]
            if loaded_cogs:
                final.add_field(
                    name="✅ Aktive Cogs",
                    value="\n".join([f"• {c}" for c in loaded_cogs]),
                    inline=True,
                )
        else:
            final = discord.Embed(
                title="❌ Auto-Reload Fehlgeschlagen",
                description=str(result),
                color=0xFF0000,
            )

        await msg.edit(embed=final)

    @master_control.command(
        name="reloadsteam",
        aliases=["rllm", "reload_livematch", "reload_lm", "reloadlive"],
    )
    async def master_reload_steam_folder(self, ctx):
        results = await self.bot.reload_steam_folder()

        ok = [k for k, v in results.items() if v in ("reloaded", "loaded")]
        err = {k: v for k, v in results.items() if v.startswith("error:")}

        embed = discord.Embed(
            title="🎯 Reload: cogs/steam",
            description="Alle Steam-Cogs neu geladen.",
            color=0x00FF00 if not err else 0xFFAA00,
        )
        if ok:
            embed.add_field(
                name="✅ Erfolgreich",
                value="\n".join(f"• {k.split('.')[-1]} ({results[k]})" for k in ok),
                inline=False,
            )
        if err:
            embed.add_field(
                name="⚠️ Fehler",
                value="\n".join(f"• {k.split('.')[-1]}: {v}" for k, v in err.items()),
                inline=False,
            )

        await ctx.send(embed=embed)

    @master_control.command(name="discover", aliases=["disc"])
    async def master_discover(self, ctx):
        old_count = len(self.bot.cogs_list)
        old = self.bot.cogs_list.copy()
        self.bot.auto_discover_cogs()
        new_count = len(self.bot.cogs_list)
        new = [c for c in self.bot.cogs_list if c not in old]

        embed = discord.Embed(title="🔍 Cog Discovery", color=0x00FFFF)
        embed.add_field(
            name="📊 Ergebnis",
            value=f"Vorher: {old_count} Cogs\nJetzt: {new_count} Cogs\nNeue: {len(new)} Cogs",
            inline=True,
        )
        if new:
            embed.add_field(
                name="🆕 Neue Cogs gefunden",
                value="\n".join([f"• {c.split('.')[-1]}" for c in new]),
                inline=True,
            )
            embed.color = 0x00FF00
        else:
            embed.add_field(name="ℹ️ Status", value="Keine neuen Cogs gefunden", inline=True)

        embed.add_field(
            name="📋 Alle entdeckten Cogs",
            value="\n".join([f"• {c.split('.')[-1]}" for c in self.bot.cogs_list]),
            inline=False,
        )
        await ctx.send(embed=embed)

    @master_control.command(name="unload", aliases=["ul"])
    async def master_unload(self, ctx, *, pattern: str):
        """
        Entlädt alle geladenen Cogs deren Modulpfad <pattern> matcht.
        Beispiele:
          !master unload tempvoice
          !master unload cogs.steam.steam_link_oauth
        """
        matches = self.bot._match_extensions(pattern)
        if not matches:
            await ctx.send(f"❌ Keine geladenen Cogs gefunden für Muster: `{pattern}`")
            return
        results = await self.bot.unload_many(matches)
        await self.bot.update_presence()

        ok = [k for k, v in results.items() if v == "unloaded"]
        timeouts = [k for k, v in results.items() if v == "timeout"]
        errs = {k: v for k, v in results.items() if v not in ("unloaded", "timeout")}

        embed = discord.Embed(
            title=f"🧹 Unload Resultate ({pattern})",
            color=0x00FF00 if ok and not timeouts and not errs else 0xFFAA00 if ok else 0xFF0000,
        )
        if ok:
            embed.add_field(name="✅ Entladen", value="\n".join(f"• {x}" for x in ok), inline=False)
        if timeouts:
            embed.add_field(
                name="⏱️ Timeouts",
                value="\n".join(f"• {x}" for x in timeouts),
                inline=False,
            )
        if errs:
            embed.add_field(
                name="⚠️ Fehler",
                value="\n".join(f"• {k}: {v}" for k, v in errs.items()),
                inline=False,
            )
        await ctx.send(embed=embed)

    @master_control.command(name="unloadtree", aliases=["ult"])
    async def master_unload_tree(self, ctx, *, prefix: str):
        """
        Entlädt ALLE Cogs unterhalb eines Prefix/Ordners.
        Beispiele:
          !master unloadtree steam
          !master unloadtree cogs.tempvoice
        """
        pref = prefix.strip()
        if not pref.startswith("cogs."):
            pref = f"cogs.{pref}"
        matches = [ext for ext in self.bot.extensions.keys() if ext.startswith(pref)]
        if not matches:
            await ctx.send(f"❌ Kein geladener Cog unter Prefix: `{pref}`")
            return
        results = await self.bot.unload_many(matches)
        await self.bot.update_presence()

        ok = [k for k, v in results.items() if v == "unloaded"]
        timeouts = [k for k, v in results.items() if v == "timeout"]
        errs = {k: v for k, v in results.items() if v not in ("unloaded", "timeout")}

        embed = discord.Embed(
            title=f"🌲 Unload-Tree Resultate ({pref})",
            color=0x00FF00 if ok and not timeouts and not errs else 0xFFAA00 if ok else 0xFF0000,
        )
        if ok:
            embed.add_field(name="✅ Entladen", value="\n".join(f"• {x}" for x in ok), inline=False)
        if timeouts:
            embed.add_field(
                name="⏱️ Timeouts",
                value="\n".join(f"• {x}" for x in timeouts),
                inline=False,
            )
        if errs:
            embed.add_field(
                name="⚠️ Fehler",
                value="\n".join(f"• {k}: {v}" for k, v in errs.items()),
                inline=False,
            )
        await ctx.send(embed=embed)

    @master_control.command(name="restart", aliases=["reboot"])
    async def master_restart(self, ctx):
        """
        Löst einen sauberen Neustart aus, gesteuert vom Lifecycle-Supervisor.
        """
        lifecycle = self.lifecycle
        if not lifecycle:
            await ctx.send("❌ Neustart nicht verfügbar (kein Lifecycle angebunden).")
            return

        embed = discord.Embed(
            title="🔁 Bot-Neustart",
            description="Restart wird vorbereitet...",
            color=0x00AAFF,
        )
        msg = await ctx.send(embed=embed)

        scheduled = await lifecycle.request_restart(reason=f"command:{ctx.author.id}")
        if scheduled:
            embed.description = (
                "Restart angefordert. Der Bot trennt gleich die Verbindung und startet neu."
            )
            embed.color = 0x00FF00
        else:
            embed.description = "Restart konnte nicht geplant werden (evtl. läuft bereits einer)."
            embed.color = 0xFF0000
        await msg.edit(embed=embed)

    @master_control.command(name="shutdown", aliases=["stop", "quit"])
    async def master_shutdown(self, ctx):
        embed = discord.Embed(
            title="🛑 Master Bot wird beendet",
            description="Bot fährt herunter...",
            color=0xFF0000,
        )
        await ctx.send(embed=embed)
        logging.info(f"Shutdown initiated by {ctx.author}")
        await self.bot.close()


# =====================================================================
# Graceful Shutdown (Timeout + Doppel-SIGINT + harter Fallback)
# =====================================================================
