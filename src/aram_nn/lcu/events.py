"""Small LCU WebSocket event listener.

The League client exposes a local WebSocket on the same port/auth as the REST
LCU API.  We use it only as a wake-up signal for the collector: events are
queued, and the polling loop decides which REST endpoint to query next.
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import socket
import ssl
import struct
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from .process import LCUCredentials


@dataclass(frozen=True)
class LCUApiEvent:
    uri: str
    event_type: str
    data: Any
    raw: dict[str, Any]


class LCUEventListener:
    """Background listener for LCU OnJsonApiEvent messages."""

    def __init__(
        self,
        creds: LCUCredentials,
        on_event: Callable[[LCUApiEvent], None],
        on_status: Callable[[str], None] | None = None,
    ):
        self.creds = creds
        self._on_event = on_event
        self._on_status = on_status
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="lcu-event-listener", daemon=True)
        self._last_status: str | None = None

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def _status(self, status: str) -> None:
        if status == self._last_status:
            return
        self._last_status = status
        if self._on_status is not None:
            self._on_status(status)

    def _run(self) -> None:
        backoff_sec = 1.0
        while not self._stop.is_set():
            sock: ssl.SSLSocket | None = None
            try:
                sock = _connect_lcu_websocket(self.creds)
                _send_ws_text(sock, json.dumps([5, "OnJsonApiEvent"], separators=(",", ":")))
                self._status("connected")
                backoff_sec = 1.0

                while not self._stop.is_set():
                    try:
                        message = _read_ws_text(sock)
                    except TimeoutError:
                        continue
                    if message is None:
                        break
                    event = _parse_lcu_event(message)
                    if event is not None:
                        self._on_event(event)
            except Exception as exc:
                if _debug_enabled():
                    self._status(f"disconnected: {exc!r}")
                else:
                    self._status("disconnected")
            finally:
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass

            if not self._stop.wait(backoff_sec):
                backoff_sec = min(backoff_sec * 2.0, 10.0)


def _debug_enabled() -> bool:
    value = os.environ.get("ARAM_LCU_DEBUG", "")
    return value.lower() in {"1", "true", "yes", "on"}


def _connect_lcu_websocket(creds: LCUCredentials) -> ssl.SSLSocket:
    raw = socket.create_connection(("127.0.0.1", creds.port), timeout=5.0)
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    sock = context.wrap_socket(raw, server_hostname="127.0.0.1")
    sock.settimeout(1.0)

    key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
    auth = base64.b64encode(f"riot:{creds.token}".encode("utf-8")).decode("ascii")
    request = (
        "GET / HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{creds.port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        f"Authorization: Basic {auth}\r\n"
        "\r\n"
    )
    sock.sendall(request.encode("ascii"))
    response = _read_http_response(sock)
    if b" 101 " not in response.split(b"\r\n", 1)[0]:
        raise ConnectionError(response.split(b"\r\n", 1)[0].decode("latin1", errors="replace"))
    return sock


def _read_http_response(sock: ssl.SSLSocket) -> bytes:
    chunks: list[bytes] = []
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        chunks.append(chunk)
        data = b"".join(chunks)
        if len(data) > 65536:
            raise ConnectionError("LCU WebSocket handshake response too large")
    return data


def _read_exact(sock: ssl.SSLSocket, n: int) -> bytes:
    parts: list[bytes] = []
    remaining = n
    while remaining > 0:
        try:
            chunk = sock.recv(remaining)
        except socket.timeout as exc:
            raise TimeoutError() from exc
        if not chunk:
            raise ConnectionError("socket closed")
        parts.append(chunk)
        remaining -= len(chunk)
    return b"".join(parts)


def _send_ws_text(sock: ssl.SSLSocket, text: str) -> None:
    payload = text.encode("utf-8")
    header = bytearray([0x81])
    length = len(payload)
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack("!Q", length))

    mask = secrets.token_bytes(4)
    masked = bytes(byte ^ mask[idx % 4] for idx, byte in enumerate(payload))
    sock.sendall(bytes(header) + mask + masked)


def _read_ws_text(sock: ssl.SSLSocket) -> str | None:
    while True:
        first, second = _read_exact(sock, 2)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", _read_exact(sock, 2))[0]
        elif length == 127:
            length = struct.unpack("!Q", _read_exact(sock, 8))[0]

        mask = _read_exact(sock, 4) if masked else b""
        payload = _read_exact(sock, length) if length else b""
        if masked:
            payload = bytes(byte ^ mask[idx % 4] for idx, byte in enumerate(payload))

        if opcode == 0x1:
            return payload.decode("utf-8", errors="replace")
        if opcode == 0x8:
            return None
        if opcode == 0x9:
            _send_ws_pong(sock, payload)
            continue
        if opcode == 0xA:
            continue
        return None


def _send_ws_pong(sock: ssl.SSLSocket, payload: bytes) -> None:
    header = bytearray([0x8A])
    length = len(payload)
    if length >= 126:
        return
    header.append(0x80 | length)
    mask = secrets.token_bytes(4)
    masked = bytes(byte ^ mask[idx % 4] for idx, byte in enumerate(payload))
    sock.sendall(bytes(header) + mask + masked)


def _parse_lcu_event(message: str) -> LCUApiEvent | None:
    try:
        data = json.loads(message)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list) or len(data) < 3 or not isinstance(data[2], dict):
        return None
    payload = data[2]
    uri = payload.get("uri")
    if not isinstance(uri, str):
        return None
    event_type = payload.get("eventType")
    return LCUApiEvent(
        uri=uri,
        event_type=event_type if isinstance(event_type, str) else "",
        data=payload.get("data"),
        raw=payload,
    )
