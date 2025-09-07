"""
shared.worker_client
--------------------
Socket-Client für den lokalen/externen Worker-Prozess.
- Thread-safe (RLock) für parallele Calls aus Tasks/Events
- Auto-Reconnect mit (leichter) Exponential-Backoff
- Einheitliche Fehlerobjekte {"ok": False, "error": "..."}
- Convenience-Methoden für gängige Worker-OPs
- Optional: Bulk-Requests

ENV:
  SOCKET_HOST=127.0.0.1
  SOCKET_PORT=45679
  SOCKET_CONNECT_TIMEOUT=1.0
  SOCKET_IO_TIMEOUT=5.0
  SOCKET_RETRIES=1
"""

from __future__ import annotations

import os
import socket
import time
import threading
from typing import Any, Dict, Optional, Sequence

# kompatible Imports (relativ/absolut), je nach Projektstruktur
try:
    from .socket_bus import send_json, recv_json
except Exception:  # pragma: no cover
    from shared.socket_bus import send_json, recv_json  # type: ignore


class WorkerUnavailable(Exception):
    """Wird geworfen/benutzt, wenn kein Worker erreicht werden kann."""


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except Exception:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except Exception:
        return default


class WorkerProxy:
    """
    Kleiner TCP-Client mit JSON-RPC-ähnlichem Protokoll (send_json/recv_json).
    Ein Socket wird persistent gehalten und bei Fehlern automatisch neu aufgebaut.
    """

    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        connect_timeout: Optional[float] = None,
        io_timeout: Optional[float] = None,
        retries: Optional[int] = None,
        autoconnect: bool = True,
    ) -> None:
        self.host = (host or os.getenv("SOCKET_HOST") or "127.0.0.1").strip()
        self.port = int(port or _env_int("SOCKET_PORT", 45679))
        self.connect_timeout = float(connect_timeout if connect_timeout is not None else _env_float("SOCKET_CONNECT_TIMEOUT", 1.0))
        self.io_timeout = float(io_timeout if io_timeout is not None else _env_float("SOCKET_IO_TIMEOUT", 5.0))
        self.retries = int(retries if retries is not None else _env_int("SOCKET_RETRIES", 1))

        self._sock: Optional[socket.socket] = None
        self._lock = threading.RLock()

        if autoconnect:
            self._ensure_connected(silent=True)

    # ------------- low level -------------------------------------------------

    def _close_nosafe(self) -> None:
        try:
            if self._sock:
                self._sock.close()
        except OSError:
            pass
        finally:
            self._sock = None

    def close(self) -> None:
        """Socket sauber schließen (optional)."""
        with self._lock:
            self._close_nosafe()

    def _ensure_connected(self, silent: bool = False) -> None:
        """
        Versucht einen Socket aufzubauen. Mehrere Versuche mit kleinem Backoff.
        """
        with self._lock:
            if self._sock is not None:
                return

            last_err: Optional[Exception] = None
            backoff = 0.2
            attempts = max(1, self.retries + 1)
            for _ in range(attempts):
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    s.settimeout(self.connect_timeout)
                    s.connect((self.host, self.port))
                    s.settimeout(self.io_timeout)
                    self._sock = s
                    return
                except Exception as e:
                    last_err = e
                    time.sleep(backoff)
                    backoff = min(backoff * 1.5, 1.5)
            if not silent:
                raise WorkerUnavailable(f"Worker nicht erreichbar auf {self.host}:{self.port} - {last_err}")

    def _request_locked(self, payload: Dict[str, Any], per_call_timeout: Optional[float] = None) -> Dict[str, Any]:
        """
        Führt eine Anfrage auf bereits gesichertem Socket aus.
        Wird unter _lock aufgerufen.
        """
        assert self._sock is not None

        # optional per-call IO-Timeout setzen
        if per_call_timeout is not None:
            try:
                self._sock.settimeout(per_call_timeout)
            except Exception:
                pass  # im Zweifel Standard-Timeout nutzen

        # senden/empfangen
        send_json(self._sock, payload)
        resp = recv_json(self._sock)
        if resp is None:
            # Verbindung weg – hart resetten
            self._close_nosafe()
            return {"ok": False, "error": "Worker-Verbindung abgebrochen"}
        return resp

    # ------------- public API ------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._sock is not None

    def request(self, payload: Dict[str, Any], *, timeout: Optional[float] = None) -> Dict[str, Any]:
        """
        Schickt ein JSON-Payload an den Worker. Liefert {"ok": bool, ...}.
        Bei I/O-Fehlern wird 1x auto-reconnected und erneut versucht.
        """
        # 1) sicherstellen, dass Verbindung besteht
        try:
            self._ensure_connected(silent=False)
        except WorkerUnavailable as e:
            return {"ok": False, "error": str(e)}

        # 2) eigentliche Anfrage (mit Lock)
        with self._lock:
            try:
                return self._request_locked(payload, per_call_timeout=timeout)
            except Exception as e:
                # einmaliger Auto-Retry nach Reset
                self._close_nosafe()
                try:
                    self._ensure_connected(silent=False)
                except WorkerUnavailable as e2:
                    return {"ok": False, "error": str(e2)}
                try:
                    return self._request_locked(payload, per_call_timeout=timeout)
                except Exception as e2:
                    self._close_nosafe()
                    return {"ok": False, "error": f"I/O-Fehler: {type(e).__name__}: {e} / retry: {type(e2).__name__}: {e2}"}

    # ------------- Convenience-OPs ------------------------------------------

    def ping(self) -> Dict[str, Any]:
        """Health-Check des Workers."""
        return self.request({"op": "ping", "ts": int(time.time())})

    def edit_channel(
        self,
        channel_id: int,
        *,
        name: Optional[str] = None,
        user_limit: Optional[int] = None,
        bitrate: Optional[int] = None,
        reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"op": "edit_channel", "channel_id": int(channel_id)}
        if name is not None:
            payload["name"] = str(name)
        if user_limit is not None:
            payload["user_limit"] = int(user_limit)
        if bitrate is not None:
            payload["bitrate"] = int(bitrate)
        if reason:
            payload["reason"] = str(reason)
        return self.request(payload)

    def set_permissions(self, channel_id: int, target_id: int, overwrite: Dict[str, Any]) -> Dict[str, Any]:
        payload = {
            "op": "set_permissions",
            "channel_id": int(channel_id),
            "target_id": int(target_id),
            "overwrite": overwrite,
        }
        return self.request(payload)

    # Speziell für Live-Match: Suffix setzen/entfernen (Server muss OP kennen)
    def rename_match_suffix(
        self,
        channel_id: int,
        base_name: str,
        minutes: int,
        *,
        template: str = " • Im Match (Min {minutes})",
        reason: str = "LiveMatch status update",
    ) -> Dict[str, Any]:
        payload = {
            "op": "rename_match_suffix",
            "channel_id": int(channel_id),
            "base_name": str(base_name),
            "minutes": int(minutes),
            "template": str(template),
            "reason": str(reason),
        }
        return self.request(payload)

    def clear_match_suffix(
        self,
        channel_id: int,
        *,
        reason: str = "LiveMatch clear",
    ) -> Dict[str, Any]:
        payload = {
            "op": "clear_match_suffix",
            "channel_id": int(channel_id),
            "reason": str(reason),
        }
        return self.request(payload)

    def bulk(self, ops: Sequence[Dict[str, Any]], *, atomic: bool = False) -> Dict[str, Any]:
        """
        Mehrere Operationen in einem Rutsch. Der Server kann 'atomic' ignorieren
        oder selber transaktional handeln.
        """
        return self.request({"op": "bulk", "ops": list(ops), "atomic": bool(atomic)})
