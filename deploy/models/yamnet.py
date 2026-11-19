"""Run the official YAMNet v1 model on a mono, 16 kHz PCM16 WAV."""
import argparse
import csv
import json
import os
from pathlib import Path
import time

ROOT = Path(__file__).resolve().parent
os.environ.setdefault("TFHUB_CACHE_DIR", str(ROOT / "models/tfhub"))
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")
os.environ.setdefault("TF_NUM_INTRAOP_THREADS", "2")
os.environ.setdefault("TF_NUM_INTEROP_THREADS", "1")
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")


def main():
    import numpy as np
    from scipy.io import wavfile
    import tensorflow_hub as hub

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wav", type=Path)
    parser.add_argument("--expect-cat", action="store_true")
    args = parser.parse_args()
    rate, audio = wavfile.read(args.wav)
    if rate != 16000 or audio.ndim != 1 or audio.dtype != np.int16:
        parser.error("Input must be mono, 16000 Hz, PCM16 WAV")
    if not 15600 <= audio.size <= 16000 * 60:
        parser.error("Input duration must be between 0.975 and 60 seconds")
    model = hub.load("https://tfhub.dev/google/yamnet/1")
    with open(model.class_map_path().numpy().decode(), newline="") as stream:
        labels = [row["display_name"] for row in csv.DictReader(stream)]
    waveform = audio.astype(np.float32) / 32768.0
    started = time.perf_counter()
    scores, _, _ = model(waveform)
    scores = scores.numpy()
    elapsed = time.perf_counter() - started
    if scores.shape[1] != 521 or not np.isfinite(scores).all():
        raise RuntimeError("Invalid YAMNet output")
    means = scores.mean(axis=0)
    top = [{"label": labels[i], "score": float(means[i])}
           for i in np.argsort(means)[-5:][::-1]]
    if args.expect_cat and not any(row["label"] in {"Cat", "Meow"} for row in top):
        raise RuntimeError(f"Cat sample did not produce Cat/Meow in top 5: {top}")
    print(json.dumps({"model": "google/yamnet/1", "audio_seconds": len(audio) / rate,
                      "inference_seconds": elapsed, "frames": len(scores),
                      "classes": len(labels), "top5": top}, indent=2))


if __name__ == "__main__":
    main()
