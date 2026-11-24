#!/bin/sh
# EM-10 inputs on macOS (say, afconvert): playback speech and the 40 ESC-50 "laughing" clips.
# ESC-50: K. J. Piczak, "ESC: Dataset for Environmental Sound Classification", ACM MM 2015,
# CC BY-NC 3.0, https://github.com/karolpiczak/ESC-50
# Writes /tmp/aec/content.wav and /tmp/aec/laugh_esc50_all.wav (16 kHz mono 16-bit).
set -eu
PY=${PY:-python3}
OUT=/tmp/aec
mkdir -p "$OUT/esc50"
say -o "$OUT/content.aiff" "Welcome back to the show. Tonight we have a very special guest who has travelled from far away to tell us a story about a dog, a cat, and a very confused parrot. Let us begin."
afconvert -f WAVE -d LEI16@16000 -c 1 "$OUT/content.aiff" "$OUT/content.wav"
BASE=https://raw.githubusercontent.com/karolpiczak/ESC-50/master
curl -sL "$BASE/meta/esc50.csv" | grep ",laughing," | cut -d, -f1 > "$OUT/esc50/files.txt"
while read -r f; do
  [ -f "$OUT/esc50/16k_$f" ] && continue
  curl -sL -o "$OUT/esc50/$f" "$BASE/audio/$f"
  afconvert -f WAVE -d LEI16@16000 -c 1 "$OUT/esc50/$f" "$OUT/esc50/16k_$f"
done < "$OUT/esc50/files.txt"
"$PY" - "$OUT" <<'PYEOF'
import glob, sys, wave
import numpy as np
out = sys.argv[1]
parts = [np.frombuffer(wave.open(f).readframes(10**7), "<i2") for f in sorted(glob.glob(f"{out}/esc50/16k_*.wav"))]
w = wave.open(f"{out}/laugh_esc50_all.wav", "w"); w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
w.writeframes(np.concatenate(parts).tobytes()); w.close()
print(len(parts), "clips,", sum(len(p) for p in parts) / 16000, "s")
PYEOF
