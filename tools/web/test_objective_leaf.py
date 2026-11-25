"""The Compare view's objective leaf: with `board: "develop"` in song mode the headline
is the team on the FIXED plans of ONE PlanCache seeded for the request, and the per-team
greedy re-develop stays on every row as the labelled what-if (`song_est_develop`,
`develop_whatif`, `board_state_develop`). Skips without the gitignored dumps.

    python -m pytest tools/web/test_objective_leaf.py
"""

from __future__ import annotations

import compare_view
import pytest
import server as web_server
from hbweb import config

CALLI_PDW = "card-04001-5-uniq-0042-00"
M1_ROSTER = [
    CALLI_PDW,
    CALLI_PDW,
    "card-00032-5-uniq-0026-00",
    "card-04003-5-uniq-0044-00",
    "card-00007-5-uniq-0008-00",
    "card-00006-5-uniq-0007-00",
]
# Marine "Crimson Captain": one of the leaders whose per-team re-develop beats the
# fixed plan (3,044,887 against 3,030,833, +0.46 %)
MARINE = "card-00023-5-uniq-0019-00"
MARINE_ROSTER = [
    MARINE,
    "card-00006-5-uniq-0007-00",
    "card-00007-5-uniq-0008-00",
    "card-00021-5-uniq-0064-00",
    "card-00022-5-uniq-0018-00",
    "card-00032-5-uniq-0026-00",
]


def _has_dumps() -> bool:
    return config.SKILLTREE_PATH.is_file() and config.NODE_MASTER_PATH.is_file()


def test_compare_develop_carries_objective_and_whatif_and_names_which_is_which():
    if not _has_dumps():
        pytest.skip("skilltree/master dumps absent (gitignored)")
    body = {
        "teams": [M1_ROSTER, MARINE_ROSTER],
        "basis": "maxed",
        "board": "develop",
        "budget": 453,
        "song": "m0334",
        "uptime_model": "finite",
        "platform": "mobile",
    }
    try:
        doc = compare_view.build_compare(body, art_dir=web_server.Handler.art_dir)
    except compare_view.CompareError as exc:
        if exc.code == 503:
            pytest.skip(exc.message)
        raise
    assert doc["objective"]["leaf"] == "plan_cache"
    assert doc["certificate_basis"]["leaf"] == "plan_cache"
    assert "objective_note" in doc["diff"] and "song_est_develop" in doc["diff"]
    a, b = doc["teams"]
    for t in (a, b):
        s = t["score"]
        assert s["headline"] == s["song_est"]
        assert isinstance(s["song_est_develop"], float)
        assert t["develop_whatif"]["song_est"] == round(s["song_est_develop"])
        assert t["board_plan_summary"]["leaf"] == "plan_cache"
        assert t["board_plan_summary_develop"]["leaf"] == "develop"
        assert t["board_state"] and t["board_state_develop"]
        assert t["certificate"]["basis"]["leaf"] == "plan_cache"
    # the two plans of one roster are near each other; which one is ahead moves with the
    # data (the develop what-if led on Marine's roster before R-064, the objective after)
    for t in (a, b):
        assert abs(t["score"]["song_est_develop"] / t["score"]["song_est"] - 1.0) < 0.01
    if doc["objective"]["seed_source"].startswith("per-leader table"):
        assert round(a["score"]["headline"]) == 3437830  # Calli PDW + incumbent
