"""Paired demo media: EMV1, camera generation, image timestamp (both BE), RGB24, encore audio frame."""
import os
import struct
import subprocess
import threading
from pathlib import Path

MAGIC = b"EMV1"
WIDTH, HEIGHT = 320, 180
RGB_BYTES = WIDTH * HEIGHT * 3


def unpack_pair(packet):
    offset = 16 + RGB_BYTES
    if len(packet) <= offset or packet[:4] != MAGIC:
        raise ValueError("invalid paired media")
    generation, capture_ms = struct.unpack_from("!IQ", packet, 4)
    return generation, capture_ms, packet[16:offset], packet[offset:]


class FaceWorker:
    """One persistent process using the existing MediaPipe virtual environment."""
    name = "mediapipe-face"

    def __init__(self, python, model):
        from .models import sha256_file, tagged

        self.sha256 = sha256_file(model)
        self.name = tagged(type(self).name, self.sha256)  # in source.model of face-assisted events
        self.lock = threading.Lock()
        self.process = subprocess.Popen(
            [python, "-u", str(Path(__file__).with_name("vision.py")), model],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"})
        if self.process.stdout.readline() != b"ready\n":
            self.close()
            raise RuntimeError("Face Landmarker failed to start")

    @property
    def ready(self):
        return self.process.poll() is None

    def __call__(self, rgb):
        with self.lock:
            self.process.stdin.write(rgb)
            self.process.stdin.flush()
            line = self.process.stdout.readline()
            if not line:
                raise RuntimeError("Face Landmarker worker stopped")
            return None if line == b"none\n" else float(line)

    def close(self):
        self.process.terminate()
        self.process.wait()
        self.process.stdin.close()
        self.process.stdout.close()


def run_worker(model_path):
    import sys
    import mediapipe as mp
    import numpy as np
    from mediapipe.tasks.python import BaseOptions, vision

    options = vision.FaceLandmarkerOptions(
        base_options=BaseOptions(model_asset_path=model_path), num_faces=4,
        output_face_blendshapes=True)
    with vision.FaceLandmarker.create_from_options(options) as detector:
        print("ready", flush=True)
        while len(rgb := sys.stdin.buffer.read(RGB_BYTES)) == RGB_BYTES:
            pixels = np.frombuffer(rgb, dtype=np.uint8).reshape(HEIGHT, WIDTH, 3)
            result = detector.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=pixels))
            smiles = []
            for face in result.face_blendshapes:
                scores = {shape.category_name: shape.score for shape in face}
                smiles.append((scores["mouthSmileLeft"] + scores["mouthSmileRight"]) / 2)
            print(max(smiles) if smiles else "none", flush=True)


if __name__ == "__main__":
    import sys
    run_worker(sys.argv[1])
