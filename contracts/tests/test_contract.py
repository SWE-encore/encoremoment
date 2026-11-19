"""Contract tests for EM-1.

Run from the repository root:
    python -m pip install -r contracts/requirements-dev.txt
    python -m unittest discover -s contracts/tests -v
"""

import copy
import json
import sys
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "reference"))

from encore_contract import (  # noqa: E402
    MAX_MS,
    SessionView,
    clip_windows,
    decode_audio_frame,
    encode_audio_frame,
    replay,
)

SCHEMA = json.loads((ROOT / "schema" / "messages.schema.json").read_text())
VALIDATOR = Draft202012Validator(SCHEMA)
SAMPLES = sorted((ROOT / "samples").glob("*.json"))


def load(path):
    return json.loads(path.read_text())


def schema_errors(msg):
    return [e.message for e in VALIDATOR.iter_errors(msg)]


class SchemaTest(unittest.TestCase):
    def test_schema_is_valid(self):
        Draft202012Validator.check_schema(SCHEMA)

    def test_every_sample_message_matches_schema(self):
        self.assertGreaterEqual(len(SAMPLES), 4)
        for path in SAMPLES:
            for i, step in enumerate(load(path)["messages"]):
                with self.subTest(sample=path.name, step=i):
                    self.assertEqual(schema_errors(step["msg"]), [])

    def _event(self):
        msg = load(ROOT / "samples" / "01_single_event.json")["messages"]
        return copy.deepcopy(next(s["msg"] for s in msg if s["msg"]["kind"] == "event_update"))

    def test_final_requires_end(self):
        msg = self._event()
        msg["event"].update(phase="FINAL", revision=2)
        msg["event"]["capture"]["end_ms"] = None
        self.assertNotEqual(schema_errors(msg), [])

    def test_open_must_be_revision_one(self):
        msg = self._event()
        msg["event"]["revision"] = 2
        self.assertNotEqual(schema_errors(msg), [])

    def test_unknown_reaction_type_rejected(self):
        msg = self._event()
        msg["event"]["reaction"]["type"] = "sigh"
        self.assertNotEqual(schema_errors(msg), [])

    def test_confidence_and_intensity_are_bounded(self):
        for path in (("reaction", "confidence"), ("intensity", "combined")):
            msg = self._event()
            msg["event"][path[0]][path[1]] = 1.2
            with self.subTest(field=".".join(path)):
                self.assertNotEqual(schema_errors(msg), [])

    def test_unknown_fields_rejected(self):
        msg = self._event()
        msg["event"]["emotion"] = "happy"
        self.assertNotEqual(schema_errors(msg), [])

    def test_audio_format_is_fixed_and_chunks_are_bounded(self):
        sample = load(ROOT / "samples" / "07_microphone_audio.json")
        header = next(s["msg"] for s in sample["messages"] if s["msg"]["kind"] == "audio_chunk")
        for field, value in (("sample_rate", 48_000), ("channels", 2), ("encoding", "f32le"),
                             ("samples", 16_001), ("samples", 0), ("aec", "maybe")):
            with self.subTest(field=field, value=value):
                self.assertNotEqual(schema_errors(header | {field: value}), [])


class ClipRuleTest(unittest.TestCase):
    def test_single_trigger_is_25_seconds(self):
        (w,) = clip_windows([100_000])
        self.assertEqual((w.start_ms, w.end_ms, w.length_ms), (85_000, 110_000, 25_000))

    def test_each_refire_extends_by_5_seconds(self):
        (w,) = clip_windows([100_000, 101_000, 102_000])
        self.assertEqual(w.end_ms, 120_000)

    def test_refire_on_the_end_boundary_still_extends(self):
        (w,) = clip_windows([100_000, 110_000])
        self.assertEqual(w.end_ms, 115_000)

    def test_refire_after_end_starts_new_clip(self):
        a, b = clip_windows([100_000, 110_001])
        self.assertEqual((b.start_ms, b.end_ms, b.linked_to), (95_001, 120_001, None))

    def test_never_exceeds_max_length(self):
        windows = clip_windows(list(range(100_000, 400_000, 1_000)))
        self.assertTrue(all(w.length_ms <= MAX_MS for w in windows))
        self.assertEqual(windows[0].length_ms, MAX_MS)
        self.assertEqual(windows[1].linked_to, 0)


class SampleReplayTest(unittest.TestCase):
    def test_samples_produce_expected_result(self):
        for path in SAMPLES:
            sample = load(path)
            with self.subTest(sample=path.name):
                self.assertEqual(replay(sample).summary(), sample["expected"])

    def test_replaying_a_sample_twice_does_not_duplicate_events(self):
        # EM-23 acceptance: sending the same results twice leaves one event each.
        for path in SAMPLES:
            sample = load(path)
            view = SessionView(sample["session_id"])
            for step in sample["messages"] * 2:
                view.receive(step["dir"], step["msg"])
            with self.subTest(sample=path.name):
                self.assertEqual(view.summary()["events"], sample["expected"]["events"])


class ReceiverRulesTest(unittest.TestCase):
    def setUp(self):
        self.sample = load(ROOT / "samples" / "01_single_event.json")
        self.view = replay(self.sample)
        self.final = copy.deepcopy(self.sample["messages"][-1]["msg"])

    def _send(self, mutate):
        msg = copy.deepcopy(self.final)
        msg["cursor"] = self.view.next_cursor
        msg["event"]["revision"] += 1
        mutate(msg["event"])
        self.view.receive("server->pc", msg)

    def test_update_after_final_rejected(self):
        self._send(lambda ev: None)
        self.assertEqual(self.view.rejected["update_after_final"], 1)

    def test_reaction_type_cannot_change(self):
        self.view.events["evt-0001"]["phase"] = "UPDATE"
        self._send(lambda ev: ev["reaction"].update(type="scream"))
        self.assertEqual(self.view.rejected["immutable_field_changed"], 1)

    def test_triggers_can_only_be_appended(self):
        self.view.events["evt-0001"]["phase"] = "UPDATE"
        self._send(lambda ev: ev["capture"].update(triggers_ms=[129_900]))
        self.assertEqual(self.view.rejected["triggers_rewritten"], 1)

    def test_wrong_direction_rejected(self):
        self.view.receive("pc->server", self.final)
        self.assertEqual(self.view.rejected["wrong_direction"], 1)

    def test_gap_holds_later_cursors(self):
        msg = copy.deepcopy(self.final)
        msg["cursor"] = self.view.next_cursor + 1
        self.view.receive("server->pc", msg)
        self.assertEqual(self.view.summary()["unapplied_cursors"], [msg["cursor"]])


class AudioRulesTest(unittest.TestCase):
    def setUp(self):
        self.sample = load(ROOT / "samples" / "07_microphone_audio.json")
        self.header = copy.deepcopy(
            next(s["msg"] for s in self.sample["messages"] if s["msg"]["kind"] == "audio_chunk")
        )

    def test_sample_counts_chunks_and_gaps(self):
        view = replay(self.sample)
        self.assertEqual((view.audio_chunks, view.audio_gaps), (4, 1))

    def test_audio_needs_the_current_microphone_generation(self):
        view = SessionView(self.sample["session_id"])
        view.receive("pc->server", self.header)  # microphone never turned on
        mic_on = self.sample["messages"][0]["msg"]
        view.receive("pc->server", mic_on)
        view.receive("pc->server", self.header | {"generation": 2})
        self.assertEqual(view.rejected["stale_audio_generation"], 2)
        view.receive("pc->server", self.header)
        self.assertEqual(view.audio_chunks, 1)

    def test_audio_is_sent_by_the_pc_only(self):
        view = SessionView(self.sample["session_id"])
        view.receive("server->pc", self.header)
        self.assertEqual(view.rejected["wrong_direction"], 1)

    def test_frame_round_trip(self):
        pcm = bytes(range(256)) * 62 + bytes(16_000 - 256 * 62)
        frame = encode_audio_frame(self.header, pcm)
        self.assertEqual(decode_audio_frame(frame), (self.header, pcm))

    def test_malformed_frames_are_refused(self):
        frame = encode_audio_frame(self.header, bytes(16_000))
        bad = {
            "too short": b"\x00\x00",
            "truncated payload": frame[:-2],
            "extra payload": frame + b"\x00\x00",
            "header length past the end": b"\x00\x01\x00\x00" + frame[4:],
            "not an audio header": encode_audio_frame(self.header, bytes(16_000)).replace(
                b'"kind":"audio_chunk"', b'"kind":"audio_chunX"'),
        }
        for name, data in bad.items():
            with self.subTest(name):
                with self.assertRaises(ValueError):
                    decode_audio_frame(data)
        with self.assertRaises(ValueError):
            encode_audio_frame(self.header, bytes(15_998))  # 7_999 samples, header says 8_000


if __name__ == "__main__":
    unittest.main()
