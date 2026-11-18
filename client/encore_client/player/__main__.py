"""Run the controlled player: python -m encore_client.player [video] [options]."""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m encore_client.player", description=__doc__)
    ap.add_argument("video", nargs="?", help="local video file to open")
    ap.add_argument("--title", help="content title to report (default: file name)")
    ap.add_argument("--session-id", default="ses-local-player", help="session_id put on anchors")
    ap.add_argument("--anchors-out", metavar="FILE", help="append playback anchors to FILE as JSON Lines")
    ap.add_argument("--audio-sink", default="autoaudiosink", help="GStreamer audio sink description")
    ap.add_argument("--autoplay", action="store_true", help="start playing after opening")
    args = ap.parse_args(argv)

    from PySide6.QtWidgets import QApplication

    from .window import PlayerWindow, jsonl_writer

    app = QApplication(sys.argv[:1])
    on_anchor = jsonl_writer(args.anchors_out) if args.anchors_out else None
    win = PlayerWindow(args.session_id, args.audio_sink, on_anchor)
    if args.video:
        win.open_file(args.video, args.title)
        if args.autoplay:
            win.player.play()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
