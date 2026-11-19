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
from ..session.session import Conn

CHUNK_MS = 500
RESYNC_MS = 250  # timestamps this far from the clock: samples were lost, start a new run
SETTLE_MS = 150  # a device's first samples can be a full-scale pop (measured: ~25 ms on a laptop microphone)
MAX_RECORDING_S = 600


class LiveMicrophone:
    def __init__(self, session, *, chunk_ms: int = CHUNK_MS,
                 clock: Callable[[], int] = monotonic_ms, keep_s: int = MAX_RECORDING_S) -> None:
        self.session = session
        self.chunk_bytes = AUDIO_RATE * chunk_ms // 1000 * 2
        self.sent = 0
        self.recording = bytearray()  # what was captured while the microphone was on, for saving
        self.last_pcm = b""  # the latest device read, for a level meter
        self.zero_ms = 0  # how long the device has delivered exact zeros: a muted input, not a quiet room
        self._keep = keep_s * AUDIO_RATE * 2
        self._clock = clock
        self._buf = bytearray()
        self._run: list[int] | None = None  # [generation, first sample ms, bytes taken from the run]
        self._skip = 0  # bytes still to discard after the device started

    def device_started(self, settle_ms: int = SETTLE_MS) -> None:
        """The device just opened: discard its first ``settle_ms`` of audio."""
        self._skip = AUDIO_RATE * settle_ms // 1000 * 2

    def feed(self, data: bytes, *, end_ms: int | None = None) -> int:
        """Audio just read from the device, ending now. Returns how many chunks went out."""
        mic = self.session.view.sensors["microphone"]
        if not mic.enabled or self.session.state is not Conn.CONNECTED:
            self._buf.clear()
            self._run = None
            return 0
        if self._skip:
            dropped = min(self._skip, len(data))
            self._skip -= dropped
            data = data[dropped:]  # what is left still ends now, so its timestamps hold
            if not data:
                return 0
        self.last_pcm = data
        self.zero_ms = 0 if any(data) else self.zero_ms + _ms(len(data))
        now = self._clock() if end_ms is None else end_ms
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


class AudioConverter:
    """Convert Qt PCM to mono PCM16, preserving resampler state across reads."""

    def __init__(self, fmt) -> None:
        import numpy as np
        import soxr
        from PySide6.QtMultimedia import QAudioFormat

        formats = {
            QAudioFormat.SampleFormat.UInt8: ("u1", 128.0, 128.0),
            QAudioFormat.SampleFormat.Int16: ("<i2", 0.0, 32768.0),
            QAudioFormat.SampleFormat.Int32: ("<i4", 0.0, 2147483648.0),
            QAudioFormat.SampleFormat.Float: ("<f4", 0.0, 1.0),
        }
        if fmt.sampleFormat() not in formats or fmt.sampleRate() <= 0 or fmt.channelCount() <= 0:
            raise RuntimeError("unsupported microphone PCM format")
        self._np = np
        self._dtype, self._offset, self._scale = formats[fmt.sampleFormat()]
        self._channels = fmt.channelCount()
        self._frame_bytes = np.dtype(self._dtype).itemsize * self._channels
        self._pending = b""
        self._resampler = (soxr.ResampleStream(fmt.sampleRate(), AUDIO_RATE, 1, dtype="float32")
                           if fmt.sampleRate() != AUDIO_RATE else None)

    @property
    def delay_ms(self) -> int:
        return round(self._resampler.delay() * 1000 / AUDIO_RATE) if self._resampler else 0

    def convert(self, data: bytes) -> bytes:
        np = self._np
        data = self._pending + data
        n = len(data) // self._frame_bytes * self._frame_bytes
        self._pending = data[n:]
        if not n:
            return b""
        samples = np.frombuffer(data[:n], dtype=self._dtype).astype(np.float32)
        samples = (samples - self._offset) / self._scale
        mono = samples.reshape(-1, self._channels).mean(axis=1)
        mono = np.nan_to_num(mono, nan=0.0, posinf=1.0, neginf=-1.0)
        if self._resampler:
            mono = self._resampler.resample_chunk(mono)
        return np.clip(np.rint(mono * 32768.0), -32768, 32767).astype("<i2").tobytes()


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
            fmt = device.preferredFormat()
        if not device.isFormatSupported(fmt):
            raise RuntimeError(f"{device.description()} has no supported capture format")
        self.converter = AudioConverter(fmt)
        self.mic = mic
        self.device = device
        self.source = QAudioSource(device, fmt)
        self.source.setBufferSize(fmt.bytesForDuration(500_000))  # 0.5 s in device format
        self._io = None

    def start(self) -> None:
        self._io = self.source.start()
        if self._io is None:
            raise RuntimeError(f"could not open {self.device.description()}")
        self.mic.device_started()
        self._io.readyRead.connect(self._read)

    def stop(self) -> None:
        if self._io is not None:
            self._io.readyRead.disconnect(self._read)
        self._io = None
        self.source.stop()

    def _read(self) -> None:
        if self._io is not None:
            data = self._io.readAll().data()
            if data:
                pcm = self.converter.convert(bytes(data))
                if pcm:
                    self.mic.feed(pcm, end_ms=self.mic._clock() - self.converter.delay_ms)


def _ms(n_bytes: int) -> int:
    return n_bytes // 2 * 1000 // AUDIO_RATE
