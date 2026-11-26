"""EM-10 (R1): which echo canceller keeps laughter detectable? Judged by YAMNet.

Uses the server's own scoring (server/encore_server/models.py YamnetModel, 0.975 s windows
every 0.48 s, laughter trigger 0.4). The microphone signal is built here: the playback
through a room response (60 ms, early reflections), scaled to -6/0/+6 dB against the
laughter, plus the laughter. 15 s of playback come first so the cancellers adapt.

Each processor's fixed output latency is removed before scoring (WebRTC AEC3 8 ms, ours
16 ms): YAMNet triggers flip under small shifts (a plain 16 ms shift keeps 61 of 79).

Run from the repository root with the YAMNet environment (server/requirements-yamnet.txt
plus pywebrtc-audio):
    python client/tools/aec/yamnet_compare.py LAUGH.wav CONTENT.wav
Both files 16 kHz mono 16-bit; client/tools/aec/make_inputs.sh makes them on macOS.
"""

import sys
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "client"), str(ROOT / "server")]
from encore_client.sensing.linear_aec import LinearEchoCanceller  # noqa: E402

RATE, WINDOW, HOP, THRESHOLD, DELAY_MS, WARMUP_S = 16_000, 15_600, 7_680, 0.4, 60, 15.0
LATENCY = {"WebRTC AEC3": 128, "linear (ours)": 256}      # samples


def read(path):
    with wave.open(str(path)) as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (RATE, 1, 2):
            sys.exit(f"{path}: need 16 kHz mono 16-bit WAV")
        return np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32768


def room(far):
    d = DELAY_MS * RATE // 1000
    ir = np.zeros(d + 1500)
    for k, g in ((d, .6), (d + 90, .25), (d + 310, .12), (d + 700, .06), (d + 1400, .03)):
        ir[k] = g
    return np.convolve(far, ir)[: len(far)].astype(np.float32)


def rms(x):
    return float(np.sqrt(np.mean(np.square(x, dtype=np.float64)) + 1e-12))


def pcm(x):
    return (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()


def make(kind):
    if kind == "WebRTC AEC3":
        from pywebrtc_audio import AudioProcessor
        return AudioProcessor(sample_rate=RATE, echo_cancellation=True)
    return LinearEchoCanceller()


def stream(kind, mic, far):
    proc = make(kind)
    out = np.concatenate([proc.process(mic[i:i + RATE // 2], far[i:i + RATE // 2])
                          for i in range(0, len(mic), RATE // 2)])
    lag = LATENCY[kind]
    return np.concatenate([out[lag:], np.zeros(lag, np.float32)])


def main():
    from encore_server.models import YamnetModel
    model = YamnetModel()

    def scores(x):
        return np.array([model(pcm(x[i:i + WINDOW]))["laughter"] for i in range(0, len(x) - WINDOW + 1, HOP)])

    laugh, content = read(sys.argv[1]), read(sys.argv[2])
    warm = int(WARMUP_S * RATE)
    far = np.resize(content, warm + len(laugh))
    laugh_t = np.concatenate([np.zeros(warm, np.float32), laugh])
    echo = room(far)
    cut = warm // HOP * HOP
    base = scores(laugh_t[cut:])
    hot = base >= THRESHOLD
    print(f"clean laugh: {hot.sum()} of {len(base)} windows trigger, mean {base[hot].mean():.2f} "
          f"(after {WARMUP_S:.0f} s warm-up)\n")
    print(f"{'echo':>5}  {'processor':<13} {'kept':>8} {'mean':>5} {'ERLE dB':>8} {'false':>6}")

    def row(label, kind, s, erle, false):
        print(f"{label:>5}  {kind:<13} {int((s[hot] >= THRESHOLD).sum()):>3} of {hot.sum():<3} "
              f"{s[hot].mean():>5.2f} {erle:>8} {false:>6}")

    for echo_db in (-6, 0, 6):
        e = echo * (rms(laugh) / rms(echo[warm:])) * 10 ** (echo_db / 20)
        mic = e + laugh_t
        label = f"{echo_db:+d}dB"
        row(label, "none (raw)", scores(mic[cut:]), "0.0", int((scores(e[cut:]) >= THRESHOLD).sum()))
        for kind in LATENCY:
            out, echo_out = stream(kind, mic, far), stream(kind, e, far)
            erle = f"{20 * np.log10(rms(e[warm:]) / rms(echo_out[warm:])):.1f}"
            row(label, kind, scores(out[cut:]), erle, int((scores(echo_out[cut:]) >= THRESHOLD).sum()))
    for kind in LATENCY:
        row("none", kind, scores(stream(kind, laugh_t, far)[cut:]), "-", "-")
    delayed = np.concatenate([np.zeros(256, np.float32), laugh_t[:-256]])
    row("none", "16ms delay", scores(delayed[cut:]), "-", "-")
    print("\nnone = no echo (headphones). false = windows where the echo alone triggers laughter.")


if __name__ == "__main__":
    main()
