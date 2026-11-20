"""Live microphone sessions in the main app without the GStreamer player.

``python -m encore_client --no-player --mic --local-server loudness`` captures the
PC microphone, analyses it in a session server started inside the app (the same
one the test bench uses), and shows the events in the normal timeline and
Highlights. It exists so the reaction path can be shown on machines without the
Ubuntu player (for example macOS); it is not the product's capture path (R1).

Without a player there is no content position. A *microphone clock* anchor is
sent at Start instead: content ``mic-clock``, position 0, playing at 1x. Event
times in the timeline are therefore seconds since Start, and the title says so,
so they cannot be mistaken for positions in a film.
"""

from __future__ import annotations

from typing import Any

MIC_CLOCK_CONTENT_ID = "mic-clock"
MIC_CLOCK_TITLE = "Microphone test - time since Start (no content player)"


def mic_clock_anchor(capture_ms: int) -> dict[str, Any]:
    """A playback anchor that maps capture time to 'time since Start'."""
    return {
        "schema": "encore.v1",
        "kind": "playback_anchor",
        "capture_ms": int(capture_ms),
        "playback": {
            "epoch": 1,
            "content_id": MIC_CLOCK_CONTENT_ID,
            "title": MIC_CLOCK_TITLE,
            "position_ms": 0,
            "rate": 1.0,
            "state": "playing",
        },
    }


def send_mic_clock_on_start(session, clock) -> None:
    """Make ``session.start()`` first set the microphone-clock anchor.

    The session sends its newest anchor as soon as the connection opens, so
    the server maps every event of this run to time since Start.
    """
    original_start = session.start

    def start() -> None:
        session.send_anchor(mic_clock_anchor(clock()))
        original_start()

    session.start = start


def parse_triggers(items: list[str] | None) -> dict[str, float]:
    """``["laughter=0.6", "gasp=0.25"]`` -> {"laughter": 0.6, "gasp": 0.25}."""
    out: dict[str, float] = {}
    for item in items or []:
        name, sep, value = item.partition("=")
        if not sep or name not in ("laughter", "scream", "gasp"):
            raise ValueError(f"--trigger {item!r}: use laughter=, scream= or gasp= with a level 0..1")
        level = float(value)
        if not 0.0 < level <= 1.0:
            raise ValueError(f"--trigger {item!r}: the level must be in (0, 1]")
        out[name] = level
    return out


def start_local_server(model_name: str, overrides: dict[str, float]):
    """Start the bench's built-in session server; returns it (``.url``, ``.stop()``)."""
    from .bench.local import LocalServer, default_triggers, import_server

    import_server()
    from encore_server.models import load_model

    triggers = default_triggers() | overrides
    return LocalServer(load_model(model_name), triggers)
