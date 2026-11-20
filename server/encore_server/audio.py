"""Audio framing and loudness for analysis (R2, R3).

Chunks from the PC are joined into overlapping analysis windows: 0.975 s
(15 600 samples, one YAMNet frame) every 0.48 s. A gap in the audio, or a new
microphone generation, starts a fresh run of windows, so no window spans
missing audio.
"""

from __future__ import annotations

import math
from array import array
from collections import deque
from dataclasses import dataclass

from .contract import AUDIO_RATE

WINDOW_SAMPLES = 15_600  # 0.975 s
HOP_SAMPLES = 7_680  # 0.48 s
WINDOW_MS = WINDOW_SAMPLES * 1000 // AUDIO_RATE
HOP_MS = HOP_SAMPLES * 1000 // AUDIO_RATE
_JUMP_MS = 50  # a chunk this far off the expected time is a discontinuity


def rms_db(pcm: bytes) -> float:
    """Loudness of PCM16 audio in dB relative to full scale (silence: -120).

    The DC offset is removed first: some microphones sit far from zero (a
    laptop microphone measured at +6 700, -14 dBFS), and an offset is not sound.
    """
    samples = array("h")
    samples.frombytes(pcm)  # PCM is little-endian; so are the platforms we run on
    if not samples:
        return -120.0
    n = len(samples)
    mean = sum(samples) / n
    variance = max(sum(v * v for v in samples) / n - mean * mean, 0.0)
    return 20 * math.log10(max(math.sqrt(variance) / 32768, 1e-6))


class RoomBaseline:
    """Audio strength against the room's quiet level, 0..1 (R3).

    The quiet level is the 10th percentile of the last minute of frames, and
    never below ``floor_db``. 0 means as quiet as the room, 1 means
    ``range_db`` louder or more. Until ``warmup`` frames have been seen the
    floor is used, so a session that starts with laughter is not its own
    baseline.
    """

    def __init__(self, *, floor_db: float = -50.0, range_db: float = 40.0,
                 frames: int = 125, warmup: int = 10) -> None:
        self.floor_db, self.range_db, self.warmup = floor_db, range_db, warmup
        self.history: deque[float] = deque(maxlen=frames)

    def strength(self, db: float) -> float:
        self.history.append(db)
        quiet = self.floor_db
        if len(self.history) >= self.warmup:
            quiet = max(quiet, sorted(self.history)[len(self.history) // 10])
        return round(min(max((db - quiet) / self.range_db, 0.0), 1.0), 3)


@dataclass
class Window:
    start_ms: int
    end_ms: int
    pcm: bytes
    generation: int
    aec: str


class AudioWindows:
    """Collects one microphone generation's chunks into analysis windows."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.generation: int | None = None
        self.start_ms = 0
        self.next_chunk: int | None = None
        self._buf = bytearray()

    def discontinuous(self, header: dict) -> bool:
        expected_ms = self.start_ms + len(self._buf) // 2 * 1000 // AUDIO_RATE
        return (header["generation"] != self.generation or header["chunk"] != self.next_chunk
                or abs(header["capture_ms"] - expected_ms) > _JUMP_MS)

    def add(self, header: dict, pcm: bytes) -> list[Window]:
        if self.discontinuous(header):
            self.generation, self.start_ms, self._buf = header["generation"], header["capture_ms"], bytearray()
        self.next_chunk = header["chunk"] + 1
        self._buf += pcm
        out = []
        while len(self._buf) >= 2 * WINDOW_SAMPLES:
            out.append(Window(self.start_ms, self.start_ms + WINDOW_MS, bytes(self._buf[: 2 * WINDOW_SAMPLES]),
                              self.generation, header["aec"]))
            del self._buf[: 2 * HOP_SAMPLES]
            self.start_ms += HOP_MS
        return out
