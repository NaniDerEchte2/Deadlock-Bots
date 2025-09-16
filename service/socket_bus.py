import json
import socket
import threading
import logging
from typing import Callable, Optional, Dict, Any

logger = logging.getLogger(__name__)


def send_json(sock: socket.socket, obj: Dict[str, Any]) -> None:
    """Sende ein einzelnes JSON-Objekt als Zeile (newline-delimited)."""
    data = (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")
    sock.sendall(data)


def recv_json(sock: socket.socket, bufsize: int = 65536) -> Optional[Dict[str, Any]]:
    """
    Empfängt bis zum ersten Newline und parsed ein einzelnes JSON-Objekt.
    Gibt None zurück, wenn die Gegenstelle sauber geschlossen hat.
    """
    chunks = []
    while True:
        chunk = sock.recv(bufsize)
        if not chunk:
            return None
        chunks.append(chunk)
        if b"\n" in chunk:
            break

    raw = b"".join(chunks)
    line, _, _rest = raw.partition(b"\n")
    try:
        return json.loads(line.decode("utf-8"))
    except json.JSONDecodeError:
        return {"ok": False, "error": "invalid_json"}


class JSONLineServer:
    """
    Sehr schlanker TCP-Server:
      - Nimmt newline-delimited JSON entgegen,
      - ruft handler(dict)->dict auf,
      - antwortet ebenfalls newline-delimited JSON.
    """

    def __init__(self, host: str, port: int, handler: Callable[[dict], dict], backlog: int = 20):
        self.host = host
        self.port = port
        self.handler = handler
        self.backlog = backlog
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self) -> None:
        """Startet den Server-Thread (idempotent)."""
        if self._sock:
            return
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.host, self.port))
        srv.listen(self.backlog)
        self._sock = srv
        self._thread = threading.Thread(target=self._serve_forever, name="JSONLineServer", daemon=True)
        self._thread.start()

    def _serve_forever(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                client, _addr = self._sock.accept()
            except OSError:
                # Wird geworfen, wenn der Socket während accept() geschlossen wurde
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
                    # Handler-Fehler gehen als strukturierte Fehlermeldung zurück
                    resp = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                try:
                    send_json(client, resp)
                except OSError:
                    # Verbindung beim Senden abgerissen
                    break

    def stop(self) -> None:
        """Stoppt den Server sauber und wartet kurz auf den Thread."""
        self._stop.set()
        if self._sock:
            try:
                # Schließen triggert OSError in accept(), der sauber abgefangen wird.
                self._sock.close()
            except OSError as e:
                # Kein "leeres except": wir loggen das als Debug, da Beenden weitergehen soll.
                logger.debug("Ignoriere OSError beim Socket-Schließen in stop(): %r", e)
            finally:
                self._sock = None

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
            self._thread = None
