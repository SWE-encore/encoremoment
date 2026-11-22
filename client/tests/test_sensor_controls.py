"""Sensor controls follow the encore.v1 contract (contracts/README.md rules 5, 6).

- Camera and microphone generations increment on every enable and disable.
- A live session turns the microphone on at Start, so the server's new events
  are accepted (R8, R11).
- Controls made before the connection opens go out first, and every message
  the PC makes shares one seq counter.

No network, Qt or GStreamer: a fake transport stands in for the WebSocket.
"""

import json
import socket
from functools import partial
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from encore_client.__main__ import main
from encore_client.sensing.state import CameraController, CameraState
from encore_client.session.contract import SAMPLES_DIR
from encore_client.session.session import Conn, Session, load_scenario

SCHEMA = json.loads((SAMPLES_DIR.parent / "schema" / "messages.schema.json").read_text())
VALIDATOR = Draft202012Validator(SCHEMA)
SAMPLE = load_scenario(SAMPLES_DIR / "01_single_event.json")
SID = SAMPLE["session_id"]
EVENT_UPDATES = [s["msg"] for s in SAMPLE["messages"] if s["msg"]["kind"] == "event_update"]


class FakeTransport:
    def __init__(self, url):
        self.url = url
        self.pending = []
        self.sent = []

    def start(self):
        pass

    def send(self, text):
        self.sent.append(json.loads(text))
        return True

    def poll(self):
        out, self.pending = self.pending, []
        return out

    def close(self):
        pass


def make_session(**kw):
    transports = []

    def factory(url):
        transports.append(FakeTransport(url))
        return transports[-1]

    session = Session("ws://test", SID, transport_factory=factory, clock=lambda: 5_000, **kw)
    return session, transports


def open_session(session, transports):
    session.start()
    transports[0].pending.append(("open", None))
    session.poll()
    assert session.state is Conn.CONNECTED
    return transports[0]


def deliver_sample_events(session, transport):
    """The sample's event updates only: its control_ack answers a camera control
    these tests do not send."""
    for msg in EVENT_UPDATES:
        transport.pending.append(("message", json.dumps(msg)))
    session.poll()


def player_anchor(seq=7):
    return {
        "schema": "encore.v1",
        "kind": "playback_anchor",
        "session_id": "ses-player",
        "seq": seq,
        "capture_ms": 4_000,
        "playback": {"epoch": 1, "content_id": "file:test", "title": "Test movie",
                     "position_ms": 0, "rate": 1.0, "state": "paused"},
    }


# ---- camera generation ----------------------------------------------------------


def test_camera_generation_advances_on_enable_and_disable():
    cam = CameraController()
    cam.request_enable(True)
    assert cam.generation == 1
    cam.on_first_frame(cam.generation)
    cam.request_disable()
    assert cam.generation == 2
    cam.on_stopped()
    cam.request_enable(True)
    assert cam.generation == 3


def test_camera_device_loss_counts_as_disable_once():
    cam = CameraController()
    cam.request_enable(True)
    cam.on_first_frame(cam.generation)
    cam.on_device_lost()
    assert (cam.state, cam.generation) == (CameraState.ERROR, 2)

    cam = CameraController()
    cam.request_enable(True)
    cam.request_disable()  # already a disable: losing the device while stopping adds nothing
    cam.on_device_lost()
    assert (cam.state, cam.generation) == (CameraState.ERROR, 2)


def test_late_first_frame_after_disable_is_ignored():
    cam = CameraController()
    cam.request_enable(True)
    started = cam.generation
    cam.request_disable()
    cam.on_first_frame(started)
    assert cam.state == CameraState.STOPPING


def test_camera_controller_generations_are_accepted_by_the_receiver():
    session, transports = make_session()
    transport = open_session(session, transports)
    cam = CameraController()

    cam.request_enable(True)
    session.set_sensor("camera", True, generation=cam.generation, capture_ms=10_000)
    cam.on_first_frame(cam.generation)
    cam.request_disable()
    session.set_sensor("camera", False, generation=cam.generation, capture_ms=20_000)
    cam.on_stopped()
    cam.request_enable(True)
    session.set_sensor("camera", True, generation=cam.generation, capture_ms=30_000)
    cam.on_first_frame(cam.generation)
    cam.on_device_lost()
    session.set_sensor("camera", False, generation=cam.generation, capture_ms=40_000)

    assert session.view.rejected == {}
    camera = session.view.sensors["camera"]
    assert (camera.generation, camera.enabled) == (4, False)
    assert [(c.generation, c.on_ms, c.off_ms) for c in session.view.camera_intervals] == [
        (1, 10_000, 20_000),
        (3, 30_000, 40_000),
    ]
    assert [m["generation"] for m in transport.sent] == [1, 2, 3, 4]
    assert len({m["idempotency_key"] for m in transport.sent}) == 4


# ---- microphone and live events -------------------------------------------------


def test_new_events_are_refused_without_microphone_on():
    session, transports = make_session()
    deliver_sample_events(session, open_session(session, transports))
    assert session.view.events == {}
    assert session.view.rejected == {"stale_microphone_generation": len(EVENT_UPDATES)}


def test_live_session_turns_microphone_on_and_accepts_events():
    session, transports = make_session(microphone_on_start=True)
    transport = open_session(session, transports)
    assert transport.sent[0]["sensor"] == "microphone"
    assert (transport.sent[0]["enabled"], transport.sent[0]["generation"]) == (True, 1)
    deliver_sample_events(session, transport)
    assert session.view.summary()["events"] == SAMPLE["expected"]["events"]
    assert session.view.rejected == {}


def test_live_cli_turns_microphone_on_at_start(tmp_path):
    with socket.socket() as s:  # a port nothing listens on: the session drops at once
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    log = tmp_path / "delivery.jsonl"
    main(["--headless", "--server", f"ws://127.0.0.1:{port}", "--log", str(log), "--timeout", "5"])
    entries = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert {"what": "unsent", "kind": "control_update"}.items() <= next(
        e for e in entries if e["what"] == "unsent").items()


def test_scenario_sessions_send_no_controls_of_their_own():
    session, transports = make_session()
    open_session(session, transports)
    assert transports[0].sent == []


@pytest.mark.parametrize(("end_reply", "expected_exit"), [("ack", 0), ("timeout", 1), ("drop", 1)])
def test_headless_cli_reports_unconfirmed_end(end_reply, expected_exit, monkeypatch, capsys):
    from encore_client.session import __main__ as headless
    from encore_server.models import LoudnessModel
    from encore_server.session import ServerSession

    server = ServerSession(SID, LoudnessModel())
    now = 0.0

    def sleep(seconds):
        nonlocal now
        now += seconds

    class ControlTransport(FakeTransport):
        def start(self):
            self.pending.append(("open", None))

        def send(self, text):
            for ack in server.on_text(text):
                if ack["generation"] == 1 or end_reply == "ack":
                    self.pending.append(("message", json.dumps(ack)))
                elif end_reply == "drop":
                    self.pending.append(("closed", "connection lost"))
            return True

    monkeypatch.setattr(headless, "time", SimpleNamespace(sleep=sleep, monotonic=lambda: now))
    monkeypatch.setattr("encore_client.__main__.Session",
                        partial(Session, transport_factory=ControlTransport, clock=lambda: round(now * 1000)))
    assert main(["--headless", "--session-id", SID, "--timeout", "1"]) == expected_exit
    assert server.view.sensors["microphone"].generation == 2
    assert ("server did not confirm" in capsys.readouterr().err) == (expected_exit == 1)


# ---- ordering and validation ------------------------------------------------------


def test_controls_before_open_go_first_and_seq_is_shared():
    session, transports = make_session(microphone_on_start=True)
    session.send_anchor(player_anchor(seq=7))
    session.start()
    assert [m["kind"] for m in session.unsent] == ["control_update"]
    transport = transports[0]
    transport.pending.append(("open", None))
    session.poll()
    assert session.unsent == []
    session.set_sensor("camera", True)
    session.send_anchor(player_anchor(seq=8))

    sent = transport.sent
    assert [m["kind"] for m in sent] == [
        "control_update", "playback_anchor", "control_update", "playback_anchor"]
    assert [m["seq"] for m in sent] == [0, 1, 2, 3]
    assert all(m["session_id"] == SID for m in sent)
    for m in sent:
        assert [e.message for e in VALIDATOR.iter_errors(m)] == []


def test_set_sensor_refuses_no_change_and_stale_generation():
    session, transports = make_session()
    open_session(session, transports)
    assert session.set_sensor("microphone", False) is None  # already off: nothing to send
    assert transports[0].sent == []
    session.set_sensor("camera", True, generation=2)
    with pytest.raises(ValueError):
        session.set_sensor("camera", False, generation=2)
    with pytest.raises(ValueError):
        session.set_sensor("speaker", True)
    assert session.view.rejected == {}


@pytest.mark.parametrize("fill_gap", [True, False])
def test_end_waits_for_the_missing_cursor_even_after_controls_are_acknowledged(fill_gap):
    session, transports = make_session(microphone_on_start=True)
    transport = open_session(session, transports)
    transport.pending.append(("message", json.dumps(EVENT_UPDATES[-1])))
    session.poll()
    assert session.view.buffered
    session.finish(grace_ms=100)
    for control in session.controls:
        session.acks[control["idempotency_key"]] = "applied"

    session.poll()
    assert session.state is Conn.ENDING
    if fill_gap:
        for message in EVENT_UPDATES[:-1]:
            transport.pending.append(("message", json.dumps(message)))
    else:
        transport.pending.append(("closed", "connection lost"))
    session.poll()
    assert session.state is Conn.ENDED
    assert session.end_settled is fill_gap
