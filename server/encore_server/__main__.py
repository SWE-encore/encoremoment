"""Run the session server: python -m encore_server [--model yamnet|loudness].

From the repository root:

    PYTHONPATH=server python -m encore_server --model yamnet

It listens on 127.0.0.1 by default. Reach it from a PC through an SSH tunnel
(``ssh -L 8765:127.0.0.1:8765 USER@SERVER``) instead of opening a port.
"""

from __future__ import annotations

import argparse
import asyncio
import sys


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m encore_server", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", choices=("yamnet", "loudness"), default="yamnet",
                    help="yamnet (needs TensorFlow) or loudness (a stand-in: any loud sound is laughter)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args(argv)

    from .models import load_model
    from .server import EncoreServer

    def say(line: str) -> None:
        print(f"[server] {line}", flush=True)

    try:
        model = load_model(args.model)
    except ImportError as exc:
        ap.exit(2, f"cannot load {args.model}: {exc}\n"
                   "Install server/requirements-yamnet.txt, or use --model loudness to test without TensorFlow.\n")
    say(f"model {model.name}; listening on ws://{args.host}:{args.port}/v1/sessions/<session_id>/stream")
    try:
        asyncio.run(EncoreServer(model, say=say).run(args.host, args.port))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
