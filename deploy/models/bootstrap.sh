#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y python3-venv libgl1 libglib2.0-0 curl
mkdir -p models samples reports
python3 -m venv .venv-audio
.venv-audio/bin/pip install --disable-pip-version-check -r requirements-audio.txt
python3 -m venv .venv-face
.venv-face/bin/pip install --disable-pip-version-check -r requirements-face.txt
curl --fail --location --retry 3 https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task -o models/face_landmarker.task
curl --fail --location --retry 3 https://storage.googleapis.com/audioset/miaow_16k.wav -o samples/miaow_16k.wav
curl --fail --location --retry 3 https://storage.googleapis.com/mediapipe-assets/portrait.jpg -o samples/portrait.jpg
export TFHUB_CACHE_DIR="$PWD/models/tfhub"
export CUDA_VISIBLE_DEVICES=-1
export TF_NUM_INTRAOP_THREADS=2
export TF_NUM_INTEROP_THREADS=1
.venv-audio/bin/python yamnet.py samples/miaow_16k.wav --expect-cat > reports/yamnet.json
.venv-face/bin/python face.py samples/portrait.jpg --expect-face > reports/face.json
.venv-audio/bin/pip check
.venv-face/bin/pip check
.venv-audio/bin/pip freeze > reports/audio-freeze.txt
.venv-face/bin/pip freeze > reports/face-freeze.txt
find models -type f -print0 | sort -z | xargs -0 sha256sum > reports/model-sha256.txt
cat reports/yamnet.json reports/face.json
