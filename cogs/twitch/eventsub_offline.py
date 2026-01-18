from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable, Dict, Iterable, List, Optional

import aiohttp

EventCallback = Callable[[str, Optional[str]], Awaitable[None]]


class EventSubOfflineListener:
    """Minimal EventSub WebSocket Client für stream.offline Benachrichtigungen."""

    def __init__(
        self,
        api,
        logger: Optional[logging.Logger] = None,
        token_resolver: Optional[Callable[[str], Awaitable[Optional[str]]]] = None,
    ):
        self.api = api
        self.log = logger or logging.getLogger("TwitchStreams.EventSub")
        self._token_resolver = token_resolver
        self._stop = False
        # Wichtig: Kein client_id-Query-Param (führt zu 403). Twitch erwartet hier nur die nackte WS-URL.
        self._ws_url = "wss://eventsub.wss.twitch.tv/ws"

    def stop(self) -> None:
        """Signalisiert dem Listener, sich zu beenden."""
        self._stop = True

    async def run(self, broadcaster_ids: Iterable[str], on_offline: EventCallback) -> None:
        """Starte Listener und hält Reconnects selbstständig am Laufen."""
        ids = [str(bid) for bid in broadcaster_ids if bid]
        if not ids:
            self.log.debug("EventSub: keine Broadcaster IDs zum Subscriben – Listener gestoppt")
            return

        while not self._stop:
            try:
                await self._run_once(ids, on_offline)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.log.exception("EventSub offline listener abgestürzt – Reconnect in 10s")
                await asyncio.sleep(10)

    async def _run_once(self, broadcaster_ids: List[str], on_offline: EventCallback) -> None:
        session = self.api.get_http_session()
        ws_url = self._ws_url
        async with session.ws_connect(ws_url, heartbeat=20) as ws:
            session_id = await self._wait_for_welcome(ws)
            if not session_id:
                self.log.error("EventSub: keine session_id erhalten, breche ab")
                return

            await self._subscribe_offline(session_id, broadcaster_ids)

            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_message(msg.json(), on_offline)
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break

    async def _wait_for_welcome(self, ws) -> Optional[str]:
        try:
            msg = await ws.receive(timeout=10)
        except asyncio.TimeoutError:
            self.log.error("EventSub: welcome timeout")
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
        """Resolve a per-broadcaster user token if available."""
        if not self._token_resolver:
            return None
        try:
            token = await self._token_resolver(str(broadcaster_id))
            if not token:
                return None
            token = token.strip()
            return token[6:] if token.lower().startswith("oauth:") else token
        except Exception:
            self.log.debug("EventSub: konnte User-Token nicht laden für %s", broadcaster_id, exc_info=True)
            return None

    async def _subscribe_offline(self, session_id: str, broadcaster_ids: List[str]) -> None:
        # Twitch WebSocket EventSub erlaubt max. 30 Subs pro Connection.
        # WICHTIG: Alle Subs auf einer Connection müssen vom SELBEN User autorisiert sein.
        # Wir nutzen hierzu das Bot-User-Token (via resolver).
        
        for bid in broadcaster_ids[:30]:
            token = await self._resolve_user_token(bid)
            try:
                await self.api.subscribe_eventsub_websocket(
                    session_id=session_id,
                    sub_type="stream.offline",
                    condition={"broadcaster_user_id": str(bid)},
                    oauth_token=token, # Nutzt Bot-Token falls vorhanden, sonst Fallback auf App-Token (wird fehlschlagen bei WS)
                )
                self.log.debug("EventSub: subscribed stream.offline for %s (auth=%s)", bid, "user" if token else "app")
            except Exception:
                self.log.exception("EventSub: subscription failed for %s", bid)

    async def _handle_message(self, data: Dict, on_offline: EventCallback) -> None:
        meta = data.get("metadata") or {}
        mtype = meta.get("message_type")
        if mtype == "session_keepalive":
            return
        if mtype == "session_reconnect":
            target = data.get("payload", {}).get("session", {}).get("reconnect_url")
            self.log.info("EventSub: reconnect requested to %s", target)
            # Wir beenden run_once, outer loop reconnectet.
            raise RuntimeError("EventSub reconnect requested")
        if mtype != "notification":
            return

        payload = data.get("payload") or {}
        subscription = payload.get("subscription") or {}
        if subscription.get("type") != "stream.offline":
            return
        event = payload.get("event") or {}
        broadcaster_id = str(event.get("broadcaster_user_id") or "").strip()
        broadcaster_login = (event.get("broadcaster_user_login") or "").strip().lower()
        if not broadcaster_id:
            return
        try:
            await on_offline(broadcaster_id, broadcaster_login or None)
        except Exception:
            self.log.exception("EventSub: callback failed for %s", broadcaster_id)
