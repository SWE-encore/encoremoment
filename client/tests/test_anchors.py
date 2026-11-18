import json
import unittest
from pathlib import Path

from encore_client.player.anchors import AnchorTracker, content_identity

REPO = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO / "contracts" / "schema" / "messages.schema.json"


class FakeClock:
    def __init__(self, t=1_000):
        self.t = t

    def __call__(self):
        return self.t


class AnchorTrackerTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.sent = []
        self.tracker = AnchorTracker("ses-local-test", self.sent.append, self.clock)
        self.tracker.new_content("file:abc123", "Sample Movie")

    def test_first_content_is_epoch_one(self):
        msg = self.tracker.report(0, 1.0, playing=True)
        self.assertEqual(msg["playback"]["epoch"], 1)
        self.assertEqual(msg["seq"], 0)

    def test_seek_and_new_content_change_epoch_pause_and_rate_do_not(self):
        t = self.tracker
        epochs = [t.report(0, 1.0, True)["playback"]["epoch"]]
        epochs.append(t.report(4_000, 1.0, False)["playback"]["epoch"])  # pause
        epochs.append(t.report(4_000, 1.5, True)["playback"]["epoch"])  # rate change
        t.seeked()
        epochs.append(t.report(30_000, 1.5, True)["playback"]["epoch"])
        t.new_content("file:def456", "Other Movie")
        epochs.append(t.report(0, 1.0, True)["playback"]["epoch"])
        self.assertEqual(epochs, [1, 1, 1, 2, 3])
        self.assertEqual([m["seq"] for m in self.sent], [0, 1, 2, 3, 4])

    def test_timecode_while_playing_paused_and_at_other_rates(self):
        t = self.tracker
        t.report(10_000, 1.0, True, capture_ms=100_000)
        t.report(13_000, 1.0, False, capture_ms=103_000)
        t.report(13_000, 2.0, True, capture_ms=110_000)
        self.assertIsNone(t.map_time(99_999))  # before any anchor: pending, not guessed
        self.assertEqual(t.map_time(101_500), (1, 11_500))
        self.assertEqual(t.map_time(108_000), (1, 13_000))  # paused: position holds
        self.assertEqual(t.map_time(111_000), (1, 15_000))  # 2x after resume

    def test_report_before_content_fails(self):
        with self.assertRaises(RuntimeError):
            AnchorTracker("ses-local-test").report(0, 1.0, True)

    def test_bad_inputs_rejected(self):
        with self.assertRaises(ValueError):
            self.tracker.new_content("has space", "x")
        with self.assertRaises(ValueError):
            self.tracker.report(0, 0, True)

    def test_content_identity_is_stable_and_contract_safe(self):
        a = content_identity("/tmp/Some Movie (2024).mp4")
        self.assertEqual(a, content_identity("/tmp/Some Movie (2024).mp4"))
        self.assertEqual(a[1], "Some Movie (2024)")
        self.assertRegex(a[0], r"^[A-Za-z0-9_.:-]{1,128}$")

    @unittest.skipUnless(SCHEMA_PATH.exists(), "contract schema not present")
    def test_messages_match_contract_schema(self):
        from jsonschema import Draft202012Validator

        validator = Draft202012Validator(json.loads(SCHEMA_PATH.read_text()))
        t = self.tracker
        t.report(0, 1.0, True)
        t.report(5_000, 1.0, False)
        t.seeked()
        t.report(60_000, 0.5, True)
        for msg in self.sent:
            self.assertEqual([e.message for e in validator.iter_errors(msg)], [])


if __name__ == "__main__":
    unittest.main()
