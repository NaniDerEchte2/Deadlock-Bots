# cogs/steam/steam_master.py
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from dataclasses import dataclass
from typing import Optional, Dict, List, Callable, Awaitable, Iterable

import discord
from discord.ext import commands

# --- robuste steam/steamio-Imports mit Fallbacks ---
try:
    from steam import Client, Intents

    try:
        from steam.enums import PersonaState  # steamio
    except Exception:
        from steam.enums import EPersonaState as PersonaState  # ältere Benennung

    from steam.user import User
    try:
        from steam.user import Friend  # nicht in allen steamio-Versionen vorhanden
    except Exception:
        Friend = User  # type: ignore[assignment]

    try:
        from steam.invite import UserInvite
    except Exception:
        class UserInvite:  # type: ignore[empty-body]
            pass

    STEAM_AVAILABLE = True
except Exception as exc:  # pragma: no cover
    Client = object  # type: ignore[assignment]
    Intents = object  # type: ignore[assignment]
    PersonaState = object  # type: ignore[assignment]
    UserInvite = object  # type: ignore[assignment]
    Friend = object  # type: ignore[assignment]
    User = object  # type: ignore[assignment]
    STEAM_AVAILABLE = False
    logging.getLogger(__name__).warning("steam package not available: %r", exc)

# zentrale DB wie gehabt
from service import db

log = logging.getLogger(__name__)


# -----------------------------
# Hilfsklassen
# -----------------------------
class GuardCodeManager:
    """Koordiniert Steam Guard Codes, geliefert via Discord (!sg CODE)."""
    def __init__(self) -> None:
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=1)
        self._waiters = 0
        self._lock = asyncio.Lock()

    async def wait_for_code(self, timeout: Optional[float] = None) -> str:
        async with self._lock:
            self._waiters += 1
        try:
            if timeout:
                code = await asyncio.wait_for(self._queue.get(), timeout)
            else:
                code = await self._queue.get()
            log.info("Received Steam Guard code from Discord (len=%d).", len(code))
            return code
        finally:
            async with self._lock:
                self._waiters = max(0, self._waiters - 1)

    def submit(self, code: str) -> bool:
        cleaned = (code or "").strip()
        if not cleaned:
            return False
        try:
            self._queue.put_nowait(cleaned)
        except asyncio.QueueFull:
            try:
                _ = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self._queue.put_nowait(cleaned)
        log.info("Steam Guard code forwarded from Discord (waiters=%d).", self._waiters)
        return True


class DiscordSteamClient(Client):  # type: ignore[misc]
    """Steam-Client, der Guard-Codes aus dem GuardCodeManager holt."""
    def __init__(self, guard_codes: GuardCodeManager, *, guard_timeout: float = 300.0, **options):
        if not STEAM_AVAILABLE:
            raise RuntimeError("steam package required")
        intents = options.pop("intents", None)
        if intents is None and hasattr(Intents, "Users"):
            intents = getattr(Intents, "Users", 0) | getattr(Intents, "Chat", 0)
        super().__init__(intents=intents, **options)  # type: ignore[arg-type]
        self._guard_codes = guard_codes
        self._guard_timeout = guard_timeout

    async def code(self) -> str:
        log.warning("Steam Guard challenge received – waiting for Discord input…")
        # Blockiert bis !sg CODE
        return await self._guard_codes.wait_for_code(self._guard_timeout)


@dataclass(slots=True)
class SteamMasterConfig:
    username: str
    password: Optional[str] = None
    guard_timeout: float = 300.0
    deadlock_app_id: str = "1422450"  # nur falls du später Präsenz sammeln willst


class SteamMasterDisabled(RuntimeError):
    """Ausnahme, wenn der Cog nicht initialisiert werden kann (z. B. fehlende ENV)."""


# -----------------------------
# Der Cog
# -----------------------------
class SteamMasterCog(commands.Cog):
    """
    Master-Cog für Steam-Anmeldung:
    - Kein Autologin.
    - Login startet nur, wenn:
       a) !sg CODE gesendet wurde (Username/Pass-Flow), oder
       b) explizit !steam_login_token aufgerufen wird (Refresh-Token-Flow).
    - Nach erfolgreichem Login wird der Refresh-Token gespeichert (DB).
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.guard_codes = GuardCodeManager()

        self.config: Optional[SteamMasterConfig] = None
        self.client: Optional[DiscordSteamClient] = None
        self._disabled_reason: Optional[str] = None

        try:
            self.config = self._load_config_from_env()
        except SteamMasterDisabled as exc:
            self._disabled_reason = str(exc)
            log.warning("Steam master disabled: %s", exc)
        else:
            try:
                self.client = DiscordSteamClient(
                    self.guard_codes,
                    guard_timeout=self.config.guard_timeout,
                )
            except Exception as exc:  # pragma: no cover - steam lib fehlt im Test
                self._disabled_reason = f"steam client unavailable: {exc!s}"
                log.warning("Steam master disabled (steam client unavailable): %s", exc)
                self.client = None
            else:
                # Steam-Eventbindung nur wenn Client existiert
                self.client.event(self._on_login)
                self.client.event(self._on_ready)
                self.client.event(self._on_disconnect)
                self.client.event(self._on_invite)
                self.client.event(self._on_user_update)

        # Status
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._runner_task: Optional[asyncio.Task[None]] = None

    # ------------- ENV laden
    def _infer_username_from_db(self) -> Optional[str]:
        try:
            row = db.query_one(
                """
                SELECT account_name
                FROM steam_refresh_tokens
                WHERE account_name IS NOT NULL AND account_name <> ''
                ORDER BY received_at DESC
                LIMIT 1
                """,
            )
        except Exception:
            log.exception("Failed to infer Steam username from DB")
            return None
        if not row:
            return None
        try:
            account = row["account_name"]
        except (KeyError, TypeError):  # pragma: no cover - defensive
            account = row[0]  # type: ignore[index]
        account = str(account or "").strip()
        return account or None

    def _load_config_from_env(self) -> SteamMasterConfig:
        username = (os.getenv("STEAM_USERNAME") or "").strip()
        password = (os.getenv("STEAM_PASSWORD") or "").strip() or None

        if not username:
            inferred = self._infer_username_from_db()
            if inferred:
                username = inferred
                log.info(
                    "Using Steam username from stored refresh token: %s",
                    username,
                )

        if not username:
            raise SteamMasterDisabled(
                "Missing STEAM_USERNAME (and no stored steam_refresh_tokens entry).",
            )

        guard_timeout = 300.0
        guard_timeout_env = (os.getenv("STEAM_GUARD_TIMEOUT") or "").strip()
        if guard_timeout_env:
            try:
                guard_timeout = max(30.0, float(guard_timeout_env))
            except ValueError:
                log.warning(
                    "Invalid STEAM_GUARD_TIMEOUT=%r – using default %.1fs",
                    guard_timeout_env,
                    guard_timeout,
                )

        config = SteamMasterConfig(username=username, password=password, guard_timeout=guard_timeout)

        if not config.password:
            log.info(
                "Steam password missing – username '%s' available for refresh-token login only.",
                config.username,
            )

        return config

    # ------------- DB: Refresh-Token persistieren/lesen (optional nutzbar)
    def _persist_refresh_token(self, token: str) -> None:
        if not self.config:
            log.warning("Cannot persist Steam refresh token – config missing")
            return
        account = self.config.username or "unknown"
        try:
            db.execute(
                """
                INSERT INTO steam_refresh_tokens(account_name, refresh_token, received_at)
                VALUES(?, ?, strftime('%s','now'))
                ON CONFLICT(account_name) DO UPDATE SET
                  refresh_token=excluded.refresh_token,
                  received_at=excluded.received_at
                """,
                (account, token),
            )
            log.debug("Stored Steam refresh token for %s", account)
        except Exception:
            log.exception("Failed to persist Steam refresh token")

    def _load_refresh_token(self) -> Optional[str]:
        if not self.config:
            return None
        account = self.config.username or "unknown"
        try:
            row = db.query_one(
                "SELECT refresh_token FROM steam_refresh_tokens WHERE account_name = ?",
                (account,),
            )
        except Exception:
            log.exception("Failed to load stored Steam refresh token")
            return None
        if row and row[0]:
            return str(row[0])
        return None

    # ------------- Steam Events
    async def _on_login(self) -> None:
        if not self.config:
            log.warning("Steam login event received without config – ignoring")
            return
        display_name = getattr(self.client.user, "name", None) or self.config.username
        log.info("Steam account logged in as %s", display_name)
        token = getattr(self.client, "refresh_token", None)
        if token:
            self._persist_refresh_token(token)

    async def _on_ready(self) -> None:
        # Optional: Freunde laden etc.
        try:
            friends: Iterable[Friend] = await self.client.user.friends()  # type: ignore
            log.info("Steam client ready (friends=%s)", len(list(friends)))
        except Exception:
            log.info("Steam client ready")
        self._ready.set()

    async def _on_disconnect(self) -> None:
        log.warning("Steam connection lost – waiting for manual login again")
        self._ready.clear()

    async def _on_invite(self, invite) -> None:  # type: ignore[override]
        if hasattr(invite, "accept"):
            try:
                await invite.accept()
                log.info(
                    "Accepted friend invite from %s",
                    getattr(invite, "author", None) and getattr(invite.author, "name", invite.author),
                )
            except Exception:
                log.exception("Failed to accept friend invite")

    async def _on_user_update(self, before: User, after: User) -> None:  # type: ignore[override]
        # Hier könntest du später Präsenz/Status pflegen
        pass

    # ------------- Lifecycle
    @commands.Cog.listener()
    async def on_ready(self):
        # keinen Auto-Login starten – nur Runner für sauberes Stop/Start
        if self.client is None:
            if self._disabled_reason:
                log.info("Steam master cog loaded without active client: %s", self._disabled_reason)
            return
        if self._runner_task is None:
            self._runner_task = asyncio.create_task(self._runner_loop(), name="SteamMasterRunner")

    async def _runner_loop(self) -> None:
        """Hält den Client-Kontext offen zwischen Logins."""
        while not self._stop.is_set():
            await asyncio.sleep(1.0)
        log.info("Steam master runner stopped")

    async def cog_unload(self):
        self._stop.set()
        if self._runner_task and not self._runner_task.done():
            self._runner_task.cancel()
        if self.client is None:
            return
        try:
            async with self.client:
                await self.client.logout()  # falls eingeloggt
        except Exception:
            pass

    async def _ensure_client_available(self, ctx: commands.Context) -> bool:
        if self.client is None or self.config is None:
            reason = self._disabled_reason or "Steam-Client nicht initialisiert."
            await ctx.reply(f"❌ Steam Master ist deaktiviert: {reason}")
            return False
        return True

    # ------------- Commands
    @commands.command(name="sg")
    @commands.has_permissions(administrator=True)
    async def cmd_guard(self, ctx: commands.Context, code: str):
        """Übermittelt den Steam Guard Code."""
        ok = self.guard_codes.submit(code)
        message = "✅ Code angenommen." if ok else "❌ Ungültiger Code."
        if self.client is None:
            reason = self._disabled_reason or "Steam Master ist derzeit deaktiviert."
            message += f"\n⚠️ Hinweis: {reason}"
        await ctx.reply(message)

    @commands.command(name="steam_login")
    @commands.has_permissions(administrator=True)
    async def cmd_login(self, ctx: commands.Context):
        """
        Startet einen Login mit Username/Passwort.
        Wartet auf !sg CODE und loggt dann ein (kein Auto-Login).
        """
        if not STEAM_AVAILABLE:
            return await ctx.reply("❌ steamio nicht installiert.")

        if not await self._ensure_client_available(ctx):
            return

        if not self.config.password:
            return await ctx.reply(
                "❌ STEAM_PASSWORD fehlt – Login mit Benutzername/Passwort nicht möglich."
                " Nutze ggf. `!steam_login_token`.",
            )

        await ctx.reply("⏳ Warte auf Guard-Code via `!sg CODE` …")

        # Warte auf Code (blockierend bis !sg)
        code = await self.guard_codes.wait_for_code(timeout=None)
        # Code nochmal zurück in die Queue legen, falls steamio später code() aufruft
        self.guard_codes.submit(code)

        try:
            async with self.client:
                await self.client.login(
                    self.config.username,
                    self.config.password,
                    # keine shared/identity secrets – explizit deaktiviert
                )
                await ctx.reply("✅ Login versucht – warte auf Steam …")
                await self.client.wait_for("ready")  # „ready“ Event
        except Exception as exc:
            log.exception("Login failed")
            return await ctx.reply(f"❌ Login fehlgeschlagen: {exc!s}")

        await ctx.reply("✅ Steam ist online.")

    @commands.command(name="steam_login_token")
    @commands.has_permissions(administrator=True)
    async def cmd_login_token(self, ctx: commands.Context):
        """
        Optional: Login mit gespeicherten Refresh-Token (NICHT automatisch).
        """
        if not STEAM_AVAILABLE:
            return await ctx.reply("❌ steamio nicht installiert.")

        if not await self._ensure_client_available(ctx):
            return

        token = self._load_refresh_token()
        if not token:
            return await ctx.reply("❌ Kein gespeicherter Refresh-Token gefunden.")

        try:
            async with self.client:
                await self.client.login(refresh_token=token)
                await ctx.reply("✅ Login mit Refresh-Token versucht – warte auf Steam …")
                await self.client.wait_for("ready")
        except Exception as exc:
            log.exception("Login with token failed")
            return await ctx.reply(f"❌ Login mit Token fehlgeschlagen: {exc!s}")

        await ctx.reply("✅ Steam ist online (Token).")

    @commands.command(name="steam_logout")
    @commands.has_permissions(administrator=True)
    async def cmd_logout(self, ctx: commands.Context):
        """Loggt den Steam-Client aus, falls eingeloggt."""
        if not await self._ensure_client_available(ctx):
            return
        try:
            async with self.client:
                await self.client.logout()
        except Exception:
            pass
        await ctx.reply("🔌 Steam wurde (falls eingeloggt) ausgeloggt.")

    @commands.command(name="steam_status")
    @commands.has_permissions(administrator=True)
    async def cmd_status(self, ctx: commands.Context):
        """Zeigt Online-/Offline-Status und einige Eckdaten."""
        has_token = bool(self._load_refresh_token())
        configured_user = self.config.username if self.config else self._infer_username_from_db() or "—"
        if self.client is None:
            reason = self._disabled_reason or "Steam-Client nicht initialisiert."
            await ctx.reply(
                f"Steam: **offline**\n"
                f"User: `{configured_user}`\n"
                f"Refresh-Token gespeichert: **{'ja' if has_token else 'nein'}**\n"
                f"Status: {reason}"
            )
            return

        online = self._ready.is_set()
        name = getattr(getattr(self.client, "user", None), "name", None) or configured_user
        extra = "Login: **manuell** via `!sg CODE` (kein Auto-Login)."
        if self.config and not self.config.password:
            extra += "\nHinweis: Kein STEAM_PASSWORD gesetzt – verwende `!steam_login_token`."

        await ctx.reply(
            f"Steam: **{'online' if online else 'offline'}**\n"
            f"User: `{name}`\n"
            f"Refresh-Token gespeichert: **{'ja' if has_token else 'nein'}**\n"
            f"{extra}",
        )


# ------------- Cog-Setup
async def setup(bot: commands.Bot):
    await bot.add_cog(SteamMasterCog(bot))
