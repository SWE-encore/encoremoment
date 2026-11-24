"""EM-10 (R1) feasibility: can pywebrtc-audio (WebRTC AEC3, no GStreamer plugin) remove
playback echo while keeping the audience's own reaction?

Synthetic, reproducible input at 16 kHz mono:
  far   = "content" playback: harmonic speech-like signal with syllable-rate envelope + noise
  echo  = far through a short room response, delayed and attenuated (what the mic hears)
  near  = audience "laughter": noise bursts pulsing at ~5 Hz (laugh-like), 3.0-4.5 s and 6.5-7.5 s
Three conditions, as in the EM-10 acceptance criteria:
  echo only, reaction only, both at once (double talk)
Metrics, same thresholds as client/tests/test_echo_capture.py:
  ERLE  = echo attenuation in dB on echo-only stretches after convergence   (test: > 3 dB)
  near_gain = RMS(out) / RMS(near) where only the reaction is present       (test: > 0.15)
  corr  = correlation of the output with the clean reaction (how much of it survives)

Run:  .venv/bin/python -m pip install pywebrtc-audio
      .venv/bin/python client/tools/aec/synthetic_pywebrtc.py
"""

from __future__ import annotations

import sys
import time

import numpy as np

try:
    from pywebrtc_audio import AudioProcessor
except ImportError:
    sys.exit("pywebrtc-audio is not installed: .venv/bin/python -m pip install pywebrtc-audio")

RATE = 16_000
SECONDS = 9.0
FRAME = RATE // 100          # 10 ms, the unit AEC3 works in
CHUNK = RATE // 2            # 0.5 s, the client's audio chunk size
CONVERGE_S = 1.5             # ignore the first 1.5 s while AEC3 adapts
REACTION = [(3.0, 4.5), (6.5, 7.5)]
rng = np.random.default_rng(406)


def content(n: int) -> np.ndarray:
    t = np.arange(n) / RATE
    pitch = 140 + 30 * np.sin(2 * np.pi * 0.7 * t)
    phase = 2 * np.pi * np.cumsum(pitch) / RATE
    voiced = sum(np.sin(k * phase) / k for k in range(1, 12))
    syllables = 0.5 * (1 + np.sin(2 * np.pi * 4 * t)) ** 2
    return (0.25 * voiced * syllables + 0.02 * rng.standard_normal(n)).astype(np.float32)


def room(far: np.ndarray, delay_ms: int) -> np.ndarray:
    ir = np.zeros(int((delay_ms + 120) * RATE / 1000))
    d = int(delay_ms * RATE / 1000)
    ir[d] = 0.6
    for k, g in ((d + 90, 0.25), (d + 310, 0.12), (d + 700, 0.06), (d + 1400, 0.03)):
        ir[k] = g
    return np.convolve(far, ir)[: len(far)].astype(np.float32)


def laughter(n: int) -> np.ndarray:
    t = np.arange(n) / RATE
    out = np.zeros(n, dtype=np.float32)
    for a, b in REACTION:
        m = (t >= a) & (t < b)
        burst = 0.5 * (1 + np.sign(np.sin(2 * np.pi * 5 * t[m])))          # ~5 "ha" per second
        tone = np.sin(2 * np.pi * 300 * t[m]) + 0.5 * np.sin(2 * np.pi * 900 * t[m])
        out[m] = 0.3 * burst * (0.6 * tone + 0.6 * rng.standard_normal(m.sum()))
    return out


def mask(n: int, inside: bool) -> np.ndarray:
    t = np.arange(n) / RATE
    m = np.zeros(n, dtype=bool)
    for a, b in REACTION:
        m |= (t >= a) & (t < b)
    keep = t >= CONVERGE_S
    return (m if inside else ~m) & keep


def rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(x, dtype=np.float64)) + 1e-12))


def run(mic: np.ndarray, far: np.ndarray, *, aec: bool, delay_hint: int) -> tuple[np.ndarray, float]:
    ap = AudioProcessor(sample_rate=RATE, echo_cancellation=aec, stream_delay_ms=delay_hint)
    out = np.empty_like(mic)
    started = time.perf_counter()
    for i in range(0, len(mic), CHUNK):           # stream it the way the client sends audio
        near, ref = mic[i:i + CHUNK], far[i:i + CHUNK]
        out[i:i + CHUNK] = ap.process(near, ref) if aec else ap.process(near)
    per_chunk_ms = (time.perf_counter() - started) * 1000 / (len(mic) / CHUNK)
    return out, per_chunk_ms


def main() -> None:
    n = int(SECONDS * RATE)
    far = content(n)
    near = laughter(n)
    rows = []
    for delay_ms in (40, 120):
        echo = room(far, delay_ms)
        cases = {"echo only": echo, "reaction only": near, "double talk": echo + near}
        for name, mic in cases.items():
            for hint in (0, delay_ms):
                out, cost = run(mic, far, aec=True, delay_hint=hint)
                quiet = mask(n, inside=False)
                loud = mask(n, inside=True)
                erle = 20 * np.log10(rms(mic[quiet]) / rms(out[quiet])) if name != "reaction only" else float("nan")
                if name == "echo only":
                    gain = corr = float("nan")
                else:
                    gain = rms(out[loud]) / rms(near[loud])
                    corr = float(np.corrcoef(out[loud], near[loud])[0, 1])
                rows.append((delay_ms, hint, name, erle, gain, corr, cost))

    print(f"pywebrtc-audio AEC3, {RATE} Hz, {SECONDS:.0f} s, 0.5 s chunks, after {CONVERGE_S} s convergence\n")
    print(f"{'room delay':>10} {'hint':>5}  {'condition':<14} {'ERLE dB':>8} {'near_gain':>9} {'corr':>6} {'ms/chunk':>9}")
    for d, h, name, erle, gain, corr, cost in rows:
        f = lambda v, p: "   -" if np.isnan(v) else f"{v:{p}}"
        print(f"{d:>8}ms {h:>4}  {name:<14} {f(erle, '8.1f'):>8} {f(gain, '9.2f'):>9} {f(corr, '6.2f'):>6} {cost:>9.2f}")

    echo_rows = [r for r in rows if r[2] != "reaction only"]
    near_rows = [r for r in rows if r[2] != "echo only"]
    ok_erle = all(r[3] > 3 for r in echo_rows)
    ok_near = all(r[4] > 0.15 for r in near_rows)
    print(f"\nERLE > 3 dB in every echo case: {'PASS' if ok_erle else 'FAIL'}")
    print(f"near_gain > 0.15 in every reaction case: {'PASS' if ok_near else 'FAIL'}")
    print("(thresholds from client/tests/test_echo_capture.py::test_synthetic_echo_mixture_and_seek)")


if __name__ == "__main__":
    main()
