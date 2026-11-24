"""Camera indicator and controls (R5, R6).

The indicator shows the actual camera state reported by the media layer
(Starting, Camera on, Stopping, Off, Error), never the button position.
Stop stays visible until capture has fully stopped. The panel sits above the
tabs, so it stays visible while switching between views (IV-F).
"""
from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from ..sensing.camera import CameraControl
from ..sensing.state import CAMERA_TEXT, CameraState

UNAVAILABLE_TEXT = "Camera: Off — capture unavailable"

COLOR = {
    CameraState.OFF: "#888888",
    CameraState.STARTING: "#c28a00",
    CameraState.ON: "#c62828",
    CameraState.STOPPING: "#c28a00",
    CameraState.ERROR: "#c62828",
}


class CameraPanel(QWidget):
    def __init__(self, camera: CameraControl | None, parent=None) -> None:
        super().__init__(parent)
        self.camera = camera
        self.unavailable_reason: str | None = None
        self.indicator = QLabel()
        self.detail = QLabel()
        self.detail.setWordWrap(True)
        self.enable_btn = QPushButton("Enable camera")
        self.stop_btn = QPushButton("■ Stop camera")
        self.enable_btn.clicked.connect(self.enable)
        self.stop_btn.clicked.connect(self.stop)

        row = QHBoxLayout()
        row.addWidget(self.indicator, 1)
        row.addWidget(self.enable_btn)
        row.addWidget(self.stop_btn)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(row)
        layout.addWidget(self.detail)
        self.refresh()

    def set_unavailable_reason(self, reason: str | None) -> None:
        """Say why there is no camera (e.g. the OS has no recorder) instead of only 'unavailable'."""
        self.unavailable_reason = reason
        self.refresh()

    def enable(self) -> None:
        if self.camera is not None:
            self.camera.enable()
        self.refresh()

    def stop(self) -> None:
        if self.camera is not None:
            self.camera.disable()
        self.refresh()

    def refresh(self) -> None:
        cam = self.camera
        if cam is None:
            # No media-layer camera device yet: say so instead of pretending.
            self.indicator.setText(UNAVAILABLE_TEXT + (f" ({self.unavailable_reason})"
                                                        if self.unavailable_reason else ""))
            self.enable_btn.setVisible(False)
            self.stop_btn.setVisible(False)
            self.detail.setVisible(False)
            return
        cam.tick()
        state = cam.state
        self.indicator.setText(
            f'<span style="color:{COLOR[state]}; font-weight:600">{CAMERA_TEXT[state]}</span>')
        self.enable_btn.setVisible(state in (CameraState.OFF, CameraState.ERROR))
        self.enable_btn.setEnabled(cam.can_enable)
        self.enable_btn.setToolTip("" if cam.session_active() else "Start a session first.")
        self.stop_btn.setVisible(cam.show_stop_button)
        self.stop_btn.setEnabled(cam.stop_button_enabled)
        self.detail.setText(cam.message or "")
        self.detail.setVisible(bool(cam.message))
