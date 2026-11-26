"""The user's echo cancellation choice and what the PC actually does (R12, Jira EM-21).

R12 asks for three things on screen: the mode the user asked for, the mode that
is in effect, and whether the playback reference is missing, kept apart from the
microphone's own on/off state (R8). Qt-free, so it is unit-tested without a window.

- **Requested** is the user's switch. It is sent to the server as a
  ``control_update`` for the ``aec`` sensor; that sensor's generation is the
  *mode revision*, so every change is numbered and the server can tell which
  audio came before or after it. The choice is kept across sessions (R11): a new
  session starts with the mode the user last chose, announced again at Start.
- **Effective** is what the last microphone block really got, from
  ``LiveMicrophone.aec_status`` (the same value each ``audio_chunk`` carries and
  the server copies into ``event.source.aec``). It can differ from the request:
  no playback reference yet, the filter still warming up, the microphone off.
- Turning cancellation off or on resets the canceller (R8: re-enabling starts a
  new generation and resets AEC state), so it warms up again rather than resuming
  with a filter learnt before the gap. The microphone generation does not change:
  sensing and events go on through the switch.
"""
from __future__ import annotations

from ..session.session import STARTED, Conn

# Effective status (contract `aec` enum) -> what the user reads.
EFFECTIVE_TEXT = {
    "active": "on, removing the film's sound",
    "warming_up": "on, learning the room (about 0.5 s)",
    "reference_unavailable": "not removing anything — no playback reference "
                             "(paused, seeking, or not 1x speed); audio is analysed as captured",
    "uncancelled": "switching — this audio was not fully cancelled",
    "off": "off — audio is analysed as captured",
}


class AecMode:
    def __init__(self, session, mic, *, requested: bool = True) -> None:
        """``mic``: the ``LiveMicrophone`` whose blocks are cancelled.
        Without a controlled player there is nothing to cancel against, so the
        mode is shown as unavailable and cannot be switched."""
        self.session = session
        self.mic = mic
        self.requested = requested
        mic.aec_requested = requested

    # ---- state -----------------------------------------------------------------

    @property
    def available(self) -> bool:
        return self.mic.player is not None

    @property
    def revision(self) -> int:
        """The ``aec`` sensor's generation: +1 for every change sent."""
        return self.session.view.sensors["aec"].generation

    @property
    def can_toggle(self) -> bool:
        return self.available and self.session.state in STARTED

    @property
    def effective(self) -> str:
        """The contract status of the newest microphone block."""
        if not self.available or not self.session.view.sensors["microphone"].enabled:
            return "off"
        return self.mic.aec_status

    def status_text(self) -> str:
        if not self.available:
            return "Echo cancellation: unavailable — no controlled player in this run"
        requested = "on" if self.requested else "off"
        head = f"Echo cancellation — requested {requested}"
        if self.session.state is Conn.IDLE:
            return f"{head}; applies from Start"
        if self.session.state in (Conn.ENDING, Conn.ENDED):
            return f"{head}; session ended"
        if not self.session.view.sensors["microphone"].enabled:
            return f"{head}; effective: none while the microphone is off (revision {self.revision})"
        effective = self.effective
        text = EFFECTIVE_TEXT.get(effective, effective)
        mismatch = "" if (effective in ("active", "warming_up")) == self.requested else " ⚠️"
        if not self.requested and effective == "off":
            mismatch = ""
        return f"{head}; effective: {text}{mismatch} (revision {self.revision})"

    def button_text(self) -> str:
        return "Turn echo cancellation off" if self.requested else "Turn echo cancellation on"

    # ---- user actions -------------------------------------------------------------

    def toggle(self) -> bool:
        if not self.can_toggle:
            return False
        self.requested = not self.requested
        self.mic.aec_requested = self.requested
        self.mic.player.reset_echo()      # on again = warm up again, never an old filter
        self.mic.aec_status = "reference_unavailable" if self.requested else "off"
        self.sync()
        return True

    def use_session(self, session) -> None:
        """A new session after End (R11): same choice, announced again at its Start."""
        self.session = session

    def sync(self) -> None:
        """Announce the requested mode once the session runs (call on a timer).

        The server's ``aec`` sensor starts off at generation 0, so Start with
        cancellation requested sends one ``control_update``; every change after
        that sends another with the next revision.
        """
        if not self.available or self.session.state not in STARTED:
            return
        if self.session.view.sensors["aec"].enabled != self.requested:
            self.session.set_sensor("aec", self.requested)
