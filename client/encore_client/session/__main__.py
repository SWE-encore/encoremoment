"""Run the PC client with a session: python -m encore_client.session [video] [options].

With --scenario the client plays the PC side of a contracts/samples file
against the fake server (python -m encore_client.session.fake_server).
With --headless it runs without a window and prints what it receives.
"""

from __future__ import annotations

import argparse
import sys
import time

from .session import Conn, DeliveryLog, ScenarioDriver, Session, event_rows, load_scenario


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m encore_client.session", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video", nargs="?", help="local video file to open in the player")
    ap.add_argument("--server", default="ws://127.0.0.1:8765", help="result server base URL")
    ap.add_argument("--scenario", help="contracts/samples/*.json to play with the fake server")
    ap.add_argument("--session-id", help="session_id (default: the scenario's, or a new one)")
    ap.add_argument("--log", metavar="FILE", help="append the delivery log to FILE (JSON Lines)")
    ap.add_argument("--headless", action="store_true", help="no window; print status and events")
    ap.add_argument("--timeout", type=float, default=30.0, help="headless: give up after N seconds")
    ap.add_argument("--title", help="content title to report (default: file name)")
    ap.add_argument("--audio-sink", default="autoaudiosink", help="GStreamer audio sink description")
    ap.add_argument("--autoplay", action="store_true", help="start playing after opening")
    args = ap.parse_args(argv)

    scenario = load_scenario(args.scenario) if args.scenario else None
    session_id = args.session_id or (scenario["session_id"] if scenario else None)
    session = Session(args.server, session_id, log=DeliveryLog(args.log))
    driver = ScenarioDriver(session, scenario) if scenario else None

    if args.headless:
        return run_headless(session, driver, args.timeout)

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QDockWidget

    from ..player.window import PlayerWindow
    from .panel import SessionPanel

    app = QApplication(sys.argv[:1])
    win = PlayerWindow(session.session_id, args.audio_sink, session.send_anchor)
    panel = SessionPanel(session, driver)
    dock = QDockWidget("Session", win)
    dock.setWidget(panel)
    dock.setFeatures(QDockWidget.DockWidgetFeature.NoDockWidgetFeatures)
    win.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
    win.resize(1400, 760)
    if args.video:
        win.open_file(args.video, args.title)
        if args.autoplay:
            win.player.play()
    win.show()
    code = app.exec()
    session.end()
    session.log.close()
    return code


def run_headless(session: Session, driver: ScenarioDriver | None, timeout_s: float) -> int:
    """Start, print every status change and the event list, stop when done."""
    session.start()
    last_state, last_rows, deadline = None, None, time.monotonic() + timeout_s
    try:
        while time.monotonic() < deadline:
            session.poll()
            if driver is not None:
                driver.pump()
            if session.state != last_state:
                last_state = session.state
                extra = f" ({session.reason})" if session.reason else ""
                print(f"[session] {session.state.value}{extra}", flush=True)
            rows = event_rows(session.view)
            if rows != last_rows:
                last_rows = rows
                print(f"[session] events: {len(rows)}", flush=True)
                for r in rows:
                    print(f"           {r['event_id']}  {r['type']:<8} {r['phase']:<6} rev {r['revision']}", flush=True)
            if session.state is Conn.DISCONNECTED:
                break
            if driver is not None and driver.done:
                break
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    dropped = session.state is Conn.DISCONNECTED
    complete = driver is None or driver.done
    session.end()
    summary = session.summary()
    print(f"[session] ignored {summary['ignored']}  rejected {summary['rejected']}  "
          f"received {summary['received']}  sent {summary['sent']}", flush=True)
    session.log.close()
    return 0 if complete and not dropped else 1


if __name__ == "__main__":
    sys.exit(main())
