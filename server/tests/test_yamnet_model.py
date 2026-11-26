"""The real YAMNet adapter. Needs server/requirements-yamnet.txt; downloads the model on first use."""

import math
import struct

import pytest

pytest.importorskip("tensorflow_hub")

from encore_server.audio import WINDOW_SAMPLES  # noqa: E402
from encore_server.models import YAMNET_CLASSES, YAMNET_HANDLE, YamnetModel, sha256_tree, tagged  # noqa: E402


@pytest.fixture(scope="module")
def model():
    return YamnetModel()


def test_one_window_gives_one_score_per_reaction(model):
    scores = model(bytes(2 * WINDOW_SAMPLES))
    assert set(scores) == set(YAMNET_CLASSES)
    assert all(0.0 <= v <= 1.0 for v in scores.values())


def test_silence_and_a_tone_are_not_reactions(model):
    tone = struct.pack(f"<{WINDOW_SAMPLES}h", *(round(16_000 * math.sin(2 * math.pi * 440 * i / 16_000))
                                                 for i in range(WINDOW_SAMPLES)))
    for pcm in (bytes(2 * WINDOW_SAMPLES), tone):
        assert max(model(pcm).values()) < 0.1


def test_the_name_carries_the_digest_of_the_loaded_weights(model):
    import tensorflow_hub as hub

    assert model.sha256 == sha256_tree(hub.resolve(YAMNET_HANDLE))
    assert model.name == tagged("google/yamnet/1", model.sha256)
