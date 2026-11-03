"""GET /api/meta -- the header strip's one line of provenance and capability.

`snapshot_mtime` (when the account was last read), `chart_count` (the library
the song tiles quote, never a hard-coded 181) and `solver_available` (whether
the exact HiGHS plan can run, so the button is disabled before the first click
instead of failing after it). REDESIGN-PLAN Phase A."""
from __future__ import annotations

import json
import socket
import threading
import urllib.request

import pytest

import server as web_server


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture(scope="module")
def web_base():
    port = _free_port()
    server = web_server.ThreadingServer((web_server.BIND_HOST, port), web_server.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://{web_server.BIND_HOST}:{port}"
    finally:
        server.shutdown()
        server.server_close()


def test_meta_carries_provenance_and_capability(web_base):
    with urllib.request.urlopen(web_base + "/api/meta", timeout=60) as response:
        body = json.loads(response.read())
    assert "error" not in body, body
    assert "snapshot_mtime" in body          # None when no snapshot on disk, never absent
    assert isinstance(body["chart_count"], int)
    assert isinstance(body["solver_available"], bool)


def test_meta_reports_power_up_under_upgraded_cards(web_base):
    """Cards "Upgraded": the sheet shows the Member Power-Up the search scores with
    (every owned card at max level), not only the account's own."""
    from hbuilder.cli import apply_collection, load_inputs

    cards, owned, _cfg, _prov = load_inputs(web_server.CARDS_PATH, web_server.OWNED_PATH, False)
    _c, basis = apply_collection(cards, str(web_server.COLLECTION_PATH), owned_ids=[c.id for c in owned],
                                 maxed=False, assume_level="max")
    want = basis.bonuses.power_up_permyriad
    with urllib.request.urlopen(web_base + "/api/meta?level=max&sp_training=4", timeout=120) as response:
        body = json.loads(response.read())
    assert "error" not in body, body
    assert body["assumed"]["power_up_permyriad"] == want
    assert body["assumed"]["power_up_permyriad"] >= body["power_up_permyriad"]


def test_meta_without_levers_has_no_assumed_figures(web_base):
    with urllib.request.urlopen(web_base + "/api/meta", timeout=60) as response:
        body = json.loads(response.read())
    assert body.get("assumed") is None


def test_meta_reports_the_maxed_account_bonuses(web_base):
    import ondemand

    want = ondemand.maxed_bonuses()
    with urllib.request.urlopen(web_base + "/api/meta?basis=maxed", timeout=120) as response:
        body = json.loads(response.read())
    assert body["assumed"] == {"power_up_permyriad": want.power_up_permyriad,
                               "memory_permil": want.memory_permil}
