"""Reaction detector: OPEN / UPDATE / FINAL from per-frame scores (R2, R3, R10)."""

import math
import struct

from encore_server.audio import HOP_MS, WINDOW_MS, AudioWindows, RoomBaseline, rms_db
from encore_server.detector import DetectorConfig, Frame, ReactionDetector


def frames(scores, start_ms=10_000, **kw):
    """One frame per score (laughter only), 0.48 s apart, like YAMNet."""
    for i, s in enumerate(scores):
        t = start_ms + i * HOP_MS
        yield Frame(t, t + WINDOW_MS, {"laughter": s}, kw.get("intensity", 0.5),
                    kw.get("microphone", 1), kw.get("camera"), "off")


def run(detector, scores, **kw):
    out = []
    for f in frames(scores, **kw):
        out += [ev for ev, _ in detector.push(f)]
    return out


def test_one_burst_opens_and_finishes():
    det = ReactionDetector()
    events = run(det, [0.1, 0.9, 0.8, 0.1, 0.1, 0.1, 0.1, 0.1])
    assert [e["phase"] for e in events] == ["OPEN", "FINAL"]
    opened, final = events
    assert opened["capture"]["start_ms"] == 10_480
    assert opened["capture"]["triggers_ms"] == [10_480 + WINDOW_MS // 2]
    assert final["capture"]["end_ms"] == 10_960 + WINDOW_MS  # end of the last frame above release
    assert final["revision"] == 2
    assert final["reaction"]["confidence"] == 0.9
    assert det.open == {}


def test_hysteresis_keeps_a_wobbling_reaction_open():
    det = ReactionDetector()  # laughter trigger 0.25, release 0.15
    events = run(det, [0.5, 0.2, 0.16, 0.2, 0.1, 0.1, 0.1, 0.1])
    assert [e["phase"] for e in events] == ["OPEN", "FINAL"]
    assert events[1]["capture"]["end_ms"] == 10_000 + 3 * HOP_MS + WINDOW_MS


def test_continuous_laughter_refires_every_refire_ms():
    det = ReactionDetector(DetectorConfig(refire_ms=2_000))
    events = run(det, [0.9] * 12)  # 5.3 s of laughter
    assert [e["phase"] for e in events] == ["OPEN", "UPDATE", "UPDATE"]
    t = events[-1]["capture"]["triggers_ms"]
    assert all(b - a >= 2_000 for a, b in zip(t, t[1:]))
    assert [e["revision"] for e in events] == [1, 2, 3]


def test_below_trigger_never_opens():
    assert run(ReactionDetector(), [0.24] * 20) == []


def test_close_all_finishes_open_events():
    det = ReactionDetector()
    run(det, [0.9, 0.9])
    (final,) = [ev for ev, _ in det.close_all()]
    assert final["phase"] == "FINAL" and final["capture"]["end_ms"] == 10_480 + WINDOW_MS


def test_generations_are_fixed_when_the_event_opens():
    det = ReactionDetector()
    (opened,) = run(det, [0.9], microphone=3, camera=2)
    assert opened["generations"] == {"microphone": 3, "camera": 2}


def test_each_reaction_class_has_its_own_event():
    det = ReactionDetector()
    f = Frame(0, WINDOW_MS, {"laughter": 0.9, "scream": 0.9, "gasp": 0.0}, 0.5, 1, None, "off")
    events = [ev for ev, _ in det.push(f)]
    assert sorted(e["reaction"]["type"] for e in events) == ["laughter", "scream"]
    assert len({e["event_id"] for e in events}) == 2


def test_room_baseline_scales_loudness():
    base = RoomBaseline(floor_db=-50, range_db=40, warmup=3)
    assert base.strength(-90) == 0.0  # quieter than the floor
    for _ in range(20):
        base.strength(-40)  # the room settles at -40 dBFS
    assert base.strength(-40) == 0.0
    assert base.strength(-20) == 0.5
    assert base.strength(0) == 1.0


def test_rms_db():
    full = struct.pack("<4h", 32767, -32767, 32767, -32767)
    assert round(rms_db(full)) == 0
    assert rms_db(bytes(8)) == -120.0


def test_rms_db_ignores_a_dc_offset():
    tone = [round(8_000 * math.sin(2 * math.pi * 440 * i / 16_000)) for i in range(16_000)]
    plain = rms_db(struct.pack("<16000h", *tone))
    shifted = rms_db(struct.pack("<16000h", *(v + 6_700 for v in tone)))
    assert abs(plain - shifted) < 0.01
    assert rms_db(struct.pack("<100h", *[6_700] * 100)) == -120.0  # an offset alone is silence


def test_audio_windows_overlap_and_restart_after_a_gap():
    win = AudioWindows()
    half = bytes(16_000)  # 0.5 s

    def chunk(n, cap, gen=1):
        return {"generation": gen, "chunk": n, "capture_ms": cap, "aec": "off"}, half

    assert win.add(*chunk(0, 1_000)) == []
    (w,) = win.add(*chunk(1, 1_500))
    assert (w.start_ms, w.end_ms, len(w.pcm)) == (1_000, 1_975, 31_200)
    (w,) = win.add(*chunk(2, 2_000))
    assert w.start_ms == 1_000 + HOP_MS
    assert win.add(*chunk(4, 3_000)) == []  # chunk 3 missing: start again at 3.0 s
    (w,) = win.add(*chunk(5, 3_500))
    assert w.start_ms == 3_000
    assert win.add(*chunk(0, 9_000, gen=3)) == []  # new generation starts again too
