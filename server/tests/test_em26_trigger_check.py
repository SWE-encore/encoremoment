"""EM-26 laughter trigger cases (server/tools/em26_trigger_check.py) through the real ServerSession.

Covers what test_rolling_fusion.py and test_paired_demo.py do not: the 5 s edge from
both sides through the session, camera frames stamped after their audio or sent
with two chunks, frames delivered one chunk late, and weak sound with the camera
off or no face.
"""
import importlib.util
import sys
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
