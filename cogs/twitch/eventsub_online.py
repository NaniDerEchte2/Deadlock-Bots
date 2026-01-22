from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable, Dict, Iterable, List, Optional

import aiohttp

EventCallback = Callable[[str, Optional[str]], Awaitable[None]]


class EventSubReconnect(Exception):
    """Signalisiert, dass Twitch einen Reconnect auf eine neue URL anfordert."""
    pass


class EventSubOnlineListener:
    """Minimal EventSub WebSocket Client für stream.online Benachrichtigungen."""

    def __init__(
        self,
        api,
        logger: Optional[logging.Logger] = None,
        token_resolver: Optional[Callable[[str], Awaitable[Optional[str]]]] = None,
    ):
        self.api = api
        self.log = logger or logging.getLogger("TwitchStreams.EventSub.Online")
        self._token_resolver = token_resolver
        self._stop = False
        # Twitch erwartet hier die nackte WS-URL ohne client_id Query-Param.
        self._ws_url = "wss://eventsub.wss.twitch.tv/ws"

    def stop(self) -> None:
        """Signalisiert dem Listener, sich zu beenden."""
        self._stop = True

    async def run(self, broadcaster_ids: Iterable[str], on_online: EventCallback) -> None:
        """Starte Listener und halte Reconnects selbstständig am Laufen."""
        ids = [str(bid) for bid in broadcaster_ids if bid]
        if not ids:
            self.log.debug("EventSub online: keine Broadcaster IDs zum Subscriben - Listener gestoppt")
            return

        is_reconnect = False
        while not self._stop:
            try:
                await self._run_once(ids, on_online, is_reconnect=is_reconnect)
                # Normales Ende (z.B. Close), kein Reconnect
                is_reconnect = False
            except EventSubReconnect as exc:
                new_url = exc.args[0]
                self.log.info("EventSub online: Reconnect requested. Neue URL: %s", new_url)
                if new_url:
                    self._ws_url = new_url
                is_reconnect = True
                continue
            except asyncio.CancelledError:
                raise
            except Exception:
                self.log.exception("EventSub online listener abgestürzt - Reconnect in 10s")
                await asyncio.sleep(10)
                # Reset auf Default-URL nach Crash
                self._ws_url = "wss://eventsub.wss.twitch.tv/ws"
                is_reconnect = False

    async def _run_once(self, broadcaster_ids: List[str], on_online: EventCallback, is_reconnect: bool = False) -> None:
        session = self.api.get_http_session()
        ws_url = self._ws_url
        async with session.ws_connect(ws_url, heartbeat=20) as ws:
            session_id = await self._wait_for_welcome(ws)
            if not session_id:
                self.log.error("EventSub online: keine session_id erhalten, breche ab")
                return

            if not is_reconnect:
                await self._subscribe_online(session_id, broadcaster_ids)
            else:
                self.log.info("EventSub online: Reconnect erfolgreich - Subscriptions werden von Twitch migriert.")

            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_message(msg.json(), on_online)
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break

    async def _wait_for_welcome(self, ws) -> Optional[str]:
        try:
            msg = await ws.receive(timeout=10)
        except asyncio.TimeoutError:
            self.log.error("EventSub online: welcome timeout")
            return None
        if msg.type != aiohttp.WSMsgType.TEXT:
            return None
        try:
            data = json.loads(msg.data)
        except Exception:
            return None
        meta = data.get("metadata") or {}
        if meta.get("message_type") != "session_welcome":
            return None
        sess = data.get("payload", {}).get("session", {})
        return sess.get("id")

    async def _resolve_user_token(self, broadcaster_id: str) -> Optional[str]:
        """Resolve a user token if available (all subs on one WS must share the same user)."""
        if not self._token_resolver:
            return None
        try:
            token = await self._token_resolver(str(broadcaster_id))
            if not token:
                return None
            token = token.strip()
            return token[6:] if token.lower().startswith("oauth:") else token
        except Exception:
            self.log.debug("EventSub online: konnte User-Token nicht laden für %s", broadcaster_id, exc_info=True)
            return None

    async def _subscribe_online(self, session_id: str, broadcaster_ids: List[str]) -> None:
        # Twitch WebSocket EventSub erlaubt max. 30 Subs pro Connection.
        # Alle Subs auf einer Connection müssen vom selben User autorisiert sein.
        for bid in broadcaster_ids[:30]:
            token = await self._resolve_user_token(bid)
            try:
                await self.api.subscribe_eventsub_websocket(
                    session_id=session_id,
                    sub_type="stream.online",
                    condition={"broadcaster_user_id": str(bid)},
                    oauth_token=token,
                )
                self.log.debug("EventSub online: subscribed stream.online für %s (auth=%s)", bid, "user" if token else "app")
            except Exception:
                self.log.exception("EventSub online: subscription failed for %s", bid)

    async def _handle_message(self, data: Dict, on_online: EventCallback) -> None:
        meta = data.get("metadata") or {}
        mtype = meta.get("message_type")
        if mtype == "session_keepalive":
            return
        if mtype == "session_reconnect":
            target = data.get("payload", {}).get("session", {}).get("reconnect_url")
            self.log.info("EventSub online: reconnect requested to %s", target)
            raise EventSubReconnect(target)
        if mtype != "notification":
            return

        payload = data.get("payload") or {}
        subscription = payload.get("subscription") or {}
        if subscription.get("type") != "stream.online":
            return
        event = payload.get("event") or {}
        broadcaster_id = str(event.get("broadcaster_user_id") or "").strip()
        broadcaster_login = (event.get("broadcaster_user_login") or "").strip().lower()
        if not broadcaster_id:
            return
        try:
            await on_online(broadcaster_id, broadcaster_login or None)
        except Exception:
            self.log.exception("EventSub online: callback failed for %s", broadcaster_id)
