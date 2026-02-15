"""Twitch EventSub Webhook handler – empfängt und verifiziert eingehende Notifications."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
from typing import Awaitable, Callable, Dict, Optional, Set

from aiohttp import web

EventCallback = Callable[[str, str, Dict], Awaitable[None]]

# Twitch EventSub Message-Types
MSG_TYPE_NOTIFICATION = "notification"
MSG_TYPE_CHALLENGE = "webhook_callback_verification"
MSG_TYPE_REVOCATION = "revocation"

# Max allowed age for incoming messages (Twitch recommendation: 10 minutes)
_MAX_MESSAGE_AGE_SECONDS = 600

# How many message IDs to keep in the deduplication set
_SEEN_ID_LIMIT = 2000


class EventSubWebhookHandler:
    """
    Empfängt und verifiziert Twitch EventSub Webhook Notifications.

    Registriert sich als Route-Handler in der aiohttp-App.
    Callbacks werden per sub_type registriert und bei eingehenden Notifications aufgerufen.
    """

    def __init__(self, secret: str, logger: Optional[logging.Logger] = None):
        if not secret:
            raise ValueError("EventSub webhook secret darf nicht leer sein")
        self._secret = secret.encode("utf-8")
        self.log = logger or logging.getLogger("TwitchStreams.EventSubWebhook")
        self._callbacks: Dict[str, EventCallback] = {}
        self._seen_message_ids: Set[str] = set()
        self._seen_ids_list: list = []  # für LRU-style Begrenzung

    def set_callback(self, sub_type: str, callback: EventCallback) -> None:
        """Registriert einen Callback für einen bestimmten EventSub-Typ."""
        self._callbacks[sub_type] = callback
        self.log.debug("EventSub Webhook: Callback gesetzt für '%s'", sub_type)

    def _verify_signature(self, message_id: str, timestamp: str, raw_body: bytes, signature: str) -> bool:
        """
        Verifiziert die HMAC-SHA256 Signatur einer Twitch EventSub Nachricht.

        Formel: HMAC-SHA256(secret, message_id + timestamp + raw_body)
        """
        if not signature or not signature.startswith("sha256="):
            return False
        expected_sig = signature[7:]  # Strip "sha256="
        message = message_id.encode("utf-8") + timestamp.encode("utf-8") + raw_body
        computed = hmac.new(self._secret, message, hashlib.sha256).hexdigest()
        return hmac.compare_digest(computed, expected_sig)

    def _is_message_too_old(self, timestamp: str) -> bool:
        """Prüft ob der Timestamp älter als _MAX_MESSAGE_AGE_SECONDS ist (Replay-Schutz)."""
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - dt).total_seconds()
            return age > _MAX_MESSAGE_AGE_SECONDS
        except Exception:
            self.log.debug("EventSub Webhook: Konnte Timestamp nicht parsen: %r", timestamp)
            return True  # Bei Parse-Fehler: Nachricht ablehnen

    def _is_duplicate(self, message_id: str) -> bool:
        """Duplikat-Erkennung anhand der Message-ID."""
        return message_id in self._seen_message_ids

    def _track_message_id(self, message_id: str) -> None:
        """Speichert Message-ID für spätere Duplikat-Erkennung (LRU-Begrenzung)."""
        if message_id in self._seen_message_ids:
            return
        self._seen_message_ids.add(message_id)
        self._seen_ids_list.append(message_id)
        # Übergelaufene IDs entfernen
        while len(self._seen_ids_list) > _SEEN_ID_LIMIT:
            old_id = self._seen_ids_list.pop(0)
            self._seen_message_ids.discard(old_id)

    async def handle_request(self, request: web.Request) -> web.Response:
        """
        Haupt-Handler für eingehende EventSub Webhook Requests.

        Twitch sendet drei Message-Types:
        1. webhook_callback_verification – Challenge-Response bei neuen Subscriptions
        2. notification – Eigentliche Event-Notification
        3. revocation – Subscription wurde widerrufen
        """
        # --- 1. Raw Body lesen (vor JSON-Parsing für HMAC-Verifikation) ---
        try:
            raw_body = await request.read()
        except Exception:
            self.log.warning("EventSub Webhook: Konnte Body nicht lesen")
            return web.Response(status=400)

        # --- 2. Headers extrahieren ---
        message_id = request.headers.get("Twitch-Eventsub-Message-Id", "")
        timestamp = request.headers.get("Twitch-Eventsub-Message-Timestamp", "")
        signature = request.headers.get("Twitch-Eventsub-Message-Signature", "")
        message_type = request.headers.get("Twitch-Eventsub-Message-Type", "")
        sub_type = request.headers.get("Twitch-Eventsub-Subscription-Type", "")

        # --- 3. Signatur verifizieren ---
        if not self._verify_signature(message_id, timestamp, raw_body, signature):
            self.log.warning(
                "EventSub Webhook: Signatur-Verifizierung fehlgeschlagen (msg_id=%r, type=%r)",
                message_id,
                message_type,
            )
            return web.Response(status=403)

        # --- 4. Replay-Schutz: Timestamp prüfen ---
        if self._is_message_too_old(timestamp):
            self.log.warning(
                "EventSub Webhook: Nachricht zu alt (ts=%r, id=%r) – abgelehnt",
                timestamp,
                message_id,
            )
            return web.Response(status=403)

        # --- 5. JSON parsen ---
        try:
            import json
            data = json.loads(raw_body)
        except Exception:
            self.log.warning("EventSub Webhook: Konnte Body nicht als JSON parsen")
            return web.Response(status=400)

        # --- 6. Nach Message-Type verarbeiten ---
        if message_type == MSG_TYPE_CHALLENGE:
            challenge = data.get("challenge", "")
            if not challenge:
                self.log.error("EventSub Webhook: Challenge-Request ohne challenge-Feld")
                return web.Response(status=400)
            self.log.debug(
                "EventSub Webhook: Challenge für '%s' beantwortet",
                data.get("subscription", {}).get("type", sub_type),
            )
            return web.Response(
                text=challenge,
                content_type="text/plain",
                status=200,
            )

        if message_type == MSG_TYPE_REVOCATION:
            revoked_type = data.get("subscription", {}).get("type", sub_type)
            reason = data.get("subscription", {}).get("status", "unknown")
            self.log.warning(
                "EventSub Webhook: Subscription widerrufen: type=%r reason=%r",
                revoked_type,
                reason,
            )
            return web.Response(status=204)

        if message_type != MSG_TYPE_NOTIFICATION:
            # Unbekannter Typ – trotzdem mit 200 antworten damit Twitch nicht retried
            self.log.debug("EventSub Webhook: Unbekannter message_type=%r – ignoriert", message_type)
            return web.Response(status=204)

        # --- 7. Duplikat-Schutz ---
        if self._is_duplicate(message_id):
            self.log.debug("EventSub Webhook: Duplikat-Nachricht ignoriert (id=%r)", message_id)
            return web.Response(status=204)
        self._track_message_id(message_id)

        # --- 8. Notification dispatchen ---
        asyncio.create_task(self._dispatch_notification(data, sub_type), name="eventsub.webhook.dispatch")
        return web.Response(status=204)

    async def _dispatch_notification(self, data: dict, sub_type: str) -> None:
        """Verarbeitet eine Notification und ruft den passenden Callback auf."""
        # Webhook notifications come as top-level {"subscription": ..., "event": ...}
        # while EventSub WebSocket messages use {"payload": {"subscription": ..., "event": ...}}.
        payload = data.get("payload")
        if isinstance(payload, dict) and ("event" in payload or "subscription" in payload):
            envelope = payload
        else:
            envelope = data

        subscription = envelope.get("subscription") or data.get("subscription") or {}
        actual_sub_type = subscription.get("type") or sub_type

        callback = self._callbacks.get(actual_sub_type)
        if not callback:
            self.log.debug("EventSub Webhook: Kein Callback für type=%r", actual_sub_type)
            return

        event = envelope.get("event") or data.get("event") or {}
        condition = subscription.get("condition") or {}

        # Broadcaster-ID und Login aus dem Event extrahieren (je nach Sub-Type)
        broadcaster_id = str(
            event.get("broadcaster_user_id")
            or event.get("to_broadcaster_user_id")
            or event.get("user_id")
            or condition.get("broadcaster_user_id")
            or condition.get("to_broadcaster_user_id")
            or ""
        ).strip()
        broadcaster_login = str(
            event.get("broadcaster_user_login")
            or event.get("to_broadcaster_user_login")
            or event.get("user_login")
            or ""
        ).strip().lower()

        if not broadcaster_id:
            self.log.debug(
                "EventSub Webhook: Notification für type=%r ohne broadcaster_id – ignoriert",
                actual_sub_type,
            )
            return

        try:
            await callback(broadcaster_id, broadcaster_login, event)
        except Exception:
            self.log.exception(
                "EventSub Webhook: Callback fehlgeschlagen für type=%r broadcaster=%r",
                actual_sub_type,
                broadcaster_login or broadcaster_id,
            )
