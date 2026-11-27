"""The Screen switch under the camera control (clips/screen.py).

Off by default. It can be turned on only while a session runs, and End turns
it off. While on, the indicator is red: the whole screen is being recorded,
including this window, notifications and anything else on it.
"""
from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

OFF = "Screen: Off"
ON = "● Screen: Recording the whole screen"


class ScreenPanel(QWidget):
    def __init__(self, recorder, unavailable_reason: str | None = None, parent=None) -> None:
        super().__init__(parent)
        self.recorder = recorder
        self.unavailable_reason = unavailable_reason
        self.indicator = QLabel()
        self.detail = QLabel()
        self.detail.setWordWrap(True)
        self.toggle_btn = QPushButton()
        self.toggle_btn.clicked.connect(self.toggle)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.indicator, 1)
        row.addWidget(self.toggle_btn)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(row)
        layout.addWidget(self.detail)
        self.session_active = False
        self.refresh(False)

    def toggle(self) -> None:
        if self.recorder.on:
            self.recorder.turn_off()
        elif self.session_active and self.unavailable_reason is None:
            self.recorder.turn_on()
        self.refresh(self.session_active)

    def refresh(self, session_active: bool) -> None:
        self.session_active = session_active
        on = self.recorder.on
        self.indicator.setText(ON if on else OFF)
        self.indicator.setStyleSheet(f"color: {'#c62828' if on else '#888888'}; font-weight: 600")
        self.toggle_btn.setText("■ Stop screen recording" if on else "Record screen")
        self.toggle_btn.setEnabled(on or (session_active and self.unavailable_reason is None))
        if self.unavailable_reason is not None:
            text = f"Screen recording unavailable: {self.unavailable_reason}."
        elif self.recorder.error:
            text = self.recorder.error
        elif on:
            text = "Each saved reaction clip gets the screen of the same moments (Screen tab)."
        else:
            text = "" if session_active else "Start a session to record the screen."
        self.detail.setText(text)
        self.detail.setVisible(bool(text))
