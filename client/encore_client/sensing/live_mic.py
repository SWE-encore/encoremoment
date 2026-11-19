"""Raw PC microphone for testing: Qt Multimedia capture, 16 kHz mono PCM16, no AEC.

This is a test tool, not the product's capture path (R1, EM-10). Nothing is
echo-cancelled, so with speakers on the content's own sound reaches the model;
use headphones while testing. Every chunk says ``aec: off``.

``LiveMicrophone`` turns whatever the device delivers into the session's
0.5 s chunks with capture timestamps; it has no Qt dependency, so it is
tested without a device. ``QtAudioInput`` connects it to a real input.
"""

from __future__ import annotations

from typing import Callable

from ..clock import monotonic_ms
from ..session.contract import AUDIO_RATE

CHUNK_MS = 500
RESYNC_MS = 250  # timestamps this far from the clock: samples were lost, start a new run
MAX_RECORDING_S = 600


class LiveMicrophone:
    def __init__(self, session, *, chunk_ms: int = CHUNK_MS,
                 clock: Callable[[], int] = monotonic_ms, keep_s: int = MAX_RECORDING_S) -> None:
        self.session = session
        self.chunk_bytes = AUDIO_RATE * chunk_ms // 1000 * 2
        self.sent = 0
        self.recording = bytearray()  # what was captured while the microphone was on, for saving
        self.last_pcm = b""  # the latest device read, for a level meter
        self._keep = keep_s * AUDIO_RATE * 2
        self._clock = clock
        self._buf = bytearray()
        self._run: list[int] | None = None  # [generation, first sample ms, bytes taken from the run]

    def feed(self, data: bytes) -> int:
        """Audio just read from the device, ending now. Returns how many chunks went out."""
        self.last_pcm = data
        mic = self.session.view.sensors["microphone"]
        if not mic.enabled:
            self._buf.clear()  # microphone off: nothing is captured (R8)
            self._run = None
            return 0
        now = self._clock()
        if self._run is not None:
            expected_end = self._run[1] + _ms(self._run[2] + len(self._buf) + len(data))
            if self._run[0] != mic.generation or abs(expected_end - now) > RESYNC_MS:
                self._buf.clear()
                self._run = None
        if self._run is None:
            self._run = [mic.generation, now - _ms(len(data)), 0]
        self._buf += data
        sent = 0
        while len(self._buf) >= self.chunk_bytes:
            chunk = bytes(self._buf[: self.chunk_bytes])
            del self._buf[: self.chunk_bytes]
            capture_ms = self._run[1] + _ms(self._run[2])
            self._run[2] += len(chunk)
            if len(self.recording) < self._keep:
                self.recording += chunk
            if self.session.send_audio(chunk, capture_ms, aec="off"):
                sent += 1
        self.sent += sent
        return sent


class QtAudioInput:
    """Reads a Qt audio input in the contract's format and feeds a LiveMicrophone."""

    def __init__(self, mic: LiveMicrophone, device=None) -> None:
        from PySide6.QtMultimedia import QAudioFormat, QAudioSource, QMediaDevices

        device = device if device is not None else QMediaDevices.defaultAudioInput()
        if device.isNull():
            raise RuntimeError("no microphone found")
        fmt = QAudioFormat()
        fmt.setSampleRate(AUDIO_RATE)
        fmt.setChannelCount(1)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        if not device.isFormatSupported(fmt):
            raise RuntimeError(f"{device.description()} cannot record 16 kHz mono 16-bit audio")
        self.mic = mic
        self.device = device
        self.source = QAudioSource(device, fmt)
        self.source.setBufferSize(AUDIO_RATE * 2 // 2)  # 0.5 s
        self._io = None

    def start(self) -> None:
        self._io = self.source.start()
        if self._io is None:
            raise RuntimeError(f"could not open {self.device.description()}")
        self._io.readyRead.connect(self._read)

    def stop(self) -> None:
        self.source.stop()
        self._io = None

    def _read(self) -> None:
        if self._io is not None:
            data = self._io.readAll().data()
            if data:
                self.mic.feed(bytes(data))


def _ms(n_bytes: int) -> int:
    return n_bytes // 2 * 1000 // AUDIO_RATE
