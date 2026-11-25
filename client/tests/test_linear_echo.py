"""EM-10 (R1): the player's linear echo path without the WebRTC plugins. NumPy only."""

import threading

import numpy as np
import pytest

from encore_client.player.linear_echo import BLOCK, RATE, WARMUP_MS, LinearEchoPath, ReferenceBuffer

MS = 1_000_000
TEN_MS = RATE // 100


def pcm(samples: np.ndarray) -> bytes:
    return np.asarray(samples, "<i2").tobytes()


def ramp(start: int, count: int) -> np.ndarray:
    return (np.arange(start, start + count) % 30000).astype(np.int16)


# ---- ReferenceBuffer ------------------------------------------------------------------

def test_a_window_returns_exactly_the_samples_played_at_that_time():
    ref = ReferenceBuffer()
    for k in range(10):                                    # 10 ms buffers from 0 ms
        ref.push(pcm(ramp(k * TEN_MS, TEN_MS)), k * 10 * MS)
    np.testing.assert_array_equal(ref.window(25 * MS, TEN_MS), ramp(25 * RATE // 1000, TEN_MS))
    assert ref.end_ns == 100 * MS


def test_a_window_that_is_not_fully_played_yet_or_already_dropped_is_none():
    ref = ReferenceBuffer(keep_ms=50)
    for k in range(10):
        ref.push(pcm(ramp(k * TEN_MS, TEN_MS)), k * 10 * MS)
    assert ref.window(95 * MS, TEN_MS) is None             # ends after what was played
    assert ref.window(20 * MS, TEN_MS) is None             # older than the 50 ms kept
    assert ref.window(50 * MS, TEN_MS) is not None


def test_a_gap_in_playback_reads_as_silence():
    ref = ReferenceBuffer()
    ref.push(pcm(np.full(TEN_MS, 7)), 0)
    ref.push(pcm(np.full(TEN_MS, 9)), 20 * MS)              # 10 ms with nothing played
    window = ref.window(0, 3 * TEN_MS)
    assert (window[:TEN_MS] == 7).all() and not window[TEN_MS:2 * TEN_MS].any() and (window[2 * TEN_MS:] == 9).all()


def test_going_back_in_time_starts_over():
    # A flushing seek resets running time: the old samples no longer match the room.
    ref = ReferenceBuffer()
    ref.push(pcm(np.full(TEN_MS * 50, 5)), 0)
    ref.push(pcm(np.full(TEN_MS, 3)), 100 * MS)
    assert ref.window(0, TEN_MS) is None
    assert (ref.window(100 * MS, TEN_MS) == 3).all()


def test_rounding_overlap_does_not_duplicate_samples():
    ref = ReferenceBuffer()
    ref.push(pcm(ramp(0, TEN_MS)), 0)
    ref.push(pcm(ramp(TEN_MS - 2, TEN_MS + 2)), 10 * MS - 2 * MS // 48)   # starts 2 samples early
    np.testing.assert_array_equal(ref.window(0, 2 * TEN_MS), ramp(0, 2 * TEN_MS))


def test_push_and_window_from_two_threads():
    ref = ReferenceBuffer()
    errors = []

    def writer():
        for k in range(300):
            ref.push(pcm(ramp(k * TEN_MS, TEN_MS)), k * 10 * MS)

    def reader():
        for k in range(300):
            w = ref.window(max(0, (k - 20)) * 10 * MS, TEN_MS)
            if w is not None and len(w) != TEN_MS:
                errors.append(len(w))

    threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors


# ---- LinearEchoPath ---------------------------------------------------------------------

def room48(far: np.ndarray, delay_ms: int = 40) -> np.ndarray:
    d = delay_ms * RATE // 1000
    ir = np.zeros(d + 4500)
    for k, g in ((d, 0.6), (d + 270, 0.25), (d + 930, 0.12), (d + 2100, 0.06), (d + 4200, 0.03)):
        ir[k] = g
    return np.convolve(far, ir)[: len(far)]


def blocks(path: LinearEchoPath, mic: np.ndarray, far: np.ndarray):
    out, states = [], []
    for i in range(0, len(mic), TEN_MS):                   # the capture side sends 10 ms blocks
        o, s = path.process(pcm(mic[i:i + TEN_MS]), far[i:i + TEN_MS].astype(np.int16))
        out.append(np.frombuffer(o, "<i2"))
        states.append(s)
    return np.concatenate(out).astype(float), states


def test_status_warms_up_for_500_ms_then_is_active():
    path = LinearEchoPath()
    silence = np.zeros(TEN_MS * 60)
    _, states = blocks(path, silence, silence)
    assert states[: WARMUP_MS // 10] == ["warming_up"] * (WARMUP_MS // 10)
    assert set(states[WARMUP_MS // 10:]) == {"active"}


def test_playback_echo_is_cancelled_at_48_khz():
    rng = np.random.default_rng(11)
    far = rng.normal(0, 3000, RATE * 8)
    echo = room48(far)
    out, _ = blocks(LinearEchoPath(), echo.astype(np.int16), far)
    last = slice(RATE * 6, RATE * 8)
    erle = 10 * np.log10(np.mean(echo[last] ** 2) / max(np.mean(out[BLOCK:][last.start - BLOCK:last.stop - BLOCK] ** 2), 1e-9))
    assert erle > 20, erle


def test_with_no_echo_the_audience_passes_through_one_block_late():
    rng = np.random.default_rng(12)
    far = rng.normal(0, 3000, RATE * 3)
    near = rng.normal(0, 2000, RATE * 3).astype(np.int16)
    out, _ = blocks(LinearEchoPath(), near, far)
    np.testing.assert_allclose(out[BLOCK:], near[:-BLOCK], atol=1)


def test_block_lengths_must_match():
    with pytest.raises(ValueError):
        LinearEchoPath().process(bytes(960), np.zeros(10, np.int16))
