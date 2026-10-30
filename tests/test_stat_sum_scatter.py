"""`_stat_sum` scatters where it used to gather — and the two must agree
BIT-for-bit, not approximately.

`SEARCH-STRATEGY.md` §2.1 measured the rewrite at **1.31x** on the real
91-leader shape and justified shipping it on the claim that bit-identicality is
*structural* rather than tolerance-based: a bucket is filled in
`zip(stat_entries, recipients)` order, which is the order the predecessor's
list-comprehension filtered in, so `combine()` sees the identical sequence; and
the outer summation still runs `members` x `STAT_NAMES`, so float addition never
sees a different order.

**That claim is exactly the kind that reads as proven and is not tested by the
rest of the suite.** Every other scoring test asserts a score against a number
it expects; none of them would notice a rate landing in the wrong bucket for a
shape `data/cards.json` does not currently produce. So this module keeps the
predecessor runnable as a reference implementation and asserts exact float
equality against it over deliberately adversarial entry shapes — overlapping
recipients, empty recipient sets, multi-stat entries, and repeated
`(card, stat)` pairs whose combine order is the thing at risk.

`==` throughout, never `pytest.approx`. A tolerance here would pass on precisely
the reordering the rewrite promises not to introduce.
"""

from __future__ import annotations

import random

import pytest

import dataclasses

from hbuilder.domain.card import Card, Stats
from hbuilder.domain.scoring import _FIXED_TARGET, STAT_NAMES, _stat_sum
from hbuilder.domain.scoring_config import ScoringConfig
from hbuilder.domain.scoring_models import STACKING_MODELS

from conftest import make_scoring_config


def config_with(stacking: str) -> ScoringConfig:
    """`make_scoring_config` exposes no `stacking_model` override, and adding one
    would reach every test module that imports it. `dataclasses.replace` over the
    same default config is local to this file and does the same thing."""
    return dataclasses.replace(
        make_scoring_config(), stacking_model=STACKING_MODELS[stacking]
    )


def gather_stat_sum(
    members: tuple[Card, ...],
    stat_entries: list,
    cfg: ScoringConfig,
    recipients: list,
) -> float:
    """The predecessor, verbatim, kept as the reference oracle.

    Deliberately a copy rather than an import: the point of the test is that
    the shipped function agrees with the algorithm this once WAS, so the
    reference has to survive the shipped one being edited again.
    """
    total = 0.0
    for card in members:
        card_id = card.id
        for stat_name in STAT_NAMES:
            matching_rates = [
                entry[0]
                for entry, target_ids in zip(stat_entries, recipients)
                if stat_name in entry[1] and card_id in target_ids
            ]
            combined = cfg.stacking_model.combine(matching_rates)
            total += card.stat(stat_name) * (1.0 + combined)
    return total


def make_member(index: int, performance: int, technique: int, sense: int) -> Card:
    return Card(
        id=f"card-{index:03d}",
        member=f"holomem-{index}",
        generations=frozenset({"gen-0"}),
        type="cute",
        rarity="SSR",
        costume_name="c",
        stats=Stats(performance=performance, technique=technique, sense=sense),
        skills=(None, None, None),
        costume_skill=None,
    )


MEMBERS = tuple(
    make_member(index, 1000 + index * 37, 900 + index * 53, 800 + index * 71)
    for index in range(5)
)
IDS = tuple(card.id for card in MEMBERS)


def entry(rate: float, stat_names: tuple[str, ...], target_ids: frozenset) -> tuple:
    """A `StatEntry` in the shape `_stat_sum` reads: only fields 0, 1 and the
    supplied recipient set matter to it — 3 and 4 are the slot-order fields and
    are never read by the arithmetic."""
    return (rate, stat_names, target_ids, _FIXED_TARGET, IDS[0])


ADVERSARIAL_CASES: tuple[tuple[str, list], ...] = (
    (
        "one entry, one recipient, one stat",
        [entry(0.15, ("performance",), frozenset({IDS[0]}))],
    ),
    (
        "an all_stats entry reaching everybody",
        [entry(0.15, STAT_NAMES, frozenset(IDS))],
    ),
    (
        # The order-sensitive case. Three rates land in ONE bucket and
        # `MultiplicativeStacking.combine` is not commutative in floating point,
        # so a scatter that filled the bucket in any order but entry order would
        # differ in the last bits here and nowhere else.
        "three entries stacking on the SAME (card, stat)",
        [
            entry(0.1, ("sense",), frozenset({IDS[2]})),
            entry(0.37, ("sense",), frozenset({IDS[2]})),
            entry(0.03, ("sense",), frozenset({IDS[2]})),
        ],
    ),
    (
        # 19.9 % of real entries resolve to an empty recipient set
        # (`SEARCH-STRATEGY.md` §1). The scatter skips them; the gather filtered
        # them out. Same answer only if `combine(())` is still called for the
        # (card, stat) pairs they would never have reached.
        "an entry whose recipients are empty",
        [
            entry(0.5, STAT_NAMES, frozenset()),
            entry(0.2, ("technique",), frozenset({IDS[1], IDS[3]})),
        ],
    ),
    (
        "every entry empty — nothing to scatter at all",
        [entry(0.5, STAT_NAMES, frozenset()), entry(0.25, ("sense",), frozenset())],
    ),
    (
        "no entries at all",
        [],
    ),
    (
        "overlapping recipient sets across differing stat tuples",
        [
            entry(0.12, ("performance", "sense"), frozenset({IDS[0], IDS[1], IDS[4]})),
            entry(0.44, STAT_NAMES, frozenset({IDS[1], IDS[2]})),
            entry(0.08, ("sense",), frozenset({IDS[4]})),
            entry(-0.10, ("performance",), frozenset({IDS[0], IDS[3]})),
        ],
    ),
)


@pytest.mark.parametrize("stacking", ["additive", "multiplicative"])
@pytest.mark.parametrize(
    "label,stat_entries", ADVERSARIAL_CASES, ids=[case[0] for case in ADVERSARIAL_CASES]
)
def test_the_scatter_equals_the_gather_bit_for_bit(label, stat_entries, stacking):
    """Both stacking models, because only the multiplicative one can detect a
    reordering inside a bucket — under additive stacking a permuted bucket is
    *usually* still equal, so testing additive alone would prove nothing about
    the ordering claim."""
    cfg = config_with(stacking)
    recipients = [item[2] for item in stat_entries]

    assert _stat_sum(MEMBERS, stat_entries, cfg, recipients) == gather_stat_sum(
        MEMBERS, stat_entries, cfg, recipients
    ), label


@pytest.mark.parametrize("stacking", ["additive", "multiplicative"])
def test_the_scatter_equals_the_gather_over_randomised_entry_shapes(stacking):
    """A fixed-seed fuzz over the shapes the hand-written cases cannot enumerate.

    Seeded, so a failure is reproducible from the printed trial index alone; the
    assertion is still exact equality, so this is a *proof attempt*, not a
    statistical check.
    """
    cfg = config_with(stacking)
    rng = random.Random(20260803)

    for trial in range(400):
        stat_entries = []
        for _ in range(rng.randrange(0, 8)):
            names = rng.choice(
                [("performance",), ("technique",), ("sense",), STAT_NAMES,
                 ("performance", "sense"), ("technique", "sense")]
            )
            reach = frozenset(rng.sample(IDS, rng.randrange(0, len(IDS) + 1)))
            stat_entries.append(entry(round(rng.uniform(-0.5, 0.9), 4), names, reach))
        recipients = [item[2] for item in stat_entries]

        assert _stat_sum(MEMBERS, stat_entries, cfg, recipients) == gather_stat_sum(
            MEMBERS, stat_entries, cfg, recipients
        ), f"trial {trial}: {stat_entries}"


def test_the_recipients_argument_overrides_the_entrys_own_target_ids():
    """`_best_order_stat_sum` re-resolves recipients per candidate slot order and
    passes them in `recipients`, NOT by rebuilding the entries. A scatter that
    read `entry[2]` instead of the supplied set would score every ordering
    identically — the slot-order search would silently become a no-op, and every
    existing test would still pass because the free rule passes `entry[2]`
    anyway."""
    cfg = config_with("additive")
    stat_entries = [entry(0.5, ("performance",), frozenset({IDS[0]}))]

    as_written = _stat_sum(MEMBERS, stat_entries, cfg, [frozenset({IDS[0]})])
    overridden = _stat_sum(MEMBERS, stat_entries, cfg, [frozenset({IDS[4]})])

    assert as_written != overridden
    assert as_written == MEMBERS[0].stat("performance") * 0.5 + sum(
        card.stat(name) for card in MEMBERS for name in STAT_NAMES
    )
