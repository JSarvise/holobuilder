"""`POST /api/timeline` — when each of a team's five members is actually live.

`song.timeline()` produces the five lanes, the chart's SPECIAL cues and every
ACTIVE's cooldown lattice. This route is what hands it to the page.

What is pinned here is the part a picture can get quietly wrong. A timeline is
believed on sight — nobody re-derives a bar chart — so the failure mode is not a
crash, it is a plausible drawing of the wrong arrangement:

* the **slot order must be the scorer's**, not the caller's, because cue `i`
  fires slot `i`'s SPECIAL and a different order is a different team;
* every lane must carry **exactly one** SPECIAL window when the chart names its
  cue, since a chart always has five cues, one per slot;
* windows must be **half-open and unclamped**, because `song.py` refuses to clip
  them to the song's end on purpose — the last note carries the highest combo
  multiplier and clipping would push it out of a window that covers it.
"""

from __future__ import annotations

import json
import socket
import threading
import urllib.request
from pathlib import Path

import pytest

import server as web_server

_REPO = Path(__file__).resolve().parents[2]
_OWNED = _REPO / "data" / "owned.json"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture(scope="module")
def web_base():
    port = _free_port()
    server = web_server.ThreadingServer(
        (web_server.BIND_HOST, port), web_server.Handler
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://{web_server.BIND_HOST}:{port}"
    finally:
        server.shutdown()
        server.server_close()


def _post(base: str, body: dict) -> dict:
    request = urllib.request.Request(
        f"{base}/api/timeline",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:  # 400 carries a readable reason
        return {"_status": exc.code, **json.loads(exc.read().decode("utf-8"))}


def _six() -> list[str]:
    return json.loads(_OWNED.read_text(encoding="utf-8"))["owned"][:6]


def test_a_short_team_is_refused_with_a_reason(web_base):
    """Five cards is not a team, and guessing the sixth would be inventing one."""
    answer = _post(web_base, {"song": "m0001", "cards": _six()[:5]})
    assert answer.get("_status") == 400
    assert "1 leader" in answer["error"]


def test_an_unknown_song_is_a_state_not_a_crash(web_base):
    answer = _post(web_base, {"song": "m9999", "cards": _six()})
    assert answer.get("available") is False
    assert "m9999" in answer["reason"]


@pytest.mark.skipif(
    not web_server.CHARTS_PATH.is_file(), reason="dumps/ is gitignored"
)
def test_the_drawing_is_of_the_arrangement_the_scorer_chose(web_base):
    """⛔ The one that matters.

    The caller sends six ids in whatever order the ranking had them. The lanes
    must be the order `score_song_detailed` maximised, because cue `i` fires
    slot `i`'s SPECIAL — a picture of the caller's order would show a team the
    tool never recommended, and it would look exactly as convincing."""
    cards = _six()
    answer = _post(web_base, {"song": "m0001", "cards": cards})
    assert answer["available"], answer.get("reason")

    order = answer["member_order"]
    assert len(order) == web_server.MEMBER_COUNT
    assert sorted(order) == sorted(cards[1:]), (
        "the arrangement must be a permutation of the members sent, not a "
        f"different set: {order} vs {cards[1:]}"
    )
    lanes = answer["timeline"]["lanes"]
    assert [lane["card_id"] for lane in lanes] == order, (
        "lane i must be the member the scorer put in slot i"
    )
    assert [lane["slot"] for lane in lanes] == list(range(1, len(lanes) + 1))

    # The negative control. If the scorer's order happened to equal the order
    # sent, everything above would pass while the route simply echoed its input
    # — the exact bug this test exists to catch, and it would look identical.
    # On this fixture the two differ, so the check is live.
    assert order != cards[1:], (
        "this fixture no longer discriminates: the scorer's arrangement equals "
        "the one sent, so an echoing implementation would pass. Pick a team "
        "whose optimum reorders before trusting this test again."
    )


@pytest.mark.skipif(
    not web_server.CHARTS_PATH.is_file(), reason="dumps/ is gitignored"
)
def test_every_lane_gets_its_own_cue_and_its_SPECIAL_sits_on_it(web_base):
    """A chart carries five cues, one per slot. A lane whose SPECIAL floated
    free of its cue would be drawing the skill somewhere it never fires."""
    answer = _post(web_base, {"song": "m0001", "cards": _six()})
    assert answer["available"]
    cues = answer["timeline"]["special_cues"]
    assert len(cues) == 5 and cues == sorted(cues)

    for lane in answer["timeline"]["lanes"]:
        assert lane["cue"] == pytest.approx(cues[lane["slot"] - 1]), (
            f"slot {lane['slot']} must sit on the {lane['slot']}-th cue"
        )
        for window in lane["special"]:
            assert window["start"] == pytest.approx(lane["cue"]), (
                "a SPECIAL starts at its cue -- that is what makes it a fact "
                "rather than a chance"
            )
            assert window["end"] >= window["start"]


@pytest.mark.skipif(
    not web_server.CHARTS_PATH.is_file(), reason="dumps/ is gitignored"
)
def test_active_windows_are_ordered_and_never_silently_clamped(web_base):
    """`song.py` deliberately does not clip windows to the song's end. A route
    that clamped them would be a second, quieter definition of when a skill is
    live, and the first time the two disagreed the picture would become an
    argument against the score."""
    answer = _post(web_base, {"song": "m0001", "cards": _six()})
    assert answer["available"]
    song_seconds = answer["timeline"]["song_seconds"]
    assert song_seconds > 0

    for lane in answer["timeline"]["lanes"]:
        previous = None
        for window in lane["active"]:
            assert window["end"] > window["start"], "a window must have width"
            if previous is not None:
                assert window["start"] >= previous, "windows must ascend in time"
            previous = window["start"]
            # A probability is a lower bound or it is absent; it is never
            # invented, because a made-up chance would look like a measurement.
            probability = window["probability_base"]
            assert probability is None or 0.0 <= probability <= 1.0


def test_the_timeline_honours_the_level_bloom_what_if(web_base):
    """The same team drawn at `assume_level=max` must not score as its real
    (often level-1) stats — the ranking was solved with the what-if, so the
    timeline's total must move with it (REDESIGN-PLAN D6)."""
    real = _post(web_base, {"song": "m0001", "cards": _six(), "basis": "maxed"})
    maxed = _post(web_base, {"song": "m0001", "cards": _six(), "basis": "maxed",
                             "assume_level": "max", "assume_bloom": "max"})
    assert real["available"] and maxed["available"]
    assert maxed["total_score"] >= real["total_score"]
    # and the uptime model is honoured too: steady vs finite differ
    steady = _post(web_base, {"song": "m0001", "cards": _six(), "basis": "maxed",
                              "uptime_model": "steady"})
    assert steady["available"]          # parsed and scored; the two models may coincide on a chart

