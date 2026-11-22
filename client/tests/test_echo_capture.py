"""EM-10. No camera, microphone or audio output device is opened by these tests."""
import tempfile
import time
import unittest
import wave
from collections import deque
from pathlib import Path
from types import SimpleNamespace

from encore_client.sensing.live_mic import LiveMicrophone, QtAudioInput
from encore_client.session.session import Conn

try:
    from encore_client.player.pipeline import Gst, PlayerPipeline
except (ImportError, ValueError):
    PlayerPipeline = None


class RawTrackTest(unittest.TestCase):
    def test_device_clock_continues_across_two_gibibytes(self):
        # 48 kHz stereo float capture reaches 2 GiB after roughly 93 minutes.
        rate, frame_bytes = 48_000, 8
        device_block = bytes(480 * frame_bytes)
        prior_bytes = (2**31 - 1) // len(device_block) * len(device_block)
        prior_ms = prior_bytes // frame_bytes * 1000 // rate
        origin = 1000

        def duration_for_bytes(size):
            if size > 2**31 - 1:
                raise OverflowError("QAudioFormat.durationForBytes takes a signed 32-bit byte count")
            return size // frame_bytes * 1_000_000 // rate

        sensor = SimpleNamespace(enabled=True, generation=1)
        session = SimpleNamespace(state=Conn.CONNECTED, view=SimpleNamespace(sensors={"microphone": sensor}))
        ends = []
        clock = iter((origin + prior_ms + 10, origin + prior_ms + 20))
        capture = object.__new__(QtAudioInput)
        capture.mic = LiveMicrophone(session, clock=lambda: next(clock))
        capture.mic.feed = lambda pcm, end_ms: ends.append(end_ms)
        capture._io = SimpleNamespace(readAll=lambda: SimpleNamespace(data=lambda: device_block))
        capture.format = SimpleNamespace(bytesPerFrame=lambda: frame_bytes, sampleRate=lambda: rate,
                                         durationForBytes=duration_for_bytes)
        capture.converter = SimpleNamespace(convert=lambda data: bytes(320), delay_ms=0)
        capture._generation, capture._device_bytes, capture._capture_start_ms = 1, prior_bytes, origin

        capture._read()
        capture._read()
        self.assertGreater(capture._device_bytes, 2**31 - 1)
        self.assertEqual(ends, [origin + prior_ms + 10, origin + prior_ms + 20])

    def test_archive_gets_raw_and_mixed_chunk_is_never_reported_active(self):
        raw, uploaded = [], []
        session = SimpleNamespace(
            state=Conn.CONNECTED, view=SimpleNamespace(sensors={"microphone": SimpleNamespace(enabled=True, generation=1)}),
            send_audio=lambda chunk, stamp, aec: uploaded.append((chunk, stamp, aec)) or True,
        )
        mic = LiveMicrophone(session, on_pcm=lambda chunk, stamp: raw.append((chunk, stamp)), keep_s=0)
        for i, status in enumerate(("warming_up", "active")):
            mic.feed(bytes(8000), end_ms=1250 + 250*i, raw_pcm=b"\x01\x00"*4000, aec=status)
        self.assertEqual(raw, [(b"\x01\x00"*8000, 1000)])
        self.assertEqual(uploaded, [(bytes(16000), 1000, "uncancelled")])
        self.assertFalse(mic.recording)

    def test_jitter_and_resampler_delay_keep_capture_time_and_aec_with_pcm(self):
        # Three contiguous 10 ms device buffers are delivered at uneven intervals.
        # The 48 -> 16 kHz converter emits no output, then 15 ms, then 15 ms.
        class BufferedConverter:
            def __init__(self):
                self.pending = b""
                self.sizes = iter((0, 480, 480))

            def convert(self, block):
                self.pending += block[:len(block) // 3]
                size = next(self.sizes)
                result, self.pending = self.pending[:size], self.pending[size:]
                return result

            @property
            def delay_ms(self):
                return len(self.pending) // 32

        stamps, uploads, recording = [], [], []
        states = iter(("warming_up", "active", "reference_unavailable"))

        def cancel_echo(block, capture_ms):
            stamps.append(capture_ms)
            return bytes(len(block)), next(states)

        session = SimpleNamespace(
            state=Conn.CONNECTED, view=SimpleNamespace(sensors={"microphone": SimpleNamespace(enabled=True, generation=1)}),
            send_audio=lambda pcm, stamp, aec: uploads.append((pcm, stamp, aec)) or True,
        )
        clock = iter((1010, 1070, 1071))
        mic = LiveMicrophone(session, chunk_ms=10, clock=lambda: next(clock),
                             player=SimpleNamespace(cancel_echo=cancel_echo),
                             on_pcm=lambda raw, stamp: recording.append((raw, stamp)))
        capture = object.__new__(QtAudioInput)
        capture.mic = mic
        capture.format = SimpleNamespace(bytesPerFrame=lambda: 2, sampleRate=lambda: 48_000)
        reads = iter(bytes([v]) * 960 for v in (1, 2, 3))
        capture._io = SimpleNamespace(readAll=lambda: SimpleNamespace(data=lambda: next(reads)))
        capture.converter = SimpleNamespace(convert=bytes, delay_ms=0)
        capture._generation = 1
        capture._capture_start_ms = None
        capture._device_bytes = 0
        capture._resampled_states = deque()
        capture._capture_buffer = bytearray()
        capture._raw_converter, capture._clean_converter = BufferedConverter(), BufferedConverter()

        for _ in range(3):
            capture._read()
        self.assertEqual(stamps, [1000, 1010, 1020])
        self.assertEqual([(stamp, aec) for _, stamp, aec in uploads],
                         [(1000, "warming_up"), (1010, "active"), (1020, "reference_unavailable")])
        self.assertEqual([pcm for pcm, _, _ in uploads], [bytes(320)] * 3)
        self.assertEqual(recording, [(bytes([v]) * 320, 1000 + 10 * (v - 1)) for v in (1, 2, 3)])


@unittest.skipUnless(PlayerPipeline and Gst.ElementFactory.find("webrtcdsp") and
                     Gst.ElementFactory.find("webrtcechoprobe"), "GStreamer WebRTC plugins required")
class EchoPlaybackTest(unittest.TestCase):
    def test_synthetic_echo_mixture_and_seek(self):
        import numpy as np

        rate = 48_000
        far = np.random.default_rng(17).normal(0, 3800, rate * 7)
        far = np.convolve(far, [.1, .2, .4, .2, .1], "same").astype("<i2")
        far[rate*5:] = 0  # last section: only the audience-side signal remains
        player = PlayerPipeline("em10-test", audio_sink="fakesink sync=true")
        self.addCleanup(player.close)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "reference.wav"
            with wave.open(str(path), "wb") as wav:
                wav.setparams((1, 2, rate, 0, "NONE", ""))
                wav.writeframes(far.tobytes())
            player.open(path)
            deadline = time.monotonic() + 3
            while not player.has_audio or not player.tracker.last:
                player.poll()
                self.assertLess(time.monotonic(), deadline, player.error)
                time.sleep(.003)
            self.assertEqual(player.cancel_echo(bytes(960), player.clock())[1], "reference_unavailable")
            player.play()
            last = -1
            echo_in, echo_out, mixed_out, near_input = [], [], [], []
            near_only_gain = []
            states = set()
            deadline = time.monotonic() + 9
            while (pos := player.position_ms() or 0) < 6300:
                self.assertLess(time.monotonic(), deadline, player.error)
                player.poll()
                if pos >= 40 and pos // 10 > last:
                    # Microphone hears playback after a 30 ms acoustic delay.
                    first = (pos - 40)*48
                    echo = far[first:first+480] * .65
                    near = 1700 * np.sin(2*np.pi*880*(np.arange(480) + last*480)/rate)
                    mic = (echo + (near if pos >= 4000 else 0)).astype("<i2")
                    output, state = player.cancel_echo(mic.tobytes(), player.clock()-10)
                    states.add(state)
                    if state == "active":
                        output = np.frombuffer(output, dtype="<i2").astype(float)
                        if 2500 <= pos < 3950:
                            echo_in.extend(mic.astype(float))
                            echo_out.extend(output)
                        elif 4600 <= pos < 4950:
                            mixed_out.extend(output)
                            near_input.extend(near)
                        elif pos >= 5600:
                            # Recover the near tone amplitude with quadrature so the
                            # filter's delay does not look like signal loss.
                            basis = np.exp(2j*np.pi*880*np.arange(480)/rate)
                            near_only_gain.append(abs(output @ basis) / abs(near @ basis))
                    last = pos // 10
                time.sleep(.002)
            self.assertTrue({"active", "warming_up"} <= states)
            self.assertGreater(len(echo_out), rate // 2)
            self.assertGreater(len(mixed_out), rate // 6)
            erle = 10*np.log10(np.mean(np.square(echo_in))/max(1, np.mean(np.square(echo_out))))
            near_gain = np.sqrt(np.mean(np.square(mixed_out))/np.mean(np.square(near_input)))
            tone_gain = float(np.median(near_only_gain))
            print(f"Synthetic echo attenuation {erle:.1f} dB, mixture/near-end RMS {near_gain:.2f}, "
                  f"near-only tone gain {tone_gain:.2f}")
            self.assertGreater(tone_gain, .25)
            self.assertGreater(erle, 3)
            self.assertGreater(near_gain, .15)
            self.assertTrue(player.seek(750))
            self.assertIsNone(player._echo)
            player.pause()
            self.assertEqual(player.cancel_echo(bytes(960), player.clock())[1], "reference_unavailable")
