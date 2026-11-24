"""Which desktop capabilities this operating system has.

Every platform decision of the PC app lives here, so the app, its tests and the
README agree. Each function takes an optional ``platform`` (a ``sys.platform``
value) so tests can ask about another OS without patching ``sys.platform``.
"""

from __future__ import annotations

import sys


def _name(platform: str | None) -> str:
    return platform or sys.platform


def desktop_capture(platform: str | None = None) -> bool:
    """The audience-camera recorder (``clips/recorder.py``) uses GStreamer ``v4l2src``: Linux only."""
    return _name(platform).startswith("linux")


def native_microphone_format(platform: str | None = None) -> bool:
    """macOS: Qt's own 16 kHz conversion delivers about 27% too few samples (500 ms of
    audio per ~683 ms, PR #40), so capture the device's native format and resample with soxr."""
    return _name(platform) == "darwin"


def os_label(platform: str | None = None) -> str:
    name = _name(platform)
    if name == "darwin":
        return "macOS"
    if name.startswith("win"):
        return "Windows"
    if name.startswith("linux"):
        return "Linux"
    return name


def camera_unavailable_reason(platform: str | None = None) -> str | None:
    """Why the camera cannot record on this OS, or None where it can."""
    if desktop_capture(platform):
        return None
    return f"not supported on {os_label(platform)} yet; the recorder needs Linux V4L2"
