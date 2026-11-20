"""One workspace for actual session state, events and saved clip review."""
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QLabel, QTabWidget, QVBoxLayout, QWidget
from ..clips.panel import ClipPanel
from ..sensing.camera import CameraControl
from ..session.panel import SessionPanel
from ..session.session import Conn, event_rows
from .camera_panel import CameraPanel
from .formatting import format_ms

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
        self.highlights = QLabel("No finalized reactions yet.")
        self.highlights.setWordWrap(True)
        self.tabs.addTab(self.highlights, "Tonight's Highlights")
        self.clips = ClipPanel(index_path)
        self.tabs.addTab(self.clips, "Saved clips")
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
        rows = [row for row in event_rows(self.session.view) if row["phase"] == "FINAL"]
        rows.sort(key=lambda row: (-(row["intensity"] if row["intensity"] is not None else -1),
                                   row["trigger_ms"], row["event_id"]))
        self.highlights.setText("\n".join(
            f"{row['type']} · intensity {row['intensity']} · "
            f"{format_ms(row['timecode_ms'])} · {row['event_id']} · no exported clip linked"
            for row in rows) or "No finalized reactions yet.")
        self.highlights.setEnabled(not self.session.stale)

    def shutdown(self):
        self.timer.stop()
        self.session_panel.timer.stop()
        if self.camera is not None:
            self.camera.stop_for_session_end()
        self.clips.player.stop()
        self.session.end()
        self.session.log.close()
