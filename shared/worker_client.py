# shared/worker_client.py
import os, asyncio, logging
from typing import Optional
from shared.socket_bus import SocketClient

log = logging.getLogger(__name__)

class WorkerProxy:
    """
    Dünner Proxy für Bot1 -> Bot2. Wenn TV_WORKER_ENABLED != "1", fällt er auf "local" zurück (Caller soll das dann direkt machen).
    """
    def __init__(self, bot, host: Optional[str]=None, port: Optional[int]=None, secret: Optional[str]=None):
        self.bot = bot
        self.enabled = os.getenv("TV_WORKER_ENABLED", "0") == "1"
        self.client = SocketClient(
            host or os.getenv("TV_WORKER_HOST", "127.0.0.1"),
            int(port or os.getenv("TV_WORKER_PORT", "45678")),
            secret or os.getenv("TV_WORKER_SECRET", "")
        )

    async def edit_channel(self, channel_id: int, *, name: Optional[str]=None, user_limit: Optional[int]=None,
                           bitrate: Optional[int]=None, reason: Optional[str]=None) -> bool:
        if not self.enabled:
            return False
        payload = {"channel_id": channel_id, "name": name, "user_limit": user_limit, "bitrate": bitrate, "reason": reason}
        resp = await self.client.send("channel_edit", payload)
        return bool(resp.get("ok"))

    async def set_connect(self, channel_id: int, target_id: int, state: Optional[bool]) -> bool:
        if not self.enabled:
            return False
        payload = {"channel_id": channel_id, "target_id": target_id, "connect": state}
        resp = await self.client.send("set_permissions_connect", payload)
        return bool(resp.get("ok"))

    async def clear_overwrite(self, channel_id: int, target_id: int) -> bool:
        if not self.enabled:
            return False
        payload = {"channel_id": channel_id, "target_id": target_id}
        resp = await self.client.send("clear_overwrite", payload)
        return bool(resp.get("ok"))

    async def create_voice(self, guild_id: int, category_id: Optional[int], name: str, *,
                           user_limit: Optional[int], bitrate: Optional[int], reason: Optional[str]) -> Optional[int]:
        if not self.enabled:
            return None
        payload = {"guild_id": guild_id, "category_id": category_id, "name": name,
                   "user_limit": user_limit, "bitrate": bitrate, "reason": reason}
        resp = await self.client.send("create_voice", payload)
        if resp.get("ok"):
            return int(resp["data"]["channel_id"])
        return None

    async def delete_channel(self, channel_id: int, reason: Optional[str]=None) -> bool:
        if not self.enabled:
            return False
        payload = {"channel_id": channel_id, "reason": reason}
        resp = await self.client.send("delete_channel", payload)
        return bool(resp.get("ok"))

    async def move_member(self, guild_id: int, user_id: int, dest_channel_id: int, reason: Optional[str]=None) -> bool:
        if not self.enabled:
            return False
        payload = {"guild_id": guild_id, "user_id": user_id, "dest_channel_id": dest_channel_id, "reason": reason}
        resp = await self.client.send("move_member", payload)
        return bool(resp.get("ok"))
