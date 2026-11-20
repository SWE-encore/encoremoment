"""EM-17 / R8: turning microphone sensing off and on.

Acceptance (Jira EM-17):
1. Off stops the client's microphone reads, the audio upload and the acceptance
   of new audio events, and shows the state.
2. The camera keeps its own state; with the microphone off, a face alone never
   makes a new sound event.
3. Off and on while an event is open or an inference is pending: results of
   the previous generation are excluded.

No Qt, network or audio device: a fake transport and a fake capture device.
"""

import json
import math
import struct
import threading

from encore_client.sensing.live_mic import LiveMicrophone
from encore_client.sensing.microphone import MicrophoneSwitch
from encore_client.session.session import Conn, Session

SID = "ses-mic-switch"


# ---- fakes ---------------------------------------------------------------------------

class FakeTransport:
    def __init__(self, url):
        self.pending, self.sent = [], []

    def start(self):
        pass

    def send(self, data):
        if isinstance(data, str):
            self.sent.append(json.loads(data))
        else:
            self.sent.append({"kind": "audio_frame", "bytes": len(data)})
        return True

    def poll(self):
        out, self.pending = self.pending, []
        return out

    def close(self):
        pass


class FakeDevice:
    def __init__(self, fail=False):
        self.fail, self.running, self.starts, self.stops = fail, False, 0, 0

    def start(self):
        if self.fail:
            raise RuntimeError("microphone permission denied")
        self.running, self.starts = True, self.starts + 1

    def stop(self):
        self.running, self.stops = False, self.stops + 1


def live_session(device=None):
    transports = []

    def factory(url):
        transports.append(FakeTransport(url))
        return transports[-1]

    session = Session("ws://test", SID, transport_factory=factory, clock=lambda: 10_000,
                      microphone_on_start=True)
    switch = MicrophoneSwitch(session, device if device is not None else FakeDevice())
    session.start()
    transports[0].pending.append(("open", None))
    session.poll()
    assert session.state is Conn.CONNECTED
    switch.sync()   # what the window's timer does
    return session, switch, transports[0]


def controls(transport, sensor="microphone"):
    return [(m["enabled"], m["generation"]) for m in transport.sent
            if m.get("kind") == "control_update" and m["sensor"] == sensor]


def event(cursor, event_id, phase, revision, mic_generation):
    return {
        "schema": "encore.v1", "kind": "event_update", "session_id": SID, "cursor": cursor,
        "event": {
            "event_id": event_id, "revision": revision, "phase": phase,
            "reaction": {"type": "laughter", "confidence": 0.9},
            "intensity": {"combined": 0.6, "audio": 0.6, "visual": None},
            "capture": {"triggers_ms": [9_000], "start_ms": 8_500,
                        "end_ms": 9_800 if phase == "FINAL" else None},
            "generations": {"microphone": mic_generation, "camera": None},
            "playback": {"status": "pending"},
            "source": {"model": "test", "config": "test", "aec": "off"},
        },
    }


def deliver(session, transport, *msgs):
    before = dict(session.view.rejected)
    for m in msgs:
        transport.pending.append(("message", json.dumps(m)))
    session.poll()
    return {k: v - before.get(k, 0) for k, v in session.view.rejected.items() if v != before.get(k, 0)}


PCM_HALF_S = b"\x01\x00" * 8000


# ---- 1. off stops reads, upload and new events; the state is shown -------------------

def test_start_opens_the_device_and_off_closes_it_before_telling_the_server():
    device = FakeDevice()
    session, switch, t = live_session(device)
    assert device.running and controls(t) == [(True, 1)]
    assert "Microphone on" in switch.status_text() and switch.button_text() == "Turn microphone off"

    assert switch.turn_off()
    assert not device.running and device.stops == 1
    assert controls(t) == [(True, 1), (False, 2)]
    assert "Microphone off" in switch.status_text() and "generation 2" in switch.status_text()
    assert switch.button_text() == "Turn microphone on"


def test_off_stops_the_upload():
    session, switch, t = live_session()
    mic = LiveMicrophone(session, clock=lambda: 10_000)
    assert mic.feed(PCM_HALF_S, end_ms=10_000) == 1
    switch.turn_off()
    sent_before = len(t.sent)
    assert mic.feed(PCM_HALF_S, end_ms=10_500) == 0
    assert session.send_audio(PCM_HALF_S, 10_500) is False
    assert len(t.sent) == sent_before


def test_on_again_reopens_the_device_with_a_new_generation():
    device = FakeDevice()
    session, switch, t = live_session(device)
    switch.turn_off()
    assert switch.turn_on()
    assert device.running and device.starts == 2
    assert controls(t) == [(True, 1), (False, 2), (True, 3)]
    mic = LiveMicrophone(session, clock=lambda: 20_000)
    assert mic.feed(PCM_HALF_S, end_ms=20_000) == 1
    frames = [m for m in t.sent if m.get("kind") == "audio_frame"]
    assert frames and session.view.sensors["microphone"].generation == 3


def test_toggling_needs_an_active_session():
    session = Session("ws://test", SID, transport_factory=FakeTransport, microphone_on_start=True)
    switch = MicrophoneSwitch(session, FakeDevice())
    assert not switch.can_toggle and not switch.toggle()
    assert switch.status_text() == "Microphone: off until Start"
    session.start()
    session.end()
    switch.sync()
    assert not switch.device_running and switch.status_text() == "Microphone: off — session ended"


def test_end_closes_the_device():
    device = FakeDevice()
    session, switch, _ = live_session(device)
    session.end()
    switch.sync()
    assert not device.running


def test_a_device_that_cannot_open_leaves_the_microphone_off():
    session, switch, t = live_session(FakeDevice(fail=True))
    # Start turned the sensor on; the device failed, so the switch turned it back off
    assert controls(t) == [(True, 1), (False, 2)]
    assert "permission denied" in switch.status_text()
    assert not switch.turn_on() and not switch.enabled


# ---- 1 + 3. new events of an old generation are refused; open ones may close ---------

def test_old_generation_events_are_refused_after_off_and_after_on_again():
    session, switch, t = live_session()
    assert deliver(session, t, event(1, "evt-a", "OPEN", 1, 1)) == {}
    switch.turn_off()
    # a late detection from generation 1 is not shown as new
    assert deliver(session, t, event(2, "evt-b", "OPEN", 1, 1)) == {"stale_microphone_generation": 1}
    # the event that was already open is still closed
    assert deliver(session, t, event(3, "evt-a", "FINAL", 2, 1)) == {}
    switch.turn_on()   # generation 3
    assert deliver(session, t, event(4, "evt-c", "OPEN", 1, 1)) == {"stale_microphone_generation": 1}
    assert deliver(session, t, event(5, "evt-d", "OPEN", 1, 3)) == {}
    phases = {eid: ev["phase"] for eid, ev in session.view.events.items()}
    assert phases == {"evt-a": "FINAL", "evt-d": "OPEN"}


# ---- 2. the camera is independent --------------------------------------------------------

def test_the_microphone_switch_never_touches_the_camera():
    session, switch, t = live_session()
    session.set_sensor("camera", True)
    cam_before = session.view.sensors["camera"]
    switch.turn_off()
    switch.turn_on()
    switch.turn_off()
    assert session.view.sensors["camera"] == cam_before
    assert controls(t, "camera") == [(True, 1)]


# ---- server side: the same rules on the real ServerSession -------------------------------

def _server_control(sensor, enabled, generation, seq):
    return json.dumps(dict(schema="encore.v1", kind="control_update", session_id=SID, seq=seq,
                           capture_ms=0, idempotency_key=f"ctl-{sensor}-{generation}",
                           sensor=sensor, enabled=enabled, generation=generation))


def _audio(seq, generation, chunk, capture_ms, pcm):
    header = json.dumps(dict(schema="encore.v1", kind="audio_chunk", session_id=SID, seq=seq,
                             capture_ms=capture_ms, generation=generation, chunk=chunk,
                             sample_rate=16000, channels=1, encoding="pcm_s16le",
                             samples=len(pcm) // 2, aec="off")).encode()
    return struct.pack("!I", len(header)) + header + pcm


LOUD = struct.pack("<8000h", *(round(0.5 * 32767 * math.sin(2 * math.pi * 440 * i / 16000))
                               for i in range(8000)))
QUIET = bytes(16000)


def _events(msgs):
    return [m["event"] for m in msgs if m.get("kind") == "event_update"]


def test_server_closes_the_open_event_ignores_old_audio_and_starts_fresh():
    from encore_server.models import LoudnessModel
    from encore_server.session import ServerSession

    s = ServerSession(SID, LoudnessModel())
    s.on_text(_server_control("microphone", True, 1, 0))
    out = []
    for i, pcm in enumerate([QUIET] * 4 + [LOUD] * 3):
        out += s.on_audio(_audio(1 + i, 1, i, 1_000 + 500 * i, pcm))
    (opened,) = [e for e in _events(out) if e["phase"] == "OPEN"]

    off = s.on_text(_server_control("microphone", False, 2, 20))
    assert off[0]["status"] == "applied"
    assert [(e["event_id"], e["phase"]) for e in _events(off)] == [(opened["event_id"], "FINAL")]

    late = []
    for i in range(7, 12):   # generation-1 audio still in flight
        late += s.on_audio(_audio(14 + i, 1, i, 1_000 + 500 * i, LOUD))
    assert _events(late) == [] and s.view.rejected["stale_audio_generation"] == 5

    s.on_text(_server_control("microphone", True, 3, 40))
    again = []
    for i, pcm in enumerate([QUIET] * 4 + [LOUD] * 3):
        again += s.on_audio(_audio(41 + i, 3, i, 20_000 + 500 * i, pcm))
    new = [e for e in _events(again) if e["phase"] == "OPEN"]
    assert len(new) == 1 and new[0]["event_id"] != opened["event_id"]
    assert new[0]["generations"]["microphone"] == 3


def _paired(seq, mic_generation, camera_generation, chunk, capture_ms):
    from encore_server.vision import MAGIC, RGB_BYTES

    data = _audio(seq, mic_generation, chunk, capture_ms, QUIET)
    return MAGIC + struct.pack("!IQ", camera_generation, capture_ms + 499) + bytes(RGB_BYTES) + data


class _Scripted:
    name = "scripted"

    def __init__(self, laughter):
        self.laughter = laughter

    def __call__(self, pcm):
        return {"laughter": self.laughter}


def test_server_face_alone_with_the_microphone_off_makes_no_sound_event():
    from encore_server.session import ServerSession

    s = ServerSession(SID, _Scripted(0.10), face_model=lambda rgb: 1.0)   # a big smile
    s.on_text(_server_control("microphone", True, 1, 0))
    s.on_text(_server_control("camera", True, 1, 1))
    s.on_text(_server_control("microphone", False, 2, 2))
    out = []
    for i in range(12):
        out += s.on_audio(_paired(3 + i, 1, 1, i, 500 * i))
        s._collect_face(grace=.5)
    assert _events(out) == [] and s.log == []
    assert s.view.sensors["camera"].enabled and s.view.sensors["camera"].generation == 1


def test_server_turning_off_while_a_face_inference_is_pending_adds_nothing():
    from encore_server.session import ServerSession

    started, release = threading.Event(), threading.Event()

    def slow_face(rgb):
        started.set()
        release.wait(2)
        return 1.0

    s = ServerSession(SID, _Scripted(0.10), face_model=slow_face)
    s.on_text(_server_control("microphone", True, 1, 0))
    s.on_text(_server_control("camera", True, 1, 1))
    try:
        s.on_audio(_paired(2, 1, 1, 0, 0))
        assert started.wait(1)
        s.on_text(_server_control("microphone", False, 2, 3))
        release.set()
        s._collect_face(grace=1)
        late = []
        for i in range(1, 8):   # generation-1 packets that were already on the way
            late += s.on_audio(_paired(3 + i, 1, 1, i, 500 * i))
        assert _events(late) == [] and s.log == []
    finally:
        release.set()
