"""Raw microphone adapter: device reads become timestamped 0.5 s chunks (R8, R9)."""

from types import SimpleNamespace

import pytest

from encore_client.sensing.live_mic import LiveMicrophone
from encore_client.session.session import Conn

READ = bytes(3_200)  # 0.1 s, a typical device read


class FakeSession:
    def __init__(self, enabled=True, generation=1):
        self.state = Conn.CONNECTED
        self.mic = SimpleNamespace(enabled=enabled, generation=generation)
        self.view = SimpleNamespace(sensors={"microphone": self.mic})
        self.chunks = []

    def send_audio(self, pcm, capture_ms, aec="off"):
        self.chunks.append((len(pcm), capture_ms, aec))
        return True


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def feed(mic, clock, n, step_ms=100):
    for _ in range(n):
        clock.t += step_ms
        mic.feed(READ)


def test_reads_become_half_second_chunks_stamped_with_their_first_sample():
    session, clock = FakeSession(), Clock(10_000)
    mic = LiveMicrophone(session, clock=clock)
    feed(mic, clock, 12)  # 1.2 s of audio, read every 0.1 s
    # The first read ended at 10 100 ms, so its first sample was captured at 10 000 ms.
    assert session.chunks == [(16_000, 10_000, "off"), (16_000, 10_500, "off")]
    assert mic.sent == 2 and len(mic.recording) == 32_000


def test_nothing_is_captured_while_the_microphone_is_off():
    session, clock = FakeSession(enabled=False), Clock(0)
    mic = LiveMicrophone(session, clock=clock)
    feed(mic, clock, 10)
    assert session.chunks == [] and mic.recording == bytearray()
    session.mic.enabled, session.mic.generation = True, 3
    feed(mic, clock, 5)
    assert session.chunks == [(16_000, 1_000, "off")]  # a fresh run from when it came back on


def test_a_new_generation_starts_a_new_run():
    session, clock = FakeSession(), Clock(0)
    mic = LiveMicrophone(session, clock=clock)
    feed(mic, clock, 3)  # 0.3 s buffered, not yet a chunk
    session.mic.generation = 2
    feed(mic, clock, 5)
    assert session.chunks == [(16_000, 300, "off")]  # the 0.3 s of generation 1 is not sent as generation 2


def test_lost_samples_restart_the_timestamps():
    session, clock = FakeSession(), Clock(0)
    mic = LiveMicrophone(session, clock=clock)
    feed(mic, clock, 5)
    clock.t += 2_000  # the device stalled and dropped 2 s
    feed(mic, clock, 5)
    assert [c[1] for c in session.chunks] == [0, 2_500]


def test_the_first_samples_after_the_device_starts_are_discarded():
    session, clock = FakeSession(), Clock(0)
    mic = LiveMicrophone(session, clock=clock)
    mic.device_started(settle_ms=150)
    feed(mic, clock, 7)  # 0.7 s read; the first 0.15 s is the device settling
    # The kept audio starts at 0.15 s and its timestamps say so.
    assert session.chunks == [(16_000, 150, "off")]
    assert len(mic.recording) == 16_000


def test_exact_zeros_from_the_device_are_counted_as_a_muted_input():
    session, clock = FakeSession(), Clock(0)
    mic = LiveMicrophone(session, clock=clock)
    feed(mic, clock, 14)
    assert mic.zero_ms == 1_400 and not mic.muted
    feed(mic, clock, 6)  # 2 s of exact zeros
    assert mic.zero_ms == 2_000 and mic.muted
    clock.t += 100
    mic.feed(b"\x01\x00" + bytes(3_198))  # any real sound, however quiet
    assert mic.zero_ms == 0 and not mic.muted


def test_a_device_opened_again_does_not_keep_the_zeros_of_its_last_run():
    session, clock = FakeSession(), Clock(0)
    mic = LiveMicrophone(session, clock=clock)
    feed(mic, clock, 20)
    assert mic.muted
    mic.device_started()  # the microphone was turned off and on again
    assert mic.zero_ms == 0 and not mic.muted


def test_qt_input_opens_the_default_device_in_the_contract_format():
    pytest.importorskip("PySide6.QtMultimedia")
    from PySide6.QtMultimedia import QMediaDevices
    from PySide6.QtWidgets import QApplication

    from encore_client.sensing.live_mic import QtAudioInput

    QApplication.instance() or QApplication([])
    if QMediaDevices.defaultAudioInput().isNull():
        with pytest.raises(RuntimeError, match="no microphone"):
            QtAudioInput(LiveMicrophone(FakeSession()))
        return
    QtAudioInput(LiveMicrophone(FakeSession()))  # opened only on start(): nothing is recorded here


@pytest.mark.parametrize("state", [Conn.CONNECTING, Conn.DISCONNECTED, Conn.ENDED])
def test_late_device_reads_are_not_recorded_outside_a_connection(state):
    session, clock = FakeSession(), Clock(10_000)
    mic = LiveMicrophone(session, clock=clock)
    feed(mic, clock, 3)  # an incomplete chunk must also be discarded
    session.state = state
    feed(mic, clock, 5)
    assert not mic.recording and not session.chunks
    session.state = Conn.CONNECTED
    feed(mic, clock, 5)
    assert session.chunks == [(16_000, 10_800, "off")]


def audio_format(rate, channels, kind):
    from PySide6.QtMultimedia import QAudioFormat
    fmt = QAudioFormat()
    fmt.setSampleRate(rate)
    fmt.setChannelCount(channels)
    fmt.setSampleFormat(getattr(QAudioFormat.SampleFormat, kind))
    return fmt


@pytest.mark.parametrize("kind,dtype,values", [
    ("UInt8", "u1", [0, 128, 255]),
    ("Int16", "<i2", [-32768, 0, 32512]),
    ("Int32", "<i4", [-2147483648, 0, 2130706432]),
    ("Float", "<f4", [-1, 0, 0.9921875]),
])
def test_native_sample_formats_and_partial_frames(kind, dtype, values):
    import numpy as np
    from encore_client.sensing.live_mic import AudioConverter
    converter = AudioConverter(audio_format(16_000, 2, kind))
    raw = np.repeat(np.array(values, dtype=dtype), 2).tobytes()
    # Real device reads need not end on sample or channel boundaries.
    output = b"".join(converter.convert(raw[i:i + 3]) for i in range(0, len(raw), 3))
    assert np.frombuffer(output, dtype="<i2").tolist() == [-32768, 0, 32512]


@pytest.mark.parametrize("rate", [44_100, 48_000])
def test_resampling_preserves_tone_time_and_rejects_aliases(rate):
    import numpy as np
    from encore_client.sensing.live_mic import AudioConverter
    fmt = audio_format(rate, 2, "Float")
    t = np.arange(rate * 2) / rate
    low = np.sin(2 * np.pi * 440 * t) * 0.5
    high = np.sin(2 * np.pi * 12_000 * t) * 0.5
    outputs = []
    for signal in (low, high):
        converter = AudioConverter(fmt)
        # One channel carries the tone, the other is silent: also tests downmix.
        raw = np.column_stack((signal, np.zeros_like(signal))).astype("<f4").tobytes()
        converted = b"".join(converter.convert(raw[i:i + 4093]) for i in range(0, len(raw), 4093))
        output = np.frombuffer(converted, dtype="<i2").astype(float) / 32768
        assert abs(len(output) + converter.delay_ms * 16 - 32_000) <= 8
        outputs.append(output[1000:-1000])
    assert 0.17 < np.sqrt(np.mean(outputs[0] ** 2)) < 0.18
    assert np.sqrt(np.mean(outputs[1] ** 2)) < 0.002


def test_resampler_delay_does_not_shift_capture_timestamps():
    session, clock = FakeSession(), Clock(10_600)
    mic = LiveMicrophone(session, clock=clock)
    mic.feed(bytes(16_000), end_ms=10_500)
    assert session.chunks == [(16_000, 10_000, "off")]


def test_native_format_is_used_when_16khz_is_unsupported():
    from PySide6.QtWidgets import QApplication
    from encore_client.sensing.live_mic import QtAudioInput
    app = QApplication.instance() or QApplication([])
    fmt = audio_format(48_000, 2, "Float")
    class NativeOnly:
        def isNull(self): return False
        def isFormatSupported(self, candidate): return candidate == fmt
        def preferredFormat(self): return fmt
        def description(self): return "Native-only test microphone"
    # Use a fake QAudioSource so no physical microphone is opened.
    from unittest.mock import patch
    with patch("PySide6.QtMultimedia.QAudioSource") as source:
        QtAudioInput(LiveMicrophone(FakeSession()), NativeOnly())
        assert source.call_args.args[1] == fmt
        source.return_value.setBufferSize.assert_called_once_with(192_000)


class _Offers16kAndNative:
    """A microphone that accepts 16 kHz mono and prefers 48 kHz float."""
    def __init__(self, native): self.native = native
    def isNull(self): return False
    def isFormatSupported(self, candidate): return True
    def preferredFormat(self): return self.native
    def description(self): return "16 kHz-capable test microphone"


@pytest.mark.parametrize("platform, native_expected", [("darwin", True), ("linux", False)])
def test_macos_captures_the_native_format_even_when_16khz_is_offered(monkeypatch, platform, native_expected):
    # macOS: Qt's own 16 kHz conversion delivered 500 ms of audio per ~683 ms (PR #40).
    from unittest.mock import patch
    from PySide6.QtWidgets import QApplication
    from encore_client.sensing import live_mic
    QApplication.instance() or QApplication([])
    native = audio_format(48_000, 1, "Float")
    monkeypatch.setattr(live_mic, "native_microphone_format", lambda: platform == "darwin")
    with patch("PySide6.QtMultimedia.QAudioSource") as source:
        live_mic.QtAudioInput(LiveMicrophone(FakeSession()), _Offers16kAndNative(native))
    requested = source.call_args.args[1]
    if native_expected:
        assert requested == native
    else:
        assert (requested.sampleRate(), requested.channelCount()) == (16_000, 1)
