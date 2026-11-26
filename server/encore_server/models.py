"""Reaction models: one 0.975 s window of 16 kHz PCM16 in, a confidence per reaction out.

``YamnetModel`` is the real one (R2). It needs TensorFlow (see
server/requirements-yamnet.txt) and is loaded once per server process.
``LoudnessModel`` is a stand-in for working on the pipeline without
TensorFlow: any loud sound counts as laughter. It is not a reaction model,
and its name says so in every event's ``source.model``.

A model loaded from files names its weights: ``google/yamnet/1@sha256:`` and
the first 16 hex digits of ``sha256_tree`` over the SavedModel directory, so
an event shows which weights produced it. The full digest is in the startup log.
"""

from __future__ import annotations

import csv
import hashlib
import os
from pathlib import Path

from .audio import rms_db

SHORT_DIGEST = 16  # hex digits in a model name; the full 64 are logged at startup


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def sha256_tree(root: str | Path) -> str:
    """SHA-256 of a model directory: the same as, inside it,
    ``find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum``."""
    root = Path(root)
    files = sorted(("./" + p.relative_to(root).as_posix()).encode()
                   for p in root.rglob("*") if p.is_file() and not p.is_symlink())
    if not files:
        raise FileNotFoundError(f"{root}: no model files")
    listing = b"".join(sha256_file(root / name[2:].decode()).encode() + b"  " + name + b"\n" for name in files)
    return hashlib.sha256(listing).hexdigest()


def tagged(name: str, digest: str) -> str:
    """The name events carry for weights with this digest."""
    return f"{name}@sha256:{digest[:SHORT_DIGEST]}"

# YAMNet (AudioSet) display names that make up each contract reaction.
YAMNET_CLASSES = {
    "laughter": ("Laughter", "Baby laughter", "Giggle", "Snicker", "Belly laugh", "Chuckle, chortle"),
    "scream": ("Screaming",),
    "gasp": ("Gasp",),
}
YAMNET_HANDLE = "https://tfhub.dev/google/yamnet/1"


class LoudnessModel:
    name = "loudness-standin"

    def __init__(self, quiet_db: float = -45.0, loud_db: float = -15.0) -> None:
        self.quiet_db, self.loud_db = quiet_db, loud_db

    def __call__(self, pcm: bytes) -> dict[str, float]:
        level = (rms_db(pcm) - self.quiet_db) / (self.loud_db - self.quiet_db)
        return {"laughter": min(max(level, 0.0), 1.0), "scream": 0.0, "gasp": 0.0}


class YamnetModel:
    name = "google/yamnet/1"

    def __init__(self, handle: str = YAMNET_HANDLE, cache_dir: str | Path | None = None) -> None:
        if cache_dir is not None:
            os.environ.setdefault("TFHUB_CACHE_DIR", str(cache_dir))
        os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")  # CPU host
        os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
        import numpy as np
        import tensorflow_hub as hub

        self._np = np
        path = hub.resolve(handle)  # the local SavedModel directory; downloads it on first use
        self.sha256 = sha256_tree(path)
        self.name = tagged(type(self).name, self.sha256)
        self._model = hub.load(path)
        with open(self._model.class_map_path().numpy().decode(), newline="") as f:
            labels = [row["display_name"] for row in csv.DictReader(f)]
        missing = [n for names in YAMNET_CLASSES.values() for n in names if n not in labels]
        if missing:
            raise RuntimeError(f"YAMNet class map has no {missing}")
        self._index = {r: [labels.index(n) for n in names] for r, names in YAMNET_CLASSES.items()}

    def __call__(self, pcm: bytes) -> dict[str, float]:
        np = self._np
        waveform = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        scores, _, _ = self._model(waveform)
        frame = scores.numpy().max(axis=0)  # a 0.975 s window is one YAMNet frame
        return {r: float(frame[idx].max()) for r, idx in self._index.items()}


def load_model(name: str, *, yamnet_path: str | None = None):
    if name == "yamnet":
        return YamnetModel(handle=yamnet_path or YAMNET_HANDLE)
    if name == "loudness":
        return LoudnessModel()
    raise ValueError(f"unknown model {name!r}; use yamnet or loudness")
