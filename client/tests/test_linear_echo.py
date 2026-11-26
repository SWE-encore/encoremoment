"""EM-10 (R1): the player's linear echo path without the WebRTC plugins. NumPy only."""

import threading

import numpy as np
import pytest

from encore_client.player import linear_echo
from encore_client.player.linear_echo import (
    BLOCK, DRIFT_MS, MAX_BULK_MS, RATE, TAPS, TARGET_MS, WARMUP_MS, LinearEchoPath, ReferenceBuffer,
)
from encore_client.sensing.linear_aec import LinearEchoCanceller, estimate_delay

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


def test_stereo_is_mixed_down_to_mono():
    ref = ReferenceBuffer()
    left, right = np.full(TEN_MS, 1000, np.int16), np.full(TEN_MS, 3000, np.int16)
    assert ref.push(pcm(np.stack([left, right], axis=1).ravel()), 0, channels=2) == TEN_MS
    assert (ref.window(0, TEN_MS) == 2000).all()


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


def played(far: np.ndarray) -> ReferenceBuffer:
    """What the player's tap would hold: ``far`` from running time 0, in 10 ms buffers."""
    ref = ReferenceBuffer(keep_ms=len(far) * 1000 // RATE + 100)
    for i in range(0, len(far), TEN_MS):
        ref.push(pcm(far[i:i + TEN_MS]), i * 1_000_000_000 // RATE)
    return ref


def blocks(mic: np.ndarray, far: np.ndarray, stamp_ms=lambda i, ms: ms):
    """Send ``mic`` in 10 ms blocks the way live_mic does: a whole-millisecond start time.

    Playback starts 10 ms before the microphone (silence first), so the first block's
    reference, read LEAD_MS early, is already there."""
    path = LinearEchoPath(played(np.concatenate([np.zeros(TEN_MS), far])))
    out, states = [], []
    for i in range(0, len(mic), TEN_MS):
        o, s = path.process(pcm(mic[i:i + TEN_MS]), (10 + stamp_ms(i, i * 1000 // RATE)) * MS)
        out.append(np.frombuffer(o, "<i2"))
        states.append(s)
    return np.concatenate(out).astype(float), states, path


def erle_db(echo: np.ndarray, out: np.ndarray) -> float:
    last = slice(RATE * 6, RATE * 8)                       # after convergence; output is one block late
    residual = out[last.start + BLOCK:last.stop + BLOCK] if len(out) >= last.stop + BLOCK else out[last.start + BLOCK:]
    return 10 * np.log10(np.mean(echo[last.start:last.start + len(residual)] ** 2) / max(np.mean(residual ** 2), 1e-9))


def test_status_warms_up_for_500_ms_then_is_active():
    silence = np.zeros(TEN_MS * 60)
    _, states, _ = blocks(silence, silence)
    assert states[: WARMUP_MS // 10] == ["warming_up"] * (WARMUP_MS // 10)
    assert set(states[WARMUP_MS // 10:]) == {"active"}


def test_playback_echo_is_cancelled_at_48_khz():
    far = np.random.default_rng(11).normal(0, 3000, RATE * 9)
    echo = room48(far)
    out, _, _ = blocks(echo.astype(np.int16), far)
    assert erle_db(echo, out) > 20


def test_jittery_microphone_timestamps_do_not_break_the_filter():
    # live_mic stamps blocks in whole ms and the Qt callback jitters: up to +-3 ms here.
    far = np.random.default_rng(13).normal(0, 3000, RATE * 9)
    echo = room48(far)
    jitter = np.random.default_rng(14).integers(-3, 4, len(far) // TEN_MS + 1)
    out, states, path = blocks(echo.astype(np.int16), far, lambda i, ms: ms + int(jitter[i // TEN_MS]))
    assert erle_db(echo, out) > 20
    assert path.reanchors == 0 and "reference_unavailable" not in states


def test_reading_the_reference_by_each_timestamp_would_have_failed():
    # Why the cursor exists: the same jitter, with the reference looked up per block.
    far = np.random.default_rng(13).normal(0, 3000, RATE * 9)
    echo = room48(far).astype(np.int16)
    jitter = np.random.default_rng(14).integers(-3, 4, len(far) // TEN_MS + 1)
    ref, canceller, out = played(far), LinearEchoCanceller(block=BLOCK, taps=TAPS), []
    for i in range(0, len(echo), TEN_MS):
        window = ref.window(max(0, i * 1000 // RATE + int(jitter[i // TEN_MS])) * MS, TEN_MS)
        window = np.zeros(TEN_MS) if window is None else window      # the last block runs past the end
        out.append(canceller.process(echo[i:i + TEN_MS] / 32768, window / 32768) * 32768)
    naive = erle_db(room48(far), np.concatenate(out))
    print(f"per-block lookup with +-3 ms jitter: {naive:.1f} dB")
    assert naive < 10


def test_a_jump_beyond_the_drift_limit_moves_the_cursor_and_restarts():
    far = np.random.default_rng(15).normal(0, 3000, RATE * 3)
    jump = DRIFT_MS + 10
    out, states, path = blocks(room48(far).astype(np.int16), far,
                               lambda i, ms: ms + (jump if ms >= 1500 else 0))
    assert path.reanchors == 1
    k = 1500 // 10
    assert states[k - 1] == "active" and states[k] == "warming_up"


def test_the_first_block_after_playback_starts_waits_for_the_lead():
    # At running time 0 the cursor would sit 5 ms before anything was played.
    path = LinearEchoPath(played(np.zeros(TEN_MS * 10)))
    mic = pcm(np.full(TEN_MS, 99))
    assert path.process(mic, 0) == (mic, "reference_unavailable")
    assert path.process(mic, 10 * MS)[1] == "warming_up"


def test_audio_not_played_yet_is_passed_through_unchanged():
    path = LinearEchoPath(played(np.zeros(TEN_MS * 10)))
    mic = pcm(np.full(TEN_MS, 1234))
    assert path.process(mic, 500 * MS) == (mic, "reference_unavailable")


def test_with_no_echo_the_audience_passes_through_one_block_late():
    rng = np.random.default_rng(12)
    far = rng.normal(0, 3000, RATE * 3)
    near = rng.normal(0, 2000, RATE * 3).astype(np.int16)
    out, _, path = blocks(near, far)
    np.testing.assert_allclose(out[BLOCK:], near[:-BLOCK], atol=1)
    assert path.realigns == 0 and path.bulk_ms == 0


# ---- bulk delay ---------------------------------------------------------------------------
# The tap is before the audio sink's buffer (160-200 ms on PulseAudio), so in a real room the
# echo can arrive later than the filter's 256 ms tail reaches.

def erle_late(echo: np.ndarray, out: np.ndarray, start_s: int = 9, stop_s: int = 11) -> float:
    a, b = RATE * start_s, RATE * stop_s
    return 10 * np.log10(np.mean(echo[a:b] ** 2) / max(np.mean(out[a + BLOCK:b + BLOCK] ** 2), 1e-9))


def test_estimate_delay_finds_an_echo_and_is_unsure_without_one():
    rng = np.random.default_rng(21)
    far = rng.normal(0, 3000, RATE * 2)
    window, search = RATE, RATE * 3 // 4
    echo = room48(far, delay_ms=300)
    lag, confidence = estimate_delay(echo[-window:], far[-(window + search):], decimate=3)
    assert abs(lag - 300 * RATE // 1000) <= 3 and confidence > linear_echo.CONFIDENT
    _, unrelated = estimate_delay(rng.normal(0, 3000, window), far[-(window + search):], decimate=3)
    assert unrelated < linear_echo.CONFIDENT
    assert estimate_delay(np.zeros(window), far[-(window + search):]) is None


def test_an_echo_beyond_the_tail_is_found_and_cancelled(monkeypatch):
    far = np.random.default_rng(22).normal(0, 3000, RATE * 12)
    echo = room48(far, delay_ms=300)
    out, states, path = blocks(echo.astype(np.int16), far)
    print(f"300 ms echo: bulk {path.bulk_ms} ms, {erle_late(echo, out):.1f} dB")
    assert path.realigns == 1
    assert abs(path.bulk_ms - (300 - linear_echo.LEAD_MS - TARGET_MS)) <= 2   # LEAD_MS is already early
    assert erle_late(echo, out) > 20
    assert states[-1] == "active"
    # Without the alignment the same room is not cancelled at all.
    monkeypatch.setattr(linear_echo, "CONFIDENT", float("inf"))
    unaligned, _, _ = blocks(echo.astype(np.int16), far)
    assert erle_late(echo, unaligned) < 3


def test_the_measured_delay_survives_a_new_path():
    # The player makes a new path after every seek; it passes the bulk delay on.
    path = LinearEchoPath(played(np.zeros(TEN_MS * 10)), bulk_ms=120)
    assert path.bulk_ms == 120


def test_an_echo_too_late_to_reach_is_reported_uncancelled():
    far = np.random.default_rng(23).normal(0, 3000, RATE * 5)
    echo = room48(far, delay_ms=MAX_BULK_MS + 40)
    mic = echo.astype(np.int16)
    out, states, path = blocks(mic, far)
    assert path.out_of_range and path.realigns == 0
    assert states[-1] == "uncancelled"
    np.testing.assert_array_equal(out[-TEN_MS:], mic[-TEN_MS:])     # passed through, not filtered


def test_near_and_far_of_different_lengths_are_refused():
    with pytest.raises(ValueError):
        LinearEchoCanceller().process(np.zeros(100), np.zeros(99))
