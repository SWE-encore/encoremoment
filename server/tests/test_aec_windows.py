import unittest

from encore_server.audio import AudioWindows


class AudioStatusTest(unittest.TestCase):
    def test_status_follows_all_overlapping_input_samples(self):
        windows = AudioWindows()
        results = []
        for i, state in enumerate(["reference_unavailable", "active", "active", "active"]):
            results.extend(windows.add({"capture_ms": 500*i, "generation": 1,
                                        "chunk": i, "aec": state}, bytes(16_000)))
        self.assertEqual([w.aec for w in results], ["uncancelled", "uncancelled", "active"])
