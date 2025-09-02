import os
import socket
import time
from typing import Any, Dict, Optional

from .socket_bus import send_json, recv_json


class WorkerUnavailable(Exception):
    """Wird geworfen, wenn keine Verbindung zum Worker hergestellt werden kann."""
    pass


class WorkerProxy:
    """
    Leichtgewichtiger Client, der Requests als JSON-Lines an den Worker-Bot sendet
    und auf eine JSON-Antwort wartet.
    """
    def __init__(self, host: str = None, port: int = None, connect_timeout: float = 1.0, io_timeout: float = 5.0, retries: int = 1):
        self.host = host or os.getenv("SOCKET_HOST", "127.0.0.1")
        self.port = port or int(os.getenv("SOCKET_PORT", "45679"))
        self.connect_timeout = connect_timeout
        self.io_timeout = io_timeout
        self.retries = retries
        self._sock: Optional[socket.socket] = None

        # Beim Init nicht hart fehlschlagen – Main Bot soll weiterlaufen und ggf. Fallback machen.
        self._ensure_connected(silent=True)

    def _ensure_connected(self, silent: bool = False) -> None:
        if self._sock:
            return
        last_err: Optional[Exception] = None
        for _ in range(self.retries + 1):
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(self.connect_timeout)
                s.connect((self.host, self.port))
                s.settimeout(self.io_timeout)
                self._sock = s
                return
            except Exception as e:
                last_err = e
                time.sleep(0.2)
        if not silent:
            raise WorkerUnavailable(f"Worker nicht erreichbar auf {self.host}:{self.port} - {last_err}")

    @property
    def is_connected(self) -> bool:
        return self._sock is not None

    def _reset(self) -> None:
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None

    def request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Sendet eine Operation an den Worker.
        Gibt {"ok": False, "error": "..."} zurück, wenn kein Worker erreichbar ist
        (damit der Aufrufer lokal fallbacken kann).
        """
        try:
            self._ensure_connected(silent=False)
        except WorkerUnavailable as e:
            return {"ok": False, "error": str(e)}

        try:
            send_json(self._sock, payload)
            resp = recv_json(self._sock)
            if resp is None:
                # Verbindung abgerissen – nächster Call versucht Reconnect
                self._reset()
                return {"ok": False, "error": "Worker-Verbindung abgebrochen"}
            return resp
        except Exception as e:
            # I/O-Fehler -> Verbindung resetten, damit nächster Versuch reconnectet
            self._reset()
            return {"ok": False, "error": f"I/O-Fehler: {type(e).__name__}: {e}"}

    # Komfort-Wrapper
    def edit_channel(self, channel_id: int, *, name: Optional[str] = None,
                     user_limit: Optional[int] = None, bitrate: Optional[int] = None) -> Dict[str, Any]:
        payload = {"op": "edit_channel", "channel_id": channel_id}
        if name is not None:
            payload["name"] = name
        if user_limit is not None:
            payload["user_limit"] = user_limit
        if bitrate is not None:
            payload["bitrate"] = bitrate
        return self.request(payload)

    def set_permissions(self, channel_id: int, target_id: int, overwrite: Dict[str, Any]) -> Dict[str, Any]:
        payload = {"op": "set_permissions", "channel_id": channel_id, "target_id": target_id, "overwrite": overwrite}
        return self.request(payload)
