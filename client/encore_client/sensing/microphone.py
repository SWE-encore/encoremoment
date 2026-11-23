"""The user's microphone switch (R8, Jira EM-17). Qt-free, so it can be unit-tested.

Turning the microphone off during a session

  - stops reading the device (the capture device is closed, not just ignored),
  - stops the audio upload: ``Session.send_audio`` refuses while the sensor is off,
  - sends ``control_update`` with a new generation, so the PC and the server
    refuse *new* audio events from the old generation (contract receiver
    rules 6 and 8); an event that was already open may still be closed.

Turning it on again starts a new generation. The camera is never touched.
The device follows the session's microphone sensor, so the microphone that
Start turns on (``Session(microphone_on_start=True)``) also opens the device.
A new session after End (``use_session``) starts with the device closed and
its own generations (R11).
"""
from __future__ import annotations

from typing import Protocol

from ..session.session import STARTED, Conn


class CaptureDevice(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...


ACTIVE = STARTED  # started, not ended


class MicrophoneSwitch:
    def __init__(self, session, device: CaptureDevice | None = None, *, source: str = "microphone") -> None:
        """``device``: the capture device to open and close (``QtAudioInput``);
        None for a stand-in such as ``WavMicrophone``, which already stops
        sending while the sensor is off. ``source`` names it in the status text.
        """
        self.session = session
        self.device = device
        self.source = source
        self.device_running = False
        self.error: str | None = None

    # ---- state -----------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self.session.view.sensors["microphone"].enabled

    @property
    def generation(self) -> int:
        return self.session.view.sensors["microphone"].generation

    @property
    def can_toggle(self) -> bool:
        return self.session.state in ACTIVE

    def status_text(self) -> str:
        if self.error:
            return f"⚠️ Microphone error: {self.error} (off)"
        if self.session.state is Conn.IDLE:
            return "Microphone: off until Start"
        if self.session.state in (Conn.ENDING, Conn.ENDED):
            return "Microphone: off — session ended"
        if self.enabled:
            reading = "reading the device" if self.device_running or self.device is None else "starting…"
            return (f"🎙️ Microphone on — {self.source}, {reading}, sending audio "
                    f"(generation {self.generation})")
        return ("🔇 Microphone off — nothing is read or sent; new sound events are refused "
                f"(generation {self.generation})")

    def button_text(self) -> str:
        return "Turn microphone off" if self.enabled else "Turn microphone on"

    # ---- user actions -------------------------------------------------------------

    def toggle(self) -> bool:
        return self.turn_off() if self.enabled else self.turn_on()

    def turn_off(self) -> bool:
        """Close the device first, then tell the server (new generation)."""
        if not self.enabled or not self.can_toggle:
            return False
        self._stop_device()
        self.session.set_sensor("microphone", False)
        return True

    def turn_on(self) -> bool:
        """Open the device, then announce the new generation. A device that
        cannot open leaves the microphone off and shows the error."""
        if self.enabled or not self.can_toggle:
            return False
        if not self._start_device():
            return False
        self.session.set_sensor("microphone", True)
        return True

    def use_session(self, session) -> None:
        """Follow a new session after End (R11). The device is closed until
        that session turns its microphone on; an old device error is cleared."""
        self._stop_device()
        self.session = session
        self.error = None

    def sync(self) -> None:
        """Keep the device in step with the sensor and the session (call on a timer).

        Start turns the microphone on without this switch; End and errors turn
        it off. The device is open exactly while the session is active and
        the microphone is on.
        """
        want = self.enabled and self.can_toggle
        if want and not self.device_running and self.error is None:
            if not self._start_device():
                self.session.set_sensor("microphone", False)
        elif not want and self.device_running:
            self._stop_device()

    # ---- device -------------------------------------------------------------------

    def _start_device(self) -> bool:
        self.error = None
        if self.device is None:
            self.device_running = True
            return True
        try:
            self.device.start()
        except RuntimeError as exc:
            self.error = str(exc)
            self.device_running = False
            return False
        self.device_running = True
        return True

    def _stop_device(self) -> None:
        if self.device is not None and self.device_running:
            self.device.stop()
        self.device_running = False
