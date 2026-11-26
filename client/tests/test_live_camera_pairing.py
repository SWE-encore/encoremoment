"""Live microphone reads and camera frames: each chunk leaves soon and carries a frame of its own span.

Live run on Ubuntu 24.04 (local server, microphone and camera on, 118 s): Qt
delivered the microphone in ~500 ms reads and the first 150 ms were discarded
(``SETTLE_MS``), so every 500 ms chunk ended ~350 ms before the read that
completed it. All 235 chunks left 349-360 ms after their end, and none carried
a camera frame: only the newest frame was offered, and it was ~350 ms newer
than the chunk. These tests replay that timing with a fake clock. Nothing
here opens a microphone or a camera.
"""
import struct
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from encore_client.clips.capture import Segment
from encore_client.clips.recorder import DesktopRecorder
from encore_client.sensing.camera import CameraControl
from encore_client.sensing.live_mic import LiveMicrophone
from encore_client.session.contract import decode_audio_frame
from encore_client.session.session import Conn, Session

RGB_BYTES = 320 * 180 * 3
CHUNK_MS = 500


class Camera:
    """A 10 fps camera: each poll returns the newest frame, captured 40 ms before."""

    def __init__(self, clock):
        self.clock = clock

    def new_fragment(self):
        pass

    def poll(self) -> tuple[list[Segment], tuple[int, bytes]]:
        image_ms = self.clock() - 40
        return [], (image_ms, image_ms.to_bytes(8, "big") * (RGB_BYTES // 8))

    def close(self):
        pass


class Link:
    def __init__(self, clock):
        self.clock = clock
        self.packets = []  # (send ms, bytes)

    def send(self, data):
        if isinstance(data, bytes):
            self.packets.append((self.clock(), data))
        return True


def unpack(packet):
    """(image ms or None, audio header) of one PC->server binary packet."""
    if packet.startswith(b"EMV1"):
        _generation, image_ms = struct.unpack("!IQ", packet[4:16])
        return image_ms, decode_audio_frame(packet[16 + RGB_BYTES:])[0]
    return None, decode_audio_frame(packet)[0]


def test_half_second_reads_still_pair_every_chunk_with_a_frame_from_its_span(tmp_path):
    now = [100_000]
    clock = lambda: now[0]  # noqa: E731
    session = Session("ws://unused", "ses-live-pairing", clock=clock)
    link = Link(clock)
    session._transport = link
    session._on_open()
    session.set_sensor("microphone", True)
    recorder = DesktopRecorder(tmp_path / "index.json", session,
                               camera_factory=lambda _directory: Camera(clock), clock=clock)
    camera = CameraControl(recorder, session, lambda: True, clock=clock)
    session.camera_frame = recorder.latest_face  # as Workspace._new_recorder wires it
    assert camera.enable()
    recorder.poll([])  # first frame: the camera is on
    mic = LiveMicrophone(session, clock=clock)
    mic.device_started()  # the first SETTLE_MS (150 ms) are discarded

    for step in range(1, 801):  # 8 s in 10 ms steps
        now[0] += 10
        if step % 10 == 0:
            recorder.poll([])  # the workspace timer, every 100 ms
        if step % 50 == 0:
            mic.feed(bytes(16_000))  # Qt delivers 500 ms of audio at once

    packets = [(sent, *unpack(data)) for sent, data in link.packets]
    assert len(packets) >= 14
    # The read period and the 150 ms skip leave every chunk 350 ms behind.
    assert {sent - (h["capture_ms"] + CHUNK_MS) for sent, _image, h in packets} == {350}
    images = [image for _sent, image, _h in packets]
    assert None not in images, f"{images.count(None)} of {len(images)} chunks sent without a frame"
    for image_ms, (_sent, _image, header) in zip(images, packets):
        assert header["capture_ms"] <= image_ms < header["capture_ms"] + CHUNK_MS
    assert images == sorted(set(images))  # one frame per chunk, never sent twice
    recorder.shutdown()


class FakeSession:
    def __init__(self, clock):
        self.clock = clock
        self.state = Conn.CONNECTED
        self.view = SimpleNamespace(sensors={"microphone": SimpleNamespace(enabled=True, generation=1)})
        self.sent = []  # (send ms, capture ms)

    def send_audio(self, pcm, capture_ms, aec="off"):
        self.sent.append((self.clock(), capture_ms))
        return True


class Microphone:
    def isNull(self): return False
    def isFormatSupported(self, _fmt): return True
    def preferredFormat(self): return None
    def description(self): return "test microphone"


class Reads:
    """What ``QIODevice.readAll()`` returns: the audio of one timer period."""

    def __init__(self):
        self.next = b""
        self.readyRead = SimpleNamespace(connect=lambda _slot: None, disconnect=lambda _slot: None)

    def readAll(self):
        data, self.next = self.next, b""
        return SimpleNamespace(data=lambda: data)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="replays Qt's PulseAudio source")
def test_linux_microphone_chunks_leave_within_a_tenth_of_a_second_of_their_end():
    """Qt 6.8's PulseAudio source asks for fragments of ``setBufferSize`` and emits
    ``readyRead`` on a timer of that period. Replay that: one read per buffer."""
    from PySide6.QtWidgets import QApplication
    from encore_client.sensing.live_mic import QtAudioInput

    QApplication.instance() or QApplication([])
    now = [100_000]
    session = FakeSession(lambda: now[0])
    mic = LiveMicrophone(session, clock=lambda: now[0])
    with patch("PySide6.QtMultimedia.QAudioSource") as source:
        reads = source.return_value.start.return_value = Reads()
        qt = QtAudioInput(mic, Microphone())
        qt.start()  # discards the first SETTLE_MS, as on a real device
    buffer = source.return_value.setBufferSize.call_args.args[0]
    period_ms = qt.format.durationForBytes(buffer) // 1000
    assert (qt.format.sampleRate(), qt.format.channelCount()) == (16_000, 1)
    while now[0] < 108_000:
        now[0] += period_ms
        reads.next = bytes(buffer)
        qt._read()
    lags = [sent - (capture + CHUNK_MS) for sent, capture in session.sent]
    assert len(lags) >= 14
    assert max(lags) <= 100, f"chunks left {min(lags)}-{max(lags)} ms after their end ({period_ms} ms reads)"
