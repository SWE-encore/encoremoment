"""Top three YAMNet classes for every 0.48 s of a 16 kHz mono WAV (what does YAMNet hear?).
Run from the repository root with the YAMNet environment:  python client/tools/aec/yamnet_top.py FILE.wav"""
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "server"))
from encore_server.models import YamnetModel  # noqa: E402

m = YamnetModel()
w = wave.open(sys.argv[1])
x = np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32768
rows = open(m._model.class_map_path().numpy().decode()).read().splitlines()[1:]
names = [r.split(",", 2)[2].strip('"') for r in rows]
scores, _, _ = m._model(x)
for i, frame in enumerate(scores.numpy()):
    top = frame.argsort()[::-1][:3]
    print(f"{i * 0.48:5.1f}s  " + "  ".join(f"{names[k]} {frame[k]:.2f}" for k in top))
