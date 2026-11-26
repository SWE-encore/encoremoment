"""EM-21 / R12: the user chooses echo cancellation and sees what is in effect.

Acceptance (Jira EM-21):
1. AEC on/off can be chosen; the requested mode, the effective mode and a
   missing playback reference are shown, apart from microphone sensing.
2. The same input in both modes is recorded with its mode revision, and the
   session and its events keep working through a switch.

No Qt, network, audio device or GStreamer: a fake transport and a fake player.
The real player and canceller are covered by test_echo_capture.py (CI, Ubuntu).
"""

import json
import math
import os
import struct
import sys
from pathlib import Path

import pytest

from encore_client.sensing.aec_mode import AecMode
from encore_client.sensing.live_mic import LiveMicrophone
from encore_client.sensing.microphone import MicrophoneSwitch
from encore_client.session.contract import decode_audio_frame
from encore_client.session.session import Conn, Session

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

SID = "ses-aec-mode"
CHUNK = 16_000  # bytes in one 0.5 s, 16 kHz PCM16 chunk


class FakeTransport:
    def __init__(self, url):
        self.pending, self.sent = [], []

    def start(self):
        pass

    def send(self, data):
        if isinstance(data, str):
            self.sent.append(json.loads(data))
        else:
            header, _ = decode_audio_frame(data)
            self.sent.append(header)
        return True

    def poll(self):
        out, self.pending = self.pending, []
        return out

    def close(self):
        pass

    def controls(self, sensor="aec"):
        return [(m["enabled"], m["generation"]) for m in self.sent
                if m["kind"] == "control_update" and m["sensor"] == sensor]

    def chunk_aec(self):
        return [m["aec"] for m in self.sent if m["kind"] == "audio_chunk"]


class FakePlayer:
    """Cancels by halving, so a test can tell cancelled audio from raw."""

    def __init__(self, status="active"):
        self.status, self.calls, self.resets = status, 0, 0

    def cancel_echo(self, pcm, capture_ms):
        self.calls += 1
        samples = struct.unpack(f"<{len(pcm) // 2}h", pcm)
        return struct.pack(f"<{len(samples)}h", *(s // 2 for s in samples)), self.status

    def reset_echo(self):
        self.resets += 1


class FakeDevice:
    def start(self):
        pass

    def stop(self):
        pass


def setup(player=None, *, start=True):
    transports = []

    def factory(url):
        transports.append(FakeTransport(url))
        return transports[-1]

    session = Session("ws://test", SID, transport_factory=factory, clock=lambda: 10_000,
                      microphone_on_start=True)
    mic = LiveMicrophone(session, player=player, clock=lambda: 10_000)
    mode = AecMode(session, mic)
    switch = MicrophoneSwitch(session, FakeDevice())
    if start:
        session.start()
        transports[0].pending.append(("open", None))
        session.poll()
        assert session.state is Conn.CONNECTED
        switch.sync()
        mode.sync()                  # what the window's timer does
    return session, mic, mode, switch, (transports[0] if transports else None)


# ---- 1. choose, and see requested vs effective ------------------------------------------

def test_start_announces_the_requested_mode_once_as_revision_1():
    session, mic, mode, _, transport = setup(FakePlayer())
    assert transport.controls() == [(True, 1)]
    mode.sync()
    mode.sync()
    assert transport.controls() == [(True, 1)] and mode.revision == 1


def test_before_start_nothing_is_sent_and_the_switch_waits():
    session, mic, mode, _, _ = setup(FakePlayer(), start=False)
    assert not mode.can_toggle and not mode.toggle()
    assert mode.status_text() == "Echo cancellation — requested on; applies from Start"


def test_turning_it_off_and_on_numbers_each_change_and_resets_the_canceller():
    player = FakePlayer()
    session, mic, mode, _, transport = setup(player)
    mic_generation = session.view.sensors["microphone"].generation
    assert mode.toggle()
    assert (mode.requested, mic.aec_requested, mode.revision) == (False, False, 2)
    assert mode.button_text() == "Turn echo cancellation on"
    assert mode.toggle()
    assert transport.controls() == [(True, 1), (False, 2), (True, 3)]
    assert player.resets == 2                       # on again warms up again
    # The switch is not the microphone: sensing and its generation go on.
    assert session.view.sensors["microphone"].generation == mic_generation
    assert session.view.sensors["microphone"].enabled
    assert transport.controls("microphone") == [(True, 1)]


def test_off_passes_the_capture_unchanged_and_never_calls_the_player():
    player = FakePlayer()
    session, mic, mode, _, _ = setup(player)
    block = struct.pack("<480h", *([1000] * 480))
    cleaned, status = mic.cancel_echo(block, 9_990, True)
    assert (status, player.calls) == ("active", 1) and cleaned != block
    mode.toggle()
    assert mic.cancel_echo(block, 10_000, True) == (block, "off")
    assert player.calls == 1


def test_the_status_line_shows_requested_effective_and_a_missing_reference():
    player = FakePlayer()
    session, mic, mode, switch, _ = setup(player)
    mic.aec_status = "active"
    assert mode.status_text() == ("Echo cancellation — requested on; "
                                  "effective: on, removing the film's sound (revision 1)")
    mic.aec_status = "reference_unavailable"
    text = mode.status_text()
    assert "requested on" in text and "no playback reference" in text and text.endswith("⚠️ (revision 1)")
    mode.toggle()
    assert mode.status_text() == ("Echo cancellation — requested off; "
                                  "effective: off — audio is analysed as captured (revision 2)")


def test_with_the_microphone_off_the_line_says_so_instead_of_an_aec_state():
    session, mic, mode, switch, _ = setup(FakePlayer())
    switch.turn_off()
    assert mode.effective == "off"
    assert mode.status_text().endswith("effective: none while the microphone is off (revision 1)")
    assert "Microphone off" in switch.status_text()  # the microphone line is separate


def test_without_a_player_the_mode_is_unavailable_and_nothing_is_sent():
    session, mic, mode, _, transport = setup(None)
    assert not mode.available and not mode.can_toggle and not mode.toggle()
    assert mode.status_text() == "Echo cancellation: unavailable — no controlled player in this run"
    assert transport.controls() == []


def test_a_new_session_keeps_the_choice_and_announces_it_again():
    player = FakePlayer()
    session, mic, mode, _, _ = setup(player)
    mode.toggle()                                   # off
    mode.toggle()                                   # on again, revision 3
    session.end()
    transports = []
    fresh = Session("ws://test", "ses-aec-mode-2", transport_factory=lambda url: transports.append(
        FakeTransport(url)) or transports[-1], clock=lambda: 20_000, microphone_on_start=True)
    mode.use_session(fresh)
    mode.sync()
    assert transports == []                          # not started: nothing goes out
    fresh.start()
    transports[0].pending.append(("open", None))
    fresh.poll()
    mode.sync()
    assert transports[0].controls() == [(True, 1)] and mode.requested


# ---- 2. the same input in both modes, through a switch ---------------------------------

def tone(n_chunks, amp=0.5):
    samples = [round(amp * 32767 * math.sin(2 * math.pi * 440 * i / 16_000)) for i in range(8000)]
    return struct.pack("<8000h", *samples) * n_chunks


def test_chunks_carry_the_mode_and_one_spanning_the_switch_is_uncancelled():
    session, mic, mode, _, transport = setup(FakePlayer())
    pcm = tone(1)
    t = 10_000
    for status in ("active", "active"):
        t += 500
        mic.feed(pcm, end_ms=t, aec=status)
    t += 250
    mic.feed(pcm[: CHUNK // 2], end_ms=t, aec="active")
    mode.toggle()                                   # the user turns it off mid-chunk
    for _ in range(3):
        t += 250
        mic.feed(pcm[: CHUNK // 2], end_ms=t, aec="off")
    assert transport.chunk_aec() == ["active", "active", "uncancelled", "off"]
    # Every chunk stays in the microphone generation the switch did not touch.
    gens = {m["generation"] for m in transport.sent if m["kind"] == "audio_chunk"}
    assert gens == {session.view.sensors["microphone"].generation}


def _server_run(switch_at=None):
    """One loud reaction through the real ServerSession, optionally turning AEC off
    in the middle of it. Returns (acks, final events)."""
    from encore_server.models import LoudnessModel
    from encore_server.session import ServerSession
    from encore_contract import encode_audio_frame

    server = ServerSession(SID, LoudnessModel())
    seq = iter(range(1000))
    out = []

    def control(sensor, enabled, generation, at):
        out.extend(server.on_text(json.dumps({
            "schema": "encore.v1", "kind": "control_update", "session_id": SID, "seq": next(seq),
            "capture_ms": at, "idempotency_key": f"ctl-{sensor}-{generation}",
            "sensor": sensor, "enabled": enabled, "generation": generation})))

    start = 50_000
    control("microphone", True, 1, start)
    control("aec", True, 1, start)
    out.extend(server.on_text(json.dumps({
        "schema": "encore.v1", "kind": "playback_anchor", "session_id": SID, "seq": next(seq),
        "capture_ms": start, "playback": {"epoch": 1, "content_id": "file:test", "title": "Test",
                                          "position_ms": 0, "rate": 1.0, "state": "playing"}})))
    quiet, loud = bytes(CHUNK), tone(1)
    aec = "active"
    for i, pcm in enumerate([quiet] * 6 + [loud] * 4 + [quiet] * 8):
        at = start + 500 * i
        if switch_at is not None and i == switch_at:
            control("aec", False, 2, at)
            aec = "off"
        header = {"schema": "encore.v1", "kind": "audio_chunk", "session_id": SID, "seq": next(seq),
                  "capture_ms": at, "generation": 1, "chunk": i, "sample_rate": 16_000, "channels": 1,
                  "encoding": "pcm_s16le", "samples": len(pcm) // 2, "aec": aec}
        out.extend(server.on_audio(encode_audio_frame(header, pcm)))
    acks = [(m["sensor"], m["generation"], m["status"]) for m in out if m["kind"] == "control_ack"]
    finals = [m["event"] for m in out if m["kind"] == "event_update" and m["event"]["phase"] == "FINAL"]
    return acks, finals


def test_the_same_input_in_both_modes_and_a_switch_during_the_reaction():
    on_acks, on = _server_run()                       # cancellation on throughout
    off_acks, off = _server_run(switch_at=0)          # turned off before any audio
    mid_acks, mid = _server_run(switch_at=8)          # turned off inside the reaction (chunks 6-9)
    assert on_acks.count(("aec", 1, "applied")) == 1
    assert ("aec", 2, "applied") in off_acks and ("aec", 2, "applied") in mid_acks
    # Each mode is recorded on its event (the window the reaction opened in).
    assert on[0]["source"]["aec"] == "active" and off[0]["source"]["aec"] == "off"
    # The switch does not end, split or move the reaction: one event, same span.
    assert len(on) == len(off) == len(mid) == 1
    span = lambda ev: (ev["playback"]["timecode_start_ms"], ev["playback"]["timecode_end_ms"])  # noqa: E731
    assert span(on[0]) == span(off[0]) == span(mid[0])
    assert {ev["generations"]["microphone"] for ev in on + off + mid} == {1}


# ---- the workspace control (Qt, CI) ----------------------------------------------------

def test_the_workspace_shows_the_switch_under_the_microphone_and_it_works(tmp_path):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from encore_client.ui.workspace import Workspace

    app = QApplication.instance() or QApplication([])
    holder = []

    def factory(url):
        holder.append(FakeTransport(url))
        holder[-1].pending.append(("open", None))
        return holder[-1]

    session = Session("ws://test", "ses-aec-ui", transport_factory=factory, microphone_on_start=True)
    workspace = Workspace(session, None, tmp_path / "index.json")
    try:
        player = FakePlayer()
        mic = LiveMicrophone(session, player=player)
        workspace.live_microphone = mic
        workspace.add_microphone(MicrophoneSwitch(session, FakeDevice(), source="test device"))
        workspace.add_echo_control(AecMode(session, mic))
        layout = workspace.layout()
        assert layout.indexOf(workspace.echo) == layout.indexOf(workspace.microphone) + 1
        workspace.refresh()
        assert not workspace.echo.button.isEnabled()
        assert workspace.echo.status.text().endswith("applies from Start")

        workspace.session_panel.start_btn.click()
        workspace.session_panel.tick()             # the stream opens
        workspace.refresh()
        assert holder[0].controls() == [(True, 1)]
        assert workspace.echo.button.isEnabled()
        assert "requested on" in workspace.echo.status.text()

        workspace.echo.button.click()
        assert holder[0].controls() == [(True, 1), (False, 2)]
        assert workspace.echo.button.text() == "Turn echo cancellation on"
        assert "requested off" in workspace.echo.status.text()
        assert not workspace.aec_label.isVisible()
        app.processEvents()
    finally:
        workspace.shutdown()
        workspace.close()
