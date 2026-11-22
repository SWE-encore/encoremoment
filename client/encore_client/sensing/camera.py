"""Camera activation and indication control flow (R5, R6). No Qt, no GStreamer.

This module owns the *control* side of the camera, following Submission B
Specifications IV-E (Camera Activation) and IV-F (Camera Indication):

- the camera starts off and opens only on an explicit enable in an active session;
- permission is required before anything is opened;
- the state shown to the user follows device callbacks, not the button;
- disabling, losing the device, or failing to open closes the video admission
  gate first, then asks the media layer to clean up;
- every on/off is reported to the server with ``Session.set_sensor``.

The GStreamer camera, disk ring and clip export implement ``CameraDevice``
in ``clips.recorder``. Tests can substitute a fake device.

Threading: call every method here, including the device callbacks, on the
UI (Qt main) thread, as the player does with its GStreamer bus ``poll()``.
"""
from __future__ import annotations

from typing import Callable, Protocol

from ..clock import monotonic_ms
from .state import CameraController, CameraState

FIRST_FRAME_TIMEOUT_MS = 5_000

# Messages shown to the user next to the indicator.
REASON_NO_SESSION = "Start a session before enabling the camera."
REASON_PERMISSION = "Camera permission denied. The camera stays off."
REASON_NO_FIRST_FRAME = "No video from the camera. The camera was turned off."


class CameraDevice(Protocol):
    """What the camera control flow needs from the media layer.

    Commands (control flow -> media layer):

    - ``permission_granted()``: whether the OS lets this app use the camera.
      Checked before a generation is issued or anything is opened (IV-E).
    - ``open(generation)``: start capture for this generation. Report
      ``on_first_frame`` when a frame has really arrived, or ``on_open_failed``.
    - ``close(generation)``: stop capture and upload, drop unsent video,
      cancel clip jobs and clear unsaved video for this generation (IV-E, IV-F),
      then report ``on_stopped``. Must be safe to call more than once.

    Before capture, enqueue, transmission and final clip commit, the media
    layer checks ``CameraControl.admission_open(generation)`` (IV-E).

    Callbacks (media layer -> control flow), on the UI thread:
    ``on_first_frame(generation, capture_ms)``, ``on_open_failed(generation, reason)``,
    ``on_stopped(generation)``, ``on_device_lost(generation, reason)``.
    """

    def set_listener(self, listener: "CameraControl") -> None: ...
    def permission_granted(self) -> bool: ...
    def open(self, generation: int) -> None: ...
    def close(self, generation: int) -> None: ...


class SensorSink(Protocol):
    """The part of ``Session`` this module uses."""

    def set_sensor(self, sensor: str, enabled: bool, *, generation: int | None = None,
                   capture_ms: int | None = None): ...


class CameraControl:
    """Runs the R5/R6 procedures between the UI, the media layer and the session."""

    def __init__(
        self,
        device: CameraDevice,
        session: SensorSink,
        session_active: Callable[[], bool],
        *,
        clock: Callable[[], int] = monotonic_ms,
        first_frame_timeout_ms: int = FIRST_FRAME_TIMEOUT_MS,
    ) -> None:
        self.device = device
        self.session = session
        self.session_active = session_active
        self.controller = CameraController()
        self.message: str | None = None   # why the camera is off or in error, for the UI
        self._clock = clock
        self._timeout_ms = first_frame_timeout_ms
        self._capture_generation: int | None = None   # generation the device was opened with
        self._admitted_generation: int | None = None  # video admission gate (None = closed)
        self._opened_at_ms: int | None = None
        self._announced_on = False                    # camera-on control sent for this capture
        device.set_listener(self)

    # ---- state for the UI ---------------------------------------------------------

    @property
    def state(self) -> CameraState:
        return self.controller.state

    @property
    def can_enable(self) -> bool:
        return self.session_active() and self.state in (CameraState.OFF, CameraState.ERROR)

    @property
    def show_stop_button(self) -> bool:
        return self.controller.show_stop_button

    @property
    def stop_button_enabled(self) -> bool:
        return self.controller.stop_button_enabled

    def admission_open(self, generation: int) -> bool:
        """The video admission gate. The media layer asks before capture,
        enqueue, transmission and final clip commit (IV-E)."""
        return self._admitted_generation is not None and generation == self._admitted_generation

    # ---- user actions ---------------------------------------------------------------

    def enable(self) -> bool:
        """``enable_camera_clicked`` (IV-E). Returns True if the camera is opening."""
        if not self.session_active():
            self.message = REASON_NO_SESSION
            return False
        if self.state not in (CameraState.OFF, CameraState.ERROR):
            return False
        if not self.device.permission_granted():
            self.message = REASON_PERMISSION   # "Permission denial ... keeps it off."
            return False
        if not self.controller.request_enable(True):
            return False
        self.message = None
        gen = self.controller.generation
        self._capture_generation = gen
        self._admitted_generation = gen
        self._opened_at_ms = self._clock()
        self._announced_on = False
        self.device.open(gen)
        return True

    def disable(self) -> bool:
        """``disable_camera_clicked`` / Stop (IV-E, IV-F). Returns True if stopping."""
        capture_gen = self._capture_generation
        if capture_gen is None or self.state not in (CameraState.STARTING, CameraState.ON):
            return False
        self._admitted_generation = None            # close_video_admission_gate()
        self.controller.request_disable()           # invalidate_camera_generation()
        self.device.close(capture_gen)              # stop upload, drop unsent, cancel clips, clear unsaved
        self._announce_off()                        # send_control_update()
        return True                                 # Off is shown once on_stopped arrives

    def stop_for_session_end(self) -> None:
        """R11: ending the session stops the camera."""
        self.disable()

    def tick(self) -> None:
        """Call on the UI timer: turns a camera that never delivers a frame off."""
        if (self.state is CameraState.STARTING and self._opened_at_ms is not None
                and self._clock() - self._opened_at_ms >= self._timeout_ms):
            self._fail_open(self._capture_generation, REASON_NO_FIRST_FRAME)

    # ---- device callbacks -----------------------------------------------------------

    def on_first_frame(self, generation: int, capture_ms: int | None = None) -> None:
        if generation != self._capture_generation or not self.admission_open(generation):
            return                                  # late frame from an older generation
        self.controller.on_first_frame(generation)
        if self.state is CameraState.ON and not self._announced_on:
            self._announced_on = True
            self.session.set_sensor("camera", True, generation=generation,
                                    capture_ms=self._clock() if capture_ms is None else capture_ms)

    def on_open_failed(self, generation: int, reason: str) -> None:
        if generation == self._capture_generation and self.state is CameraState.STARTING:
            self._fail_open(generation, reason)

    def on_stopped(self, generation: int) -> None:
        if generation == self._capture_generation:
            self.controller.on_stopped()

    def on_device_lost(self, generation: int, reason: str) -> None:
        """``camera_device_lost`` (IV-F)."""
        if generation != self._capture_generation or self.state in (CameraState.OFF, CameraState.ERROR):
            return
        was_stopping = self.state is CameraState.STOPPING
        self._admitted_generation = None            # close_video_admission_gate()
        if not was_stopping:
            self.device.close(generation)           # stop upload, cancel clips, clear unsaved
        self.controller.on_device_lost()            # render_state(ERROR)
        self.message = f"Camera lost: {reason}"
        self._announce_off()

    # ---- helpers --------------------------------------------------------------------

    def _fail_open(self, generation: int | None, reason: str) -> None:
        """IV-E: no first frame -> close_camera(); state = OFF_WITH_ERROR."""
        self._admitted_generation = None
        if generation is not None:
            self.device.close(generation)
        self.controller.on_device_lost()
        self.message = reason
        self._announce_off()

    def _announce_off(self) -> None:
        """Tell the server the camera is off, if it was told it was on."""
        if self._announced_on:
            self._announced_on = False
            self.session.set_sensor("camera", False, generation=self.controller.generation,
                                    capture_ms=self._clock())
