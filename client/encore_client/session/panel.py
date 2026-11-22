"""PySide6 panel: session Start/End, connection status and received events."""

from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..ui.formatting import format_ms
from .session import STARTED, Conn, ScenarioDriver, Session, event_rows

POLL_MS = 100
COLUMNS = ("Time", "Reaction", "Phase", "Rev", "Intensity", "Confidence", "Event ID")
STATUS = {
    Conn.IDLE: ("#888888", "Not started"),
    Conn.CONNECTING: ("#c28a00", "Connecting…"),
    Conn.CONNECTED: ("#1a8f3c", "Connected"),
    Conn.DISCONNECTED: ("#c62828", "Disconnected"),
    Conn.RECONNECTING: ("#c28a00", "Reconnecting…"),
    Conn.ENDING: ("#c28a00", "Ending…"),
    Conn.ENDED: ("#888888", "Session ended"),
}


class SessionPanel(QWidget):
    def __init__(self, session: Session, driver: ScenarioDriver | None = None, parent=None,
                 require_anchor: bool = False) -> None:
        super().__init__(parent)
        self.session = session
        self.driver = driver
        self.require_anchor = require_anchor
        self._shown_rows: list[dict] | None = None

        self.start_btn = QPushButton("Start session")
        self.end_btn = QPushButton("End session")
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.counts = QLabel()
        self.counts.setWordWrap(True)

        buttons = QHBoxLayout()
        buttons.addWidget(self.start_btn)
        buttons.addWidget(self.end_btn)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"Session <b>{session.session_id}</b>"))
        layout.addLayout(buttons)
        layout.addWidget(self.status)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.counts)

        self.start_btn.clicked.connect(self.start)
        self.end_btn.clicked.connect(self.end)
        self.timer = QTimer(self)
        self.timer.setInterval(POLL_MS)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        self.refresh()

    def start(self) -> None:
        if self.require_anchor and self.session.last_anchor is None:
            return
        self.session.start()
        self.refresh()

    def end(self) -> None:
        self.session.finish()  # waits for the server to close open reactions (R11)
        self.refresh()

    def tick(self) -> None:
        self.session.poll()
        if self.driver is not None:
            self.driver.pump()
        self.refresh()

    def refresh(self) -> None:
        s = self.session
        ready = not self.require_anchor or s.last_anchor is not None
        self.start_btn.setEnabled(s.state is Conn.IDLE and ready)
        self.start_btn.setToolTip("Open a video before starting." if not ready else "")
        self.end_btn.setEnabled(s.state in STARTED)
        self.status.setText(f'<span style="color:{STATUS[s.state][0]}; font-weight:600">● {status_text(s)}</span>')

        rows = event_rows(s.view)
        if rows != self._shown_rows:
            self._shown_rows = rows
            self.table.setRowCount(len(rows))
            for r, row in enumerate(rows):
                when = format_ms(row["timecode_ms"]) if row["timecode_ms"] is not None else "pending"
                cells = (
                    when,
                    row["type"],
                    row["phase"],
                    str(row["revision"]),
                    _num(row["intensity"]),
                    _num(row["confidence"]),
                    row["event_id"],
                )
                for c, value in enumerate(cells):
                    self.table.setItem(r, c, QTableWidgetItem(value))
        self.table.setEnabled(not s.stale)

        summary = s.view.summary()
        self.counts.setText(
            f"{len(rows)} event(s) · received {s.received} · sent {s.sent}"
            + (f" · unsent {len(s.unsent)}" if s.unsent else "")
            + (f" · audio chunks {s.audio_sent}" if s.audio_sent or s.audio_dropped else "")
            + (f" ({s.audio_dropped} dropped)" if s.audio_dropped else "")
            + (f" · ignored {summary['ignored']}" if summary["ignored"] else "")
            + (f" · rejected {summary['rejected']}" if summary["rejected"] else "")
        )


def status_text(s: Session) -> str:
    """The connection line. A drop is never shown as recovered until the stream is open again (R11)."""
    text = STATUS[s.state][1]
    if s.state is Conn.DISCONNECTED:
        text += f": {s.reason}."
        if s.retry_in_ms is not None:
            text += f" Reconnecting in {s.retry_in_ms / 1000:.0f} s (attempt {s.attempt + 1})."
        text += " Results below may be out of date."
    elif s.state is Conn.RECONNECTING:
        text += f" (attempt {s.attempt}). Results below may be out of date."
    elif s.state is Conn.ENDING:
        n = len(s.open_events)
        text += (f" waiting for the server to close {n} open reaction(s)" if n
                 else " waiting for the server to confirm")
    elif s.state is Conn.ENDED and s.end_settled is False:
        text += ": the server did not confirm in time; a reaction in progress at End may be incomplete"
    elif s.state is Conn.CONNECTED and s.reconnects:
        text += (f" — reconnected after {s.reconnects} drop(s); "
                 "audio captured while disconnected was not analysed")
    return text


def _num(value) -> str:
    return "—" if value is None else f"{value:.2f}"
