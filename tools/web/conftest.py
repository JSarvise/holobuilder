"""Fixtures the web tests share: the real web server on a free loopback port."""

from __future__ import annotations

import contextlib
import socket
import threading
from collections.abc import Iterator

import pytest


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@contextlib.contextmanager
def _serving() -> Iterator[str]:
    # imported here, not at the top: only the tests that start a server pay for loading the app
    import server as web_server  # noqa: PLC0415

    port = _free_port()
    srv = web_server.ThreadingServer((web_server.BIND_HOST, port), web_server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://{web_server.BIND_HOST}:{port}"
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture(scope="session")
def serve_web():
    """`with serve_web() as base:` runs the web server for the block and yields its base URL,
    for a fixture that patches something first or needs its own scope."""
    return _serving


@pytest.fixture(scope="module")
def web_base():
    """The web server for the whole test module; yields its base URL."""
    with _serving() as base:
        yield base
