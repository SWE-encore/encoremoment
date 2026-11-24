"""Why a clip has no footage, or only part of it, is shown the same way everywhere (R4, R7, R10).

The recorder, crash recovery and the contract's clip plan write a ``reason`` code. These
tests keep one wording for every code, bound the label length, and fail when a new code
appears in the sources without a wording.
"""

import json
import re
from pathlib import Path

import pytest

from encore_client.clips.highlights import build_highlights, plan_text
from encore_client.clips.model import (
    MAX_REASON_CHARS, NO_REASON, REASON_TEXT, Clip, ClipStatus, effective_label,
    gap_detail, gap_summary, load_index, reason_text,
)
from encore_client.clips.storage import _prefix, recover
from encore_client.session.contract import SAMPLES_DIR, SessionView

ROOT = Path(__file__).resolve().parents[2]
SESSION = "ses-sample-05"


def replay(name):
    data = json.loads((SAMPLES_DIR / f"{name}.json").read_text(encoding="utf-8"))
    view = SessionView(data["session_id"])
    for step in data["messages"]:
        view.receive(step["dir"], step["msg"])
    return view


def row(status, reason=None, *, gaps=(), clip_id="c-1", events=("evt-0006",)):
    data = {"clip_id": clip_id, "event_type": "laughter", "timecode": "00:00:00",
            "intensity": 0.5, "status": status, "path": None, "session_id": SESSION,
            "event_ids": list(events), "gaps": [list(g) for g in gaps]}
    if reason is not None:
        data["reason"] = reason
    return data


# ---- 1. every code written anywhere has a wording ----------------------------------

CODE_PATTERNS = [
    re.compile(r'"reason":\s*"([a-z_]+)"'),                 # recorder rows
    re.compile(r'reason="([a-z_]+)"'),                      # crash recovery
    re.compile(r'_unavailable\([^)]*,\s*"([a-z_]+)"\)'),    # recorder helper
    re.compile(r'return "(?:unavailable|partial)", "([a-z_]+)"'),  # contract clip plan
]
SOURCES = [
    ROOT / "client/encore_client/clips/recorder.py",
    ROOT / "client/encore_client/clips/storage.py",
    ROOT / "contracts/reference/encore_contract.py",
]


def written_codes():
    codes = set()
    for source in SOURCES:
        text = source.read_text(encoding="utf-8")
        for pattern in CODE_PATTERNS:
            codes.update(pattern.findall(text))
    return codes


def test_the_sources_still_write_the_codes_this_test_looks_for():
    # Guards the scan itself: if these vanish, the patterns no longer match the code.
    assert {"camera_off", "camera_stopped", "interrupted_capture",
            "before_camera_activation", "unknown_camera_generation"} <= written_codes()


def test_every_reason_code_in_the_sources_has_a_wording():
    missing = written_codes() - REASON_TEXT.keys()
    assert not missing, f"add these codes to REASON_TEXT in clips/model.py: {sorted(missing)}"


# ---- 2. reason_text -----------------------------------------------------------------

@pytest.mark.parametrize("code", sorted(REASON_TEXT))
def test_known_codes_read_as_words_not_as_errors(code):
    text = reason_text(code)
    assert text == REASON_TEXT[code]
    assert "export failed" not in text and "_" not in text


def test_no_reason_keeps_the_old_wording():
    assert reason_text(None) == reason_text("") == NO_REASON


def test_an_export_error_is_one_short_line():
    error = "clip encoding\n  or decode check failed: " + "x" * 500
    text = reason_text(error)
    assert text.startswith("export failed: clip encoding or decode check failed")
    assert "\n" not in text and "  " not in text
    assert len(text) <= len("export failed: ") + MAX_REASON_CHARS
    assert text.endswith("…")


def test_a_short_export_error_is_shown_whole():
    assert reason_text("disk full") == "export failed: disk full"


# ---- 3. labels ----------------------------------------------------------------------

@pytest.mark.parametrize("code", sorted(REASON_TEXT))
def test_an_unavailable_clip_label_says_why(tmp_path, code):
    clip = Clip.from_dict(row("unavailable", code))
    assert effective_label(clip, tmp_path) == f"— No footage ({REASON_TEXT[code]})"


def test_a_reason_that_is_not_text_is_ignored(tmp_path):
    data = row("unavailable")
    data["reason"] = {"code": "camera_off"}
    clip = Clip.from_dict(data)
    assert clip.reason is None
    assert NO_REASON in effective_label(clip, tmp_path)


def test_a_few_gaps_are_listed(tmp_path):
    (tmp_path / "c.webm").write_bytes(b"video")
    data = row("partial", gaps=[(0, 4), (10.5, 12)])
    data["path"] = "c.webm"
    label = effective_label(Clip.from_dict(data), tmp_path)
    assert label == "⚠️ Partial (missing: 0–4s, 10.5–12s)"


def test_many_small_gaps_from_a_real_webcam_stay_one_short_line(tmp_path):
    # The #35 review: 59 gaps of up to 36 ms made a 900+ character label.
    gaps = [(i * 0.5, i * 0.5 + 0.036) for i in range(59)]
    (tmp_path / "c.webm").write_bytes(b"video")
    data = row("partial", gaps=gaps)
    data["path"] = "c.webm"
    label = effective_label(Clip.from_dict(data), tmp_path)
    assert label == "⚠️ Partial (missing 2.1 s in 59 places)"
    assert len(label) < 60


def test_gap_summary_switches_from_list_to_total_after_three():
    assert gap_summary([(0, 1), (2, 3), (4, 5)]).startswith("missing: ")
    assert gap_summary([(0, 1), (2, 3), (4, 5), (6, 7)]) == "missing 4.0 s in 4 places"


def test_gap_detail_lists_ranges_and_counts_the_rest():
    gaps = [(i, i + 0.5) for i in range(14)]
    detail = gap_detail(gaps)
    assert detail.startswith("Missing ranges: 0–0.5s, 1–1.5s")
    assert detail.endswith("… and 4 more")
    assert "more" not in gap_detail(gaps[:3])


# ---- 4. the same wording in every view ---------------------------------------------

@pytest.mark.parametrize("code", ["camera_off", "camera_stopped", "unknown_camera_generation"])
def test_plan_and_saved_clip_use_the_same_words(tmp_path, code):
    planned = plan_text("unavailable", code)
    saved = effective_label(Clip.from_dict(row("unavailable", code)), tmp_path)
    assert REASON_TEXT[code] in planned and REASON_TEXT[code] in saved


def test_plan_texts_for_the_camera_off_sample():
    items = {h.event_id: h for h in build_highlights(replay("05_camera_off"), [], Path("."))}
    texts = {event: h.clips[0].status for event, h in items.items() if h.clips}
    assert "no footage: camera was off" in texts.values()
    assert any("partial clip expected (camera turned on late)" in t for t in texts.values())


def test_highlight_and_saved_clips_show_the_identical_label(tmp_path):
    clip = Clip.from_dict(row("unavailable", "camera_stopped"))
    highlight = {h.event_id: h for h in build_highlights(replay("05_camera_off"), [clip], tmp_path)}
    assert highlight["evt-0006"].clips[0].status == effective_label(clip, tmp_path)


def test_unknown_plan_status_is_still_readable():
    assert plan_text("archived", None) == "clip expected: archived"


# ---- 5. end to end through crash recovery -----------------------------------------

def test_a_capture_interrupted_by_a_crash_reads_as_interrupted_not_failed(tmp_path):
    index = tmp_path / "clip_index.json"
    pending = row("pending", clip_id="a" * 32)
    pending["capture_owner"] = _prefix(index) + "gone"   # its owner directory no longer exists
    index.write_text(json.dumps({"clips": [pending]}))
    recover(index)
    (clip,) = load_index(index)
    assert clip.status is ClipStatus.UNAVAILABLE and clip.reason == "interrupted_capture"
    label = effective_label(clip, tmp_path)
    assert label == "— No footage (recording stopped when the app closed)"
    assert "export failed" not in label
