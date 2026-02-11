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
        self._failed = False
        self._ws_url = "wss://eventsub.wss.twitch.tv/ws"
        self._subscriptions: List[Tuple[str, str, Dict]] = [] # (sub_type, broadcaster_id, condition)
        self._callbacks: Dict[str, EventCallback] = {} # sub_type -> callback
        self._session_id: Optional[str] = None  # Stored for dynamic subscriptions

    @staticmethod
    def _condition_key(condition: Optional[Dict]) -> Tuple[Tuple[str, str], ...]:
        """Normalize condition payload for stable duplicate checks."""
        if not condition:
            return tuple()
        return tuple(sorted((str(k), str(v)) for k, v in condition.items()))

    @staticmethod
    def _is_already_exists_error(exc: Exception) -> bool:
        status = getattr(exc, "status", None)
        message = f"{getattr(exc, 'message', '')} {exc}".lower()
        return status == 409 and "already exists" in message

    def _has_subscription(self, sub_type: str, broadcaster_id: str, condition: Optional[Dict]) -> bool:
        bid = str(broadcaster_id)
        cond_key = self._condition_key(condition)
        for existing_type, existing_bid, existing_cond in self._subscriptions:
            if existing_type != sub_type or existing_bid != bid:
                continue
            if self._condition_key(existing_cond) == cond_key:
                return True
        return False

    def _track_subscription(self, sub_type: str, broadcaster_id: str, condition: Optional[Dict]) -> bool:
        bid = str(broadcaster_id)
        cond = condition or {"broadcaster_user_id": bid}
        if self._has_subscription(sub_type, bid, cond):
            return False
        self._subscriptions.append((sub_type, bid, cond))
        return True

    @property
    def cost(self) -> int:
        """
        Calculate the total cost of all registered subscriptions.
        stream.online and stream.offline (v1) cost 1 each.
        Max cost per transport: 10 (Twitch limit)
        """
        if self._failed:
            return 9999 # Mark as full/broken
        # In a more advanced version, we could look up costs per sub_type.
        return len(self._subscriptions)

    @property
    def subscription_count(self) -> int:
        """Number of tracked subscriptions assigned to this listener."""
        return len(self._subscriptions)
    
    @property
    def has_capacity(self) -> bool:
        """Check if this listener can accept more subscriptions (max 10 per transport)"""
        return self.cost < 10 and not self._failed

    @property
    def is_failed(self) -> bool:
        return self._failed

    @property
    def is_ready(self) -> bool:
        """True once Twitch assigned a session_id for dynamic subscriptions."""
        return bool(self._session_id) and not self._failed

    async def wait_until_ready(self, timeout: float = 8.0, poll_interval: float = 0.1) -> bool:
        """
        Wait until this listener has a valid EventSub session_id.

        Returns:
            True if ready within timeout, False otherwise.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout)
        while loop.time() < deadline:
            if self.is_ready:
                return True
            if self._failed or self._stop:
                return False
            await asyncio.sleep(max(0.01, poll_interval))
        return self.is_ready

    def stop(self) -> None:
        """Signal the listener to stop."""
        self._stop = True

    def add_subscription(self, sub_type: str, broadcaster_id: str, condition: Optional[Dict] = None):
        """Add a subscription to be registered on connect."""
        self._track_subscription(sub_type, str(broadcaster_id), condition)

    async def add_subscription_dynamic(
        self,
        sub_type: str,
        broadcaster_id: str,
        condition: Optional[Dict] = None,
        oauth_token: Optional[str] = None
    ) -> bool:
        """
        Add and register a subscription dynamically while the listener is running.

        Returns:
            True if successful, False otherwise.
        """
        if not self._session_id:
            self.log.error("EventSub WS: Keine Session ID verfügbar für dynamische Subscription")
            return False

        cond = condition or {"broadcaster_user_id": str(broadcaster_id)}
        if self._has_subscription(sub_type, str(broadcaster_id), cond):
            self.log.debug(
                "EventSub WS: Dynamic subscription already tracked: %s for %s",
                sub_type,
                broadcaster_id,
            )
            return True

        if self.cost >= 10:
            self.log.error("EventSub WS: Listener ist voll (10/10), kann %s für %s nicht hinzufügen", sub_type, broadcaster_id)
            return False

        # Resolve token if not provided
        token = oauth_token
        if not token:
            token = await self._resolve_token()
        if not token:
            self.log.error("EventSub WS: Kein Token verfügbar für dynamische Subscription")
            return False

        try:
            await self.api.subscribe_eventsub_websocket(
                session_id=self._session_id,
                sub_type=sub_type,
                condition=cond,
                oauth_token=token,
            )
            # Add to tracking list
            self._track_subscription(sub_type, str(broadcaster_id), cond)
            self.log.info("EventSub WS: Dynamische Subscription hinzugefügt: %s für %s (%d/%d)", sub_type, broadcaster_id, self.cost, 10)
            return True
        except Exception as e:
            if self._is_already_exists_error(e):
                self._track_subscription(sub_type, str(broadcaster_id), cond)
                self.log.info(
                    "EventSub WS: Dynamic subscription already exists (409): %s for %s",
                    sub_type,
                    broadcaster_id,
                )
                return True
            self.log.error("EventSub WS: Dynamische Subscription fehlgeschlagen für %s (%s): %s", broadcaster_id, sub_type, e)
            return False

    def set_callback(self, sub_type: str, callback: EventCallback):
        """Set callback for a specific subscription type."""
        self._callbacks[sub_type] = callback

    async def run(self) -> None:
        """Start the listener and handle reconnects."""
        if not self._subscriptions:
            self.log.debug("EventSub WS: Starting listener without initial subscriptions.")

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
            except Exception as exc:
                if "No session_id received" in str(exc):
                    self.log.warning(
                        "EventSub WS: No session_id on connect/reconnect (url=%s). Retrying fresh endpoint in 1s.",
                        self._ws_url,
                    )
                    await asyncio.sleep(1)
                else:
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
                raise ConnectionError("EventSub WS: No session_id received, aborting.")

            if not is_reconnect:
                await self._register_all_subscriptions(session_id)
            else:
                self.log.info("EventSub WS: Reconnect successful - Subscriptions are migrated by Twitch.")

            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_message(msg.json())
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
        
        # If we exit the loop and we are not stopping, it's an error
        if not self._stop:
            raise ConnectionResetError("EventSub WS: Connection closed unexpectedly")

    async def _wait_for_welcome(self, ws) -> Optional[str]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 15

        while True:
            timeout = max(0.0, deadline - loop.time())
            if timeout <= 0:
                self.log.error("EventSub WS: Welcome timeout")
                return None
            try:
                msg = await ws.receive(timeout=timeout)
            except asyncio.TimeoutError:
                self.log.error("EventSub WS: Welcome timeout")
                return None

            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except Exception:
                    continue
                meta = data.get("metadata") or {}
                mtype = meta.get("message_type")
                if mtype == "session_welcome":
                    sess = data.get("payload", {}).get("session", {})
                    session_id = sess.get("id")

                    # Store session_id for dynamic subscriptions
                    if session_id:
                        self._session_id = session_id

                    return session_id
                if mtype == "session_reconnect":
                    target = data.get("payload", {}).get("session", {}).get("reconnect_url")
                    self.log.info("EventSub WS: Reconnect requested to %s", target)
                    raise EventSubReconnect(target)
                if mtype == "session_keepalive":
                    continue
                continue

            if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                return None

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
        if not self._subscriptions:
            self.log.debug("EventSub WS: No initial subscriptions to register.")
            return

        token = await self._resolve_token()
        if not token:
            self.log.error("EventSub WS: No user token available. Subscriptions will fail.")
            return

        # Twitch limits:
        # - Max 3 concurrent WebSocket connections (transports) per Client ID
        # - Max 10 subscriptions per transport (websocket transport subscriptions total cost)
        # - Max 100 subscriptions total per Client ID
        
        max_subs_per_transport = 10
        if len(self._subscriptions) > max_subs_per_transport:
            self.log.error(
                "EventSub WS: Versuche %d Subscriptions zu registrieren, aber Limit ist %d pro Transport! "
                "Nur die ersten %d werden registriert.",
                len(self._subscriptions), max_subs_per_transport, max_subs_per_transport
            )
            # Markiere Session als fehlgeschlagen wenn zu viele Subs
            self._failed = True
            self._subscriptions = self._subscriptions[:max_subs_per_transport]
        
        # Batch subscriptions to avoid hitting API rate limits too hard
        successful_count = 0
        for i, (sub_type, bid, condition) in enumerate(self._subscriptions):
            try:
                await self.api.subscribe_eventsub_websocket(
                    session_id=session_id,
                    sub_type=sub_type,
                    condition=condition,
                    oauth_token=token,
                )
                successful_count += 1
                self.log.debug("EventSub WS: Subscribed %s for %s (%d/%d)", sub_type, bid, successful_count, len(self._subscriptions))
                
                # Small delay every 3 subs to be nice to the API
                if (i + 1) % 3 == 0:
                    await asyncio.sleep(0.3)
                    
            except Exception as e:
                if self._is_already_exists_error(e):
                    successful_count += 1
                    self.log.info(
                        "EventSub WS: Subscription already exists (409): %s for %s",
                        sub_type,
                        bid,
                    )
                    continue
                msg = str(e)
                if "429" in msg or "transport limit exceeded" in msg.lower() or "websocket transport" in msg.lower():
                    # 429 Error - Transport limit erreicht
                    if "websocket transports limit exceeded" in msg.lower():
                        self.log.error(
                            "EventSub WS: Max WebSocket Transport Limit (3) erreicht! "
                            "Keine weiteren Transports möglich. Bitte alte Sessions beenden."
                        )
                        self._failed = True
                        break
                    
                    if "websocket transport subscriptions total cost exceeded" in msg.lower():
                        self.log.error(
                            "EventSub WS: Transport Subscription Cost Limit (%d) erreicht bei %s for %s. "
                            "Dieser Transport ist voll. Erstelle eine neue Session.",
                            max_subs_per_transport, sub_type, bid
                        )
                        self._failed = True
                        break
                    
                    # Generischer 429 - warte und retry einmal
                    self.log.warning(
                        "EventSub WS: Rate limit (429) hit during %s for %s – warte 2s und versuche einmal retry.",
                        sub_type, bid,
                    )
                    await asyncio.sleep(2)
                    try:
                        await self.api.subscribe_eventsub_websocket(
                            session_id=session_id,
                            sub_type=sub_type,
                            condition=condition,
                            oauth_token=token,
                        )
                        successful_count += 1
                        self.log.info("EventSub WS: Retry nach 429 erfolgreich: %s for %s", sub_type, bid)
                    except Exception as retry_err:
                        retry_msg = str(retry_err)
                        if "429" in retry_msg or "transport limit" in retry_msg.lower():
                            self.log.error(
                                "EventSub WS: Transport limit (429) auch nach Retry – Session als fehlgeschlagen markiert."
                            )
                            self._failed = True
                            break
                        self.log.error("EventSub WS: Retry für %s (%s) fehlgeschlagen: %s", bid, sub_type, retry_err)
                    continue
                self.log.error("EventSub WS: Subscription failed for %s (%s): %s", bid, sub_type, e)
        
        self.log.info(
            "EventSub WS: Subscription-Registrierung abgeschlossen: %d/%d erfolgreich",
            successful_count, len(self._subscriptions)
        )

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
        # Different EventSub types use different field names for the "target" broadcaster.
        broadcaster_id = str(
            event.get("broadcaster_user_id")
            or event.get("to_broadcaster_user_id")
            or event.get("user_id")
            or ""
        ).strip()
        broadcaster_login = str(
            event.get("broadcaster_user_login")
            or event.get("to_broadcaster_user_login")
            or event.get("user_login")
            or ""
        ).strip().lower()
        
        if not broadcaster_id:
            return
            
        try:
            await callback(broadcaster_id, broadcaster_login, event)
        except Exception:
            self.log.exception("EventSub WS: Callback failed for %s (%s)", broadcaster_id, sub_type)
