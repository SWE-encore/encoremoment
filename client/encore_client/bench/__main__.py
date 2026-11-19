"""python -m encore_client.bench: test reaction detection with a microphone or an audio file.

From client/ in a repository checkout:

    ../.venv/bin/python -m encore_client.bench
    ../.venv/bin/python -m encore_client.bench --model yamnet --file laugh.wav --speed 4

The built-in server needs nothing else running. --server connects to a
session server you started yourself (python -m encore_server) instead.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    from .local import MODELS

    ap = argparse.ArgumentParser(prog="python -m encore_client.bench", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", choices=tuple(MODELS), default="loudness",
                    help="model for the built-in server (yamnet needs server/requirements-yamnet.txt)")
    ap.add_argument("--server", metavar="URL", help="use this session server instead of the built-in one")
    ap.add_argument("--file", type=Path, help="start with this audio file selected")
    ap.add_argument("--speed", type=int, choices=(1, 4, 16), default=1, help="playback speed for a file")
    args = ap.parse_args(argv)

    from PySide6.QtWidgets import QApplication

    from .window import BenchWindow

    app = QApplication.instance() or QApplication(sys.argv[:1])
    win = BenchWindow(model=args.model, server_url=args.server, audio_file=args.file, speed=args.speed)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
