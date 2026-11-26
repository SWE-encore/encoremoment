"""ServerSession: controls, anchors, audio admission and committed events (R2, R3, R5, R8, R9)."""

import json
import math
import struct

from encore_server.contract import SessionView, schema_errors
from encore_server.contract import _contract as contract
from encore_server.models import LoudnessModel
from encore_server.session import ServerSession

SID = "ses-test-server"
HALF_S = 8_000  # samples in a 0.5 s chunk


def tone(samples=HALF_S, amp=0.5):
    return struct.pack(f"<{samples}h", *(round(amp * 32767 * math.sin(2 * math.pi * 440 * i / 16_000))
                                          for i in range(samples)))


QUIET = bytes(2 * HALF_S)
LOUD = tone()


class Pc:
    """Builds PC messages with one shared seq counter, like the client."""

    def __init__(self, sid=SID):
        self.sid, self.seq, self.chunks = sid, 0, {}

    def _next(self):
        self.seq += 1
        return self.seq - 1

    def control(self, sensor, enabled, gen, cap=0, key=None):
        return json.dumps({
            "schema": "encore.v1", "kind": "control_update", "session_id": self.sid, "seq": self._next(),
            "capture_ms": cap, "idempotency_key": key or f"ctl-{sensor}-{gen}", "sensor": sensor,
            "enabled": enabled, "generation": gen,
        })

    def anchor(self, cap, position_ms, *, epoch=1, state="playing", content="film-1"):
        return json.dumps({
            "schema": "encore.v1", "kind": "playback_anchor", "session_id": self.sid, "seq": self._next(),
            "capture_ms": cap, "playback": {"epoch": epoch, "content_id": content, "title": "Test film",
                                            "position_ms": position_ms, "rate": 1.0, "state": state},
        })

    def audio(self, gen, cap, pcm, chunk=None):
        if chunk is None:
            chunk = self.chunks.get(gen, -1) + 1
        self.chunks[gen] = max(chunk, self.chunks.get(gen, -1))
        header = {
            "schema": "encore.v1", "kind": "audio_chunk", "session_id": self.sid, "seq": self._next(),
            "capture_ms": cap, "generation": gen, "chunk": chunk, "sample_rate": 16_000, "channels": 1,
            "encoding": "pcm_s16le", "samples": len(pcm) // 2, "aec": "off",
        }
        return contract.encode_audio_frame(header, pcm)


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def new_session(**kw):
    return ServerSession(SID, LoudnessModel(), **kw)


def stream(session, pc, gen, start_ms, pcms):
    """Send chunks back to back from ``start_ms``; returns everything the server sent."""
    out = []
    for i, pcm in enumerate(pcms):
        out += session.on_audio(pc.audio(gen, start_ms + 500 * i, pcm))
    return out


def events(msgs):
    return [m["event"] for m in msgs if m["kind"] == "event_update"]


def test_control_acks_applied_duplicate_rejected():
    s, pc = new_session(), Pc()
    on = pc.control("microphone", True, 1)
    (ack,) = s.on_text(on)
    assert ack["status"] == "applied" and "reason" not in ack
    (ack,) = s.on_text(on)  # retried after a reconnect
    assert ack["status"] == "duplicate"
    (ack,) = s.on_text(pc.control("microphone", True, 1, key="ctl-other"))
    assert (ack["status"], ack["reason"]) == ("rejected", "stale_generation")
    (ack,) = s.on_text(pc.control("camera", False, 1))
    assert (ack["status"], ack["reason"]) == ("rejected", "no_state_change")
    assert all(schema_errors(m) == [] for m in [ack])


def test_bad_text_is_counted_not_answered():
    s = new_session()
    assert s.on_text("not json") == []
    assert s.on_text(json.dumps({"kind": "control_update"})) == []  # fails the schema
    assert s.on_text(Pc("ses-someone-else").control("microphone", True, 1)) == []
    assert s.on_audio(b"\x00\x00") == []
    assert s.invalid == 4


def test_audio_needs_the_microphone_on_in_its_generation():
    s, pc = new_session(), Pc()
    assert stream(s, pc, 1, 1_000, [LOUD] * 4) == []  # microphone never turned on
    assert s.audio_chunks == 0 and s.view.rejected["stale_audio_generation"] == 4


def test_loud_audio_opens_and_finishes_an_event():
    s, pc = new_session(), Pc()
    s.on_text(pc.control("microphone", True, 1, cap=1_000))
    out = stream(s, pc, 1, 1_000, [QUIET] * 4 + [LOUD] * 4 + [QUIET] * 8)
    evs = events(out)
    phases = [e["phase"] for e in evs]
    assert phases[0] == "OPEN" and phases[-1] == "FINAL" and set(phases[1:-1]) <= {"UPDATE"}
    final = evs[-1]
    assert {e["event_id"] for e in evs} == {final["event_id"]}
    assert final["revision"] == len(evs)
    assert final["reaction"]["type"] == "laughter"
    assert final["generations"] == {"microphone": 1, "camera": None}
    assert final["source"] == {"model": "loudness-standin", "config": "detector-v3", "aec": "off"}
    assert final["playback"] == {"status": "pending"}  # no anchor yet: never guessed
    assert 3_000 - 1_000 <= final["capture"]["start_ms"] <= 3_000
    assert final["capture"]["end_ms"] >= 5_000
    assert [m["cursor"] for m in out] == list(range(1, len(out) + 1))
    assert all(schema_errors(m) == [] for m in out)


def test_events_map_to_content_timecode_through_anchors():
    s, pc = new_session(), Pc()
    s.on_text(pc.control("microphone", True, 1, cap=1_000))
    s.on_text(pc.anchor(500, 60_000))  # 60.0 s into the film at capture 0.5 s
    out = stream(s, pc, 1, 1_000, [QUIET] * 4 + [LOUD] * 4 + [QUIET] * 8)
    final = events(out)[-1]
    pb, cap = final["playback"], final["capture"]
    assert pb["status"] == "mapped" and pb["epoch"] == 1 and pb["title"] == "Test film"
    assert pb["timecode_start_ms"] == 60_000 + cap["start_ms"] - 500
    assert pb["timecode_end_ms"] == 60_000 + cap["end_ms"] - 500


def test_a_seek_during_the_event_leaves_the_end_unmapped():
    s, pc = new_session(), Pc()
    s.on_text(pc.control("microphone", True, 1, cap=1_000))
    s.on_text(pc.anchor(500, 60_000))
    out = stream(s, pc, 1, 1_000, [QUIET] * 4 + [LOUD] * 2)
    s.on_text(pc.anchor(4_200, 600_000, epoch=2))  # the viewer seeks while laughing
    out += stream(s, pc, 1, 4_000, [LOUD] * 2 + [QUIET] * 8)
    final = events(out)[-1]
    assert final["phase"] == "FINAL"
    assert final["playback"]["epoch"] == 1 and final["playback"]["timecode_end_ms"] is None


def test_paused_anchor_pins_the_timecode():
    s, pc = new_session(), Pc()
    s.on_text(pc.control("microphone", True, 1))
    s.on_text(pc.anchor(500, 42_000, state="paused"))
    final = events(stream(s, pc, 1, 1_000, [QUIET] * 4 + [LOUD] * 4 + [QUIET] * 8))[-1]
    assert final["playback"]["timecode_start_ms"] == final["playback"]["timecode_end_ms"] == 42_000


def test_microphone_off_closes_events_and_old_audio_is_ignored():
    s, pc = new_session(), Pc()
    s.on_text(pc.control("microphone", True, 1))
    out = stream(s, pc, 1, 1_000, [QUIET] * 4 + [LOUD] * 3)
    assert [e["phase"] for e in events(out)] == ["OPEN"]
    ack, update = s.on_text(pc.control("microphone", False, 2, cap=4_500))
    assert ack["status"] == "applied"
    assert update["event"]["phase"] == "FINAL"
    # A chunk of generation 1 that was in flight: not analysed (R8).
    assert s.on_audio(pc.audio(1, 4_500, LOUD)) == []
    assert s.view.rejected["stale_audio_generation"] == 1
    # The microphone comes back as generation 3: chunk numbers restart.
    s.on_text(pc.control("microphone", True, 3))
    later = events(stream(s, pc, 3, 20_000, [QUIET] * 4 + [LOUD] * 4 + [QUIET] * 8))
    assert later[0]["generations"]["microphone"] == 3
    assert later[0]["event_id"] != update["event"]["event_id"]


def test_retried_and_missing_chunks():
    s, pc = new_session(), Pc()
    s.on_text(pc.control("microphone", True, 1))
    s.on_audio(pc.audio(1, 1_000, QUIET, chunk=0))
    s.on_audio(pc.audio(1, 1_500, QUIET, chunk=1))
    assert s.on_audio(pc.audio(1, 1_500, QUIET, chunk=1)) == []  # retried
    s.on_audio(pc.audio(1, 2_500, QUIET, chunk=3))  # chunk 2 lost
    assert s.view.ignored["duplicate_audio"] == 1
    assert s.view.audio_gaps == 1
    assert s.audio_chunks == 3


def test_camera_generation_is_recorded_when_on():
    s, pc = new_session(), Pc()
    s.on_text(pc.control("microphone", True, 1))
    s.on_text(pc.control("camera", True, 1))
    (opened, *_) = events(stream(s, pc, 1, 1_000, [QUIET] * 4 + [LOUD] * 3))
    assert opened["generations"] == {"microphone": 1, "camera": 1}


def test_delayed_result_uses_camera_consent_at_the_capture_trigger():
    # Two chunks form a 975 ms model window. Its trigger is at 1487 ms.
    # Camera controls can reach the server before the second chunk does.
    for toggles, expected in [
        ([(True, 1750)], None),
        ([(True, 1487)], 1),
        ([(True, 1200), (False, 1487)], None),
        ([(True, 1200), (False, 1488), (True, 1800)], 1),
    ]:
        s, pc = new_session(), Pc()
        s.on_text(pc.control("microphone", True, 1))
        s.on_audio(pc.audio(1, 1000, LOUD))
        for generation, (enabled, capture_ms) in enumerate(toggles, 1):
            s.on_text(pc.control("camera", enabled, generation, cap=capture_ms))
        opened, = events(s.on_audio(pc.audio(1, 1500, LOUD)))
        assert opened["capture"]["triggers_ms"] == [1487]
        assert opened["generations"]["camera"] == expected


def test_idle_and_end_close_open_events():
    clock = Clock()
    s, pc = new_session(clock=clock, idle_s=2.0), Pc()
    s.on_text(pc.control("microphone", True, 1))
    assert events(stream(s, pc, 1, 1_000, [QUIET] * 4 + [LOUD] * 3))[0]["phase"] == "OPEN"
    clock.t = 1.9
    assert s.on_idle() == []
    clock.t = 2.0
    (update,) = s.on_idle()
    assert update["event"]["phase"] == "FINAL"
    assert s.on_idle() == [] and s.end() == []


def test_on_frame_sees_every_analysed_window():
    seen = []
    s, pc = ServerSession(SID, LoudnessModel(), on_frame=seen.append), Pc()
    s.on_text(pc.control("microphone", True, 1))
    stream(s, pc, 1, 1_000, [QUIET] * 4 + [LOUD] * 4)
    assert len(seen) == 7  # 4 s of audio: windows start every 0.48 s and need 0.975 s
    assert [f.start_ms for f in seen] == [1_000 + 480 * i for i in range(7)]
    assert seen[0].scores["laughter"] == 0.0 and seen[-1].scores["laughter"] == 1.0
    assert all(f.microphone == 1 for f in seen)


def test_results_replay_from_a_cursor():
    s, pc = new_session(), Pc()
    s.on_text(pc.control("microphone", True, 1))
    stream(s, pc, 1, 1_000, [QUIET] * 4 + [LOUD] * 4 + [QUIET] * 8)
    n = len(s.log)
    assert n >= 2
    assert [m["cursor"] for m in s.results_after(0)] == list(range(1, n + 1))
    assert [m["cursor"] for m in s.results_after(1)] == list(range(2, n + 1))
    assert s.results_after(n) == []


def test_the_pc_receiver_accepts_everything_the_server_sends():
    s, pc = new_session(), Pc()
    pc_view = SessionView(SID)

    def deliver(pc_msg, out):
        pc_view.receive("pc->server", json.loads(pc_msg) if isinstance(pc_msg, str)
                        else contract.decode_audio_frame(pc_msg)[0])
        for m in out:
            assert schema_errors(m) == []
            pc_view.receive("server->pc", m)

    def text(msg):
        deliver(msg, s.on_text(msg))

    def audio(gen, start, pcms):
        for i, pcm in enumerate(pcms):
            frame = pc.audio(gen, start + 500 * i, pcm)
            deliver(frame, s.on_audio(frame))

    text(pc.control("microphone", True, 1))
    text(pc.anchor(500, 10_000))
    audio(1, 1_000, [QUIET] * 4 + [LOUD] * 12 + [QUIET] * 8)  # long laughter: OPEN, UPDATEs, FINAL
    audio(1, 13_000, [LOUD] * 3)
    text(pc.control("microphone", False, 2))
    for m in s.end():
        pc_view.receive("server->pc", m)
    assert pc_view.rejected == {}
    assert pc_view.next_cursor == len(s.log) + 1
    phases = [m["event"]["phase"] for m in s.log]
    assert phases.count("FINAL") == len(pc_view.events) == 2
    assert "UPDATE" in phases
    assert all(e["phase"] == "FINAL" for e in pc_view.events.values())
