"""EM-10 (R1) acceptance case 2 in a real room: playback only, reaction only, both at once.

Records three takes through the computer's own speaker and microphone, then runs the
linear canceller the player uses (48 kHz, 768-sample blocks, 256 ms tail) on them and
scores the result with the server's YAMNet (laughter / scream / gasp, same triggers).

    record    plays the content and records the microphone in one duplex stream
              (sounddevice), so playback and recording share one clock and every
              microphone sample is matched to the sample being played.
    analyse   measures the echo delay, the echo removed (ERLE), whether the echo alone
              triggers a reaction, and how many reaction windows survive the canceller.
              Writes results.md next to the takes, ready to paste into EM-10.

Takes (each SECONDS long; reactions start after a 15 s warm-up so the filter learns the room first):
    echo      content plays, nobody reacts
    reaction  nothing plays, you react on the cues
    both      content plays and you react on the same cues
"mix" in the results is echo + reaction added together: sound adds linearly in the
air, so it is "both" with a known clean reaction to compare against.

macOS, from the repository root, with the YAMNet environment
(server/requirements-yamnet.txt, see docs/em-10-aec-evaluation.md) plus sounddevice:
    ~/venvs/yamnet/bin/pip install sounddevice
    ~/venvs/yamnet/bin/python client/tools/aec/room_recording.py record /tmp/em10-room
    ~/venvs/yamnet/bin/python client/tools/aec/room_recording.py analyse /tmp/em10-room
Speaker volume at a normal listening level, no headphones, laptop on a table.
``analyse --no-yamnet`` skips the model (ERLE and delay only).
"""

from __future__ import annotations

import argparse
import datetime as dt
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "client"), str(ROOT / "server")]
from encore_client.player.linear_echo import BLOCK, TAPS  # noqa: E402
from encore_client.sensing.linear_aec import LinearEchoCanceller, estimate_delay  # noqa: E402

RATE = 48_000
YAMNET_RATE, WINDOW, HOP = 16_000, 15_600, 7_680          # server/encore_server/audio.py
TRIGGER = {"laughter": 0.4, "scream": 0.4, "gasp": 0.3}   # server/encore_server/detector.py
SECONDS = 45
WARMUP_S = 15
# (start s, end s, what to do): the same cues in "reaction" and "both".
CUES = [(16, 20, "laughter", "LAUGH  (웃기)"),
        (23, 26, "scream", "SCREAM (소리 지르기)"),
        (29, 31, "gasp", "GASP   (헉 하고 놀라기)"),
        (34, 39, "laughter", "LAUGH  (웃기)")]
TAKES = ("echo", "reaction", "both")
SPEECH = ("Welcome back to the show. Tonight we have a very special guest who has travelled from "
          "far away to tell us a story about a dog, a cat, and a very confused parrot. The parrot, "
          "it turns out, had learned to answer the telephone, and every morning at seven it would "
          "tell the neighbours that nobody was home. Let us begin with the first chapter. ")


# ---- files ---------------------------------------------------------------------------------

def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path)) as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (RATE, 1, 2):
            sys.exit(f"{path}: need {RATE} Hz mono 16-bit WAV")
        return np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32768


def write_wav(path: Path, x: np.ndarray, rate: int = RATE) -> None:
    with wave.open(str(path), "wb") as w:
        w.setparams((1, 2, rate, 0, "NONE", ""))
        w.writeframes((np.clip(x, -1, 32767 / 32768) * 32768).astype("<i2").tobytes())


def make_content(path: Path, source: Path | None) -> np.ndarray:
    """The playback: ``source`` (any format afconvert reads) or macOS speech, SECONDS long."""
    with tempfile.TemporaryDirectory() as tmp:
        if source is None:
            if not shutil.which("say"):
                sys.exit("no --content given and no macOS 'say' to make one")
            source = Path(tmp) / "speech.aiff"
            subprocess.run(["say", "-o", str(source), SPEECH * 3], check=True)
        converted = Path(tmp) / "content.wav"
        subprocess.run(["afconvert", "-f", "WAVE", "-d", f"LEI16@{RATE}", "-c", "1",
                        str(source), str(converted)], check=True)
        far = read_wav(converted)
    far = np.resize(far, SECONDS * RATE) * (0.5 / max(np.abs(far).max(), 1e-6))
    write_wav(path, far)
    return far


# ---- record --------------------------------------------------------------------------------

def record(out: Path, content: Path | None) -> None:
    import sounddevice as sd

    out.mkdir(parents=True, exist_ok=True)
    far = make_content(out / "content.wav", content)
    device = sd.query_devices(kind="input")["name"], sd.query_devices(kind="output")["name"]
    (out / "devices.txt").write_text(f"input: {device[0]}\noutput: {device[1]}\n")
    print(f"microphone: {device[0]}\nspeaker:    {device[1]}\n")
    for take in TAKES:
        play = far if take != "reaction" else np.zeros_like(far)
        print(f"== take '{take}' ({SECONDS} s): " + {
            "echo": "stay quiet, the content plays",
            "reaction": "nothing plays; react on the cues",
            "both": "the content plays; react on the cues",
        }[take])
        input("   press Enter to start ")
        mic = sd.playrec(play.reshape(-1, 1), samplerate=RATE, channels=1, dtype="float32")
        start = time.monotonic()
        shown = None
        while (t := time.monotonic() - start) < SECONDS + 0.5:
            cue = next((c for c in CUES if c[0] <= t < c[1]), None) if take != "echo" else None
            text = f"   {t:4.0f} s  " + (f">>> {cue[3]} <<<" if cue else "...")
            if text != shown:
                print("\r" + text.ljust(60), end="", flush=True)
                shown = text
            time.sleep(0.05)
        sd.wait()
        print()
        write_wav(out / f"{take}.wav", mic[:, 0])
        print(f"   saved {take}.wav, peak {20 * np.log10(np.abs(mic).max() + 1e-9):.0f} dBFS\n")
    print(f"done. now: python {Path(__file__).relative_to(ROOT)} analyse {out}")


# ---- analyse -------------------------------------------------------------------------------

def cancel(mic: np.ndarray, far: np.ndarray) -> np.ndarray:
    """What the player does: 10 ms blocks through the linear canceller, output realigned."""
    aec = LinearEchoCanceller(block=BLOCK, taps=TAPS)
    out = np.concatenate([aec.process(mic[i:i + RATE // 100], far[i:i + RATE // 100])
                          for i in range(0, len(mic), RATE // 100)])
    return np.concatenate([out[BLOCK:], np.zeros(BLOCK, np.float32)])


def aligned(far: np.ndarray, delay: int, target: int = 30 * RATE // 1000) -> np.ndarray:
    """Read the reference ``delay - target`` earlier, as LinearEchoPath does past its tail."""
    shift = max(0, delay - target) if delay > 150 * RATE // 1000 else 0
    return np.concatenate([np.zeros(shift, np.float32), far[:len(far) - shift]])


def to_16k(x: np.ndarray) -> np.ndarray:
    taps = np.arange(-96, 97)
    h = np.sinc(taps / 3) * np.hamming(len(taps))
    return (np.convolve(x, h / h.sum(), "same")[::3]).astype(np.float32)


def rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(x, dtype=np.float64)) + 1e-20))


class Scorer:
    def __init__(self) -> None:
        from encore_server.models import YamnetModel
        self.model = YamnetModel()

    def __call__(self, x: np.ndarray) -> list[tuple[float, dict[str, float]]]:
        """(window centre s, scores) every 0.48 s, as the server windows the audio."""
        y = to_16k(x)
        pcm = (np.clip(y, -1, 32767 / 32768) * 32768).astype("<i2")
        return [((i + WINDOW / 2) / YAMNET_RATE, self.model(pcm[i:i + WINDOW].tobytes()))
                for i in range(0, len(pcm) - WINDOW + 1, HOP)]


def hits(scores, reaction: str, start: float, end: float) -> int:
    return sum(s[reaction] >= TRIGGER[reaction] for t, s in scores if start <= t < end)


def false_hits(scores) -> dict[str, int]:
    return {r: sum(s[r] >= level for t, s in scores if t >= WARMUP_S) for r, level in TRIGGER.items()}


def analyse(out: Path, use_yamnet: bool) -> None:
    far = read_wav(out / "content.wav")
    takes = {t: read_wav(out / f"{t}.wav")[:len(far)] for t in TAKES}
    takes["mix"] = takes["echo"] + takes["reaction"]
    lines = [f"# EM-10 acceptance case 2: room recording ({dt.date.today()})", ""]
    devices = out / "devices.txt"
    if devices.exists():
        lines += ["Devices: " + devices.read_text().strip().replace("\n", ", "), ""]

    # Echo delay from the quiet stretch of the echo take (speaker and microphone latency
    # plus the room). The player adds its audio sink's buffer on top of this.
    a, b = 2 * RATE, WARMUP_S * RATE
    found = estimate_delay(takes["echo"][a + RATE * 3 // 4:b], far[a:b], decimate=3)
    delay = found[0] if found and found[1] >= 9 else 0
    lines.append(f"Echo delay (speaker to microphone, without the player's sink buffer): "
                 + (f"{delay * 1000 / RATE:.0f} ms (confidence {found[1]:.0f})" if delay
                    else "not measurable (no clear echo)"))
    ref = aligned(far, delay)
    cleaned = {t: cancel(x, ref if t != "reaction" else np.zeros_like(ref)) for t, x in takes.items()}

    late = slice(WARMUP_S * RATE, None)
    erle = 20 * np.log10(rms(takes["echo"][late]) / rms(cleaned["echo"][late]))
    level = 20 * np.log10(rms(takes["echo"][late]) / max(rms(takes["reaction"][late]), 1e-9))
    lines += [f"Echo removed after the {WARMUP_S} s warm-up (ERLE): {erle:.1f} dB",
              f"Echo level against the reaction take: {level:+.1f} dB", ""]
    print("\n".join(lines))
    if not use_yamnet:
        (out / "results.md").write_text("\n".join(lines) + "\n")
        return

    score = Scorer()
    raw = {t: score(x) for t, x in takes.items()}
    aec = {t: score(x) for t, x in cleaned.items()}
    fe_raw, fe_aec = false_hits(raw["echo"]), false_hits(aec["echo"])
    lines += ["## Echo alone (take 'echo'): windows that trigger a reaction",
              "", "| | " + " | ".join(TRIGGER) + " |", "|---|" + "---|" * len(TRIGGER),
              "| no AEC | " + " | ".join(str(fe_raw[r]) for r in TRIGGER) + " |",
              "| linear AEC | " + " | ".join(str(fe_aec[r]) for r in TRIGGER) + " |", ""]
    lines += ["## Reactions: triggering windows during each cue", "",
              "`clean` is the reaction take with nothing playing. `kept` counts windows that trigger "
              "in clean and still trigger after AEC on the mix.", "",
              "| cue | reaction | clean | reaction take + AEC | mix, no AEC | mix + AEC (kept) | both, no AEC | both + AEC |",
              "|---|---|---|---|---|---|---|---|"]
    for start, end, reaction, _ in CUES:
        span = (start - 0.5, end + 0.5)
        clean = [(t, s) for t, s in raw["reaction"] if span[0] <= t < span[1]]
        clean_hot = {round(t, 2) for t, s in clean if s[reaction] >= TRIGGER[reaction]}
        kept = sum(round(t, 2) in clean_hot and s[reaction] >= TRIGGER[reaction]
                   for t, s in aec["mix"] if span[0] <= t < span[1])
        lines.append(f"| {start}-{end} s | {reaction} | {len(clean_hot)} | {hits(aec['reaction'], reaction, *span)} | "
                     f"{hits(raw['mix'], reaction, *span)} | {hits(aec['mix'], reaction, *span)} ({kept}) | "
                     f"{hits(raw['both'], reaction, *span)} | {hits(aec['both'], reaction, *span)} |")
    lines += ["", "Settings: linear canceller as in the player (48 kHz, 768-sample blocks, 256 ms tail), "
              "YAMNet windows 0.975 s every 0.48 s, triggers " +
              ", ".join(f"{r} {v}" for r, v in TRIGGER.items()) + ". Made by "
              f"`client/tools/aec/room_recording.py`."]
    (out / "results.md").write_text("\n".join(lines) + "\n")
    for name, x in cleaned.items():
        write_wav(out / f"{name}_aec.wav", x)
    print("\n".join(lines[lines.index("## Echo alone (take 'echo'): windows that trigger a reaction"):]))
    print(f"\nwrote {out / 'results.md'} and the *_aec.wav files")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    rec = sub.add_parser("record", help="play and record the three takes")
    rec.add_argument("out", type=Path)
    rec.add_argument("--content", type=Path, help="playback audio (default: macOS speech)")
    ana = sub.add_parser("analyse", help="cancel the echo and score the takes")
    ana.add_argument("out", type=Path)
    ana.add_argument("--no-yamnet", action="store_true")
    args = parser.parse_args()
    if args.command == "record":
        record(args.out, args.content)
    else:
        analyse(args.out, not args.no_yamnet)


if __name__ == "__main__":
    main()
