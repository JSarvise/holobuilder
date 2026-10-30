"""The three collection scenarios of BOARD-EXPORT-CONTRACT.md, as the Oracle
builds them.

`tree_optimise.Oracle(scenario=...)` is two axes at once — the scoring BASIS
(real account levels vs the fully-maxed `data/cards.json` values) and the POOL
(the cards the account owns vs the whole catalogue):

    account               real levels   owned cards
    my-cards-maxed         maxed         owned cards
    full-collection-maxed  maxed         every card in the game

Getting either axis wrong is silent — a plan costed on the wrong basis ranks
different nodes, and a pool that is secretly the whole catalogue answers a
question about an account nobody has. These pin both axes per scenario without
scoring (construction only), so the suite stays fast.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_TREE_OPTIMISE = _REPO / "tools" / "recon" / "proxy" / "tree_optimise.py"


def _load_tree_optimise():
    spec = importlib.util.spec_from_file_location("tree_optimise", _TREE_OPTIMISE)
    module = importlib.util.module_from_spec(spec)
    sys.modules["tree_optimise"] = module
    spec.loader.exec_module(module)
    return module


try:
    _TREE = _load_tree_optimise()
except SystemExit as exc:  # gitignored account data missing
    _TREE = None
    _WHY = str(exc)
else:
    _WHY = ""

pytestmark = pytest.mark.skipif(
    _TREE is None, reason=f"tree_optimise needs gitignored account data: {_WHY}"
)


def _oracle(scenario):
    # pool_size is irrelevant to construction (priming is lazy), so the smallest
    # legal value keeps these cheap.
    return _TREE.Oracle(pool_size=1, quiet=True, scenario=scenario)


def test_account_scores_real_levels_over_the_owned_pool():
    oracle = _oracle("account")
    assert oracle.scenario == "account"
    assert oracle.maxed is False, "account must restate cards to their real levels"
    # the owned pool is a strict subset of the catalogue
    assert 0 < len(oracle.pool) < len(oracle._by_id)


def test_my_cards_maxed_is_the_owned_pool_at_the_maxed_basis():
    account = _oracle("account")
    maxed = _oracle("my-cards-maxed")
    assert maxed.scenario == "my-cards-maxed"
    assert maxed.maxed is True, "my-cards-maxed must NOT restate down to real levels"
    # SAME pool as account (owned cards), only the basis changes.
    assert len(maxed.pool) == len(account.pool), (
        "my-cards-maxed and account differ only in basis, not in which cards are "
        f"in the pool; got {len(maxed.pool)} vs {len(account.pool)}"
    )


def test_full_collection_maxed_searches_the_whole_catalogue():
    owned = _oracle("my-cards-maxed")
    full = _oracle("full-collection-maxed")
    assert full.scenario == "full-collection-maxed"
    assert full.maxed is True
    # the pool is EVERY card in the catalogue, strictly more than the owned one.
    assert len(full.pool) == len(full._by_id), (
        "full-collection-maxed must pool the whole catalogue, not just owned"
    )
    assert len(full.pool) > len(owned.pool)


def test_the_default_scenario_is_account():
    assert _oracle(None).scenario == "account"
    assert _TREE.Oracle(pool_size=1, quiet=True).scenario == "account"


def test_an_unknown_scenario_is_refused_not_guessed():
    with pytest.raises(SystemExit):
        _oracle("everything-maxed")


# --- the character-level (board-point) assumption per scenario ----------------
# `resolve_at_max_level` is pure, so these need no account data and run even on a
# checkout where the Oracle cannot be built.


def test_account_defaults_to_real_levels_and_toggles_to_max():
    r = _TREE.resolve_at_max_level
    assert r("account", False, False) is False
    assert r("account", True, False) is True  # --at-max-level lifts it


def test_my_cards_maxed_defaults_to_max_and_toggles_to_real():
    r = _TREE.resolve_at_max_level
    assert r("my-cards-maxed", False, False) is True
    assert r("my-cards-maxed", False, True) is False  # --real-char-level drops it


def test_full_collection_maxed_is_forced_to_level_50():
    r = _TREE.resolve_at_max_level
    assert r("full-collection-maxed", False, False) is True
    with pytest.raises(SystemExit):
        r("full-collection-maxed", False, True)  # cannot drop a complete account


def test_the_two_toggles_cannot_both_be_set():
    with pytest.raises(SystemExit):
        _TREE.resolve_at_max_level("account", True, True)


# --- respec starts bare from S-001 ONLY (the disconnected-board bug) -----------


@pytest.mark.skipif(
    not (_TREE and (_TREE.DUMPS / "board_state.json").exists()),
    reason="respec_budget needs the board/price dumps",
)
def test_respec_starts_from_the_free_root_only_not_every_socket():
    """S-002/003/004 cost a point AND a path — starting them owned let the beam
    ground a branch on an unconnected socket and dump a DISCONNECTED board
    (measured: 1 of 17 nodes reachable). Respec must start every character from
    S-001 alone."""
    prices = _TREE._prices()
    owned = _TREE._load(_TREE.DUMPS / "board_state.json")
    balance = {item: 0 for item in _TREE.CURRENCIES}

    _wallet, _points, bare, _levels = _TREE.respec_budget(owned, prices, balance)

    assert bare, "respec produced no starting boards"
    for chr_id, nodes in bare.items():
        assert nodes == [_TREE.ROOT], (
            f"{chr_id} respec board must start as exactly [{_TREE.ROOT}] (the free "
            f"root); got {nodes} — a non-root socket here is the disconnected-board bug"
        )
