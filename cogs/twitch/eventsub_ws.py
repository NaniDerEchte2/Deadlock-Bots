from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

import aiohttp

EventCallback = Callable[[str, str, Dict], Awaitable[None]]


class EventSubReconnect(Exception):
    """Signals that Twitch requested a reconnect to a new URL."""
    pass


class EventSubWSListener:
    """
    Consolidated EventSub WebSocket Client.
    Handles multiple subscription types (stream.online, stream.offline, etc.)
    on a single WebSocket connection to save transport slots (limit 3 per Client ID).
    """

    def __init__(
        self,
        api,
        logger: Optional[logging.Logger] = None,
        token_resolver: Optional[Callable[[str], Awaitable[Optional[str]]]] = None,
    ):
        self.api = api
        self.log = logger or logging.getLogger("TwitchStreams.EventSubWS")
        self._token_resolver = token_resolver
        self._stop = False
        self._ws_url = "wss://eventsub.wss.twitch.tv/ws"
        self._subscriptions: List[Tuple[str, str, Dict]] = [] # (sub_type, broadcaster_id, condition)
        self._callbacks: Dict[str, EventCallback] = {} # sub_type -> callback

    def stop(self) -> None:
        """Signal the listener to stop."""
        self._stop = True

    def add_subscription(self, sub_type: str, broadcaster_id: str, condition: Optional[Dict] = None):
        """Add a subscription to be registered on connect."""
        cond = condition or {"broadcaster_user_id": str(broadcaster_id)}
        self._subscriptions.append((sub_type, str(broadcaster_id), cond))

    def set_callback(self, sub_type: str, callback: EventCallback):
        """Set callback for a specific subscription type."""
        self._callbacks[sub_type] = callback

    async def run(self) -> None:
        """Start the listener and handle reconnects."""
        if not self._subscriptions:
            self.log.debug("EventSub WS: No subscriptions added. Listener not started.")
            return

        is_reconnect = False
        while not self._stop:
            try:
                await self._run_once(is_reconnect=is_reconnect)
                # Normal end, not a reconnect request
                is_reconnect = False
            except EventSubReconnect as exc:
                new_url = exc.args[0]
                self.log.info("EventSub WS: Reconnect requested. New URL: %s", new_url)
                if new_url:
                    self._ws_url = new_url
                is_reconnect = True
                continue
            except asyncio.CancelledError:
                raise
            except Exception:
                self.log.exception("EventSub WS listener crashed - Reconnecting in 10s")
                await asyncio.sleep(10)
                self._ws_url = "wss://eventsub.wss.twitch.tv/ws"
                is_reconnect = False

    async def _run_once(self, is_reconnect: bool = False) -> None:
        session = self.api.get_http_session()
        ws_url = self._ws_url
        async with session.ws_connect(ws_url, heartbeat=20) as ws:
            session_id = await self._wait_for_welcome(ws)
            if not session_id:
                self.log.error("EventSub WS: No session_id received, aborting.")
                return

            if not is_reconnect:
                await self._register_all_subscriptions(session_id)
            else:
                self.log.info("EventSub WS: Reconnect successful - Subscriptions are migrated by Twitch.")

            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_message(msg.json())
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break

    async def _wait_for_welcome(self, ws) -> Optional[str]:
        try:
            msg = await ws.receive(timeout=10)
        except asyncio.TimeoutError:
            self.log.error("EventSub WS: Welcome timeout")
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

    async def _resolve_token(self) -> Optional[str]:
        """Resolve the token to be used for all subscriptions on this WS."""
        if not self._token_resolver:
            return None
        try:
            # We pass a dummy ID because we expect the same bot token for all
            token = await self._token_resolver("bot")
            if not token:
                return None
            token = token.strip()
            return token[6:] if token.lower().startswith("oauth:") else token
        except Exception:
            self.log.debug("EventSub WS: Could not resolve bot token", exc_info=True)
            return None

    async def _register_all_subscriptions(self, session_id: str) -> None:
        token = await self._resolve_token()
        if not token:
            self.log.error("EventSub WS: No user token available. Subscriptions will fail.")
            return

        # Twitch limit: 3000 subscriptions per verified session, 300 per unverified.
        # We assume we stay within these limits.
        
        # Batch subscriptions to avoid hitting API rate limits too hard
        for i, (sub_type, bid, condition) in enumerate(self._subscriptions):
            try:
                await self.api.subscribe_eventsub_websocket(
                    session_id=session_id,
                    sub_type=sub_type,
                    condition=condition,
                    oauth_token=token,
                )
                self.log.debug("EventSub WS: Subscribed %s for %s", sub_type, bid)
                
                # Small delay every 5 subs to be nice to the API
                if (i + 1) % 5 == 0:
                    await asyncio.sleep(0.5)
                    
            except Exception as e:
                msg = str(e)
                if "429" in msg or "transport limit exceeded" in msg.lower():
                    self.log.error("EventSub WS: Transport limit exceeded (429) during subscription of %s for %s. Aborting further subs on this session.", sub_type, bid)
                    break
                self.log.error("EventSub WS: Subscription failed for %s (%s): %s", bid, sub_type, e)

    async def _handle_message(self, data: Dict) -> None:
        meta = data.get("metadata") or {}
        mtype = meta.get("message_type")
        if mtype == "session_keepalive":
            return
        if mtype == "session_reconnect":
            target = data.get("payload", {}).get("session", {}).get("reconnect_url")
            self.log.info("EventSub WS: Reconnect requested to %s", target)
            raise EventSubReconnect(target)
        if mtype != "notification":
            return

        payload = data.get("payload") or {}
        subscription = payload.get("subscription") or {}
        sub_type = subscription.get("type")
        
        callback = self._callbacks.get(sub_type)
        if not callback:
            return

        event = payload.get("event") or {}
        broadcaster_id = str(event.get("broadcaster_user_id") or "").strip()
        broadcaster_login = (event.get("broadcaster_user_login") or "").strip().lower()
        
        if not broadcaster_id:
            return
            
        try:
            await callback(broadcaster_id, broadcaster_login, event)
        except Exception:
            self.log.exception("EventSub WS: Callback failed for %s (%s)", broadcaster_id, sub_type)
