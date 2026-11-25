"""EM-10 (R1): echo cancellation for the player when the GStreamer WebRTC plugins are missing.

Ubuntu 24.04 and Homebrew GStreamer ship neither ``webrtcdsp`` nor ``webrtcechoprobe``.
Without them the player taps its own decoded audio (a ``tee`` before the audio sink,
see pipeline.py), keeps it here by pipeline running time, and cancels the echo with the
linear canceller (sensing/linear_aec.py). docs/em-10-aec-evaluation.md explains why a
linear canceller and not WebRTC AEC3: AEC3's suppressor removed the laughter too.

No GStreamer import: the pipeline pushes PCM and running times; these classes only do
the bookkeeping and the DSP, so they are tested without a player.
"""

from __future__ import annotations

import threading

import numpy as np

from ..sensing.linear_aec import LinearEchoCanceller

RATE = 48_000                 # the player's audio branch and the microphone blocks
NS_PER_SAMPLE = 1_000_000_000 / RATE
WARMUP_MS = 500               # same rule as the WebRTC filter (player/echo.py)
KEEP_MS = 2_000               # reference history; the mic may lag playback by up to 500 ms
BLOCK = 768                   # 16 ms at 48 kHz: the canceller's fixed output delay
TAPS = 12_288                 # 256 ms echo tail at 48 kHz


class ReferenceBuffer:
    """Mono S16 playback samples indexed by pipeline running time (thread-safe).

    ``push`` comes from the GStreamer streaming thread, ``window`` from the capture
    thread. A gap in the playback is filled with silence; going back in time (a seek,
    a flush) starts over, because the old samples no longer match what is heard.
    """

    def __init__(self, rate: int = RATE, keep_ms: int = KEEP_MS) -> None:
        self.rate = rate
        self.keep = rate * keep_ms // 1000
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self._samples = np.zeros(0, np.int16)
            self._start = None        # sample index (running time * rate) of _samples[0]

    def _index(self, running_ns: int) -> int:
        return round(running_ns * self.rate / 1_000_000_000)

    @property
    def end_ns(self) -> int | None:
        with self._lock:
            if self._start is None:
                return None
            return round((self._start + len(self._samples)) * 1_000_000_000 / self.rate)

    def push(self, pcm: bytes, start_ns: int) -> None:
        data = np.frombuffer(pcm, "<i2")
        first = self._index(start_ns)
        with self._lock:
            if self._start is not None:
                end = self._start + len(self._samples)
                if first < end - self.rate // 100:          # more than 10 ms back: seek or flush
                    self._samples, self._start = np.zeros(0, np.int16), None
                elif first > end:                           # playback gap: silence
                    self._samples = np.concatenate([self._samples, np.zeros(first - end, np.int16)])
                elif first < end:                           # small overlap from rounding
                    data = data[end - first:]
            if self._start is None:
                self._start = first
            self._samples = np.concatenate([self._samples, data])
            extra = len(self._samples) - self.keep
            if extra > 0:
                self._samples = self._samples[extra:]
                self._start += extra

    def window(self, start_ns: int, count: int) -> np.ndarray | None:
        """The ``count`` samples played from ``start_ns``, or None if any are missing."""
        first = self._index(start_ns)
        with self._lock:
            if self._start is None or first < self._start or first + count > self._start + len(self._samples):
                return None
            offset = first - self._start
            return self._samples[offset:offset + count].copy()


class LinearEchoPath:
    """The player's ``cancel_echo`` backend: 48 kHz 10 ms blocks in, same length out.

    Status follows the WebRTC filter: ``warming_up`` for the first 500 ms, then
    ``active``. The output is 16 ms (one block) behind the input.
    """

    def __init__(self) -> None:
        self.canceller = LinearEchoCanceller(block=BLOCK, taps=TAPS)
        self.processed_ms = 0

    def process(self, pcm: bytes, reference: np.ndarray) -> tuple[bytes, str]:
        mic = np.frombuffer(pcm, "<i2").astype(np.float32) / 32768
        if len(reference) != len(mic):
            raise ValueError("reference and microphone blocks differ in length")
        out = self.canceller.process(mic, reference.astype(np.float32) / 32768)
        status = "active" if self.processed_ms >= WARMUP_MS else "warming_up"
        self.processed_ms += len(mic) * 1000 // RATE
        return (np.clip(out, -1, 32767 / 32768) * 32768).astype("<i2").tobytes(), status

    def close(self) -> None:
        """Nothing to release; matches EchoFilter.close for pipeline.reset_echo."""
