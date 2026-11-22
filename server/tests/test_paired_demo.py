import json
import struct
import threading
import unittest

from encore_server.contract import schema_errors
from encore_server.session import ServerSession
from encore_server.vision import MAGIC, RGB_BYTES


class Audio:
    name = "test-audio"
    laughter = .10

    def __call__(self, pcm):
        return {"laughter": self.laughter}


class PairedDemo(unittest.TestCase):
    def setUp(self):
        self.smile = None
        self.audio = Audio()
        self.session = ServerSession("demo", self.audio, face_model=lambda rgb: self.smile)
        self.control("microphone", True, 1)
        self.control("camera", True, 1)

    def control(self, sensor, enabled, generation, capture_ms=0):
        result = self.session.on_text(json.dumps(dict(schema="encore.v1", session_id="demo",
            kind="control_update", sensor=sensor, generation=generation, enabled=enabled,
            seq=generation, capture_ms=capture_ms, idempotency_key=f"{sensor}-{generation}")))
        self.assertEqual(result[0]["status"], "applied")

    def packet(self, index, *, paired=True, generation=1, shift=0):
        header = json.dumps(dict(schema="encore.v1", session_id="demo", seq=index+2,
            kind="audio_chunk", capture_ms=index*500, generation=1, chunk=index,
            samples=8000, encoding="pcm_s16le", channels=1, sample_rate=16000, aec="off")).encode()
        data = struct.pack("!I", len(header)) + header + bytes(16000)
        if paired:
            data = MAGIC + struct.pack("!IQ", generation, index*500+499+shift) + bytes(RGB_BYTES) + data
        out = self.session.on_audio(data)
        # Simulate the time between real packets, deterministically.
        if not getattr(self, "blocked", False):
            self.session._collect_face(grace=.5)
        return out

    def test_fusion_and_replay(self):
        for i in range(4):
            self.packet(i)
        self.assertFalse(self.session.log)
        self.smile = .20
        for i in range(4, 15):
            self.packet(i)
            self.packet(i)
        self.session.end()
        events = self.session.results_after(0)
        self.assertTrue(all(not schema_errors(msg) for msg in events))
        self.assertEqual([m["event"]["phase"] for m in events], ["OPEN", "UPDATE", "FINAL"])
        self.assertEqual(len({m["event"]["event_id"] for m in events}), 1)
        self.assertEqual(events[0]["event"]["intensity"]["visual"], .20)
        self.assertEqual(events[0]["event"]["reaction"]["confidence"], .10)

    def test_neutral_face_and_missing_face_are_distinct(self):
        self.packet(0)
        self.smile = 0.0
        self.packet(1)
        self.assertTrue(self.session.face_detected)
        self.assertEqual((self.session.faces_seen,self.session.face_frames), (1,2))
        self.smile = None
        self.packet(2)
        self.assertFalse(self.session.face_detected)
        self.assertFalse(self.session.log)

    def test_stale_camera_and_audio_only(self):
        self.control("camera", False, 2)
        self.assertEqual(self.packet(0), [])
        self.packet(0, paired=False)
        self.audio.laughter = .25
        self.packet(1, paired=False)
        self.assertEqual(self.session.log[0]["event"]["phase"], "OPEN")
        self.audio.laughter = .4
        self.packet(2, paired=False)
        self.assertIsNone(self.session.log[0]["event"]["intensity"]["visual"])

    def test_laughter_recovers_during_quiet_then_finishes_once(self):
        self.audio.laughter = .2499
        self.packet(0)
        self.packet(1)
        self.assertFalse(self.session.log)
        self.audio.laughter = .25
        self.packet(2)
        self.assertEqual([m["event"]["phase"] for m in self.session.log], ["OPEN"])
        self.audio.laughter = 0
        for i in range(3, 8):
            score = self.packet(i)[0]
        self.assertGreater(score["quiet_elapsed_ms"], 2000)
        self.assertLess(score["quiet_elapsed_ms"], 3000)
        self.audio.laughter = .125  # holds the open event without retriggering
        self.assertEqual(self.packet(8)[0]["quiet_elapsed_ms"], 0)
        end_ms = self.session.detector.open["laughter"].last_active_ms
        self.audio.laughter = 0
        for i in range(9, 15):
            self.assertLess(self.packet(i)[0]["quiet_elapsed_ms"], 3000)
        out = self.packet(15)
        self.assertEqual(out[-1]["event"]["phase"], "FINAL")
        self.assertEqual(out[-1]["event"]["capture"]["end_ms"], end_ms)
        for i in range(16, 20):
            self.packet(i)
        self.assertEqual([m["event"]["phase"] for m in self.session.log], ["OPEN", "FINAL"])

    def test_smile_without_laughter_does_not_record(self):
        self.smile = 1.0
        self.audio.laughter = .02
        for i in range(6):
            self.packet(i)
        self.assertFalse(self.session.log)

    def test_stream_idle_respects_demo_quiet_period(self):
        now = [0.0]
        self.session._clock = lambda: now[0]
        self.audio.laughter = .3
        self.packet(0)
        self.packet(1)
        now[0] = 2.99
        self.assertEqual(self.session.on_idle(), [])
        now[0] = 3.0
        self.assertEqual(self.session.on_idle()[0]["event"]["phase"], "FINAL")
        self.assertEqual(self.session.on_idle(), [])

    def test_future_face_cannot_confirm_past_audio(self):
        self.smile = 1
        for i in range(4):
            self.packet(i, shift=5000)
        self.assertFalse(self.session.log)

    def test_reenabled_camera_does_not_admit_a_frame_captured_while_off(self):
        self.control("camera", False, 2, capture_ms=1000)
        self.control("camera", True, 3, capture_ms=1800)
        self.packet(2, generation=3)  # the frame was captured at 1499 ms
        self.assertEqual((self.session.audio_chunks, self.session.face_frames), (1, 0))
        self.packet(3, generation=3)  # captured at 1999 ms
        self.assertEqual((self.session.audio_chunks, self.session.face_frames), (2, 1))

    def test_strong_audio_while_vision_blocked(self):
        started, release = threading.Event(), threading.Event()
        def face(rgb):
            started.set()
            release.wait(2)
            return 1.
        self.blocked = True
        self.session.face_model = face
        self.audio.laughter = .25
        try:
            self.packet(0)
            self.assertTrue(started.wait(1))
            self.packet(1)
            self.assertEqual(self.session.log[0]["event"]["phase"], "OPEN")
            self.assertFalse(self.session.pending_face[-1].done())
            self.control("camera", False, 2)
            release.set()
            self.session._collect_face(grace=1)
            self.assertFalse(self.session.smiles)
        finally:
            release.set()
