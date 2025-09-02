# shared/socket_bus.py
import asyncio, json, uuid, logging
from typing import Callable, Dict, Any, Awaitable, Optional

log = logging.getLogger(__name__)

class SocketServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 45678, secret: str = ""):
        self.host = host
        self.port = port
        self.secret = secret
        self._server: Optional[asyncio.base_events.Server] = None
        self._handlers: Dict[str, Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]] = {}

    def add_handler(self, msg_type: str, handler: Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]):
        self._handlers[msg_type] = handler

    async def _handle_conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            raw = await reader.readline()
            if not raw:
                writer.close(); await writer.wait_closed(); return
            try:
                msg = json.loads(raw.decode("utf-8"))
            except Exception:
                writer.write(json.dumps({"ok": False, "error": "bad_json"}).encode()+b"\n")
                await writer.drain(); writer.close(); await writer.wait_closed(); return

            if not isinstance(msg, dict) or msg.get("secret") != self.secret:
                writer.write(json.dumps({"ok": False, "error": "auth_failed"}).encode()+b"\n")
                await writer.drain(); writer.close(); await writer.wait_closed(); return

            mtype = msg.get("type")
            handler = self._handlers.get(mtype)
            if not handler:
                writer.write(json.dumps({"ok": False, "error": f"no_handler:{mtype}"}).encode()+b"\n")
                await writer.drain(); writer.close(); await writer.wait_closed(); return

            try:
                res = await handler(msg.get("data") or {})
                out = {"ok": True, "data": res}
            except Exception as e:
                log.exception("handler error")
                out = {"ok": False, "error": str(e)}
            writer.write(json.dumps(out).encode()+b"\n")
            await writer.drain()
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def start(self):
        self._server = await asyncio.start_server(self._handle_conn, self.host, self.port)
        sock = ", ".join(str(s.getsockname()) for s in self._server.sockets)
        log.info(f"SocketServer listening on {sock} (secret set: {bool(self.secret)})")

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None


class SocketClient:
    def __init__(self, host: str = "127.0.0.1", port: int = 45678, secret: str = ""):
        self.host = host
        self.port = port
        self.secret = secret

    async def send(self, msg_type: str, data: Dict[str, Any], timeout: float = 5.0) -> Dict[str, Any]:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(self.host, self.port), timeout=timeout)
        req = {"type": msg_type, "data": data, "secret": self.secret, "request_id": str(uuid.uuid4())}
        writer.write(json.dumps(req).encode()+b"\n"); await writer.drain()
        raw = await asyncio.wait_for(reader.readline(), timeout=timeout)
        try:
            resp = json.loads(raw.decode("utf-8")) if raw else {"ok": False, "error": "no_response"}
        except Exception:
            resp = {"ok": False, "error": "bad_json_response"}
        try:
            writer.close(); await writer.wait_closed()
        except Exception:
            pass
        return resp
