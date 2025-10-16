# filename: cogs/steam/steam_master.py
"""Steam login manager cog for the master bot.

Funktion:
- Startet einen Hintergrund-Thread, der eine persistente Steam-Session hält.
- Login-Strategie: login_key -> (sonst) Passwort -> (falls verlangt) 2FA via !sg CODE.
- Persistiert login_key und Sentry/Machine-Auth im Datenverzeichnis.
- Befehle: !sg, !steam_status, !steam_token, !steam_token_clear
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Optional

import discord
from discord.ext import commands

from steam.client import SteamClient
from steam.enums import EResult
from steam.enums.emsg import EMsg

log = logging.getLogger(__name__)


def _data_dir() -> Path:
    """Resolve the data directory used to persist Steam auth artefacts."""
    base = (os.getenv("STEAM_MASTER_DATA_DIR") or ".steam-data").strip() or ".steam-data"
    path = Path(base)
    path.mkdir(parents=True, exist_ok=True)
    return path


class SteamLoginManager(threading.Thread):
    """Background thread that manages a persistent Steam session (no presence here)."""

    def __init__(self, username: str, password: str, data_dir: Path):
        super().__init__(daemon=True)
        self.username = username
        self.password = password
        self.data_dir = data_dir
        self.login_key_file = self.data_dir / "login_key.txt"
        self.sentry_file = self.data_dir / "sentry.bin"

        self.client = SteamClient()
        # Leite Steam dazu an, Anmeldedaten/Sentry in unserem Ordner zu persistieren
        try:
            self.client.set_credential_location(str(self.data_dir))
        except Exception:
            # ältere/andere Builds haben die Methode evtl. nicht – ist ok
            pass

        self.guard_code: Optional[str] = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()

        self.logged_on = False
        self.last_result: Optional[EResult] = None

        # Steam event wiring
        self.client.on(EMsg.ClientUpdateMachineAuth, self._on_machine_auth)
        self.client.on(EMsg.ClientLogOnResponse, self._on_logon_response)
        self.client.on(EMsg.ClientLoggedOff, self._on_logged_off)

        # Greenlet-Runner für den Steam IO-Loop
        self._io_runner = None

    # ---------- public API (used by Cog commands) ----------
    def set_guard_code(self, code: str) -> None:
        sanitized = code.strip()
        with self._lock:
            self.guard_code = sanitized
        log.info("Steam Guard-Code übernommen (len=%s). Login wird erneut versucht.", len(sanitized))

    def status(self) -> str:
        has_key = self.login_key_file.exists() and self.login_key_file.stat().st_size > 0
        has_sentry = self.sentry_file.exists() and self.sentry_file.stat().st_size > 0
        return (
            "logged_on={logged} last_result={result} login_key={key} sentry={sentry}".format(
                logged=self.logged_on,
                result=self.last_result,
                key="yes" if has_key else "no",
                sentry="yes" if has_sentry else "no",
            )
        )

    def clear_login_key(self) -> bool:
        try:
            if self.login_key_file.exists():
                self.login_key_file.unlink()
                return True
            return False
        except Exception:
            log.exception("Konnte login_key nicht löschen")
            return False

    # ---------- persistence helpers ----------
    def _read_login_key(self) -> Optional[str]:
        try:
            if self.login_key_file.exists():
                key = self.login_key_file.read_text(encoding="utf-8").strip()
                if key:
                    log.debug("login_key gelesen (len=%s)", len(key))
                else:
                    log.debug("login_key-Datei vorhanden, aber leer")
                return key or None
            log.debug("login_key-Datei %s existiert nicht", self.login_key_file)
        except Exception:
            log.exception("Konnte login_key nicht lesen")
        return None

    def _write_login_key(self, key: str) -> None:
        try:
            self.login_key_file.write_text(key, encoding="utf-8")
            log.info("Steam login_key gespeichert (%s)", self.login_key_file)
        except Exception:
            log.exception("Konnte login_key nicht speichern")

    def _write_sentry(self, data: bytes) -> None:
        try:
            self.sentry_file.write_bytes(data)
            log.info("Steam Sentry gespeichert (%s)", self.sentry_file)
        except Exception:
            log.exception("Konnte Sentry nicht speichern")

    # ---------- steam event handlers ----------
    def _on_machine_auth(self, msg) -> None:
        data = getattr(msg.body, "bytes", b"")
        if data:
            log.debug(
                "MachineAuth erhalten (offset=%s, sha1=%s)",
                getattr(msg.body, "offset", "?"),
                getattr(msg.body, "sha_file", "?"),
            )
            self._write_sentry(data)
        else:
            log.debug("MachineAuth Event ohne Daten erhalten: %s", msg.body)

    def _on_logon_response(self, msg) -> None:
        self.last_result = msg.body.eresult
        log.debug(
            "LogOnResponse erhalten: eresult=%s, extended=%s",
            self.last_result,
            getattr(msg.body, "eresult_extended", None),
        )
        if self.last_result == EResult.OK:
            self.logged_on = True
            key = getattr(self.client, "login_key", None)
            if key:
                self._write_login_key(str(key))
            user = getattr(self.client.user, "name", "<unknown>")
            log.info("Steam eingeloggt als %s", user)
        else:
            self.logged_on = False
            log.warning(
                "Steam LogOnResponse: %s (eresult_extended=%s, msg=%s)",
                self.last_result,
                getattr(msg.body, "eresult_extended", None),
                getattr(msg.body, "error_message", None),
            )

    def _on_logged_off(self, msg) -> None:
        self.logged_on = False
        result = getattr(getattr(msg, "body", None), "eresult", None)
        log.warning(
            "Steam Logged off: %s (client_connected=%s)",
            result,
            getattr(self.client, "connected", None),
        )

    # ---------- login loop ----------
    def _try_login(self) -> EResult:
        log.debug("Login-Versuch gestartet. logged_on=%s", self.logged_on)

        if not self.username:
            log.error("STEAM_USERNAME ist nicht gesetzt. Abbruch.")
            return EResult.InvalidParam

        # 1) bevorzugt: login_key
        login_key = self._read_login_key()
        if login_key:
            log.info("Steam-Login mit login_key ...")
            log.debug("Login-Parameter: username=%s (login_key len=%s)", self.username, len(login_key))
            return self.client.login(username=self.username, login_key=login_key)

        # 2) Passwort (+ optional 2FA, wenn via !sg gesetzt)
        with self._lock:
            code = self.guard_code
            self.guard_code = None

        log.debug(
            "Guard-Code %s gefunden und %s.",
            "wurde" if code else "wurde nicht",
            "verbraucht" if code else "nicht benötigt",
        )

        if code:
            log.info("Steam-Login mit Passwort + 2FA ...")
            log.debug(
                "Login-Parameter: username=%s, two_factor_code_len=%s",
                self.username,
                len(code),
            )
            return self.client.login(
                username=self.username,
                password=self.password,
                two_factor_code=code,
            )

        log.info("Steam-Login mit Passwort (ohne 2FA) ...")
        log.debug("Login-Parameter: username=%s", self.username)
        return self.client.login(username=self.username, password=self.password)

    def run(self) -> None:
        import gevent  # lokal im Thread ok

        # Start IO-Loop als Greenlet, damit der gevent-Hub "lebt"
        if self._io_runner is None:
            self._io_runner = gevent.spawn(self.client.run_forever)

        try:
            while not self._stop_event.is_set():
                try:
                    with self._lock:
                        has_guard = bool(self.guard_code)
                    log.debug(
                        "Starte Steam-Login-Iteration. guard_code=%s, login_key=%s, logged_on=%s",
                        has_guard,
                        self.login_key_file.exists(),
                        self.logged_on,
                    )
                    res = self._try_login()
                    self.last_result = res

                    if res == EResult.AccountLoginDeniedNeedTwoFactor:
                        log.warning("Steam benötigt 2FA. Bitte per !sg CODE senden.")
                        wait_logged = False
                        # auf Guard-Code warten
                        while not self._stop_event.is_set():
                            with self._lock:
                                if self.guard_code:
                                    break
                            if not wait_logged:
                                log.debug("Warte auf neuen Guard-Code ...")
                                wait_logged = True
                            gevent.sleep(1.0)
                        log.debug("Guard-Code wurde gesetzt. Fahre mit neuem Login-Fenster fort.")
                        continue

                    if res != EResult.OK:
                        log.warning(
                            "Steam-Login fehlgeschlagen: %s. Details: logged_on=%s, connected=%s. Neuer Versuch in 15s.",
                            res,
                            self.logged_on,
                            getattr(self.client, "connected", None),
                        )
                        gevent.sleep(15.0)
                        continue

                    # Erfolgreich eingeloggt – run_forever läuft bereits im Greenlet.
                    while not self._stop_event.is_set() and self.logged_on:
                        gevent.sleep(2.0)

                    if not self._stop_event.is_set():
                        log.warning("Steam getrennt. Reconnect in 10s.")
                        gevent.sleep(10.0)

                except Exception:
                    log.exception("Fehler im Steam Login-Loop")
                    gevent.sleep(10.0)
        finally:
            try:
                if self._io_runner is not None:
                    self._io_runner.kill()
            except Exception:
                pass

    def stop(self) -> None:
        self._stop_event.set()
        try:
            self.client.logout()
        except Exception:
            log.exception("Steam-Logout fehlgeschlagen", exc_info=True)


class SteamMaster(commands.Cog):
    """Discord Cog zur Verwaltung des Steam-Login-Managers (ohne Presence)."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.manager: Optional[SteamLoginManager] = None
        self.data_dir = _data_dir()
        self._manager_lock = threading.Lock()

    async def cog_load(self) -> None:
        self._ensure_manager()

    async def cog_unload(self) -> None:
        if self.manager:
            self.manager.stop()
            self.manager.join(timeout=5)
            self.manager = None

    # ---------- helpers ----------
    def _lookup_env(self, *names: str, secret: bool = False) -> tuple[str, Optional[str]]:
        """Return the first non-empty env var value together with its name."""
        for name in names:
            raw = os.getenv(name)
            if raw is None:
                log.debug("Env %s ist nicht gesetzt.", name)
                continue
            value = raw.strip()
            if not value:
                log.debug("Env %s ist gesetzt, aber leer.", name)
                continue
            if secret:
                log.debug("Credential in %s gefunden (len=%s).", name, len(value))
            else:
                log.debug("Env %s -> %s", name, value)
            return value, name
        return "", None

    def _credentials(self) -> tuple[str, str]:
        username, user_env = self._lookup_env("STEAM_USERNAME", "STEAM_USER", "STEAM_ACCOUNT")
        password, pass_env = self._lookup_env("STEAM_PASSWORD", "STEAM_PASSWORT", "STEAM_PW", secret=True)

        missing = []
        if not username:
            missing.append("Username")
        if not password:
            missing.append("Passwort")

        if missing:
            raise RuntimeError(
                " oder ".join(missing)
                + " fehlt. Erwartet einen der ENV Variablennamen: Username -> STEAM_USERNAME/STEAM_USER/STEAM_ACCOUNT, Passwort -> STEAM_PASSWORD/STEAM_PASSWORT/STEAM_PW."
            )

        if user_env:
            log.info("Steam Username aus %s geladen.", user_env)
        if pass_env:
            log.info("Steam Passwort aus %s geladen (Wert verborgen).", pass_env)

        return username, password

    def _ensure_manager(self) -> SteamLoginManager:
        with self._manager_lock:
            if self.manager and self.manager.is_alive():
                return self.manager
            username, password = self._credentials()
            manager = SteamLoginManager(username=username, password=password, data_dir=self.data_dir)
            manager.start()
            self.manager = manager
            log.info("SteamLoginManager gestartet (DataDir=%s)", self.data_dir)
            return manager

    async def _get_manager(self, ctx: commands.Context) -> Optional[SteamLoginManager]:
        try:
            return self._ensure_manager()
        except RuntimeError as exc:
            await ctx.reply(f"❌ {exc}")
            return None

    # ---------- commands ----------
    @commands.command(name="sg", aliases=["steam_guard", "steamguard"])
    @commands.has_permissions(administrator=True)
    async def cmd_sg(self, ctx: commands.Context, code: str) -> None:
        """Steam Guard / 2FA-Code an den Login-Thread übergeben."""
        manager = await self._get_manager(ctx)
        if not manager:
            return
        manager.set_guard_code(code)
        await ctx.reply("✅ Guard-Code gesetzt. Login wird erneut versucht.")

    @commands.command(name="steam_status")
    @commands.has_permissions(administrator=True)
    async def cmd_status(self, ctx: commands.Context) -> None:
        """Aktuellen Login-Status inkl. Token-/Sentry-Verfügbarkeit anzeigen."""
        manager = await self._get_manager(ctx)
        if not manager:
            return
        await ctx.reply(f"```{manager.status()}```")

    @commands.command(name="steam_token")
    @commands.has_permissions(administrator=True)
    async def cmd_token(self, ctx: commands.Context) -> None:
        """Anzeigen, ob ein login_key vorhanden ist (Pfad ohne Inhalt)."""
        manager = await self._get_manager(ctx)
        if not manager:
            return
        has_key = manager.login_key_file.exists() and manager.login_key_file.stat().st_size > 0
        await ctx.reply(
            "🔐 login_key: {status}\nPfad: `{path}`".format(
                status="vorhanden" if has_key else "nicht vorhanden",
                path=manager.login_key_file,
            )
        )

    @commands.command(name="steam_token_clear")
    @commands.has_permissions(administrator=True)
    async def cmd_token_clear(self, ctx: commands.Context) -> None:
        """login_key bewusst löschen (erzwingt nächsten Guard-Login)."""
        manager = await self._get_manager(ctx)
        if not manager:
            return
        ok = manager.clear_login_key()
        await ctx.reply("🧹 login_key gelöscht." if ok else "ℹ️ Kein login_key vorhanden.")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SteamMaster(bot))
