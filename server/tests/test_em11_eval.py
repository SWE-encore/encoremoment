"""EM-11 evaluation tool (server/tools/em11_eval.py) without TensorFlow or data.

A scripted model stands in for YAMNet, so these check the tool itself: which clips go into
which group, the onset and level arithmetic, and how a run through the real ServerSession
is scored.
"""
import importlib.util
import sys
from pathlib import Path

import numpy as np

_spec = importlib.util.spec_from_file_location(
    "em11_eval", Path(__file__).resolve().parents[1] / "tools" / "em11_eval.py")
em11 = importlib.util.module_from_spec(_spec)
sys.modules["em11_eval"] = em11  # dataclasses look their module up
_spec.loader.exec_module(em11)

RATE = em11.RATE


class LoudIsReaction:
    """Scores ``reaction`` .5 for a window louder than -30 dBFS, everything else 0."""

    name = "scripted"

    def __init__(self, reaction="laughter"):
        self.reaction = reaction

    def __call__(self, pcm):
        x = np.frombuffer(pcm, "<i2").astype(np.float64) / 32768
        loud = 10 * np.log10(max((x ** 2).mean(), 1e-12)) > -30
        return {r: (.5 if loud and r == self.reaction else 0.0) for r in em11.REACTIONS}


def noise(seconds, level=.3, seed=1):
    return (np.random.default_rng(seed).normal(0, level, int(seconds * RATE)) * 32768).clip(-32768, 32767).astype(np.int16)


def test_each_clip_joins_one_group_and_seen_clips_stay_out():
    labels = {
        "1": {"Laughter", "Human_voice"},
        "2": {"Laughter", "Screaming"},          # both reactions: neither positive group
        "3": {"Screaming", "Shout"},
        "4": {"Gasp", "Breathing"},              # gasp first, never also a breathing negative
        "5": {"Speech", "Male_speech_and_man_speaking"},
        "6": {"Speech", "Shout"},                # a shout is not plain speech
        "7": {"Music", "Guitar"},
        "8": {"Music", "Singing", "Human_voice"},
        "9": {"Cough"},
        "1791": {"Laughter"},                    # an ESC-50 clip EM-26 already ran
    }
    pools = em11.candidates(labels)
    assert {g: sorted(ids) for g, ids in pools.items() if ids} == {
        "laughter": ["1"], "scream": ["3"], "gasp": ["4"], "speech": ["5"], "music": ["7"],
        "shout_yell": ["6"], "breath_cough": ["9"]}
    assert em11.candidates(labels) == pools  # the order is fixed by the seed


def test_onset_and_active_level():
    pcm = np.concatenate([np.zeros(RATE // 2, np.int16), noise(1.0, .1)])
    assert abs(em11.onset_ms(pcm) - 500) <= 10
    # Silence does not dilute the active level.
    assert abs(em11.active_rms(pcm) / em11.active_rms(noise(1.0, .1)) - 1) < .05


def test_playback_is_mixed_at_the_requested_level():
    clip, back = noise(1.0, .05, seed=2), noise(3.0, .05, seed=3)
    for snr in (10, 0):
        mixed = em11.mixed_stream(clip, back, snr)
        lead = RATE * em11.LEAD_MS // 1000
        playback = mixed.astype(np.float64)
        playback[lead:lead + len(clip)] -= clip
        ratio = 20 * np.log10(em11.active_rms(clip) / em11.active_rms(playback.round().astype(np.int16)))
        assert abs(ratio - snr) < .2
        assert len(mixed) == lead + len(clip) + RATE * em11.TAIL_MS // 1000


def test_a_run_through_the_server_is_scored_by_what_opened_while_the_clip_played():
    clip = em11.Clip("x", "laughter", "laughter", noise(1.5), onset=0)
    rows = em11.evaluate([clip], LoudIsReaction("laughter"), snrs=())
    assert [(r["setup"], r["condition"]) for r in rows] == [("audio-only", "clean"), ("face-server", "clean")]
    for row in rows:
        assert row["opened"] == ["laughter"] and row["hit"] and row["outside"] == []
        # The first window over the clip opens; it reaches the server with its chunk.
        assert 0 < row["delay_ms"] < 1_500
        assert row["peak"]["laughter"] == .5
    assert rows[1]["source"]["config"].startswith("fusion-")  # the face-server set-up uses TabRules

    scream = em11.Clip("y", "laughter", "laughter", noise(1.5), onset=0)
    row = em11.evaluate([scream], LoudIsReaction("scream"), snrs=())[0]
    assert row["opened"] == ["scream"] and row["hit"] is False and row["delay_ms"] is None

    quiet = em11.Clip("z", "speech", None, noise(1.5, .001), onset=0)
    row = em11.evaluate([quiet], LoudIsReaction("laughter"), snrs=())[0]
    assert row["opened"] == [] and row["hit"] is None


def test_playback_alone_is_counted_apart_from_the_reaction():
    # Playback the model scores as laughter opens the event before the clip starts; the
    # still-open event then swallows the reaction, which therefore counts as missed.
    clip = em11.Clip("x", "laughter", "laughter", noise(1.0, .3, seed=4), onset=0)
    speech = em11.Clip("s", "speech", None, noise(2.0, .3, seed=5), onset=0)
    rows = em11.evaluate([clip, speech], LoudIsReaction("laughter"), snrs=(0,))
    mixed = [r for r in rows if r["condition"] == "speech+0dB" and r["setup"] == "audio-only"]
    assert len(mixed) == 1 and mixed[0]["outside"] == ["laughter"] and mixed[0]["hit"] is False
    table = em11.report(rows)
    assert "| laughter | speech | +0 dB |" in table and "| speech | 1 |" in table
