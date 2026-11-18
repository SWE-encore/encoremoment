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
