"""EM-10. No camera, microphone or audio output device is opened by these tests."""
import tempfile
import time
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace

from encore_client.sensing.live_mic import LiveMicrophone
from encore_client.session.session import Conn

try:
    from encore_client.player.pipeline import Gst, PlayerPipeline
except (ImportError, ValueError):
    PlayerPipeline = None


class RawTrackTest(unittest.TestCase):
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
