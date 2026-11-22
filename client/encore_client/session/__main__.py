"""Run the PC client with a session: python -m encore_client.session [video] [options].

With --scenario the client plays the PC side of a contracts/samples file
against the fake server (python -m encore_client.session.fake_server).
With --headless it runs without a window and prints what it receives.
"""

from __future__ import annotations

import sys
import time

from .session import Conn, ScenarioDriver, Session, event_rows


def main(argv=None):
    """Compatibility alias for the common application."""
    from ..__main__ import main as common_main
    return common_main(argv)


def run_headless(session: Session, driver: ScenarioDriver | None, timeout_s: float, mic=None) -> int:
    """Start, print every status change and the event list, stop when done.

    ``mic`` is an optional stand-in microphone (sensing.wav_mic). A scenario
    run ends when its steps and the file are done. A live run lasts until
    ``timeout_s`` after the file has been streamed, so results about its last
    seconds can still arrive.
    """
    session.start()
    last_state, last_rows, deadline = None, None, time.monotonic() + timeout_s
    try:
        while time.monotonic() < deadline:
            session.poll()
            if driver is not None:
                driver.pump()
            if mic is not None:
                mic.pump()
                if not mic.done:
                    deadline = max(deadline, time.monotonic() + timeout_s)
            if session.state != last_state:
                last_state = session.state
                extra = f" ({session.reason})" if session.reason else ""
                print(f"[session] {session.state.value}{extra}", flush=True)
            last_rows = _print_rows(session, last_rows)
            if session.state is Conn.DISCONNECTED and session.retry_at_ms is None:
                break  # dropped and not reconnecting
            # A live session (no scenario) runs until the timeout.
            if driver is not None and driver.done and _done(mic) and not session.media_pending:
                break
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    dropped = session.stale
    complete = _done(driver) and _done(mic)
    session.finish()  # a live session waits for the server to close open reactions
    while session.state is Conn.ENDING:
        session.poll()
        time.sleep(0.02)
    _print_rows(session, last_rows)  # the FINAL results End waited for
    summary = session.summary()
    audio = f"  audio sent {summary['audio_sent']} dropped {summary['audio_dropped']}" if mic else ""
    print(f"[session] ignored {summary['ignored']}  rejected {summary['rejected']}  "
          f"received {summary['received']}  sent {summary['sent']}{audio}", flush=True)
    session.log.close()
    return 0 if complete and not dropped else 1


def _print_rows(session: Session, last_rows):
    rows = event_rows(session.view)
    if rows != last_rows:
        print(f"[session] events: {len(rows)}", flush=True)
        for r in rows:
            print(f"           {r['event_id']}  {r['type']:<8} {r['phase']:<6} rev {r['revision']}", flush=True)
    return rows


def _done(source) -> bool:
    return source is None or source.done


if __name__ == "__main__":
    sys.exit(main())
