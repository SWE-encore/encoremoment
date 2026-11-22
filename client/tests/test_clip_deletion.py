"""EM-16 / R7: clip review, deletion and cleanup state.

Acceptance (Jira EM-16):
1. Saved clips play locally; a deletion request is shown as pending or completed.
2. The clip, its thumbnail, temporary exports, links from events, and the
   needed server temporary media are cleaned up.
3. Delete during playback/export, repeat deletion, offline, and restart are
   tested, and a late result cannot restore the media or a play link.
"""
import json
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from encore_client.clips.deletion import DeletionManager, journal_path
from encore_client.clips.highlights import build_highlights
from encore_client.clips.model import ClipStatus, load_index, playable_path
from encore_client.session.contract import SAMPLES_DIR, SessionView

SESSION = "ses-sample-05"


class Clock:
    def __init__(self):
        self.now = 1_000_000

    def __call__(self):
        return self.now


class Remote:
    """Fake server cleanup: records op IDs, answers according to ``online``."""

    def __init__(self, online=True):
        self.online = online
        self.requests = []

    def request(self, op_id, session_id, clip_id):
        self.requests.append((op_id, session_id, clip_id))
        return self.online


def entry(clip_id, path, events, **extra):
    return {"clip_id": clip_id, "event_type": "laughter", "timecode": "00:07:03", "intensity": .7,
            "status": "ready", "path": path, "session_id": SESSION, "event_ids": events, **extra}


def setup(tmp_path, entries, files=("c1.mp4",)):
    for name in files:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_bytes(b"video")
    index = tmp_path / "clip_index.json"
    index.write_text(json.dumps({"clips": entries}))
    return index


def manager(index, **kw):
    kw.setdefault("clock", Clock())
    return DeletionManager(index, **kw)


def clip_of(dm, clip_id):
    return next(c for c in dm.load_clips() if c.clip_id == clip_id)


def view_05():
    data = json.loads((SAMPLES_DIR / "05_camera_off.json").read_text(encoding="utf-8"))
    view = SessionView(data["session_id"])
    for step in data["messages"]:
        view.receive(step["dir"], step["msg"])
    return view


# ---- 1. pending and completed ---------------------------------------------------


def test_local_deletion_completes_and_keeps_event_metadata(tmp_path):
    index = setup(tmp_path, [entry("c1", "c1.mp4", ["evt-0006"])])
    dm = manager(index)
    op = dm.delete("c1")
    assert op.completed and op.status_text() == "🗑 Deleted"
    assert not (tmp_path / "c1.mp4").exists()
    raw = json.loads(index.read_text())["clips"][0]
    assert raw["status"] == "deleted" and raw["path"] is None and raw["event_ids"] == ["evt-0006"]
    h = next(h for h in build_highlights(view_05(), dm.load_clips(), tmp_path) if h.event_id == "evt-0006")
    assert h.playable == () and "Deleted" in h.clips[0].status


def test_pending_until_the_server_acknowledges_then_completed(tmp_path):
    index = setup(tmp_path, [entry("c1", "c1.mp4", ["evt-0006"])])
    remote, clock = Remote(online=False), Clock()
    dm = manager(index, remote=remote, clock=clock)
    op = dm.delete("c1")
    assert not op.completed and op.local_done and "waiting for server" in op.status_text()
    assert clip_of(dm, "c1").status is ClipStatus.DELETION_PENDING
    assert not (tmp_path / "c1.mp4").exists()            # local media is gone already
    remote.online = True
    dm.tick()                                             # before the retry time: no new request
    assert len(remote.requests) == 1
    clock.now += 2_000
    dm.tick()
    assert dm.state("c1").completed and clip_of(dm, "c1").status is ClipStatus.DELETED
    assert len({op_id for op_id, _, _ in remote.requests}) == 1   # retries reuse the operation ID
    assert remote.requests[0][1:] == (SESSION, "c1")


# ---- 2. what is cleaned up ----------------------------------------------------------


def test_thumbnail_and_temporary_exports_are_removed(tmp_path):
    files = ("c1.mp4", "thumbs/c1.jpg", "tmp/c1.part1.mkv", "tmp/c1.part2.mkv")
    index = setup(tmp_path, [entry("c1", "c1.mp4", ["evt-0006"], thumbnail="thumbs/c1.jpg",
                                   temp_paths=["tmp/c1.part1.mkv", "tmp/c1.part2.mkv"])], files)
    manager(index).delete("c1")
    assert not any((tmp_path / f).exists() for f in files)


def test_a_file_used_by_another_retained_clip_is_kept_until_that_clip_goes(tmp_path):
    index = setup(tmp_path, [entry("c1", "shared.mp4", ["evt-0005"]),
                             entry("c2", "shared.mp4", ["evt-0006"])], ("shared.mp4",))
    dm = manager(index)
    dm.delete("c1")
    assert (tmp_path / "shared.mp4").exists()            # reference count: c2 still uses it
    assert playable_path(clip_of(dm, "c2"), tmp_path) is not None
    dm.delete("c2")
    assert not (tmp_path / "shared.mp4").exists()


def test_files_outside_the_clip_folder_are_never_deleted(tmp_path):
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir()
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"keep me")
    index = setup(clips_dir, [entry("c1", "../outside.mp4", ["evt-0006"])], ())
    assert manager(index).delete("c1").completed
    assert outside.exists()


def test_a_shared_clip_disappears_from_every_linked_event(tmp_path):
    index = setup(tmp_path, [entry("c1", "c1.mp4", ["evt-0005", "evt-0006"])])
    dm = manager(index)
    before = {h.event_id: h for h in build_highlights(view_05(), dm.load_clips(), tmp_path)}
    assert before["evt-0005"].playable and before["evt-0006"].playable
    dm.delete("c1")
    after = {h.event_id: h for h in build_highlights(view_05(), dm.load_clips(), tmp_path)}
    assert after["evt-0005"].playable == () and after["evt-0006"].playable == ()
    assert len(after) == 3                                # the events themselves remain


def test_journal_is_written_before_any_file_is_deleted(tmp_path, monkeypatch):
    index = setup(tmp_path, [entry("c1", "c1.mp4", ["evt-0006"])])
    seen = []
    real_unlink = Path.unlink

    def spy(self, *a, **kw):
        if self.name == "c1.mp4":
            seen.append("c1" in journal_path(index).read_text())
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(Path, "unlink", spy)
    manager(index).delete("c1")
    assert seen == [True]


# ---- 3. playback/export, repeat, offline, restart, late results ------------------------


def test_delete_during_export_cancels_it_and_blocks_its_commit(tmp_path):
    index = setup(tmp_path, [entry("c1", "c1.mp4", ["evt-0006"], status="pending")])
    cancelled = []
    dm = manager(index, cancel_export=cancelled.append)
    assert dm.may_commit_export("c1")
    dm.delete("c1")
    assert cancelled == ["c1"] and not dm.may_commit_export("c1")


def test_repeated_deletion_is_one_operation(tmp_path):
    index = setup(tmp_path, [entry("c1", "c1.mp4", ["evt-0006"])])
    remote = Remote()
    dm = manager(index, remote=remote)
    first = dm.delete("c1")
    again = dm.delete("c1")
    assert first.op_id == again.op_id and len(remote.requests) == 1


def test_a_late_result_cannot_restore_media_or_a_link(tmp_path):
    index = setup(tmp_path, [entry("c1", "c1.mp4", ["evt-0006"])])
    dm = manager(index)
    dm.delete("c1")
    (tmp_path / "c1.mp4").write_bytes(b"late export")                 # pipeline writes late
    index.write_text(json.dumps({"clips": [entry("c1", "c1.mp4", ["evt-0006"])]}))
    clip = clip_of(dm, "c1")
    assert clip.status is ClipStatus.DELETED and playable_path(clip, tmp_path) is None
    h = next(h for h in build_highlights(view_05(), dm.load_clips(), tmp_path) if h.event_id == "evt-0006")
    assert h.playable == ()


def test_restart_resumes_an_incomplete_deletion(tmp_path):
    index = setup(tmp_path, [entry("c1", "c1.mp4", ["evt-0006"])])
    offline = Remote(online=False)
    dm = manager(index, remote=offline)
    op_id = dm.delete("c1").op_id
    assert not dm.state("c1").completed
    # app restarts: the server is still offline -> still pending, still tombstoned
    still = manager(index, remote=Remote(online=False))
    assert not still.state("c1").completed and not still.may_commit_export("c1")
    # app restarts again with the server back: resumed before anything else
    online = Remote(online=True)
    later = manager(index, remote=online)
    assert later.resumed and later.state("c1").completed
    assert online.requests[0][0] == op_id                              # same operation ID


def test_a_failed_local_delete_stays_pending_and_is_retried(tmp_path, monkeypatch):
    index = setup(tmp_path, [entry("c1", "c1.mp4", ["evt-0006"])])
    real_unlink = Path.unlink
    fail = {"on": True}

    def flaky(self, *a, **kw):
        if fail["on"] and self.name == "c1.mp4":
            raise PermissionError(13, "Permission denied")
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(Path, "unlink", flaky)
    clock = Clock()
    dm = manager(index, clock=clock)
    op = dm.delete("c1")
    assert not op.completed and "Permission denied" in op.status_text()
    assert clip_of(dm, "c1").status is ClipStatus.DELETION_PENDING     # link hidden meanwhile
    fail["on"] = False
    clock.now += 2_000
    dm.tick()
    assert dm.state("c1").completed and not (tmp_path / "c1.mp4").exists()


def test_unknown_clip_and_damaged_journal(tmp_path):
    index = setup(tmp_path, [entry("c1", "c1.mp4", ["evt-0006"])])
    with pytest.raises(KeyError):
        manager(index).delete("nope")
    journal_path(index).write_text("{not json")
    dm = manager(index)
    assert dm.journal_error and journal_path(index).with_name(
        journal_path(index).name + ".corrupt").exists()


def test_index_without_event_links_still_loads(tmp_path):
    index = setup(tmp_path, [{"clip_id": "old", "event_type": "gasp", "timecode": "00:01:00",
                              "intensity": .4, "status": "ready", "path": "c1.mp4"}])
    assert [c.clip_id for c in manager(index).load_clips()] == ["old"]
    assert load_index(index)[0].thumbnail is None


# ---- UI (offscreen) ---------------------------------------------------------------


@pytest.fixture(scope="module")
def app():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_panel_deletes_the_playing_clip_and_shows_pending_then_deleted(app, tmp_path):
    from PySide6.QtCore import QUrl
    from encore_client.clips.panel import ClipPanel
    index = setup(tmp_path, [entry("c1", "c1.mp4", ["evt-0006"])])
    remote, clock = Remote(online=False), Clock()
    dm = DeletionManager(index, remote=remote, clock=clock)
    panel = ClipPanel(index, deletions=dm)
    panel.confirm_delete = lambda clip: True
    panel.list.setCurrentRow(0)
    assert panel.play_button.isEnabled() and panel.delete_button.isEnabled()
    panel.player.setSource(QUrl.fromLocalFile(str(tmp_path / "c1.mp4")))   # playing this clip

    assert panel.delete_selected()
    assert panel.player.source().isEmpty()                # released before the file is removed
    assert "waiting for server" in panel.detail.text()
    assert not panel.play_button.isEnabled() and not panel.delete_button.isEnabled()

    remote.online = True
    clock.now += 2_000
    dm.tick()
    panel.sync()
    assert "Deleted" in panel.list.item(0).text() and panel.selected().clip_id == "c1"


def test_panel_cancel_keeps_the_clip(app, tmp_path):
    from encore_client.clips.panel import ClipPanel
    index = setup(tmp_path, [entry("c1", "c1.mp4", ["evt-0006"])])
    panel = ClipPanel(index, deletions=DeletionManager(index, clock=Clock()))
    panel.confirm_delete = lambda clip: False
    panel.list.setCurrentRow(0)
    assert not panel.delete_selected()
    assert (tmp_path / "c1.mp4").exists() and not journal_path(index).exists()


def test_two_desktops_preserve_pending_cleanup_and_operation_identity(tmp_path):
    index = setup(tmp_path, [entry("one", "one.mp4", ["e1"]), entry("two", "two.mp4", ["e2"])],
                  ("one.mp4", "two.mp4"))
    remote = Remote(online=False)
    first = manager(index, remote=remote)
    second = manager(index)
    expected = first.delete("one")
    assert second.delete("one").op_id == expected.op_id
    assert second.delete("two").completed
    assert not second.may_commit_export("one")
    restarted = manager(index, remote=remote)
    assert set(restarted.ops) == {"one", "two"}
    assert not restarted.state("one").completed
    remote.online = True
    first.process()
    assert first.state("one").completed and first.state("two").completed
    assert {request[0] for request in remote.requests} == {expected.op_id}
