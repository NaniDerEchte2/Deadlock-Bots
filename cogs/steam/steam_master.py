"""Steam login manager cog for the master bot."""

from __future__ import annotations

import logging
import os
import threading
import time
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
    base = os.getenv("STEAM_MASTER_DATA_DIR", ".steam-data").strip() or ".steam-data"
    path = Path(base)
    path.mkdir(parents=True, exist_ok=True)
    return path


class SteamLoginManager(threading.Thread):
    """Background thread that manages a persistent Steam session."""

    def __init__(self, username: str, password: str, data_dir: Path):
        super().__init__(daemon=True)
        self.username = username
        self.password = password
        self.data_dir = data_dir
        self.login_key_file = self.data_dir / "login_key.txt"
        self.sentry_file = self.data_dir / "sentry.bin"

        self.client = SteamClient()
        self.guard_code: Optional[str] = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self.logged_on = False
        self.last_result: Optional[EResult] = None

        self.client.on(EMsg.ClientUpdateMachineAuth, self._on_machine_auth)
        self.client.on(EMsg.ClientLogOnResponse, self._on_logon_response)
        self.client.on(EMsg.ClientLoggedOff, self._on_logged_off)

    # ---- API für Discord-Commands ----
    def set_guard_code(self, code: str) -> None:
        with self._lock:
            self.guard_code = code.strip()
        log.info("Steam Guard-Code übernommen. Login wird erneut versucht.")

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

    # ---- Persist helpers ----
    def _read_login_key(self) -> Optional[str]:
        try:
            if self.login_key_file.exists():
                return self.login_key_file.read_text(encoding="utf-8").strip()
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

    # ---- Steam Events ----
    def _on_machine_auth(self, msg) -> None:
        data = getattr(msg.body, "bytes", b"")
        if data:
            self._write_sentry(data)

    def _on_logon_response(self, msg) -> None:
        self.last_result = msg.body.eresult
        if self.last_result == EResult.OK:
            self.logged_on = True
            key = getattr(self.client, "login_key", None)
            if key:
                self._write_login_key(str(key))
            user = getattr(self.client.user, "name", "<unknown>")
            log.info("Steam eingeloggt als %s", user)
        else:
            self.logged_on = False
            log.warning("Steam LogOnResponse: %s", self.last_result)

    def _on_logged_off(self, msg) -> None:
        self.logged_on = False
        result = getattr(getattr(msg, "body", None), "eresult", None)
        log.warning("Steam Logged off: %s", result)

    # ---- Login-Loop ----
    def _try_login(self) -> EResult:
        if not self.username:
            log.error("STEAM_USERNAME ist nicht gesetzt. Abbruch.")
            return EResult.InvalidParam

        login_key = self._read_login_key()
        if login_key:
            log.info("Steam-Login mit login_key ...")
            return self.client.login(username=self.username, login_key=login_key)

        with self._lock:
            code = self.guard_code
            self.guard_code = None

        if code:
            log.info("Steam-Login mit Passwort + 2FA ...")
            return self.client.login(
                username=self.username,
                password=self.password,
                two_factor_code=code,
            )

        log.info("Steam-Login mit Passwort (ohne 2FA) ...")
        return self.client.login(username=self.username, password=self.password)

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                res = self._try_login()
                self.last_result = res

                if res == EResult.AccountLoginDeniedNeedTwoFactor:
                    log.warning("Steam benötigt 2FA. Bitte per !sg CODE senden.")
                    while not self._stop_event.is_set():
                        with self._lock:
                            if self.guard_code:
                                break
                        time.sleep(1)
                    continue

                if res != EResult.OK:
                    log.warning("Steam-Login fehlgeschlagen: %s. Neuer Versuch in 15s.", res)
                    time.sleep(15)
                    continue

                self.client.run_forever()
                if not self._stop_event.is_set():
                    log.warning("Steam getrennt. Reconnect in 10s.")
                    time.sleep(10)

            except Exception:
                log.exception("Fehler im Steam Login-Loop")
                time.sleep(10)

    def stop(self) -> None:
        self._stop_event.set()
        try:
            self.client.logout()
        except Exception:
            log.exception("Steam-Logout fehlgeschlagen", exc_info=True)


class SteamMaster(commands.Cog):
    """Discord Cog zur Verwaltung des Steam-Login-Managers."""

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

    # ---- Helpers ----
    def _credentials(self) -> tuple[str, str]:
        username = (os.getenv("STEAM_USERNAME") or "").strip()
        password = (os.getenv("STEAM_PASSWORD") or "").strip()
        if not username or not password:
            raise RuntimeError("STEAM_USERNAME oder STEAM_PASSWORD fehlen.")
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

    # ---- Commands ----
    @commands.command(name="sg", aliases=["steam_guard", "steamguard"])
    @commands.has_permissions(administrator=True)
    async def cmd_sg(self, ctx: commands.Context, code: str) -> None:
        manager = await self._get_manager(ctx)
        if not manager:
            return
        manager.set_guard_code(code)
        await ctx.reply("✅ Guard-Code gesetzt. Login wird erneut versucht.")

    @commands.command(name="steam_status")
    @commands.has_permissions(administrator=True)
    async def cmd_status(self, ctx: commands.Context) -> None:
        manager = await self._get_manager(ctx)
        if not manager:
            return
        await ctx.reply(f"```{manager.status()}```")

    @commands.command(name="steam_token")
    @commands.has_permissions(administrator=True)
    async def cmd_token(self, ctx: commands.Context) -> None:
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
        manager = await self._get_manager(ctx)
        if not manager:
            return
        ok = manager.clear_login_key()
        await ctx.reply("🧹 login_key gelöscht." if ok else "ℹ️ Kein login_key vorhanden.")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SteamMaster(bot))
