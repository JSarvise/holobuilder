"""`board_reconcile.build_cards` must score the levels it was HANDED, never a
stale `live_collection.json` left in the OS temp dir by an earlier process.

`LIVE_COLLECTION` survives across processes. If `build_cards(live_levels)`
read it instead of its parameter, an account refresh that moved a card's level
(say 1 -> 70) would leave `resolve_live_levels` correct and `build_cards`
scoring the stale doc — the card at its LEVEL-1 stats, which looks like a
curve mis-scaling that does not exist. The desync is general — any card of any
rarity whose level moved since the temp doc was last written — so the
contract, not the card, is what these tests pin:

* non-empty `live_levels` is AUTHORITATIVE — `build_cards` (re)writes the doc
  from it, so the pair can never desync;
* empty `live_levels` means "the doc `write_live_collection` wrote EARLIER IN
  THIS PROCESS" (the frozen-basis callers) — a doc merely found on disk is
  refused, loudly.

The catalogue (`data/cards.json`) is deliberately never opened here — the
fixture-isolation rule — membership is checked against `build_cards`' own
`by_id` after the call.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_PROXY = _REPO / "tools" / "recon" / "proxy"
_CURVE = _PROXY / "dumps" / "card_level_curve.json"


def _two_level_candidates():
    """(card_id, high_level) pairs whose group defines two distinct
    multipliers, so a stale-vs-fresh level is visible in the stats — plus the
    `StatBasis` to compute the expected numbers from."""
    from hbuilder.domain.collection import stat_basis_from_mapping

    raw = json.loads(_CURVE.read_text("utf8"))
    basis = stat_basis_from_mapping(raw)
    picks = []
    for cid, meta in raw["cards"].items():
        levels = raw["level_mult"].get(meta["level_group"]) or {}
        if "1" not in levels or len(levels) < 2:
            continue
        hi = max(int(k) for k in levels)
        if levels[str(hi)] == levels["1"]:
            continue
        if basis.stats_at(cid, 1, 0) and basis.stats_at(cid, hi, 0):
            picks.append((cid, hi))
    if not picks:
        pytest.skip("no curve card with a two-level group")
    return picks, basis


@pytest.mark.skipif(
    not _CURVE.exists(),
    reason="card_level_curve.json is a gitignored account dump",
)
def test_build_cards_scores_the_levels_it_was_handed_not_a_stale_doc():
    """The stale-doc shape: the temp doc says level 1, the caller says
    the high level. Every card must come out at the CALLER's level."""
    sys.path.insert(0, str(_PROXY))
    import board_reconcile as BR

    picks, basis = _two_level_candidates()
    stale = {cid: {"level": 1, "bloom": 0, "limit_break": "max"} for cid, _ in picks}
    fresh = {cid: {"level": hi, "bloom": 0, "limit_break": "max"} for cid, hi in picks}

    BR.write_live_collection(stale)  # what an earlier run left behind
    by_id, _cfg, applied = BR.build_cards(fresh)
    assert applied >= 1

    checked = 0
    for cid, hi in picks:
        card = by_id.get(cid)
        if card is None:  # curve card not in the catalogue — not this contract
            continue
        checked += 1
        got = (card.stats.performance, card.stats.technique, card.stats.sense)
        assert got == basis.stats_at(cid, hi, 0), (
            f"{cid} scored at the stale doc's level 1 "
            f"(stats {basis.stats_at(cid, 1, 0)}), not the handed level {hi}"
        )
    assert checked >= 1, "no curve card found in the catalogue"


@pytest.mark.skipif(
    not _CURVE.exists(),
    reason="card_level_curve.json is a gitignored account dump",
)
def test_build_cards_refuses_a_doc_from_another_process():
    """`build_cards({})` is the frozen-basis form and is only honest when the
    doc was written in THIS process. Simulate a fresh process finding a stale
    doc on disk: the call must refuse rather than silently score it."""
    sys.path.insert(0, str(_PROXY))
    import board_reconcile as BR

    picks, _basis = _two_level_candidates()
    cid, _hi = picks[0]
    BR.write_live_collection({cid: {"level": 1, "bloom": 0, "limit_break": "max"}})
    BR._LIVE_COLLECTION_IN_PROCESS = False  # what a new process would see
    try:
        with pytest.raises(SystemExit, match="write_live_collection"):
            BR.build_cards({})
    finally:
        BR._LIVE_COLLECTION_IN_PROCESS = True
