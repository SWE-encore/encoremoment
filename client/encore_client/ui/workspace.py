"""One workspace for actual session state, events and saved clip review.

Repeat Start/End (R11). With a ``session_factory``, Start after End creates a
fresh session (new ID, generations, cursor and controls) and hands it to every
part that held the old one: the session panel, Highlights, the camera control
and panel, the desktop recorder, the microphone switch and the audio sources
(``use_session``). Between sessions the microphone and camera are off, and the
previous session's unsaved capture is removed with its recorder; saved clips
and the clip index stay.
"""
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QLabel, QPushButton, QTabWidget, QVBoxLayout, QWidget
from ..clips.deletion import DeletionManager
from ..clips.model import playable_path
from ..clips.panel import ClipPanel
from ..sensing.camera import CameraControl
from ..session.panel import SessionPanel
from ..session.session import STARTED, Conn
from .camera_panel import CameraPanel
from .highlights import HighlightsPanel

ACTIVE = STARTED  # Start pressed, End not: a drop does not stop the devices
MUTED_TEXT = ("Microphone: no sound at all, so reactions cannot be detected. "
              "Is this microphone muted in the system settings?")


class Workspace(QWidget):
    def __init__(self, session, driver, index_path, *, require_anchor=False, demo=False,
                 camera_device=None, recorder_factory=None, session_factory=None):
        """``session_factory``: returns a new, not yet started ``Session``. With it,
        Start after End begins a new session in the same run (R11); without it
        (a scenario run), a session ends the run."""
        super().__init__()
        self.setMinimumWidth(540)
        self.session = session
        self.index_path = index_path
        self.session_factory = session_factory
        self.session_users = []  # other holders of the session: each has use_session(session)
        self.microphone = None
        self.deletions = DeletionManager(index_path)
        self.recorder = None
        self.live_microphone = None
        self.echo = None   # EchoControl (R12), under the microphone control
        self.screen = None        # ScreenRecorder (clips/screen.py), with desktop capture
        self.screen_panel = None
        self._camera_device = camera_device
        self._recorder_factory = recorder_factory
        if recorder_factory is not None:
            self.recorder = self._new_recorder(session)
            camera_device = self.recorder
        layout = QVBoxLayout(self)
        # Camera (R5, R6): real state from the media layer's device, or "unavailable".
        self.camera = self._new_camera(camera_device, session)
        self.camera_panel = CameraPanel(self.camera)
        layout.addWidget(self.camera_panel)
        self.sensing = QLabel("Microphone: Off — capture unavailable")
        layout.addWidget(self.sensing)
        self.aec_label = QLabel()
        self.aec_label.hide()
        layout.addWidget(self.aec_label)
        self.mic_muted = QLabel(MUTED_TEXT)
        self.mic_muted.setWordWrap(True)
        self.mic_muted.setStyleSheet("color: #c62828; font-weight: 600")
        self.mic_muted.hide()
        layout.addWidget(self.mic_muted)
        self.recording_error = QLabel()
        self.recording_error.setWordWrap(True)
        self.recording_error.hide()
        layout.addWidget(self.recording_error)
        if demo:
            label = QLabel("Demo: prerecorded contract messages; no camera, microphone or AI inference.")
            label.setWordWrap(True)
            layout.addWidget(label)
        self.session_panel = SessionPanel(session, driver, require_anchor=require_anchor,
                                          restartable=session_factory is not None)
        # Start after End makes a new session first; End stops the camera and the
        # microphone first, so their off controls go out and the server closes open
        # reactions before the stream closes (R11).
        self.session_panel.start_btn.clicked.disconnect()
        self.session_panel.start_btn.clicked.connect(self.start_session)
        self.session_panel.end_btn.clicked.disconnect()
        self.session_panel.end_btn.clicked.connect(self.end_session)
        layout.addWidget(self.session_panel, 2)
        self.play_event = QPushButton("Play selected reaction clip")
        self.play_event.setEnabled(False)
        self.play_event.clicked.connect(self.play_selected_event)
        self.session_panel.table.itemSelectionChanged.connect(self.refresh_event_link)
        self.session_panel.table.cellDoubleClicked.connect(lambda _row, _col: self.play_selected_event())
        layout.addWidget(self.play_event)
        self.tabs = QTabWidget()
        # Tonight's Highlights (R4): FINAL events by intensity, linked to saved clips.
        # Clip deletion (R7): journal and tombstones next to the clip index. The
        # server has no cleanup API yet, so only local cleanup is required.
        self.highlights = HighlightsPanel(session, index_path, deletions=self.deletions)
        self.tabs.addTab(self.highlights, "Tonight's Highlights")
        self.clips = ClipPanel(index_path, deletions=self.deletions)
        self.tabs.addTab(self.clips, "Saved clips")
        self.highlights.play_requested.connect(self.play_clip)
        layout.addWidget(self.tabs, 1)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(100)

    def _new_recorder(self, session):
        recorder = self._recorder_factory(self.index_path, session, may_commit=self.deletions.may_commit_export)
        session.camera_frame = recorder.latest_face
        return recorder

    def _new_camera(self, device, session):
        """A camera control for one session: off, generation 0 (R5, R11)."""
        if device is None:
            return None
        return CameraControl(device, session, lambda: session.state in ACTIVE)

    def add_microphone(self, switch):
        """Show the microphone on/off control under the sensing line (R8)."""
        from .microphone import MicrophoneControl
        self.microphone = MicrophoneControl(switch)
        layout = self.layout()
        layout.insertWidget(layout.indexOf(self.sensing) + 1, self.microphone)

    def add_screen_recording(self, recorder, unavailable_reason=None):
        """Show the Screen switch under the camera control (clips/screen.py)."""
        from .screen_panel import ScreenPanel
        self.screen = recorder
        self.screen_panel = ScreenPanel(recorder, unavailable_reason)
        layout = self.layout()
        layout.insertWidget(layout.indexOf(self.camera_panel) + 1, self.screen_panel)

    def add_echo_control(self, mode):
        """Show the echo cancellation switch and its requested/effective mode (R12).
        It replaces the plain status line and follows each new session."""
        from .echo import EchoControl
        self.echo = EchoControl(mode)
        self.session_users.append(mode)
        layout = self.layout()
        layout.insertWidget(layout.indexOf(self.aec_label), self.echo)
        self.aec_label.hide()

    def feed_audio(self, pcm, capture_ms):
        """Microphone PCM for the current session's camera recording (``on_pcm``)."""
        if self.recorder is not None:
            self.recorder.feed_audio(pcm, capture_ms)

    def start_session(self):
        """Start; after End, first replace the ended session with a new one (R11)."""
        if self.session.state is Conn.ENDED and self.session_factory is not None:
            if self.session_panel.require_anchor and self.session.last_anchor is None:
                return  # no video open: the new session could not start either
            self.use_session(self.session_factory())
        self.session_panel.start()  # the devices follow on the next refresh, as for the first session

    def use_session(self, session):
        """Hand a new, not yet started session to everything that held the old one.

        Nothing of the old session carries over: its devices are closed, its
        recorder (and the unsaved capture in it) is shut down, its stream is
        closed and its log file is closed. The camera control is new, so camera
        generations start again at 0 like the session's own.
        """
        old = self.session
        if old.state not in (Conn.IDLE, Conn.ENDED):
            raise RuntimeError(f"use_session() while the session is {old.state.value}; end it first")
        if session.state is not Conn.IDLE:
            raise RuntimeError("use_session() needs a session that has not started")
        if self.camera is not None:
            self.camera.stop_for_session_end()  # already off after End; closes it otherwise
        if self.microphone is not None:
            self.microphone.switch.use_session(session)  # closes the device first
        if self.live_microphone is not None:
            self.live_microphone.use_session(session)
        for user in self.session_users:
            user.use_session(session)
        camera_device = self._camera_device
        if self.recorder is not None:
            old.camera_frame = None
            self.recorder.shutdown()  # removes the old session's unsaved capture
            self.recorder = self._new_recorder(session)
            camera_device = self.recorder
        old.end()
        old.log.close()
        self.session = session
        self.camera = self._new_camera(camera_device, session)
        self.camera_panel.camera = self.camera
        self.session_panel.use_session(session)
        self.highlights.use_session(session)
        self.refresh()

    def end_session(self):
        if self.screen is not None:
            self.screen.turn_off()
        if self.camera is not None:
            self.camera.stop_for_session_end()
        if self.microphone is not None:
            self.microphone.switch.turn_off()  # closes the device, then tells the server
        self.session_panel.end()
        self.refresh()

    def refresh(self):
        if self.camera is not None and self.session.state not in ACTIVE:
            self.camera.stop_for_session_end()   # the session ended or dropped out of Start
        if self.recorder is not None:
            self.recorder.poll(self.session.view.events.values())
            error = self.recorder.error
            self.recording_error.setText(f"Clip recording: {error}" if error else "")
            self.recording_error.setVisible(bool(error))
        self.camera_panel.refresh()
        if self.microphone is not None:
            self.microphone.refresh()
        if self.echo is not None:
            self.echo.refresh()
        elif self.live_microphone is not None:
            self.aec_label.setText(f"Echo cancellation: {self.live_microphone.aec_status}")
            self.aec_label.setVisible(True)
            # Only while the microphone is on: its zeros from before it was turned off are stale.
            self.mic_muted.setVisible(self.session.state in ACTIVE and self.live_microphone.muted
                                      and self.session.view.sensors["microphone"].enabled)
        self.deletions.tick()
        if self.screen is not None:
            active = self.session.state in ACTIVE
            if not active and self.screen.on:
                self.screen.turn_off()   # the session ended or dropped out of Start
            self.screen.sessions.add(self.session.session_id)
            self.screen.poll()
            self.screen_panel.refresh(active)
        self.clips.sync()
        self.refresh_event_link()
        self.highlights.refresh()
        self.highlights.setEnabled(not self.session.stale)

    def _selected_event_clip(self):
        table = self.session_panel.table
        event = table.item(table.currentRow(), table.columnCount() - 1)
        if event is None or self.session.stale:
            return None
        matching = sorted((clip for clip in self.clips.clips
                           if clip.session_id == self.session.session_id and event.text() in clip.event_ids),
                          key=lambda clip: (clip.part, clip.clip_id))
        for clip in matching:
            path = playable_path(clip, self.clips.index_path.parent)
            if path is not None:
                return path
        return None

    def refresh_event_link(self):
        self.play_event.setEnabled(self._selected_event_clip() is not None)

    def play_selected_event(self):
        path = self._selected_event_clip()
        if path is not None:
            self.play_clip(path)

    def play_clip(self, path):
        """Play a highlight's clip with the Saved clips player (pauses the content player).

        With the film player, the clip shows in its big video area (ui/clip_stage.py);
        without it (--no-player), under the Saved clips buttons."""
        self.tabs.setCurrentWidget(self.clips)
        self.clips.play_file(path)

    def shutdown(self):
        self.timer.stop()
        self.session_panel.timer.stop()
        if self.camera is not None:
            self.camera.stop_for_session_end()
        if self.recorder is not None:
            self.session.camera_frame = None
            self.recorder.shutdown()
        if self.screen is not None:
            self.screen.turn_off()
        self.clips.player.stop()
        self.session.end()
        self.session.log.close()
