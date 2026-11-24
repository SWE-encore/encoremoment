"""EM-26 laughter trigger cases (server/tools/em26_trigger_check.py) through the real ServerSession.

Covers what test_rolling_fusion.py and test_paired_demo.py do not: the 5 s edge from
both sides through the session, camera frames stamped after their audio or sent
with two chunks, frames delivered one chunk late, and weak sound with the camera
off or no face. Also checks the --wav path for recorded inputs, with generated
audio and the scripted model (no download).
"""
import importlib.util
import sys
import wave
from array import array
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "tools" / "em26_trigger_check.py"
_spec = importlib.util.spec_from_file_location("em26_trigger_check", _PATH)
check = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = check  # dataclasses look their module up here
_spec.loader.exec_module(check)


@pytest.mark.parametrize("case", check.CASES, ids=lambda c: c.name)
def test_case(case):
    result = check.run_case(case)
    assert result["doubled_hops"] == []
    assert result["open"] == case.expect_open
    assert result["open_hop"] == case.expect_hop
    assert result["events"] == (1 if case.expect_open else 0)


def test_every_case_has_an_expectation_for_its_open_hop():
    for case in check.CASES:
        assert (case.expect_hop is not None) == case.expect_open, case.name


def test_cli_reports_every_case(capsys):
    assert check.main([]) == 0
    out = capsys.readouterr().out
    assert all(case.name in out for case in check.CASES)


def _square_wav(path, level, seconds=5, rate=16000):
    """A recorded input the scripted audio model reads as a constant laughter score."""
    samples = array("h", (round(level * 32767) * (1 if i % 2 else -1) for i in range(seconds * rate)))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(samples.tobytes())
    return path


def test_recorded_input_plays_after_silence_with_each_face(tmp_path):
    cases = check.wav_cases("laughter", _square_wav(tmp_path / "weak.wav", .10), real_face=False)
    results = {case.name.split("/")[1]: check.run_case(case) for case in cases}
    assert list(results) == ["camera-off", "neutral", "smile"]
    assert not results["camera-off"]["open"] and not results["neutral"]["open"]
    smile = results["smile"]
    assert smile["open"] and smile["doubled_hops"] == []
    # Silence before the file: the first hops score nothing, then three paired hops open.
    assert smile["trace"][0]["audio"] == 0
    assert 1440 < smile["onset_to_open_ms"] < 2500

    strong = check.wav_cases("laughter", _square_wav(tmp_path / "strong.wav", .30), real_face=True)
    assert [case.name.split("/")[1] for case in strong] == ["camera-off", "no-face"]
    assert all(check.run_case(case)["ok"] for case in strong)


def test_recorded_input_must_be_what_the_pc_sends(tmp_path):
    path = tmp_path / "stereo.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(bytes(400))
    with pytest.raises(SystemExit):
        check.read_wav(path)
