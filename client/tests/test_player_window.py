"""Smoke test of the Qt window on the offscreen platform.

Set ENCORE_SCREENSHOT=path.png to save what the window rendered.
"""

import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

CLIENT = Path(__file__).resolve().parents[1]

try:
    from PySide6.QtWidgets import QApplication

    from encore_client.player.window import PlayerWindow, format_ms
except (ImportError, ValueError):
    PlayerWindow = None


@unittest.skipIf(PlayerWindow is None, "PySide6 or GStreamer not available")
@unittest.skipIf(shutil.which("ffmpeg") is None, "ffmpeg not available")
class PlayerWindowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.tmp = tempfile.TemporaryDirectory()
        cls.clip = Path(cls.tmp.name) / "Window Test.webm"
        subprocess.run(
            [str(CLIENT / "tools" / "make_test_video.sh"), str(cls.clip), "6"],
            check=True,
            capture_output=True,
        )

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def spin(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.app.processEvents()
            time.sleep(0.005)

    def test_plays_and_logs_anchors(self):
        sent = []
        win = PlayerWindow("ses-local-window", "fakesink sync=true", sent.append)
        win.show()
        win.open_file(self.clip)
        self.spin(0.5)
        self.assertGreaterEqual(win.video.frames_shown, 1, "paused first frame not painted")
        win.toggle()
        self.spin(1.5)
        self.assertEqual(win.play_btn.text(), "Pause")
        self.assertGreater(win.video.frames_shown, 20)
        self.assertEqual(win.slider.maximum(), win.player.duration_ms())
        self.assertIn("/ 00:06", win.time_label.text())
        win.step(2_000)
        self.spin(0.5)
        states = [m["playback"]["state"] for m in sent]
        self.assertEqual(states[:2], ["paused", "playing"])
        self.assertEqual(len(win.anchor_log.toPlainText().splitlines()), len(sent))
        self.assertEqual(sent[-1]["playback"]["title"], "Window Test")
        if os.environ.get("ENCORE_SCREENSHOT"):
            win.grab().save(os.environ["ENCORE_SCREENSHOT"])
        win.close()

    def test_format_ms(self):
        self.assertEqual(format_ms(None), "--:--")
        self.assertEqual(format_ms(61_999), "01:01")
        self.assertEqual(format_ms(3_725_000), "1:02:05")


if __name__ == "__main__":
    unittest.main()
