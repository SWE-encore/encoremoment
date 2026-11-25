"""The main window warns when the microphone delivers only exact zeros (EM-26 live run).

A muted input (system settings or a hardware switch) sends silence that looks like a
quiet room, so no reaction is ever detected. The bench window already said so; the
workspace said nothing.
"""
import json

import pytest

from encore_client.sensing.live_mic import LiveMicrophone
from encore_client.sensing.microphone import MicrophoneSwitch
from encore_client.session.session import Conn, Session

ZEROS = bytes(3_200)  # 0.1 s read of exact zeros
QUIET = b"\x01\x00" + bytes(3_198)  # a quiet room: never exactly zero


class Transport:
    def __init__(self, url):
        self.pending = [("open", None)]

    def start(self):
        pass

    def send(self, frame):
        if isinstance(frame, str) and json.loads(frame)["kind"] == "control_update":
            msg = json.loads(frame)
            self.pending.append(("message", json.dumps({
                "schema": "encore.v1", "kind": "control_ack", "session_id": msg["session_id"],
                "idempotency_key": msg["idempotency_key"], "sensor": msg["sensor"],
                "generation": msg["generation"], "status": "applied"})))
        return True

    def poll(self):
        out, self.pending = self.pending, []
        return out

    def close(self):
        pass


class Device:
    """Stands in for QtAudioInput, which tells the microphone when it opens."""

    def __init__(self, mic):
        self.mic = mic

    def start(self):
        self.mic.device_started(settle_ms=0)

    def stop(self):
        pass


def feed(mic, reads, pcm=ZEROS):
    for _ in range(reads):
        mic.feed(pcm)


def test_the_workspace_warns_only_while_an_open_microphone_sends_exact_zeros(tmp_path):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from encore_client.ui.workspace import Workspace

    app = QApplication.instance() or QApplication([])
    session = Session("ws://test", "ses-muted-mic", transport_factory=Transport, microphone_on_start=True)
    workspace = Workspace(session, None, tmp_path / "index.json")
    warning = workspace.mic_muted
    try:
        mic = LiveMicrophone(session, keep_s=0)
        workspace.live_microphone = mic
        switch = MicrophoneSwitch(session, Device(mic), source="test device")
        workspace.add_microphone(switch)
        workspace.refresh()
        assert warning.isHidden()  # before Start

        workspace.session_panel.start_btn.click()
        workspace.session_panel.tick()  # the stream opens
        workspace.session_panel.tick()  # the microphone's acknowledgement
        workspace.refresh()  # the switch opens the device
        assert session.state is Conn.CONNECTED and switch.enabled and switch.device_running

        feed(mic, 14)  # 1.4 s of zeros: could still be the device starting
        workspace.refresh()
        assert warning.isHidden()
        feed(mic, 2)
        workspace.refresh()
        assert not warning.isHidden() and "muted" in warning.text()

        feed(mic, 1, QUIET)  # sound again, however quiet
        workspace.refresh()
        assert warning.isHidden()

        feed(mic, 20)
        workspace.refresh()
        assert not warning.isHidden()
        switch.turn_off()  # the user turns the microphone off: nothing is read, nothing to warn about
        workspace.refresh()
        assert warning.isHidden()
        switch.turn_on()  # on again: the zeros of the last run do not count
        workspace.session_panel.tick()
        workspace.refresh()
        assert switch.enabled and warning.isHidden()

        feed(mic, 20)
        workspace.refresh()
        assert not warning.isHidden()
        workspace.session_panel.end_btn.click()
        workspace.refresh()
        assert warning.isHidden()
        app.processEvents()
    finally:
        workspace.shutdown()
        workspace.close()
