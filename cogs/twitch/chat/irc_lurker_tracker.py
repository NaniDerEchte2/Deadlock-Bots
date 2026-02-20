"""
IRC Lurker Tracker für Twitch Chat.

Nutzt eine separate IRC-Connection (parallel zum EventSub WebSocket) um:
- NAMES-Liste zu pollen (alle Chatters inkl. Lurker)
- JOIN/PART Events zu tracken
- Keine OAuth benötigt (anonymous oder App Token)

Twitch IRC: irc.chat.twitch.tv:6667 (oder SSL auf :443)
"""

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Dict, Optional, Set

from ..storage import get_conn

log = logging.getLogger("TwitchStreams.IRCLurkerTracker")


class IRCLurkerTracker:
    """
    Separate IRC-Connection für Lurker-Tracking UND Category-wide Message-Collection.

    Zwei Modi:
    1. PARTNER: Volle Lurker-Tracking (NAMES, JOIN/PART, Messages)
    2. CATEGORY: Nur Messages sammeln (keine Lurker-Daten)
    """

    def __init__(self, client_id: str, access_token: str):
        """
        client_id: Twitch Client ID
        access_token: App Access Token (NICHT User Token) oder Bot Token
        """
        self.client_id = client_id
        self.access_token = access_token
        self.nick = "justinfan12345"  # Anonymous Twitch IRC nick

        # Connection State
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self.connected = False
        self.running = False

        # Tracked Channels
        self.partner_channels: Set[str] = set()  # Partner: Volle Lurker-Tracking
        self.category_channels: Set[str] = set()  # Category: Nur Messages
        self.channel_chatters: Dict[
            str, Set[str]
        ] = {}  # channel -> set of nicks (nur Partner!)

        # Tasks
        self.connect_task: Optional[asyncio.Task] = None
        self.read_task: Optional[asyncio.Task] = None
        self.poll_task: Optional[asyncio.Task] = None

    async def start(self):
        """Start IRC connection and background tasks."""
        if self.running:
            log.warning("IRC Lurker Tracker already running")
            return

        self.running = True
        self.connect_task = asyncio.create_task(self._connection_loop())
        self.poll_task = asyncio.create_task(self._poll_names_loop())
        log.info("IRC Lurker Tracker started")

    async def stop(self):
        """Stop IRC connection and background tasks."""
        self.running = False

        if self.poll_task:
            self.poll_task.cancel()
            try:
                await self.poll_task
            except asyncio.CancelledError:
                log.debug("IRC: poll task cancelled")

        if self.read_task:
            self.read_task.cancel()
            try:
                await self.read_task
            except asyncio.CancelledError:
                log.debug("IRC: read task cancelled")

        if self.connect_task:
            self.connect_task.cancel()
            try:
                await self.connect_task
            except asyncio.CancelledError:
                log.debug("IRC: connect task cancelled")

        await self._disconnect()
        log.info("IRC Lurker Tracker stopped")

    async def _connect(self) -> bool:
        """Establish IRC connection to Twitch."""
        try:
            # Connect to Twitch IRC (non-SSL)
            self.reader, self.writer = await asyncio.open_connection(
                "irc.chat.twitch.tv", 6667
            )

            # Authenticate
            # Twitch IRC accepts "oauth:token" or anonymous (justinfan...)
            # Anonymous users can read chat but not send messages
            self.writer.write(f"PASS oauth:{self.access_token}\r\n".encode())
            self.writer.write(f"NICK {self.nick}\r\n".encode())
            await self.writer.drain()

            # Request capabilities (membership = JOIN/PART events, commands = NAMES)
            self.writer.write(b"CAP REQ :twitch.tv/membership twitch.tv/commands\r\n")
            await self.writer.drain()

            # Wait for connection acknowledgement
            while True:
                line = await asyncio.wait_for(self.reader.readline(), timeout=10.0)
                msg = line.decode("utf-8", errors="ignore").strip()
                log.debug("IRC: %s", msg)

                if msg.startswith(":tmi.twitch.tv 001"):
                    # Connection successful
                    self.connected = True
                    log.info("IRC connected successfully")
                    return True
                elif msg.startswith("PING"):
                    pong = msg.replace("PING", "PONG")
                    self.writer.write(f"{pong}\r\n".encode())
                    await self.writer.drain()

        except Exception:
            log.exception("IRC connection failed")
            self.connected = False
            return False

    async def _disconnect(self):
        """Close IRC connection."""
        if self.writer:
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception:
                log.debug("IRC: writer close failed", exc_info=True)

        self.connected = False
        self.reader = None
        self.writer = None
        log.info("IRC disconnected")

    async def _connection_loop(self):
        """Maintain IRC connection with auto-reconnect."""
        while self.running:
            try:
                if not self.connected:
                    log.info("IRC: Attempting to connect...")
                    success = await self._connect()

                    if success:
                        # Re-join all channels after reconnect
                        for channel in list(self.channels):
                            await self._join_channel(channel)

                        # Start read loop
                        if self.read_task:
                            self.read_task.cancel()
                        self.read_task = asyncio.create_task(self._read_loop())
                    else:
                        log.warning("IRC: Connection failed, retry in 30s")
                        await asyncio.sleep(30)
                else:
                    # Wait before checking again
                    await asyncio.sleep(10)

            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("IRC: Connection loop error")
                await asyncio.sleep(30)

    async def _read_loop(self):
        """Read and process IRC messages."""
        try:
            while self.running and self.connected and self.reader:
                line = await self.reader.readline()
                if not line:
                    log.warning("IRC: Connection closed by server")
                    self.connected = False
                    break

                msg = line.decode("utf-8", errors="ignore").strip()
                await self._handle_message(msg)

        except asyncio.CancelledError:
            log.debug("IRC: read loop cancelled")
        except Exception:
            log.exception("IRC: Read loop error")
            self.connected = False

    async def _handle_message(self, msg: str):
        """Process IRC message."""
        if not msg:
            return

        # PING/PONG keepalive
        if msg.startswith("PING"):
            pong = msg.replace("PING", "PONG")
            if self.writer:
                self.writer.write(f"{pong}\r\n".encode())
                await self.writer.drain()
            return

        # JOIN event: :nick!user@host JOIN #channel
        join_match = re.match(r":(\w+)!.+ JOIN #(\w+)", msg)
        if join_match:
            nick, channel = join_match.groups()
            await self._on_user_join(channel, nick)
            return

        # PART event: :nick!user@host PART #channel
        part_match = re.match(r":(\w+)!.+ PART #(\w+)", msg)
        if part_match:
            nick, channel = part_match.groups()
            await self._on_user_part(channel, nick)
            return

        # NAMES reply: :tmi.twitch.tv 353 nick = #channel :nick1 nick2 nick3
        names_match = re.match(r":\S+ 353 \S+ = #(\w+) :(.+)", msg)
        if names_match:
            channel, nicks_str = names_match.groups()
            nicks = nicks_str.split()
            await self._on_names_list(channel, nicks)
            return

        # End of NAMES: :tmi.twitch.tv 366 nick #channel :End of /NAMES list
        # (optional - we can process incrementally)

    async def _on_user_join(self, channel: str, nick: str):
        """Handle user joining channel."""
        channel = channel.lower()
        nick = nick.lower()

        if channel not in self.channel_chatters:
            self.channel_chatters[channel] = set()

        self.channel_chatters[channel].add(nick)
        log.debug("IRC: %s joined #%s", nick, channel)

        # Update DB
        await self._update_chatter_seen(channel, nick)

    async def _on_user_part(self, channel: str, nick: str):
        """Handle user leaving channel."""
        channel = channel.lower()
        nick = nick.lower()

        if channel in self.channel_chatters:
            self.channel_chatters[channel].discard(nick)
            log.debug("IRC: %s left #%s", nick, channel)

    async def _on_names_list(self, channel: str, nicks: list):
        """Handle NAMES list (all chatters in channel)."""
        channel = channel.lower()
        nicks_lower = [n.lower() for n in nicks]

        self.channel_chatters[channel] = set(nicks_lower)
        log.info("IRC: NAMES for #%s: %d chatters", channel, len(nicks_lower))

        # Update DB for all chatters
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

        try:
            with get_conn() as conn:
                # Get active session for this channel
                session_row = conn.execute(
                    "SELECT active_session_id FROM twitch_live_state WHERE LOWER(streamer_login) = ? AND is_live = 1",
                    (channel,),
                ).fetchone()

                if not session_row or not session_row[0]:
                    log.debug(
                        "IRC: No active session for #%s, skipping DB update", channel
                    )
                    return

                session_id = session_row[0]

                # Batch update/insert chatters
                existing = {
                    r[0]
                    for r in conn.execute(
                        "SELECT chatter_login FROM twitch_session_chatters WHERE session_id = ?",
                        (session_id,),
                    ).fetchall()
                }

                to_insert = []
                to_update = []

                for nick in nicks_lower:
                    if nick in existing:
                        to_update.append((now_iso, session_id, nick))
                    else:
                        to_insert.append(
                            (session_id, channel, nick, None, now_iso, now_iso)
                        )

                if to_update:
                    conn.executemany(
                        "UPDATE twitch_session_chatters SET last_seen_at = ? WHERE session_id = ? AND chatter_login = ?",
                        to_update,
                    )

                if to_insert:
                    conn.executemany(
                        """
                        INSERT OR IGNORE INTO twitch_session_chatters
                            (session_id, streamer_login, chatter_login, chatter_id,
                             first_message_at, messages, is_first_time_global,
                             seen_via_chatters_api, last_seen_at)
                        VALUES (?, ?, ?, ?, ?, 0, 0, 1, ?)
                        """,
                        to_insert,
                    )

                log.info(
                    "IRC: DB updated for #%s: %d inserts, %d updates",
                    channel,
                    len(to_insert),
                    len(to_update),
                )

        except Exception:
            log.exception("IRC: Failed to update DB for #%s", channel)

    async def _update_chatter_seen(self, channel: str, nick: str):
        """Update last_seen timestamp for a chatter."""
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

        try:
            with get_conn() as conn:
                session_row = conn.execute(
                    "SELECT active_session_id FROM twitch_live_state WHERE LOWER(streamer_login) = ? AND is_live = 1",
                    (channel,),
                ).fetchone()

                if not session_row or not session_row[0]:
                    return

                session_id = session_row[0]

                # Update or insert
                conn.execute(
                    """
                    INSERT INTO twitch_session_chatters
                        (session_id, streamer_login, chatter_login, chatter_id,
                         first_message_at, messages, is_first_time_global,
                         seen_via_chatters_api, last_seen_at)
                    VALUES (?, ?, ?, ?, ?, 0, 0, 1, ?)
                    ON CONFLICT(session_id, chatter_login) DO UPDATE SET
                        last_seen_at = excluded.last_seen_at
                    """,
                    (session_id, channel, nick, None, now_iso, now_iso),
                )

        except Exception:
            log.debug(
                "IRC: Failed to update chatter %s in #%s", nick, channel, exc_info=True
            )

    async def _join_channel(self, channel: str):
        """Join IRC channel."""
        if not self.connected or not self.writer:
            return False

        channel = channel.lower().lstrip("#")

        try:
            self.writer.write(f"JOIN #{channel}\r\n".encode())
            await self.writer.drain()
            log.info("IRC: Joined #%s", channel)
            return True
        except Exception:
            log.exception("IRC: Failed to join #%s", channel)
            return False

    async def _request_names(self, channel: str):
        """Request NAMES list for a channel."""
        if not self.connected or not self.writer:
            return

        channel = channel.lower().lstrip("#")

        try:
            self.writer.write(f"NAMES #{channel}\r\n".encode())
            await self.writer.drain()
        except Exception:
            log.debug("IRC: Failed to request NAMES for #%s", channel, exc_info=True)

    async def _poll_names_loop(self):
        """Periodically request NAMES for all channels (every 2 minutes)."""
        await asyncio.sleep(30)  # Wait for initial connection

        while self.running:
            try:
                if self.connected:
                    for channel in list(self.channels):
                        await self._request_names(channel)
                        await asyncio.sleep(1)  # Stagger requests

                await asyncio.sleep(120)  # Poll every 2 minutes

            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("IRC: NAMES poll loop error")
                await asyncio.sleep(60)

    # Public API

    async def track_channel(self, channel: str):
        """Add channel to tracking list."""
        channel = channel.lower().lstrip("#")

        if channel in self.channels:
            return

        self.channels.add(channel)

        if self.connected:
            await self._join_channel(channel)

        log.info("IRC: Now tracking #%s", channel)

    async def untrack_channel(self, channel: str):
        """Remove channel from tracking list."""
        channel = channel.lower().lstrip("#")

        if channel not in self.channels:
            return

        self.channels.discard(channel)

        if self.connected and self.writer:
            try:
                self.writer.write(f"PART #{channel}\r\n".encode())
                await self.writer.drain()
            except Exception:
                log.debug("IRC: failed to PART #%s", channel, exc_info=True)

        log.info("IRC: Stopped tracking #%s", channel)

    def get_chatters(self, channel: str) -> Set[str]:
        """Get current chatters for a channel."""
        channel = channel.lower().lstrip("#")
        return self.channel_chatters.get(channel, set()).copy()
