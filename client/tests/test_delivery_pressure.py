"""R9: UI work stays bounded, revoked media is dropped, and missing cursors trigger replay."""
import json
import queue
import socket
import threading
import time

from encore_client.session import ws
from encore_client.session.contract import SAMPLES_DIR
from encore_client.session.session import Conn, Session, WsTransport, load_scenario


def until(predicate):
    deadline = time.monotonic() + 3
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(.002)


def test_bounded_incoming_messages_and_stop_while_sender_is_blocked():
    # A full-duplex byte pipe exercises the actual WebSocket framing without a port.
    class BytePipe:
        def __init__(self):
            self.incoming = queue.Queue()
            self.timeout = 2

        def settimeout(self, timeout):
            self.timeout = timeout

        def sendall(self, data):
            self.other.incoming.put(data)

        def recv(self, _size):
            try:
                return self.incoming.get(timeout=self.timeout)
            except queue.Empty:
                raise socket.timeout from None

        def shutdown(self, _how):
            self.other.incoming.put(b"")
            self.incoming.put(b"")

        def close(self):
            pass

    local, remote = BytePipe(), BytePipe()
    local.other, remote.other = remote, local
    client = ws.WebSocket(local, mask=True)
    peer = ws.WebSocket(remote, mask=False)
    started = threading.Event()
    release = threading.Event()
    write = client.send

    def delayed_write(packet):
        started.set()
        assert release.wait(3)
        write(packet)

    client.send = delayed_write
    transport = WsTransport("local", connect=lambda _: client)
    transport.start()
    try:
        until(lambda: ("open", None) in transport.poll())
        # Back up real framed WebSocket output while a capture write is in flight.
        assert transport.send(b"active")
        assert started.wait(2)
        for _ in range(4):
            assert transport.send(b"not yet sent")
        assert not transport.send(b"over budget")
        for i in range(145):
            peer.send(str(i))
        until(lambda: transport.backlogged)
        assert len(transport.poll()) == 32
        assert transport.backlogged
        # None of these GUI operations waits on the writer.
        off = json.dumps({"kind": "control_update", "sensor": "microphone", "enabled": False})
        assert transport.send(off)
        assert transport.take_dropped() == 4
        transport.close()
        release.set()
        remote.settimeout(2)
        assert peer.recv() == b"active"
        assert peer.recv() == off
    finally:
        release.set()
        transport.abort()
        peer.abort()


def test_missing_cursor_reconnects_and_superseded_events_are_applied_once():
    sample = load_scenario(SAMPLES_DIR / "01_single_event.json")
    events = [step["msg"] for step in sample["messages"] if step["msg"]["kind"] == "event_update"]
    opened, finished = events[0], events[-1]
    assert finished["cursor"] > opened["cursor"]
    connections = []
    now = [0]

    class Peer:
        def __init__(self, url):
            self.url, self.items = url, [("open", None)]
            self.aborted = False
            connections.append(self)

        def start(self):
            pass

        def poll(self):
            items, self.items = self.items, []
            return items

        def send(self, packet):
            return True

        def abort(self):
            self.aborted = True

        def close(self):
            pass

    s = Session("ws://unused", sample["session_id"], microphone_on_start=True,
                reconnect_delays_ms=(50,), clock=lambda: now[0], transport_factory=Peer)
    s.start()
    s.poll()
    connections[0].items = [("message", json.dumps(finished))]
    s.poll()
    assert s.stale and not s.view.events
    now[0] = 1001
    s.poll()
    assert connections[0].aborted and s.state == Conn.DISCONNECTED
    now[0] += 50
    s.poll()
    assert connections[1].url.endswith("?after=0")
    connections[1].items.extend(("message", json.dumps(m)) for m in [*events, finished])
    s.poll()
    assert not s.stale
    assert len(s.view.events) == 1
    assert s.view.events[opened["event"]["event_id"]]["phase"] == "FINAL"
