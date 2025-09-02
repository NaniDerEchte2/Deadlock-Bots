import json
import socket
import threading
from typing import Callable, Optional

# Einfaches JSON-Lines Protokoll:
# - Jede Nachricht ist ein JSON-Objekt und endet mit '\n'
# - Dadurch ist Framing trivial und robust.


def send_json(sock: socket.socket, obj: dict) -> None:
    data = (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")
    sock.sendall(data)


def recv_json(sock: socket.socket, bufsize: int = 65536) -> Optional[dict]:
    """
    Liest bis zum nächsten '\n' und parsed JSON.
    Gibt None zurück, wenn die Verbindung endet.
    """
    chunks = []
    while True:
        chunk = sock.recv(bufsize)
        if not chunk:
            return None  # Verbindung beendet
        chunks.append(chunk)
        if b"\n" in chunk:
            break
    raw = b"".join(chunks)
    # Nur bis zum ersten Newline
    line, _, _rest = raw.partition(b"\n")
    try:
        return json.loads(line.decode("utf-8"))
    except json.JSONDecodeError:
        return {"ok": False, "error": "invalid_json"}


class JSONLineServer:
    def __init__(self, host: str, port: int, handler: Callable[[dict], dict], backlog: int = 20):
        self.host = host
        self.port = port
        self.handler = handler
        self.backlog = backlog
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._sock:
            return
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.host, self.port))
        self._sock.listen(self.backlog)
        self._thread = threading.Thread(target=self._serve_forever, name="JSONLineServer", daemon=True)
        self._thread.start()

    def _serve_forever(self) -> None:
        while not self._stop.is_set():
            try:
                client, _addr = self._sock.accept()
            except OSError:
                break
            threading.Thread(target=self._serve_client, args=(client,), daemon=True).start()

    def _serve_client(self, client: socket.socket) -> None:
        with client:
            while not self._stop.is_set():
                req = recv_json(client)
                if req is None:
                    break
                try:
                    resp = self.handler(req) or {"ok": True}
                except Exception as e:
                    resp = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                try:
                    send_json(client, resp)
                except OSError:
                    break

    def stop(self) -> None:
        self._stop.set()
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
