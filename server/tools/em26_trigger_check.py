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

Real models: ``--audio-model yamnet`` with ``--wav LABEL=FILE`` (mono 16 kHz
PCM16, repeatable) replaces the scripted cases. A file labelled ``laughter``
expects an OPEN; any other label expects none. Each file plays after 2 s of
silence and is followed by 3 s of silence, once with the camera off, once with
a neutral face and once smiling (.20) while it plays; those faces are scripted.
``--face-python``/``--face-task`` use the real MediaPipe worker instead, on
blank frames (camera on, no face found), and drop the scripted face runs.
Needs server/requirements-yamnet.txt; TFHUB_CACHE_DIR picks the YAMNet copy.

Times are on the PC capture clock. Without ``--realtime`` the clock is
simulated: a chunk reaches the server when its last sample is captured, plus
the measured time ``on_audio`` took (no network). With ``--realtime`` chunks
are sent at their real capture times and OPEN is timed when it is returned.

Exit status 1 when any case's OPEN (or its trigger hop) differs from the
expectation.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import statistics
import struct
import subprocess
import sys
import time
import wave
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
WAV_AT_MS, WAV_TAIL_MS = 2_000, 3_000  # silence before and after a recorded input
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
    onset_ms: int | None = None  # or the reaction's start after the first sample
    camera: bool = True
    no_face: bool = False
    frame_offset_ms: int = FRAME_MS  # image time relative to its chunk's first sample
    frames: str = "own"  # own: chunk c carries frame c; late: frame c-1; twice: frame c - c % 2
    resend: bool = False  # every packet is sent twice
    chunks: int = 16
    wav: bytes | None = None  # recorded PCM16 at WAV_AT_MS, silence around it; replaces ``audio``


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


def read_wav(path: Path) -> bytes:
    with wave.open(str(path), "rb") as w:
        if (w.getnchannels(), w.getframerate(), w.getsampwidth()) != (1, RATE, 2):
            raise SystemExit(f"{path}: need mono {RATE} Hz 16-bit PCM WAV")
        return w.readframes(w.getnframes())


def wav_cases(label: str, path: Path, real_face: bool) -> list[Case]:
    """Camera off, then a neutral face and a smile (scripted) or blank frames (real face model)."""
    pcm = read_wav(path)
    laughter = label == "laughter"
    end_ms = WAV_AT_MS + len(pcm) // 2 * 1000 // RATE
    common = dict(wav=pcm, onset_ms=WAV_AT_MS, chunks=-(-(end_ms + WAV_TAIL_MS) // CHUNK_MS))
    what = f"{label}: {path.name}"
    cases = [Case(f"{path.stem}/camera-off", f"{what}, camera off", laughter, camera=False, **common)]
    if real_face:
        return cases + [Case(f"{path.stem}/no-face", f"{what}, camera on, blank frames", laughter, **common)]
    smile = span(hop_of(T0 + WAV_AT_MS, T0), hop_of(T0 + end_ms - 1, T0), SMILE)
    return cases + [Case(f"{path.stem}/neutral", f"{what}, neutral face", laughter, **common),
                    Case(f"{path.stem}/smile", f"{what}, smiling while it plays", laughter, smile=smile, **common)]


def hop_of(t: float, t0: float) -> int:
    """The hop whose face bin holds capture time ``t`` (bins are centred on window centres)."""
    return int((t - t0 - CENTRE_MS + HOP_MS // 2) // HOP_MS)


def pcm_for(case: Case, t0: int, chunk: int) -> bytes:
    if case.wav is not None:
        pcm = bytearray(2 * CHUNK_SAMPLES)
        first, at = chunk * CHUNK_SAMPLES, WAV_AT_MS * RATE // 1000
        lo, hi = max(first, at), min(first + CHUNK_SAMPLES, at + len(case.wav) // 2)
        if lo < hi:
            pcm[2 * (lo - first):2 * (hi - first)] = case.wav[2 * (lo - at):2 * (hi - at)]
        return bytes(pcm)
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
    face = Timed(face_model or ScriptedFace())  # each case times its own calls
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
    rules = session.tab_rules
    scores = [r["audio"] for r in trace]
    result = {"case": case.name, "what": case.what, "expect_open": case.expect_open, "open": bool(opens),
              "max_laughter": max(scores, default=0),
              "strong_hops": sum(s >= rules.strong_audio for s in scores),
              "weak_hops": sum(rules.assisted_audio * rules.release_ratio <= s < rules.strong_audio for s in scores),
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
        onset = case.onset_ms
        if onset is None and case.onset_hop is not None:
            onset = case.onset_hop * HOP_MS + CENTRE_MS - HOP_MS // 2
        if onset is not None:
            result["onset_to_open_ms"] = round(received - t0 - onset, 1)
    result["ok"] = (bool(opens) == case.expect_open and not doubled
                    and (case.expect_hop is None or actual_hop == case.expect_hop))
    result["trace"] = trace
    return result


def _stats(values):
    if not values:
        return None
    return {"n": len(values), "median": round(statistics.median(values), 2), "max": round(max(values), 2)}


def header(rules: TabRules, audio_name: str, face_name: str) -> dict:
    return {"audio_model": audio_name, "face_model": face_name, "thresholds": {"strong_audio": rules.strong_audio, "assisted_audio": rules.assisted_audio,
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
    print(f"models: {info['audio_model']} + {info['face_model']}; strong {t['strong_audio']}, "
          f"assisted {t['assisted_audio']}, smile {t['smile']}, release x{t['release_ratio']}, "
          f"{t['window_ms']} ms window, target {t['target_s']} s; hop {HOP_MS} ms, chunk {CHUNK_MS} ms; "
          f"runs {args.runs}{', realtime' if args.realtime else ', simulated clock'}")
    print(f"{'case':30} {'expect':>6} {'actual':>6} {'hop e/a':>7} {'trig->OPEN':>10} {'onset->OPEN':>11}"
          f" {'laugh max':>9} {'strong/weak hops':>16} {'evid s':>6} {'audio ms':>8} {'face ms':>7}  ok")
    for runs in results:
        r = runs[0]
        hops = f"{'-' if r['expect_hop'] is None else r['expect_hop']}/{'-' if r['open_hop'] is None else r['open_hop']}"
        print(f"{r['case']:30} {'OPEN' if r['expect_open'] else '-':>6} {'OPEN' if r['open'] else '-':>6}"
              f" {hops:>7} {_median([x['trigger_to_open_ms'] for x in runs]):>10}"
              f" {_median([x['onset_to_open_ms'] for x in runs]):>11} {r['max_laughter']:9.3f}"
              f" {str(r['strong_hops']) + '/' + str(r['weak_hops']):>16} {r['max_evidence_s']:6.2f}"
              f" {_median([(x['audio_ms'] or {}).get('median') for x in runs], 2):>8}"
              f" {_median([(x['face_ms'] or {}).get('median') for x in runs], 2):>7}"
              f"  {'ok' if all(x['ok'] for x in runs) else 'FAIL'}")
    flat = [x for runs in results for x in runs]
    laughs = [x for x in flat if x["expect_open"]]
    others = [x for x in flat if not x["expect_open"]]
    print(f"misses: {sum(not x['open'] for x in laughs)} of {len(laughs)} runs expecting OPEN; "
          f"false positives: {sum(x['open'] for x in others)} of {len(others)} runs expecting none")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", type=int, default=1, help="repeat each case (latency statistics)")
    ap.add_argument("--realtime", action="store_true", help="send chunks at their real capture times")
    ap.add_argument("--json", action="store_true", help="print JSON instead of a table")
    ap.add_argument("--audio-model", choices=("scripted", "yamnet"), default="scripted")
    ap.add_argument("--wav", action="append", default=[], metavar="LABEL=FILE",
                    help="recorded input (mono 16 kHz PCM16); LABEL laughter expects an OPEN")
    ap.add_argument("--face-python", help="Python inside the MediaPipe environment")
    ap.add_argument("--face-task", help="face_landmarker.task")
    args = ap.parse_args(argv)
    if bool(args.wav) != (args.audio_model != "scripted"):
        ap.error("--wav goes with a real --audio-model, and a real model needs --wav inputs")
    if bool(args.face_python) != bool(args.face_task) or (args.face_task and not args.wav):
        ap.error("--face-python and --face-task go together, with --wav")

    audio_model = face_model = None
    audio_name, face_name = ScriptedAudio.name, ScriptedFace.name
    cases = CASES
    if args.wav:
        from encore_server.models import YamnetModel
        audio_model = YamnetModel()
        audio_name = (f"{audio_model.name} (tensorflow {importlib.metadata.version('tensorflow')}, "
                      f"tensorflow-hub {importlib.metadata.version('tensorflow-hub')})")
        cases = []
        for item in args.wav:
            label, _, path = item.partition("=")
            if not path:
                ap.error(f"--wav {item!r}: use LABEL=FILE")
            cases += wav_cases(label, Path(path), bool(args.face_task))
    if args.face_task:
        from encore_server.vision import FaceWorker
        version = subprocess.run([args.face_python, "-c", "import mediapipe; print(mediapipe.__version__)"],
                                 capture_output=True, text=True, check=True).stdout.strip()
        digest = hashlib.sha256(Path(args.face_task).read_bytes()).hexdigest()[:12]
        face_model = FaceWorker(args.face_python, args.face_task)
        face_name = face_model.name = f"mediapipe {version} {Path(args.face_task).name} sha256 {digest}"
    try:
        results = [[run_case(c, realtime=args.realtime, audio_model=audio_model, face_model=face_model)
                    for _ in range(args.runs)] for c in cases]
    finally:
        if face_model is not None:
            face_model.close()
    info = header(TabRules(), audio_name, face_name)
    if args.json:
        print(json.dumps({"setup": info, "runs": args.runs, "realtime": args.realtime,
                          "cases": [r for runs in results for r in runs]}, indent=1))
    else:
        print_table(info, results, args)
    return 0 if all(r["ok"] for runs in results for r in runs) else 1


if __name__ == "__main__":
    sys.exit(main())
