"""R11 (EM-20): repeat Start/End in one run of the PC app.

After End, Start begins a new session with a new ID. Each session gets its own
events from the real session server, starts with the microphone and the camera
off, and nothing of the session before it (generations, cursor, controls, audio,
buffered capture, recorder) carries over. Offscreen Qt; the real server through
the relay in live_harness, except where a scripted transport makes a late
message from the old stream possible to check.
"""

import json
import os
import subprocess
import sys

import pytest

from encore_client.__main__ import ROOT, SessionMaker
from encore_client.player.anchors import AnchorTracker
from encore_client.sensing.microphone import MicrophoneSwitch
from encore_client.sensing.state import CameraState
from encore_client.sensing.wav_mic import WavMicrophone
from encore_client.session.session import Conn, Session
from live_harness import Clock, assert_clean, wait, wait_for, write_wav

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def app():
    """One QApplication for the module: PySide6 destroys an unreferenced one."""
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def laugh(tmp_path):
    """2 s quiet, 2 s of laughter, 4 s quiet: one FINAL laugh per session."""
    return write_wav(tmp_path / "laugh.wav", [(2, 0), (2, 0.5), (4, 0)])


class FakeRecorder:
    """Stands in for DesktopRecorder: the clip recorder and the camera device."""

    def __init__(self, made, index, session, *, may_commit):
        made.append(self)
        self.session = session
        self.error = None
        self.listener = None
        self.opened, self.closed, self.audio, self.events = [], [], [], []
        self.shut_down = False

    def set_listener(self, listener):
        self.listener = listener

    def permission_granted(self):
        return True

    def open(self, generation):
        self.opened.append(generation)

    def close(self, generation):
        self.closed.append(generation)
        self.listener.on_stopped(generation)

    def latest_face(self, start_ms=None, end_ms=None):
        return None

    def feed_audio(self, pcm, capture_ms):
        self.audio.append(capture_ms)

    def poll(self, events):
        self.events = [e["event_id"] for e in events]

    def shutdown(self):
        self.shut_down = True


class Device:
    """A microphone capture device that records when it is opened and closed."""

    def __init__(self):
        self.log = []

    def start(self):
        self.log.append("open")

    def stop(self):
        self.log.append("close")


def open_workspace(maker, tmp_path, *, recorders=None):
    """The app's live window without the player: a workspace whose Start after End uses ``maker``."""
    from encore_client.ui.workspace import Workspace

    first = maker()
    # The player's newest anchor goes to whichever session is current (__main__ wiring).
    tracker = AnchorTracker("ses-player", emit=maker.send_anchor)
    tracker.new_content("file:film-a", "Film A")
    tracker.report(60_000, 1.0, True, capture_ms=9_500)
    factory = None if recorders is None else (lambda *a, **kw: FakeRecorder(recorders, *a, **kw))
    return Workspace(first, None, tmp_path / "index.json", require_anchor=True,
                     recorder_factory=factory, session_factory=maker)


def start_and_wait(workspace, server_app):
    workspace.session_panel.start_btn.click()
    session = workspace.session
    wait(session, lambda: session.acks.get("ctl-microphone-1") == "applied")
    wait_for(lambda: server_app.sessions[session.session_id].view.anchors, "the server's anchor")
    workspace.refresh()  # the microphone switch follows the microphone Start turned on
    return session


def end_and_wait(workspace):
    session = workspace.session
    workspace.session_panel.end_btn.click()
    wait(session, lambda: session.state is Conn.ENDED)
    workspace.session_panel.refresh()
    workspace.refresh()
    assert session.end_settled is True


def finals(session):
    return [e["event_id"] for e in session.view.events.values() if e["phase"] == "FINAL"]


def test_start_end_start_end_gives_two_sessions_with_their_own_events(app, server, relay, laugh, tmp_path):
    server_app, _ = server
    maker = SessionMaker(f"ws://127.0.0.1:{relay.port}")
    workspace = open_workspace(maker, tmp_path)
    clock = Clock(10_000)
    mic = WavMicrophone(workspace.session, laugh, clock=clock)
    workspace.session_users.append(mic)
    workspace.add_microphone(MicrophoneSwitch(workspace.session, None, source="test WAV"))
    panel = workspace.session_panel
    sessions = []
    try:
        for n in range(2):
            session = start_and_wait(workspace, server_app)
            sessions.append(session)
            mic.pump()  # fixes the capture time of the file's first sample
            clock.play(mic, 8_000)
            wait(session, lambda: finals(session) == ["evt-0001"])
            panel.tick()
            workspace.refresh()
            assert "evt-0001" in workspace.highlights.text()
            end_and_wait(workspace)
            assert panel.start_btn.isEnabled() and "Start session begins a new one" in panel.status.text()
            if n == 0:
                first_events = json.loads(json.dumps(session.view.events))

        first, second = sessions
        assert first.session_id != second.session_id
        assert all(s.session_id.startswith("ses-") for s in sessions)
        assert set(server_app.sessions) >= {first.session_id, second.session_id}
        for session in sessions:
            server_session = server_app.sessions[session.session_id]
            (event,) = session.view.events.values()
            # Each session holds exactly the events its own server session sent.
            assert event == server_session.log[-1]["event"] and event["phase"] == "FINAL"
            assert len(server_session.log) == session.view.next_cursor - 1
            assert event["playback"]["title"] == "Film A"
            assert_clean(session, server_session)
            # Each session's microphone started at generation 1, its audio at chunk 0.
            assert [(m["generation"], m["enabled"]) for m in session.controls] == [(1, True), (2, False)]
            assert session.controls[0]["seq"] == 0
            audio = [e["msg"] for e in session.log.entries if e["what"] == "out"
                     and e["msg"]["kind"] == "audio_chunk"]
            assert (audio[0]["generation"], audio[0]["chunk"]) == (1, 0)
        assert first.view.events == first_events  # the second session did not touch the first
        assert first.view.events["evt-0001"]["capture"] != second.view.events["evt-0001"]["capture"]
        # The window shows the session that is current, and only its events.
        assert second.session_id in panel.title.text() and first.session_id not in panel.title.text()
        assert panel.table.rowCount() == 1
        assert workspace.highlights.session is second and workspace.highlights.text().count("evt-0001") == 1
    finally:
        workspace.shutdown()
        workspace.close()
        app.processEvents()


def test_the_second_session_starts_with_the_microphone_and_camera_off(app, server, relay, tmp_path):
    server_app, _ = server
    maker = SessionMaker(f"ws://127.0.0.1:{relay.port}")
    recorders = []
    workspace = open_workspace(maker, tmp_path, recorders=recorders)
    device = Device()
    switch = MicrophoneSwitch(workspace.session, device, source="test device")
    workspace.add_microphone(switch)
    try:
        first = start_and_wait(workspace, server_app)
        assert device.log == ["open"]
        workspace.camera_panel.enable_btn.click()
        recorders[0].listener.on_first_frame(1, 10_000)
        workspace.microphone.button.click()  # off and on again: microphone generation 3
        workspace.microphone.button.click()
        workspace.refresh()
        wait(first, lambda: first.unacked_controls == [])
        assert workspace.camera.state is CameraState.ON
        assert (first.view.sensors["microphone"].generation, first.view.sensors["camera"].generation) == (3, 1)
        old_camera = workspace.camera
        end_and_wait(workspace)

        # Between sessions: both devices are off.
        assert recorders[0].closed == [1] and workspace.camera.state is CameraState.OFF
        assert not switch.device_running and device.log[-1] == "close"
        assert "session ended" in workspace.microphone.status.text()

        workspace.session_panel.start_btn.click()
        second = workspace.session
        assert second is not first and second.state is Conn.CONNECTING
        # Fresh generations: the camera is off at generation 0 and the microphone
        # that Start turns on is generation 1, not the old session's 4.
        assert workspace.camera is not old_camera and workspace.camera.state is CameraState.OFF
        assert workspace.camera.controller.generation == 0
        assert second.view.sensors["camera"].enabled is False and second.view.sensors["camera"].generation == 0
        assert second.view.sensors["microphone"].generation == 1
        assert [m["idempotency_key"] for m in second.controls] == ["ctl-microphone-1"]

        wait(second, lambda: second.acks.get("ctl-microphone-1") == "applied")
        workspace.refresh()
        # The device was closed at End and opens again for the new session's microphone.
        assert switch.device_running and device.log == ["open", "close", "open", "close", "open"]
        server_second = server_app.sessions[second.session_id]
        assert server_second.view.sensors["camera"].enabled is False
        assert server_second.view.sensors["camera"].generation == 0
        assert server_second.view.sensors["microphone"].generation == 1
        # The camera is enabled only on request, again from generation 1, on the new recorder.
        assert recorders[1].opened == []
        workspace.camera_panel.enable_btn.click()
        recorders[1].listener.on_first_frame(1, 20_000)
        wait(second, lambda: second.acks.get("ctl-camera-1") == "applied")
        assert recorders[1].opened == [1] and recorders[0].opened == [1]
        end_and_wait(workspace)
        assert recorders[1].closed == [1]
    finally:
        workspace.shutdown()
        workspace.close()
        app.processEvents()


class ScriptedTransport:
    """A stream the test controls: acknowledges controls, and can deliver a late frame."""

    made = None

    def __init__(self, url):
        self.url = url
        self.pending = [("open", None)]
        self.sent = []
        self.closed = False
        ScriptedTransport.made.append(self)

    def start(self):
        pass

    def send(self, data):
        if isinstance(data, bytes):
            self.sent.append(data)
            return True
        msg = json.loads(data)
        self.sent.append(msg)
        if msg["kind"] == "control_update":
            self.pending.append(("message", json.dumps({
                "schema": "encore.v1", "kind": "control_ack", "session_id": msg["session_id"],
                "idempotency_key": msg["idempotency_key"], "sensor": msg["sensor"],
                "generation": msg["generation"], "status": "applied"})))
        return True

    def poll(self):
        out, self.pending = self.pending, []
        return out

    def close(self):
        self.closed = True


def test_nothing_of_the_ended_session_carries_over(app, tmp_path):
    from encore_client.sensing.live_mic import LiveMicrophone

    ScriptedTransport.made = transports = []
    log_path = tmp_path / "delivery.jsonl"
    maker = SessionMaker("ws://test", log_path, transport_factory=ScriptedTransport)
    recorders = []
    workspace = open_workspace(maker, tmp_path, recorders=recorders)
    source = LiveMicrophone(workspace.session, on_pcm=workspace.feed_audio)
    workspace.live_microphone = source
    workspace.add_microphone(MicrophoneSwitch(workspace.session, None, source="test"))
    try:
        workspace.session_panel.start_btn.click()
        first = workspace.session
        workspace.session_panel.tick()
        workspace.refresh()
        workspace.camera_panel.enable_btn.click()
        recorders[0].listener.on_first_frame(1, 10_000)
        # 1.25 s of capture: two chunks sent and recorded, a quarter second still buffered.
        captured = first.state_since_ms + 1_000
        for _ in range(5):
            captured += 250
            source.feed(bytes(8_000), end_ms=captured)
        assert source.sent == 2 and len(source.recording) == 32_000 and source._buf
        assert recorders[0].audio and first.audio_sent == 2
        workspace.session_panel.end_btn.click()
        workspace.session_panel.tick()
        assert first.state is Conn.ENDED
        first_view = first.view.summary()
        first_received = first.received

        workspace.session_panel.start_btn.click()
        second = workspace.session
        # The old stream is closed and never read again: a frame it still delivers is lost.
        transports[0].pending.append(("message", json.dumps({
            "schema": "encore.v1", "kind": "control_ack", "session_id": first.session_id,
            "idempotency_key": "ctl-camera-9", "sensor": "camera", "generation": 9, "status": "applied"})))
        workspace.session_panel.tick()  # the new stream opens
        workspace.session_panel.tick()  # its acknowledgement arrives
        workspace.refresh()
        assert transports[0].closed and first.received == first_received and first.view.summary() == first_view
        assert second.state is Conn.CONNECTED and second.acks == {"ctl-microphone-1": "applied"}

        # The session: new ID, cursor, seq, controls, counters.
        assert second.session_id != first.session_id
        assert second.view.events == {} and second.view.next_cursor == 1
        assert [m["seq"] for m in transports[1].sent][:2] == [0, 1]  # the microphone, then the anchor
        assert transports[1].sent[1]["kind"] == "playback_anchor"
        assert transports[1].sent[1]["session_id"] == second.session_id
        assert all(m["session_id"] == second.session_id for m in transports[1].sent)
        assert (second.audio_sent, second.audio_dropped, second.unsent) == (0, 0, [])
        # The audio source: nothing captured for the first session is sent or recorded in the second.
        assert source.session is second and source.sent == 0 and source.recording == bytearray()
        assert not source._buf and source._run is None and source.aec_status == "off"
        # The recorder: the old one is shut down (its unsaved capture with it), a new one records.
        assert recorders[0].shut_down and recorders[1].session is second and not recorders[1].shut_down
        assert first.camera_frame is None and second.camera_frame == recorders[1].latest_face
        audio_before = list(recorders[0].audio)
        workspace.feed_audio(b"\0\0", 1)
        assert recorders[1].audio == [1] and recorders[0].audio == audio_before
        # The window: the panel, Highlights, the switch and the camera all follow the new session.
        assert workspace.session_panel.session is second and workspace.session_panel.table.rowCount() == 0
        assert workspace.highlights.session is second and workspace.microphone.switch.session is second
        assert workspace.camera.session is second and workspace.camera.state is CameraState.OFF
        # Both sessions' steps are in the one delivery log; the first session's file handle is closed.
        assert first.log._fh is None
        starts = [json.loads(line)["session_id"] for line in log_path.read_text().splitlines()
                  if json.loads(line)["what"] == "start"]
        assert starts == [first.session_id, second.session_id]
    finally:
        workspace.shutdown()
        workspace.close()
        app.processEvents()


class CameraSource:
    """Stands in for GstCamera under the real DesktopRecorder: a frame on every poll."""

    def __init__(self, directory):
        self.path = directory

    def new_fragment(self):
        pass

    def poll(self):
        from encore_client.clock import monotonic_ms
        return [], (monotonic_ms(), bytes(320 * 180 * 3))

    def close(self):
        pass


def test_unsaved_capture_is_gone_between_sessions_and_saved_clips_stay(app, tmp_path, monkeypatch):
    """The real DesktopRecorder: End removes the capture directory, the next session gets a
    new recorder and directory, and the saved clip and its index row are untouched."""
    from encore_client.clips.recorder import DesktopRecorder
    from encore_client.clock import monotonic_ms
    from encore_client.ui.workspace import Workspace

    monkeypatch.setenv("ENCORE_CAMERA_DEVICE", str(tmp_path / "no-camera"))  # no OS permission prompt
    ScriptedTransport.made = []
    index = tmp_path / "index.json"
    saved = {"clip_id": "saved-earlier", "session_id": "ses-earlier", "event_ids": ["evt-0001"],
             "event_type": "laughter", "intensity": 0.7, "timecode": "00:01:00", "status": "ready",
             "path": "saved-earlier.webm"}
    index.write_text(json.dumps({"clips": [saved]}))
    (tmp_path / "saved-earlier.webm").write_bytes(b"saved clip")
    maker = SessionMaker("ws://test", transport_factory=ScriptedTransport)
    recorders = []

    def recorder_factory(index_path, session, *, may_commit):
        recorder = DesktopRecorder(index_path, session, may_commit=may_commit, camera_factory=CameraSource)
        recorders.append(recorder)
        return recorder

    workspace = Workspace(maker(), None, index, recorder_factory=recorder_factory, session_factory=maker)
    captures = lambda: sorted(p.name for p in tmp_path.glob(".encore-camera-*"))  # noqa: E731
    directories = []
    try:
        for n in range(2):
            workspace.session_panel.start_btn.click()
            session = workspace.session
            workspace.session_panel.tick()
            workspace.refresh()
            workspace.camera_panel.enable_btn.click()
            workspace.refresh()  # the recorder sees the first frame: the camera is on
            assert workspace.camera.state is CameraState.ON and session.view.sensors["camera"].enabled
            workspace.feed_audio(bytes(3_200), monotonic_ms())
            (capture,) = captures()
            directories.append(capture)
            assert list((tmp_path / capture).glob("mic-*.pcm"))  # unsaved audience audio
            workspace.session_panel.end_btn.click()
            workspace.session_panel.tick()
            assert session.state is Conn.ENDED and captures() == []
            assert json.loads(index.read_text())["clips"] == [saved]
            assert (tmp_path / "saved-earlier.webm").read_bytes() == b"saved clip"
        assert directories[0] != directories[1] and len(recorders) == 2
        assert recorders[1].session is workspace.session and recorders[0].session is not workspace.session
        assert workspace.clips.clips[0].clip_id == "saved-earlier"
    finally:
        workspace.shutdown()
        workspace.close()
        app.processEvents()


def test_sessions_option_is_for_headless_live_runs():
    """A scenario replays a sample with a fixed session ID; the window uses Start instead."""
    from encore_client.__main__ import main

    for argv in (["--demo", "--headless", "--sessions", "2"], ["--sessions", "2", "--no-player", "--mic"]):
        with pytest.raises(SystemExit) as exc:
            main(argv)
        assert exc.value.code == 2


def test_session_maker_hands_the_newest_anchor_to_the_next_session_only():
    maker = SessionMaker("ws://test", live=True)
    first = maker()
    tracker = AnchorTracker("ses-player", emit=maker.send_anchor)
    tracker.new_content("file:film-a", "Film A")
    tracker.report(60_000, 1.0, True, capture_ms=9_500)
    assert first.last_anchor["playback"]["position_ms"] == 60_000
    second = maker()
    assert second.last_anchor["session_id"] == second.session_id
    assert second.last_anchor["playback"] == first.last_anchor["playback"]
    tracker.report(70_000, 1.0, False, capture_ms=19_500)  # a pause after the new session exists
    assert second.last_anchor["playback"]["position_ms"] == 70_000
    assert first.last_anchor["playback"]["position_ms"] == 60_000
    assert second.microphone_on_start and second.reconnect_delays_ms
    maker.microphone_on_start = False  # no microphone could be opened
    assert maker().microphone_on_start is False


def ui_wait(app, predicate, timeout=8.0):
    import time
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "UI condition timed out"
        app.processEvents()
        time.sleep(0.01)


def test_window_run_starts_a_new_session_after_end(app, server, tmp_path, monkeypatch):
    """The app's own window path (``--no-player --mic-wav``), driven through its buttons:
    Start after End gives a new session that the WAV microphone and the recorder follow."""
    from PySide6.QtWidgets import QApplication, QMainWindow

    from encore_client.__main__ import main
    from encore_client.ui.workspace import Workspace

    server_app, port = server
    wav = write_wav(tmp_path / "quiet.wav", [(1, 0)])
    shots = os.environ.get("ENCORE_RESTART_SCREENSHOTS")  # a directory for PR evidence
    seen = []

    def drive(_app):
        win = next(w for w in app.topLevelWidgets() if isinstance(w, QMainWindow) and w.isVisible())
        workspace = win.findChild(Workspace)
        mic = workspace.session_users[0]
        try:
            for n in range(2):
                ui_wait(app, lambda: workspace.session_panel.start_btn.isEnabled())
                workspace.session_panel.start_btn.click()
                session = workspace.session
                ui_wait(app, lambda: mic.done and session.audio_sent == 2 and not session.media_pending)
                ui_wait(app, lambda: server_app.sessions[session.session_id].view.audio_chunks == 2)
                assert mic.session is session
                # The app makes the clip recorder only on Linux; elsewhere there is none.
                if sys.platform.startswith("linux"):
                    assert workspace.recorder.session is session
                else:
                    assert workspace.recorder is None
                if shots and n == 1:
                    win.grab().save(os.path.join(shots, "2-second-session.png"))
                workspace.session_panel.end_btn.click()
                ui_wait(app, lambda: session.state is Conn.ENDED)
                seen.append(session)
                if shots and n == 0:
                    ui_wait(app, lambda: workspace.session_panel.start_btn.isEnabled())
                    win.grab().save(os.path.join(shots, "1-after-first-end.png"))
        finally:
            workspace.shutdown()
            win.close()
        return 0

    monkeypatch.setattr(QApplication, "exec", drive)
    assert main(["--no-player", "--mic-wav", str(wav), "--server", f"ws://127.0.0.1:{port}",
                 "--clip-index", str(tmp_path / "index.json")]) == 0
    first, second = seen
    assert first.session_id != second.session_id
    for session in seen:
        server_session = server_app.sessions[session.session_id]
        assert server_session.view.last_audio_chunk == {1: 1}  # chunks 0 and 1 of generation 1
        assert server_session.view.audio_gaps == 0 and server_session.view.rejected == {}
        assert server_session.view.anchors[0]["content_id"] == "mic-clock"  # each Start sets its own


def test_headless_sessions_start_and_end_one_after_another(server, tmp_path):
    """``--headless --sessions 2``: two live sessions with new IDs against the real server."""
    server_app, port = server
    wav = write_wav(tmp_path / "quiet.wav", [(1, 0)])
    result = subprocess.run(
        [sys.executable, "-m", "encore_client", "--headless", "--server", f"ws://127.0.0.1:{port}",
         "--mic-wav", str(wav), "--timeout", "0.3", "--sessions", "2", "--log", str(tmp_path / "log.jsonl")],
        cwd=ROOT / "client", capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    ids = [line.split(": ", 1)[1] for line in result.stdout.splitlines() if line.startswith("[session] 1/2")
           or line.startswith("[session] 2/2")]
    assert len(ids) == 2 and ids[0] != ids[1]
    assert result.stdout.count("[session] connected") == 2
    assert result.stdout.count("[session] ignored {}  rejected {}") == 2
    for sid in ids:
        server_session = server_app.sessions[sid]
        assert server_session.view.sensors["microphone"].generation == 2  # on at Start, off at End
        assert server_session.view.audio_gaps == 0 and server_session.view.rejected == {}
    entries = [json.loads(line) for line in (tmp_path / "log.jsonl").read_text().splitlines()]
    assert [e["session_id"] for e in entries if e["what"] == "start"] == ids
