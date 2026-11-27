"""EM-11 model comparison, model stages: ``score`` and ``timing`` (see em11_compare.py).

Four variants:

- ``yamnet``: the server's own model (encore_server.models.YamnetModel) on each 0.975 s window,
  on the CPU as in production.
- ``ast-pad10s``: AST on each window, its features padded to the 10.24 s (1024 frames) AST was
  trained on. It behaves as trained, at the full cost of a 10 s input.
- ``ast-cut1s``: AST on each window with 100 frames, its time position embeddings cut to the
  middle 9 patches the way the original AST code adapts the model to short input (Gong et al.,
  github.com/YuanGongND/ast). Much cheaper; it was not retrained for this, so it is measured.
- ``ast-clip``: AST on whole clips in 10.24 s pieces, the highest score of the pieces. It shows
  what AST does with the context it was trained on; there are no windows, so no server path.

AST comes from Hugging Face transformers with the pinned weights (em11_compare.PINS). Scores are
the sigmoid of its 527 AudioSet logits; YAMNet's are its own 521 class scores.
"""

from __future__ import annotations

import json
import os
import platform
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

import em11_compare as core

CLIP_SAMPLES = 163_840  # 10.24 s at 16 kHz: one AST input


def cut_positions(pe, freq: int, time_: int, keep: int, cat=np.concatenate):
    """AST position embeddings (1, 2 + freq*time, d) cut to the middle ``keep`` time patches.

    Patches are numbered frequency-major (index f*time + t), as transformers' ASTPatchEmbeddings
    flattens them. The slice starts where the original AST code's does: time//2 - keep//2.
    Works on numpy arrays and torch tensors (pass torch.cat).
    """
    lead, grid = pe[:, :2], pe[:, 2:]
    d = grid.shape[-1]
    start = time_ // 2 - keep // 2
    grid = grid.reshape(1, freq, time_, d)[:, :, start:start + keep, :].reshape(1, freq * keep, d)
    return cat([lead, grid], 1)


def _patches(length: int, patch: int, stride: int) -> int:
    return (length - patch) // stride + 1


def environment() -> dict:
    env = {"python": platform.python_version(), "machine": platform.machine(), "cpus": os.cpu_count(),
           "platform": platform.platform()}
    try:
        with open("/proc/cpuinfo") as f:
            env["cpu"] = next((ln.split(":", 1)[1].strip() for ln in f if ln.startswith("model name")), None)
    except OSError:
        pass
    return env


class YamnetScorer:
    kind = "window"

    def __init__(self, weights: Path | None = None, device: str = "cpu", threads: int | None = None):
        if device != "cpu":
            raise SystemExit("yamnet runs on the CPU, as on the server")
        os.environ["CUDA_VISIBLE_DEVICES"] = "-1"
        import tensorflow as tf

        if threads:
            tf.config.threading.set_intra_op_parallelism_threads(threads)
            tf.config.threading.set_inter_op_parallelism_threads(1)
        from encore_server.models import YAMNET_CLASSES, YAMNET_HANDLE, YamnetModel

        assert all(core.CLASSES[r] == YAMNET_CLASSES[r] for r in core.PRODUCT), "reaction classes drifted"
        self.model = YamnetModel(handle=str(weights) if weights else YAMNET_HANDLE)
        if self.model.sha256 != core.PINS["yamnet"]["sha256"]:
            raise SystemExit(f"YAMNet weights sha256 {self.model.sha256}, expected {core.PINS['yamnet']['sha256']}")
        self.labels, self.name = self.model.labels, self.model.name
        self.meta = {"model": self.name, "weights_sha256": self.model.sha256, "tensorflow": tf.__version__,
                     "threads": threads}

    def __call__(self, batch: list[bytes]) -> np.ndarray:
        return np.stack([self.model.class_scores(pcm) for pcm in batch])


class AstScorer:
    def __init__(self, variant: str, weights: Path, device: str = "cpu", precision: str = "fp32",
                 threads: int | None = None):
        import torch
        import transformers
        from transformers import ASTFeatureExtractor, ASTForAudioClassification
        from transformers.utils import is_speech_available

        if threads:
            torch.set_num_threads(threads)
        if precision == "fp16" and not device.startswith("cuda"):
            raise SystemExit("half precision needs a CUDA device")
        pin = core.PINS["ast"]
        digest = core.sha256_file(Path(weights) / pin["file"])
        if digest != pin["sha256"]:
            raise SystemExit(f"{pin['file']}: sha256 {digest}, expected {pin['sha256']}")
        self.torch, self.variant, self.device, self.half = torch, variant, device, precision == "fp16"
        self.kind = "clip" if variant == "ast-clip" else "window"
        model = ASTForAudioClassification.from_pretrained(weights, torch_dtype=torch.float32)
        cfg = model.config
        frames = 100 if variant == "ast-cut1s" else cfg.max_length
        if variant == "ast-cut1s":
            emb = model.audio_spectrogram_transformer.embeddings
            freq = _patches(cfg.num_mel_bins, cfg.patch_size, cfg.frequency_stride)
            full_t = _patches(cfg.max_length, cfg.patch_size, cfg.time_stride)
            keep = _patches(frames, cfg.patch_size, cfg.time_stride)
            emb.position_embeddings = torch.nn.Parameter(
                cut_positions(emb.position_embeddings.data, freq, full_t, keep, torch.cat).contiguous())
            cfg.max_length = frames
        self.model = model.eval().to(device)
        self.features = ASTFeatureExtractor.from_pretrained(weights, max_length=frames)
        self.labels = [cfg.id2label[i] for i in range(cfg.num_labels)]
        self.name = f"{pin['repo']}@{pin['revision'][:12]}:{variant}"
        self.meta = {"model": self.name, "weights_sha256": digest, "frames": frames, "precision": precision,
                     "torch": torch.__version__, "transformers": transformers.__version__,
                     "fbank": "torchaudio kaldi" if is_speech_available() else "numpy",
                     "device": device, "threads": threads or torch.get_num_threads(),
                     "gpu": torch.cuda.get_device_name(device) if device.startswith("cuda") else None}
        probe = self([np.zeros(15_600, np.int16).tobytes()])  # fails here, not hours later, if the cut is wrong
        assert probe.shape == (1, len(self.labels)), probe.shape

    def _run(self, waves: list[np.ndarray]) -> np.ndarray:
        torch = self.torch
        x = self.features(waves, sampling_rate=16_000, return_tensors="pt")["input_values"].to(self.device)
        dev = "cuda" if self.device.startswith("cuda") else "cpu"
        with torch.inference_mode(), torch.autocast(device_type=dev, dtype=torch.float16, enabled=self.half):
            logits = self.model(x).logits
        return torch.sigmoid(logits.float()).cpu().numpy()

    def __call__(self, batch: list[bytes]) -> np.ndarray:
        return self._run([np.frombuffer(p, "<i2").astype(np.float32) / 32768 for p in batch])

    def clip(self, pcm: np.ndarray, batch: int = 8) -> np.ndarray:
        """Whole clip: 10.24 s pieces (the last one padded), highest score per class."""
        wave = pcm.astype(np.float32) / 32768
        pieces = [wave[i:i + CLIP_SAMPLES] for i in range(0, max(len(wave), 1), CLIP_SAMPLES)]
        return np.concatenate([self._run(pieces[i:i + batch]) for i in range(0, len(pieces), batch)]).max(axis=0)


def make_scorer(variant: str, weights, device: str, precision: str, threads: int | None = None):
    if variant == "yamnet":
        return YamnetScorer(weights, device, threads)
    if weights is None:
        raise SystemExit(f"{variant} needs --weights (the AST snapshot folder)")
    return AstScorer(variant, weights, device, precision, threads)


def _load(data: Path, limit):
    manifest = json.loads((data / "manifest.json").read_text())
    return manifest, core.limited(manifest["selection"], limit), core.Clips(data, manifest)


def verify_inputs(data: Path, manifest: dict, keys) -> None:
    bad = [k for k in keys if core.sha256_file(data / "16k" / f"{k}.wav") != manifest["files"][k]["sha256_16k"]]
    if bad:
        raise SystemExit(f"{len(bad)} 16 kHz files differ from the manifest, e.g. {bad[:3]}")


def score(args) -> dict:
    data = Path(args.data)
    manifest, selection, clips = _load(data, args.limit)
    sets = [s for s in args.sets.split(",") if s]
    unknown = set(sets) - set(core.SETS)
    if unknown:
        raise SystemExit(f"unknown sets {sorted(unknown)}")
    shard_i, shard_n = (int(v) for v in args.shard.split("/"))
    skip = set(core.load_bank(*args.skip)[0]) if args.skip else set()
    scorer = make_scorer(args.model, args.weights, args.device, args.precision, getattr(args, "threads", None))
    streams = core.streams(selection, sets if scorer.kind == "window" else [s for s in sets if s != "mixed"])
    used = sorted({f"{s.split}/{s.fname}" for s in streams} | {f"{s.split}/{s.background}" for s in streams if s.background})
    verify_inputs(data, manifest, used)
    keys, rows, full = [], [], []
    model_s, started = 0.0, time.time()

    def flush(batch_keys, batch_items):
        nonlocal model_s
        t = time.perf_counter()
        vectors = (scorer(batch_items) if scorer.kind == "window"
                   else np.stack([scorer.clip(pcm) for pcm in batch_items]))
        model_s += time.perf_counter() - t
        keys.extend(batch_keys)
        rows.append(core.reaction_scores(vectors, scorer.labels))
        if args.full:
            full.append(vectors.astype(np.float16))

    pending_k, pending = [], []
    seen: set[str] = set()
    for n, s in enumerate(streams):
        if scorer.kind == "clip":
            if s.condition != "clean":
                continue
            candidates = [(f"{s.split}/{s.fname}", clips.pcm(s.split, s.fname))]
        else:
            candidates = [(core.window_key(w.pcm), w.pcm) for w in core.stream_windows(clips.stream_pcm(s))]
        for key, item in candidates:
            if key in seen or key in skip:
                continue
            seen.add(key)
            part = int(key[-8:], 16) if scorer.kind == "window" else n
            if part % shard_n != shard_i:
                continue
            pending_k.append(key)
            pending.append(item)
            if len(pending) == args.batch:
                flush(pending_k, pending)
                pending_k, pending = [], []
        if args.sample and len(keys) + len(pending) >= args.sample:
            break
        if n % 500 == 0:
            print(f"{n}/{len(streams)} streams, {len(keys)} scored, {time.time() - started:.0f} s", file=sys.stderr, flush=True)
    if pending:
        flush(pending_k, pending)
    meta = {"variant": args.model, "precision": args.precision, "sets": sets, "limit": args.limit, "shard": args.shard,
            "sample": args.sample, "rows": len(keys), "model_seconds": round(model_s, 1),
            "per_second": round(len(keys) / model_s, 1) if model_s else None, "batch": args.batch,
            "wall_seconds": round(time.time() - started), "finished": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "data_created": manifest["created"], "scorer": scorer.meta, "environment": environment(),
            "labels": scorer.labels if args.full else None}
    core.save_bank(Path(args.out), keys, np.concatenate(rows) if rows else np.zeros((0, len(core.REACTIONS))), meta,
                   np.concatenate(full) if full else None)
    print(json.dumps({k: meta[k] for k in ("variant", "rows", "model_seconds", "per_second", "wall_seconds")}))
    return meta


def timing(args) -> dict:
    """Per-window time with batch 1 (what a live server pays per window), after a short warm-up."""
    data = Path(args.data)
    manifest, selection, clips = _load(data, None)
    scorer = make_scorer(args.model, args.weights, args.device, args.precision, args.threads)
    windows = []
    for s in core.streams(selection, ["curated"]):
        for w in core.touching(list(core.stream_windows(clips.stream_pcm(s))), clips.pcm(s.split, s.fname).size * 1000 // 16_000):
            windows.append(w.pcm)
        if len(windows) >= args.windows + 5:
            break
    for pcm in windows[:5]:
        scorer([pcm])
    times = []
    for pcm in windows[5:args.windows + 5]:
        t = time.perf_counter()
        scorer([pcm])
        if args.device.startswith("cuda"):
            scorer.torch.cuda.synchronize()
        times.append((time.perf_counter() - t) * 1000)
    times.sort()
    result = {"variant": args.model, "device": args.device, "threads": args.threads, "precision": args.precision,
              "windows": len(times), "median_ms": round(statistics.median(times), 1),
              "p90_ms": round(times[min(len(times) - 1, int(0.9 * len(times)))], 1),
              "scorer": scorer.meta, "environment": environment()}
    Path(args.out).write_text(json.dumps(result, indent=1) + "\n")
    print(json.dumps({k: result[k] for k in ("variant", "device", "threads", "median_ms", "p90_ms")}))
    return result


def main(args) -> int:
    (score if args.cmd == "score" else timing)(args)
    return 0
