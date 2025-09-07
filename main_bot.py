# main_bot.py
# Deadlock Master Bot – sichere Secrets, zentrale DB, rekursive Cog-Auto-Discovery

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
import sys
import signal
import types
import builtins
from pathlib import Path
from typing import Dict, List, Tuple

import datetime as _dt
import pytz
import traceback

import discord
from discord.ext import commands

# =========================
# .env robust laden
# =========================
def _load_env_robust() -> str | None:
    """
    Lädt die .env robust:
      1) DOTENV_PATH, falls gesetzt
      2) Projektpfad (dieses File) / ".env"
      3) %USERPROFILE%/Documents/.env
    Loggt nur den Pfad, nicht den Inhalt.
    """
    try:
        from dotenv import load_dotenv
    except Exception:
        # dotenv nicht installiert -> nichts laden
        return None

    # Reihenfolge der Kandidaten
    candidates: List[Path] = []
    custom = os.getenv("DOTENV_PATH")
    if custom:
        candidates.append(Path(custom))

    here = Path(__file__).resolve()
    candidates.append(here.parent / ".env")
    # Windows-Documents (roh, keine \-Escapes)
    candidates.append(Path(os.path.expandvars(r"%USERPROFILE%")) / "Documents" / ".env")

    for p in candidates:
        try:
            if p.exists():
                load_dotenv(dotenv_path=str(p), override=False)
                logging.getLogger().info(f".env geladen: {p}")
                return str(p)
        except Exception:
            # still & safe
            pass
    return None


def _mask_tail(secret: str, keep: int = 4) -> str:
    if not secret:
        return ""
    s = str(secret)
    if len(s) <= keep:
        return "*" * len(s)
    return "*" * (len(s) - keep) + s[-keep:]


def _log_secret_present(name: str, env_keys: List[str], mode: str = "off") -> None:
    """
    Sichere Secret-Logger:
      mode = "off"      -> nie loggen (Default)
      mode = "present"  -> nur melden, dass Secret vorhanden ist, ohne Wert
      mode = "masked"   -> letzten 4 Zeichen maskiert loggen (nicht für Prod empfohlen)
    """
    try:
        val = None
        for k in env_keys:
            v = os.getenv(k)
            if v:
                val = v
                break
        if not val or mode == "off":
            return
        if mode == "present":
            logging.info("%s: vorhanden (Wert wird nicht geloggt)", name)
        elif mode == "masked":
            logging.info("%s: %s", name, _mask_tail(val))
    except Exception:
        # Niemals Exceptions werfen beim Secret-Logging
        pass


class _RedactSecretsFilter(logging.Filter):
    """
    Optionaler Redact-Filter: ersetzt bekannte Secret-Werte in *allen* Logs
    durch ***REDACTED***. Aktivierung via ENV: REDACT_SECRETS=1
    """
    def __init__(self, keys: List[str]):
        super().__init__()
        self.secrets = [os.getenv(k) for k in keys if os.getenv(k)]

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = str(record.getMessage())
            redacted = msg
            for s in self.secrets:
                if s and s in redacted:
                    redacted = redacted.replace(s, "***REDACTED***")
            # record.msg austauschen, args beibehalten
            record.msg = redacted
        except Exception:
            pass
        return True


# Früh Logging basic konfigurieren, um .env-Load zu sehen
logging.basicConfig(level=logging.INFO, handlers=[logging.StreamHandler(sys.stdout)])
_load_env_robust()  # einmalig; im setup_hook NICHT erneut


# =========================
# WorkerProxy Shim (fallback-sicher)
# =========================
def _install_workerproxy_shim():
    """
    Stellt sicher, dass:
      - builtins.WorkerProxy existiert
      - Modul 'shared.worker_client' existiert
    Falls echtes shared/worker_client.py existiert, wird das verwendet.
    """
    try:
        from shared.worker_client import WorkerProxy  # type: ignore
        setattr(builtins, "WorkerProxy", WorkerProxy)
        return
    except Exception:
        pass

    class _WorkerProxyStub:
        def __init__(self, *a, **kw): pass
        def request(self, *a, **kw): return {"ok": False, "error": "worker_stub"}
        def edit_channel(self, *a, **kw): return {"ok": False, "error": "worker_stub"}
        def set_permissions(self, *a, **kw): return {"ok": False, "error": "worker_stub"}
        def rename_match_suffix(self, *a, **kw): return {"ok": False, "error": "worker_stub"}
        def clear_match_suffix(self, *a, **kw): return {"ok": False, "error": "worker_stub"}
        def bulk(self, *a, **kw): return {"ok": False, "error": "worker_stub"}

    setattr(builtins, "WorkerProxy", _WorkerProxyStub)

    if "shared" not in sys.modules:
        sys.modules["shared"] = types.ModuleType("shared")
    if "shared.worker_client" not in sys.modules:
        wc_mod = types.ModuleType("shared.worker_client")
        setattr(wc_mod, "WorkerProxy", _WorkerProxyStub)
        sys.modules["shared.worker_client"] = wc_mod


_install_workerproxy_shim()


# =========================
# Zentrale DB Init (quiet)
# =========================
def _init_db_if_available():
    try:
        from shared import db as _db  # Deadlock-Bots/shared/db.py
        _db.connect()
        logging.info("Zentrale DB initialisiert (quiet).")
    except Exception as e:
        logging.warning(f"Zentrale DB nicht verfügbar (shared.db): {e}")


# =====================================================================
# MasterBot
# =====================================================================
class MasterBot(commands.Bot):
    """
    Master Discord Bot:
     - Rekursive Auto-Discovery (cogs/**.py)
     - Exclude Worker-Cogs
     - ENV-Filter: COG_EXCLUDE, COG_ONLY
     - sichere Secret-Logs
     - zentrale DB-Init
    """

    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.voice_states = True
        intents.guilds = True

        super().__init__(
            command_prefix=os.getenv("COMMAND_PREFIX", "!"),
            intents=intents,
            description="Master Bot System - Verwaltet alle Bot-Funktionen",
            owner_id=int(os.getenv("OWNER_ID", 0)),
            case_insensitive=True,
            chunk_guilds_at_startup=False,
            max_messages=1000,
            member_cache_flags=discord.MemberCacheFlags.from_intents(intents),
        )

        self.setup_logging()

        self.cogs_dir = Path(__file__).parent / "cogs"
        self.cogs_list: List[str] = []
        self.auto_discover_cogs()

        tz = pytz.timezone("Europe/Berlin")
        self.startup_time = _dt.datetime.now(tz=tz)
        self.cog_status: Dict[str, str] = {}

    # --------------------- Discovery & Filters -------------------------
    def _should_exclude(self, module_path: str) -> bool:
        """
        Exklusionslogik:
        - Default: Worker-Cogs aussperren (z. B. cogs.live_match.live_match_worker)
        - ENV COG_EXCLUDE="mod1,mod2"
        - ENV COG_ONLY -> Whitelist
        """
        default_excludes = {
            "cogs.live_match.live_match_worker",
        }

        env_ex = (os.getenv("COG_EXCLUDE") or "").strip()
        for item in [x.strip() for x in env_ex.split(",") if x.strip()]:
            default_excludes.add(item)

        only = {x.strip() for x in (os.getenv("COG_ONLY") or "").split(",") if x.strip()}
        if only:
            return module_path not in only

        if module_path in default_excludes:
            return True

        # Heuristik: alles mit "worker" im Modulnamen blocken
        if ".worker" in module_path or "worker." in module_path or module_path.endswith("_worker"):
            return True

        return False

    def auto_discover_cogs(self):
        """Rekursives Entdecken aller Cogs in cogs/"""
        try:
            if not self.cogs_dir.exists():
                logging.warning(f"Cogs directory not found: {self.cogs_dir}")
                return

            discovered: List[str] = []

            for cog_file in self.cogs_dir.rglob("*.py"):
                if cog_file.name.startswith("_") or cog_file.name == "__init__.py":
                    continue
                if any(part == "__pycache__" for part in cog_file.parts):
                    continue

                rel = cog_file.relative_to(self.cogs_dir.parent)
                module_path = ".".join(rel.with_suffix("").parts)

                try:
                    with open(cog_file, "r", encoding="utf-8") as f:
                        content = f.read()
                        looks_like_cog = (
                            "async def setup(" in content
                            or "def setup(" in content
                            or ("class " in content and "Cog" in content)
                        )
                        if not looks_like_cog:
                            logging.info(f"⏭️ Skipped {cog_file}: no setup/Cog detected")
                            continue
                except Exception as e:
                    logging.warning(f"⚠️ Error checking {cog_file.name}: {e}")
                    continue

                if self._should_exclude(module_path):
                    logging.info(f"🚫 Excluded cog: {module_path}")
                    continue

                discovered.append(module_path)
                logging.info(f"🔍 Auto-discovered cog: {module_path}")

            self.cogs_list = sorted(set(discovered))
            logging.info(f"✅ Auto-discovery complete: {len(self.cogs_list)} cogs found")

        except Exception as e:
            logging.error(f"❌ Error during cog auto-discovery: {e}")
            logging.error("❌ CRITICAL: No cogs will be loaded! Check cogs/ directory")
            self.cogs_list = []

    # --------------------- Logging ------------------------------------
    def setup_logging(self):
        """Logging (Rotation + optionaler Redact-Filter)"""
        log_dir = Path(__file__).parent / "logs"
        log_dir.mkdir(exist_ok=True)

        level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)

        root_handlers: List[logging.Handler] = [
            logging.handlers.RotatingFileHandler(
                log_dir / "master_bot.log",
                maxBytes=5 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            ),
            logging.StreamHandler(sys.stdout),
        ]

        logging.getLogger().handlers.clear()
        logging.basicConfig(level=level, handlers=root_handlers, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

        # Discord-Noise reduzieren
        logging.getLogger("discord").setLevel(logging.WARNING)
        logging.getLogger("discord.http").setLevel(logging.WARNING)

        # Optional: globale Redaction echter Secret-Werte
        if (os.getenv("REDACT_SECRETS") or "0") in ("1", "true", "TRUE", "yes", "YES"):
            redact_keys = [
                "DISCORD_TOKEN",
                "BOT_TOKEN",
                "RANK_BOT_TOKEN",
                "STEAM_API_KEY",
                "STEAM_WEB_API_KEY",
                "DISCORD_TOKEN_WORKER",
            ]
            flt = _RedactSecretsFilter(redact_keys)
            for h in logging.getLogger().handlers:
                h.addFilter(flt)

        logging.info("Master Bot logging initialized")

    # --------------------- Lifecycle ----------------------------------
    async def setup_hook(self):
        logging.info("Master Bot setup starting...")

        # Sichere Secret-Logs (Default: off)
        secret_mode = (os.getenv("SECRET_LOG_MODE") or "off").lower()
        _log_secret_present("Steam API Key", ["STEAM_API_KEY", "STEAM_WEB_API_KEY"], mode=secret_mode)
        _log_secret_present("Discord Token (Master)", ["DISCORD_TOKEN", "BOT_TOKEN"], mode="off")  # prinzipiell nie loggen

        # Zentrale DB initialisieren (falls verfügbar)
        _init_db_if_available()

        # Cogs laden
        await self.load_all_cogs()

        # Slash Commands syncen
        try:
            synced = await self.tree.sync()
            logging.info(f"Synced {len(synced)} slash commands")
        except Exception as e:
            logging.error(f"Failed to sync slash commands: {e}")

        logging.info("Master Bot setup completed")

    async def on_ready(self):
        logging.info(f"Bot logged in as {self.user} (ID: {self.user.id})")
        logging.info(f"Connected to {len(self.guilds)} guilds")

        activity = discord.Activity(
            type=discord.ActivityType.watching,
            name=f"{len(self.cogs_list)} Cogs | {os.getenv('COMMAND_PREFIX','!')}help",
        )
        await self.change_presence(activity=activity)

        loaded_cogs = [name for name, status in self.cog_status.items() if status == "loaded"]
        logging.info(f"Loaded cogs: {len(loaded_cogs)}/{len(self.cogs_list)}")

        try:
            tempvoice_cog = self.get_cog("TempVoiceCog")
            if tempvoice_cog and hasattr(tempvoice_cog, "create_channels"):
                cnt = len(getattr(tempvoice_cog, "create_channels") or [])
                logging.info(f"TempVoice ready with {cnt} create channels")
        except Exception:
            pass

        self.loop.create_task(self.hourly_health_check())

    async def load_all_cogs(self):
        logging.info("Loading all cogs in parallel...")

        async def load_single_cog(cog_name: str):
            try:
                await self.load_extension(cog_name)
                self.cog_status[cog_name] = "loaded"
                logging.info(f"✅ Loaded cog: {cog_name}")
                return True, cog_name, None
            except Exception as e:
                self.cog_status[cog_name] = f"error: {str(e)[:100]}"
                logging.error(f"❌ Failed to load cog {cog_name}: {e}")
                return False, cog_name, e

        tasks = [load_single_cog(c) for c in self.cogs_list]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        ok = 0
        for r in results:
            if isinstance(r, tuple) and r[0]:
                ok += 1
            elif isinstance(r, Exception):
                logging.error(f"❌ Unexpected error during cog loading: {r}")

        logging.info(f"Parallel cog loading completed: {ok}/{len(self.cogs_list)} successful")

    async def reload_all_cogs_with_discovery(self):
        try:
            unload_results = []
            loaded_extensions = [ext for ext in list(self.extensions.keys()) if ext.startswith("cogs.")]

            for ext_name in loaded_extensions:
                try:
                    await self.unload_extension(ext_name)
                    unload_results.append(f"✅ Unloaded: {ext_name}")
                    logging.info(f"Unloaded extension: {ext_name}")
                except Exception as e:
                    unload_results.append(f"❌ Error unloading {ext_name}: {str(e)[:50]}")
                    logging.error(f"Error unloading {ext_name}: {e}")

            old_count = len(self.cogs_list)
            self.auto_discover_cogs()
            new_count = len(self.cogs_list)

            self.cog_status = {}
            await self.load_all_cogs()

            loaded_count = len([s for s in self.cog_status.values() if s == "loaded"])

            summary = {
                "unloaded": len(unload_results),
                "discovered": new_count,
                "loaded": loaded_count,
                "new_cogs": new_count - old_count,
                "unload_details": unload_results,
            }
            return True, summary

        except Exception as e:
            logging.error(f"Error during full cog reload: {e}")
            return False, f"Error: {str(e)}"

    async def reload_cog(self, cog_name: str) -> Tuple[bool, str]:
        try:
            await self.reload_extension(cog_name)
            self.cog_status[cog_name] = "loaded"
            msg = f"✅ Successfully reloaded {cog_name}"
            logging.info(msg)
            return True, msg
        except commands.ExtensionNotLoaded:
            try:
                await self.load_extension(cog_name)
                self.cog_status[cog_name] = "loaded"
                msg = f"✅ Loaded {cog_name} (was not loaded before)"
                logging.info(msg)
                return True, msg
            except Exception as e:
                err = f"❌ Failed to load {cog_name}: {str(e)[:200]}"
                self.cog_status[cog_name] = f"error: {str(e)[:100]}"
                logging.error(err)
                return False, err
        except Exception as e:
            err = f"❌ Failed to reload {cog_name}: {str(e)[:200]}"
            self.cog_status[cog_name] = f"error: {str(e)[:100]}"
            logging.error(err)
            return False, err

    async def hourly_health_check(self):
        critical_check_interval = 3600  # 1h
        last_critical_check = 0.0

        while not self.is_closed():
            try:
                await asyncio.sleep(300)
                current = asyncio.get_event_loop().time()

                if current - last_critical_check >= critical_check_interval:
                    issues = []

                    if not self.get_cog("TempVoiceCog"):
                        issues.append("TempVoice not loaded")
                    if not self.get_cog("VoiceActivityTrackerCog"):
                        issues.append("VoiceActivityTracker not loaded")
                    if "cogs.live_match.live_match_master" not in self.extensions:
                        issues.append("LiveMatchMaster (module) not loaded")

                    if issues:
                        logging.warning(f"Critical Health Check: Issues found: {issues}")
                    else:
                        logging.info("Critical Health Check: Core cogs operational")

                    last_critical_check = current

            except Exception as e:
                logging.error(f"Health check error: {e}")

    async def close(self):
        logging.info("Master Bot shutting down...")
        for ext_name in [ext for ext in list(self.extensions.keys()) if ext.startswith("cogs.")]:
            try:
                await self.unload_extension(ext_name)
                logging.info(f"Unloaded extension: {ext_name}")
            except Exception as e:
                logging.error(f"Error unloading extension {ext_name}: {e}")

        await super().close()
        logging.info("Master Bot shutdown complete")


def is_bot_owner():
    async def predicate(ctx):
        return ctx.author.id == ctx.bot.owner_id
    return commands.check(predicate)


class MasterControlCog(commands.Cog):
    """Master control commands for bot management"""

    def __init__(self, bot: MasterBot):
        self.bot = bot

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
                f"`{p}master discover` - Neue Cogs entdecken (ohne laden)\n"
                f"`{p}master shutdown` - Bot beenden"
            ),
            inline=False,
        )
        await ctx.send(embed=embed)

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

        loaded_cogs = []
        error_cogs = []
        for cog_name, status in self.bot.cog_status.items():
            short = cog_name.split(".")[-1]
            if status == "loaded":
                loaded_cogs.append(f"✅ {short}")
            else:
                error_cogs.append(f"❌ {short}")

        if loaded_cogs:
            embed.add_field(
                name=f"📦 Loaded Cogs ({len(loaded_cogs)})",
                value="\n".join(loaded_cogs),
                inline=True,
            )
        if error_cogs:
            embed.add_field(
                name=f"⚠️ Error Cogs ({len(error_cogs)})",
                value="\n".join(error_cogs),
                inline=True,
            )
        await ctx.send(embed=embed)

    @master_control.command(name="reload", aliases=["rl"])
    async def master_reload(self, ctx, cog_name: str = None):
        if cog_name:
            matches = [c for c in self.bot.cogs_list if cog_name.lower() in c.lower()]
            if not matches:
                await ctx.send(f"❌ Cog '{cog_name}' nicht gefunden!")
                return
            target = matches[0]
            ok, msg = await self.bot.reload_cog(target)
            embed = discord.Embed(title="🔄 Cog Reload", description=msg, color=0x00FF00 if ok else 0xFF0000)
            await ctx.send(embed=embed)
        else:
            await ctx.send(
                "❌ Bitte Cog-Namen angeben! Verfügbar:\n" + "\n".join([c.split(".")[-1] for c in self.bot.cogs_list])
            )

    @master_control.command(name="reloadall", aliases=["rla"])
    async def master_reload_all(self, ctx):
        embed = discord.Embed(
            title="🔄 Alle Cogs neu laden (Auto-Discovery)",
            description="Entdecke neue Cogs und lade alle neu...",
            color=0xFFAA00,
        )
        msg = await ctx.send(embed=embed)

        ok, result = await self.bot.reload_all_cogs_with_discovery()
        if ok:
            summary = result
            final = discord.Embed(
                title="🔄 Auto-Reload Abgeschlossen",
                description=f"**{summary['loaded']}/{summary['discovered']}** Cogs erfolgreich geladen",
                color=0x00FF00 if summary["loaded"] == summary["discovered"] else 0xFFAA00,
            )
            if summary["new_cogs"] > 0:
                final.add_field(name="🆕 Neue Cogs", value=f"{summary['new_cogs']} neue Cogs automatisch entdeckt!", inline=False)
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
            loaded_cogs = [n.split(".")[-1] for n, s in self.bot.cog_status.items() if s == "loaded"]
            if loaded_cogs:
                final.add_field(name="✅ Aktive Cogs", value="\n".join([f"• {c}" for c in loaded_cogs]), inline=True)
        else:
            final = discord.Embed(title="❌ Auto-Reload Fehlgeschlagen", description=str(result), color=0xFF0000)

        await msg.edit(embed=final)

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
            embed.add_field(name="🆕 Neue Cogs gefunden", value="\n".join([f"• {c.split('.')[-1]}" for c in new]), inline=True)
            embed.color = 0x00FF00
        else:
            embed.add_field(name="ℹ️ Status", value="Keine neuen Cogs gefunden", inline=True)

        embed.add_field(
            name="📋 Alle entdeckten Cogs",
            value="\n".join([f"• {c.split('.')[-1]}" for c in self.bot.cogs_list]),
            inline=False,
        )
        await ctx.send(embed=embed)

    @master_control.command(name="shutdown", aliases=["stop", "quit"])
    async def master_shutdown(self, ctx):
        embed = discord.Embed(title="🛑 Master Bot wird beendet", description="Bot fährt herunter...", color=0xFF0000)
        await ctx.send(embed=embed)
        logging.info(f"Shutdown initiated by {ctx.author}")
        await self.bot.close()


# =====================================================================
# main
# =====================================================================
async def main():
    bot = MasterBot()
    await bot.add_cog(MasterControlCog(bot))

    def _sig_handler(signum, frame):
        logging.info(f"Received signal {signum}, shutting down gracefully...")
        asyncio.create_task(bot.close())

    try:
        signal.signal(signal.SIGINT, _sig_handler)
        signal.signal(signal.SIGTERM, _sig_handler)
    except Exception:
        # Windows kann SIGTERM/Signals einschränken – ignorieren
        pass

    token = os.getenv("DISCORD_TOKEN") or os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN fehlt in ENV/.env")

    try:
        await bot.start(token)
    except KeyboardInterrupt:
        logging.info("Keyboard interrupt received, shutting down...")
    except Exception as e:
        logging.error(f"Bot crashed: {e}")
        logging.error(traceback.format_exc())
    finally:
        if not bot.is_closed():
            await bot.close()


if __name__ == "__main__":
    asyncio.run(main())
