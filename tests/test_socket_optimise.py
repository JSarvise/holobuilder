"""The socket optimiser (`tools/recon/proxy/socket_optimise.py`) picks the best
card per Connect socket for global account score.

The greedy assignment and the state helpers are pinned hermetically (they carry
the account-wide one-card-per-socket rule, which is the load-bearing constraint);
the full oracle-scored optimise() is exercised by hand -- it is a ~15s pool
re-score sweep and does not belong in the unit suite.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_PROXY = _REPO / "tools" / "recon" / "proxy"


def _load():
    spec = importlib.util.spec_from_file_location("socket_optimise", _PROXY / "socket_optimise.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["socket_optimise"] = module
    spec.loader.exec_module(module)
    return module


def test_greedy_assign_is_highest_first_one_card_one_socket():
    so = _load()
    triples = [
        (100.0, "chr-A", "S-001", "card-X"),
        (90.0, "chr-B", "S-002", "card-X"),   # card-X already spent account-wide -> skip
        (80.0, "chr-A", "S-001", "card-Y"),    # chr-A/S-001 already filled -> skip
        (70.0, "chr-B", "S-002", "card-Z"),    # both free -> take
    ]
    assignment = so._greedy_assign(triples)
    assert assignment == {("chr-A", "S-001"): "card-X", ("chr-B", "S-002"): "card-Z"}
    # a card is never placed twice, a socket never double-filled
    assert len(set(assignment.values())) == len(assignment)
    assert len(set(assignment)) == len(assignment)


def test_greedy_prefers_the_higher_value_placement_of_a_contended_card():
    so = _load()
    # card-X wanted by two sockets; the 100 placement must win, the 90 is dropped
    assignment = so._greedy_assign([
        (90.0, "chr-B", "S-001", "card-X"),
        (100.0, "chr-A", "S-001", "card-X"),
    ])
    assert assignment == {("chr-A", "S-001"): "card-X"}


def test_empty_clears_slots_without_mutating_the_input():
    so = _load()
    board = {"chr-A": {"nodes": ["S-001"], "slots": {"S-001": "card-old"}}}
    empty = so._empty(board)
    assert empty["chr-A"]["slots"] == {}
    assert board["chr-A"]["slots"] == {"S-001": "card-old"}, "input must not be mutated"


def test_with_slots_places_without_mutating_the_base():
    so = _load()
    base = {"chr-A": {"nodes": ["S-001"], "slots": {}}}
    placed = so._with_slots(base, {("chr-A", "S-001"): "card-new"})
    assert placed["chr-A"]["slots"] == {"S-001": "card-new"}
    assert base["chr-A"]["slots"] == {}, "base must not be mutated"


def test_owned_connect_cards_are_a_subset_of_the_catalogue_connect_cards():
    so = _load()
    skilltree = _PROXY / "dumps" / "skilltree.json"
    collection = _REPO / "state" / "collection.json"
    if not skilltree.exists() or not collection.exists():
        pytest.skip("dumps/skilltree.json or state/collection.json unavailable")
    import json

    catalogue = set(json.loads(skilltree.read_text("utf-8")).get("card_connect", {}))
    owned = so._owned_connect_cards()
    assert owned, "the account owns no connect-capable cards?"
    assert owned <= catalogue, "an owned connect card is not in card_connect"
