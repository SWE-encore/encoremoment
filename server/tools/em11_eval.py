#!/usr/bin/env python3
"""EM-11 (R2): how well the server recognises laughter, scream and gasp on clips it was never tuned on.

Data: the FSD50K eval split (Fonseca et al., IEEE/ACM TASLP 2022; Zenodo record 4060432).
Its clips are Freesound sounds by uploaders who are not in FSD50K's dev split, and they are
not from AudioSet (YouTube), which YAMNet was trained on. Nothing is committed: labels and
clips live in a cache directory, and only the numbers go into docs/em-11-evaluation.md.

    # 1. labels: FSD50K.ground_truth.zip (334.7 kB) from https://zenodo.org/records/4060432,
    #    unzipped into CACHE/labels/ (gives CACHE/labels/FSD50K.ground_truth/eval.csv)
    # 2. pick the clips and fetch them one WAV at a time from the Hugging Face mirror
    python server/tools/em11_eval.py fetch --cache ~/.cache/encore/fsd50k-eval
    # 3. score them through the real server path (needs server/requirements-yamnet.txt)
    TFHUB_CACHE_DIR=~/.cache/encore/tfhub python server/tools/em11_eval.py run \\
        --cache ~/.cache/encore/fsd50k-eval --json results.json

Levels are chosen on the dev split, never on eval: ``fetch --split dev`` into another cache,
then ``tune`` scans levels and rules on those clips (docs/em-11-evaluation.md, section 5).

Each clip goes through ``ServerSession`` (server/encore_server/session.py) as a PC would send
it: 0.5 s audio chunks, microphone generation 1, camera off. Everything after the socket is
production code: admission, 0.975 s windows every 0.48 s, the model and the detector. Two
server set-ups are scored, because laughter goes through different rules in each:

- ``audio-only``: no face model, ``DetectorConfig`` levels (``detector-v3``: laughter .25, scream .2,
  gasp .3; ``--detector detector-v1`` or ``detector-v2`` runs the earlier levels);
- ``face-server``: started with a face model (the demo set-up), camera off: laughter opens on
  sound alone at ``TabRules.strong_audio`` (.25). Scream and gasp are the same in both.

Conditions: ``clean`` (2 s of silence, the clip, 3 s of silence) for every clip, and, for the
three reaction groups, the clip over playback: a speech or a music clip from the same set,
looped from 2 s before the reaction to 3 s after it, at +10 dB and 0 dB reaction-to-playback
level (active levels: 10 ms frames within 30 dB of the loudest). +10 dB stands for playback
the echo canceller mostly removed; 0 dB for none removed.

Delay is from the reaction's onset (the first 10 ms frame within 20 dB of the clip's loudest)
to OPEN on the simulated clock: a chunk reaches the server when its last sample is captured,
plus the time ``on_audio`` took (no network). FSD50K labels are per clip, not per moment, so
a clip may hold other sound before the labelled reaction; the delay is then an upper bound.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
import statistics
import struct
import subprocess
import sys
import time
import urllib.request
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from encore_server.audio import WINDOW_MS  # noqa: E402
from encore_server.detector import REACTIONS, DetectorConfig  # noqa: E402
from encore_server.fusion import TabRules  # noqa: E402
from encore_server.session import ServerSession  # noqa: E402

RATE = 16_000
CHUNK_SAMPLES = RATE // 2  # the PC sends 0.5 s chunks
T0 = 10_000  # capture clock at the first sample
LEAD_MS, TAIL_MS = 2_000, 3_000
SNRS = (10, 0)
SEED = 11
MIRROR = "https://huggingface.co/datasets/Fhrozen/FSD50k/resolve/main/clips/{split}/{fname}.wav"
SPLITS = ("eval", "dev")  # dev is for tuning; eval is only ever measured

LAUGH = {"Laughter", "Giggle", "Chuckle_and_chortle"}
REACTION_LABELS = LAUGH | {"Screaming", "Gasp"}
# Group -> (any of these labels, none of these, the reaction it should open). Checked in this
# order and a clip joins the first group it fits, so the groups never share a clip.
GROUPS = {
    "laughter": (LAUGH, {"Screaming", "Gasp", "Music"}, "laughter"),
    "scream": ({"Screaming"}, LAUGH | {"Gasp", "Music"}, "scream"),
    "gasp": ({"Gasp"}, LAUGH | {"Screaming", "Music"}, "gasp"),
    "speech": ({"Speech"}, REACTION_LABELS | {"Shout", "Yell", "Music"}, None),
    "music": ({"Music"}, {"Human_voice"}, None),
    "shout_yell": ({"Shout", "Yell"}, REACTION_LABELS | {"Music"}, None),
    "breath_cough": ({"Breathing", "Cough", "Sigh", "Sneeze"}, REACTION_LABELS | {"Music"}, None),
}
# What ``run --detector`` measures: the server's current settings, or earlier ones
# (detector-v1: what PR #50 measured; detector-v2: PR #51).
DETECTORS = {
    "current": DetectorConfig(),
    "detector-v1": DetectorConfig(name="detector-v1", trigger={"laughter": 0.4, "scream": 0.4, "gasp": 0.3}),
    "detector-v2": DetectorConfig(name="detector-v2", trigger={"laughter": 0.25, "scream": 0.4, "gasp": 0.3}),
}
# Freesound ids of the ESC-50 clips EM-26 already ran (docs/em-26-trigger-check.md): kept out.
SEEN_IDS = {"104089", "1791", "187207", "19111", "30039", "30043", "33658", "36164"}


# ---- clip selection and download --------------------------------------------------------

def read_labels(path: Path) -> dict[str, set[str]]:
    with path.open(newline="") as f:
        return {row["fname"]: set(row["labels"].split(",")) for row in csv.DictReader(f)}


def candidates(labels: dict[str, set[str]], seed: int = SEED) -> dict[str, list[str]]:
    """Every eligible clip per group, in a fixed shuffled order."""
    pools: dict[str, list[str]] = {g: [] for g in GROUPS}
    for fname in sorted(labels, key=int):
        if fname in SEEN_IDS:
            continue
        for group, (anyof, noneof, _) in GROUPS.items():
            if labels[fname] & anyof and not labels[fname] & noneof:
                pools[group].append(fname)
                break
    for group, ids in pools.items():
        random.Random(f"{seed}-{group}").shuffle(ids)
    return pools


def to_16k(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".tmp.wav")
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(src),
                    "-ac", "1", "-ar", str(RATE), "-sample_fmt", "s16", str(tmp)], check=True)
    tmp.replace(dst)


def fetch(cache: Path, per_group: int, max_clip: int, max_total: int, seed: int = SEED, *,
          split: str = "eval", labels_dir: Path | None = None) -> dict:
    labels = read_labels((labels_dir or cache / "labels" / "FSD50K.ground_truth") / f"{split}.csv")
    raw_dir = cache / "clips"
    raw_dir.mkdir(parents=True, exist_ok=True)
    total = sum(p.stat().st_size for p in raw_dir.glob("*.wav"))
    manifest: dict[str, list[str]] = {}
    skipped = []
    for group, ids in candidates(labels, seed).items():
        got = manifest.setdefault(group, [])
        for fname in ids:
            if len(got) == per_group:
                break
            raw = raw_dir / f"{fname}.wav"
            if not raw.exists():
                req = urllib.request.Request(MIRROR.format(split=split, fname=fname), headers={"User-Agent": "encore-eval/1"})
                with urllib.request.urlopen(req, timeout=60) as resp:
                    size = int(resp.headers.get("Content-Length") or 0)
                    if not size or size > max_clip:
                        skipped.append((fname, size))
                        continue
                    if total + size > max_total:
                        print(f"stopping: {total + size} bytes would pass the {max_total} cap", file=sys.stderr)
                        break
                    data = resp.read(max_clip + 1)
                if len(data) != size or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
                    skipped.append((fname, len(data)))
                    continue
                tmp = raw.with_suffix(".part")
                tmp.write_bytes(data)
                tmp.replace(raw)
                total += size
            to_16k(raw, cache / "16k" / f"{fname}.wav")
            got.append(fname)
        print(f"{group}: {len(got)} clips", file=sys.stderr)
    info = {"split": split, "seed": seed, "per_group": per_group, "max_clip_bytes": max_clip, "bytes": total,
            "groups": manifest, "skipped_too_large": skipped}
    (cache / "manifest.json").write_text(json.dumps(info, indent=1))
    return info


# ---- signals ----------------------------------------------------------------------------

def read_pcm(path: Path) -> np.ndarray:
    with wave.open(str(path)) as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (RATE, 1, 2):
            raise ValueError(f"{path}: expected 16 kHz mono PCM16")
        return np.frombuffer(w.readframes(w.getnframes()), "<i2").copy()


def _frame_db(pcm: np.ndarray, frame: int = RATE // 100) -> np.ndarray:
    n = len(pcm) // frame
    x = pcm[: n * frame].astype(np.float64).reshape(n, frame) / 32768
    return 10 * np.log10(np.maximum((x ** 2).mean(axis=1), 1e-12))


def onset_ms(pcm: np.ndarray, below_peak_db: float = 20) -> int:
    """Start of the first 10 ms frame within ``below_peak_db`` of the loudest frame."""
    db = _frame_db(pcm)
    if not len(db):
        return 0
    return int(np.argmax(db >= db.max() - below_peak_db)) * 10


def active_rms(pcm: np.ndarray, below_peak_db: float = 30) -> float:
    frame = RATE // 100
    db = _frame_db(pcm, frame)
    if not len(db):
        return 0.0
    keep = np.repeat(db >= db.max() - below_peak_db, frame)
    x = pcm[: len(keep)].astype(np.float64)[keep]
    return float(np.sqrt((x ** 2).mean())) if len(x) else 0.0


def clean_stream(clip: np.ndarray) -> np.ndarray:
    lead, tail = np.zeros(RATE * LEAD_MS // 1000, np.int16), np.zeros(RATE * TAIL_MS // 1000, np.int16)
    return np.concatenate([lead, clip, tail])


def mixed_stream(clip: np.ndarray, background: np.ndarray, snr_db: float) -> np.ndarray:
    """``clip`` over ``background`` (looped) at ``snr_db`` reaction-to-playback active level."""
    lead = RATE * LEAD_MS // 1000
    n = lead + len(clip) + RATE * TAIL_MS // 1000
    back = np.resize(background.astype(np.float64), n)
    level = active_rms(background)
    gain = active_rms(clip) / level / 10 ** (snr_db / 20) if level else 0.0
    mix = back * gain
    mix[lead:lead + len(clip)] += clip
    peak = np.abs(mix).max()
    if peak > 32_000:  # scale the whole mixture: the ratio stays
        mix *= 32_000 / peak
    return np.round(mix).astype(np.int16)


# ---- the server path --------------------------------------------------------------------

class CachedModel:
    """Runs the model once per distinct window; a repeat costs the time the first call took."""

    def __init__(self, model):
        self.model, self.name = model, model.name
        self._cache: dict[bytes, tuple[dict, float]] = {}
        self.calls_ms: list[float] = []
        self.replayed_ms = 0.0

    def __call__(self, pcm: bytes) -> dict[str, float]:
        key = hashlib.blake2b(pcm, digest_size=16).digest()
        hit = self._cache.get(key)
        if hit is not None:
            self.replayed_ms += hit[1]
            return dict(hit[0])
        started = time.perf_counter()
        scores = self.model(pcm)
        ms = (time.perf_counter() - started) * 1000
        self.calls_ms.append(ms)
        self._cache[key] = (dict(scores), ms)
        return scores


class _UnusedFace:
    """Starts the server in the demo set-up (TabRules for laughter). The camera stays off."""

    name = "no-camera"

    def __call__(self, rgb):
        raise AssertionError("the camera is off in this evaluation")


def _packet(chunk: int, pcm: bytes) -> bytes:
    header = json.dumps(dict(schema="encore.v1", session_id="em11", seq=chunk + 1, kind="audio_chunk",
                             capture_ms=T0 + chunk * 500, generation=1, chunk=chunk,
                             samples=CHUNK_SAMPLES, encoding="pcm_s16le", channels=1,
                             sample_rate=RATE, aec="off")).encode()
    return struct.pack("!I", len(header)) + header + pcm


def run_stream(pcm: np.ndarray, model: CachedModel, *, face_server: bool,
               config: DetectorConfig | None = None) -> tuple[list, list]:
    """Feed one stream as PC chunks. Returns (OPEN events with their arrival time, frames)."""
    frames = []
    session = ServerSession("em11", model, config, face_model=_UnusedFace() if face_server else None,
                            on_frame=frames.append)
    session.on_text(json.dumps(dict(schema="encore.v1", session_id="em11", kind="control_update",
                                    sensor="microphone", generation=1, enabled=True, seq=0,
                                    capture_ms=T0, idempotency_key="microphone-1")))
    opens = []
    chunks = math.ceil(len(pcm) / CHUNK_SAMPLES)
    padded = np.zeros(chunks * CHUNK_SAMPLES, np.int16)
    padded[: len(pcm)] = pcm
    for c in range(chunks):
        model.replayed_ms = 0.0
        started = time.perf_counter()
        out = session.on_audio(_packet(c, padded[c * CHUNK_SAMPLES:(c + 1) * CHUNK_SAMPLES].tobytes()))
        took = (time.perf_counter() - started) * 1000 + model.replayed_ms
        arrived = T0 + (c + 1) * 500 + took
        opens += [(m["event"], arrived) for m in out
                  if m.get("kind") == "event_update" and m["event"]["phase"] == "OPEN"]
    session.end()
    return opens, frames


def judge(opens: list, frames: list, start_ms: int, length_ms: int, onset: int, expect: str | None) -> dict:
    """Which reactions opened while the clip played (any window touching it), and how soon."""
    first, last = T0 + start_ms - WINDOW_MS // 2, T0 + start_ms + length_ms + WINDOW_MS // 2
    during, outside = [], []
    for ev, arrived in opens:
        (during if first <= ev["capture"]["triggers_ms"][0] <= last else outside).append((ev, arrived))
    opened = sorted({ev["reaction"]["type"] for ev, _ in during})
    row = {"opened": opened, "outside": [ev["reaction"]["type"] for ev, _ in outside],
           "hit": expect in opened if expect else None, "delay_ms": None,
           "peak": {r: round(max((f.scores.get(r, 0.0) for f in frames
                                  if f.end_ms > T0 + start_ms and f.start_ms < T0 + start_ms + length_ms),
                                 default=0.0), 3) for r in REACTIONS}}
    if row["hit"]:
        arrived = min(a for ev, a in during if ev["reaction"]["type"] == expect)
        row["delay_ms"] = round(arrived - (T0 + start_ms + onset), 1)
        row["source"] = next(ev["source"] for ev, _ in during if ev["reaction"]["type"] == expect)
    return row


@dataclass
class Clip:
    fname: str
    group: str
    expect: str | None
    pcm: np.ndarray
    onset: int

    @property
    def length_ms(self) -> int:
        return len(self.pcm) * 1000 // RATE


def load_clips(cache: Path) -> list[Clip]:
    manifest = json.loads((cache / "manifest.json").read_text())
    clips = []
    for group, ids in manifest["groups"].items():
        for fname in ids:
            pcm = read_pcm(cache / "16k" / f"{fname}.wav")
            clips.append(Clip(fname, group, GROUPS[group][2], pcm, onset_ms(pcm)))
    return clips


def evaluate(clips: list[Clip], model, *, snrs=SNRS, config: DetectorConfig | None = None,
             progress=None) -> list[dict]:
    cached = model if isinstance(model, CachedModel) else CachedModel(model)
    backgrounds = {g: [c for c in clips if c.group == g] for g in ("speech", "music")}
    rows = []
    for i, clip in enumerate(clips):
        streams = [("clean", None, None, clean_stream(clip.pcm))]
        if clip.expect:
            for bg_group, pool in backgrounds.items():
                if not pool:
                    continue
                bg = pool[i % len(pool)]
                for snr in snrs:
                    streams.append((f"{bg_group}{snr:+d}dB", bg_group, snr, mixed_stream(clip.pcm, bg.pcm, snr)))
        for condition, bg_group, snr, pcm in streams:
            for setup in ("audio-only", "face-server"):
                opens, frames = run_stream(pcm, cached, face_server=setup == "face-server", config=config)
                row = judge(opens, frames, LEAD_MS, clip.length_ms, clip.onset, clip.expect)
                row.update(fname=clip.fname, group=clip.group, expect=clip.expect, condition=condition,
                           background=bg_group, snr_db=snr, setup=setup, length_ms=clip.length_ms,
                           onset_ms=clip.onset, background_s=round((LEAD_MS + TAIL_MS) / 1000, 1))
                rows.append(row)
        if progress:
            progress(i + 1, len(clips))
    return rows


# ---- tuning on the dev split ------------------------------------------------------------
#
# Settings are chosen here on dev clips only, then measured once on eval with ``run``.
# A clip opens a reaction when any server window touching it passes (the detector opens on
# the first window at the level, and nothing is open before a clean clip).

LEVELS = (.1, .15, .2, .25, .3, .35, .4, .5)
RIVAL_SETS = {
    "none": (),
    "Breathing": ("Breathing",),
    "Breathing, Sigh, Sneeze": ("Breathing", "Sigh", "Sneeze"),
    "all respiratory": ("Breathing", "Wheeze", "Snoring", "Pant", "Snort", "Cough", "Throat clearing",
                        "Sneeze", "Sniff", "Sigh"),
}
RIVAL_RATIOS = (.5, 1.0, 1.5, 2.0)  # the reaction counts only while it is >= ratio x its strongest rival
MAX_FALSE = .10  # no other group of sounds may open the reaction on more than 10 % of its clips


class AllScores:
    """The real YAMNet wrapper, also keeping every class score of every window it scores."""

    def __init__(self, yamnet):
        self.yamnet, self.name, self.replayed_ms = yamnet, yamnet.name, 0.0
        self.vectors: list[np.ndarray] = []

    def __call__(self, pcm: bytes) -> dict[str, float]:
        vector = self.yamnet.class_scores(pcm)
        self.vectors.append(vector)
        return self.yamnet.from_class_scores(vector)


def clip_class_scores(clip: Clip, yamnet) -> np.ndarray:
    """Every class score of each server window that touches the clip, clip played alone."""
    model = AllScores(yamnet)
    _, frames = run_stream(clean_stream(clip.pcm), model, face_server=False)
    start, end = T0 + LEAD_MS, T0 + LEAD_MS + clip.length_ms
    return np.array([v for f, v in zip(frames, model.vectors, strict=True)
                     if f.end_ms > start and f.start_ms < end], np.float32).reshape(-1, len(yamnet.labels))


def clip_opens(windows: np.ndarray, labels: list[str], reaction: str, level: float,
          rivals: tuple[str, ...] = (), ratio: float = 1.0) -> bool:
    from encore_server.models import YAMNET_CLASSES

    score = windows[:, [labels.index(n) for n in YAMNET_CLASSES[reaction]]].max(axis=1, initial=0)
    passes = score >= level
    if rivals:
        passes &= score >= ratio * windows[:, [labels.index(n) for n in rivals]].max(axis=1, initial=0)
    return bool(passes.any())


def scan(windows: dict[str, np.ndarray], groups: dict[str, list[str]], labels: list[str],
         reaction: str, rules: list[tuple[str, float]]) -> list[dict]:
    """Every (rival set, ratio) x level: share of target clips found, false opens per other group."""
    target = next(g for g, (_, _, expect) in GROUPS.items() if expect == reaction)
    rows = []
    for rule, (rival_name, ratio) in enumerate(rules):
        for level in LEVELS:
            hit = {g: sum(clip_opens(windows[f], labels, reaction, level, RIVAL_SETS[rival_name], ratio) for f in ids)
                   for g, ids in groups.items()}
            false = {g: hit[g] / len(ids) for g, ids in groups.items() if g != target and ids}
            rows.append(dict(reaction=reaction, rule=rule, rivals=rival_name, ratio=ratio if rival_name != "none" else None,
                             level=level, found=hit[target], of=len(groups[target]), opened=hit,
                             worst_false=max(false.values()), allowed=max(false.values()) <= MAX_FALSE))
    return rows


def choose(rows: list[dict]) -> dict | None:
    """Most target clips found with every other group at or under MAX_FALSE; then fewest false
    opens; then the simplest rule (no rival first, then the most lenient ratio); then the highest
    level, the one furthest from opening on sounds dev did not have."""
    allowed = [r for r in rows if r["allowed"]]
    if not allowed:
        return None
    false = lambda r: sum(v for g, v in r["opened"].items() if GROUPS[g][2] != r["reaction"])  # noqa: E731
    return max(allowed, key=lambda r: (r["found"], -false(r), -r["rule"], r["level"]))


def tune(cache: Path, yamnet) -> dict:
    manifest = json.loads((cache / "manifest.json").read_text())
    clips = load_clips(cache)
    windows = {c.fname: clip_class_scores(c, yamnet) for c in clips}
    groups = manifest["groups"]
    laughter = scan(windows, groups, yamnet.labels, "laughter", [("none", 1.0)])
    scream = scan(windows, groups, yamnet.labels, "scream", [("none", 1.0)])
    gasp = scan(windows, groups, yamnet.labels, "gasp",
                [("none", 1.0)] + [(name, r) for name in RIVAL_SETS if name != "none" for r in RIVAL_RATIOS])
    return {"split": manifest.get("split"), "clips": {g: len(ids) for g, ids in groups.items()},
            "max_false": MAX_FALSE, "windows": int(sum(len(w) for w in windows.values())),
            "best": {r: choose(rows) for r, rows in (("laughter", laughter), ("scream", scream), ("gasp", gasp))},
            "scan": laughter + scream + gasp}


def tune_report(result: dict) -> str:
    groups = list(result["clips"])
    head = ["| Rule | Level | Found | " + " | ".join(f"{g} opened" for g in groups) + " |",
            "|---|---|---|" + "---|" * len(groups)]

    def line(r):
        rule = "no rival" if r["rivals"] == "none" else f"{r['ratio']:g} x {r['rivals']}"
        mark = " **best under the limit**" if r == result["best"][r["reaction"]] else ""
        cells = [_pct(r["opened"][g], result["clips"][g]) for g in groups]
        return f"| {rule}{mark} | {r['level']:g} | {_pct(r['found'], r['of'])} | " + " | ".join(cells) + " |"

    lines = []
    for reaction in REACTIONS:
        rows = [r for r in result["scan"] if r["reaction"] == reaction]
        lines += [f"\n### {reaction}: every level, no rival\n", *head,
                  *[line(r) for r in rows if r["rivals"] == "none"]]
        ruled = [r for r in rows if r["rivals"] != "none"]
        if ruled:
            best = []
            for key in dict.fromkeys((r["rivals"], r["ratio"]) for r in ruled):
                pick = choose([r for r in ruled if (r["rivals"], r["ratio"]) == key])
                if pick:
                    best.append(pick)
            lines += [f"\n### {reaction} with a rival: best allowed level per rule "
                      f"(every other group at or under {result['max_false']:.0%})\n", *head, *[line(r) for r in best]]
    return "\n".join(lines)


# ---- reports ----------------------------------------------------------------------------

def _pct(k: int, n: int) -> str:
    return f"{k}/{n} ({100 * k / n:.0f}%)" if n else "—"


def _delays(values: list[float]) -> str:
    if not values:
        return "—"
    values = sorted(values)
    p90 = values[min(len(values) - 1, math.ceil(0.9 * len(values)) - 1)]
    return f"{statistics.median(values) / 1000:.2f} / {p90 / 1000:.2f} s"


def report(rows: list[dict]) -> str:
    lines = []
    for setup in ("audio-only", "face-server"):
        lines += [f"\n### {setup}, clean (clip alone, silence around it)\n",
                  "| Group | Clips | laughter opened | scream opened | gasp opened | any | Delay median / p90 |",
                  "|---|---|---|---|---|---|---|"]
        for group in GROUPS:
            sel = [r for r in rows if r["setup"] == setup and r["condition"] == "clean" and r["group"] == group]
            if not sel:
                continue
            n = len(sel)
            cells = [_pct(sum(r in x["opened"] for x in sel), n) for r in REACTIONS]
            lines.append(f"| {group} | {n} | " + " | ".join(cells) + f" | {_pct(sum(bool(x['opened']) for x in sel), n)}"
                         f" | {_delays([x['delay_ms'] for x in sel if x['delay_ms'] is not None])} |")
        mixed = [r for r in rows if r["setup"] == setup and r["condition"] != "clean"]
        if mixed:
            lines += [f"\n### {setup}, over playback\n",
                      "| Group | Playback | Level | Detected | Other reaction opened | Delay median / p90 | Opened during playback alone |",
                      "|---|---|---|---|---|---|---|"]
            for group, (_, _, expect) in GROUPS.items():
                for bg in ("speech", "music"):
                    for snr in sorted({r["snr_db"] for r in mixed}, reverse=True):
                        sel = [r for r in mixed if r["group"] == group and r["background"] == bg and r["snr_db"] == snr]
                        if not sel:
                            continue
                        n = len(sel)
                        other = sum(any(o != expect for o in x["opened"]) for x in sel)
                        alone = sum(len(x["outside"]) for x in sel)
                        lines.append(f"| {group} | {bg} | {snr:+d} dB | {_pct(sum(x['hit'] for x in sel), n)} | "
                                     f"{_pct(other, n)} | {_delays([x['delay_ms'] for x in sel if x['delay_ms'] is not None])} | "
                                     f"{alone} in {n * (LEAD_MS + TAIL_MS) / 1000:.0f} s |")
    clean = [r for r in rows if r["setup"] == "audio-only" and r["condition"] == "clean"]
    if clean:
        lines += ["\n### Peak score while the clip plays (clean): share of clips at or above each level\n",
                  "| Reaction | Level | " + " | ".join(GROUPS) + " |",
                  "|---|---|" + "---|" * len(GROUPS)]
        for reaction in REACTIONS:
            for level in (0.1, 0.2, 0.25, 0.3, 0.4, 0.5):
                cells = []
                for group in GROUPS:
                    sel = [r for r in clean if r["group"] == group]
                    k = sum(r["peak"][reaction] >= level for r in sel)
                    cells.append(f"{100 * k / len(sel):.0f}%" if sel else "—")
                lines.append(f"| {reaction} | {level:g} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="pick the clips and download them (needs the labels)")
    f.add_argument("--cache", type=Path, required=True)
    f.add_argument("--split", choices=SPLITS, default="eval")
    f.add_argument("--labels", type=Path, help="folder with FSD50K's eval.csv and dev.csv "
                   "(default: CACHE/labels/FSD50K.ground_truth)")
    f.add_argument("--per-group", type=int, default=40)
    f.add_argument("--max-clip-bytes", type=int, default=2_000_000)
    f.add_argument("--max-total-bytes", type=int, default=300_000_000)
    r = sub.add_parser("run", help="score the fetched clips through ServerSession")
    r.add_argument("--cache", type=Path, required=True)
    r.add_argument("--json", type=Path, help="write every row here")
    r.add_argument("--detector", choices=sorted(DETECTORS), default="current",
                   help="detector-v1 / detector-v2 re-run the settings PR #50 / PR #51 measured")
    t = sub.add_parser("tune", help="scan rules and levels on fetched dev clips")
    t.add_argument("--cache", type=Path, required=True)
    t.add_argument("--json", type=Path, help="write the whole scan here")
    args = ap.parse_args(argv)
    if args.cmd == "fetch":
        info = fetch(args.cache.expanduser(), args.per_group, args.max_clip_bytes, args.max_total_bytes,
                     split=args.split, labels_dir=args.labels.expanduser() if args.labels else None)
        print(json.dumps({g: len(ids) for g, ids in info["groups"].items()} | {"bytes": info["bytes"]}))
        return 0
    from encore_server.models import YamnetModel

    if args.cmd == "tune":
        result = tune(args.cache.expanduser(), YamnetModel())
        print(json.dumps({k: result[k] for k in ("split", "clips", "windows", "max_false")}))
        print(tune_report(result))
        if args.json:
            args.json.write_text(json.dumps(result, indent=1))
        return 0
    clips = load_clips(args.cache.expanduser())
    model = CachedModel(YamnetModel())
    config = DETECTORS[args.detector]
    started = time.time()
    rows = evaluate(clips, model, config=config,
                    progress=lambda i, n: print(f"\r{i}/{n} clips", end="", file=sys.stderr))
    print(file=sys.stderr)
    info = {"clips": len(clips), "rows": len(rows), "model": model.name, "detector": config.name,
            "trigger": config.trigger, "face_server_laughter": TabRules.strong_audio,
            "windows_scored": len(model.calls_ms),
            "model_ms_median": round(statistics.median(model.calls_ms), 1) if model.calls_ms else None,
            "wall_s": round(time.time() - started)}
    print(json.dumps(info))
    print(report(rows))
    if args.json:
        args.json.write_text(json.dumps({"info": info, "rows": rows}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
