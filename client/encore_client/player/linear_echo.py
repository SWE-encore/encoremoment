"""EM-10 (R1): echo cancellation for the player when the GStreamer WebRTC plugins are missing.

Ubuntu 24.04 and Homebrew GStreamer ship neither ``webrtcdsp`` nor ``webrtcechoprobe``.
Without them the player taps its own decoded audio (a buffer probe just before the
audio sink, see pipeline.py), keeps it here by pipeline running time, and cancels the echo with the
linear canceller (sensing/linear_aec.py). docs/em-10-aec-evaluation.md explains why a
linear canceller and not WebRTC AEC3: AEC3's suppressor removed the laughter too.

No GStreamer import: the pipeline pushes PCM and running times; these classes only do
the bookkeeping and the DSP, so they are tested without a player.
"""

from __future__ import annotations

import threading

import numpy as np

from ..sensing.linear_aec import LinearEchoCanceller, estimate_delay

RATE = 48_000                 # the player's audio branch and the microphone blocks
NS_PER_SAMPLE = 1_000_000_000 / RATE
WARMUP_MS = 500               # same rule as the WebRTC filter (player/echo.py)
KEEP_MS = 3_000               # reference history: mic lag (<= 500 ms) + bulk delay + delay search
BLOCK = 768                   # 16 ms at 48 kHz: the canceller's fixed output delay
OUTPUT_DELAY_MS = BLOCK * 1000 // RATE   # the cleaned audio is this much behind the raw audio
TAPS = 12_288                 # 256 ms echo tail at 48 kHz
DRIFT_MS = 20                 # re-anchor when a block's timestamp is this far from the cursor
LEAD_MS = 5                   # read the reference this much earlier than the timestamp says
# Bulk delay. The tap sits before the audio sink's buffer (160-200 ms on PulseAudio), so the
# echo reaches the microphone that much later than the reference, plus the room and the input
# latency. Past the 256 ms tail nothing is cancelled. Every CHECK_MS the last WINDOW_MS of
# microphone audio is correlated with the reference up to SEARCH_MS earlier; two confident
# estimates in a row outside [ALIGN_LOW_MS, ALIGN_HIGH_MS] move the reference so the direct echo
# lands at TARGET_MS, leaving the rest of the tail for the room's reverberation.
CHECK_MS = 1_000
WINDOW_MS = 1_000
SEARCH_MS = 750
TARGET_MS = 30
ALIGN_LOW_MS = 2
ALIGN_HIGH_MS = 150
MAX_BULK_MS = 700             # 0.5 s mic lag + 0.7 s bulk + 1.75 s search fits in KEEP_MS
CONFIDENT = 9.0               # estimate_delay: ~5 for unrelated signals, 12-20 for an echo
AGREE_MS = 2                  # two estimates this close count as the same delay


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

    def index(self, running_ns: int) -> int:
        """The sample index played at ``running_ns`` (running time * rate)."""
        return round(running_ns * self.rate / 1_000_000_000)

    @property
    def end_ns(self) -> int | None:
        with self._lock:
            if self._start is None:
                return None
            return round((self._start + len(self._samples)) * 1_000_000_000 / self.rate)

    def push(self, pcm: bytes, start_ns: int, channels: int = 1) -> int:
        """Add interleaved S16 audio played from ``start_ns``, mixed down to mono.

        Returns the number of mono samples in ``pcm``."""
        data = np.frombuffer(pcm, "<i2")
        if channels > 1:
            frames = len(data) // channels
            data = data[:frames * channels].reshape(frames, channels).mean(axis=1).astype(np.int16)
        count = len(data)
        first = self.index(start_ns)
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
        return count

    def window(self, start_ns: int, count: int) -> np.ndarray | None:
        """The ``count`` samples played from ``start_ns``, or None if any are missing."""
        return self.samples(self.index(start_ns), count)

    def samples(self, first: int, count: int) -> np.ndarray | None:
        """The ``count`` samples from sample index ``first``, or None if any are missing."""
        with self._lock:
            if self._start is None or first < self._start or first + count > self._start + len(self._samples):
                return None
            offset = first - self._start
            return self._samples[offset:offset + count].copy()


class LinearEchoPath:
    """The player's ``cancel_echo`` backend: 48 kHz 10 ms blocks in, same length out.

    A linear filter needs the reference to be the same continuous signal the speakers
    played, sample for sample. The microphone's timestamps are not that exact: they are
    whole milliseconds and the capture callback jitters, and a 1 ms error is 48 samples,
    enough to undo what the filter has learnt. So only the first block is placed by its
    timestamp; after that the reference is read from a cursor that advances by exactly
    the samples consumed. The timestamp is still checked: if it is more than
    ``DRIFT_MS`` away from the cursor (a dropped block, clock drift), the cursor moves
    to it and the filter starts again.

    The cursor is placed ``LEAD_MS`` before the timestamp. To the filter that is 5 ms
    more echo delay, well inside its 256 ms; in return a timestamp that is a little
    late can neither ask for audio the player has not played yet nor make the echo
    arrive before its reference, which no causal filter can cancel.

    The echo's bulk delay is measured as it plays (see CHECK_MS) and the reference is
    read ``bulk_ms`` earlier when the direct echo would otherwise fall outside the filter.
    If the echo is later than ``MAX_BULK_MS`` allows, the block is passed through as
    ``uncancelled``: the status must not say ``active`` for a filter that cannot reach it.

    Status follows the WebRTC filter: ``warming_up`` for the first 500 ms, then
    ``active``; ``reference_unavailable`` when the played audio for the block is not
    in the buffer. The output is ``OUTPUT_DELAY_MS`` (one block) behind the input.
    """

    def __init__(self, reference: ReferenceBuffer, bulk_ms: int = 0) -> None:
        self.reference = reference
        self.reanchors = 0
        self.realigns = 0
        self.bulk = bulk_ms * RATE // 1000      # samples the reference is read earlier
        self.out_of_range = False               # the echo is later than MAX_BULK_MS
        self.last_estimate: tuple[int, float] | None = None   # (residual lag ms, confidence)
        self._start()

    @property
    def bulk_ms(self) -> int:
        return self.bulk * 1000 // RATE

    def _start(self) -> None:
        self.canceller = LinearEchoCanceller(block=BLOCK, taps=TAPS)
        self.processed_ms = 0
        self._cursor: int | None = None
        self._history = np.zeros(0, np.float32)   # newest microphone audio, ends at the cursor
        self._since_check = 0
        self._previous_lag: int | None = None

    def process(self, pcm: bytes, start_ns: int) -> tuple[bytes, str]:
        count = len(pcm) // 2
        anchor = self.reference.index(start_ns) - LEAD_MS * RATE // 1000 - self.bulk
        if self._cursor is not None and abs(anchor - self._cursor) > DRIFT_MS * RATE // 1000:
            self.reanchors += 1
            self._start()
        first = anchor if self._cursor is None else self._cursor
        reference = self.reference.samples(first, count)
        if reference is None:
            self._start()
            return pcm, "reference_unavailable"
        self._cursor = first + count
        mic = np.frombuffer(pcm, "<i2").astype(np.float32) / 32768
        self._remember(mic)
        if self._cursor is None:        # realigned just now: the filter starts with the next block
            return pcm, "warming_up"
        if self.out_of_range:
            return pcm, "uncancelled"
        out = self.canceller.process(mic, reference.astype(np.float32) / 32768)
        status = "active" if self.processed_ms >= WARMUP_MS else "warming_up"
        self.processed_ms += count * 1000 // RATE
        return (np.clip(out, -1, 32767 / 32768) * 32768).astype("<i2").tobytes(), status

    def _remember(self, mic: np.ndarray) -> None:
        window = WINDOW_MS * RATE // 1000
        self._history = np.concatenate([self._history, mic])[-window:]
        self._since_check += len(mic)
        if self._since_check < CHECK_MS * RATE // 1000 or len(self._history) < window:
            return
        self._since_check = 0
        search = SEARCH_MS * RATE // 1000
        far = self.reference.samples(self._cursor - window - search, window + search)
        found = estimate_delay(self._history, far, decimate=3) if far is not None else None
        if found is None or found[1] < CONFIDENT:
            # No echo to measure (headphones, silence, or too weak to matter): never block it.
            self.last_estimate = None if found is None else (found[0] * 1000 // RATE, found[1])
            self._previous_lag = None
            self._leave_out_of_range()
            return
        lag, confidence = found
        self.last_estimate = (lag * 1000 // RATE, confidence)
        agree = (self._previous_lag is not None and
                 abs(lag - self._previous_lag) <= AGREE_MS * RATE // 1000)
        self._previous_lag = lag
        if ALIGN_LOW_MS * RATE // 1000 <= lag <= ALIGN_HIGH_MS * RATE // 1000:
            self._leave_out_of_range()
            return
        if not agree:
            return
        bulk = max(0, self.bulk + lag - TARGET_MS * RATE // 1000)
        if bulk > MAX_BULK_MS * RATE // 1000:
            self.out_of_range = True        # keep reading where we are and keep measuring
            return
        self.out_of_range = False
        self.bulk = bulk
        self.realigns += 1
        self._start()               # the next block is placed by its timestamp with the new bulk

    def _leave_out_of_range(self) -> None:
        if self.out_of_range:
            self.out_of_range = False
            self._start()           # the filter did not run meanwhile: start it cleanly

    def close(self) -> None:
        """Nothing to release; matches EchoFilter.close for pipeline.reset_echo."""
