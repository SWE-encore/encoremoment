"""WebSocket front end of the session server.

Serves ``/v1/sessions/<session_id>/stream`` (contracts/README.md): text
frames are PC messages, binary frames are audio. On connect the PC first gets
every committed result after ``?after=<cursor>`` (default 0), so a PC that
reconnects catches up (R11). A normal close (code 1000, the PC's End) ends
the session; a dropped connection leaves it open for a reconnect.

Sessions live in memory for the life of the process. Persistence (Redis,
PostgreSQL) and HTTP session management are later work.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
from contextlib import aclosing
from http import HTTPStatus
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from websockets.asyncio.server import serve, unix_serve
from websockets.exceptions import ConnectionClosed

from .detector import DetectorConfig, Frame, TabRules
from .session import IDLE_S, ServerSession
from .stream import messages_with_idle

PATH = re.compile(r"^/v1/sessions/([A-Za-z0-9_-]{4,64})/stream$")
POLL_S = 0.5
MAX_MESSAGE = 1 << 20


class EncoreServer:
    def __init__(
        self,
        model: Callable[[bytes], dict[str, float]],
        config: DetectorConfig | None = None,
        *,
        face_model=None,
        smile_threshold: float = TabRules.smile,
        quiet_ms: int = TabRules.quiet_ms,
        say: Callable[[str], None] = lambda line: None,
        idle_s: float = IDLE_S,
        on_frame: Callable[[str, Frame], None] | None = None,
    ) -> None:
        """``on_frame(session_id, frame)``: every analysed frame, for debugging tools.
        It is called on a worker thread."""
        self.face_model = face_model
        self.smile_threshold = smile_threshold
        self.quiet_ms = quiet_ms
        self.model = model
        self.config = config or DetectorConfig()
        self.say = say
        self.idle_s = idle_s
        self.on_frame = on_frame
        self.sessions: dict[str, ServerSession] = {}
        self._active: dict[str, Any] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def run(self, host: str, port: int, *, unix_socket: str | None = None) -> None:
        options = dict(process_request=self.check_path, max_size=MAX_MESSAGE)
        if unix_socket:
            listener = unix_serve(self.handler, unix_socket, **options)
            address = unix_socket
        else:
            listener = serve(self.handler, host, port, **options)
            address = f"ws://{host}:{port}/v1/sessions/<session_id>/stream"
        async with listener:
            self.say(f"listening on {address}")
            await asyncio.get_running_loop().create_future()  # until cancelled

    def check_path(self, connection, request):
        if PATH.match(urlsplit(request.path).path) is None:
            return connection.respond(HTTPStatus.NOT_FOUND, "Not found: use /v1/sessions/<session_id>/stream\n")
        return None

    async def handler(self, ws) -> None:
        url = urlsplit(ws.request.path)
        sid = PATH.match(url.path).group(1)
        try:
            after = int(parse_qs(url.query).get("after", ["0"])[0])
        except ValueError:
            after = 0
        session = self.sessions.get(sid)
        if session is None:
            watch = (lambda frame, sid=sid: self.on_frame(sid, frame)) if self.on_frame else None
            session = self.sessions[sid] = ServerSession(sid, self.model, self.config, idle_s=self.idle_s,
                                                         on_frame=watch, face_model=self.face_model,
                                                         smile_threshold=self.smile_threshold, quiet_ms=self.quiet_ms)
            self._locks[sid] = asyncio.Lock()
        windows = 0
        lock = self._locks[sid]
        old = self._active.get(sid)
        self._active[sid] = ws
        if old is not None:
            await old.close(1000, "replaced by a new connection")
        self.say(f"{sid}: connected, {len(session.results_after(after))} result(s) to replay")
        try:
            for msg in session.results_after(after):
                await ws.send(json.dumps(msg, ensure_ascii=False))
            async with aclosing(messages_with_idle(ws.recv, POLL_S)) as messages:
                async for data in messages:
                    async with lock:
                        if data is None:
                            out = session.on_idle()
                        elif isinstance(data, bytes):
                            out = await asyncio.to_thread(session.on_audio, data)
                        else:
                            out = session.on_text(data)
                    for msg in out:
                        if msg["kind"] == "demo_scores":
                            windows += 1
                            if windows == 1 or windows % 10 == 0:
                                self.say(f"{sid}: window {windows} laughter={msg['laughter']:.3f} "
                                         f"smile={msg['smile']} faces={msg['faces_seen']}/{msg['face_frames']}")
                        self._report(sid, msg)
                        await ws.send(json.dumps(msg, ensure_ascii=False))
        except ConnectionClosed:
            pass
        finally:
            if self._active.get(sid) is ws:
                del self._active[sid]
                if ws.close_code == 1000:
                    async with lock:
                        for msg in session.end():
                            self._report(sid, msg)
                    self.say(f"{sid}: session ended")
                else:
                    self.say(f"{sid}: connection dropped ({ws.close_code}); kept for a reconnect")

    def _report(self, sid: str, msg: dict[str, Any]) -> None:
        if msg["kind"] == "control_ack":
            self.say(f"{sid}: {msg['sensor']} generation {msg['generation']} {msg['status']}"
                     + (f" ({msg['reason']})" if "reason" in msg else ""))
        elif msg["kind"] == "event_update":
            ev = msg["event"]
            pb = ev["playback"]
            where = f" at {pb['timecode_start_ms'] / 1000:.1f} s of {pb['title']!r}" if pb["status"] == "mapped" else ""
            self.say(f"{sid}: #{msg['cursor']} {ev['event_id']} {ev['reaction']['type']} {ev['phase']} "
                     f"confidence {ev['reaction']['confidence']:.2f} intensity {ev['intensity']['combined']:.2f}{where}")


def serve_in_thread(app: EncoreServer, host: str = "127.0.0.1", port: int = 0):
    """Run ``app`` on a background thread (tests, demos). Returns (ws URL, stop function)."""
    loop = asyncio.new_event_loop()
    started = threading.Event()
    box: dict[str, Any] = {}

    async def main() -> None:
        async with serve(app.handler, host, port, process_request=app.check_path,
                         max_size=MAX_MESSAGE) as server:
            box["server"] = server
            box["port"] = server.sockets[0].getsockname()[1]
            started.set()
            await server.wait_closed()

    thread = threading.Thread(target=loop.run_until_complete, args=(main(),), name="encore-server", daemon=True)
    thread.start()
    if not started.wait(5):
        raise RuntimeError("server did not start")

    def stop() -> None:
        loop.call_soon_threadsafe(box["server"].close)
        thread.join(5)

    return f"ws://{host}:{box['port']}", stop
