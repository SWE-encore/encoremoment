"""Common client entry point. From client/: python -m encore_client --demo."""
from __future__ import annotations
import argparse
from pathlib import Path
import sys
from .clock import monotonic_ms
from .live import send_mic_clock_on_start
from .session.session import RECONNECT_DELAYS_MS, DeliveryLog, ScenarioDriver, Session, load_scenario

ROOT = Path(__file__).resolve().parents[2]


class SessionMaker:
    """Creates the sessions of one run: the first at launch, then a new one for
    every Start after End (R11, repeat Start/End).

    Each session has a new ID and nothing of the one before it (generations,
    cursor, controls, audio). Its delivery log appends to the same ``--log``
    file. The player's newest anchor is handed to each new session, so Start
    does not wait for the next play or seek. ``options`` go to every
    ``Session`` (for example a test's ``transport_factory``).
    """

    def __init__(self, url, log_path=None, *, live=True, mic_clock=False, **options):
        self.url = url
        self.log_path = log_path
        self.options = options
        self.live = live  # microphone on at Start, reconnect after a drop (EM-20); a scenario is neither
        self.microphone_on_start = live
        self.mic_clock = mic_clock
        self.anchor = None  # the player's newest anchor
        self.current = None

    def __call__(self, session_id=None):
        session = Session(self.url, session_id, log=DeliveryLog(self.log_path),
                          microphone_on_start=self.microphone_on_start,
                          reconnect_delays_ms=RECONNECT_DELAYS_MS if self.live else None, **self.options)
        if self.mic_clock:
            send_mic_clock_on_start(session, monotonic_ms)
        if self.anchor is not None:
            session.send_anchor(self.anchor)
        self.current = session
        return session

    def send_anchor(self, msg):
        """Player callback: to the current session, and kept for the next one."""
        self.anchor = msg
        self.current.send_anchor(msg)


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
    parser.add_argument("--sessions", type=int, default=1, metavar="N",
                        help="headless live run: Start and End N sessions one after another, each with "
                             "a new session ID (--mic-wav plays the file again in each)")
    parser.add_argument("--title")
    parser.add_argument("--autoplay", action="store_true")
    parser.add_argument("--audio-sink", default="autoaudiosink")
    parser.add_argument("--mic-wav", type=Path, metavar="FILE.wav",
                        help="use a file as the microphone: stream this mono 16 kHz "
                             "16-bit WAV to the server while the session's microphone is on")
    parser.add_argument("--mic", action="store_true",
                        help="capture the PC's default microphone on Start")
    parser.add_argument("--local-server", choices=("loudness", "yamnet"),
                        help="start a session server inside the app with this model instead of using --server")
    parser.add_argument("--trigger", action="append", metavar="REACTION=LEVEL",
                        help="with --local-server: detector trigger level, e.g. laughter=0.6 (repeatable)")
    args = parser.parse_args(argv)
    if args.video and (args.no_player or args.headless):
        parser.error("video requires the graphical GStreamer player")
    live_input = args.mic or args.mic_wav is not None
    if args.no_player and not (args.demo or args.scenario or live_input):
        parser.error("--no-player requires --demo, --scenario, --mic or --mic-wav; "
                     "a live session without a player needs a microphone")
    if args.mic and args.mic_wav:
        parser.error("use either --mic or --mic-wav")
    if args.mic and args.headless:
        parser.error("--mic needs the window (Qt Multimedia); use --mic-wav with --headless")
    if args.local_server and (args.demo or args.scenario):
        parser.error("--local-server is for live sessions; it cannot be combined with --demo or --scenario")
    if args.trigger and not args.local_server:
        parser.error("--trigger only applies to --local-server")
    if args.sessions < 1:
        parser.error("--sessions must be at least 1")
    if args.sessions > 1 and not args.headless:
        parser.error("--sessions applies to --headless; in the window, press Start session again after End")
    if args.sessions > 1 and (args.demo or args.scenario):
        parser.error("--sessions is for live sessions; a scenario plays one session")
    try:
        from .live import parse_triggers
        triggers = parse_triggers(args.trigger)
    except ValueError as exc:
        parser.error(str(exc))
    scenario_path = args.scenario
    if args.demo and scenario_path is None:
        scenario_path = ROOT / "contracts/samples/04_duplicate_delivery.json"
    scenario = load_scenario(scenario_path) if scenario_path else None
    if scenario and args.session_id and args.session_id != scenario["session_id"]:
        parser.error("--session-id must match the selected scenario")
    server = None
    local = None
    qt_mic = None
    maker = None
    try:
        if args.local_server:
            from .live import start_local_server
            if args.local_server == "yamnet":
                from .bench.local import preload_tensorflow
                preload_tensorflow()  # before Qt Multimedia opens devices (see bench/local.py)
            try:
                local = start_local_server(args.local_server, triggers)
            except ImportError as exc:
                parser.exit(2, f"cannot start the {args.local_server} server: {exc}\n"
                            "Install requirements-dev.txt (YAMNet also needs server/requirements-yamnet.txt).\n")
            print(f"[encore] built-in session server: {local.model_name}, {local.config.name}", flush=True)
        if args.demo:
            from .session.fake_server import FakeServer
            server = FakeServer(scenario, port=0, interval_s=0.05).start()
        # A live session turns the microphone on at Start (R11), so the server's
        # events carry a microphone generation the client accepts, and reconnects
        # after a drop (EM-20). A scenario sends its own controls and is not
        # reconnected: the fake server would replay its script from the start.
        # A scenario also plays exactly one session; a live run may start a new
        # one after End (R11).
        url = server.url if server else (local.url if local else args.server)
        mic_clock = args.no_player and scenario is None
        maker = SessionMaker(url, args.log, live=scenario is None, mic_clock=mic_clock)
        session = maker(scenario["session_id"] if scenario else args.session_id)
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
            code = 0
            for n in range(args.sessions):
                if n:  # the previous session has ended: a fresh one, as Start after End does
                    session = maker()
                    if mic is not None:
                        mic.use_session(session)
                if args.sessions > 1:
                    print(f"[session] {n + 1}/{args.sessions}: {session.session_id}", flush=True)
                code = max(code, run_headless(session, driver, args.timeout, mic))
            return code
        from PySide6.QtCore import Qt, QTimer
        from PySide6.QtWidgets import QApplication, QDockWidget, QMainWindow
        from .ui.workspace import Workspace
        app = QApplication.instance() or QApplication(sys.argv[:1])
        recorder_factory = None
        from .platform_support import camera_unavailable_reason, desktop_capture
        if scenario is None and desktop_capture():
            from .clips.recorder import DesktopRecorder
            recorder_factory = DesktopRecorder
        index_path = args.clip_index or (ROOT / "samples/clip_index.json" if args.demo
                                        else ROOT / "artifacts/clip_index.json")
        workspace = Workspace(session, driver, index_path,
                              require_anchor=scenario is None and not mic_clock, demo=scenario is not None,
                              recorder_factory=recorder_factory,
                              session_factory=maker if scenario is None else None)
        if scenario is None and not desktop_capture():
            workspace.camera_panel.set_unavailable_reason(camera_unavailable_reason())
        if args.no_player:
            win = QMainWindow()
            win.setCentralWidget(workspace)
            win.setWindowTitle("Encore Moment — microphone test (no player)" if mic_clock
                               else "Encore Moment — contract/UI development")
            win.resize(800, 900)
        else:
            try:
                from .player.window import PlayerWindow
                win = PlayerWindow(session.session_id, args.audio_sink, maker.send_anchor)
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
        from .sensing.microphone import MicrophoneSwitch
        if mic is not None:
            if workspace.recorder is not None:
                mic.on_pcm = workspace.feed_audio  # the recorder of whichever session is current
            workspace.session_users.append(mic)
            workspace.sensing.setText(
                f"Microphone: stand-in file {args.mic_wav.name} — no real capture")
            workspace.add_microphone(MicrophoneSwitch(session, None, source=f"file {args.mic_wav.name}"))
            mic_timer = QTimer(win)
            mic_timer.timeout.connect(mic.pump)
            mic_timer.start(50)
        if mic is None and (args.mic or scenario is None):
            from .sensing.live_mic import LiveMicrophone, QtAudioInput
            source = LiveMicrophone(session, player=None if args.no_player else win.player, keep_s=0,
                                    on_pcm=workspace.feed_audio if workspace.recorder else None)
            try:
                qt_mic = QtAudioInput(source)  # opens only when microphone sensing is enabled
            except (RuntimeError, ImportError) as exc:
                session.microphone_on_start = maker.microphone_on_start = False
                workspace.sensing.setText(f"Microphone unavailable: {exc}")
            else:
                workspace.live_microphone = source
                where = f"built-in {local.model_name} server" if local else url
                workspace.sensing.setText(
                    f"Start enables microphone analysis at {where}. "
                    "Enable camera to record and share audience frames."
                    + (" Times are seconds since Start." if mic_clock else ""))
                workspace.sensing.setWordWrap(True)
                workspace.add_microphone(MicrophoneSwitch(session, qt_mic, source=qt_mic.device.description()))
        app.aboutToQuit.connect(workspace.shutdown)
        win.show()
        return app.exec()
    finally:
        if qt_mic is not None:
            qt_mic.stop()
        if maker is not None and maker.current is not None:
            maker.current.end()
            maker.current.log.close()
        if server:
            server.stop()
        if local:
            local.stop()


if __name__ == "__main__":
    sys.exit(main())
