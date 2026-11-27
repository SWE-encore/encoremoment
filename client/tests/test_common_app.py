"""Integration checks across the previously separate branches."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication
from encore_client.__main__ import ROOT
from encore_client.session.fake_server import FakeServer
from encore_client.session.session import Conn, ScenarioDriver, Session, load_scenario
from encore_client.ui.workspace import Workspace


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def wait_for(app, predicate):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    assert predicate(), "UI condition timed out"


def test_workspace_receives_deduplicates_and_shows_actual_disconnect(app, tmp_path):
    scenario = load_scenario(ROOT / "contracts/samples/04_duplicate_delivery.json")
    server = FakeServer(scenario, port=0, interval_s=0.005).start()
    session = Session(server.url, scenario["session_id"])
    driver = ScenarioDriver(session, scenario)
    workspace = Workspace(session, driver, tmp_path / "index.json", demo=True)
    workspace.show()
    try:
        assert "Camera: Off" in workspace.camera_panel.indicator.text()
        assert "Microphone: Off" in workspace.sensing.text()
        workspace.session_panel.start_btn.click()
        wait_for(app, lambda: driver.done and "evt-0004" in workspace.highlights.text())
        assert workspace.session_panel.table.rowCount() == 1
        assert session.view.summary() == scenario["expected"]
        assert "no exported clip linked" in workspace.highlights.text()
        server.stop()
        wait_for(app, lambda: session.state is Conn.DISCONNECTED)
        assert "Disconnected" in workspace.session_panel.status.text()
        assert not workspace.session_panel.table.isEnabled()
        workspace.session_panel.end_btn.click()
        assert session.state is Conn.ENDED
        assert not workspace.session_panel.start_btn.isEnabled()
    finally:
        workspace.shutdown()
        workspace.close()
        server.stop()


def test_start_requires_content_anchor_and_missing_clip_cannot_play(app, tmp_path):
    index = tmp_path / "index.json"
    index.write_text(json.dumps({"clips": [{"clip_id": "c1", "event_type": "gasp",
        "timecode": "00:00:01", "intensity": 0.5, "status": "ready", "path": "gone.mp4"}]}))
    session = Session("ws://127.0.0.1:1")
    workspace = Workspace(session, None, index, require_anchor=True)
    try:
        assert not workspace.session_panel.start_btn.isEnabled()
        workspace.session_panel.start()
        assert session.state is Conn.IDLE
        workspace.clips.list.setCurrentRow(0)
        assert not workspace.clips.play_button.isEnabled()
        assert "File missing" in workspace.clips.detail.text()
        index.write_text("not valid json")
        workspace.clips.reload()
        assert "Cannot load" in workspace.clips.detail.text()
    finally:
        workspace.shutdown()
        workspace.close()


@pytest.mark.parametrize("scenario", sorted((ROOT / "contracts/samples").glob("*.json")))
def test_common_headless_demo_cli(scenario):
    result = subprocess.run([sys.executable, "-m", "encore_client", "--demo", "--headless",
                             "--scenario", str(scenario)], cwd=ROOT / "client",
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "connected" in result.stdout


def test_full_entrypoint_player_session_and_clip_review(app, monkeypatch, tmp_path):
    pytest.importorskip("gi")
    from encore_client.__main__ import main
    from encore_client.player.window import PlayerWindow
    clip = tmp_path / "content.mp4"
    subprocess.run(["bash", str(ROOT / "client/tools/make_test_video.sh"), str(clip), "5"],
                   check=True, capture_output=True)

    def exercise_window(_app):
        win = next(w for w in app.topLevelWidgets() if isinstance(w, PlayerWindow) and w.isVisible())
        workspace = win.findChild(Workspace)
        try:
            wait_for(app, lambda: win.video.frames_shown > 0 and workspace.session.last_anchor is not None)
            workspace.session_panel.start_btn.click()
            wait_for(app, lambda: "evt-0004" in workspace.highlights.text())
            assert workspace.session_panel.table.rowCount() == 1
            original_content = win.player.title
            workspace.tabs.setCurrentIndex(1)
            workspace.clips.list.setCurrentRow(0)
            assert workspace.clips.play_button.isEnabled()
            workspace.clips.play_button.click()
            wait_for(app, lambda: workspace.clips.player.position() > 100)
            assert not win.player.want_playing
            assert win.player.title == original_content
            # The clip plays in the big video area over the paused film, not in the panel.
            assert win.stage.showing_clip()
            assert workspace.clips.video.parent() is win.stage.clip_page
            screenshot = os.environ.get("ENCORE_INTEGRATION_SCREENSHOT")
            if screenshot:
                win.grab().save(screenshot)
            # While the clip shows, the window's controls play and pause the clip, not the film.
            wait_for(app, lambda: win.slider.maximum() == workspace.clips.player.duration())
            win.toggle()
            assert workspace.clips.player.playbackState().name == "PausedState"
            assert win.stage.showing_clip() and not win.player.want_playing
            # Back to film shows the film where it was; Play then plays the film.
            win.stage.back_btn.click()
            assert not win.stage.showing_clip()
            win.toggle()
            assert win.player.want_playing and win.player.title == original_content
        finally:
            workspace.shutdown()
            win.close()
        return 0

    monkeypatch.setattr(QApplication, "exec", exercise_window)
    assert main([str(clip), "--demo", "--audio-sink", "fakesink sync=true", "--autoplay"]) == 0
