"""Fixtures the web tests share: the real web server on a free loopback port."""

from __future__ import annotations

import contextlib
import socket
import threading
import urllib.request
from collections.abc import Iterator

import pytest


class _SessionKey(urllib.request.BaseHandler):
    """The tests' requests to the in-process server stand for its own pages' requests: every
    non-GET one to 127.0.0.1 carries the session key a page would (hbweb/session.py). The
    tests of that guard itself send their requests by hand (`test_local_security.py`)."""

    def http_request(self, req: urllib.request.Request) -> urllib.request.Request:
        if req.get_method() not in ("GET", "HEAD") and req.host.split(":")[0] == "127.0.0.1":
            from hbweb import session  # noqa: PLC0415

            req.add_unredirected_header(session.HEADER, session.TOKEN)
        return req


urllib.request.install_opener(urllib.request.build_opener(_SessionKey()))


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
