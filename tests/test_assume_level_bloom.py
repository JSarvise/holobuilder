"""The level/bloom "what-if" filter — assume every card at a given level/bloom.

`_override_collection_levels` rewrites the collection document; `resolve_card_state`
accepts `limit_break: "max"` so a hypothetical high level is not clamped by a real
card's low limit break. (The keystone invariant — assuming MAX level AND MAX bloom
reproduces `data/cards.json`'s fully-maxed stats for every card — needs a real
account and is not pinned here, since the fixture carries no collection; the unit
checks below cover the transform and the cap lift.)
"""

from __future__ import annotations

from hbuilder.cli import _override_collection_levels
from hbuilder.domain.collection import (
    MAX,
    UNKNOWN,
    CardLevelCaps,
    resolve_card_state,
)

CAPS = CardLevelCaps(
    level_by_limit_break=(30, 40, 50, 55, 60),
    max_bloom=6,
    bloom_mult_permil=(0, 0, 100, 100, 100, 100, 100),
)


# --- resolve_card_state: limit_break "max" ---------------------------------

def test_max_limit_break_lifts_the_level_cap():
    hi = resolve_card_state("c", {"level": 60, "limit_break": "max"}, UNKNOWN, CAPS)
    assert hi.limit_break == 4          # the top index of level_by_limit_break
    assert hi.level_cap == 60
    assert hi.level == 60               # not clamped
    # The same level at the REAL low limit break clamps, which is why the filter
    # lifts it: without "max", "assume level 60" would silently become 30.
    lo = resolve_card_state("c", {"level": 60, "limit_break": 0}, UNKNOWN, CAPS)
    assert lo.level == 30


def test_max_limit_break_without_caps_is_a_first_class_unknown():
    s = resolve_card_state("c", {"limit_break": "max"}, UNKNOWN, None)
    assert s.limit_break is None        # no card_caps.json -> unknown, not a guess


# --- _override_collection_levels -------------------------------------------

def test_override_forces_level_and_lifts_limit_break_without_touching_bloom():
    doc = {"cards": {"a": {"level": 5, "bloom": 1, "limit_break": 0},
                     "b": {"level": 10}},
           "card_default": "unknown"}
    out = _override_collection_levels(doc, assume_level=50, assume_bloom=None)
    assert out["cards"]["a"] == {"level": 50, "bloom": 1, "limit_break": "max"}
    assert out["cards"]["b"] == {"level": 50, "limit_break": "max"}
    assert out["card_default"] == {"level": 50, "limit_break": "max"}
    # source document is not mutated
    assert doc["cards"]["a"] == {"level": 5, "bloom": 1, "limit_break": 0}


def test_override_bloom_only_leaves_level_and_limit_break_alone():
    doc = {"cards": {"a": {"level": 5, "bloom": 1, "limit_break": 2}}}
    out = _override_collection_levels(doc, assume_level=None, assume_bloom=6)
    assert out["cards"]["a"] == {"level": 5, "bloom": 6, "limit_break": 2}


def test_keep_higher_does_not_lower_a_card_already_above_the_assumption():
    # "do not delevel / debloom" (as in the community tool holodori.best): 70 stays 70
    # under an assumed 60, 40 rises
    doc = {"cards": {"hi": {"level": 70, "bloom": 4, "limit_break": 3},
                     "lo": {"level": 40, "bloom": 0, "limit_break": 1}}}
    out = _override_collection_levels(doc, assume_level=60, assume_bloom=2,
                                      keep_higher_level=True, keep_higher_bloom=True)
    assert out["cards"]["hi"] == {"level": 70, "bloom": 4, "limit_break": 3}
    assert out["cards"]["lo"] == {"level": 60, "bloom": 2, "limit_break": "max"}
    # without the flags both are forced
    out = _override_collection_levels(doc, assume_level=60, assume_bloom=2)
    assert out["cards"]["hi"] == {"level": 60, "bloom": 2, "limit_break": "max"}


def test_assumed_limit_break_caps_the_level_after_the_level_assumption():
    # "Assumed SP Training": level max at limit break 2 -> that break's cap
    doc = {"cards": {"a": {"level": 40, "limit_break": 4}, "b": {"level": 30, "limit_break": 0}}}
    out = _override_collection_levels(doc, assume_level="max", assume_bloom=None,
                                      assume_limit_break=2)
    assert out["cards"]["a"] == {"level": "max", "limit_break": 2}
    assert out["cards"]["b"] == {"level": "max", "limit_break": 2}
    # keep higher: a card already past that break keeps its own
    out = _override_collection_levels(doc, assume_level=None, assume_bloom=None,
                                      keep_higher_level=True, assume_limit_break=2)
    assert out["cards"]["a"]["limit_break"] == 4 and out["cards"]["b"]["limit_break"] == 2
