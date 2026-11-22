"""EM-13 / R4: Tonight's Highlights.

Acceptance (Jira EM-13):
1. FINAL events of the session, highest observed intensity first, with a
   deterministic tie rule.
2. Events without a clip are still listed with their metadata; only a
   finalized, checked file gets a play link.
3. Order and links are checked for equal scores, shared clips, camera off,
   failed exports and pending deletion.

Events come from the contract fixtures replayed through the shared receiver.
"""
import copy
import json
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from encore_client.clips.highlights import NO_CLIP, build_highlights, order_key
from encore_client.clips.model import Clip, ClipStatus
from encore_client.session.contract import SAMPLES_DIR, SessionView

SESSION = "ses-sample-05"


def scenario(name):
    return json.loads((SAMPLES_DIR / f"{name}.json").read_text(encoding="utf-8"))


def replay(data):
    view = SessionView(data["session_id"])
    for step in data["messages"]:
        view.receive(step["dir"], step["msg"])
    return view


def with_intensity(data, values):
    """Copy of a scenario with every update of an event set to one combined intensity."""
    data = copy.deepcopy(data)
    for step in data["messages"]:
        msg = step["msg"]
        if msg["kind"] == "event_update" and msg["event"]["event_id"] in values:
            msg["event"]["intensity"]["combined"] = values[msg["event"]["event_id"]]
    return data


def clip(clip_id, status, events, *, path=None, part=0, session=SESSION):
    return Clip(clip_id, "laughter", "00:00:00", 0.5, ClipStatus(status), path,
                session_id=session, event_ids=tuple(events), part=part)


def write(tmp_path, name, data=b"video"):
    (tmp_path / name).write_bytes(data)
    return name


def by_id(items):
    return {h.event_id: h for h in items}


# ---- 1. order -------------------------------------------------------------------


def test_final_events_by_intensity_highest_first():
    items = build_highlights(replay(scenario("05_camera_off")), [], Path("."))
    assert [(h.rank, h.event_id, h.intensity) for h in items] == [
        (1, "evt-0006", 0.72), (2, "evt-0005", 0.6), (3, "evt-0007", 0.5)]


def test_equal_scores_fall_back_to_trigger_time_then_event_id():
    data = with_intensity(scenario("05_camera_off"), {"evt-0005": .6, "evt-0006": .6, "evt-0007": .6})
    items = build_highlights(replay(data), [], Path("."))
    assert [h.event_id for h in items] == ["evt-0005", "evt-0006", "evt-0007"]   # trigger order
    rows = [{"intensity": .6, "trigger_ms": 1000, "event_id": e} for e in ("evt-b", "evt-a")]
    assert [r["event_id"] for r in sorted(rows, key=order_key)] == ["evt-a", "evt-b"]


def test_order_is_the_same_whatever_order_the_events_arrived_in():
    data = with_intensity(scenario("05_camera_off"), {"evt-0005": .6, "evt-0006": .6, "evt-0007": .6})
    first = [h.event_id for h in build_highlights(replay(data), [], Path("."))]
    for _ in range(3):
        assert [h.event_id for h in build_highlights(replay(data), [], Path("."))] == first


def test_events_without_intensity_go_last():
    rows = [{"intensity": None, "trigger_ms": 1, "event_id": "evt-a"},
            {"intensity": 0.1, "trigger_ms": 9, "event_id": "evt-b"}]
    assert [r["event_id"] for r in sorted(rows, key=order_key)] == ["evt-b", "evt-a"]


def test_only_final_events_are_listed():
    data = scenario("01_single_event")
    open_only = dict(data, messages=[s for s in data["messages"]
                                     if not (s["msg"]["kind"] == "event_update"
                                             and s["msg"]["event"]["phase"] == "FINAL")])
    assert build_highlights(replay(open_only), [], Path(".")) == []
    assert [h.event_id for h in build_highlights(replay(data), [], Path("."))] == ["evt-0001"]


# ---- 2. metadata without clips, links only for checked files -----------------------


def test_events_without_a_saved_clip_are_listed_without_a_link():
    items = by_id(build_highlights(replay(scenario("05_camera_off")), [], Path(".")))
    assert items["evt-0005"].clips[0].status == "no footage: camera was off"
    assert "turned on late" in items["evt-0006"].clips[0].status
    assert items["evt-0007"].clips[0].status == "no footage: camera was stopped during the clip"
    for h in items.values():
        assert h.playable == () and h.clip_summary.startswith(NO_CLIP)
        assert h.title and h.type                     # metadata is still shown


def test_a_ready_clip_with_its_file_gets_the_only_link(tmp_path):
    clips = [clip("c-6", "partial", ["evt-0006"], path=write(tmp_path, "c6.mp4"))]
    items = by_id(build_highlights(replay(scenario("05_camera_off")), clips, tmp_path))
    assert items["evt-0006"].playable[0].path == (tmp_path / "c6.mp4").resolve()
    assert items["evt-0005"].playable == () and items["evt-0007"].playable == ()


# ---- 3. shared clips, failure, pending deletion, missing files ----------------------


def test_a_shared_clip_is_linked_from_both_events(tmp_path):
    shared = clip("c-shared", "ready", ["evt-0005", "evt-0006"], path=write(tmp_path, "s.mp4"))
    items = by_id(build_highlights(replay(scenario("05_camera_off")), [shared], tmp_path))
    assert items["evt-0005"].playable[0].path == items["evt-0006"].playable[0].path
    assert items["evt-0005"].clips[0].shared_with == ("evt-0006",)
    assert items["evt-0006"].clips[0].shared_with == ("evt-0005",)


@pytest.mark.parametrize("status, expect", [
    ("pending", "Processing"),
    ("unavailable", "export failed"),
    ("deletion_pending", "Deleting"),
])
def test_no_link_while_pending_failed_or_being_deleted(tmp_path, status, expect):
    entry = clip("c-6", status, ["evt-0006"], path=write(tmp_path, "c6.mp4"))
    h = by_id(build_highlights(replay(scenario("05_camera_off")), [entry], tmp_path))["evt-0006"]
    assert h.playable == () and expect in h.clips[0].status


def test_no_link_when_the_indexed_file_is_missing_or_empty(tmp_path):
    entries = [clip("c-5", "ready", ["evt-0005"], path="gone.mp4"),
               clip("c-6", "ready", ["evt-0006"], path=write(tmp_path, "empty.mp4", b""))]
    items = by_id(build_highlights(replay(scenario("05_camera_off")), entries, tmp_path))
    assert items["evt-0005"].playable == () and "File missing" in items["evt-0005"].clips[0].status
    assert items["evt-0006"].playable == ()


def test_clips_are_matched_only_by_explicit_session_and_event_ids(tmp_path):
    name = write(tmp_path, "x.mp4")
    entries = [clip("other-session", "ready", ["evt-0005"], path=name, session="ses-other"),
               clip("no-events", "ready", [], path=name),
               Clip("legacy", "laughter", "00:06:45", 0.6, ClipStatus.READY, name)]
    items = build_highlights(replay(scenario("05_camera_off")), entries, tmp_path)
    assert all(h.playable == () and h.clip_summary.startswith(NO_CLIP) for h in items)


def test_several_parts_are_listed_in_order(tmp_path):
    entries = [clip("p2", "ready", ["evt-0006"], path=write(tmp_path, "p2.mp4"), part=1),
               clip("p1", "pending", ["evt-0006"], part=0)]
    h = by_id(build_highlights(replay(scenario("05_camera_off")), entries, tmp_path))["evt-0006"]
    assert [(c.name, c.path is not None) for c in h.clips] == [("Part 1", False), ("Part 2", True)]


def test_planned_parts_of_a_capped_clip_are_named():
    h = build_highlights(replay(scenario("03_refire_cap")), [], Path("."))[0]
    assert len(h.clips) == 2 and "continues part 1" in h.clips[1].name


def test_clip_index_reads_event_links(tmp_path):
    from encore_client.clips.model import load_index
    index = tmp_path / "index.json"
    index.write_text(json.dumps({"clips": [
        {"clip_id": "c", "event_type": "gasp", "timecode": "00:01:00", "intensity": .4,
         "status": "ready", "path": "c.mp4", "session_id": "s", "event_ids": ["e1", "e2"], "part": 1},
        {"clip_id": "old", "event_type": "gasp", "timecode": "00:01:00", "intensity": .4,
         "status": "ready", "path": "old.mp4"}]}))
    new, old = load_index(index)
    assert (new.session_id, new.event_ids, new.part) == ("s", ("e1", "e2"), 1)
    assert (old.session_id, old.event_ids, old.part) == (None, (), 0)


# ---- UI (offscreen) -------------------------------------------------------------


@pytest.fixture(scope="module")
def app():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


class ViewOnly:
    def __init__(self, view):
        self.view = view


def test_panel_lists_every_highlight_and_offers_play_only_for_checked_files(app, tmp_path):
    from encore_client.ui.highlights import HighlightsPanel
    (tmp_path / "c6.mp4").write_bytes(b"video")
    (tmp_path / "index.json").write_text(json.dumps({"clips": [
        {"clip_id": "c-6", "event_type": "laughter", "timecode": "00:07:03", "intensity": .72,
         "status": "partial", "path": "c6.mp4", "session_id": SESSION, "event_ids": ["evt-0006"]},
        {"clip_id": "c-5", "event_type": "laughter", "timecode": "00:06:45", "intensity": .6,
         "status": "deletion_pending", "path": "c6.mp4", "session_id": SESSION,
         "event_ids": ["evt-0005"]}]}))
    panel = HighlightsPanel(ViewOnly(replay(scenario("05_camera_off"))), tmp_path / "index.json")
    played = []
    panel.play_requested.connect(played.append)
    assert panel.table.rowCount() == 3
    assert [panel.table.item(r, 0).text() for r in range(3)] == ["1", "2", "3"]
    assert [panel.table.item(r, 1).text() for r in range(3)] == ["scream", "laughter", "gasp"]
    assert "evt-0006" in panel.text().splitlines()[0]

    panel.table.setCurrentCell(0, 0)                  # evt-0006: partial clip with a file
    buttons = panel.play_buttons()
    assert len(buttons) == 1
    buttons[0].click()
    assert played == [(tmp_path / "c6.mp4").resolve()]

    panel.table.setCurrentCell(1, 0)                  # evt-0005: deletion pending
    assert panel.play_buttons() == [] and "Deleting" in panel.detail.text()
    panel.table.setCurrentCell(2, 0)                  # evt-0007: camera stopped, no clip
    assert panel.play_buttons() == [] and "camera was stopped" in panel.detail.text()


def test_panel_rereads_the_index_when_a_pending_clip_finishes(app, tmp_path):
    from encore_client.ui.highlights import HighlightsPanel
    index = tmp_path / "index.json"
    entry = {"clip_id": "c-6", "event_type": "laughter", "timecode": "00:07:03", "intensity": .72,
             "status": "pending", "path": None, "session_id": SESSION, "event_ids": ["evt-0006"]}
    index.write_text(json.dumps({"clips": [entry]}))
    panel = HighlightsPanel(ViewOnly(replay(scenario("05_camera_off"))), index)
    panel.table.setCurrentCell(0, 0)
    assert panel.play_buttons() == []
    (tmp_path / "c6.mp4").write_bytes(b"video")
    index.write_text(json.dumps({"clips": [dict(entry, status="ready", path="c6.mp4")]}) + " ")
    panel.refresh()
    assert len(panel.play_buttons()) == 1             # selection kept, link appears


def test_empty_session_says_so(app, tmp_path):
    from encore_client.ui.highlights import EMPTY, HighlightsPanel
    panel = HighlightsPanel(ViewOnly(SessionView("ses-empty")), tmp_path / "index.json")
    assert panel.text() == EMPTY and panel.table.isHidden()
