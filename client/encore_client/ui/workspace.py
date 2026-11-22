"""One workspace for actual session state, events and saved clip review."""
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QLabel, QPushButton, QTabWidget, QVBoxLayout, QWidget
from ..clips.deletion import DeletionManager
from ..clips.model import playable_path
from ..clips.panel import ClipPanel
from ..sensing.camera import CameraControl
from ..session.panel import SessionPanel
from ..session.session import STARTED
from .camera_panel import CameraPanel
from .highlights import HighlightsPanel

ACTIVE = STARTED  # Start pressed, End not: a drop does not stop the devices


class Workspace(QWidget):
    def __init__(self, session, driver, index_path, *, require_anchor=False, demo=False,
                 camera_device=None, recorder_factory=None):
        super().__init__()
        self.setMinimumWidth(540)
        self.session = session
        self.microphone = None
        self.deletions = DeletionManager(index_path)
        self.recorder = None
        self.live_microphone = None
        if recorder_factory is not None:
            self.recorder = recorder_factory(index_path, session, may_commit=self.deletions.may_commit_export)
            camera_device = self.recorder
            session.camera_frame = self.recorder.latest_face
        layout = QVBoxLayout(self)
        # Camera (R5, R6): real state from the media layer's device, or "unavailable".
        self.camera = (CameraControl(camera_device, session, lambda: session.state in ACTIVE)
                       if camera_device is not None else None)
        self.camera_panel = CameraPanel(self.camera)
        layout.addWidget(self.camera_panel)
        self.sensing = QLabel("Microphone: Off — capture unavailable")
        layout.addWidget(self.sensing)
        self.aec_label = QLabel()
        self.aec_label.hide()
        layout.addWidget(self.aec_label)
        self.recording_error = QLabel()
        self.recording_error.setWordWrap(True)
        self.recording_error.hide()
        layout.addWidget(self.recording_error)
        if demo:
            label = QLabel("Demo: prerecorded contract messages; no camera, microphone or AI inference.")
            label.setWordWrap(True)
            layout.addWidget(label)
        self.session_panel = SessionPanel(session, driver, require_anchor=require_anchor)
        # End stops the camera and the microphone first, so their off controls go out
        # and the server closes open reactions before the stream closes (R11).
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

    def add_microphone(self, switch):
        """Show the microphone on/off control under the sensing line (R8)."""
        from .microphone import MicrophoneControl
        self.microphone = MicrophoneControl(switch)
        layout = self.layout()
        layout.insertWidget(layout.indexOf(self.sensing) + 1, self.microphone)

    def end_session(self):
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
        if self.live_microphone is not None:
            self.aec_label.setText(f"Echo cancellation: {self.live_microphone.aec_status}")
            self.aec_label.setVisible(True)
        self.deletions.tick()
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
        """Play a highlight's clip in the Saved clips player (pauses the content player)."""
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
        self.clips.player.stop()
        self.session.end()
        self.session.log.close()
