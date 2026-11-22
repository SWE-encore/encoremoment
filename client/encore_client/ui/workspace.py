"""One workspace for actual session state, events and saved clip review."""
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QLabel, QTabWidget, QVBoxLayout, QWidget
from ..clips.deletion import DeletionManager
from ..clips.panel import ClipPanel
from ..sensing.camera import CameraControl
from ..session.panel import SessionPanel
from ..session.session import Conn
from .camera_panel import CameraPanel
from .highlights import HighlightsPanel

ACTIVE = (Conn.CONNECTING, Conn.CONNECTED, Conn.DISCONNECTED)


class Workspace(QWidget):
    def __init__(self, session, driver, index_path, *, require_anchor=False, demo=False,
                 camera_device=None):
        super().__init__()
        self.setMinimumWidth(540)
        self.session = session
        self.microphone = None
        layout = QVBoxLayout(self)
        # Camera (R5, R6): real state from the media layer's device, or "unavailable".
        self.camera = (CameraControl(camera_device, session, lambda: session.state in ACTIVE)
                       if camera_device is not None else None)
        self.camera_panel = CameraPanel(self.camera)
        layout.addWidget(self.camera_panel)
        self.sensing = QLabel("Microphone: Off — capture unavailable")
        layout.addWidget(self.sensing)
        if demo:
            label = QLabel("Demo: prerecorded contract messages; no camera, microphone or AI inference.")
            label.setWordWrap(True)
            layout.addWidget(label)
        self.session_panel = SessionPanel(session, driver, require_anchor=require_anchor)
        # End stops the camera first, so its off control goes out before the stream closes (R11).
        self.session_panel.end_btn.clicked.disconnect()
        self.session_panel.end_btn.clicked.connect(self.end_session)
        layout.addWidget(self.session_panel, 2)
        self.tabs = QTabWidget()
        # Tonight's Highlights (R4): FINAL events by intensity, linked to saved clips.
        # Clip deletion (R7): journal and tombstones next to the clip index. The
        # server has no cleanup API yet, so only local cleanup is required.
        self.deletions = DeletionManager(index_path)
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
        self.session_panel.end()
        self.refresh()

    def refresh(self):
        if self.camera is not None and self.session.state not in ACTIVE:
            self.camera.stop_for_session_end()   # the session ended or dropped out of Start
        self.camera_panel.refresh()
        if self.microphone is not None:
            self.microphone.refresh()
        self.deletions.tick()
        self.clips.sync()
        self.highlights.refresh()
        self.highlights.setEnabled(not self.session.stale)

    def play_clip(self, path):
        """Play a highlight's clip in the Saved clips player (pauses the content player)."""
        self.tabs.setCurrentWidget(self.clips)
        self.clips.play_file(path)

    def shutdown(self):
        self.timer.stop()
        self.session_panel.timer.stop()
        if self.camera is not None:
            self.camera.stop_for_session_end()
        self.clips.player.stop()
        self.session.end()
        self.session.log.close()
