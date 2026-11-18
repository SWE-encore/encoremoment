"""One workspace for actual session state, events and saved clip review."""
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QLabel, QTabWidget, QVBoxLayout, QWidget
from ..clips.panel import ClipPanel
from ..session.panel import SessionPanel
from ..session.session import event_rows
from .formatting import format_ms


class Workspace(QWidget):
    def __init__(self, session, driver, index_path, *, require_anchor=False, demo=False):
        super().__init__()
        self.setMinimumWidth(540)
        self.session = session
        layout = QVBoxLayout(self)
        self.sensing = QLabel("Camera: Off — capture unavailable\nMicrophone: Off — capture unavailable")
        layout.addWidget(self.sensing)
        if demo:
            label = QLabel("Demo: prerecorded contract messages; no camera, microphone or AI inference.")
            label.setWordWrap(True)
            layout.addWidget(label)
        self.session_panel = SessionPanel(session, driver, require_anchor=require_anchor)
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

    def refresh(self):
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
        self.clips.player.stop()
        self.session.end()
        self.session.log.close()
