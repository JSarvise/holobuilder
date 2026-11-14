"""The local server must not refuse a page's connections while it computes.

A page opens ~10 connections at once (its stylesheets and scripts). With Python's default listen
backlog of 5, Windows REFUSES the connections past it whenever the accept loop is slow -- and it
is slow while the Optimizer's requests (row detail, board, compare) run pure Python in other
threads holding the GIL. A refused <script> fails for good: the Collection page stayed on
"Loading the catalogue..." with no rail, and nothing on it could be clicked.
"""

from __future__ import annotations

import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hbweb import router  # noqa: E402


def test_the_listen_backlog_holds_a_page_burst():
    assert router.ThreadingServer.request_queue_size >= 64


class _Tiny(router.http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


@pytest.mark.slow  # ~30 s: the handler threads wait on the GIL behind the busy ones; -m slow
def test_a_burst_of_connections_is_served_while_threads_compute():
    """The failure itself, reproduced: with the default backlog 10-20 % of these are refused."""
    srv = router.ThreadingServer(("127.0.0.1", 0), _Tiny)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    stop = threading.Event()

    def burn():  # stands in for a row-detail request: pure Python, holding the GIL
        x = 0
        while not stop.is_set():
            for i in range(10_000):
                x += i * i

    for _ in range(6):
        threading.Thread(target=burn, daemon=True).start()
    time.sleep(0.2)

    def get(_):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
                s.sendall(b"GET / HTTP/1.0\r\nHost: x\r\n\r\n")
                return s.recv(64)[:12] == b"HTTP/1.0 200"
        except OSError:
            return False

    try:
        with ThreadPoolExecutor(20) as ex:
            served = [ok for _ in range(3) for ok in ex.map(get, range(20))]
    finally:
        stop.set()
        srv.shutdown()
        srv.server_close()
    assert all(served), f"{served.count(False)} of {len(served)} connections refused or failed"
