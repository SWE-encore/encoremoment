"""Stand-in microphone: a WAV file played into a Session in real time.

Until real capture exists (R1, EM-10) this lets the server side be built and
shown with known audio. To the session it behaves like a microphone:

- audio flows only while the session's microphone is on, and pauses while it
  is off;
- each chunk carries the capture time of its first sample on the PC
  monotonic clock, and is handed over only once that much time has passed;
- echo cancellation is reported as ``off``, because nothing is cancelled.

Input must already be mono 16 kHz 16-bit PCM, the contract's audio format.
Convert other files with ``ffmpeg -i IN -ac 1 -ar 16000 -sample_fmt s16 OUT.wav``.
"""

from __future__ import annotations

import wave
from pathlib import Path
from typing import Callable

from ..clock import monotonic_ms
from ..session.contract import AUDIO_RATE

CHUNK_MS = 500


class WavMicrophone:
    def __init__(
        self,
        session,
        path: str | Path,
        *,
        chunk_ms: int = CHUNK_MS,
        clock: Callable[[], int] = monotonic_ms,
    ) -> None:
        with wave.open(str(path), "rb") as wav:
            fmt = (wav.getframerate(), wav.getnchannels(), wav.getsampwidth(), wav.getcomptype())
            if fmt != (AUDIO_RATE, 1, 2, "NONE"):
                raise ValueError(
                    f"{path}: need mono {AUDIO_RATE} Hz 16-bit PCM, got {fmt[1]} channel(s), "
                    f"{fmt[0]} Hz, {8 * fmt[2]}-bit. Convert with: "
                    f"ffmpeg -i IN -ac 1 -ar {AUDIO_RATE} -sample_fmt s16 OUT.wav"
                )
            self.pcm = wav.readframes(wav.getnframes())
        if not self.pcm:
            raise ValueError(f"{path}: no audio")
        if not 0 < chunk_ms <= 1000:
            raise ValueError("chunk_ms must be 1..1000")
        self.session = session
        self.chunk_bytes = AUDIO_RATE * chunk_ms // 1000 * 2
        self.pos = 0  # bytes of pcm already captured
        self.sent = 0
        self._clock = clock
        self._run: tuple[int, int, int] | None = None  # (generation, start ms, start pos)

    @property
    def done(self) -> bool:
        return self.pos >= len(self.pcm)

    def pump(self) -> int:
        """Hand every chunk whose last sample is due to the session. Returns how many were sent."""
        mic = self.session.view.sensors["microphone"]
        if not mic.enabled or self.done:
            self._run = None  # microphone off: nothing is captured
            return 0
        now = self._clock()
        if self._run is None or self._run[0] != mic.generation:
            self._run = (mic.generation, now, self.pos)
        _, start_ms, start_pos = self._run
        sent = 0
        while not self.done:
            data = self.pcm[self.pos : self.pos + self.chunk_bytes]
            capture_ms = start_ms + _ms(self.pos - start_pos)
            if now < capture_ms + _ms(len(data)):
                break
            # Captured either way: audio from a dropped connection is lost, as with a real microphone.
            if self.session.send_audio(data, capture_ms, aec="off"):
                sent += 1
            self.pos += len(data)
        self.sent += sent
        return sent


def _ms(n_bytes: int) -> int:
    return n_bytes // 2 * 1000 // AUDIO_RATE
