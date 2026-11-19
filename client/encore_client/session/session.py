"""Session and result delivery for the PC client (EM-23).

A ``Session`` owns one connection to the server's result stream and the PC's
view of that session. It

- sends the PC's own messages (playback anchors, sensor controls),
- applies every server message through the shared contract receiver, so a
  result delivered twice still gives one event (contracts/README.md),
- tracks the connection and reports when it drops (R9, R11), and
- writes every step to a JSON Lines delivery log for reproduction.

It does no Qt work and never blocks: the UI (or the headless runner) calls
``poll()`` on a timer. Reconnection and replay after a drop are R11 work
(EM-20) and are not done here; a dropped session stays ``disconnected``.

Messages the PC makes itself (``set_sensor``, ``send_anchor``) share one
``seq`` counter, as in contracts/samples. Sensor controls made before the
connection opens are sent first when it opens, then the newest anchor, so
the server learns sensor state before anything else (R9, R11).
"""

from __future__ import annotations

import json
import queue
import re
import secrets
import socket
import threading
import time
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Protocol

from ..clock import monotonic_ms
from . import ws
from .contract import PC_TO_SERVER, SessionView

STREAM_PATH = "/v1/sessions/{session_id}/stream"
SENSORS = ("camera", "microphone", "aec")
_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{4,64}$")


def new_session_id() -> str:
    return f"ses-{secrets.token_hex(6)}"


def stream_url(server: str, session_id: str) -> str:
    return server.rstrip("/") + STREAM_PATH.format(session_id=session_id)


class Conn(str, Enum):
    IDLE = "idle"  # created, Start not pressed
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"  # dropped or never reached; results may be stale
    ENDED = "ended"  # user pressed End


# ---- transport ---------------------------------------------------------------


class Transport(Protocol):
    def start(self) -> None: ...
    def send(self, text: str) -> bool: ...
    def poll(self) -> list[tuple[str, Any]]: ...
    def close(self) -> None: ...


class WsTransport:
    """One WebSocket connection on a background thread.

    ``poll()`` returns what happened since the last call:
    ``("open", None)``, ``("message", text)`` or ``("closed", reason)``.
    A peer that sends nothing, not even a pong, for ``silence_s`` counts as
    dropped, so a pulled cable is noticed and not only a closed socket.
    """

    def __init__(
        self,
        url: str,
        *,
        connect: Callable[[str], ws.WebSocket] = ws.connect,
        ping_s: float = 5.0,
        silence_s: float = 15.0,
    ) -> None:
        self.url = url
        self._connect = connect
        self._ping_s = ping_s
        self._silence_s = silence_s
        self._q: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._ws: ws.WebSocket | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="encore-ws", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def send(self, text: str) -> bool:
        conn = self._ws
        if conn is None or conn.closed:
            return False
        try:
            conn.send(text)
            return True
        except ws.WsClosed:
            return False

    def poll(self) -> list[tuple[str, Any]]:
        out = []
        while True:
            try:
                out.append(self._q.get_nowait())
            except queue.Empty:
                return out

    def close(self) -> None:
        self._stop.set()
        if self._ws is not None:
            self._ws.close(1000, "session ended")

    def _run(self) -> None:
        try:
            conn = self._connect(self.url)
        except (OSError, ValueError) as exc:
            self._q.put(("closed", f"could not connect: {exc}"))
            return
        self._ws = conn
        if self._stop.is_set():
            conn.close(1000, "session ended")
            self._q.put(("closed", "session ended"))
            return
        self._q.put(("open", None))
        conn.sock.settimeout(min(1.0, self._ping_s))
        last_ping = time.monotonic()
        while True:
            try:
                self._q.put(("message", conn.recv()))
            except socket.timeout:
                pass
            except ws.WsClosed as exc:
                reason = "session ended" if self._stop.is_set() else _close_reason(exc)
                self._q.put(("closed", reason))
                return
            now = time.monotonic()
            if now - conn.last_rx > self._silence_s:
                conn.abort()
                self._q.put(("closed", f"no response from server for {self._silence_s:g} s"))
                return
            if now - last_ping >= self._ping_s:
                last_ping = now
                try:
                    conn.ping()
                except ws.WsClosed:
                    pass  # reported by the next recv()


def _close_reason(exc: ws.WsClosed) -> str:
    if exc.code == 1006:
        return exc.reason or "connection lost"
    return f"server closed the connection ({exc.code}{' ' + exc.reason if exc.reason else ''})"


# ---- delivery log --------------------------------------------------------------


class DeliveryLog:
    """Appends one JSON line per delivery step: the reproduction record."""

    def __init__(self, path: str | Path | None, clock: Callable[[], int] = monotonic_ms) -> None:
        self.clock = clock
        self.entries: list[dict[str, Any]] = []
        self._fh = open(path, "a", encoding="utf-8") if path else None  # noqa: SIM115

    def write(self, what: str, **data: Any) -> None:
        entry = {"t_ms": self.clock(), "what": what, **data}
        self.entries.append(entry)
        if self._fh is not None:
            self._fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self._fh.flush()

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


# ---- session -------------------------------------------------------------------


class Session:
    """One viewing session's connection and received results."""

    def __init__(
        self,
        server: str,
        session_id: str | None = None,
        *,
        transport_factory: Callable[[str], Transport] = WsTransport,
        log: DeliveryLog | None = None,
        clock: Callable[[], int] = monotonic_ms,
        microphone_on_start: bool = False,
    ) -> None:
        """``microphone_on_start``: Start turns the microphone on (R11
        ``enable_requested_audio``). Leave it off when a ScenarioDriver sends
        the sample's own controls, or the two would collide.
        """
        self.session_id = session_id or new_session_id()
        if not _SESSION_ID.match(self.session_id):
            raise ValueError(f"session_id must match {_SESSION_ID.pattern}: {self.session_id!r}")
        self.url = stream_url(server, self.session_id)
        self.view = SessionView(self.session_id)
        self.state = Conn.IDLE
        self.state_since_ms: int | None = None
        self.reason: str | None = None
        self.received = 0
        self.received_at_open = 0
        self.sent = 0
        self.unsent: list[dict[str, Any]] = []
        self.bad_messages = 0
        self.last_anchor: dict[str, Any] | None = None
        self.microphone_on_start = microphone_on_start
        self.log = log or DeliveryLog(None, clock)
        self._seq = 0
        self._clock = clock
        self._factory = transport_factory
        self._transport: Transport | None = None

    # ---- user actions ----------------------------------------------------------

    def start(self) -> None:
        if self.state is not Conn.IDLE:
            raise RuntimeError(f"start() in state {self.state.value}; create a new Session instead")
        self.log.write("start", session_id=self.session_id, url=self.url)
        self._set_state(Conn.CONNECTING)
        self._transport = self._factory(self.url)
        self._transport.start()
        if self.microphone_on_start:
            self.set_sensor("microphone", True)

    def end(self) -> None:
        if self.state in (Conn.IDLE, Conn.ENDED):
            return
        self._set_state(Conn.ENDED)
        self.log.write("end", unsent=len(self.unsent))
        if self._transport is not None:
            self._transport.close()

    # ---- outgoing --------------------------------------------------------------

    def send(self, msg: dict[str, Any]) -> bool:
        """Send one PC message. Returns False if it could not go out now.

        The message is applied to the local view either way: a camera turned
        off is off whether or not the server heard about it. Messages made
        before the connection opens are sent when it opens; after a drop they
        stay in ``unsent`` until reconnection exists (R11, EM-20).
        """
        if msg.get("kind") not in PC_TO_SERVER:
            raise ValueError(f"not a PC-to-server message: {msg.get('kind')!r}")
        if msg.get("session_id") != self.session_id:
            raise ValueError("message belongs to another session")
        self.view.receive("pc->server", msg)
        return self._transmit(msg)

    def set_sensor(
        self,
        sensor: str,
        enabled: bool,
        *,
        generation: int | None = None,
        capture_ms: int | None = None,
    ) -> dict[str, Any] | None:
        """Turn a sensor on or off for this session (R5, R8, R12).

        Sends a ``control_update`` with the sensor's next generation, or with
        ``generation`` when the device code keeps its own counter (for example
        ``CameraController.generation``); it must be newer than the last one
        sent. Returns the message, or None if the sensor is already in that
        state, since the contract rejects a control that changes nothing.

        Call it when the device state really changes: microphone on when audio
        capture starts, off when it stops (R8); camera on and off from the
        camera controller (R5). New events are only accepted while the
        microphone is on (contracts/README.md, receiver rule 6).
        """
        if sensor not in SENSORS:
            raise ValueError(f"unknown sensor {sensor!r}; expected one of {SENSORS}")
        current = self.view.sensors[sensor]
        if enabled == current.enabled:
            return None
        if generation is None:
            generation = current.generation + 1
        if generation <= current.generation:
            raise ValueError(
                f"{sensor} generation {generation} is not newer than {current.generation}"
            )
        msg = {
            "schema": "encore.v1",
            "kind": "control_update",
            "session_id": self.session_id,
            "seq": self._next_seq(),
            "capture_ms": self._clock() if capture_ms is None else capture_ms,
            "idempotency_key": f"ctl-{sensor}-{generation}",
            "sensor": sensor,
            "enabled": enabled,
            "generation": generation,
        }
        self.send(msg)
        return msg

    def _next_seq(self) -> int:
        seq = self._seq
        self._seq += 1
        return seq

    def _transmit(self, msg: dict[str, Any]) -> bool:
        ok = self.state is Conn.CONNECTED and self._transport is not None
        ok = ok and self._transport.send(json.dumps(msg, ensure_ascii=False))
        if ok:
            self.sent += 1
            self.log.write("out", msg=msg)
        else:
            self.unsent.append(msg)
            self.log.write("unsent", kind=msg["kind"], state=self.state.value)
        return ok

    def send_anchor(self, msg: dict[str, Any]) -> None:
        """Player callback. Keeps the newest anchor; sends it if connected.

        Anchors made before Start are not queued. The newest one is sent as
        soon as the connection opens, which gives the server the title and
        time basis for the session (R3, R11).
        """
        self.last_anchor = msg | {"session_id": self.session_id}
        if self.state is Conn.CONNECTED:
            self._send_last_anchor()

    def _send_last_anchor(self) -> None:
        # The player numbers its own anchors; on the wire the session's counter is used.
        self.last_anchor = self.last_anchor | {"seq": self._next_seq()}
        self.send(self.last_anchor)

    # ---- incoming --------------------------------------------------------------

    def poll(self) -> bool:
        """Handle what the transport delivered. Returns True if anything changed."""
        if self._transport is None:
            return False
        changed = False
        for what, data in self._transport.poll():
            changed = True
            if what == "open":
                if self.state is Conn.ENDED:
                    continue
                self._set_state(Conn.CONNECTED)
                self.received_at_open = self.received
                self.log.write("open")
                queued, self.unsent = self.unsent, []
                for msg in queued:  # sensor controls made before the connection opened
                    self._transmit(msg)
                if self.last_anchor is not None:
                    self._send_last_anchor()
            elif what == "message":
                self._on_message(data)
            elif what == "closed":
                self.log.write("closed", reason=data)
                if self.state is not Conn.ENDED:
                    self.reason = data
                    self._set_state(Conn.DISCONNECTED)
        return changed

    def _on_message(self, text: str) -> None:
        self.received += 1
        try:
            msg = json.loads(text)
        except ValueError:
            msg = None
        if not isinstance(msg, dict):
            self.bad_messages += 1
            self.log.write("in_bad", text=text[:200])
            return
        self.log.write("in", msg=msg)
        self.view.receive("server->pc", msg)

    # ---- state -----------------------------------------------------------------

    @property
    def stale(self) -> bool:
        """Live results may be out of date: the stream dropped (R9)."""
        return self.state is Conn.DISCONNECTED

    def _set_state(self, state: Conn) -> None:
        self.state = state
        self.state_since_ms = self._clock()
        if state is not Conn.DISCONNECTED:
            self.reason = None

    def summary(self) -> dict[str, Any]:
        return self.view.summary() | {
            "connection": self.state.value,
            "received": self.received,
            "sent": self.sent,
            "unsent": len(self.unsent),
            "bad_messages": self.bad_messages,
        }


# ---- view model ----------------------------------------------------------------


def event_rows(view: SessionView) -> list[dict[str, Any]]:
    """One row per event for the UI, ordered by trigger time.

    The view already holds one entry per event_id, so a result delivered twice
    is one row here.
    """
    rows = []
    for ev in view.events.values():
        playback = ev.get("playback") or {}
        mapped = playback.get("status") == "mapped"
        intensity = ev.get("intensity") or {}
        rows.append(
            {
                "event_id": ev["event_id"],
                "type": ev["reaction"]["type"],
                "phase": ev["phase"],
                "revision": ev["revision"],
                "confidence": ev["reaction"].get("confidence"),
                "intensity": intensity.get("combined"),
                "trigger_ms": ev["capture"]["triggers_ms"][0],
                "title": playback.get("title") if mapped else None,
                "timecode_ms": playback.get("timecode_start_ms") if mapped else None,
            }
        )
    rows.sort(key=lambda r: (r["trigger_ms"], r["event_id"]))
    return rows


# ---- sample scenarios ------------------------------------------------------------


class ScenarioDriver:
    """Plays the PC side of a contracts/samples scenario.

    Used with ``fake_server``, which plays the server side of the same file.
    Both walk the message list in lock step: the PC sends its messages when
    their turn comes, and waits until the server's messages before them have
    arrived. The order the PC sees is then exactly the sample's order, so the
    sample's ``expected`` block is the result to check.
    """

    def __init__(self, session: Session, scenario: dict[str, Any]) -> None:
        if scenario["session_id"] != session.session_id:
            raise ValueError(
                f"scenario is for {scenario['session_id']}, session is {session.session_id}"
            )
        self.session = session
        self.steps = scenario["messages"]
        self.i = 0
        self._server_before: list[int] = []
        n = 0
        for step in self.steps:
            if step["dir"] == "server->pc":
                n += 1
            self._server_before.append(n)

    @property
    def done(self) -> bool:
        return self.i >= len(self.steps)

    def pump(self) -> None:
        if self.session.state is not Conn.CONNECTED:
            return
        arrived = self.session.received - self.session.received_at_open
        while not self.done:
            step = self.steps[self.i]
            if step["dir"] == "pc->server":
                self.session.send(step["msg"])
            elif arrived < self._server_before[self.i]:
                return
            self.i += 1


def load_scenario(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))
