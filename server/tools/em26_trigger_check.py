#!/usr/bin/env python3
"""EM-26: does the laughter trigger open when it should, and how soon?

Drives the real ``ServerSession`` (server/encore_server/session.py) with the
packets a PC sends: one 0.5 s audio chunk at a time, each optionally paired
with one camera frame. Nothing is downloaded. The audio "model" reads a
laughter score written into the audio itself, and the face "model" reads a
smile score written into the image, so every case states exactly what the
models would have reported and the check exercises only the server's
windowing, face pairing and 5 s fusion (``fusion.py``).

    .venv/bin/python server/tools/em26_trigger_check.py [--runs N] [--realtime] [--json]

Times are on the PC capture clock. Without ``--realtime`` the clock is
simulated: a chunk reaches the server when its last sample is captured, plus
the measured time ``on_audio`` took (no network). With ``--realtime`` chunks
are sent at their real capture times and OPEN is timed when it is returned.

Exit status 1 when any case's OPEN (or its trigger hop) differs from the
expectation.
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import struct
import sys
import time
from array import array
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from encore_server.audio import HOP_MS, WINDOW_MS, WINDOW_SAMPLES  # noqa: E402
from encore_server.fusion import TabRules  # noqa: E402
from encore_server.session import ServerSession  # noqa: E402
from encore_server.vision import MAGIC, RGB_BYTES  # noqa: E402

RATE = 16_000
CHUNK_MS = 500
CHUNK_SAMPLES = RATE * CHUNK_MS // 1000
FRAME_MS = 480  # the PC pairs a chunk with its newest camera frame, ~20 ms before the chunk ends
T0 = 10_000  # capture clock at the first sample (simulated runs)
CENTRE_MS = WINDOW_MS // 2  # a hop's trigger time is its window's centre


class ScriptedAudio:
    """Laughter score = amplitude of the sample at the window's centre (a square wave)."""

    name = "scripted-audio"

    def __call__(self, pcm: bytes) -> dict[str, float]:
        samples = array("h")
        samples.frombytes(pcm[: 2 * WINDOW_SAMPLES])
        return {"laughter": abs(samples[WINDOW_SAMPLES // 2]) / 32767, "scream": 0.0, "gasp": 0.0}


class ScriptedFace:
    """Smile written into the image's first two bytes as smile x 10 000 + 1; 0 means no face."""

    name = "scripted-face"

    def __call__(self, rgb: bytes) -> float | None:
        value = int.from_bytes(rgb[:2], "big")
        return None if value == 0 else (value - 1) / 10_000


class Timed:
    """Wraps a model and keeps each call's duration in milliseconds."""

    def __init__(self, model):
        self.model, self.name, self.ms = model, model.name, []

    def __call__(self, data):
        started = time.perf_counter()
        try:
            return self.model(data)
        finally:
            self.ms.append((time.perf_counter() - started) * 1000)


def span(first: int, last: int, value: float) -> dict[int, float]:
    """Hops ``first``..``last`` inclusive."""
    return {i: value for i in range(first, last + 1)}


@dataclass
class Case:
    name: str
    what: str
    expect_open: bool
    audio: dict[int, float] = field(default_factory=dict)  # hop -> laughter score (default .01)
    smile: dict[int, float | None] = field(default_factory=dict)  # hop -> smile (default 0, neutral face)
    expect_hop: int | None = None  # hop whose window should OPEN
    onset_hop: int | None = None  # first hop of the reaction, for onset -> OPEN
    camera: bool = True
    no_face: bool = False
    frame_offset_ms: int = FRAME_MS  # image time relative to its chunk's first sample
    frames: str = "own"  # own: chunk c carries frame c; late: frame c-1; twice: frame c - c % 2
    resend: bool = False  # every packet is sent twice
    chunks: int = 16


QUIET, WEAK, STRONG = .01, .10, .30
SMILE, NEUTRAL = .20, .0

CASES = [
    Case("strong-alone", "strong laughter, neutral face", True,
         audio=span(2, 6, STRONG), expect_hop=2, onset_hop=2),
    Case("weak-with-smile", "weak sound and a smile at the same time", True,
         audio=span(2, 8, WEAK), smile=span(2, 8, SMILE), expect_hop=4, onset_hop=2),
    Case("weak-alone", "weak sound, neutral face", False, audio=span(2, 8, WEAK)),
    Case("smile-alone", "smile without laughter (sound .02)", False,
         audio=span(2, 8, .02), smile=span(2, 8, 1.0)),
    Case("no-reaction", "quiet room, neutral face", False),
    Case("camera-off-strong", "camera off, strong laughter", True,
         audio=span(2, 6, STRONG), camera=False, expect_hop=2, onset_hop=2),
    Case("camera-off-weak", "camera off, weak sound", False, audio=span(2, 8, WEAK), camera=False),
    Case("no-face-strong", "camera on, no face found, strong laughter", True,
         audio=span(2, 6, STRONG), no_face=True, expect_hop=2, onset_hop=2),
    Case("no-face-weak", "camera on, no face found, weak sound", False,
         audio=span(2, 8, WEAK), no_face=True),
    Case("two-hops", "weak + smile for two hops only (0.96 s of 1.44 s)", False,
         audio=span(2, 3, WEAK), smile=span(2, 3, SMILE)),
    Case("resent-packets", "weak + smile, every packet sent twice", True,
         audio=span(2, 8, WEAK), smile=span(2, 8, SMILE), resend=True, expect_hop=4, onset_hop=2),
    Case("same-frame-twice", "weak + smile, each frame stamped 100 ms after its chunk and sent with two chunks",
         True, audio=span(2, 8, WEAK), smile=span(2, 8, SMILE), frame_offset_ms=CHUNK_MS + 100,
         frames="twice", expect_hop=6, onset_hop=2),
    Case("smile-after-sound", "weak sound, then a smile as it stops (one shared hop); frames 399 ms after their audio",
         False, audio=span(2, 8, WEAK), smile=span(8, 14, SMILE), frame_offset_ms=CHUNK_MS + 399),
    Case("frames-one-chunk-late", "weak + smile, each frame delivered with the next chunk", False,
         audio=span(2, 8, WEAK), smile=span(2, 8, SMILE), frames="late"),
    Case("edge-inside", "weak + smile at hops 2, 3 and 11: oldest hop 4.8 s back, inside 5 s", True,
         audio={2: WEAK, 3: WEAK, 11: WEAK}, smile={2: SMILE, 3: SMILE, 11: SMILE}, expect_hop=11, onset_hop=2),
    Case("edge-outside", "weak + smile at hops 2, 3 and 12: oldest hop 5.28 s back, outside 5 s", False,
         audio={2: WEAK, 3: WEAK, 12: WEAK}, smile={2: SMILE, 3: SMILE, 12: SMILE}),
]


def hop_of(t: float, t0: float) -> int:
    """The hop whose face bin holds capture time ``t`` (bins are centred on window centres)."""
    return int((t - t0 - CENTRE_MS + HOP_MS // 2) // HOP_MS)


def pcm_for(case: Case, t0: int, chunk: int) -> bytes:
    samples = array("h")
    start = t0 + chunk * CHUNK_MS
    for j in range(CHUNK_SAMPLES):
        score = case.audio.get(hop_of(start + j * 1000 / RATE, t0), QUIET)
        level = round(min(max(score, 0.0), 1.0) * 32767)
        samples.append(level if j % 2 else -level)
    return samples.tobytes()


def image(smile: float | None) -> bytes:
    value = 0 if smile is None else round(smile * 10_000) + 1
    return value.to_bytes(2, "big") + bytes(RGB_BYTES - 2)


def packet(case: Case, t0: int, chunk: int, frame_chunk: int | None) -> bytes:
    header = json.dumps(dict(schema="encore.v1", session_id="em26", seq=chunk + 2, kind="audio_chunk",
                             capture_ms=t0 + chunk * CHUNK_MS, generation=1, chunk=chunk,
                             samples=CHUNK_SAMPLES, encoding="pcm_s16le", channels=1,
                             sample_rate=RATE, aec="off")).encode()
    data = struct.pack("!I", len(header)) + header + pcm_for(case, t0, chunk)
    if frame_chunk is None:
        return data
    image_ms = t0 + frame_chunk * CHUNK_MS + case.frame_offset_ms
    smile = None if case.no_face else case.smile.get(hop_of(image_ms, t0), NEUTRAL)
    return MAGIC + struct.pack("!IQ", 1, image_ms) + image(smile) + data


def control(session: ServerSession, sensor: str, seq: int, capture_ms: int) -> None:
    session.on_text(json.dumps(dict(schema="encore.v1", session_id="em26", kind="control_update",
                                    sensor=sensor, generation=1, enabled=True, seq=seq,
                                    capture_ms=capture_ms, idempotency_key=f"{sensor}-1")))


def run_case(case: Case, *, realtime: bool = False, audio_model=None, face_model=None) -> dict:
    audio = Timed(audio_model or ScriptedAudio())
    face = Timed(face_model or ScriptedFace())
    session = ServerSession("em26", audio, face_model=face)
    fusion = session.detector.fusion
    trace, credited, doubled = [], set(), []
    push = fusion.push

    def traced(t, raw, visual):  # records each hop and any audio hop credited a second time
        result = push(t, raw, visual)
        if fusion.samples and fusion.samples[-1][0] == t:
            if t in credited:
                doubled.append(t)
            credited.add(t)
        trace.append({"t": t, "audio": round(raw, 3), "smile": visual, "evidence_s": round(fusion.total, 3)})
        return result
    fusion.push = traced

    now_ms = lambda: time.monotonic() * 1000  # noqa: E731
    t0 = int(now_ms()) + 200 if realtime else T0
    control(session, "microphone", 0, t0)
    if case.camera:
        control(session, "camera", 1, t0)
    opens, server_ms = [], []
    for chunk in range(case.chunks):
        frame_chunk = None
        if case.camera:
            frame_chunk = {"own": chunk, "late": chunk - 1, "twice": chunk - chunk % 2}[case.frames]
            if frame_chunk < 0:
                frame_chunk = None
        packets = [packet(case, t0, chunk, frame_chunk)]
        if case.resend:
            packets *= 2
        ready_ms = t0 + (chunk + 1) * CHUNK_MS
        if realtime:
            time.sleep(max(0.0, (ready_ms - now_ms()) / 1000))
        for data in packets:
            started = time.perf_counter()
            out = session.on_audio(data)
            took = (time.perf_counter() - started) * 1000
            server_ms.append(took)
            received = now_ms() if realtime else ready_ms + took
            if not realtime:
                session._collect_face(grace=CHUNK_MS / 1000)  # the time until the next chunk
            for msg in out:
                if msg.get("kind") == "event_update" and msg["event"]["phase"] == "OPEN":
                    opens.append((msg["event"]["capture"]["triggers_ms"][0], received, msg["event"]))
    session.end()

    actual_hop = None
    result = {"case": case.name, "what": case.what, "expect_open": case.expect_open, "open": bool(opens),
              "expect_hop": case.expect_hop, "open_hop": None, "trigger_to_open_ms": None,
              "onset_to_open_ms": None, "max_evidence_s": max((r["evidence_s"] for r in trace), default=0),
              "doubled_hops": doubled, "events": len(opens),
              "audio_ms": _stats(audio.ms), "face_ms": _stats(face.ms), "on_audio_ms": _stats(server_ms),
              "face_frames": session.face_frames, "audio_windows": len(audio.ms)}
    if opens:
        trigger, received, event = opens[0]
        actual_hop = (trigger - t0 - CENTRE_MS) // HOP_MS
        result.update(open_hop=actual_hop, trigger_to_open_ms=round(received - trigger, 1),
                      model=event["source"]["model"], config=event["source"]["config"])
        if case.onset_hop is not None:
            onset = t0 + case.onset_hop * HOP_MS + CENTRE_MS - HOP_MS // 2
            result["onset_to_open_ms"] = round(received - onset, 1)
    result["ok"] = (bool(opens) == case.expect_open and not doubled
                    and (case.expect_hop is None or actual_hop == case.expect_hop))
    result["trace"] = trace
    return result


def _stats(values):
    if not values:
        return None
    return {"n": len(values), "median": round(statistics.median(values), 2), "max": round(max(values), 2)}


def header(rules: TabRules) -> dict:
    return {"thresholds": {"strong_audio": rules.strong_audio, "assisted_audio": rules.assisted_audio,
                           "smile": rules.smile, "release_ratio": rules.release_ratio,
                           "window_ms": rules.window_ms, "equivalent_hops": rules.equivalent_hops,
                           "target_s": rules.equivalent_hops * HOP_MS / 1000, "quiet_ms": rules.quiet_ms},
            "hop_ms": HOP_MS, "window_ms": WINDOW_MS, "chunk_ms": CHUNK_MS, "frame_offset_ms": FRAME_MS,
            "python": platform.python_version(), "machine": platform.machine()}


def _median(values, digits=0):
    values = [v for v in values if v is not None]
    return f"{statistics.median(values):.{digits}f}" if values else "-"


def print_table(info: dict, results: list[list[dict]], args) -> None:
    t = info["thresholds"]
    print(f"models: {ScriptedAudio.name} + {ScriptedFace.name}; strong {t['strong_audio']}, "
          f"assisted {t['assisted_audio']}, smile {t['smile']}, release x{t['release_ratio']}, "
          f"{t['window_ms']} ms window, target {t['target_s']} s; hop {HOP_MS} ms, chunk {CHUNK_MS} ms; "
          f"runs {args.runs}{', realtime' if args.realtime else ', simulated clock'}")
    print(f"{'case':30} {'expect':>6} {'actual':>6} {'hop e/a':>7} {'trig->OPEN':>10} {'onset->OPEN':>11}"
          f" {'evid s':>6} {'audio ms':>8} {'face ms':>7}  ok")
    for runs in results:
        r = runs[0]
        hops = f"{'-' if r['expect_hop'] is None else r['expect_hop']}/{'-' if r['open_hop'] is None else r['open_hop']}"
        print(f"{r['case']:30} {'OPEN' if r['expect_open'] else '-':>6} {'OPEN' if r['open'] else '-':>6}"
              f" {hops:>7} {_median([x['trigger_to_open_ms'] for x in runs]):>10}"
              f" {_median([x['onset_to_open_ms'] for x in runs]):>11} {r['max_evidence_s']:6.2f}"
              f" {_median([(x['audio_ms'] or {}).get('median') for x in runs], 2):>8}"
              f" {_median([(x['face_ms'] or {}).get('median') for x in runs], 2):>7}"
              f"  {'ok' if all(x['ok'] for x in runs) else 'FAIL'}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", type=int, default=1, help="repeat each case (latency statistics)")
    ap.add_argument("--realtime", action="store_true", help="send chunks at their real capture times")
    ap.add_argument("--json", action="store_true", help="print JSON instead of a table")
    args = ap.parse_args(argv)

    results = [[run_case(c, realtime=args.realtime) for _ in range(args.runs)] for c in CASES]
    info = header(TabRules())
    if args.json:
        print(json.dumps({"setup": info, "runs": args.runs, "realtime": args.realtime,
                          "cases": [r for runs in results for r in runs]}, indent=1))
    else:
        print_table(info, results, args)
    return 0 if all(r["ok"] for runs in results for r in runs) else 1


if __name__ == "__main__":
    sys.exit(main())
