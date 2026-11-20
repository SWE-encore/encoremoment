"""Camera activation and indication (R5, R6): Specifications IV-E and IV-F.

A fake camera device stands in for the media layer here only. The app itself
shows the camera as unavailable until a real device exists.
No network or GStreamer: a fake transport stands in for the WebSocket.
"""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from encore_client.sensing.camera import (REASON_NO_FIRST_FRAME, REASON_NO_SESSION,
                                          REASON_PERMISSION, CameraControl)
from encore_client.sensing.state import CameraState
from encore_client.session.session import Conn, Session


class FakeTransport:
    def __init__(self, url):
        self.pending, self.sent = [], []

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


class FakeCamera:
    """Records commands; the test fires the device callbacks."""

    def __init__(self, permission=True):
        self.permission = permission
        self.listener = None
        self.opened, self.closed = [], []

    def set_listener(self, listener):
        self.listener = listener

    def permission_granted(self):
        return self.permission

    def open(self, generation):
        self.opened.append(generation)

    def close(self, generation):
        self.closed.append(generation)


class Clock:
    def __init__(self):
        self.now = 10_000

    def __call__(self):
        return self.now


def live_session(clock):
    transports = []

    def factory(url):
        transports.append(FakeTransport(url))
        return transports[-1]

    session = Session("ws://test", "ses-camera", transport_factory=factory, clock=clock)
    session.start()
    transports[0].pending.append(("open", None))
    session.poll()
    assert session.state is Conn.CONNECTED
    return session, transports[0]


def make(permission=True, active=True):
    clock = Clock()
    session, transport = live_session(clock)
    device = FakeCamera(permission)
    flags = {"active": active}
    cam = CameraControl(device, session, lambda: flags["active"], clock=clock,
                        first_frame_timeout_ms=5_000)
    return cam, device, session, transport, clock, flags


def camera_controls(transport):
    return [(m["enabled"], m["generation"]) for m in transport.sent
            if m["kind"] == "control_update" and m["sensor"] == "camera"]


# ---- R5: off by default, explicit enable in an active session ----------------------


def test_camera_is_off_by_default_and_nothing_is_opened():
    cam, device, _, transport, _, _ = make()
    assert cam.state is CameraState.OFF
    assert device.opened == [] and camera_controls(transport) == []
    assert not cam.admission_open(1)


def test_enable_requires_an_active_session():
    cam, device, _, transport, _, _ = make(active=False)
    assert not cam.can_enable
    assert cam.enable() is False
    assert (cam.state, cam.message) == (CameraState.OFF, REASON_NO_SESSION)
    assert device.opened == [] and camera_controls(transport) == []


def test_permission_denial_keeps_it_off_and_opens_nothing():
    cam, device, _, transport, _, _ = make(permission=False)
    assert cam.enable() is False
    assert (cam.state, cam.message) == (CameraState.OFF, REASON_PERMISSION)
    assert cam.controller.generation == 0       # no generation issued before permission
    assert device.opened == [] and camera_controls(transport) == []


# ---- R6: the indicator follows device callbacks -----------------------------------


def test_on_only_after_the_first_real_frame():
    cam, device, session, transport, clock, _ = make()
    assert cam.enable() is True
    assert (cam.state, device.opened) == (CameraState.STARTING, [1])
    assert cam.admission_open(1)
    assert camera_controls(transport) == []      # not on until a frame really arrived
    clock.now = 10_400
    cam.on_first_frame(1)
    assert cam.state is CameraState.ON
    assert camera_controls(transport) == [(True, 1)]
    assert session.view.camera_intervals[0].on_ms == 10_400
    assert session.view.rejected == {}


def test_stop_closes_gate_first_and_shows_off_only_when_capture_stopped():
    cam, device, session, transport, clock, _ = make()
    cam.enable()
    cam.on_first_frame(1)
    assert cam.show_stop_button
    clock.now = 20_000
    assert cam.disable() is True
    assert not cam.admission_open(1)             # gate closed before cleanup
    assert device.closed == [1]                  # media layer cancels clips, clears unsaved video
    assert cam.state is CameraState.STOPPING and cam.show_stop_button
    assert camera_controls(transport) == [(True, 1), (False, 2)]
    cam.on_stopped(1)
    assert cam.state is CameraState.OFF and not cam.show_stop_button
    assert [(c.on_ms, c.off_ms) for c in session.view.camera_intervals] == [(10_000, 20_000)]
    assert session.view.rejected == {}


def test_no_first_frame_turns_camera_off_with_error():
    cam, device, _, transport, clock, _ = make()
    cam.enable()
    clock.now += 4_999
    cam.tick()
    assert cam.state is CameraState.STARTING
    clock.now += 1
    cam.tick()
    assert (cam.state, cam.message) == (CameraState.ERROR, REASON_NO_FIRST_FRAME)
    assert device.closed == [1] and not cam.admission_open(1)
    assert camera_controls(transport) == []      # it was never on


def test_open_failure_from_device():
    cam, device, _, transport, _, _ = make()
    cam.enable()
    cam.on_open_failed(1, "no camera found")
    assert (cam.state, cam.message) == (CameraState.ERROR, "no camera found")
    assert device.closed == [1] and camera_controls(transport) == []


def test_unplug_during_capture_shows_error_and_turns_camera_off():
    cam, device, session, transport, _, _ = make()
    cam.enable()
    cam.on_first_frame(1)
    cam.on_device_lost(1, "webcam unplugged")
    assert cam.state is CameraState.ERROR and not cam.show_stop_button
    assert "webcam unplugged" in cam.message
    assert device.closed == [1] and not cam.admission_open(1)
    assert camera_controls(transport) == [(True, 1), (False, 2)]
    assert session.view.rejected == {}


def test_reenable_starts_a_new_generation_and_ignores_late_callbacks():
    cam, device, session, transport, _, _ = make()
    cam.enable()
    cam.on_first_frame(1)
    cam.disable()
    cam.on_stopped(1)
    assert cam.enable() is True
    assert device.opened == [1, 3]
    cam.on_first_frame(1)                        # late frame from the old capture
    assert cam.state is CameraState.STARTING
    assert not cam.admission_open(1) and cam.admission_open(3)
    cam.on_first_frame(3)
    assert camera_controls(transport) == [(True, 1), (False, 2), (True, 3)]
    assert session.view.rejected == {}


def test_stop_while_starting_sends_nothing_and_late_frame_cannot_turn_it_on():
    cam, device, _, transport, _, _ = make()
    cam.enable()
    cam.disable()
    cam.on_first_frame(1)
    assert cam.state is CameraState.STOPPING
    cam.on_stopped(1)
    assert cam.state is CameraState.OFF
    assert camera_controls(transport) == []


def test_session_end_stops_the_camera():
    cam, device, _, transport, _, flags = make()
    cam.enable()
    cam.on_first_frame(1)
    cam.stop_for_session_end()
    flags["active"] = False
    assert device.closed == [1] and cam.state is CameraState.STOPPING
    assert camera_controls(transport) == [(True, 1), (False, 2)]
    assert not cam.can_enable


# ---- UI: indicator and Stop control (offscreen) -----------------------------------


@pytest.fixture(scope="module")
def app():
    """One QApplication kept alive for the whole module: PySide6 destroys an
    unreferenced QApplication, and creating widgets after that aborts Python."""
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_camera_panel_shows_actual_state_and_persistent_stop(app, tmp_path):
    from encore_client.ui.workspace import Workspace

    clock = Clock()
    session, transport = live_session(clock)
    device = FakeCamera()
    workspace = Workspace(session, None, tmp_path / "index.json", camera_device=device)
    panel = workspace.camera_panel
    try:
        workspace.refresh()
        assert "Camera off" in panel.indicator.text()
        assert panel.enable_btn.isEnabled() and panel.stop_btn.isHidden()

        panel.enable_btn.click()
        workspace.refresh()
        assert "starting" in panel.indicator.text().lower()
        assert not panel.stop_btn.isHidden()          # Stop is available while starting

        device.listener.on_first_frame(1)
        workspace.tabs.setCurrentIndex(1)             # switching views keeps the indicator
        workspace.refresh()
        assert "Camera on" in panel.indicator.text()

        panel.stop_btn.click()
        workspace.refresh()
        assert "stopping" in panel.indicator.text().lower()
        device.listener.on_stopped(1)
        workspace.refresh()
        assert "Camera off" in panel.indicator.text() and panel.stop_btn.isHidden()

        panel.enable_btn.click()
        device.listener.on_first_frame(3)
        workspace.session_panel.end_btn.click()       # End stops the camera first
        assert device.closed == [1, 3]
        assert camera_controls(transport)[-1] == (False, 4)
        assert session.state is Conn.ENDED
        app.processEvents()
    finally:
        workspace.shutdown()
        workspace.close()


def test_without_a_device_the_camera_is_shown_unavailable(app, tmp_path):
    from encore_client.ui.workspace import Workspace

    workspace = Workspace(Session("ws://127.0.0.1:1"), None, tmp_path / "index.json")
    try:
        assert workspace.camera_panel.indicator.text() == "Camera: Off — capture unavailable"
        assert workspace.camera_panel.enable_btn.isHidden()
    finally:
        workspace.shutdown()
        workspace.close()


def test_mic_and_camera_report_independent_states_in_the_same_workspace(app, tmp_path):
    from encore_client.sensing.microphone import MicrophoneSwitch
    from encore_client.ui.workspace import Workspace

    session, _ = live_session(Clock())
    device = FakeCamera()
    workspace = Workspace(session, None, tmp_path / "index.json", camera_device=device)
    try:
        switch = MicrophoneSwitch(session, None, source="test WAV")
        workspace.add_microphone(switch)
        workspace.camera_panel.enable_btn.click()
        device.listener.on_first_frame(1)
        session.set_sensor("microphone", True)
        workspace.refresh()
        assert "Microphone on" in workspace.microphone.status.text()
        workspace.microphone.button.click()
        workspace.refresh()
        assert "Microphone off" in workspace.microphone.status.text()
        assert "Camera on" in workspace.camera_panel.indicator.text()
        assert "Camera" not in workspace.sensing.text()
        workspace.microphone.button.click()
        workspace.session_panel.end_btn.click()
        assert not switch.device_running
        assert device.closed == [1]
    finally:
        workspace.shutdown()
        workspace.close()
