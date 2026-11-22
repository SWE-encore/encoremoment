"""Run the session server: python -m encore_server [--model yamnet|loudness].

From the repository root:

    PYTHONPATH=server python -m encore_server --model yamnet

It listens on 127.0.0.1 by default. Reach it from a PC through an SSH tunnel
(``ssh -L 8765:127.0.0.1:8765 USER@SERVER``) instead of opening a port.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from .fusion import TabRules


async def _serve(app, args):
    """Let container SIGTERM unwind the listener, connections and archive retry jobs."""
    loop = asyncio.get_running_loop()
    if sys.platform == "win32":
        await app.run(args.host, args.port, unix_socket=args.unix_socket)
        return
    task = asyncio.current_task()
    stopping = False

    def terminate():
        nonlocal stopping
        if not stopping:
            stopping = True
            task.cancel()

    loop.add_signal_handler(signal.SIGTERM, terminate)
    try:
        await app.run(args.host, args.port, unix_socket=args.unix_socket)
    except asyncio.CancelledError:
        if not stopping:
            raise
    finally:
        loop.remove_signal_handler(signal.SIGTERM)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m encore_server", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", choices=("yamnet", "loudness"), default="yamnet",
                    help="yamnet (needs TensorFlow) or loudness (a stand-in: any loud sound is laughter)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--yamnet-path", help="load a preverified YAMNet SavedModel directory offline")
    ap.add_argument("--record-history", action="store_true",
                    help="archive FINAL events in PostgreSQL using DATABASE_URL")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--unix-socket", help="listen at a local socket path for an SSH tunnel")
    ap.add_argument("--face-python", help="Python inside the MediaPipe environment")
    ap.add_argument("--face-task", help="Path to face_landmarker.task")
    ap.add_argument("--smile-threshold", type=float, default=TabRules.smile)
    ap.add_argument("--demo-quiet-ms", type=int, default=TabRules.quiet_ms,
                    help="time below the release thresholds before closing a shared-tab clip")
    args = ap.parse_args(argv)
    if args.yamnet_path and args.model != "yamnet":
        ap.error("--yamnet-path requires --model yamnet")
    if bool(args.face_python) != bool(args.face_task) or not 0 < args.smile_threshold <= 1:
        ap.error("supply both face options and a smile threshold in (0, 1]")
    if args.demo_quiet_ms <= 0:
        ap.error("--demo-quiet-ms must be positive")

    history = None
    if args.record_history:
        from .history import HistoryUnavailable, PostgresHistory
        dsn = os.environ.get("DATABASE_URL")
        if not dsn:
            ap.error("--record-history requires DATABASE_URL")
        try:
            history = PostgresHistory(dsn)
        except ImportError:
            ap.error("install server/requirements-history.txt to use --record-history")
        try:
            asyncio.run(history.check())
        except HistoryUnavailable:
            ap.error("could not access reaction_events; check DATABASE_URL, permissions and the schema")

    from .models import load_model
    from .server import EncoreServer

    def say(line: str) -> None:
        print(f"[server] {line}", flush=True)

    try:
        model = load_model(args.model, yamnet_path=args.yamnet_path)
    except ImportError as exc:
        ap.exit(2, f"cannot load {args.model}: {exc}\n"
                   "Install server/requirements-yamnet.txt, or use --model loudness to test without TensorFlow.\n")
    say(f"loaded {model.name}")
    from .vision import FaceWorker
    face = FaceWorker(args.face_python, args.face_task) if args.face_task else None
    try:
        if face is not None:
            say(f"rolling fusion: laughter >= {TabRules.strong_audio} or laughter >= {TabRules.assisted_audio} "
                f"with smile >= {args.smile_threshold:g} for {TabRules.equivalent_hops} equivalent hops in 5 s; quiet {args.demo_quiet_ms} ms")
        app = EncoreServer(model, say=say, face_model=face, history=history,
                           smile_threshold=args.smile_threshold, quiet_ms=args.demo_quiet_ms)
        asyncio.run(_serve(app, args))
    except KeyboardInterrupt:
        pass
    finally:
        if face is not None:
            face.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
