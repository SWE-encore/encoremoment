"""Run MediaPipe Face Landmarker on an image; outputs are not emotion labels."""
import argparse
import json
from pathlib import Path
import time


def main():
    import mediapipe as mp
    from mediapipe.tasks import python
    from mediapipe.tasks.python import vision

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("--expect-face", action="store_true")
    args = parser.parse_args()
    model = Path(__file__).resolve().parent / "models/face_landmarker.task"
    options = vision.FaceLandmarkerOptions(
        base_options=python.BaseOptions(model_asset_path=str(model),
                                       delegate=python.BaseOptions.Delegate.CPU),
        running_mode=vision.RunningMode.IMAGE,
        num_faces=4, output_face_blendshapes=True)
    with vision.FaceLandmarker.create_from_options(options) as detector:
        image = mp.Image.create_from_file(str(args.image))
        started = time.perf_counter()
        result = detector.detect(image)
        elapsed = time.perf_counter() - started
    if args.expect_face and not result.face_landmarks:
        raise RuntimeError("No face detected in the reference portrait")
    faces = []
    for landmarks, shapes in zip(result.face_landmarks, result.face_blendshapes):
        if len(landmarks) != 478 or len(shapes) != 52:
            raise RuntimeError("Unexpected face output dimensions")
        faces.append({"landmark_count": len(landmarks), "blendshape_count": len(shapes),
                      "top5_blendshapes": [{"name": c.category_name, "score": c.score}
                          for c in sorted(shapes, key=lambda c: c.score, reverse=True)[:5]]})
    print(json.dumps({"model": "mediapipe/face_landmarker/float16/1",
                      "inference_seconds": elapsed, "faces": faces}, indent=2))


if __name__ == "__main__":
    main()
