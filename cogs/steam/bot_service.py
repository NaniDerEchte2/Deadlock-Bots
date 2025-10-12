from __future__ import annotations

"""Steam-Bot Service-Implementierung innerhalb des ``cogs.steam``-Pakets."""

import asyncio
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Dict, Iterable, List, Optional

import aiohttp

# --- robuste steam/steamio-Imports mit Fallbacks ---
try:  # pragma: no cover - optional dependency guard
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
except Exception as exc:  # pragma: no cover - runtime safety für Umgebungen ohne steam
    Client = object  # type: ignore[assignment]
    Intents = object  # type: ignore[assignment]
    PersonaState = object  # type: ignore[assignment]
    UserInvite = object  # type: ignore[assignment]
    Friend = object  # type: ignore[assignment]
    User = object  # type: ignore[assignment]
    STEAM_AVAILABLE = False
    logging.getLogger(__name__).warning("steam package not available: %r", exc)

from service import db


def _missing_steam_message() -> str:
    """Provide a helpful installation hint for the missing ``steam`` package."""
    python_exe = Path(sys.executable).resolve()
    python_cmd = f'"{python_exe}"' if " " in str(python_exe) else str(python_exe)
    return (
        "steam package is required to start the SteamBotService. Install it via "
        f"{python_cmd} -m pip install steamio to match the bot environment."
    )


log = logging.getLogger(__name__)


@dataclass(slots=True)
class FriendPresence:
    """Snapshot information for a friend currently tracked by the Steam bot."""

    steam_id: str
    name: str
    persona_state: str
    app_id: Optional[int]
    app_name: Optional[str]
    rich_presence: Dict[str, str]
    last_update: float

    @property
    def rich_presence_text(self) -> Optional[str]:
        for key in ("steam_display", "status", "matchmode", "mode", "map", "state", "status_text"):
            value = self.rich_presence.get(key)
            if value:
                return value
        if self.rich_presence:
            # deterministic order for debugging
            key, value = next(iter(sorted(self.rich_presence.items())))
            if isinstance(value, str) and value.strip():
                return value
        return None

    def format_line(self) -> str:
        pieces: List[str] = []
        if self.app_name:
            pieces.append(self.app_name)
        rp = self.rich_presence_text
        if rp and rp not in pieces:
            pieces.append(rp)
        detail = " – ".join(pieces) if pieces else ""
        state = self.persona_state or "Unknown"
        if detail:
            return f"**{self.name}** ({state}) – {detail}"
        return f"**{self.name}** ({state})"


class GuardCodeManager:
    """Koordiniert Steam-Guard-Codes, die via Discord eingesendet werden."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=1)
        self._waiters = 0
        self._lock = asyncio.Lock()

    async def wait_for_code(self, timeout: Optional[float] = None) -> str:
        """Warte, bis ein Guard-Code eingesendet wurde (z. B. über !sg CODE)."""
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
        """Guard-Code von Discord-Admins entgegennehmen."""
        cleaned = str(code or "").strip()
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
    """Steam-Client-Subklasse, die Guard-Codes von Discord bezieht."""

    def __init__(self, guard_codes: GuardCodeManager, *, guard_timeout: float = 300.0, **options):
        if not STEAM_AVAILABLE:  # pragma: no cover - sanity check
            raise RuntimeError("steam package is required to create DiscordSteamClient")
        intents = options.pop("intents", None)
        if intents is None and hasattr(Intents, "Users"):
            intents = getattr(Intents, "Users", 0) | getattr(Intents, "Chat", 0)
        super().__init__(intents=intents, **options)  # type: ignore[arg-type]
        self._guard_codes = guard_codes
        self._guard_timeout = guard_timeout

    async def code(self) -> str:  # pragma: no cover - requires runtime interaction
        # Wird von steamio aufgerufen, wenn ein Guard-Code gebraucht wird.
        log.warning("Steam Guard challenge received – waiting for Discord input…")
        return await self._guard_codes.wait_for_code(self._guard_timeout)


@dataclass(slots=True)
class SteamBotConfig:
    username: Optional[str]
    password: Optional[str]
    shared_secret: Optional[str] = None
    identity_secret: Optional[str] = None
    refresh_token: Optional[str] = None
    refresh_token_path: Optional[str] = None
    account_name: Optional[str] = None
    web_api_key: Optional[str] = None
    deadlock_app_id: str = "1422450"
    status_interval: float = 60.0
    guard_timeout: float = 300.0
    friend_request_interval: float = 20.0
    quick_invite_interval: float = 300.0
    quick_invite_pool: int = 5
    quick_invite_duration: int = 30 * 24 * 3600
    quick_invite_limit: int = 1


class SteamBotService:
    """Kapselt die Steam-Client-Verbindung und Hintergrund-Loops."""

    def __init__(self, config: SteamBotConfig, guard_codes: GuardCodeManager) -> None:
        if not STEAM_AVAILABLE:
            raise RuntimeError(_missing_steam_message())

        self.config = config
        self.guard_codes = guard_codes
        self.client = DiscordSteamClient(guard_codes, guard_timeout=config.guard_timeout)

        self._ready = asyncio.Event()
        self._status_dirty = asyncio.Event()
        self._stop = asyncio.Event()
        self._deadlock_friends: Dict[str, FriendPresence] = {}
        self._status_callbacks: List[Callable[[List[FriendPresence]], Awaitable[None]]] = []
        self._connection_callbacks: List[Callable[[bool], Awaitable[None]]] = []
        self._web_session: Optional[aiohttp.ClientSession] = None

        # background tasks
        self._runner_task: Optional[asyncio.Task[None]] = None
        self._friend_task: Optional[asyncio.Task[None]] = None
        self._status_task: Optional[asyncio.Task[None]] = None
        self._invite_task: Optional[asyncio.Task[None]] = None
        self._last_refresh_source: Optional[str] = None

        # steam events registrieren
        self.client.event(self._on_login)
        self.client.event(self._on_ready)
        self.client.event(self._on_disconnect)
        self.client.event(self._on_invite)
        self.client.event(self._on_user_update)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        if self._runner_task:
            return
        self._stop.clear()
        self._runner_task = asyncio.create_task(self._run_client(), name="SteamBotRunner")
        self._friend_task = asyncio.create_task(self._friend_request_loop(), name="SteamFriendQueue")
        self._status_task = asyncio.create_task(self._status_loop(), name="SteamStatusLoop")
        self._invite_task = asyncio.create_task(self._quick_invite_loop(), name="SteamQuickInvites")

    async def stop(self) -> None:
        self._stop.set()
        for task in (self._status_task, self._friend_task, self._invite_task, self._runner_task):
            if task and not task.done():
                task.cancel()
        tasks = [t for t in (self._status_task, self._friend_task, self._invite_task, self._runner_task) if t]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._status_task = self._friend_task = self._invite_task = self._runner_task = None
        if self._web_session and not self._web_session.closed:
            await self._web_session.close()
        self._ready.clear()

    async def wait_until_ready(self) -> None:
        await self._ready.wait()

    def register_status_callback(self, callback: Callable[[List[FriendPresence]], Awaitable[None]]) -> None:
        self._status_callbacks.append(callback)

    def register_connection_callback(self, callback: Callable[[bool], Awaitable[None]]) -> None:
        self._connection_callbacks.append(callback)

    # ------------------------------------------------------------------
    # Steam event handlers
    # ------------------------------------------------------------------
    async def _on_login(self) -> None:
        display_name = (
            getattr(self.client.user, "name", None)
            or self.config.account_name
            or self.config.username
            or "unknown"
        )
        log.info("Steam account logged in as %s", display_name)
        token = getattr(self.client, "refresh_token", None)
        if token:
            self._persist_refresh_token(token)

    async def _on_ready(self) -> None:
        log.info("Steam client ready (friends=%s)", len(await self.client.user.friends()))
        await self._refresh_friend_snapshot()
        self._ready.set()
        self._status_dirty.set()
        await self._emit_connection(True)

    async def _on_disconnect(self) -> None:
        log.warning("Steam connection lost – waiting for reconnect")
        self._ready.clear()
        await self._emit_connection(False)

    async def _on_invite(self, invite) -> None:  # type: ignore[override]
        # Duck-Typing statt isinstance(UserInvite) – robuster über Versionen
        if hasattr(invite, "accept"):  # pragma: no branch
            try:
                await invite.accept()
                log.info(
                    "Accepted friend invite from %s",
                    getattr(invite, "author", None) and getattr(invite.author, "name", invite.author),
                )
            except Exception:  # pragma: no cover
                log.exception("Failed to accept friend invite")

    async def _on_user_update(self, before: User, after: User) -> None:  # type: ignore[override]
        if self._update_deadlock_friend(after):
            self._status_dirty.set()

    # ------------------------------------------------------------------
    # Token-Laden
    # ------------------------------------------------------------------
    def _persist_refresh_token(self, token: str) -> None:
        account = self.config.account_name or self.config.username or "unknown"
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
        path = self.config.refresh_token_path
        if not path:
            return
        candidate = Path(path)
        try:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            candidate.write_text(token.strip() + "\n", encoding="utf-8")
            log.debug("Wrote Steam refresh token to %s", candidate)
        except OSError:
            log.exception("Failed to write refresh token to %s", candidate)

    def _load_persisted_refresh_token(self) -> Optional[str]:
        account = self.config.account_name or self.config.username or "unknown"
        try:
            row = db.query_one(
                "SELECT refresh_token FROM steam_refresh_tokens WHERE account_name = ?",
                (account,),
            )
        except Exception:
            log.exception("Failed to load stored Steam refresh token")
            return None
        if row:
            return str(row[0])
        return None

    def _load_external_refresh_token(self) -> Optional[str]:
        path = self.config.refresh_token_path
        if not path:
            return None
        candidate = Path(path)
        try:
            if not candidate.exists():
                return None
            token = candidate.read_text(encoding="utf-8").strip()
        except OSError:
            log.exception("Failed to read refresh token from %s", candidate)
            return None
        if token:
            return token
        return None

    def _select_refresh_token(self) -> Optional[str]:
        """Prio: externe Datei → Konfiguration → DB. Gibt None, wenn keiner vorhanden."""
        for source, loader in (
            ("external file", self._load_external_refresh_token),
            ("configuration", lambda: self.config.refresh_token),
            ("database", self._load_persisted_refresh_token),
        ):
            token = loader()
            if token:
                if self._last_refresh_source != source:
                    log.info("Using Steam refresh token from %s", source)
                    self._last_refresh_source = source
                return token
        if self._last_refresh_source != "interactive":
            log.info("No refresh token available – waiting for Steam Guard input command (!sg CODE)")
            self._last_refresh_source = "interactive"
        return None

    # ------------------------------------------------------------------
    # Background loops
    # ------------------------------------------------------------------
    async def _run_client(self) -> None:
        backoff = 5.0
        while not self._stop.is_set():
            try:
                # 1) Versuche Login mit Refresh-Token
                refresh_token = self._select_refresh_token()
                if refresh_token:
                    async with self.client:  # pragma: no cover - requires live Steam
                        await self.client.login(refresh_token=refresh_token)
                        await self.client.wait_for("logout")
                else:
                    # 2) KEIN Auto-Login! Warten, bis du manuell per !sg CODE einen Guard-Code lieferst
                    log.info("Awaiting manual Steam Guard code via Discord command (!sg CODE). No auto login.")
                    # Warte „endlos“ (bis stop) – Wunschverhalten: kein automatischer Versuch
                    # Sobald ein Code kommt, starten wir den Passwort-Login; der Client ruft dann code() auf,
                    # welches sofort den bereits vorliegenden Code zurückgibt.
                    code = await self.guard_codes.wait_for_code(timeout=None)  # blockierend warten
                    # Optional: Code kurz „zurücklegen“, falls steamio code() später aufruft (Queue ist bereits gefüllt)
                    self.guard_codes.submit(code)  # sicherstellen, dass der Code in der Queue liegt

                    username = self.config.username
                    password = self.config.password
                    if not (username and password):
                        raise RuntimeError("Steam login requires credentials when no refresh token is available")

                    async with self.client:  # pragma: no cover - requires live Steam
                        await self.client.login(
                            username,
                            password,
                            shared_secret=self.config.shared_secret,
                            identity_secret=self.config.identity_secret,
                            # refresh_token bleibt None
                        )
                        await self.client.wait_for("logout")

            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("Steam client crashed; retrying in %.1fs", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
            else:
                backoff = 5.0
                if not self._stop.is_set():
                    log.warning("Steam client logged out unexpectedly; reconnecting…")
        log.info("Steam client runner stopped")

    # ------------------------------------------------------------------
    # Status/Invites/Friends
    # ------------------------------------------------------------------
    async def _friend_request_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.wait_until_ready()
            except asyncio.CancelledError:
                break
            try:
                await self._process_friend_queue()
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("Error processing Steam friend queue")
            await asyncio.sleep(self.config.friend_request_interval)

    async def _status_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.wait_until_ready()
            except asyncio.CancelledError:
                break
            try:
                await asyncio.wait_for(self._status_dirty.wait(), timeout=self.config.status_interval)
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                break
            self._status_dirty.clear()
            try:
                await self._broadcast_status()
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("Failed to broadcast Steam friend status")

    async def _quick_invite_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.wait_until_ready()
            except asyncio.CancelledError:
                break
            try:
                await self._ensure_quick_invites()
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("Failed to maintain Steam quick invites")
            await asyncio.sleep(self.config.quick_invite_interval)

    # ------------------------------------------------------------------
    # Friend queue + quick invites
    # ------------------------------------------------------------------
    async def _process_friend_queue(self) -> None:
        rows = db.query_all(
            """
            SELECT steam_id, attempts, status
            FROM steam_friend_requests
            WHERE status = 'pending'
            ORDER BY requested_at ASC
            LIMIT 10
            """
        )
        if not rows:
            return
        for row in rows:
            steam_id = str(row["steam_id"])
            try:
                friend = self.client.user.get_friend(int(steam_id))  # type: ignore[arg-type]
                if friend is not None:
                    db.execute(
                        "UPDATE steam_friend_requests SET status='sent', last_attempt=strftime('%s','now') WHERE steam_id=?",
                        (steam_id,),
                    )
                    continue
                user = await self.client.fetch_user(int(steam_id))
                await user.add()
                db.execute(
                    """
                    UPDATE steam_friend_requests
                    SET status='sent', last_attempt=strftime('%s','now'), attempts=attempts+1, error=NULL
                    WHERE steam_id=?
                    """,
                    (steam_id,),
                )
                log.info("Queued Steam friend request for %s", steam_id)
            except Exception as exc:
                db.execute(
                    """
                    UPDATE steam_friend_requests
                    SET error=?, last_attempt=strftime('%s','now'), attempts=attempts+1
                    WHERE steam_id=?
                    """,
                    (str(exc), steam_id),
                )
                log.warning("Failed to send Steam friend request to %s: %s", steam_id, exc)

    async def _ensure_quick_invites(self) -> None:
        if not self.config.web_api_key:
            return
        row = db.query_one(
            """
            SELECT COUNT(*)
            FROM steam_quick_invites
            WHERE status = 'available'
              AND (expires_at IS NULL OR expires_at > strftime('%s','now'))
            """
        )
        available = int(row[0]) if row else 0
        missing = max(0, self.config.quick_invite_pool - available)
        for _ in range(missing):
            await self._create_quick_invite()

    async def _create_quick_invite(self) -> None:
        session = await self._ensure_session()
        url = "https://api.steampowered.com/IPlayerService/CreateFriendInviteToken/v1/"
        payload = {
            "key": self.config.web_api_key,
            "steamid": str(getattr(self.client.user, "id64", "")),
            "invite_duration": self.config.quick_invite_duration,
            "invite_limit": self.config.quick_invite_limit,
        }
        async with session.post(url, data=payload, timeout=20) as resp:  # pragma: no cover - network I/O
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"CreateFriendInviteToken HTTP {resp.status}: {text[:200]}")
            data = await resp.json()
        response = data.get("response", {}) if isinstance(data, dict) else {}
        token = str(response.get("token") or response.get("invite_token") or "").strip()
        link = str(response.get("invite_link") or response.get("inviteurl") or "").strip()
        if not token and "tokens" in response:
            tokens = response.get("tokens") or []
            if tokens:
                token = str(tokens[0].get("token") or tokens[0].get("invite_token") or "")
                link = str(tokens[0].get("invite_link") or tokens[0].get("inviteurl") or link)
        if not token:
            raise RuntimeError(f"Unexpected CreateFriendInviteToken response: {data}")
        expires_at = int(time.time()) + int(response.get("invite_duration") or self.config.quick_invite_duration)
        if not link:
            link = f"https://s.team/p/{token}"
        db.execute(
            """
            INSERT INTO steam_quick_invites(token, invite_link, invite_limit, invite_duration, created_at, expires_at, status, last_seen)
            VALUES(?, ?, ?, ?, strftime('%s','now'), ?, 'available', strftime('%s','now'))
            ON CONFLICT(token) DO UPDATE SET
              invite_link=excluded.invite_link,
              invite_limit=excluded.invite_limit,
              invite_duration=excluded.invite_duration,
              expires_at=excluded.expires_at,
              status='available',
              last_seen=excluded.last_seen
            """,
            (token, link, self.config.quick_invite_limit, self.config.quick_invite_duration, expires_at),
        )
        log.info("Generated new Steam quick invite link: %s", link)


__all__ = [
    "FriendPresence",
    "GuardCodeManager",
    "SteamBotConfig",
    "SteamBotService",
]
