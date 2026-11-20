"""Fixtures for the tests that run the real session server (R11)."""

from urllib.parse import urlsplit

import pytest

from live_harness import Relay


@pytest.fixture
def server():
    """The real session server with the loudness stand-in model."""
    from encore_server.models import LoudnessModel
    from encore_server.server import EncoreServer, serve_in_thread

    app = EncoreServer(LoudnessModel(), idle_s=0.3)
    url, stop = serve_in_thread(app)
    yield app, urlsplit(url).port
    stop()


@pytest.fixture
def relay(server):
    relay = Relay(server[1])
    yield relay
    relay.down()
    relay.cut()
