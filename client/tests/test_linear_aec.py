"""EM-10 (R1): the linear echo canceller removes playback echo and keeps the audience's sound.

Each test pins a property measured while building it (docs/em-10-aec-evaluation.md),
including defects found on the way: tonal playback blowing up empty bins, 4 ms gaps when
chunks were not block multiples, and a misadjusted filter injecting playback when there
is no echo. (The onset-following power normaliser is now backed up by the background
reset, so no test can single it out.)
NumPy only, no GStreamer or TensorFlow.
"""

import numpy as np
import pytest

from encore_client.sensing.linear_aec import LinearEchoCanceller

RATE = 16_000
CHUNK = RATE // 2          # the client's 0.5 s audio chunks
DELAY = 256                # one block: the canceller's fixed output latency


def room(far: np.ndarray, delay_ms: int = 60) -> np.ndarray:
    d = delay_ms * RATE // 1000
    ir = np.zeros(d + 1500)
    for k, g in ((d, 0.6), (d + 90, 0.25), (d + 310, 0.12), (d + 700, 0.06), (d + 1400, 0.03)):
        ir[k] = g
    return np.convolve(far, ir)[: len(far)].astype(np.float32)


def run(mic: np.ndarray, far: np.ndarray, chunk: int = CHUNK, **kw) -> tuple[np.ndarray, LinearEchoCanceller]:
    aec = LinearEchoCanceller(**kw)
    out = np.concatenate([aec.process(mic[i:i + chunk], far[i:i + chunk]) for i in range(0, len(mic), chunk)])
    return out, aec


def erle_db(echo: np.ndarray, out: np.ndarray, start_s: float, end_s: float) -> float:
    a, b = int(start_s * RATE), int(end_s * RATE)
    residual = out[a + DELAY:b + DELAY]
    return 10 * np.log10(np.mean(np.square(echo[a:b], dtype=np.float64))
                         / max(np.mean(np.square(residual, dtype=np.float64)), 1e-30))


def noise(seconds: float, seed: int, level: float = 0.2) -> np.ndarray:
    return (level * np.random.default_rng(seed).standard_normal(int(seconds * RATE))).astype(np.float32)


def test_white_noise_echo_is_removed_almost_completely():
    far = noise(10, 1)
    echo = room(far)
    out, _ = run(echo, far)
    assert len(out) == len(far)
    assert erle_db(echo, out, 7, 9.5) > 40       # measured 49.7 dB


def test_with_no_echo_the_microphone_passes_through_exactly():
    # Two-filter structure: the background learns from the laugh, but never beats
    # "subtract nothing", so the foreground stays zero and nothing is injected.
    far, near = noise(10, 2), noise(10, 3)
    out, aec = run(near, far)
    assert aec.copies == 0
    np.testing.assert_array_equal(out[DELAY:], near[:-DELAY])
    assert not out[:DELAY].any()


def test_output_is_the_same_for_any_chunk_size():
    # 8000 = 31 x 256 + 64: zero-padding short outputs once put a 4 ms gap in every chunk.
    far = noise(6, 4)
    mic = room(far) + noise(6, 5, 0.05)
    reference, _ = run(mic, far, chunk=8000)
    for chunk in (7999, 300, 256, 100_000):
        out, _ = run(mic, far, chunk=chunk)
        np.testing.assert_allclose(out, reference, atol=1e-6)


@pytest.mark.parametrize("name, make", [
    ("amplitude-modulated 220 Hz tone",
     lambda t: 0.2 * np.sin(2 * np.pi * 220 * t) * (1 + np.sin(2 * np.pi * 3 * t))),
    ("C major chord", lambda t: 0.1 * sum(np.sin(2 * np.pi * f * t) for f in (261.6, 329.6, 392.0))),
])
def test_tonal_playback_converges_instead_of_blowing_up(name, make):
    # A held note leaves most bins empty; without the bin floor the AM tone diverged.
    t = np.arange(int(12 * RATE)) / RATE
    far = make(t).astype(np.float32)
    echo = room(far)
    out, _ = run(echo, far)
    assert np.isfinite(out).all() and np.abs(out).max() < 1
    assert erle_db(echo, out, 9, 11.5) > 15, name


def test_laughter_during_playback_survives_while_the_echo_goes():
    far = noise(12, 6)
    echo = room(far)
    t = np.arange(len(far)) / RATE
    laugh = np.zeros_like(far)
    bursts = (t >= 8) & (t < 10)
    laugh[bursts] = 0.3 * (np.sin(2 * np.pi * 5 * t[bursts]) > 0) * noise(12, 7, 1.0)[bursts]
    out, _ = run(echo + laugh, far)
    kept = out[DELAY:][bursts[:-DELAY]]
    assert np.corrcoef(kept, laugh[:-DELAY][bursts[:-DELAY]])[0, 1] > 0.95
    quiet = (t >= 6) & (t < 7.9)
    assert erle_db(echo * quiet, out * np.concatenate([np.zeros(DELAY), quiet[:-DELAY]]), 6, 7.9) > 30


def test_double_talk_detector_is_off_unless_asked():
    far = noise(4, 8)
    out, aec = run(room(far) + noise(4, 9, 0.3), far)
    assert aec.frozen == 0
    _, guarded = run(room(far) + noise(4, 9, 0.3), far, double_talk=2.0)
    assert guarded.double_talk == 2.0
