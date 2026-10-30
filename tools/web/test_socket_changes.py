"""What the board plan shows about sockets, so a changed socket is visible: the socket
map the plan leaves the account with
(`server._settle_slots`) and each socket's change against the account's card
(`server._socket_changes`).

    python -m pytest tools/web/test_socket_changes.py
"""
from __future__ import annotations

import server as web_server


def test_a_card_the_plan_places_leaves_the_board_that_kept_it():
    state = {"chr-1": {"nodes": [], "slots": {"S-001": "card-x"}},          # the team: re-chosen
             "chr-9": {"nodes": [], "slots": {"S-003": "card-x", "S-001": "card-y"}}}   # bench: kept
    captured = {"chr-1": {"slots": {"S-001": "card-z"}},
                "chr-9": {"slots": {"S-003": "card-x", "S-001": "card-y"}}}
    web_server._settle_slots(state, captured, {"chr-1"})
    assert state["chr-9"]["slots"] == {"S-001": "card-y"}                # one card, one socket
    # card-z came off the team socket the plan filled with card-x: a real change, kept
    assert state["chr-1"]["slots"] == {"S-001": "card-x"}


def test_a_socket_left_empty_keeps_its_card_when_the_plan_uses_it_nowhere():
    """The matching only places a card where it adds score: an empty socket there means
    nothing to gain, not "take the card out" -- no move is shown for it."""
    state = {"chr-1": {"nodes": ["S-002"], "slots": {"S-001": "card-x"}}}
    captured = {"chr-1": {"slots": {"S-001": "card-x", "S-002": "card-w"}},
                "chr-2": {"slots": {"S-001": "card-v"}}}
    web_server._settle_slots(state, captured, {"chr-1"})
    assert state["chr-1"]["slots"] == {"S-001": "card-x", "S-002": "card-w"}
    # ... but not when the plan put that card on another board
    state = {"chr-1": {"nodes": [], "slots": {"S-001": "card-x"}},
             "chr-2": {"nodes": [], "slots": {"S-001": "card-w"}}}
    web_server._settle_slots(state, captured, {"chr-1", "chr-2"})
    assert state["chr-1"]["slots"] == {"S-001": "card-x"}
    assert state["chr-2"]["slots"] == {"S-001": "card-w"}


def test_each_socket_is_marked_with_what_the_plan_does_and_counted():
    doc = {"characters": [{"chr": "chr-1", "plan": {}, "sockets": [
        {"socket": "S-001", "placed_card": "a", "recommended_card": "a"},
        {"socket": "S-002", "placed_card": None, "recommended_card": "b"},
        {"socket": "S-003", "placed_card": "c", "recommended_card": "d"},
        {"socket": "S-004", "placed_card": "e"}]}]}
    web_server._socket_changes(doc)
    socks = {s["socket"]: s.get("change") for s in doc["characters"][0]["sockets"]}
    assert socks == {"S-001": None, "S-002": "add", "S-003": "swap", "S-004": "remove"}
    assert doc["characters"][0]["plan"]["socket_changes"] == 3


def test_an_equivalent_card_already_in_the_socket_is_not_shown_as_a_swap():
    """The matching names a placement by the lowest id of an interchangeable group (same
    shape, same permil): a socket already holding an equivalent card keeps it, and the
    plan's id goes where the account's card was planned -- same multipliers, no move."""
    group = {"card-a": ("L", 1500), "card-b": ("L", 1500), "card-c": ("T", 900)}
    state = {"chr-1": {"nodes": [], "slots": {"S-001": "card-a", "S-002": "card-c"}},
             "chr-2": {"nodes": [], "slots": {"S-001": "card-b"}}}
    captured = {"chr-1": {"slots": {"S-001": "card-b"}}, "chr-2": {"slots": {"S-001": "card-c"}}}
    web_server._settle_slots(state, captured, {"chr-1", "chr-2"}, group)
    assert state["chr-1"]["slots"] == {"S-001": "card-b", "S-002": "card-c"}   # b stays, no swap
    assert state["chr-2"]["slots"] == {"S-001": "card-a"}                      # c != a: a real swap


def test_a_socket_the_plan_resets_does_not_keep_its_card():
    """When the plan resets a character's S-004 on a song she does not sing, the board
    must not still show her card there -- a socket the plan does not own holds nothing."""
    state = {"chr-1": {"nodes": ["B-001"], "slots": {"S-001": "card-x"}}}
    captured = {"chr-1": {"slots": {"S-001": "card-x", "S-004": "card-w"}}}
    web_server._settle_slots(state, captured, {"chr-1"})
    assert state["chr-1"]["slots"] == {"S-001": "card-x"}
