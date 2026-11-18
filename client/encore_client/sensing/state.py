"""Sensing state model (Qt-free, so it can be unit-tested).

Requirements 5, 6, 9:
  - The camera is off by default and is enabled only by explicit user action during a session.
  - The indicator follows the actual device state, not the requested switch position.
  - Starting / Camera on / Stopping / Off / Error are distinguished.
  - The Stop control stays visible until capture has fully stopped.
"""
from __future__ import annotations

from enum import Enum


class CameraState(str, Enum):
    OFF = "off"
    STARTING = "starting"
    ON = "on"
    STOPPING = "stopping"
    ERROR = "error"


class ConnectionState(str, Enum):
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    DISCONNECTED = "disconnected"


CAMERA_TEXT = {
    CameraState.OFF: "⚪ Camera off",
    CameraState.STARTING: "🟡 Camera starting…",
    CameraState.ON: "🔴 Camera on (recording)",
    CameraState.STOPPING: "🟡 Camera stopping…",
    CameraState.ERROR: "⚠️ Camera error (off)",
}

CONNECTION_TEXT = {
    ConnectionState.CONNECTED: "🟢 Server connected",
    ConnectionState.RECONNECTING: "🟡 Reconnecting…",
    ConnectionState.DISCONNECTED: "🔴 Server disconnected (live results delayed)",
}


class CameraController:
    """Camera state machine. State changes only through device callbacks
    (on_first_frame, on_stopped, on_device_lost)."""

    def __init__(self) -> None:
        self.state = CameraState.OFF   # Requirement 5: off by default
        self.generation = 0            # Incremented on each enable; late results from older generations are ignored

    def request_enable(self, session_active: bool) -> bool:
        if not session_active or self.state not in (CameraState.OFF, CameraState.ERROR):
            return False
        self.generation += 1
        self.state = CameraState.STARTING
        return True

    def request_disable(self) -> bool:
        if self.state not in (CameraState.STARTING, CameraState.ON):
            return False
        self.state = CameraState.STOPPING
        return True

    # ---- Actual device events (from GStreamer) ----
    def on_first_frame(self, generation: int) -> None:
        if generation == self.generation and self.state == CameraState.STARTING:
            self.state = CameraState.ON

    def on_stopped(self) -> None:
        if self.state == CameraState.STOPPING:
            self.state = CameraState.OFF

    def on_device_lost(self) -> None:
        if self.state != CameraState.OFF:
            self.state = CameraState.ERROR

    @property
    def show_stop_button(self) -> bool:
        """Keep the Stop control visible until capture fully stops (Requirement 6)."""
        return self.state in (CameraState.STARTING, CameraState.ON, CameraState.STOPPING)

    @property
    def stop_button_enabled(self) -> bool:
        return self.state in (CameraState.STARTING, CameraState.ON)
