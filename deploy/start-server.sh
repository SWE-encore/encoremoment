#!/bin/sh
set -eu

set -- python -m encore_server --host 0.0.0.0 --port 8765
case "${ENCORE_MODEL:-loudness}" in
    loudness)
        set -- "$@" --model loudness
        ;;
    yamnet)
        cd "${ENCORE_BUNDLE_DIR:-/bundle}"
        : "${ENCORE_MANIFEST_SHA256:?supply the trusted SHA-256 of reports/model-sha256.txt}"
        case "$ENCORE_MANIFEST_SHA256" in *[!0-9a-f]*) echo "Invalid manifest digest" >&2; exit 2 ;; esac
        test "${#ENCORE_MANIFEST_SHA256}" -eq 64 || { echo "Invalid manifest digest" >&2; exit 2; }
        printf '%s  reports/model-sha256.txt\n' "$ENCORE_MANIFEST_SHA256" | sha256sum --check --status -
        sha256sum --check --strict --quiet reports/model-sha256.txt
        # The bootstrap bundle has one TF Hub SavedModel and one face task.
        # Load by local path so a missing bundle cannot trigger an online download.
        for artifact in models/tfhub/*/saved_model.pb; do
            test -f "$artifact" || { echo "Missing YAMNet SavedModel" >&2; exit 1; }
            test -z "${yamnet_dir:-}" || { echo "Ambiguous YAMNet SavedModel" >&2; exit 1; }
            yamnet_dir="$PWD/${artifact%/saved_model.pb}"
        done
        test -r models/face_landmarker.task || { echo "Missing Face Landmarker task" >&2; exit 1; }
        set -- "$@" --model yamnet --yamnet-path "$yamnet_dir" --face-python /opt/face/bin/python \
            --face-task "$PWD/models/face_landmarker.task"
        ;;
    *) echo "Unknown ENCORE_MODEL" >&2; exit 2 ;;
esac

if [ "${ENCORE_REQUIRE_GPU:-0}" = 1 ]; then
    test "${ENCORE_MODEL:-loudness}" = yamnet || { echo "GPU smoke requires YAMNet" >&2; exit 2; }
    export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-0}"
    python - <<'PY'
import tensorflow as tf

tf.config.set_soft_device_placement(False)
with tf.device("/GPU:0"):
    value = tf.matmul(tf.ones((2, 2)), tf.ones((2, 2)))
    if "GPU:" not in value.device or value.numpy()[0, 0] != 2:
        raise RuntimeError("GPU computation failed")
print("GPU compute ready", value.device, flush=True)
PY
fi

case "${ENCORE_HISTORY:-0}" in
    0) ;;
    1) set -- "$@" --record-history ;;
    *) echo "ENCORE_HISTORY must be 0 or 1" >&2; exit 2 ;;
esac
exec "$@"
