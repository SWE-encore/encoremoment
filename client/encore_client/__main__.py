"""Common client entry point. From client/: python -m encore_client --demo."""
from __future__ import annotations
import argparse
from pathlib import Path
import sys
from .session.session import DeliveryLog, ScenarioDriver, Session, load_scenario

ROOT = Path(__file__).resolve().parents[2]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", nargs="?", help="controlled content video (Ubuntu/GStreamer)")
    parser.add_argument("--demo", action="store_true", help="start a local contract fixture server")
    parser.add_argument("--scenario", type=Path, help="contract scenario for demo or external fake server")
    parser.add_argument("--server", default="ws://127.0.0.1:8765")
    parser.add_argument("--session-id")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--no-player", action="store_true", help="UI/contract development without GStreamer")
    parser.add_argument("--clip-index", type=Path, help="local saved clip index")
    parser.add_argument("--log")
    parser.add_argument("--timeout", type=float, default=30,
                        help="headless run length in seconds; with --mic-wav, counted after the file ends")
    parser.add_argument("--title")
    parser.add_argument("--autoplay", action="store_true")
    parser.add_argument("--audio-sink", default="autoaudiosink")
    parser.add_argument("--mic-wav", type=Path, metavar="FILE.wav",
                        help="stand-in microphone until capture exists: stream this mono 16 kHz "
                             "16-bit WAV to the server while the session's microphone is on")
    args = parser.parse_args(argv)
    if args.video and (args.no_player or args.headless):
        parser.error("video requires the graphical GStreamer player")
    if args.no_player and not (args.demo or args.scenario):
        parser.error("--no-player requires --demo or --scenario; a live session needs a player anchor")
    scenario_path = args.scenario
    if args.demo and scenario_path is None:
        scenario_path = ROOT / "contracts/samples/04_duplicate_delivery.json"
    scenario = load_scenario(scenario_path) if scenario_path else None
    if scenario and args.session_id and args.session_id != scenario["session_id"]:
        parser.error("--session-id must match the selected scenario")
    server = None
    session = None
    try:
        if args.demo:
            from .session.fake_server import FakeServer
            server = FakeServer(scenario, port=0, interval_s=0.05).start()
        # A live session turns the microphone on at Start (R11), so the server's
        # events carry a microphone generation the client accepts. A scenario
        # sends its own controls instead.
        session = Session(server.url if server else args.server,
                          scenario["session_id"] if scenario else args.session_id,
                          log=DeliveryLog(args.log),
                          microphone_on_start=scenario is None)
        driver = ScenarioDriver(session, scenario) if scenario else None
        mic = None
        if args.mic_wav:
            import wave
            from .sensing.wav_mic import WavMicrophone
            try:
                mic = WavMicrophone(session, args.mic_wav)
            except (OSError, EOFError, ValueError, wave.Error) as exc:
                parser.error(f"--mic-wav: {exc}")
        if args.headless:
            from .session.__main__ import run_headless
            return run_headless(session, driver, args.timeout, mic)
        from PySide6.QtCore import Qt, QTimer
        from PySide6.QtWidgets import QApplication, QDockWidget, QMainWindow
        from .ui.workspace import Workspace
        app = QApplication.instance() or QApplication(sys.argv[:1])
        index_path = args.clip_index or (ROOT / "samples/clip_index.json" if args.demo
                                        else ROOT / "artifacts/clip_index.json")
        workspace = Workspace(session, driver, index_path,
                              require_anchor=scenario is None, demo=scenario is not None)
        if args.no_player:
            win = QMainWindow()
            win.setCentralWidget(workspace)
            win.setWindowTitle("Encore Moment — contract/UI development")
            win.resize(800, 900)
        else:
            try:
                from .player.window import PlayerWindow
                win = PlayerWindow(session.session_id, args.audio_sink, session.send_anchor)
            except (ImportError, ValueError) as exc:
                parser.exit(2, f"GStreamer player unavailable: {exc}\n"
                            "Use the Ubuntu setup in README, or --demo --no-player for UI development.\n")
            dock = QDockWidget("Session and highlights", win)
            dock.setWidget(workspace)
            dock.setFeatures(QDockWidget.DockWidgetFeature.NoDockWidgetFeatures)
            win.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
            win.resize(1440, 900)
            win.resizeDocks([dock], [560], Qt.Orientation.Horizontal)
            workspace.clips.player.playbackStateChanged.connect(
                lambda state: win.player.pause() if state.name == "PlayingState" else None)
            win.anchor_sent.connect(
                lambda anchor: workspace.clips.player.stop()
                if anchor["playback"]["state"] == "playing" else None)
            if args.video:
                win.open_file(args.video, args.title)
                if args.autoplay:
                    win.player.play()
        if mic is not None:
            workspace.sensing.setText(
                f"Microphone: stand-in file {args.mic_wav.name} — no real capture")
            mic_timer = QTimer(win)
            mic_timer.timeout.connect(mic.pump)
            mic_timer.start(50)
        app.aboutToQuit.connect(workspace.shutdown)
        win.show()
        return app.exec()
    finally:
        if session:
            session.end()
            session.log.close()
        if server:
            server.stop()


if __name__ == "__main__":
    sys.exit(main())
