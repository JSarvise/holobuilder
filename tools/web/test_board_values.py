"""`board_values` — node values and Connect boosts served by `/api/board-doc`.

Synthetic master rows pin the arithmetic (no dumps needed); one check runs on the
real `skilltree.json` when the gitignored dump is present.

    python -m pytest tools/web/test_board_values.py
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import board_values as bv

_SKILLTREE = {
    "nodes": {
        "R-001": {"br": "R", "e": {"t": "All", "v": 50, "d": "Grants All Stats UP [value] to All."}},
        "R-002": {"br": "R", "e": {"t": "Act.Prob", "v": 40,
                                   "d": "Grants Active Skill Activation Rate UP [value/10]% to [character]."}},
        "R-003": {"br": "R", "e": {"t": "All", "v": 50, "d": "Grants All Stats UP [value] to All."},
                  "chr": {"chr-1": {"t": "All", "v": 70, "d": "Grants All Stats UP [value] to All."}}},
        "Y-001": {"br": "Y", "e": {"t": "skill_tree_effect-live_score_bonus_add_permil_up_by_music_x-5-"
                                        "skill_tree_effect_passive_trigger-music-solo-always-solo",
                                   "v": 0, "d": ""}},
        "L-001": {"br": "R", "e": {"t": "x", "v": 2, "d": "Enhance a Holomem Skill."}},
    },
    "models": {"m1": {"S-001": [0, 0], "R-001": [0, -1], "R-002": [0, -2], "R-003": [1, 0],
                      "Y-001": [5, 5], "L-001": [0, -3]}},
    "extents": {"line_up": [[0, -1], [0, -2]]},
    "connect": {"eff-a": {"area": "line_up", "rare": "rare_5",
                          "levels": [{"v": 550, "d": "Grants Holomem Board Effect UP 155% for all within range."},
                                     {"v": 1050, "d": "UP [highlight]205[/highlight]%"}]}},
    "card_connect": {"card-00001-5-uniq-0001-00": "eff-a", "card-00002-5-uniq-0002-00": "eff-a"},
}


def _doc(placed):
    return {"characters": [{
        "chr": "chr-1", "model": "m1",
        "nodes": [{"id": n, "pos": p} for n, p in _SKILLTREE["models"]["m1"].items() if not n.startswith("S-")],
        "sockets": [{"socket": "S-001", "pos": [0, 0], "placed_card": placed}],
    }]}


def _node(doc, nid):
    return next(n for n in doc["characters"][0]["nodes"] if n["id"] == nid)


def test_boost_is_ceiling_of_the_summed_multiplier_in_exact_integers():
    assert bv.boosted(50, 550) == 78        # 77.5 -> 78, the game's ceiling (board.py)
    assert bv.boosted(40, 550) == 62        # whole in the game: never ceils up to 63
    assert bv.boosted(50, 0) == 50
    assert bv.boosted(10, 1400) == 24       # R-002 reads 24.0 % at x2.4


def test_values_are_the_masters_and_print_like_the_game():
    collection = {"cards": {"card-00001-5-uniq-0001-00": {"bloom": 2}}}
    doc = bv.annotate(_doc("card-00001-5-uniq-0001-00"), _SKILLTREE, collection)
    r1, r2, r3 = _node(doc, "R-001"), _node(doc, "R-002"), _node(doc, "R-003")
    assert (r1["value"], r1["value_unit"], r1["value_text"]) == (50, "flat", "50")
    assert (r2["value"], r2["value_unit"], r2["value_text"]) == (40, "permil", "4%")
    assert r3["value"] == 70                                   # the per-character override wins
    y = _node(doc, "Y-001")
    assert (y["value"], y["value_unit"], y["value_text"]) == (5, "permil", "0.5%")   # from the effect id
    assert _node(doc, "L-001")["value"] is None                # no [value] in the text: no number


def test_connect_covers_its_shape_and_boosts_only_those_nodes():
    collection = {"cards": {"card-00001-5-uniq-0001-00": {"bloom": 2}}}
    doc = bv.annotate(_doc("card-00001-5-uniq-0001-00"), _SKILLTREE, collection)
    sock = doc["characters"][0]["sockets"][0]["connect"]
    assert sock["covers"] == ["R-001", "R-002"]
    assert (sock["level"], sock["level_basis"], sock["permil"]) == (1, "account", 550)
    r1, r2, r3 = _node(doc, "R-001"), _node(doc, "R-002"), _node(doc, "R-003")
    assert (r1["boosted_value"], r1["boosted_text"], r1["boosted_by"]) == (78, "78", ["S-001"])
    assert (r2["boosted_value"], r2["boosted_text"]) == (62, "6.2%")
    assert (r3["connect_permil"], r3["boosted_value"]) == (0, 70)


def test_connect_level_follows_the_accounts_bloom():
    bloom5 = {"cards": {"card-00001-5-uniq-0001-00": {"bloom": 5}}}
    cards = bv.connect_cards(_SKILLTREE, bloom5, maxed=False)
    assert (cards["card-00001-5-uniq-0001-00"]["level"], cards["card-00001-5-uniq-0001-00"]["permil"]) == (2, 1050)
    assert cards["card-00001-5-uniq-0001-00"]["text"] == "UP 205%"            # [highlight] tags stripped
    assert cards["card-00002-5-uniq-0002-00"]["level_basis"] == "assumed"     # no bloom recorded
    assert cards["card-00002-5-uniq-0002-00"]["level"] == 1
    maxed = bv.connect_cards(_SKILLTREE, None, maxed=True)
    assert maxed["card-00002-5-uniq-0002-00"]["level"] == 2


def test_an_empty_socket_boosts_nothing():
    doc = bv.annotate(_doc(None), _SKILLTREE, {})
    assert doc["characters"][0]["sockets"][0]["connect"] is None
    assert all(n["connect_permil"] == 0 for n in doc["characters"][0]["nodes"])


_REAL = Path(__file__).resolve().parents[1] / "recon" / "proxy" / "dumps" / "skilltree.json"


@pytest.mark.skipif(not _REAL.is_file(), reason="skilltree.json dump absent (gitignored)")
def test_every_real_connect_card_has_a_shape_and_a_base_permil():
    skilltree = json.loads(_REAL.read_text(encoding="utf-8"))
    cards = bv.connect_cards(skilltree, None, maxed=False)
    assert len(cards) == len(skilltree["card_connect"])
    for info in cards.values():
        assert info["cells"] and info["permil"] == info["permil_levels"][0]
        assert len(info["permil_levels"]) == 2 and info["permil_levels"][1] - info["permil_levels"][0] == 500
