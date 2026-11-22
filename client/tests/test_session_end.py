"""R11 (EM-20): End finishes what is open before the stream closes.

A reaction in progress when the viewer presses End must arrive on the PC as
FINAL (Highlights lists only FINAL events), the microphone must be off before
the stream closes, and End must never hang or turn into a reconnect. The real
PC session against the real session server, through the relay in live_harness.
"""

import os
import time

import pytest

from encore_client.session.panel import status_text
from encore_client.session.session import Conn, Session, event_rows
from live_harness import assert_clean, microphone, start, wait, wait_for, write_wav

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def laugh_in_progress(tmp_path):
    """2 s quiet, then 2 s of laughter that has not stopped when the audio ends."""
    return write_wav(tmp_path / "laugh_in_progress.wav", [(2, 0), (2, 0.5)])


def index(session, what, kind=None, **fields):
    """Position of the first delivery-log entry that matches."""
    for i, e in enumerate(session.log.entries):
        msg = e.get("msg", {})
        if e["what"] == what and (kind is None or msg.get("kind") == kind) and all(
                msg.get(k) == v or (msg.get("event") or {}).get(k) == v for k, v in fields.items()):
            return i
    raise AssertionError(f"no {what} {kind} {fields} in the log")


def test_end_during_a_reaction_delivers_its_final_before_closing(server, relay, laugh_in_progress):
    app, _ = server
    session = start(app, relay, "ses-r11-end-open")
    server_session = app.sessions["ses-r11-end-open"]
    mic, clock = microphone(session, laugh_in_progress)
    clock.t += 4_000
    mic.pump()
    wait(session, lambda: session.open_events == ["evt-0001"])

    session.finish()
    assert session.state is Conn.ENDING
    assert "waiting for the server to close 1 open reaction" in status_text(session)
    wait(session, lambda: session.state is Conn.ENDED)

    assert session.end_settled is True
    (event,) = session.view.events.values()
    assert event["phase"] == "FINAL" and event == server_session.log[-1]["event"]
    assert [r["event_id"] for r in event_rows(session.view) if r["phase"] == "FINAL"] == ["evt-0001"]
    # Microphone off went out, the FINAL came back, and only then did the stream close.
    off = index(session, "out", "control_update", sensor="microphone", enabled=False)
    final = index(session, "in", "event_update", phase="FINAL")
    assert off < final < index(session, "end")
    assert server_session.view.sensors["microphone"].enabled is False
    # The server ended the session and had nothing left to send.
    wait_for(lambda: "ses-r11-end-open" not in app._active, "the server to end the session")
    assert len(server_session.log) == session.view.next_cursor - 1
    assert_clean(session, server_session)


def test_end_with_nothing_open_waits_only_for_the_acknowledgement(server, relay):
    app, _ = server
    session = start(app, relay, "ses-r11-end-quiet")
    started = time.monotonic()
    session.finish()
    wait(session, lambda: session.state is Conn.ENDED)
    assert time.monotonic() - started < 1.0
    assert session.end_settled is True and session.unacked_controls == []
    assert session.acks["ctl-microphone-2"] == "applied"


def test_microphone_already_off_is_not_turned_off_twice(server, relay):
    app, _ = server
    session = start(app, relay, "ses-r11-end-mic-off")
    session.set_sensor("microphone", False)
    session.finish()
    wait(session, lambda: session.state is Conn.ENDED)
    assert [m["generation"] for m in session.controls] == [1, 2]
    assert session.end_settled is True


def test_end_gives_up_after_the_grace_when_the_server_does_not_answer(server, relay):
    app, _ = server
    session = start(app, relay, "ses-r11-end-silent")
    relay.drop["server->pc"] = True
    session.finish(grace_ms=300)
    wait(session, lambda: session.state is Conn.ENDED)
    entries = session.log.entries
    waited = entries[index(session, "end_wait")]["t_ms"] - entries[index(session, "ending")]["t_ms"]
    assert 300 <= waited < 1_000  # on the session's own clock
    assert session.end_settled is False
    assert "did not confirm in time" in status_text(session)


def test_a_drop_while_ending_ends_without_reconnecting(server, relay):
    app, _ = server
    session = start(app, relay, "ses-r11-end-drop")
    relay.drop["server->pc"] = True
    session.finish(grace_ms=5_000)
    relay.cut()
    wait(session, lambda: session.state is Conn.ENDED, timeout=2.0)  # well before the grace
    assert session.end_settled is False and session.retry_in_ms is None
    time.sleep(0.5)
    session.poll()
    assert not any(e["what"] == "reconnect" for e in session.log.entries)


def test_end_while_disconnected_ends_at_once(server, relay):
    app, _ = server
    session = start(app, relay, "ses-r11-end-offline")
    relay.down()
    relay.cut()
    wait(session, lambda: session.state is Conn.DISCONNECTED)
    session.finish()
    assert session.state is Conn.ENDED and session.retry_in_ms is None and session.end_settled is None


def test_a_scenario_session_ends_at_once_and_sends_no_control():
    """Scenario runs replay a sample's own controls; End adds none of its own."""
    from encore_client.session.contract import SAMPLES_DIR
    from encore_client.session.fake_server import FakeServer
    from encore_client.session.session import load_scenario

    scenario = load_scenario(SAMPLES_DIR / "01_single_event.json")
    fake = FakeServer(scenario, port=0, interval_s=0).start()
    try:
        session = Session(fake.url, scenario["session_id"])
        session.start()
        wait(session, lambda: session.state is Conn.CONNECTED)
        session.finish()
        assert session.state is Conn.ENDED and session.controls == []
    finally:
        fake.stop()


def test_the_end_button_closes_the_microphone_before_telling_the_server(tmp_path):
    """Workspace End: device closed, then the off control, then Ending until the server confirms."""
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from encore_client.sensing.microphone import MicrophoneSwitch
    from encore_client.ui.workspace import Workspace

    app = QApplication.instance() or QApplication([])
    events = []

    class Device:
        def start(self):
            events.append("device open")

        def stop(self):
            events.append("device closed")

    class Transport:
        def __init__(self, url):
            self.pending = [("open", None)]

        def start(self):
            pass

        def send(self, text):
            import json
            msg = json.loads(text)
            if msg["kind"] == "control_update":
                events.append(f"control microphone {msg['enabled']}")
                self.pending.append(("message", json.dumps({
                    "schema": "encore.v1", "kind": "control_ack", "session_id": msg["session_id"],
                    "idempotency_key": msg["idempotency_key"], "sensor": msg["sensor"],
                    "generation": msg["generation"], "status": "applied"})))
            return True

        def poll(self):
            out, self.pending = self.pending, []
            return out

        def close(self):
            events.append("stream closed")

    session = Session("ws://test", "ses-r11-end-ui", transport_factory=Transport, microphone_on_start=True)
    workspace = Workspace(session, None, tmp_path / "index.json")
    try:
        switch = MicrophoneSwitch(session, Device(), source="test device")
        workspace.add_microphone(switch)
        workspace.session_panel.start_btn.click()
        workspace.session_panel.tick()  # the stream opens
        workspace.refresh()  # the switch opens the device for the microphone Start turned on
        assert events == ["control microphone True", "device open"]

        workspace.session_panel.end_btn.click()
        assert events[2:] == ["device closed", "control microphone False"]
        assert session.state is Conn.ENDING and "Ending" in workspace.session_panel.status.text()
        assert not workspace.session_panel.end_btn.isEnabled()
        session.poll()  # the acknowledgement arrives
        assert session.state is Conn.ENDED and events[-1] == "stream closed"
        assert session.end_settled is True
        app.processEvents()
    finally:
        workspace.shutdown()
        workspace.close()
