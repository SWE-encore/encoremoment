"""Linear acoustic echo canceller for EM-10 (R1): partitioned-block frequency-domain NLMS.

Learns the room's echo path from the playback reference and subtracts the predicted
echo. A background filter learns all the time; the output uses a foreground copy that
is only updated when the background clearly removes more (two-filter structure, as in
Speex MDF), so with no echo in the room the microphone passes through untouched.

Unlike WebRTC AEC3 there is no residual-echo suppressor. In our use the content keeps
playing while the audience laughs (permanent double talk), and AEC3's suppressor turned
the laughter down with it: on 40 ESC-50 laughter clips with spoken playback, YAMNet kept
0-3% of the laughter windows after AEC3, 3-13% with no AEC, and 51-59% after this
canceller (docs/em-10-aec-evaluation.md).

An optional Geigel-style double-talk detector (``double_talk=2.0``) freezes adaptation
while the microphone is much louder than the predicted echo. It is off by default: on
the synthetic cases it kept less of the reaction (corr 0.55 vs 0.95 at 120 ms).

Measured (16 kHz, 60 ms room, 0.5 s chunks), echo removed after 3 / 9 / 24 s:
speech-like 14 / 22 / 28 dB, AM 220 Hz tone 26 / 29 / 38 dB, white noise 25 / 64 / 103 dB.
Output lags the input by exactly one block (16 ms at 16 kHz), without gaps.
The player uses it when the WebRTC plugins are missing (player/linear_echo.py).
"""

from __future__ import annotations

import numpy as np


class LinearEchoCanceller:
    def __init__(self, block: int = 256, taps: int = 4096, mu: float = 0.5,
                 double_talk: float = float("inf"), leak: float = 0.9,
                 reg: float = 0.1) -> None:
        self.N = block
        self.P = -(-taps // block)                      # partitions covering the echo tail
        self.mu = mu
        self.double_talk = double_talk                  # freeze when |mic| > k * |predicted echo|
        self.leak = leak                                # power estimate smoothing
        self.reg = reg                                  # bin regularisation, share of mean power
        bins = block + 1
        self.X = np.zeros((self.P, bins), np.complex128)   # far-end spectra, newest first
        self.W = np.zeros((self.P, bins), np.complex128)   # background: adapts every block
        self.Wf = np.zeros((self.P, bins), np.complex128)  # foreground: used for the output
        self._eb = 0.0                                     # smoothed error energy, background
        self._ef = 0.0                                     # smoothed error energy, foreground
        self._md = 0.0                                     # smoothed microphone energy
        self.copies = 0
        self.power = np.zeros(bins)
        self.prev = np.zeros(block)
        self.frozen = 0
        self._mic_energy = 0.0
        self._err_energy = 1e-12
        self.blocks = 0
        self._far = np.zeros(0, np.float32)
        self._mic = np.zeros(0, np.float32)
        # Start one block behind: every call can then return exactly as many samples as it got,
        # with a constant 16 ms delay. Padding a short output with zeros instead put a 4 ms gap
        # into every 0.5 s chunk (8000 = 31 x 256 + 64), which cost YAMNet laughter triggers.
        self._pending = np.zeros(block, np.float32)

    def erle_estimate(self) -> float:
        """Echo removed so far, as a power ratio, from blocks the filter learned on."""
        return self._mic_energy / max(self._err_energy, 1e-12)

    def _block(self, far: np.ndarray, mic: np.ndarray) -> np.ndarray:
        N = self.N
        xf = np.fft.rfft(np.concatenate([self.prev, far]))
        self.prev = far.copy()
        self.X = np.roll(self.X, 1, axis=0)
        self.X[0] = xf
        y = np.fft.irfft((self.X * self.W).sum(axis=0))[N:]       # background prediction
        yf = np.fft.irfft((self.X * self.Wf).sum(axis=0))[N:]     # foreground prediction
        e = mic - y
        out = mic - yf
        self.blocks += 1
        # Two-filter structure (as in Speex MDF): the background learns all the time, even when a
        # laugh pulls it off course; the output uses the foreground, which only takes the
        # background's coefficients once they remove clearly more than the foreground does.
        # With no echo in the room the background can never beat "subtract nothing", so the
        # microphone passes through untouched instead of picking up misadjusted playback.
        a = 0.8
        self._eb = a * self._eb + (1 - a) * float(np.dot(e, e))
        self._ef = a * self._ef + (1 - a) * float(np.dot(out, out))
        self._md = a * self._md + (1 - a) * float(np.dot(mic, mic))
        if self._eb < 0.5 * self._ef and self._eb < 0.5 * self._md:
            self.Wf = self.W.copy()
            self._ef = self._eb
            self.copies += 1
        elif self._eb > 4 * self._md and self.blocks > self.P:
            self.W = self.Wf.copy()                                # background diverged: restart from foreground
            self._eb = self._ef
        # Geigel-style: a reaction louder than the predicted echo stops learning for this block.
        talking = np.max(np.abs(mic)) > self.double_talk * max(np.max(np.abs(y)), 1e-4)
        # Trust the detector only once the filter predicts the echo (>= 6 dB echo removed),
        # otherwise a not-yet-learned echo looks like a reaction and learning never starts.
        if talking and self.erle_estimate() >= 4.0:
            self.frozen += 1
            return out
        self._mic_energy = 0.95 * self._mic_energy + 0.05 * float(np.dot(mic, mic))
        self._err_energy = 0.95 * self._err_energy + 0.05 * float(np.dot(e, e))
        # Normalise by the power of everything the filter remembers (all P partitions), not just
        # the newest block: when playback suddenly gets quiet, the older loud blocks are still in
        # the history, and a current-block normaliser lets the step explode (found with an AM tone).
        p = (np.abs(self.X) ** 2).sum(axis=0)
        # Smooth on the way down, but follow a rise at once: a lagging normaliser at a sudden
        # onset makes the step larger than 1 for that block and the bin diverges. This fixed
        # the AM-tone divergence before the two-filter structure existed; the background reset
        # now also catches it, so this is a second guard (a test cannot tell them apart).
        self.power = np.maximum(p, self.leak * self.power + (1 - self.leak) * p)
        ef = np.fft.rfft(np.concatenate([np.zeros(N), e]))
        # Regularise empty bins: with tonal playback (music, a held note) most bins carry no
        # power, and dividing by it alone makes those bins blow up (found with a 220 Hz tone).
        floor = self.reg * self.power.mean() + 1e-8
        grad = self.mu * np.conj(self.X) * ef / (self.power + floor)
        g = np.fft.irfft(grad, axis=1)
        g[:, N:] = 0                                    # gradient constraint: keep it linear, not circular
        self.W += np.fft.rfft(g, axis=1)
        return out

    def process(self, near: np.ndarray, far: np.ndarray) -> np.ndarray:
        """Any length in, same length out, delayed by exactly one block (16 ms at 16 kHz)."""
        near = np.asarray(near, np.float32)
        far = np.asarray(far, np.float32)
        if len(near) != len(far):
            # Unequal lengths would shift the reference against the microphone from here on,
            # and the filter would quietly stop cancelling.
            raise ValueError(f"near and far must have the same length ({len(near)} != {len(far)})")
        self._mic = np.concatenate([self._mic, near])
        self._far = np.concatenate([self._far, far])
        out = []
        while len(self._mic) >= self.N:
            out.append(self._block(self._far[:self.N].astype(np.float64), self._mic[:self.N].astype(np.float64)))
            self._mic, self._far = self._mic[self.N:], self._far[self.N:]
        done = np.concatenate(out).astype(np.float32) if out else np.zeros(0, np.float32)
        self._pending = np.concatenate([self._pending, done])
        assert len(self._pending) >= len(near), "pending must cover the request (one-block lead)"
        result, self._pending = self._pending[:len(near)], self._pending[len(near):]
        return result


def estimate_delay(mic: np.ndarray, far: np.ndarray, decimate: int = 1) -> tuple[int, float] | None:
    """Bulk echo delay: how many samples ``mic`` lags the playback in ``far`` (GCC-PHAT).

    ``far`` covers the same time as ``mic`` plus ``len(far) - len(mic)`` samples before it,
    so lags from 0 to that many samples are searched: ``mic[i] ~ far[len(far) - len(mic) + i - lag]``.
    Returns ``(lag, confidence)``; confidence is the correlation peak over the mean of its
    absolute values (about 6 for unrelated signals, tens for a clear echo). None when either
    signal is silent. ``decimate`` averages that many samples first (48 kHz -> 16 kHz with 3);
    the lag is returned at the input rate, to the nearest ``decimate`` samples.
    """
    mic = np.asarray(mic, np.float64)
    far = np.asarray(far, np.float64)
    search = len(far) - len(mic)
    if search < 0:
        raise ValueError("far must start before mic and cover it")
    if decimate > 1:
        mic = mic[:len(mic) // decimate * decimate].reshape(-1, decimate).mean(axis=1)
        far = far[len(far) % decimate:].reshape(-1, decimate).mean(axis=1)
        search = len(far) - len(mic)
    if not mic.any() or not far.any() or not mic.std() or not far.std():
        return None
    n = 1 << (len(far) + len(mic)).bit_length()
    cross = np.conj(np.fft.rfft(mic - mic.mean(), n)) * np.fft.rfft(far - far.mean(), n)
    cross /= np.abs(cross) + 1e-12 * np.abs(cross).max()
    r = np.fft.irfft(cross, n)[:search + 1]          # r[t] = sum mic[j] far[j + t]
    c = r[::-1]                                         # c[lag] = r[search - lag]
    lag = int(np.argmax(c))
    confidence = float(c[lag] / (np.mean(np.abs(c)) + 1e-12))
    return lag * decimate, confidence
