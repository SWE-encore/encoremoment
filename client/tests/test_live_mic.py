"""Raw microphone adapter: device reads become timestamped 0.5 s chunks (R8, R9)."""

from types import SimpleNamespace

import pytest

from encore_client.sensing.live_mic import LiveMicrophone

READ = bytes(3_200)  # 0.1 s, a typical device read


class FakeSession:
    def __init__(self, enabled=True, generation=1):
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
    feed(mic, clock, 20)  # 2 s of exact zeros
    assert mic.zero_ms == 2_000
    clock.t += 100
    mic.feed(b"\x01\x00" + bytes(3_198))  # any real sound, however quiet
    assert mic.zero_ms == 0


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
