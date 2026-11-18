"""Create sample clips for each status and the clip index. (Requires FFmpeg)

Run: python scripts/make_samples.py
Output: samples/clips/*.mp4, samples/clip_index.json

Five samples:
  1) ready       - normal 25 s clip (15 s before + 10 s after the trigger)
  2) partial     - 18 s clip with the first 7 s missing (camera enabled late)
  3) pending     - still being created (no file)
  4) unavailable - reaction while the camera was off (no footage, metadata only)
  5) ready, but the file does not exist - verifies "no playback link for a missing file"
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
CLIPS = SAMPLES / "clips"


def make_video(out: Path, seconds: int, freq: int) -> None:
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size=640x360:rate=30",
        "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(out),
    ]
    subprocess.run(cmd, check=True)


def main() -> None:
    if shutil.which("ffmpeg") is None:
        sys.exit("FFmpeg is required. Ubuntu: sudo apt install ffmpeg / macOS: brew install ffmpeg")
    CLIPS.mkdir(parents=True, exist_ok=True)
    make_video(CLIPS / "clip_001_ready.mp4", 25, 440)
    make_video(CLIPS / "clip_002_partial.mp4", 18, 660)
    missing = CLIPS / "clip_005_missing.mp4"
    if missing.exists():
        missing.unlink()

    title = "Sample Movie (test player)"
    index = {"clips": [
        {"clip_id": "clip_001", "event_type": "laughter", "timecode": "00:12:05",
         "intensity": 0.91, "status": "ready", "path": "clips/clip_001_ready.mp4", "title": title},
        {"clip_id": "clip_002", "event_type": "scream", "timecode": "00:31:40",
         "intensity": 0.78, "status": "partial", "path": "clips/clip_002_partial.mp4",
         "gaps": [[0, 7]], "title": title},
        {"clip_id": "clip_003", "event_type": "gasp", "timecode": "00:45:12",
         "intensity": 0.66, "status": "pending", "path": None, "title": title},
        {"clip_id": "clip_004", "event_type": "laughter", "timecode": "00:05:50",
         "intensity": 0.52, "status": "unavailable", "path": None, "title": title},
        {"clip_id": "clip_005", "event_type": "gasp", "timecode": "00:58:03",
         "intensity": 0.84, "status": "ready", "path": "clips/clip_005_missing.mp4", "title": title},
    ]}
    (SAMPLES / "clip_index.json").write_text(json.dumps(index, indent=2), encoding="utf-8")
    print(f"Samples created: {SAMPLES}")


if __name__ == "__main__":
    main()
