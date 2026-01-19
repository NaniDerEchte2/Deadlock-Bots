from __future__ import annotations

import builtins
import importlib
import inspect
import hashlib
import logging
import os
import sys
import types
from pathlib import Path
from typing import List


def _log_src(modname: str) -> None:
    try:
        module = importlib.import_module(modname)
        path = inspect.getfile(module)
        with open(path, "rb") as fh:
            sha = hashlib.sha1(fh.read()).hexdigest()[:12]
        logging.getLogger().info("SRC %s -> %s [sha1:%s]", modname, path, sha)
    except Exception as exc:
        logging.getLogger().error("SRC %s -> %r", modname, exc)


def _load_secrets_from_keyring() -> None:
    """
    Versucht, sensitive Geheimnisse aus dem Windows Credential Manager (Tresor) zu laden
    und in os.environ zu injizieren.
    Service Name: 'DeadlockBot'
    """
    try:
        import keyring
    except ImportError:
        logging.getLogger().debug("keyring nicht installiert, überspringe Tresor-Check.")
        return

    service_name = "DeadlockBot"
    # Liste der Schlüssel, die wir im Tresor erwarten
    keys_to_check = [
        "DISCORD_TOKEN", "DISCORD_TOKEN_WORKER", "DISCORD_TOKEN_RANKED", "DISCORD_TOKEN_PATCHNOTES",
        "DISCORD_OAUTH_CLIENT_ID", "DISCORD_OAUTH_CLIENT_SECRET",
        "STEAM_API_KEY", "STEAM_BOT_PASSWORD",
        "TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET", "TWITCH_BOT_CLIENT_ID", "TWITCH_BOT_CLIENT_SECRET", "TWITCH_BOT_TOKEN", "TWITCH_BOT_REFRESH_TOKEN",
        "TWITCH_RAID_REDIRECT_URI",
        "OPENAI_API_KEY", "GEMINI_API_KEY", "PERPLEXITY_API_KEY", "GITHUB_TOKEN", "PPLX_API_KEY",
        "aws_access_key_id", "aws_secret_access_key", "DEADLOCK_API_KEY", "BOT_TOKEN"
    ]
    
    loaded_keys = []
    for key in keys_to_check:
        try:
            # Variante 1: Adresse=DeadlockBot, Benutzer=KEY
            val = keyring.get_password(service_name, key)
            
            # Variante 2: Adresse=KEY@DeadlockBot, Benutzer=KEY (deine übersichtliche Variante)
            if not val:
                val = keyring.get_password(f"{key}@{service_name}", key)
            
            if val:
                os.environ[key] = val
                loaded_keys.append(key)
        except Exception:
            pass  # Ignorieren, wenn Key nicht im Tresor
            
    if loaded_keys:
        logging.getLogger().info("🔐 %d Secrets aus Windows Tresor (%s) geladen: %s", len(loaded_keys), service_name, ", ".join(loaded_keys))


def _load_env_robust() -> str | None:
    try:
        from dotenv import load_dotenv
    except ImportError as exc:
        logging.getLogger().debug("dotenv nicht installiert: %r", exc)
        return None
    except Exception as exc:
        logging.getLogger().debug("dotenv Import-Fehler: %r", exc)
        return None

    candidates: List[Path] = []
    custom = os.getenv("DOTENV_PATH")
    if custom:
        candidates.append(Path(custom))

    here = Path(__file__).resolve()
    candidates.append(here.parent.parent / ".env")
    candidates.append(Path(os.path.expandvars(r"%USERPROFILE%")) / "Documents" / ".env")

    for path in candidates:
        try:
            if path.exists():
                load_dotenv(dotenv_path=str(path), override=False)
                logging.getLogger().info(".env geladen: %s", path)
                return str(path)
        except Exception as exc:
            logging.getLogger().debug("Konnte .env nicht laden (%s): %r", path, exc)
    
    # NACH dem Laden der Datei: Tresor checken und ggf. überschreiben
    _load_secrets_from_keyring()
    return None


def _mask_tail(secret: str, keep: int = 4) -> str:
    if not secret:
        return ""
    text = str(secret)
    if len(text) <= keep:
        return "*" * len(text)
    return "*" * (len(text) - keep) + text[-keep:]


def _log_secret_present(name: str, env_keys: List[str], mode: str = "off") -> None:
    try:
        value = None
        for key in env_keys:
            env_val = os.getenv(key)
            if env_val:
                value = env_val
                break
        if not value or mode == "off":
            return
        if mode in ("present", "masked"):
            logging.info("%s: vorhanden (Wert wird nicht geloggt)", name)
    except Exception as exc:
        logging.getLogger().debug("Secret-Check fehlgeschlagen (%s): %r", name, exc)


class _RedactSecretsFilter(logging.Filter):
    def __init__(self, keys: List[str]):
        super().__init__()
        self.secrets = [os.getenv(k) for k in keys if os.getenv(k)]

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            # getMessage() formatiert msg % args
            msg = str(record.getMessage())
            redacted = msg
            for secret in self.secrets:
                if secret and secret in redacted:
                    redacted = redacted.replace(secret, "***REDACTED***")
            
            # Da wir die Nachricht jetzt fertig formatiert haben (getMessage),
            # müssen wir record.args leeren. Sonst versucht der Formatter später
            # erneut, args in den String einzufügen, was zum TypeError führt.
            record.msg = redacted
            record.args = ()
        except Exception:
            # NIEMALS im Filter loggen -> Endlosschleife!
            pass
        return True


def _configure_root_logging() -> None:
    root = logging.getLogger()
    if root.handlers:
        return
    logging.basicConfig(level=logging.INFO, handlers=[logging.StreamHandler(sys.stdout)])


def _install_workerproxy_shim() -> None:
    try:
        from shared.worker_client import WorkerProxy  # type: ignore

        setattr(builtins, "WorkerProxy", WorkerProxy)
        return
    except Exception as exc:
        logging.getLogger().info("WorkerProxy nicht verfügbar – verwende Stub: %r", exc)

    class _WorkerProxyStub:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, *args, **kwargs):
            return {"ok": False, "error": "worker_stub"}

        def edit_channel(self, *args, **kwargs):
            return {"ok": False, "error": "worker_stub"}

        def set_permissions(self, *args, **kwargs):
            return {"ok": False, "error": "worker_stub"}

        def rename_match_suffix(self, *args, **kwargs):
            return {"ok": False, "error": "worker_stub"}

        def clear_match_suffix(self, *args, **kwargs):
            return {"ok": False, "error": "worker_stub"}

        def bulk(self, *args, **kwargs):
            return {"ok": False, "error": "worker_stub"}

    setattr(builtins, "WorkerProxy", _WorkerProxyStub)

    if "shared" not in sys.modules:
        sys.modules["shared"] = types.ModuleType("shared")
    if "shared.worker_client" not in sys.modules:
        wc_mod = types.ModuleType("shared")
        setattr(wc_mod, "WorkerProxy", _WorkerProxyStub)
        sys.modules["shared.worker_client"] = wc_mod


def _init_db_if_available() -> None:
    try:
        from service import db as _db  # Deadlock-Bots/service/db.py
    except Exception as exc:
        logging.critical("Zentrale DB-Modul 'service.db' konnte nicht importiert werden: %s", exc)
        return
    try:
        _db.connect()
        logging.info("Zentrale DB initialisiert (quiet) via service.db.")
    except Exception as exc:
        logging.critical("Zentrale DB (service.db) konnte nicht initialisiert werden: %s", exc)


def _log_runtime_info() -> None:
    logging.getLogger().info("PYTHON exe=%s", sys.executable)
    logging.getLogger().info("CWD=%s", os.getcwd())
    logging.getLogger().info("sys.path[0]=%s", sys.path[0] if sys.path else None)


def _log_known_sources() -> None:
    for name in [
        "cogs.rules_channel",
        "cogs.welcome_dm.dm_main",
        "cogs.welcome_dm.step_streamer",
        "cogs.welcome_dm.step_steam_link",
    ]:
        _log_src(name)


def bootstrap_runtime() -> None:
    """
    Early process bootstrap: logging, .env, worker shim, and debug hints.
    """
    _configure_root_logging()
    _load_env_robust()
    _install_workerproxy_shim()
    _log_runtime_info()
    _log_known_sources()


__all__ = [
    "_RedactSecretsFilter",
    "_init_db_if_available",
    "_load_env_robust",
    "_log_secret_present",
    "_mask_tail",
    "bootstrap_runtime",
]
