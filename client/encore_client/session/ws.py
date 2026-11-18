"""A small RFC 6455 WebSocket, standard library only.

The contract (contracts/README.md) sends one JSON object per WebSocket text
frame. This module covers exactly that: text messages, ping/pong and close,
for a client (masked frames) and for the development fake server. It keeps
the client free of extra dependencies next to GStreamer and PySide6.

Not covered: binary messages, extensions (permessage-deflate), subprotocols.
``wss://`` works through the ``ssl`` module.
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import ssl
import struct
import threading
import time
from typing import Callable
from urllib.parse import urlsplit

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA
MAX_MESSAGE = 1 << 20  # 1 MiB: far above any contract message
MAX_HEAD = 16 * 1024


class WsClosed(Exception):
    """The connection is closed. ``code`` follows RFC 6455 (1006 = dropped)."""

    def __init__(self, code: int = 1006, reason: str = "") -> None:
        super().__init__(f"{code} {reason}".strip())
        self.code = code
        self.reason = reason


class HandshakeError(OSError):
    """The HTTP upgrade failed (wrong path, not a WebSocket server, …)."""


def accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()


def _xor(data: bytes, key: bytes) -> bytes:
    if not data:
        return data
    n = len(data)
    k = (key * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(k, "big")).to_bytes(n, "big")


def _read_head(sock: socket.socket) -> tuple[bytes, bytes]:
    """Read an HTTP head up to the blank line. Returns (head, bytes after it)."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise HandshakeError("connection closed during handshake")
        buf += chunk
        if len(buf) > MAX_HEAD:
            raise HandshakeError("handshake header too large")
    head, rest = buf.split(b"\r\n\r\n", 1)
    return head, rest


def _parse_head(head: bytes) -> tuple[str, dict[str, str]]:
    lines = head.decode("latin-1").split("\r\n")
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    return lines[0], headers


class WebSocket:
    """One open connection.

    ``recv()`` is meant for a single reader thread; ``send()`` may be called
    from any thread. ``last_rx`` is the monotonic time of the last frame of any
    kind, so a reader can notice a silent peer.
    """

    def __init__(self, sock: socket.socket, *, mask: bool, leftover: bytes = b"") -> None:
        self.sock = sock
        self.mask = mask
        self.closed = False
        self.close_code: int | None = None
        self.last_rx = time.monotonic()
        self._buf = bytearray(leftover)
        self._frag: list[bytes] | None = None
        self._send_lock = threading.Lock()

    # ---- receiving ---------------------------------------------------------

    def recv(self) -> str:
        """Return the next text message.

        Raises ``socket.timeout`` if the socket timeout passes with no complete
        message (nothing is lost; call again), and ``WsClosed`` once closed.
        """
        if self.closed:
            raise WsClosed(self.close_code or 1006, "closed")
        while True:
            frame = self._parse_frame()
            if frame is None:
                try:
                    chunk = self.sock.recv(65536)
                except socket.timeout:
                    raise
                except OSError as exc:
                    self._mark_closed(1006)
                    raise WsClosed(1006, f"connection lost ({exc})") from None
                if not chunk:
                    self._mark_closed(1006)
                    raise WsClosed(1006, "connection lost")
                self._buf += chunk
                continue
            self.last_rx = time.monotonic()
            fin, op, payload = frame
            if op == OP_PING:
                self._send_frame(OP_PONG, payload)
            elif op == OP_PONG:
                pass
            elif op == OP_CLOSE:
                code = struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else 1005
                reason = payload[2:].decode("utf-8", "replace")
                try:
                    self._send_frame(OP_CLOSE, payload[:2])
                except WsClosed:
                    pass
                self._mark_closed(code)
                raise WsClosed(code, reason)
            elif op == OP_TEXT:
                if fin:
                    return payload.decode("utf-8")
                self._frag = [payload]
            elif op == OP_CONT and self._frag is not None:
                self._frag.append(payload)
                if sum(map(len, self._frag)) > MAX_MESSAGE:
                    self.abort()
                    raise WsClosed(1009, "message too big")
                if fin:
                    data, self._frag = b"".join(self._frag), None
                    return data.decode("utf-8")
            else:
                # Binary or a stray continuation: not part of the contract.
                self.close(1003, "text messages only")
                raise WsClosed(1003, "text messages only")

    def _parse_frame(self) -> tuple[bool, int, bytes] | None:
        """Take one complete frame off the buffer, or None if it is incomplete."""
        b = self._buf
        if len(b) < 2:
            return None
        fin, op = bool(b[0] & 0x80), b[0] & 0x0F
        masked, n, i = bool(b[1] & 0x80), b[1] & 0x7F, 2
        if n == 126:
            if len(b) < 4:
                return None
            n, i = struct.unpack_from("!H", b, 2)[0], 4
        elif n == 127:
            if len(b) < 10:
                return None
            n, i = struct.unpack_from("!Q", b, 2)[0], 10
        if n > MAX_MESSAGE:
            self.abort()
            raise WsClosed(1009, "message too big")
        key = b""
        if masked:
            if len(b) < i + 4:
                return None
            key, i = bytes(b[i : i + 4]), i + 4
        if len(b) < i + n:
            return None
        payload = bytes(b[i : i + n])
        del b[: i + n]
        return fin, op, _xor(payload, key) if masked else payload

    # ---- sending -----------------------------------------------------------

    def send(self, text: str) -> None:
        self._send_frame(OP_TEXT, text.encode("utf-8"))

    def ping(self) -> None:
        self._send_frame(OP_PING, b"")

    def _send_frame(self, op: int, payload: bytes) -> None:
        n = len(payload)
        head = bytearray([0x80 | op])
        mask_bit = 0x80 if self.mask else 0
        if n < 126:
            head.append(mask_bit | n)
        elif n < 1 << 16:
            head.append(mask_bit | 126)
            head += struct.pack("!H", n)
        else:
            head.append(mask_bit | 127)
            head += struct.pack("!Q", n)
        if self.mask:
            key = os.urandom(4)
            head += key
            payload = _xor(payload, key)
        with self._send_lock:
            if self.closed:
                raise WsClosed(self.close_code or 1006, "closed")
            try:
                self.sock.sendall(bytes(head) + payload)
            except OSError as exc:
                self._mark_closed(1006)
                raise WsClosed(1006, f"connection lost ({exc})") from None

    # ---- closing -----------------------------------------------------------

    def close(self, code: int = 1000, reason: str = "") -> None:
        """Send a close frame (best effort) and close the socket."""
        if self.closed:
            return
        try:
            self._send_frame(OP_CLOSE, struct.pack("!H", code) + reason.encode()[:120])
        except WsClosed:
            pass
        self._mark_closed(code)

    def abort(self) -> None:
        """Drop the connection without a close frame, like a crash or cable pull."""
        self._mark_closed(1006)

    def _mark_closed(self, code: int) -> None:
        if self.closed:
            return
        self.closed, self.close_code = True, code
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


def connect(url: str, timeout: float = 5.0, ssl_context: ssl.SSLContext | None = None) -> WebSocket:
    """Open a client connection to ``ws://`` or ``wss://`` ``url``."""
    u = urlsplit(url)
    if u.scheme not in ("ws", "wss") or not u.hostname:
        raise ValueError(f"not a ws:// or wss:// URL: {url!r}")
    secure = u.scheme == "wss"
    port = u.port or (443 if secure else 80)
    path = (u.path or "/") + (f"?{u.query}" if u.query else "")
    sock = socket.create_connection((u.hostname, port), timeout=timeout)
    try:
        if secure:
            ctx = ssl_context or ssl.create_default_context()
            sock = ctx.wrap_socket(sock, server_hostname=u.hostname)
        key = base64.b64encode(os.urandom(16)).decode()
        host = u.hostname if u.port is None else f"{u.hostname}:{port}"
        sock.sendall(
            (
                f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        head, rest = _read_head(sock)
        status, headers = _parse_head(head)
        if status.split(" ")[1:2] != ["101"]:
            raise HandshakeError(f"server answered {status!r}")
        if headers.get("sec-websocket-accept") != accept_key(key):
            raise HandshakeError("bad Sec-WebSocket-Accept")
    except BaseException:
        sock.close()
        raise
    return WebSocket(sock, mask=True, leftover=rest)


def accept(
    sock: socket.socket, allow_path: Callable[[str], bool] | None = None
) -> tuple[WebSocket | None, str]:
    """Server side: read the upgrade request from an accepted socket.

    Returns (WebSocket, path) on success. A bad request is answered with 400,
    a path that ``allow_path`` refuses with 404; both return (None, path).
    """
    head, rest = _read_head(sock)
    request, headers = _parse_head(head)
    parts = request.split(" ")
    path = parts[1] if len(parts) >= 2 else ""
    key = headers.get("sec-websocket-key")
    if parts[:1] != ["GET"] or headers.get("upgrade", "").lower() != "websocket" or not key:
        reject(sock, "400 Bad Request")
        return None, path
    if allow_path is not None and not allow_path(path):
        reject(sock, "404 Not Found")
        return None, path
    sock.sendall(
        (
            "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept_key(key)}\r\n\r\n"
        ).encode()
    )
    return WebSocket(sock, mask=False, leftover=rest), path


def reject(sock: socket.socket, status: str = "404 Not Found") -> None:
    try:
        sock.sendall(f"HTTP/1.1 {status}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())
    finally:
        sock.close()
