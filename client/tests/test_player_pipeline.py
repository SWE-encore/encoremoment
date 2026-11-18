"""End-to-end checks of the GStreamer player on a generated test clip.

Needs GStreamer 1.24 with PyGObject, and ffmpeg to generate the clip. Audio
goes to a fakesink so the tests run without a sound device.
"""

import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

CLIENT = Path(__file__).resolve().parents[1]
MAKE_VIDEO = CLIENT / "tools" / "make_test_video.sh"

try:
    from encore_client.player.pipeline import PlayerPipeline
except (ImportError, ValueError) as exc:  # no PyGObject / GStreamer
    PlayerPipeline = None
    IMPORT_ERROR = exc


def make_clip(directory: Path, name: str, seconds: int = 8) -> Path:
    out = directory / name
    subprocess.run([str(MAKE_VIDEO), str(out), str(seconds)], check=True, capture_output=True)
    return out


@unittest.skipIf(PlayerPipeline is None, "GStreamer/PyGObject not available")
@unittest.skipIf(shutil.which("ffmpeg") is None, "ffmpeg not available")
class PlayerPipelineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.clips = {ext: make_clip(Path(cls.tmp.name), f"clip.{ext}") for ext in ("mp4", "webm")}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.anchors = []
        self.frames = []
        self.player = PlayerPipeline("ses-local-test", self.anchors.append, audio_sink="fakesink sync=true")

    def tearDown(self):
        self.player.close()

    def pump(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            frame = self.player.poll()
            if frame is not None:
                self.frames.append(frame)
            time.sleep(0.005)

    def wait_for(self, predicate, timeout=5.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.pump(0.02)
            if predicate():
                return
        self.fail(f"timed out; anchors={[a['playback'] for a in self.anchors]} error={self.player.error}")

    def states(self):
        """(epoch relative to the first anchor + 1, state) for each anchor."""
        base = self.anchors[0]["playback"]["epoch"] - 1 if self.anchors else 0
        return [(a["playback"]["epoch"] - base, a["playback"]["state"]) for a in self.anchors]

    def test_open_play_pause_seek_rate_eos(self):
        for ext, clip in self.clips.items():
            with self.subTest(container=ext):
                self.anchors.clear()
                self.frames.clear()
                p = self.player
                p.open(clip)
                self.wait_for(lambda: len(self.anchors) == 1 and self.frames)
                self.assertIsNone(p.error)
                self.assertTrue(p.has_video and p.has_audio)
                self.assertEqual(self.anchors[0]["playback"]["position_ms"], 0)
                self.assertEqual(self.anchors[0]["playback"]["state"], "paused")
                self.assertEqual((self.frames[-1].width, self.frames[-1].height), (1280, 720))

                p.play()
                self.wait_for(lambda: self.states()[-1] == (1, "playing"))
                self.pump(1.0)
                p.pause()
                self.wait_for(lambda: self.states()[-1] == (1, "paused"))
                paused = self.anchors[-1]["playback"]["position_ms"]
                self.assertAlmostEqual(paused, 1000, delta=250)

                p.seek(5000)
                self.wait_for(lambda: self.states()[-1] == (2, "paused"))
                self.assertAlmostEqual(self.anchors[-1]["playback"]["position_ms"], 5000, delta=50)
                self.assertAlmostEqual(self.frames[-1].pts_ms, 5000, delta=100)

                p.play()
                self.wait_for(lambda: self.states()[-1] == (2, "playing"))
                start = self.anchors[-1]
                self.pump(0.5)
                self.assertTrue(p.set_rate(2.0))
                rate_anchor = self.anchors[-1]
                self.assertEqual(rate_anchor["playback"]["rate"], 2.0)
                # The anchor before the rate change predicts the position at the change.
                predicted = start["playback"]["position_ms"] + (
                    rate_anchor["capture_ms"] - start["capture_ms"]
                )
                self.assertAlmostEqual(rate_anchor["playback"]["position_ms"], predicted, delta=150)

                self.wait_for(lambda: p.eos, timeout=6)
                self.wait_for(lambda: self.states()[-1][1] == "paused")
                self.assertAlmostEqual(self.anchors[-1]["playback"]["position_ms"], 8000, delta=100)
                seqs = [a["seq"] for a in self.anchors]
                self.assertEqual(seqs, list(range(seqs[0], seqs[0] + len(seqs))))

    def test_heartbeat_anchor_keeps_epoch(self):
        self.player.heartbeat_ms = 400
        self.player.open(self.clips["webm"])
        self.wait_for(lambda: len(self.anchors) == 1)
        self.player.play()
        self.pump(1.5)
        playing = [a for a in self.anchors if a["playback"]["state"] == "playing"]
        self.assertGreaterEqual(len(playing), 3)
        self.assertEqual({a["playback"]["epoch"] for a in playing}, {1})

    def test_play_after_end_restarts_from_zero_in_new_epoch(self):
        p = self.player
        p.open(self.clips["webm"])
        self.wait_for(lambda: len(self.anchors) == 1)
        p.seek(7_500)
        self.wait_for(lambda: self.states()[-1] == (2, "paused"))
        p.play()
        self.wait_for(lambda: p.eos, timeout=4)
        self.wait_for(lambda: self.states()[-1][1] == "paused")
        p.play()
        self.wait_for(lambda: self.states()[-1] == (3, "playing"))
        self.assertLess(self.anchors[-1]["playback"]["position_ms"], 200)

    def test_mute_does_not_send_gap_buffers(self):
        # volume=0 makes GStreamer emit GAP buffers, which stalls a real audio
        # sink and the pipeline clock with it.
        from encore_client.player.pipeline import MIN_VOLUME

        self.player.open(self.clips["webm"])
        self.wait_for(lambda: self.player.has_audio)
        self.player.set_volume(0.0)
        volume = self.player._volume.get_property("volume")
        self.assertGreater(volume, 0.0)
        self.assertAlmostEqual(volume, MIN_VOLUME, places=7)

    def test_missing_file(self):
        with self.assertRaises(FileNotFoundError):
            self.player.open(Path(self.tmp.name) / "nope.mp4")


if __name__ == "__main__":
    unittest.main()
