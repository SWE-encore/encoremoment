#!/usr/bin/env python3
"""EM-11 model comparison: YAMNet against AST on a larger FSD50K evaluation.

docs/em-11-model-comparison.md explains the question, the data and how to run it on Kaggle.
The work is split into stages so that each runs where it fits:

    select    choose the clips from FSD50K's labels (run once; result: docs/em-11-compare-clips.json)
    refhash   checksums of the clips EM-11 already used, from a local cache (run once;
              result: docs/em-11-compare-reference.json)
    prepare   find the chosen clips in a copy of FSD50K, check them, convert to 16 kHz (CPU)
    score     run one model over every analysis window and keep its scores (GPU or CPU)
    timing    time one model per window on this machine
    analyze   compare without thresholds, choose levels on dev clips, then play the eval
              clips through ServerSession with those levels
    sanity    re-run em11_eval.py on the original 280 clips and compare with the committed rows
    preflight a small selection with the clips most likely to break a stage, for a run of every
              stage in minutes before the long ones (--selection on score and analyze)

Two rules carry over from docs/em-11-evaluation.md: levels are chosen on dev clips only, and the
same eval clips are measured once per model. This is an evaluation tool only. It adds candidate
reactions (cheering, applause, shout) to the analysis, but the server's detector and the
contract still know laughter, scream and gasp only.

    python server/tools/em11_compare.py select --labels ~/.cache/encore/fsd50k-eval/labels/FSD50K.ground_truth
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import subprocess
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np

TOOLS = Path(__file__).resolve().parent
REPO = TOOLS.parents[1]
for _p in (TOOLS, TOOLS.parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import em11_eval as em11  # noqa: E402
from encore_server.audio import AudioWindows  # noqa: E402

# ---- what is compared ---------------------------------------------------------------------

REACTIONS = ("laughter", "scream", "gasp", "cheering", "applause", "shout")
PRODUCT = ("laughter", "scream", "gasp")  # what the server detects; the rest are candidates
# AudioSet display names that make up each reaction. YAMNet and AST both use AudioSet's names.
# The first three are the server's own (encore_server.models.YAMNET_CLASSES). Shout is AudioSet's
# Shout class and the four listed under it in the ontology. Fixed before any eval result was seen.
CLASSES = {
    "laughter": ("Laughter", "Baby laughter", "Giggle", "Snicker", "Belly laugh", "Chuckle, chortle"),
    "scream": ("Screaming",),
    "gasp": ("Gasp",),
    "cheering": ("Cheering",),
    "applause": ("Applause", "Clapping"),
    "shout": ("Shout", "Bellow", "Whoop", "Yell", "Children shouting"),
}
# FSD50K labels that make a clip a positive for each reaction.
LAUGH = frozenset({"Laughter", "Giggle", "Chuckle_and_chortle"})
LABELS = {
    "laughter": LAUGH,
    "scream": frozenset({"Screaming"}),
    "gasp": frozenset({"Gasp"}),
    "cheering": frozenset({"Cheering"}),
    "applause": frozenset({"Applause", "Clapping"}),
    "shout": frozenset({"Shout", "Yell"}),
}
REACTION_LABELS = frozenset().union(*LABELS.values())
CROWD = LABELS["cheering"] | LABELS["applause"]
# Group -> (any of these labels, none of these). A clip joins the first group it fits, in this
# order. The first three match em11_eval.py's groups; cheering, applause and shout are new, and
# the three groups of other sounds now also leave out clips with those labels.
GROUPS = {
    "laughter": (LAUGH, frozenset({"Screaming", "Gasp", "Music"})),
    "scream": (LABELS["scream"], LAUGH | {"Gasp", "Music"}),
    "gasp": (LABELS["gasp"], LAUGH | {"Screaming", "Music"}),
    "cheering": (LABELS["cheering"], LAUGH | {"Screaming", "Gasp", "Music"}),
    "applause": (LABELS["applause"], LAUGH | {"Screaming", "Gasp", "Music", "Cheering"}),
    "shout": (LABELS["shout"], LAUGH | {"Screaming", "Gasp", "Music"} | CROWD),
    "speech": (frozenset({"Speech"}), REACTION_LABELS | {"Music"}),
    "music": (frozenset({"Music"}), REACTION_LABELS | {"Human_voice"}),
    "breath_cough": (frozenset({"Breathing", "Cough", "Sigh", "Sneeze"}), REACTION_LABELS | {"Music"}),
}
OTHER_GROUPS = ("speech", "music", "breath_cough")
EVERYDAY = "everyday"  # eval: every other eval clip with no reaction label; dev: a sample of them
SEED = 11
MUSIC_SAMPLE = 300  # eval music clips in the living-room set; the other music clips are everyday sounds
DEV_CAP = 300  # dev clips per group, for choosing levels
DEV_EVERYDAY = 600

# Everything a run depends on, pinned. docs/em-11-model-comparison.md says where each came from.
PINS = {
    "ground_truth": {"url": "https://zenodo.org/records/4060432/files/FSD50K.ground_truth.zip?download=1",
                     "md5": "ca27382c195e37d2269c4c866dd73485"},
    "mirror": {"repo": "Fhrozen/FSD50k", "revision": "ccf1acaff12f3f4a4c10052dddafbb6d22152c9b"},
    "kaggle_copy": {"dataset": "yousirui1/fsd50k", "version": 1},
    "yamnet": {"handle": "https://tfhub.dev/google/yamnet/1",
               "sha256": "c59c5744e1b92942f67d9c0c8c53cb69757d002007870c394e5d126af98c0b59"},
    "ast": {"repo": "MIT/ast-finetuned-audioset-10-10-0.4593",
            "revision": "f826b80d28226b62986cc218e5cec390b1096902",
            "file": "model.safetensors", "bytes": 346_404_948,
            "sha256": "ae0c1e2ad4e1381d851fa9bf298ba13ebc9c5a914cdee2dbe427a6583869924d"},
}
SELECTION = REPO / "docs" / "em-11-compare-clips.json"
REFERENCE = REPO / "docs" / "em-11-compare-reference.json"
SANITY = REPO / "docs" / "em-11-compare-sanity.json"
VARIANTS = ("yamnet", "ast-pad10s", "ast-cut1s", "ast-clip")  # ast-clip scores whole clips, not windows
AST_SAMPLES = 163_840  # 10.24 s at 16 kHz: one AST input; ast-clip cuts longer clips into pieces this long


# ---- choosing the clips -------------------------------------------------------------------

def group_of(labels: set[str] | frozenset[str]) -> str | None:
    for group, (anyof, noneof) in GROUPS.items():
        if labels & anyof and not labels & noneof:
            return group
    return None


def reactions_of(labels) -> set[str]:
    """The reactions a clip's labels make it a positive for (cheering and applause often come together)."""
    return {r for r in REACTIONS if set(labels) & LABELS[r]}


def select(labels_dir: Path, seed: int = SEED) -> dict:
    """The clips this comparison uses, from FSD50K's eval.csv and dev.csv. Deterministic."""
    out = {"source": "FSD50K (Zenodo 4060432) eval and dev splits; Freesound ids",
           "seed": seed, "music_sample": MUSIC_SAMPLE, "dev_cap": DEV_CAP, "dev_everyday": DEV_EVERYDAY}
    for split in ("eval", "dev"):
        labels = em11.read_labels(labels_dir / f"{split}.csv")
        pools: dict[str, list[str]] = {g: [] for g in GROUPS}
        rest: list[str] = []
        for fname in sorted(labels, key=int):
            if fname in em11.SEEN_IDS:
                continue
            group = group_of(labels[fname])
            if group:
                pools[group].append(fname)
            elif not labels[fname] & REACTION_LABELS:
                rest.append(fname)
        for group, ids in pools.items():
            random.Random(f"{seed}-{split}-{group}").shuffle(ids)
        if split == "eval":
            groups = dict(pools)
            groups["music"], extra = pools["music"][:MUSIC_SAMPLE], pools["music"][MUSIC_SAMPLE:]
            everyday = sorted(rest + extra, key=int)
        else:
            groups = {g: ids[:DEV_CAP] for g, ids in pools.items()}
            random.Random(f"{seed}-{split}-{EVERYDAY}").shuffle(rest)
            groups[EVERYDAY] = rest[:DEV_EVERYDAY]
            everyday = []
        counts = {g: len(ids) for g, ids in groups.items()}
        if split == "eval":
            counts[EVERYDAY] = len(everyday)
        out[split] = {"counts": counts, "groups": groups, EVERYDAY: everyday}
    return out


def selected_ids(selection: dict, split: str) -> list[str]:
    ids = [f for ids in selection[split]["groups"].values() for f in ids] + selection[split].get(EVERYDAY, [])
    return list(dict.fromkeys(ids))


def chosen(manifest: dict, limit: int | None = None, selection: Path | None = None) -> dict:
    """The clips a stage works on: a selection file (``preflight``), or the manifest's, cut by ``limit``."""
    if selection:
        return json.loads(Path(selection).read_text())
    return limited(manifest["selection"], limit)


def preflight(manifest: dict, per_group: int = 1) -> tuple[dict, dict]:
    """A selection small enough to run every stage in minutes: ``per_group`` clips of every group,
    plus, per split, the clips most likely to break a stage: the shortest, the longest, and the two
    whose last 10.24 s AST piece would be shortest without clip_pieces. Returns it and the extra clips."""
    full = manifest["selection"]
    out = limited(full, per_group)
    extra = {}
    for split in ("eval", "dev"):
        where = {f: g for g, ids in full[split]["groups"].items() for f in ids}
        where |= {f: EVERYDAY for f in full[split].get(EVERYDAY, [])}
        n = {f: manifest["files"][f"{split}/{f}"]["samples"] for f in where}
        tails = sorted((x % AST_SAMPLES, f) for f, x in n.items() if x > AST_SAMPLES and x % AST_SAMPLES)
        picks = [("shortest", min(n, key=n.get)), ("longest", max(n, key=n.get))]
        picks += [(f"last piece {x} samples", f) for x, f in tails[:2]]
        reasons: dict[str, list[str]] = {}
        for why, f in picks:
            reasons.setdefault(f, []).append(why)
        for f, why in reasons.items():
            groups = out[split]["groups"]
            bucket = groups[where[f]] if where[f] in groups else out[split][EVERYDAY]
            if f not in bucket:
                bucket.append(f)
            extra[f"{split}/{f}"] = f"{', '.join(why)}: {n[f] / em11.RATE:.2f} s, group {where[f]}"
    return out, extra


def code_commit() -> str | None:
    """The git commit of this code, when it runs from a checkout."""
    try:
        return subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True, text=True,
                              check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def limited(selection: dict, limit: int | None) -> dict:
    """The first ``limit`` clips of every group (and of the eval everyday list): a quick smoke run."""
    if not limit:
        return selection
    out = json.loads(json.dumps(selection))
    for split in ("eval", "dev"):
        out[split]["groups"] = {g: ids[:limit] for g, ids in out[split]["groups"].items()}
        out[split][EVERYDAY] = out[split].get(EVERYDAY, [])[: 4 * limit]
    return out


# ---- streams and windows ------------------------------------------------------------------

@dataclass(frozen=True)
class Stream:
    """One audio stream played into the server: a clip alone (``clean``), or over playback."""

    split: str
    fname: str
    group: str
    condition: str  # "clean", "speech+10dB", "music+0dB", ...
    background: str | None = None
    snr_db: int | None = None

    @property
    def key(self) -> str:
        return f"{self.split}/{self.fname}/{self.condition}"


class Clips:
    """16 kHz clips of a prepared data folder (``prepare``), read on demand."""

    def __init__(self, data: Path, manifest: dict | None = None):
        self.data = Path(data)
        self.manifest = manifest or json.loads((self.data / "manifest.json").read_text())

    @lru_cache(maxsize=256)
    def pcm(self, split: str, fname: str) -> np.ndarray:
        return em11.read_pcm(self.data / "16k" / split / f"{fname}.wav")

    def labels(self, split: str, fname: str) -> frozenset[str]:
        return frozenset(self.manifest["labels"][f"{split}/{fname}"])

    def stream_pcm(self, s: Stream) -> np.ndarray:
        clip = self.pcm(s.split, s.fname)
        if s.condition == "clean":
            return em11.clean_stream(clip)
        return em11.mixed_stream(clip, self.pcm(s.split, s.background), s.snr_db)


SETS = ("curated", "mixed", "dev", "everyday")


def streams(selection: dict, sets=SETS) -> list[Stream]:
    """Every stream of the chosen sets, in a fixed order.

    curated: each eval clip of the reaction and living-room groups, alone;
    mixed: each eval reaction clip over speech and over music, at +10 and 0 dB (em11_eval.SNRS);
      the playback clip is the i-th reaction clip's i-th speech or music clip, wrapping around;
    dev: each dev clip alone; everyday: each other eval clip alone.
    """
    ev, dev = selection["eval"], selection["dev"]
    out: list[Stream] = []
    if "curated" in sets:
        out += [Stream("eval", f, g, "clean") for g, ids in ev["groups"].items() for f in ids]
    if "mixed" in sets:
        reaction_clips = [(g, f) for g in REACTIONS for f in ev["groups"].get(g, [])]
        for i, (g, f) in enumerate(reaction_clips):
            for bg_group in ("speech", "music"):
                pool = ev["groups"][bg_group]
                if not pool:
                    continue
                for snr in em11.SNRS:
                    out.append(Stream("eval", f, g, f"{bg_group}{snr:+d}dB", pool[i % len(pool)], snr))
    if "dev" in sets:
        out += [Stream("dev", f, g, "clean") for g, ids in dev["groups"].items() for f in ids]
    if "everyday" in sets:
        out += [Stream("eval", f, EVERYDAY, "clean") for f in ev.get(EVERYDAY, [])]
    return out


def window_key(pcm: bytes) -> str:
    """The same digest em11_eval.CachedModel uses, as text."""
    return hashlib.blake2b(pcm, digest_size=16).hexdigest()


def stream_windows(pcm: np.ndarray):
    """The analysis windows ServerSession makes from ``pcm`` when em11_eval.run_stream plays it:
    0.5 s chunks from T0, one microphone generation. Yields encore_server.audio.Window objects."""
    windows = AudioWindows()
    chunk = em11.CHUNK_SAMPLES
    chunks = math.ceil(len(pcm) / chunk)
    padded = np.zeros(chunks * chunk, np.int16)
    padded[: len(pcm)] = pcm
    for c in range(chunks):
        header = {"generation": 1, "chunk": c, "capture_ms": em11.T0 + c * 500, "aec": "off"}
        yield from windows.add(header, padded[c * chunk:(c + 1) * chunk].tobytes())


def touching(windows, length_ms: int):
    """Windows of a clean stream that overlap the clip (em11_eval.clip_class_scores' rule)."""
    start, end = em11.T0 + em11.LEAD_MS, em11.T0 + em11.LEAD_MS + length_ms
    return [w for w in windows if w.end_ms > start and w.start_ms < end]


# ---- score banks --------------------------------------------------------------------------
#
# A bank holds one model's scores: one row per distinct window (key: window_key) or, for ast-clip,
# per clip (key: "split/fname"). ``scores`` has the six reactions in REACTIONS order as float32,
# exactly what the detector compares with its levels; ``full`` (optional) has every class as
# float16 for later questions.

def save_bank(path: Path, keys: list[str], scores: np.ndarray, meta: dict, full: np.ndarray | None = None) -> None:
    arrays = {"keys": np.array(keys, dtype="U"), "scores": np.asarray(scores, np.float32).reshape(-1, len(REACTIONS)),
              "meta": np.array(json.dumps(meta))}
    if full is not None:
        arrays["full"] = np.asarray(full, np.float16)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part.npz")
    np.savez(tmp, **arrays)
    tmp.replace(path)


def load_bank(*paths: Path) -> tuple[dict[str, np.ndarray], list[dict]]:
    """Merge banks (shards, or a full-precision bank before a half-precision one): the first
    file that has a key wins. Returns key -> reaction scores, and each file's meta."""
    table: dict[str, np.ndarray] = {}
    metas = []
    for path in paths:
        with np.load(path) as z:
            metas.append(json.loads(str(z["meta"])))
            for key, row in zip(z["keys"].tolist(), z["scores"]):
                table.setdefault(key, row)
    return table, metas


def reaction_scores(vectors: np.ndarray, labels: list[str]) -> np.ndarray:
    """(windows, classes) -> (windows, reactions): the highest score among each reaction's classes."""
    missing = [n for names in CLASSES.values() for n in names if n not in labels]
    if missing:
        raise RuntimeError(f"model labels have no {missing}")
    vectors = np.asarray(vectors, np.float32).reshape(-1, len(labels))
    return np.stack([vectors[:, [labels.index(n) for n in CLASSES[r]]].max(axis=1) for r in REACTIONS], axis=1)


def dumps(obj) -> str:
    """JSON with every list of ids on one line, so the committed files stay readable."""
    import re

    text = json.dumps(obj, indent=1)
    return re.sub(r"\[\n\s*([^\[\]{}]*?)\n\s*\]", lambda m: "[" + re.sub(r",\n\s*", ", ", m.group(1)) + "]", text) + "\n"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# ---- command line ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("select", help="choose the clips from FSD50K's eval.csv and dev.csv")
    s.add_argument("--labels", type=Path, required=True, help="folder with eval.csv and dev.csv")
    s.add_argument("--out", type=Path, default=SELECTION)

    r = sub.add_parser("refhash", help="checksums of the clips in local em11_eval caches")
    r.add_argument("--cache", type=Path, action="append", required=True, help="em11_eval cache (repeat)")
    r.add_argument("--out", type=Path, default=REFERENCE)

    p = sub.add_parser("prepare", help="find, check and convert the chosen clips")
    p.add_argument("--source", type=Path, help="a copy of FSD50K to read clips from (e.g. /kaggle/input/fsd50k)")
    p.add_argument("--labels", type=Path, help="folder with eval.csv and dev.csv (default: download from Zenodo)")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--policy", choices=("auto", "copy", "mirror"), default="auto",
                   help="auto: use the copy only if every check passes; copy: use it where it has the clip; "
                        "mirror: fetch every clip from the pinned Hugging Face mirror")
    p.add_argument("--crosscheck", type=int, default=30, help="extra copy clips compared with the mirror")
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--limit", type=int, help="first N clips per group only (smoke run)")
    p.add_argument("--tmp", type=Path, help="scratch folder for downloads (default: OUT/../em11-tmp)")

    c = sub.add_parser("score", help="score every window (or clip) with one model")
    c.add_argument("--data", type=Path, required=True, help="the folder prepare wrote")
    c.add_argument("--model", choices=VARIANTS, required=True)
    c.add_argument("--weights", type=Path, help="local model folder (AST snapshot or YAMNet SavedModel)")
    c.add_argument("--sets", default="curated,mixed,dev,everyday")
    c.add_argument("--out", type=Path, required=True)
    c.add_argument("--skip", type=Path, action="append", default=[], help="banks whose keys are already scored")
    c.add_argument("--precision", choices=("fp32", "fp16"), default="fp32")
    c.add_argument("--device", default="cpu")
    c.add_argument("--threads", type=int, help="CPU threads for this model (leave cores for the others)")
    c.add_argument("--batch", type=int, default=32)
    c.add_argument("--shard", default="0/1", help="i/n: score only the keys whose digest falls in share i")
    c.add_argument("--sample", type=int, help="score only the first N distinct windows (precision check)")
    c.add_argument("--full", action="store_true", help="also keep every class score (float16)")
    c.add_argument("--limit", type=int)
    c.add_argument("--selection", type=Path, help="score only these clips (a file preflight wrote)")

    t = sub.add_parser("timing", help="time one model per window on this machine")
    t.add_argument("--data", type=Path, required=True)
    t.add_argument("--model", choices=VARIANTS[:3], required=True)
    t.add_argument("--weights", type=Path)
    t.add_argument("--device", default="cpu")
    t.add_argument("--threads", type=int, help="CPU threads (2 = the cloud server)")
    t.add_argument("--precision", choices=("fp32", "fp16"), default="fp32")
    t.add_argument("--windows", type=int, default=60)
    t.add_argument("--out", type=Path, required=True)

    a = sub.add_parser("analyze", help="compare the banks and write the report")
    a.add_argument("--data", type=Path, required=True)
    a.add_argument("--bank", action="append", required=True,
                   help="VARIANT=FILE[,FILE...] (files merged in order; repeat per variant)")
    a.add_argument("--check", action="append", default=[], help="VARIANT=FILE: a half-precision sample to compare")
    a.add_argument("--timing", type=Path, action="append", default=[])
    a.add_argument("--sanity", type=Path, help="the JSON the sanity stage wrote")
    a.add_argument("--out", type=Path, required=True)
    a.add_argument("--workers", type=int, default=4)
    a.add_argument("--limit", type=int)
    a.add_argument("--commit", help="the code's git commit, for the report")
    a.add_argument("--selection", type=Path, help="analyze only these clips (a file preflight wrote)")

    y = sub.add_parser("sanity", help="em11_eval.py on the original 280 clips, compared with the committed rows")
    y.add_argument("--data", type=Path, required=True)
    y.add_argument("--work", type=Path, required=True, help="scratch folder for the em11_eval cache")
    y.add_argument("--out", type=Path, required=True)

    f = sub.add_parser("preflight", help="write a small selection with the clips most likely to break a stage")
    f.add_argument("--data", type=Path, required=True)
    f.add_argument("--out", type=Path, required=True)
    f.add_argument("--per-group", type=int, default=1)

    args = ap.parse_args(argv)
    if args.cmd == "select":
        result = select(args.labels.expanduser())
        args.out.write_text(dumps(result))
        print(json.dumps({split: result[split]["counts"] for split in ("eval", "dev")}))
        return 0
    if args.cmd == "preflight":
        selection, extra = preflight(json.loads((args.data / "manifest.json").read_text()), args.per_group)
        args.out.write_text(dumps(selection))
        print(json.dumps({split: sum(map(len, selection[split]["groups"].values())) + len(selection[split].get(EVERYDAY, []))
                          for split in ("eval", "dev")}))
        for key, why in extra.items():
            print(f"  {key}: {why}")
        return 0
    if args.cmd in ("refhash", "prepare"):
        import em11_compare_data as data
        return data.main(args)
    if args.cmd in ("score", "timing"):
        import em11_compare_models as models
        return models.main(args)
    import em11_compare_analysis as analysis
    return analysis.main(args)


if __name__ == "__main__":
    sys.exit(main())
