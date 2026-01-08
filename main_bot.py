# main_bot.py — angepasst: Service-Manager wird NUR noch vom Cog gesteuert
from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
import os
import sys
import signal
import types
import builtins
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import datetime as _dt
import pytz
import traceback

import importlib
import inspect
import hashlib

import discord
from discord.ext import commands
from service.config import settings
from service.http_client import build_resilient_connector
# --- DEBUG: Herkunft der geladenen Dateien ausgeben ---
import re
from service import db # Import db for worker communication

try:
    from service.dashboard import DashboardServer
except Exception as _dashboard_import_error:
    DashboardServer = None  # type: ignore[assignment]
    logging.getLogger(__name__).warning(
        "Dashboard module unavailable: %s", _dashboard_import_error
    )

try:
    from service.standalone_manager import StandaloneBotConfig, StandaloneBotManager
except Exception as _standalone_import_error:
    StandaloneBotConfig = None  # type: ignore[assignment]
    StandaloneBotManager = None  # type: ignore[assignment]
    logging.getLogger(__name__).warning(
        "Standalone manager unavailable: %s", _standalone_import_error
    )

def _log_src(modname: str):
    try:
        m = importlib.import_module(modname)
        path = inspect.getfile(m)
        with open(path, 'rb') as fh:
            sha = hashlib.sha1(fh.read()).hexdigest()[:12]
        logging.getLogger().info("SRC %s -> %s [sha1:%s]", modname, path, sha)
    except Exception as e:
        logging.getLogger().error("SRC %s -> %r", modname, e)

logging.getLogger().info("PYTHON exe=%s", sys.executable)
logging.getLogger().info("CWD=%s", os.getcwd())
logging.getLogger().info("sys.path[0]=%s", sys.path[0] if sys.path else None)

# prüfe gezielt die „verdächtigen“
for name in [
    "cogs.rules_channel",
    "cogs.welcome_dm.dm_main",
    "cogs.welcome_dm.step_streamer",
    "cogs.welcome_dm.step_steam_link",
]:
    _log_src(name)
# --- /DEBUG ---

# =========================
# .env robust laden
# =========================
def _load_env_robust() -> str | None:
    try:
        from dotenv import load_dotenv
    except Exception as e:
        logging.getLogger().debug("dotenv nicht verfügbar/fehlgeschlagen: %r", e)
        return None

    candidates: List[Path] = []
    custom = os.getenv("DOTENV_PATH")
    if custom:
        candidates.append(Path(custom))

    here = Path(__file__).resolve()
    candidates.append(here.parent / ".env")
    candidates.append(Path(os.path.expandvars(r"%USERPROFILE%")) / "Documents" / ".env")

    for p in candidates:
        try:
            if p.exists():
                load_dotenv(dotenv_path=str(p), override=False)
                logging.getLogger().info(f".env geladen: {p}")
                return str(p)
        except Exception as e:
            logging.getLogger().debug("Konnte .env nicht laden (%s): %r", p, e)
    return None


def _mask_tail(secret: str, keep: int = 4) -> str:
    if not secret:
        return ""
    s = str(secret)
    if len(s) <= keep:
        return "*" * len(s)
    return "*" * (len(s) - keep) + s[-keep:]


def _log_secret_present(name: str, env_keys: List[str], mode: str = "off") -> None:
    try:
        val = None
        for k in env_keys:
            v = os.getenv(k)
            if v:
                val = v
                break
        if not val or mode == "off":
            return
        if mode in ("present", "masked"):
            logging.info("%s: vorhanden (Wert wird nicht geloggt)", name)
    except Exception as e:
        logging.getLogger().debug("Secret-Check fehlgeschlagen (%s): %r", name, e)


class _RedactSecretsFilter(logging.Filter):
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
            record.msg = redacted
        except Exception as e:
            logging.getLogger().debug("RedactSecretsFilter Fehler (ignoriert): %r", e)
        return True


logging.basicConfig(level=logging.INFO, handlers=[logging.StreamHandler(sys.stdout)])
_load_env_robust()

# =========================
# WorkerProxy Shim (fallback-sicher)
# =========================
def _install_workerproxy_shim():
    try:
        from shared.worker_client import WorkerProxy  # type: ignore
        setattr(builtins, "WorkerProxy", WorkerProxy)
        return
    except Exception as e:
        logging.getLogger().info("WorkerProxy nicht verfügbar – verwende Stub: %r", e)

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
        wc_mod = types.ModuleType("shared") # Fixed module name
        setattr(wc_mod, "WorkerProxy", _WorkerProxyStub)
        sys.modules["shared.worker_client"] = wc_mod # Fixed module name

_install_workerproxy_shim()

# =========================
# Zentrale DB Init (quiet)
# =========================
def _init_db_if_available():
    try:
        from service import db as _db  # Deadlock-Bots/service/db.py
    except Exception as e:
        logging.critical("Zentrale DB-Modul 'service.db' konnte nicht importiert werden: %s", e)
        return
    try:
        _db.connect()
        logging.info("Zentrale DB initialisiert (quiet) via service.db.")
    except Exception as e:
        logging.critical("Zentrale DB (service.db) konnte nicht initialisiert werden: %s", e)


# =====================================================================
# MasterBot
# =====================================================================
class MasterBot(commands.Bot):
    """
    Master Discord Bot mit:
     - Präziser Auto-Discovery
     - Exclude Worker-Cogs
     - ENV-Filter: COG_EXCLUDE, COG_ONLY
     - zentrale DB-Init
     - Timeout-gestütztem Cog-Unload
     - KORREKTER Runtime-Status (self.extensions) + Presence-Updates
     - Deadlock-Presence läuft vollständig im Cog (kein externer Service nötig)
    """

    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.voice_states = True
        intents.guilds = True

        connector = build_resilient_connector()

        super().__init__(
            command_prefix=settings.command_prefix,
            intents=intents,
            description="Master Bot System - Verwaltet alle Bot-Funktionen",
            owner_id=settings.owner_id,
            case_insensitive=True,
            chunk_guilds_at_startup=False,
            max_messages=1000,
            member_cache_flags=discord.MemberCacheFlags.from_intents(intents),
            connector=connector,
        )

        self.setup_logging()

        self.cogs_dir = Path(__file__).parent / "cogs"
        blocklist_path = os.getenv("COG_BLOCKLIST_FILE")
        if blocklist_path:
            self.blocklist_path = Path(blocklist_path)
        else:
            self.blocklist_path = self.cogs_dir.parent / "cog_blocklist.json"
        self.blocked_namespaces: Set[str] = set()
        self._load_blocklist()

        self.cogs_list: List[str] = []
        self.cog_status: Dict[str, str] = {}
        self.auto_discover_cogs()

        tz = pytz.timezone("Europe/Berlin")
        self.startup_time = _dt.datetime.now(tz=tz)

        # Dashboard is now loaded as a Cog (cogs/dashboard_cog.py)
        # This allows it to be reloaded without restarting the bot
        self.dashboard: Optional[DashboardServer] = None  # Set by DashboardCog
        self._dashboard_start_task: Optional[asyncio.Task[None]] = None  # Legacy, kept for compatibility

        self.standalone_manager: Optional[StandaloneBotManager] = None
        if StandaloneBotManager is not None and StandaloneBotConfig is not None:
            try:
                repo_root = Path(__file__).resolve().parent
                standalone_dir = repo_root / "standalone"
                rank_script = standalone_dir / "rank_bot.py"
                custom_env: Dict[str, str] = {}
                pythonpath_entries: List[str] = []
                existing_pythonpath = os.environ.get("PYTHONPATH")
                if existing_pythonpath:
                    pythonpath_entries.append(existing_pythonpath)
                pythonpath_entries.append(str(repo_root))
                custom_env["PYTHONPATH"] = os.pathsep.join(pythonpath_entries)
                custom_env["PYTHONUNBUFFERED"] = "1"

                self.standalone_manager = StandaloneBotManager()
                manager = self.standalone_manager
                manager.register(
                    StandaloneBotConfig(
                        key="rank",
                        name="Rank Bot",
                        script=rank_script,
                        workdir=repo_root,
                        description="Standalone Deadlock Rank Bot (eigener Discord-Token).",
                        autostart=True,
                        env=custom_env,
                        tags=["discord", "ranks", "dm"],
                        command_namespace="rank",
                        max_log_lines=400,
                        metrics_provider=self._collect_rank_bot_metrics,
                    )
                )
                logging.info("Standalone manager initialisiert (Rank Bot registriert)")


                steam_dir = repo_root / "cogs" / "steam" / "steam_presence"
                steam_script = steam_dir / "index.js"
                if steam_script.exists():
                    steam_env: Dict[str, str] = {}
                    try:
                        from service import db as _db

                        steam_env["DEADLOCK_DB_PATH"] = str(_db.db_path())
                    except Exception as db_exc: # defensive logging
                        logging.getLogger(__name__).warning(
                            "Konnte DEADLOCK_DB_PATH für Steam Bridge nicht bestimmen: %s",
                            db_exc,
                        )

                    default_data_dir = steam_dir / ".steam-data"
                    if os.getenv("STEAM_PRESENCE_DATA_DIR"):
                        steam_env["STEAM_PRESENCE_DATA_DIR"] = os.getenv("STEAM_PRESENCE_DATA_DIR", "")
                    else:
                        steam_env["STEAM_PRESENCE_DATA_DIR"] = str(default_data_dir)

                    steam_env["NODE_ENV"] = os.getenv("STEAM_BRIDGE_NODE_ENV", "production")

                    node_executable = os.getenv("STEAM_BRIDGE_NODE") or "node"

                    manager.register(
                        StandaloneBotConfig(
                            key="steam",
                            name="Steam Bridge",
                            script=steam_script,
                            workdir=steam_dir,
                            description="Node.js Steam Presence Bridge (Quick Invites, Auth Tasks).",
                            executable=node_executable,
                            env=steam_env,
                            autostart=True,
                            restart_on_crash=True,
                            daily_restart_at="05:00",
                            max_uptime_seconds=24 * 3600,
                            tags=["steam", "node", "presence"],
                            command_namespace="steam",
                            max_log_lines=400,
                            metrics_provider=self._collect_steam_bridge_metrics,
                        )
                    )
                    logging.info("Standalone manager: Steam Bridge registriert")
                else:
                    logging.warning("Steam Bridge Script %s nicht gefunden – Registrierung übersprungen", steam_script)
            except Exception as exc:
                logging.getLogger(__name__).error(
                    "Standalone manager konnte nicht initialisiert werden: %s", exc, exc_info=True
                )
                self.standalone_manager = None
        else:
            logging.getLogger(__name__).info("Standalone manager Modul nicht verfügbar - überspringe")

        # Hinweis: Der frühere SteamPresenceServiceManager wurde entfernt.
        # Die neue Deadlock-Presence-Integration läuft vollständig im Cog selbst.

        try:
            self.per_cog_unload_timeout = float(os.getenv("PER_COG_UNLOAD_TIMEOUT", "3.0"))
        except ValueError:
            self.per_cog_unload_timeout = 3.0
        
        # Rate Limit Aware Rename Queue is now handled by a dedicated Cog (RenameManagerCog)

    # --------------------- Discovery & Filters -------------------------
    def normalize_namespace(self, raw: str) -> str:
        text = (raw or "").strip()
        if not text:
            raise ValueError("namespace must not be empty")

        normalized = text.replace("\\", "/").strip("/")
        if not normalized:
            raise ValueError("namespace must not be empty")

        if "." in normalized and "/" not in normalized:
            parts = [segment for segment in normalized.split(".") if segment]
        else:
            parts = [segment for segment in normalized.split("/") if segment]

        if not parts:
            raise ValueError("namespace must not be empty")

        if parts[0] != "cogs":
            parts.insert(0, "cogs")

        return ".".join(parts)

    def _load_blocklist(self) -> None:
        try:
            if not self.blocklist_path.exists():
                self.blocked_namespaces = set()
                return
            data = json.loads(self.blocklist_path.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                raise ValueError("blocklist must be a list")
            loaded = set()
            for item in data:
                try:
                    loaded.add(self.normalize_namespace(str(item)))
                except Exception:
                    continue
            self.blocked_namespaces = loaded
        except Exception as e:
            logging.getLogger(__name__).warning("Konnte Blockliste nicht laden (%s): %s", self.blocklist_path, e)
            self.blocked_namespaces = set()

    def _save_blocklist(self) -> None:
        try:
            self.blocklist_path.parent.mkdir(parents=True, exist_ok=True)
            self.blocklist_path.write_text(
                json.dumps(sorted(self.blocked_namespaces)),
                encoding="utf-8",
            )
        except Exception as e:
            logging.getLogger(__name__).error("Blockliste konnte nicht gespeichert werden (%s): %s", self.blocklist_path, e)

    def is_namespace_blocked(self, namespace: str, *, assume_normalized: bool = False) -> bool:
        try:
            target = namespace if assume_normalized else self.normalize_namespace(namespace)
        except ValueError:
            return False
        for blocked in self.blocked_namespaces:
            if target == blocked or target.startswith(f"{blocked}."):
                return True
        return False

    async def block_namespace(self, namespace: str) -> Dict[str, Any]:
        normalized = self.normalize_namespace(namespace)
        if normalized in self.blocked_namespaces:
            return {"namespace": normalized, "changed": False, "unloaded": {}}

        self.blocked_namespaces.add(normalized)
        self._save_blocklist()

        to_unload = [
            ext for ext in list(self.extensions.keys()) if ext.startswith(normalized)
        ]
        unload_results: Dict[str, str] = {}
        if to_unload:
            unload_results = await self.unload_many(to_unload)

        for key in list(self.cog_status.keys()):
            if key == normalized or key.startswith(f"{normalized}."):
                self.cog_status[key] = "blocked"
        if normalized not in self.cog_status:
            self.cog_status[normalized] = "blocked"

        self.auto_discover_cogs()
        return {"namespace": normalized, "changed": True, "unloaded": unload_results}

    async def unblock_namespace(self, namespace: str) -> Dict[str, Any]:
        normalized = self.normalize_namespace(namespace)
        if normalized not in self.blocked_namespaces:
            return {"namespace": normalized, "changed": False}

        self.blocked_namespaces.discard(normalized)
        self._save_blocklist()

        for key in list(self.cog_status.keys()):
            if key == normalized or key.startswith(f"{normalized}."):
                self.cog_status[key] = "unloaded"

        self.auto_discover_cogs()
        return {"namespace": normalized, "changed": True}

    def _should_exclude(self, module_path: str) -> bool:
        default_excludes = {
            "",
        }
        env_ex = (os.getenv("COG_EXCLUDE") or "").strip()
        for item in [x.strip() for x in env_ex.split(",") if x.strip()]:
            default_excludes.add(item)
        only = {x.strip() for x in (os.getenv("COG_ONLY") or "").split(",") if x.strip()}
        if only:
            return module_path not in only
        if module_path in default_excludes:
            return True
        if self.is_namespace_blocked(module_path, assume_normalized=True):
            return True
        return False

    def auto_discover_cogs(self):
        try:
            importlib.invalidate_caches()
            if not self.cogs_dir.exists():
                logging.warning(f"Cogs directory not found: {self.cogs_dir}")
                return

            discovered: List[str] = []
            pkg_dirs_with_setup: List[Path] = []

            # Pass 1: Paket-Cogs mit setup() in __init__.py
            for init_file in self.cogs_dir.rglob("__init__.py"):
                if any(part == "__pycache__" for part in init_file.parts):
                    continue
                try:
                    content = init_file.read_text(encoding="utf-8", errors="ignore")
                except Exception as e:
                    logging.warning(f"⚠️ Error reading {init_file}: {e}")
                    continue
                has_setup = ("async def setup(" in content) or ("def setup(" in content)
                if not has_setup:
                    continue
                rel = init_file.relative_to(self.cogs_dir.parent)
                module_path = ".".join(rel.parts[:-1])
                if self._should_exclude(module_path):
                    logging.info(f"🚫 Excluded cog (package): {module_path}")
                    continue
                discovered.append(module_path)
                pkg_dirs_with_setup.append(init_file.parent)
                logging.info(f"🔍 Auto-discovered package cog: {module_path}")

            # Pass 2: Einzelne .py
            for cog_file in self.cogs_dir.rglob("*.py"):
                if cog_file.name == "__init__.py":
                    continue
                if any(part == "__pycache__" for part in cog_file.parts):
                    continue
                if any(cog_file.is_relative_to(pkg_dir) for pkg_dir in pkg_dirs_with_setup):
                    continue
                try:
                    content = cog_file.read_text(encoding="utf-8", errors="ignore")
                except Exception as e:
                    logging.warning(f"⚠️ Error checking {cog_file.name}: {e}")
                    continue
                has_setup = ("async def setup(" in content) or ("def setup(" in content)
                if not has_setup:
                    logging.info(f"⏭️ Skipped {cog_file}: no setup() found")
                    continue
                rel = cog_file.relative_to(self.cogs_dir.parent)
                module_path = ".".join(rel.with_suffix("").parts)
                if self._should_exclude(module_path):
                    logging.info(f"🚫 Excluded cog: {module_path}")
                    continue
                discovered.append(module_path)
                logging.info(f"🔍 Auto-discovered cog: {module_path}")

            self.cogs_list = sorted(set(discovered))
            logging.info(f"✅ Auto-discovery complete: {len(self.cogs_list)} cogs found")

            for key in list(self.cog_status.keys()):
                if self.is_namespace_blocked(key, assume_normalized=True):
                    self.cog_status[key] = "blocked"

        except Exception as e:
            logging.error(f"❌ Error during cog auto-discovery: {e}")
            logging.error("❌ CRITICAL: No cogs will be loaded! Check cogs/ directory")
            self.cogs_list = []

    def resolve_cog_identifier(self, identifier: str | None) -> Tuple[Optional[str], List[str]]:
        if not identifier:
            return None, []

        ident = identifier.strip()
        if not ident:
            return None, []

        if ident in self.extensions:
            return ident, []
        if ident in self.cogs_list:
            return ident, []
        if ident.startswith("cogs."):
            return ident, []

        matches = [c for c in self.cogs_list if c.endswith(f".{ident}")]
        if len(matches) == 1:
            return matches[0], []
        if len(matches) > 1:
            return None, matches

        prefixed = f"cogs.{ident}"
        if prefixed in self.cogs_list or prefixed in self.extensions:
            return prefixed, []

        return None, []

    # --------------------- Logging ------------------------------------
    def setup_logging(self):
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
        logging.basicConfig(
            level=level,
            handlers=root_handlers,
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        )

        logging.getLogger("discord").setLevel(logging.WARNING)
        logging.getLogger("discord.http").setLevel(logging.WARNING)

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

    # --------------------- Runtime-Status & Presence -------------------
    def active_cogs(self) -> List[str]:
        """Aktuell geladene Extensions (runtime), nur 'cogs.'-Namespace."""
        return sorted([ext for ext in self.extensions.keys() if ext.startswith("cogs.")])

    async def update_presence(self):
        """Presence immer anhand der echten Runtime-Anzahl setzen."""
        pfx = os.getenv("COMMAND_PREFIX", "!")
        try:
            if not self.is_ready() or getattr(self, "ws", None) is None:
                logging.debug("Presence-Update übersprungen – Bot noch nicht bereit")
                return
            activity = discord.Activity(
                type=discord.ActivityType.watching,
                name=f"{len(self.active_cogs())} Cogs | {pfx}help",
            )
            await self.change_presence(activity=activity)
        except Exception as exc:
            logging.exception("Konnte Presence nicht aktualisieren: %s", exc)

    # --------------------- Lifecycle ----------------------------------
    async def setup_hook(self):
        logging.info("Master Bot setup starting...")

        secret_mode = (os.getenv("SECRET_LOG_MODE") or "off").lower()
        _log_secret_present("Steam API Key", ["STEAM_API_KEY", "STEAM_WEB_API_KEY"], mode=secret_mode)
        _log_secret_present("Discord Token (Master)", ["DISCORD_TOKEN", "BOT_TOKEN"], mode="off")
        _log_secret_present("Twitch Client Credentials", ["TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET"], mode=secret_mode)
        _log_secret_present("Twitch Chat Token", ["TWITCH_BOT_TOKEN", "TWITCH_BOT_TOKEN_FILE"], mode=secret_mode)

        _init_db_if_available()
        await self.load_all_cogs()

        try:
            synced = await self.tree.sync()
            logging.info(f"Synced {len(synced)} slash commands")
        except Exception as e:
            logging.error(f"Failed to sync slash commands: {e}")

        # ⚠️ KEIN Autostart des Steam-Services hier!
        logging.info("Master Bot setup completed")

    async def _start_dashboard_background(self) -> None:
        if not self.dashboard:
            return
        try:
            logging.info("Dashboard HTTP server startup task running...")
            await self.dashboard.start()
            logging.info("Dashboard HTTP server startup completed.")
        except RuntimeError as e:
            logging.error(f"Dashboard konnte nicht gestartet werden: {e}. Laeuft bereits ein anderer Prozess?")
        except Exception as e:
            logging.error(f"Dashboard konnte nicht gestartet werden: {e}")

    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        """
        Voice Event Router - verteilt Voice State Updates parallel an alle Handler-Cogs.
        Verhindert sequenzielle Abarbeitung (40% schneller!).
        """
        # Sammle alle Voice-Handler aus den Cogs (mit Metadaten für Error-Logging)
        handler_info = []
        for cog_name, cog in self.cogs.items():
            if hasattr(cog, "on_voice_state_update"):
                handler = getattr(cog, "on_voice_state_update")
                if callable(handler):
                    handler_info.append((cog_name, handler))

        if not handler_info:
            return

        # Führe alle Handler PARALLEL aus (nicht sequenziell wie discord.py Default!)
        tasks = [(cog_name, handler(member, before, after)) for cog_name, handler in handler_info]
        results = await asyncio.gather(*[task for _, task in tasks], return_exceptions=True)

        # Log Fehler mit korrektem Cog-Namen (Race-Safe!)
        for (cog_name, _), result in zip(tasks, results):
            if isinstance(result, Exception):
                logging.error(f"Voice handler error in {cog_name}: {result}", exc_info=result)

    async def on_ready(self):
        logging.info(f"Bot logged in as {self.user} (ID: {self.user.id})")
        logging.info(f"Connected to {len(self.guilds)} guilds")

        await self.update_presence()

        runtime_loaded = self.active_cogs()
        logging.info(f"Loaded cogs (runtime): {len(runtime_loaded)}")
        logging.info(f"Loaded cogs: {len(runtime_loaded)}/{len(self.cogs_list)}")

        # TempVoice Log (neu)
        try:
            tv_core = self.get_cog("TempVoiceCore")
            if tv_core:
                cnt = len(getattr(tv_core, "created_channels", set()))
                logging.info(f"TempVoiceCore bereit • verwaltete Lanes: {cnt}")
            tv_if = self.get_cog("TempVoiceInterface")
            if tv_if:
                logging.info("TempVoiceInterface bereit • Interface-View registriert")
        except Exception as e:
            logging.getLogger().debug("TempVoice Ready-Log fehlgeschlagen (ignoriert): %r", e)

        # Performance-Info loggen
        voice_handlers = sum(1 for cog in self.cogs.values() if hasattr(cog, "on_voice_state_update"))
        if voice_handlers > 0:
            logging.info(f"Voice Event Router aktiv: {voice_handlers} Handler (parallel)")

        asyncio.create_task(self.hourly_health_check())
        if self.standalone_manager:
            asyncio.create_task(self._bootstrap_standalone_autostart())
    
    # --------------------- Rename Queue (Delegation) ----------------------------------
    async def queue_channel_rename(self, channel_id: int, new_name: str, reason: str = "Automated Rename"):
        rename_cog = self.get_cog("RenameManagerCog")
        if rename_cog:
            rename_cog.queue_local_rename_request(channel_id, new_name, reason)
        else:
            logging.error(
                "RenameManagerCog nicht geladen. Rename fuer Channel %s zu '%s' kann nicht verarbeitet werden.",
                channel_id,
                new_name,
            )


    async def load_all_cogs(self):
        logging.info("Loading all cogs in parallel...")

        async def load_single_cog(cog_name: str):
            try:
                self._purge_namespace_modules(cog_name)
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
        await self.update_presence()

    async def reload_all_cogs_with_discovery(self):
        try:
            unload_results = []
            loaded_extensions = [ext for ext in list(self.extensions.keys()) if ext.startswith("cogs.")]

            for ext_name in loaded_extensions:
                try:
                    await asyncio.wait_for(self.unload_extension(ext_name), timeout=self.per_cog_unload_timeout)
                    self._purge_namespace_modules(ext_name)
                    unload_results.append(f"✅ Unloaded: {ext_name}")
                    self.cog_status[ext_name] = "unloaded"
                    logging.info(f"Unloaded extension: {ext_name}")
                except asyncio.TimeoutError:
                    unload_results.append(f"⏱️ Timeout unloading {ext_name}")
                    logging.error(f"Timeout unloading extension {ext_name}")
                except Exception as e:
                    unload_results.append(f"❌ Error unloading {ext_name}: {str(e)[:50]}")
                    logging.error(f"Error unloading {ext_name}: {e}")

            old_count = len(self.cogs_list)
            self.auto_discover_cogs()
            new_count = len(self.cogs_list)

            self.cog_status = {}
            await self.load_all_cogs()

            loaded_count = len([s for s in self.cog_status.values() if s == "loaded"])
            await self.update_presence()

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

    def _purge_namespace_modules(self, namespace: str) -> None:
        """Ensure that a namespace will be freshly imported on the next load."""

        try:
            importlib.invalidate_caches()
        except Exception as e:
            logging.debug("Failed to invalidate import caches: %s", e)

        trimmed = namespace.rstrip(".")
        if not trimmed:
            return

        removed = []
        for mod_name in list(sys.modules.keys()):
            if mod_name == trimmed or mod_name.startswith(f"{trimmed}."):
                removed.append(mod_name)
                sys.modules.pop(mod_name, None)

        if removed:
            logging.debug("Cold reload purge for %s: %s", trimmed, removed)

    async def reload_cog(self, cog_name: str) -> Tuple[bool, str]:
        try:
            self._purge_namespace_modules(cog_name)
            await self.reload_extension(cog_name)
            self.cog_status[cog_name] = "loaded"
            await self.update_presence()
            msg = f"✅ Successfully reloaded {cog_name}"
            logging.info(msg)
            return True, msg
        except commands.ExtensionNotLoaded:
            try:
                self._purge_namespace_modules(cog_name)
                await self.load_extension(cog_name)
                self.cog_status[cog_name] = "loaded"
                await self.update_presence()
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
                current = asyncio.get_running_loop().time()

                if self.standalone_manager:
                    try:
                        await self.standalone_manager.ensure_autostart()
                    except Exception as exc:
                        logging.warning("Standalone Manager Autostart-Pruefung fehlgeschlagen: %s", exc)

                if current - last_critical_check >= critical_check_interval:
                    issues = []

                    if not self.get_cog("TempVoiceCore"):
                        issues.append("TempVoiceCore not loaded")
                    if not self.get_cog("TempVoiceInterface"):
                        issues.append("TempVoiceInterface not loaded")
                    if "cogs.steam.steam_link_oauth" not in self.extensions:
                        issues.append("SteamLinkOAuth (module) not loaded")

                    if issues:
                        logging.warning(f"Critical Health Check: Issues found: {issues}")
                    else:
                        logging.info("Critical Health Check: Core cogs operational")

                    last_critical_check = current

            except Exception as e:
                logging.error(f"Health check error: {e}")


    async def _bootstrap_standalone_autostart(self) -> None:
        if not self.standalone_manager:
            return
        try:
            await self.standalone_manager.ensure_autostart()
        except Exception as exc:
            logging.getLogger(__name__).error(
                "Standalone Manager Autostart fehlgeschlagen: %s", exc
            )

    async def _collect_rank_bot_metrics(self) -> Dict[str, Any]:
        try:
            from service import db
        except Exception as exc: # defensive import
            logging.getLogger(__name__).warning(
                "DB module unavailable for rank metrics: %s", exc
            )
            return {}

        def _query() -> Dict[str, Any]:
            meta_row = db.query_one(
                "SELECT heartbeat, payload, updated_at FROM standalone_bot_state WHERE bot=?",
                ("rank",),
            )
            payload: Dict[str, Any] = {}
            if meta_row and meta_row["payload"]:
                try:
                    payload = json.loads(meta_row["payload"])
                except Exception as decode_exc:
                    logging.getLogger(__name__).warning(
                        "Rank state payload decode failed: %s", decode_exc
                    )
                    payload = {}

            pending_rows = db.query_all(
                """
                SELECT id, command, status, created_at
                  FROM standalone_commands
                 WHERE bot=? AND status='pending'
              ORDER BY id ASC
                 LIMIT 20
                """,
                ("rank",),
            )
            recent_rows = db.query_all(
                """
                SELECT id, command, status, created_at, finished_at, error
                  FROM standalone_commands
                 WHERE bot=?
              ORDER BY id DESC
                 LIMIT 20
                """,
                ("rank",),
            )

            pending = [
                {
                    "id": int(row["id"]),
                    "command": row["command"],
                    "status": row["status"],
                    "created_at": row["created_at"],
                }
                for row in pending_rows
            ]
            recent = [
                {
                    "id": int(row["id"]),
                    "command": row["command"],
                    "status": row["status"],
                    "created_at": row["created_at"],
                    "finished_at": row["finished_at"],
                    "error": row["error"],
                }
                for row in recent_rows
            ]

            return {
                "state": payload,
                "pending_commands": pending,
                "recent_commands": recent,
                "heartbeat": meta_row["heartbeat"] if meta_row else None,
                "updated_at": meta_row["updated_at"] if meta_row else None,
            }

        return await asyncio.to_thread(_query)

    async def _collect_steam_bridge_metrics(self) -> Dict[str, Any]:
        try:
            from service import db
        except Exception as exc: # defensive import
            logging.getLogger(__name__).warning(
                "DB module unavailable for steam metrics: %s", exc
            )
            return {}

        def _query() -> Dict[str, Any]:
            state_row = db.query_one(
                "SELECT heartbeat, payload, updated_at FROM standalone_bot_state WHERE bot=?",
                ("steam",),
            )
            payload: Dict[str, Any] = {}
            if state_row and state_row["payload"]:
                try:
                    payload = json.loads(state_row["payload"])
                except Exception as decode_exc:
                    logging.getLogger(__name__).warning(
                        "Steam bridge payload decode failed: %s",
                        decode_exc,
                    )
                    payload = {}

            pending_rows = db.query_all(
                """
                SELECT id, command, status, created_at
                  FROM standalone_commands
                 WHERE bot=? AND status='pending'
              ORDER BY id ASC
                 LIMIT 20
                """,
                ("steam",),
            )
            recent_rows = db.query_all(
                """
                SELECT id, command, status, created_at, finished_at, error
                  FROM standalone_commands
                 WHERE bot=?
              ORDER BY id DESC
                 LIMIT 20
                """,
                ("steam",),
            )

            task_counts_rows = db.query_all(
                """
                SELECT status, COUNT(*) AS count
                  FROM steam_tasks
              GROUP BY status
                """,
            )
            recent_tasks = db.query_all(
                """
                SELECT id, type, status, updated_at, finished_at
                  FROM steam_tasks
              ORDER BY updated_at DESC
                 LIMIT 10
                """,
            )
            quick_counts_rows = db.query_all(
                """
                SELECT status, COUNT(*) AS count
                  FROM steam_quick_invites
              GROUP BY status
                """,
            )
            quick_recent_rows = db.query_all(
                """
                SELECT invite_link, status, created_at
                  FROM steam_quick_invites
              ORDER BY created_at DESC
                 LIMIT 5
                """,
            )
            quick_available_row = db.query_one(
                """
                SELECT COUNT(*) AS count
                  FROM steam_quick_invites
                 WHERE status='available'
                   AND (expires_at IS NULL OR expires_at > strftime('%s','now'))
                """,
            )

            def _format_command_rows(rows: List[Any], *, include_finished: bool) -> List[Dict[str, Any]]:
                formatted: List[Dict[str, Any]] = []
                for row in rows:
                    keys = set(row.keys()) if hasattr(row, "keys") else set()
                    formatted.append(
                        {
                            "id": int(row["id"]),
                            "command": row["command"],
                            "status": row["status"],
                            "created_at": row["created_at"],
                            "finished_at": row["finished_at"] if include_finished and "finished_at" in keys else None,
                            "error": row["error"] if include_finished and "error" in keys else None,
                        }
                    )
                return formatted

            def _format_task_counts(rows: List[Any]) -> Dict[str, int]:
                counts: Dict[str, int] = {}
                for row in rows:
                    status = str(row["status"] or "").upper()
                    try:
                        counts[status] = int(row["count"] or 0)
                    except (TypeError, ValueError):
                        counts[status] = 0
                return counts

            def _format_recent_tasks(rows: List[Any]) -> List[Dict[str, Any]]:
                recent: List[Dict[str, Any]] = []
                for row in rows:
                    recent.append(
                        {
                            "id": int(row["id"]),
                            "type": row["type"],
                            "status": row["status"],
                            "updated_at": row["updated_at"],
                            "finished_at": row["finished_at"],
                        }
                    )
                return recent

            def _format_quick_counts(rows: List[Any]) -> Dict[str, int]:
                result: Dict[str, int] = {}
                for row in rows:
                    status = str(row["status"] or "unknown")
                    try:
                        result[status] = int(row["count"] or 0)
                    except (TypeError, ValueError):
                        result[status] = 0
                return result

            quick_recent = [
                {
                    "invite_link": row["invite_link"],
                    "status": row["status"],
                    "created_at": row["created_at"],
                }
                for row in quick_recent_rows
            ]

            task_counts = _format_task_counts(task_counts_rows)
            quick_counts = _format_quick_counts(quick_counts_rows)

            return {
                "state": payload,
                "runtime": payload.get("runtime", {}),
                "pending_commands": _format_command_rows(pending_rows, include_finished=False),
                "recent_commands": _format_command_rows(recent_rows, include_finished=True),
                "tasks": {
                    "counts": task_counts,
                    "recent": _format_recent_tasks(recent_tasks),
                },
                "quick_invites": {
                    "counts": quick_counts,
                    "recent": quick_recent,
                    "available": int(quick_available_row["count"]) if quick_available_row and quick_available_row["count"] is not None else 0,
                    "total": sum(quick_counts.values()),
                },
                "heartbeat": int(state_row["heartbeat"]) if state_row and state_row["heartbeat"] is not None else None,
                "updated_at": state_row["updated_at"] if state_row else None,
            }

        return await asyncio.to_thread(_query)

    # --------- Reload für den Ordner cogs/steam ----------
    async def reload_namespace(self, namespace: str) -> Dict[str, str]:
        try:
            target_ns = self.normalize_namespace(namespace)
        except ValueError:
            return {}

        if self.is_namespace_blocked(target_ns, assume_normalized=True):
            logging.info("Namespace %s ist blockiert – kein Reload", target_ns)
            return {}

        self.auto_discover_cogs()
        targets = [
            mod
            for mod in self.cogs_list
            if mod.startswith(target_ns)
            and not self._should_exclude(mod)
        ]

        if not targets:
            logging.info("Keine Cogs für Namespace %s gefunden", target_ns)
            return {}

        results: Dict[str, str] = {}
        for mod in targets:
            try:
                self._purge_namespace_modules(mod)
                if mod in self.extensions:
                    await self.reload_extension(mod)
                    results[mod] = "reloaded"
                    logging.info(f"🔁 Reloaded {mod}")
                else:
                    await self.load_extension(mod)
                    results[mod] = "loaded"
                    logging.info(f"✅ Loaded {mod}")
                self.cog_status[mod] = "loaded"
            except Exception as e:
                trimmed = str(e)[:200]
                results[mod] = f"error: {trimmed}"
                self.cog_status[mod] = f"error: {trimmed[:100]}"
                logging.error(f"❌ Reload error for {mod}: {e}")

        await self.update_presence()
        return results

    async def reload_steam_folder(self) -> Dict[str, str]:
        return await self.reload_namespace("cogs.steam")


    # --------- Gezieltes Unload (mit Timeout) ----------
    def _match_extensions(self, query: str) -> List[str]:
        q = query.strip().lower()
        loaded = [ext for ext in self.extensions.keys() if ext.startswith("cogs.")]
        if q.startswith("cogs."):
            return [ext for ext in loaded if ext.lower().startswith(q)]
        # erlaub Substring und Ordnerkurznamen
        return [ext for ext in loaded if q in ext.lower() or ext.lower().startswith(f"cogs.{q}.")]

    async def unload_many(self, targets: List[str], timeout: float | None = None) -> Dict[str, str]:
        timeout = float(timeout) if timeout is not None else self.per_cog_unload_timeout
        results: Dict[str, str] = {}
        for ext_name in targets:
            try:
                await asyncio.wait_for(self.unload_extension(ext_name), timeout=timeout)
                results[ext_name] = "unloaded"
                self.cog_status[ext_name] = "unloaded"
                logging.info(f"Unloaded extension: {ext_name}")
            except asyncio.TimeoutError:
                results[ext_name] = "timeout"
                logging.error(f"Timeout unloading extension {ext_name} (>{timeout:.1f}s)")
            except Exception as e:
                results[ext_name] = f"error: {str(e)[:200]}"
                logging.error(f"Error unloading extension {ext_name}: {e}")
        await self.update_presence()
        return results

    async def close(self):
        logging.info("Master Bot shutting down...")

        if self.dashboard:
            try:
                await self.dashboard.stop()
            except Exception as e:
                logging.error(f"Fehler beim Stoppen des Dashboards: {e}")

        if self.standalone_manager:
            try:
                await self.standalone_manager.shutdown()
            except Exception as exc:
                logging.error(f"Fehler beim Stoppen des Standalone-Managers: {exc}")

        # 1) Alle Cogs entladen (parallel/sequenziell mit Timeout pro Cog)
        to_unload = [ext for ext in list(self.extensions.keys()) if ext.startswith("cogs.")]
        if to_unload:
            logging.info(f"Unloading {len(to_unload)} cogs with timeout {self.per_cog_unload_timeout:.1f}s each ...")
            _ = await self.unload_many(to_unload, timeout=self.per_cog_unload_timeout)
        
        # 2) Rename Queue Task abbrechen
        # Removed: Rename logic is in Cog now

        # 3) discord.Client.close() mit Timeout schützen
        try:
            timeout = float(os.getenv("DISCORD_CLOSE_TIMEOUT", "5"))
        except ValueError:
            timeout = 5.0
        try:
            await asyncio.wait_for(super().close(), timeout=timeout)
            logging.info("discord.Client.close() returned")
        except asyncio.TimeoutError:
            logging.error(f"discord.Client.close() timed out after {timeout:.1f}s; continuing shutdown")
        except Exception as e:
            logging.error(f"Error in discord.Client.close(): {e}")

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
                f"`{p}master reloadsteam` - Alle Steam-Cogs neu laden (Ordner)\n"
                f"`{p}master discover` - Neue Cogs entdecken (ohne laden)\n"
                f"`{p}master unload <muster>` - Cogs mit Muster entladen\n"
                f"`{p}master unloadtree <prefix>` - ganzen Cog-Ordner entladen\n"
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
            embed.add_field(name=f"📦 Loaded Cogs ({len(active)})", value="\n".join(short), inline=True)

        if inactive:
            short_inactive = [f"• {a.split('.')[-1]}" for a in inactive]
            embed.add_field(name=f"🗂️ Inaktiv/Entdeckt ({len(inactive)})", value="\n".join(short_inactive), inline=True)

        # Optional: zeig fehlerhafte Ladeversuche aus letzter Runde
        errs = [k for k, v in self.bot.cog_status.items() if isinstance(v, str) and v.startswith("error:")]
        if errs:
            err_short = [f"❌ {e.split('.')[-1]}" for e in errs]
            embed.add_field(name="⚠️ Fehlerhafte Cogs (letzter Versuch)", value="\n".join(err_short), inline=False)

        await ctx.send(embed=embed)

    @master_control.command(name="reload", aliases=["rl"])
    async def master_reload(self, ctx, cog_name: str = None):
        if not cog_name:
            await ctx.send(
                "❌ Bitte Cog-Namen angeben! Verfügbar:\n" + "\n".join([c.split(".")[-1] for c in self.bot.cogs_list])
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
        embed = discord.Embed(title="🔄 Cog Reload", description=msg, color=0x00FF00 if ok else 0xFF0000)
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
            loaded_cogs = [n.split(".")[-1] for n in self.bot.active_cogs()]
            if loaded_cogs:
                final.add_field(name="✅ Aktive Cogs", value="\n".join([f"• {c}" for c in loaded_cogs]), inline=True)
        else:
            final = discord.Embed(title="❌ Auto-Reload Fehlgeschlagen", description=str(result), color=0xFF0000)

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
            embed.add_field(name="✅ Erfolgreich", value="\n".join(f"• {k.split('.')[-1]} ({results[k]})" for k in ok), inline=False)
        if err:
            embed.add_field(name="⚠️ Fehler", value="\n".join(f"• {k.split('.')[-1]}: {v}" for k, v in err.items()), inline=False)

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
            embed.add_field(name="⏱️ Timeouts", value="\n".join(f"• {x}" for x in timeouts), inline=False)
        if errs:
            embed.add_field(name="⚠️ Fehler", value="\n".join(f"• {k}: {v}" for k, v in errs.items()), inline=False)
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
            embed.add_field(name="⏱️ Timeouts", value="\n".join(f"• {x}" for x in timeouts), inline=False)
        if errs:
            embed.add_field(name="⚠️ Fehler", value="\n".join(f"• {k}: {v}" for k, v in errs.items()), inline=False)
        await ctx.send(embed=embed)

    @master_control.command(name="shutdown", aliases=["stop", "quit"])
    async def master_shutdown(self, ctx):
        embed = discord.Embed(title="🛑 Master Bot wird beendet", description="Bot fährt herunter...", color=0xFF0000)
        await ctx.send(embed=embed)
        logging.info(f"Shutdown initiated by {ctx.author}")
        await self.bot.close()


# =====================================================================
# Graceful Shutdown (Timeout + Doppel-SIGINT + harter Fallback)
# =====================================================================
_shutdown_started = False
_kill_timer: threading.Timer | None = None

async def _graceful_shutdown(bot: MasterBot, reason: str = "signal",
                             timeout_close: float = 3.0, timeout_total: float = 4.0):
    global _shutdown_started, _kill_timer
    if _shutdown_started:
        return
    _shutdown_started = True

    logging.info(f"Graceful shutdown initiated ({reason}) ...")

    # 1) Bot sauber schließen (mit Timeout)
    try:
        await asyncio.wait_for(bot.close(), timeout=timeout_close)
        logging.info("bot.close() returned")
    except asyncio.TimeoutError:
        logging.error(f"bot.close() timed out after {timeout_close:.1f}s")
    except Exception as e:
        logging.error(f"Error during bot.close(): {e}")

    # 2) Übrige Tasks abbrechen (außer dieser)
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for t in pending:
        t.cancel()
    try:
        await asyncio.wait(pending, timeout=max(0.0, timeout_total - timeout_close))
    except Exception as e:
        logging.getLogger().debug("Warten auf Pending-Tasks schlug fehl (ignoriert): %r", e)

    # Kill-Watchdog stoppen, wenn wir bis hierhin sauber sind
    try:
        if _kill_timer:
            _kill_timer.cancel()
    except Exception as exc:
        logging.getLogger().debug("Kill-Timer konnte nicht gestoppt werden: %s", exc)

    # 3) Loop stoppen + harter Exit als letzte Eskalationsstufe
    try:
        loop = asyncio.get_running_loop()
        loop.stop()
        loop.call_later(0.2, lambda: os._exit(0))
    except Exception as e:
        logging.getLogger().debug("Loop Stop/Hard Exit (ignoriert): %r", e)
        os._exit(0)


# =====================================================================
# main
# =====================================================================
async def main():
    bot = MasterBot()
    await bot.add_cog(MasterControlCog(bot))

    def _sig_handler(signum, frame):
        # Zweites Strg+C => sofortiger Hard-Exit
        if _shutdown_started:
            logging.error("Second signal received -> hard exit now.")
            os._exit(1)

        logging.info(f"Received signal {signum}, shutting down gracefully...")

        # Watchdog: harter Kill nach KILL_AFTER_SECONDS (default 2s)
        try:
            kill_after = float(os.getenv("KILL_AFTER_SECONDS", "2"))
        except ValueError:
            kill_after = 2.0

        global _kill_timer
        try:
            _kill_timer = threading.Timer(
                kill_after,
                lambda: (logging.error(f"Kill watchdog fired after {kill_after:.1f}s -> os._exit(2)"),
                         os._exit(2))
            )
            _kill_timer.daemon = True
            _kill_timer.start()
        except Exception as e:
            logging.getLogger().debug("Kill-Timer konnte nicht gestartet werden (ignoriert): %r", e)

        try:
            asyncio.get_running_loop().create_task(_graceful_shutdown(bot, reason=f"signal {signum}",
                                                                      timeout_close=float(os.getenv("DISCORD_CLOSE_TIMEOUT", "5")),
                                                                      timeout_total=max(kill_after, 2.0)))
        except RuntimeError:
            os._exit(0)

    try:
        signal.signal(signal.SIGINT, _sig_handler)
        signal.signal(signal.SIGTERM, _sig_handler)
    except Exception as e:
        logging.getLogger().debug("Signal-Handler Registrierung teilweise fehlgeschlagen (OS?): %r", e)

    token = settings.discord_token.get_secret_value()
    if not token:
        raise SystemExit("DISCORD_TOKEN fehlt in ENV/.env")

    try:
        await bot.start(token)
    except KeyboardInterrupt:
        logging.info("Keyboard interrupt received, shutting down...")
        _sig_handler(signal.SIGINT, None)
    except Exception as e:
        logging.error(f"Bot crashed: {e}")
        logging.error(traceback.format_exc())
    finally:
        if not bot.is_closed():
            await _graceful_shutdown(bot, reason="finally-clause")

if __name__ == "__main__":
    asyncio.run(main())
