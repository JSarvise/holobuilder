"""Per-card collection state and the stat formula.

Seeded from `tools/explorer/card_state.py`'s 10-check `_selftest()`, plus the
two things that self-test could not do: a bloom-0 decision and an acceptance
check of the stat formula against real cards.

The acceptance check reads `dumps/cards_enriched.json`, which is gitignored
account-adjacent data, and skips when it is absent — the same contract
`tests/test_board_landing.py` uses for the real board.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from conftest import make_card, make_scoring_config, make_team
from hbuilder.cli import build_parser
from hbuilder.domain.collection import (
    MAX,
    FULLY_SCORABLE_BLOOM,
    SKILL_BLOOM_STEP,
    connect_bonus_level,
    overstated_skills,
    UNKNOWN,
    CardLevelCaps,
    CollectionError,
    bloom_multiplier,
    caps_from_mapping,
    effective_stats,
    _f32,
    level_cap_for,
    resolve_card_state,
    resolve_collection,
    stat_basis_from_mapping,
    collection_stats,
    caps_by_rarity_from_mapping,
    MEMORY_BONUS_CAP_PERMIL,
    MEMORY_BONUS_MAX_POSTERS,
    MEMBER_POWER_UP_CAP_PERMYRIAD,
    PLAYER_LEVEL_MAX,
    memory_bonus_permil,
    max_member_power_up_permyriad,
    AccountBonuses,
    account_bonuses_from_mapping,
    memory_bonus,
    memory_bonus_per_stat,
    member_power_up,
)

_REPO = Path(__file__).resolve().parents[1]
_CAPS_FILE = _REPO / "tools" / "recon" / "proxy" / "dumps" / "card_caps.json"
_ENRICHED = _REPO / "tools" / "recon" / "proxy" / "dumps" / "cards_enriched.json"


# The real 5* shape: level caps 40/50/60/70/80 by limit break, bloom
# 0..5 with a cumulative permil array. Written out rather than loaded so the
# unit tests below do not depend on a gitignored file.
FIVE_STAR = CardLevelCaps(
    level_by_limit_break=(40, 50, 60, 70, 80),
    max_bloom=5,
    bloom_mult_permil=(0, 0, 100, 100, 100, 100),
)


# ---------------------------------------------------------------------------
# 1. "max" resolves against the real ceilings, per rarity AND per limit break
# ---------------------------------------------------------------------------


def test_max_with_limit_break_zero_is_forty_not_eighty():
    """The check the original self-test existed for. `max` is not "the biggest
    number in the table" — it is the biggest number THIS card can reach, and on
    a 5* at limit break 0 that is 40."""
    state = resolve_card_state(
        "c", {"level": MAX, "limit_break": 0}, UNKNOWN, FIVE_STAR
    )
    assert state.level == 40
    assert state.level_cap == 40


def test_max_with_no_limit_break_stated_takes_the_top_row():
    state = resolve_card_state("c", MAX, UNKNOWN, FIVE_STAR)
    assert state.level == 80
    assert state.bloom == 5
    assert state.source == MAX


def test_max_with_no_caps_file_resolves_to_none_and_says_so():
    """With no ceilings available, `max` is unknown, never a guessed number.
    This is the behaviour that matters most."""
    state = resolve_card_state("c", MAX, UNKNOWN, None)

    assert state.level is None
    assert state.bloom is None
    assert not state.known
    assert any("card_caps" in w for w in state.warnings)


# ---------------------------------------------------------------------------
# 2. Partial entries inherit; clamps REPORT
# ---------------------------------------------------------------------------


def test_a_partial_entry_takes_the_rest_from_the_default():
    """`{"bloom": 3}` is legal — it is what a UI writes when one control is
    touched — and its level must come from the default rather than vanishing."""
    state = resolve_card_state("c", {"bloom": 3}, MAX, FIVE_STAR)

    assert state.bloom == 3
    assert state.level == 80, "the level should have been inherited from 'max'"


def test_a_level_above_the_cap_is_clamped_AND_reported():
    """Clamped and reported, not clamped silently: a collection that says 99 is
    a collection someone mistyped, and correcting it without saying so hides
    the mistake behind a plausible number."""
    state = resolve_card_state("c", {"level": 99, "limit_break": 0}, UNKNOWN, FIVE_STAR)

    assert state.level == 40
    assert any("exceeds the cap" in w for w in state.warnings)


def test_a_bloom_above_the_maximum_is_clamped_and_reported():
    state = resolve_card_state("c", {"level": 50, "bloom": 9}, UNKNOWN, FIVE_STAR)

    assert state.bloom == 5
    assert any("exceeds the maximum" in w for w in state.warnings)


def test_a_level_below_one_is_raised_and_reported():
    state = resolve_card_state("c", {"level": 0, "bloom": 2}, UNKNOWN, FIVE_STAR)

    assert state.level == 1
    assert any("below 1" in w for w in state.warnings)


def test_a_non_numeric_value_is_reported_and_left_unknown_not_defaulted():
    state = resolve_card_state("c", {"level": "twenty", "bloom": 2}, UNKNOWN, FIVE_STAR)

    assert state.level is None
    assert any("not a number" in w for w in state.warnings)


def test_an_unrecognised_string_reads_as_unknown_rather_than_inheriting():
    """A typo must not silently inherit the default and come back looking like
    a real answer."""
    state = resolve_card_state("c", "maxx", MAX, FIVE_STAR)

    assert state.level is None and state.bloom is None
    assert not state.known


# ---------------------------------------------------------------------------
# 3. Bloom 0 — decided rather than left open
# ---------------------------------------------------------------------------


def test_bloom_zero_is_recorded_but_not_scorable_and_says_why():
    """A real account state the dataset has no magnitudes for: represent it,
    mark it, do not score it."""
    state = resolve_card_state("c", {"level": 40, "bloom": 0}, UNKNOWN, FIVE_STAR)

    assert state.bloom == 0
    assert state.known, "the state IS known — the owner told us it is bloom 0"
    assert not state.scorable
    assert state.overstated == ("ACTIVE", "SPECIAL", "PASSIVE")
    assert any("OVERSTATE" in w for w in state.warnings)


def test_bloom_one_upgrades_the_active_and_leaves_the_other_two_overstated():
    """The correction that the master data forced. `data/cards.json` holds
    LEVEL-2 magnitudes for all three kinds (every card), and they unlock
    at bloom 1, 3 and 4 — so bloom 1 fixes the ACTIVE and nothing else.

    Treating bloom >= 1 as fully scorable is true of the ACTIVE and wrong
    about SPECIAL and PASSIVE: a true statement about one table reused as a
    general fact."""
    state = resolve_card_state("c", {"level": 40, "bloom": 1}, UNKNOWN, FIVE_STAR)

    assert state.overstated == ("SPECIAL", "PASSIVE")
    assert not state.scorable


def test_only_bloom_four_and_up_is_fully_scorable():
    """PASSIVE is the last to unlock, at bloom 4, so that is the threshold."""
    assert FULLY_SCORABLE_BLOOM == 4
    below = resolve_card_state("c", {"level": 40, "bloom": 3}, UNKNOWN, FIVE_STAR)
    at = resolve_card_state("c", {"level": 40, "bloom": 4}, UNKNOWN, FIVE_STAR)

    assert below.overstated == ("PASSIVE",) and not below.scorable
    assert at.overstated == () and at.scorable


def test_overstated_skills_matches_the_master_bloom_steps():
    """Stated against the game's table rather than transcribed, so a change
    to `SKILL_BLOOM_STEP` moves this test with it."""
    for kind, step in SKILL_BLOOM_STEP.items():
        assert kind in overstated_skills(step - 1)
        assert kind not in overstated_skills(step)
    assert overstated_skills(None) == ()


def test_a_three_star_never_reaches_connect_bonus_level_two():
    """From CardPotential: 3* spends its bloom 5 on stats
    (effect_type 4, +100 permil) while 4*/5* spend it on the connect bonus
    (effect_type 5). `domain/board.py` treats the connect multiplier as an
    input precisely because the placed card's bloom was unrecoverable; this is
    the rule that recovers it."""
    assert connect_bonus_level(5, "3") == 1
    assert connect_bonus_level(5, "4") == 2
    assert connect_bonus_level(5, "5") == 2
    assert connect_bonus_level(4, "5") == 1
    assert connect_bonus_level(None, "5") is None


def test_a_card_with_a_level_but_no_bloom_is_not_known():
    """Half a state produces no stats — the formula needs both — so it must not
    report itself as known."""
    state = resolve_card_state("c", {"level": 40}, UNKNOWN, FIVE_STAR)

    assert state.level == 40 and state.bloom is None
    assert not state.known and not state.scorable


# ---------------------------------------------------------------------------
# 4. The document level
# ---------------------------------------------------------------------------


def test_an_id_not_in_the_dataset_is_warned_about_not_ignored():
    """How a renamed or removed card shows up. Dropping it silently is how a
    collection rots against cards.json without anyone noticing."""
    _states, warnings = resolve_collection(
        [("real", 5, 1)],
        {"card_default": UNKNOWN, "cards": {"ghost": {"level": 1, "bloom": 1}}},
        {"rarity_5-attribute_1": FIVE_STAR},
    )

    assert any("ghost" in w for w in warnings)


def test_an_absent_document_resolves_every_card_as_unknown():
    """No collection file is a first-class state, the same contract
    `load_board` has — not an error, and not an invented default."""
    states, warnings = resolve_collection([("a", 5, 1)], None, None)

    assert states["a"].source == UNKNOWN
    assert not states["a"].known
    assert warnings == [] or all("no level caps" in w for w in warnings)


def test_a_document_that_is_not_an_object_raises():
    with pytest.raises(CollectionError):
        resolve_collection([("a", 5, 1)], ["not", "an", "object"], None)


# ---------------------------------------------------------------------------
# 5. The stat formula
# ---------------------------------------------------------------------------


def test_bloom_multiplier_indexes_the_cumulative_array_and_does_not_sum_it():
    """`[0, 0, 50, 50, 50, 150]` is cumulative. Summing it gives 1.30 where the
    game gives 1.15 — and 1.15 is what `stats_max` was independently built
    with, so a summing bug would show up as a 13 % stat error everywhere."""
    assert bloom_multiplier(FIVE_STAR, 5) == pytest.approx(1.10)
    assert bloom_multiplier(FIVE_STAR, 2) == pytest.approx(1.10)
    assert bloom_multiplier(FIVE_STAR, 1) == pytest.approx(1.0)
    assert bloom_multiplier(FIVE_STAR, 0) == pytest.approx(1.0)
    # 3* is the rarity whose array actually steps twice, and it is the one a
    # summing bug shows up on: indexed gives 1.15, summed would give 1.30.
    three_star = CardLevelCaps((20, 30, 40, 50, 60), 5, (0, 0, 50, 50, 50, 150))
    assert bloom_multiplier(three_star, 5) == pytest.approx(1.15)
    assert bloom_multiplier(three_star, 2) == pytest.approx(1.05)


def test_bloom_multiplier_is_none_without_caps_never_a_neutral_one():
    """1.0 is a real multiplier meaning "bloom contributes nothing". It must not
    be indistinguishable from "what bloom contributes is not known"."""
    assert bloom_multiplier(None, 3) is None
    assert bloom_multiplier(FIVE_STAR, None) is None


def test_effective_stats_rounds_up_not_to_nearest():
    """With `round()`, 32 of 50 transcribed SSR cards come out
    exactly 1 low on their two smaller stats. This pins `ceil` directly — a
    ratio that lands just above an integer must go up."""
    # 1000 permil * 1.0 * 1.0 = 1.0 exactly; 1001 must become 2, not 1.
    assert effective_stats([1000, 1001, 1999], 1.0, 1.0) == (1, 2, 2)


def test_effective_stats_ceilings_twice_not_once():
    """The level-scaled parameter becomes an INTEGER before the bloom
    multiplier applies, and the product is rounded up again.

    Hand-checkable, and it discriminates the two rules by exactly +1:

        ratio 294, level_multiplier 1005, bloom_mult 1.15
          two ceilings: ceil(294 * 1.005) = ceil(295.47)  = 296
                        ceil(296 * 1.15)  = ceil(340.40)  = 341   <-- the game
          one ceiling : ceil(294 * 1.005 * 1.15) = ceil(339.79) = 340

    A CONFOUND worth naming: at bloom 0 and 1 `bloom_permil` is 0, so
    `bloom_mult` is 1.0 and the second ceiling is a no-op. On an account whose
    high-level cards are all bloom 0-1 the residual looks like it tracks LEVEL
    when it tracks BLOOM, and every all-level-70 deck reads residual 0 under
    BOTH rules.
    """
    assert effective_stats([294], 1005, 1.15) is None  # three stats or nothing
    assert effective_stats([294, 400, 306], 1005, 1.15) == (341, 463, 355)

    # The no-op case: at bloom_mult 1.0 the two rules are identical, so this
    # pins that the change cannot move a bloom-0 card.
    assert effective_stats([294, 400, 306], 1005, 1.0) == (296, 402, 308)


def test_effective_stats_is_single_precision_like_the_client():
    """From `CardStatus::.ctor` (`Vision.Common.Core`, RVA 0x52acff0): the whole chain is scalar SINGLE — `cvtsi2ss` / `divss` /
    `mulss` / `addss`, with constants 0x75ceef0 = 1000.0f and 0x75cebd4 =
    1.0f — and the only widening is one `cvtps2pd` feeding `Math.Ceiling`,
    which takes a `double` in C#.

    Takane Lui "Strategic Prodigy" (4*), level 2, bloom 0: the performance
    ratio is 400 permil and `CardLevel.parameter_base_value` is 4695, so the
    exact product is 0.400 * 4695 = 1878.0 on the nose. In f32 the client's
    own operand order — `permil * (parameter/1000.0f)` — lands at
    1878.0001220703125, and `ceil` promotes that to **1879**.

    A f64 implementation returns 1878 here and is WRONG: this pins the
    divergence at the one input shape where it is observable."""
    assert effective_stats([400, 294, 306], 4695.0, 1.0) == (1879, 1381, 1437)
    # Same card at level 22 (parameter_base_value 9015): 0.400 * 9015 = 3606.0.
    assert effective_stats([400, 294, 306], 9015.0, 1.0)[0] == 3607


def test_effective_stats_operand_order_matches_the_client():
    """The divergence above is caused by the ORDER, not by f32 alone.
    `(permil/1000) * parameter` and `permil * parameter / 1000` both give
    exactly 1878.0 in f32; only the client's `permil * (parameter/1000)`
    overshoots. Pinned so a "simplification" cannot silently undo it."""
    param, permil = 4695.0, 400
    assert _f32(_f32(permil) * _f32(_f32(param) / _f32(1000.0))) > 1878.0
    assert _f32(_f32(_f32(permil) / _f32(1000.0)) * _f32(param)) == 1878.0


def test_effective_stats_returns_none_on_any_missing_input():
    """Two of three stats is not a card, and a zero for the third is a
    fabricated number."""
    assert effective_stats([100, 200, None], 1.0, 1.0) is None
    assert effective_stats([100, 200, 300], None, 1.0) is None
    assert effective_stats([100, 200, 300], 1.0, None) is None
    assert effective_stats([100, 200], 1.0, 1.0) is None


# ---------------------------------------------------------------------------
# 6. Acceptance: the formula reproduces 167 real cards' maxed stats
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _ENRICHED.exists(), reason="dumps/cards_enriched.json is gitignored account data"
)
def test_the_formula_is_consistent_with_every_cards_stats_max():
    """The formula against 167 real cards, WITHOUT knowing the level multiplier.

    The per-level `CardLevel` curve is not exported (see the module docstring),
    and it cannot be recovered exactly from `stats_max`: solving it out of each
    card's largest stat assumes the largest lands on an integer, which is
    false, so the recovered multiplier inherits the ceiling it is supposed to
    invert (most cards come out 1 high).

    What IS falsifiable without it: the formula bounds `k` to a set of
    half-open intervals per stat, and the three sets must INTERSECT. If this
    module's arithmetic disagreed with the dataset builder's — a `round` where
    the builder ceils, a ratio read in the wrong order, a stat triple
    transposed — no single multiplier could satisfy all three at once and the
    intersection would be empty.

    ⛔ THE INVERSION IS TWO-STAGE. `(t_i - 1)/scale < k <= t_i/scale` would
    invert `ceil(r*k*b/1000)` — ONE ceiling over the whole product. The formula
    is `ceil(ceil(r*k/1000) * b)`, so the inner integer `m` has to be inverted
    first:

        ceil(m*b) == t   =>   m in the integers of ((t-1)/b, t/b]
        m == ceil(r*k/1000)  =>  k in (1000*(m-1)/r, 1000*m/r]

    a UNION over the admissible `m`, not a single interval.

    ⚠ What it does NOT catch, stated so nobody quotes it as more than it is: a
    wrong `bloom_mult` scales `k` and the intervals with it, so this cannot
    detect a summed-instead-of-indexed bloom array. That is pinned separately
    and exactly against the real caps file below.

    ⚠ And it is WEAKER than it looks: both sides of this comparison are the
    project's own code, and `build_cards_dataset.py` deliberately keeps its
    chain identical to this module's. It can only catch a DRIFT between the
    two, never a shared wrong assumption. The check against the server's own
    `card_potential_parameter` is the one that can — see `effective_stats`.
    """
    rows = json.loads(_ENRICHED.read_text(encoding="utf-8"))
    if isinstance(rows, dict):
        rows = rows.get("cards", list(rows.values()))

    checked = 0
    empty = []
    for row in rows:
        target = row.get("stats_max")
        ratio = row.get("stat_ratio_permil")
        if not target or not ratio or not target.get("at"):
            continue
        if any(r is None for r in ratio) or not all(ratio):
            continue
        bloom_mult = target["at"]["bloom_mult"]
        names = ("performance", "technique", "sense")

        # Per stat, the admissible intervals for `k`: one per integer `m` that
        # survives `ceil(m * bloom_mult) == target`.
        per_stat: list[list[tuple[float, float]]] = []
        for index, name in enumerate(names):
            t = target[name]
            spans = []
            lo_m = math.floor((t - 1) / bloom_mult)
            hi_m = math.ceil(t / bloom_mult)
            for m in range(max(1, lo_m), hi_m + 1):
                # ⚠ In SINGLE precision, like the module. `bloom_mult` here is
                # the file's `at.bloom_mult`, which the exporter stores as
                # `round(bloom, 3)` — a display value. With two ceilings, `m`
                # is an exact integer and the outer product lands ON the boundary, so
                # float64 against a rounded 1.1 reports `ceil(5580 * 1.1) =
                # 6139` where the f32 chain gives exactly 6138. Three cards
                # (00021/04014/06004, ratio 278 at k = 20,070) sit on it.
                if math.ceil(_f32(_f32(float(m)) * _f32(bloom_mult))) != t:
                    continue
                spans.append(
                    (1000 * (m - 1) / ratio[index], 1000 * m / ratio[index])
                )
            per_stat.append(spans)

        # Intersect the three unions.
        candidates = [(0.0, float("inf"))]
        for spans in per_stat:
            merged = []
            for c_lo, c_hi in candidates:
                for s_lo, s_hi in spans:
                    lo, hi = max(c_lo, s_lo), min(c_hi, s_hi)
                    if lo < hi:
                        merged.append((lo, hi))
            candidates = merged

        checked += 1
        if not candidates:
            empty.append((row.get("id"), "no k satisfies all three stats"))
            continue

        # And the positive half: a multiplier from inside an admissible interval
        # must reproduce all three stats exactly through this module.
        low, high = candidates[0]
        midpoint = (low + high) / 2
        got = effective_stats(ratio, midpoint, bloom_mult)
        want = tuple(target[name] for name in names)
        if got != want:
            empty.append((row.get("id"), got, want))

    assert checked >= 100, f"expected the full roster, only checked {checked}"
    assert not empty, (
        f"{len(empty)} of {checked} cards have no level multiplier consistent "
        f"with this module's formula: {empty[:5]}"
    )


@pytest.mark.skipif(
    not _CAPS_FILE.exists(), reason="dumps/card_caps.json is gitignored account data"
)
def test_caps_from_mapping_joins_the_two_real_tables():
    """`card_caps.json` keys level caps by rarity AND attribute but bloom by
    rarity ALONE. This pins the join against the real file, because a join that
    silently produced empty bloom arrays would make every `max` bloom resolve
    to `None` and look like a missing caps file."""
    caps = caps_from_mapping(json.loads(_CAPS_FILE.read_text(encoding="utf-8")))

    assert caps, "no groups parsed out of the real card_caps.json"
    for group, entry in caps.items():
        assert entry.level_by_limit_break, f"{group} has no level caps"
        assert entry.bloom_mult_permil, f"{group} lost its bloom array in the join"
        assert entry.max_bloom == 5
        # The caps are 5 long (limit break 0..4), the bloom array 6
        # (bloom 0..5). Swapping them is the shape error this catches.
        assert len(entry.level_by_limit_break) == 5
        assert len(entry.bloom_mult_permil) == 6


@pytest.mark.skipif(
    not _CAPS_FILE.exists(), reason="dumps/card_caps.json is gitignored account data"
)
def test_the_real_five_star_caps_match_the_fixture_used_above():
    """The fixture at the top of this file is written out rather than loaded,
    so it can drift from the real data it claims to model. This is the tie."""
    caps = caps_from_mapping(json.loads(_CAPS_FILE.read_text(encoding="utf-8")))
    real = caps.get("rarity_5-attribute_1")

    assert real is not None
    assert real.level_by_limit_break == FIVE_STAR.level_by_limit_break
    assert real.max_bloom == FIVE_STAR.max_bloom
    assert real.bloom_mult_permil == FIVE_STAR.bloom_mult_permil


# ---------------------------------------------------------------------------
# 7. StatBasis — the per-level curve, and the acceptance it makes possible
# ---------------------------------------------------------------------------

_CURVE = _REPO / "tools" / "recon" / "proxy" / "dumps" / "card_level_curve.json"


def _basis():
    return stat_basis_from_mapping(json.loads(_CURVE.read_text(encoding="utf-8")))


@pytest.mark.skipif(
    not _CURVE.exists(),
    reason="dumps/card_level_curve.json is gitignored; run tools/recon/proxy/card_level_curve.py",
)
@pytest.mark.skipif(
    not _ENRICHED.exists(), reason="dumps/cards_enriched.json is gitignored account data"
)
def test_stat_basis_reproduces_every_cards_stats_max_exactly():
    """The acceptance the interval test above can only approximate, made EXACT.

    With the per-level curve exported, the level multiplier is read rather than solved, so this
    is a straight equality against `build_cards_dataset.py`'s independently-computed
    `stats_max` on every card on disk.

    Run through `StatBasis`, not through the exporter's own `verify()`: the exporter reporting
    success proves the exporter agrees with itself. This proves the DOMAIN module agrees with a
    file it did not write (two derivations, one comparison).
    """
    basis = _basis()
    rows = json.loads(_ENRICHED.read_text(encoding="utf-8"))
    if isinstance(rows, dict):
        rows = rows.get("cards", list(rows.values()))

    checked, mismatches = 0, []
    for row in rows:
        target = row.get("stats_max")
        card_id = row.get("id")
        if not target or card_id not in basis.cards:
            continue
        got = basis.stats_at(card_id, target["at"]["level"], _max_bloom_of(basis, card_id))
        want = (target["performance"], target["technique"], target["sense"])
        checked += 1
        if got != want:
            mismatches.append((card_id, got, want))

    assert checked >= 150, f"expected the full roster, only checked {checked}"
    assert not mismatches, (
        f"{len(mismatches)} of {checked} cards disagree with stats_max: {mismatches[:5]}"
    )


def _max_bloom_of(basis, card_id):
    """The top bloom this card's potential group defines — `stats_max` is the fully-bloomed
    figure, so the comparison above has to ask for that bloom and not for a guess at it."""
    group = basis.cards[card_id][1]
    return len(basis.bloom_permil[group]) - 1


@pytest.mark.skipif(not _CURVE.exists(), reason="dumps/card_level_curve.json is gitignored")
def test_the_curve_is_monotonic_and_covers_every_level_it_claims():
    """Two properties of the real table, checked rather than assumed: the multiplier never
    decreases with level, and every group's levels are contiguous from 1. A gap would make
    `stats_at` return None for a level a player can legitimately have a card at."""
    basis = _basis()
    assert basis.level_mult, "no groups parsed"

    for group, levels in basis.level_mult.items():
        keys = sorted(levels)
        assert keys[0] == 1, f"{group} starts at level {keys[0]}, not 1"
        assert keys == list(range(keys[0], keys[-1] + 1)), f"{group} has a gap"
        values = [levels[k] for k in keys]
        assert values == sorted(values), f"{group}'s multiplier decreases with level"


@pytest.mark.skipif(not _CURVE.exists(), reason="dumps/card_level_curve.json is gitignored")
def test_a_level_outside_the_curve_is_none_not_the_nearest():
    """The curve is the game's own. A level it does not define is a question this module
    cannot answer, and answering with the nearest one would be a fabricated stat."""
    basis = _basis()
    card_id = next(iter(basis.cards))

    assert basis.level_multiplier(card_id, 0) is None
    assert basis.level_multiplier(card_id, 9999) is None
    assert basis.level_multiplier("no-such-card", 10) is None


@pytest.mark.skipif(not _CURVE.exists(), reason="dumps/card_level_curve.json is gitignored")
def test_bloom_past_the_last_stat_step_still_has_a_multiplier():
    """Rarities 4 and 5 carry a stat permil only at bloom 2, so blooms 3-5 grant
    other effect types and no stat change. The array is padded to max bloom for exactly this —
    truncated, a 5* at bloom 5 would read as "unknown" instead of 1.10, and the card would drop
    out of a ranking it belongs in."""
    basis = _basis()
    five_star = [c for c in basis.cards if basis.cards[c][1].endswith("rarity_5")]
    assert five_star, "no 5* cards in the curve export"

    card_id = five_star[0]
    assert basis.bloom_multiplier(card_id, 5) == pytest.approx(1.10)
    assert basis.bloom_multiplier(card_id, 2) == pytest.approx(1.10)
    assert basis.bloom_multiplier(card_id, 1) == pytest.approx(1.0)
    assert basis.bloom_multiplier(card_id, 6) is None


@pytest.mark.skipif(not _CURVE.exists(), reason="dumps/card_level_curve.json is gitignored")
def test_levelling_one_card_actually_moves_its_stats():
    """The whole point of the export, stated as a behaviour: the same card at two levels must
    give two different stat triples, strictly increasing. Without this the collection editor's
    level control would be decorative."""
    basis = _basis()
    card_id = next(c for c in basis.cards if all(basis.cards[c][2]))

    low = basis.stats_at(card_id, 10, 1)
    high = basis.stats_at(card_id, 50, 1)

    assert low is not None and high is not None
    assert all(h > l for h, l in zip(high, low)), f"{card_id}: {low} -> {high}"


_ORACLE = _REPO / "tools" / "recon" / "proxy" / "dumps" / "deck_oracle.json"
_MP_KEY = "card_potential_parameter [Member Parameter]"


@pytest.mark.skipif(
    not (_ORACLE.exists() and _CURVE.exists() and _CAPS_FILE.exists()),
    reason="dumps/ is gitignored account data",
)
def test_member_parameter_reproduces_the_servers_own_figure():
    """⛔ THE NON-CIRCULAR CHECK. Everything else that guards this formula
    compares the project's code against its own generated files.

    `build_cards_dataset.py` deliberately keeps its arithmetic identical to
    `collection.effective_stats`, so `cards_enriched.json`'s `stats_max` agrees
    with the domain module WHATEVER the rounding rule is. It cannot catch a
    wrong rounding rule.

    `card_potential_parameter` is the SERVER's own Member Parameter, off the
    wire. It is the only number in this comparison the project does not compute.

    ⛔ **EACH ROW IS SCORED AGAINST ITS OWN CAPTURE'S LEVELS.** The wire figure
    is frozen at the instant of the capture, while `state/collection.json` is
    re-merged from a live `User/Get` on every refresh, so reading it would make
    every level-up in between read as a model error (the same few cards, with
    residuals of exactly one level's worth, over and over). `deck_oracle.py`
    records each row's capture-time levels, so every row is checked exactly and
    the check cannot rot with the account.
    """
    basis = _basis()
    caps = caps_by_rarity_from_mapping(json.loads(_CAPS_FILE.read_text(encoding="utf-8")))
    rows = json.loads(_ORACLE.read_text(encoding="utf-8"))

    exact, checked, residuals, undated = 0, 0, [], []
    for index, row in enumerate(rows):
        wire = (row.get("power") or {}).get(_MP_KEY)
        if wire is None:
            continue
        levels = row.get("levels")
        if not levels:
            # Never fall back to the live collection (see the docstring).
            # A row with no levels is unjudgeable.
            undated.append((index, row.get("capture")))
            continue
        ids = [row["cards"][slot] for slot in sorted(row["cards"])]
        ids = [c if isinstance(c, str) else (c.get("card") or "") for c in ids]
        document = {"card_default": "unknown", "cards": levels}
        stats, _report = collection_stats(ids, document, caps, basis)
        if len(stats) != len(ids):
            continue
        checked += 1
        total = sum(sum(triple) for triple in stats.values())
        if total == wire:
            exact += 1
        else:
            residuals.append((index, total - wire, row.get("capture")))

    assert not undated, (
        "every oracle row must carry the card levels of its own capture — "
        "regenerate with `proxyenv/Scripts/python deck_oracle.py`; "
        f"rows without levels: {undated[:5]}"
    )
    assert checked >= 25, f"expected the captured decks, only checked {checked}"
    assert not residuals, (
        f"{len(residuals)} of {checked} decks disagree with the server's own "
        f"Member Parameter: {residuals[:5]}"
    )
    assert exact == checked


# ---------------------------------------------------------------------------
# 8. collection_stats — and the migration property the wiring depends on
# ---------------------------------------------------------------------------

# The canonical cards file, read from the CLI's OWN default rather than
# restated as a path literal — `tests/test_fixture_isolation.py` forbids the
# literal, and reading the tool's default is stricter than a second constant
# because it cannot disagree with the tool. Same pattern as
# `tests/test_cli_heuristic.py`.
_CARDS_JSON = _REPO / build_parser().parse_args(["solve"]).cards


@pytest.mark.skipif(not _CURVE.exists(), reason="dumps/card_level_curve.json is gitignored")
@pytest.mark.skipif(not _CAPS_FILE.exists(), reason="dumps/card_caps.json is gitignored")
def test_a_max_collection_reproduces_data_cards_json_exactly():
    """THE migration property, and the reason the wiring is safe to land.

    Every card in `data/cards.json` carries its FULLY-MAXED stats (equal to
    `cards_enriched.json`'s `stats_max`), so a ranking without a collection
    assumes a fully-maxed collection. A `card_default: "max"` document must
    therefore reproduce those stats to the unit — if it did not, applying the
    collection would silently move every maxed answer.
    """
    basis = _basis()
    caps = caps_by_rarity_from_mapping(json.loads(_CAPS_FILE.read_text(encoding="utf-8")))
    cards = json.loads(_CARDS_JSON.read_text(encoding="utf-8"))["cards"]
    ids = [c["id"] for c in cards]

    stats, report = collection_stats(ids, {"card_default": MAX, "cards": {}}, caps, basis)

    assert not report.unchanged, f"could not restate: {report.unchanged[:5]}"
    assert len(report.applied) == len(ids)
    mismatches = [
        (c["id"], stats[c["id"]], (c["stats"]["performance"], c["stats"]["technique"], c["stats"]["sense"]))
        for c in cards
        if stats.get(c["id"])
        != (c["stats"]["performance"], c["stats"]["technique"], c["stats"]["sense"])
    ]
    assert not mismatches, f"{len(mismatches)} of {len(ids)} differ: {mismatches[:5]}"


@pytest.mark.skipif(not _CURVE.exists(), reason="dumps/card_level_curve.json is gitignored")
@pytest.mark.skipif(not _CAPS_FILE.exists(), reason="dumps/card_caps.json is gitignored")
def test_a_lower_level_collection_actually_lowers_the_stats():
    """The negative control for the test above. If `collection_stats` ignored
    the document and always returned the maxed figure, the migration test would
    pass while the feature did nothing."""
    basis = _basis()
    caps = caps_by_rarity_from_mapping(json.loads(_CAPS_FILE.read_text(encoding="utf-8")))
    cards = json.loads(_CARDS_JSON.read_text(encoding="utf-8"))["cards"]
    ids = [c["id"] for c in cards]

    maxed, _ = collection_stats(ids, {"card_default": MAX, "cards": {}}, caps, basis)
    low, _ = collection_stats(
        ids, {"card_default": {"level": 10, "bloom": 1}, "cards": {}}, caps, basis
    )

    assert low and len(low) == len(maxed)
    assert all(
        all(a < b for a, b in zip(low[cid], maxed[cid])) for cid in low
    ), "a level-10 collection did not come out strictly below a maxed one"


@pytest.mark.skipif(not _CURVE.exists(), reason="dumps/card_level_curve.json is gitignored")
def test_a_card_with_no_resolvable_state_is_left_to_data_cards_json():
    """Omitted from the result and NAMED, never given a substitute number: the
    caller then keeps `data/cards.json`'s value, which is the authoritative
    one."""
    basis = _basis()
    ids = list(basis.cards)[:5]

    stats, report = collection_stats(ids, {"card_default": UNKNOWN, "cards": {}}, None, basis)

    assert stats == {}
    assert set(report.unchanged) == set(ids)
    assert report.applied == ()


@pytest.mark.skipif(not _CURVE.exists(), reason="dumps/card_level_curve.json is gitignored")
@pytest.mark.skipif(not _CAPS_FILE.exists(), reason="dumps/card_caps.json is gitignored")
def test_bloom_zero_applies_stats_and_is_reported_as_not_scorable():
    """The stat formula IS defined at bloom 0; the skill magnitudes are not. So
    the stats land and the card is named, rather than the run quietly ranking it
    with magnitudes it does not have."""
    basis = _basis()
    caps = caps_by_rarity_from_mapping(json.loads(_CAPS_FILE.read_text(encoding="utf-8")))
    card_id = next(iter(basis.cards))

    stats, report = collection_stats(
        [card_id], {"card_default": {"level": 20, "bloom": 0}, "cards": {}}, caps, basis
    )

    assert card_id in stats
    assert card_id in report.not_scorable
    assert "not fully scorable" in report.summary()


def test_collection_stats_without_a_curve_changes_nothing_and_says_so():
    """No `card_level_curve.json` is a first-class state, the same contract
    `load_board` has — every card keeps its `data/cards.json` value."""
    stats, report = collection_stats(["a", "b"], {"card_default": MAX}, None, None)

    assert stats == {}
    assert report.unchanged == ("a", "b")
    assert any("card_level_curve" in w for w in report.warnings)


# ---------------------------------------------------------------------------
# 9. The CLI seam — `--collection`
# ---------------------------------------------------------------------------


def _solve(capsys, argv):
    """One `solve` through `cli.main`, returning (exit_code, stdout).

    Driven through pytest's `capsys` rather than `redirect_stdout`, because
    `main` calls `sys.stdout.reconfigure(encoding="utf-8")` and a bare
    `StringIO` has no such method — the same way `test_estimate_backstops.py`
    exercises `cli.main`."""
    from hbuilder import cli

    code = cli.main(["solve", *argv])
    return code, capsys.readouterr().out


def _top_score(stdout):
    for line in stdout.splitlines():
        if line.startswith("1 "):
            return line.split()[1]
    return None


@pytest.fixture
def real_small_pool(tmp_path):
    """A tmp `owned.json` naming eight REAL card ids from `data/cards.json`.

    The shared fixture pool cannot exercise this seam at all: its cards are
    `fubuki_01`-style test ids, which appear in no master table, so nothing is
    restatable and every assertion below would pass vacuously. Real ids keep the pool small
    enough for an EXACT search, so the comparison is about the wiring rather
    than about the beam.
    """
    cards = json.loads(_CARDS_JSON.read_text(encoding="utf-8"))["cards"]
    # One card per HOLOMEM: Team rejects any member set repeating a holomem, and
    # the first eight cards by id share only a handful of characters — a pool
    # built from them enumerates no team at all and every assertion below would
    # compare two empty tables.
    seen, ids = set(), []
    for card in cards:
        if card["member"] in seen:
            continue
        seen.add(card["member"])
        ids.append(card["id"])
        if len(ids) == 8:
            break
    owned = tmp_path / "owned.json"
    owned.write_text(
        json.dumps({"schema_version": "2", "owned": ids}), encoding="utf-8"
    )
    return ["--cards", str(_CARDS_JSON), "--owned", str(owned), "--top", "1"]


@pytest.mark.skipif(not _CURVE.exists(), reason="dumps/card_level_curve.json is gitignored")
@pytest.mark.skipif(not _CAPS_FILE.exists(), reason="dumps/card_caps.json is gitignored")
def test_a_max_collection_leaves_the_cli_answer_identical(tmp_path, capsys, real_small_pool):
    """The migration property at the CLI seam, which is where it matters: a
    `card_default: "max"` document must give the SAME top score as
    `--no-collection`, because `data/cards.json` already holds maxed stats.

    Run on the small fixture pool so this is an exact search, not a beam — a
    heuristic run would make the comparison about the beam, not the wiring.
    """
    doc = tmp_path / "collection.json"
    doc.write_text(json.dumps({"card_default": MAX, "cards": {}}), encoding="utf-8")
    args = real_small_pool

    code_off, off = _solve(capsys, [*args, "--no-collection"])
    code_max, maxed = _solve(capsys, [*args, "--collection", str(doc)])

    assert code_off == 0 and code_max == 0
    assert _top_score(off) is not None
    assert _top_score(maxed) == _top_score(off)


@pytest.mark.skipif(not _CURVE.exists(), reason="dumps/card_level_curve.json is gitignored")
@pytest.mark.skipif(not _CAPS_FILE.exists(), reason="dumps/card_caps.json is gitignored")
def test_a_low_level_collection_moves_the_cli_answer(tmp_path, capsys, real_small_pool):
    """The negative control. Without it the test above would pass if
    `--collection` were ignored entirely."""
    doc = tmp_path / "collection.json"
    doc.write_text(
        json.dumps({"card_default": {"level": 20, "bloom": 4}, "cards": {}}),
        encoding="utf-8",
    )
    args = real_small_pool

    _code_off, off = _solve(capsys, [*args, "--no-collection"])
    _code_low, low = _solve(capsys, [*args, "--collection", str(doc)])

    assert _top_score(low) != _top_score(off)


@pytest.mark.skipif(not _CURVE.exists(), reason="dumps/card_level_curve.json is gitignored")
def test_an_absent_collection_file_is_not_an_error_and_changes_nothing(tmp_path, capsys, real_small_pool):
    """First-class state, the same contract `--board` has: an absent document
    keeps every card at `data/cards.json`'s value and exits 0."""
    args = real_small_pool

    code_off, off = _solve(capsys, [*args, "--no-collection"])
    code_absent, absent = _solve(capsys, [*args, "--collection", str(tmp_path / "nope.json")])

    assert code_off == 0 and code_absent == 0
    assert _top_score(absent) == _top_score(off)
    assert "COLLECTION: none" in absent


@pytest.mark.skipif(not _CURVE.exists(), reason="dumps/card_level_curve.json is gitignored")
@pytest.mark.skipif(not _CAPS_FILE.exists(), reason="dumps/card_caps.json is gitignored")
def test_the_json_payload_says_which_collection_the_run_used(tmp_path, capsys, real_small_pool):
    """A ranking scored against the account's real levels is a different answer
    from the maxed one, so the payload must carry which — the same reason the
    `board` key exists."""
    doc = tmp_path / "collection.json"
    doc.write_text(json.dumps({"card_default": MAX, "cards": {}}), encoding="utf-8")
    out = tmp_path / "payload.json"

    code, _ = _solve(
        capsys,
        [*real_small_pool, "--collection", str(doc), "--json", str(out)],
    )

    assert code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["collection"]["loaded"] is True
    assert payload["collection"]["cards_restated"] > 0
    assert payload["collection"]["path"] == str(doc)


@pytest.mark.skipif(not _CURVE.exists(), reason="dumps/card_level_curve.json is gitignored")
@pytest.mark.skipif(not _CAPS_FILE.exists(), reason="dumps/card_caps.json is gitignored")
def test_real_stats_supersedes_the_collection_and_says_so(tmp_path, capsys, real_small_pool):
    """`--real-stats` wins over `--collection`.

    `--real-stats` carries the account's REAL measured per-card stats; a
    collection COMPUTES them from level and bloom. Both answer the same
    question, and a measurement outranks a derivation. Applying the collection
    on top of `--real-stats` would silently overwrite the measurement and make
    a mode-B run and a maxed run return byte-identical rankings.

    Pinned here as well as in `test_mode_b.py` because that test asserts the
    two runs DIFFER, which is a symptom; this one names the rule.
    """
    from hbuilder import cli

    doc = tmp_path / "collection.json"
    doc.write_text(json.dumps({"card_default": MAX, "cards": {}}), encoding="utf-8")

    cards, basis = cli.apply_collection((), str(doc), superseded_by_real_stats=True)

    assert cards == ()
    assert basis.applied == 0
    assert basis.superseded_by_real_stats is True
    assert "supersedes" in basis.lines()[0]
    assert basis.payload()["superseded_by_real_stats"] is True


# ---------------------------------------------------------------------------
# 10. The two account-level terms: Memory Bonus and Member Power-Up
# ---------------------------------------------------------------------------


def test_memory_bonus_follows_the_master_ladder_and_caps_at_eighty():
    """From `PosterCollectEffect`: 50 rows, `permil(t) = min(2t, t+30)`
    with zero violations. The closed form is asserted across the whole domain
    rather than at three sample points."""
    assert MEMORY_BONUS_MAX_POSTERS == 50
    assert MEMORY_BONUS_CAP_PERMIL == 80

    for posters in range(1, MEMORY_BONUS_MAX_POSTERS + 1):
        assert memory_bonus_permil(posters) == min(2 * posters, posters + 30)

    # A real account value: 22 posters.
    assert memory_bonus_permil(22) == 44
    assert memory_bonus_permil(MEMORY_BONUS_MAX_POSTERS) == MEMORY_BONUS_CAP_PERMIL
    # Monotone, which is what makes a slider legible.
    values = [memory_bonus_permil(t) for t in range(0, 51)]
    assert values == sorted(values)


def test_memory_bonus_clamps_out_of_range_and_keeps_none_distinct_from_zero():
    """`None` is "the player has not said"; `0` is "the player said they
    have none". A UI that collapses them reports an assumption as a fact."""
    assert memory_bonus_permil(None) is None
    assert memory_bonus_permil(0) == 0
    assert memory_bonus_permil(-5) == 0
    assert memory_bonus_permil(9999) == MEMORY_BONUS_CAP_PERMIL


@pytest.mark.skipif(not _CURVE.exists(), reason="dumps/card_level_curve.json is gitignored")
def test_the_member_power_up_ceiling_is_derived_from_the_roster_not_typed():
    """The 50 % cap is the game's declared ceiling and is where the ladder is
    HEADED as cards ship. What is reachable TODAY is bounded by the roster, and
    that bound moves every time a card is added — so it is computed.

    The literal below is the current roster's SCALE and is expected to move
    when cards ship; the properties after it are not, and cannot be satisfied
    by a stale constant. Each new 5★ card adds exactly the ladder's own +8
    step, so the literal moving by a multiple of 8 per 5★ is the check that the
    roster grew rather than the derivation changing."""
    basis = _basis()
    reachable = max_member_power_up_permyriad(basis)

    # 1230 on a 201-card roster (1158 on 192: nine new 5★ cards, +8 each).
    assert reachable == 1230
    assert reachable < MEMBER_POWER_UP_CAP_PERMYRIAD == 5000

    # Derived, not typed: recomputing it by hand from the ladders must agree.
    by_hand = sum(
        basis.power_up_permyriad[entry[0]][max(basis.power_up_permyriad[entry[0]])]
        for entry in basis.cards.values()
        if entry[0] in basis.power_up_permyriad
    )
    assert reachable == min(by_hand, MEMBER_POWER_UP_CAP_PERMYRIAD)


@pytest.mark.skipif(not _CURVE.exists(), reason="dumps/card_level_curve.json is gitignored")
def test_the_power_up_ladders_are_the_three_the_master_defines():
    """27 groups collapse to exactly 3 ladders, purely (rarity,
    level), with per-card maxima 4 / 5 / 8 permyriad for 3*/4*/5*. If a fourth
    ladder ever appears the roster ceiling above is computed over a shape this
    project has never seen."""
    basis = _basis()
    assert basis.power_up_permyriad, "the curve export predates the power-up ladder"

    distinct = {tuple(sorted(lv.items())) for lv in basis.power_up_permyriad.values()}
    assert len(distinct) == 3, f"expected 3 ladders, found {len(distinct)}"
    assert sorted(max(v for _, v in lad) for lad in distinct) == [4, 5, 8]
    # Monotone in level, every ladder — a level-up may never LOWER the bonus.
    for ladder in distinct:
        values = [v for _, v in ladder]
        assert values == sorted(values)


def test_the_power_up_ceiling_is_none_without_a_curve():
    """First-class absent, the same contract as everything else here: no export
    means no ceiling, never a zero one."""
    assert max_member_power_up_permyriad(None) is None
    empty = stat_basis_from_mapping({"level_mult": {}, "cards": {}})
    assert max_member_power_up_permyriad(empty) is None


def test_the_player_block_parses_and_keeps_unset_distinct_from_zero():
    """`None` is "not stated", `0` is "stated as none"."""
    empty = account_bonuses_from_mapping({})
    assert empty.posters is None and empty.power_up_permyriad is None
    assert empty.memory_permil is None

    stated = account_bonuses_from_mapping({"player": {"posters": 0, "power_up_permyriad": 0}})
    assert stated.posters == 0 and stated.power_up_permyriad == 0
    assert stated.memory_permil == 0

    real = account_bonuses_from_mapping({"player": {"posters": 22, "power_up_permyriad": 35}})
    assert real.memory_permil == 44


def test_player_level_parses_and_unset_is_not_level_one():
    """`player.level` gates board node AVAILABILITY (127 of 152 nodes carry a
    `cd-player_level-N`). Unset must stay `None` so the editor can gate
    nothing, rather than collapsing to 1 and locking almost the whole board."""
    assert account_bonuses_from_mapping({}).level is None
    assert account_bonuses_from_mapping({"player": {"posters": 22}}).level is None
    assert account_bonuses_from_mapping({"player": {"level": 1}}).level == 1
    assert account_bonuses_from_mapping({"player": {"level": 30}}).level == 30
    assert account_bonuses_from_mapping({"player": {"level": PLAYER_LEVEL_MAX}}).level == (
        PLAYER_LEVEL_MAX
    )


def test_a_player_level_off_the_ladder_is_refused_not_clamped():
    """Clamping 500 down to 50 would unlock the whole board on a typo — the one
    direction where being wrong is invisible. 0 is off the ladder too: the
    ladder starts at 1, and `read()` floors negatives to 0, so both land here."""
    for bad in (0, 51, 500, -3):
        with pytest.raises(CollectionError, match="player.level"):
            account_bonuses_from_mapping({"player": {"level": bad}})


def test_a_non_numeric_player_value_raises_rather_than_defaulting():
    """Defaulting a typo to zero would report an unset account as a measured
    one, silently."""
    with pytest.raises(CollectionError, match="posters"):
        account_bonuses_from_mapping({"player": {"posters": "twenty-two"}})
    with pytest.raises(CollectionError, match="'player'"):
        account_bonuses_from_mapping({"player": ["not", "an", "object"]})


def test_memory_bonus_is_the_documented_approximation_and_rounds_up():
    """`ceil(total * R / 1000)`. Unset stays unset; 0 permil is a real 0."""
    assert memory_bonus(100_000, None) is None
    assert memory_bonus(100_000, 0) == 0
    # 20,362 is the captured Member Parameter of the decomposed deck; at the
    # account's 44 permil that is 896 (exact ceil), against a captured 903 — the
    # -7 the docstring records, and it is LOW, which is the documented sign.
    assert memory_bonus(20_362, 44) == 896
    assert 903 - memory_bonus(20_362, 44) == 7
    # Rounds up, never to nearest: 1 permil of 1001 is 1.001 -> 2.
    assert memory_bonus(1001, 1) == 2


def test_member_power_up_ceilings_per_member_not_on_the_total():
    """Five ceilings, not one. On five equal bases the difference is visible:
    one ceiling on the total would give 1, five give 5."""
    assert member_power_up([1000] * 5, None) is None
    assert member_power_up([1000] * 5, 0) == 0
    # 1000 * 1/10000 = 0.1 -> ceil 1, five times.
    assert member_power_up([1000] * 5, 1) == 5
    assert math.ceil(5000 * 1 / 10000) == 1  # the wrong form, for contrast

    # A real account's P=35 on the decomposed deck's per-member bases.
    bases = [10158, 11011, 9777, 10583, 8874]
    assert member_power_up(bases, 35) == sum(math.ceil(b * 35 / 10000) for b in bases)


_MP = "card_potential_parameter [Member Parameter]"
_OUTFIT = "up_by_live_leader_skill [Outfit Skill]"
_PASSIVE_LINE = "up_by_live_passive_skill [Passive Skill]"
_BOARD_LINE = "up_by_skill_tree [Holomem Board]"
_POWERUP = "card_training_evaluation [Member Power-Up]"


def _feasible_permyriads(rows, keys):
    """Per row, the integer `P` values the wire admits for a candidate base.

    `SUM over 5 slots of ceil(base_m * P / 10000)` is bounded by
    `base*P/10000 <= wire < base*P/10000 + 5` whatever the per-slot split is,
    because every ceiling adds strictly less than 1. An EMPTY set on any row
    refutes the base outright — no `P` can produce that line."""
    out = []
    for row in rows:
        power = row.get("power") or {}
        if _POWERUP not in power or _MP not in power:
            continue
        base = sum(power.get(key, 0) for key in keys)
        wire = power[_POWERUP]
        lo = (wire - 5) * 10000 / base
        hi = wire * 10000 / base
        out.append({p for p in range(int(lo), int(hi) + 2) if lo < p <= hi})
    return out


@pytest.mark.skipif(
    not _ORACLE.exists(), reason="dumps/ is gitignored account data"
)
def test_member_power_up_base_includes_the_outfit_skill_line():
    """⛔ The base is `MemberParameter + PassiveSkill + Outfit + HolomemBoard` —
    the OUTFIT is easy to leave out. The call site passes `effective_stat_sum`,
    which carries the costume's stat effect; this pins which base the SERVER
    uses so the documentation cannot drift from it.

    Non-circular in the same sense as
    `test_member_parameter_reproduces_the_servers_own_figure`:
    `card_training_evaluation` is the server's own line and every base here is
    built from the server's own other lines. Nothing in the comparison is a
    number this project computed.

    The `P` values are not asserted as single constants — `P` is a sum over the
    account's cards and their levels, so it MOVES when the account levels, and
    `deck_oracle.json` spans such events. What is asserted is the shape: with
    the outfit in the base every row admits a `P`, and only the few account
    states the file spans are needed."""
    rows = json.loads(_ORACLE.read_text(encoding="utf-8"))

    with_outfit = _feasible_permyriads(rows, (_MP, _PASSIVE_LINE, _OUTFIT, _BOARD_LINE))
    without_outfit = _feasible_permyriads(rows, (_MP, _PASSIVE_LINE, _BOARD_LINE))

    assert len(with_outfit) >= 30, "expected the captured decks"
    empty = [i for i, s in enumerate(with_outfit) if not s]
    assert not empty, f"rows with no feasible P under the outfit-inclusive base: {empty}"

    # The account states the file spans: {39, 41, 43}, each a genuine account
    # state recorded by a capture, not a fit drifting -- the set grows as the
    # account levels, which is exactly what a wire-anchored check should track
    # rather than freeze.
    admitted = sorted(set().union(*with_outfit))
    assert set(admitted) <= {39, 41, 43}, admitted
    assert all(s <= {39, 41, 43} for s in with_outfit)
    # Every state present is one the account actually held; the latest derived
    # value (43) must be among them.
    assert 43 in admitted and 39 in admitted

    # The negative control. Dropping the Outfit line does not merely fit worse:
    # on every row that HAS an outfit line it needs a different P, and on four
    # rows no integer P works at all. It survives only where the outfit is zero,
    # which is why the wrong base is easy to miss.
    contradicted = [
        index for index, s in enumerate(without_outfit) if not (s & {39, 41})
    ]
    assert len(contradicted) >= 10, (
        "dropping the Outfit line should contradict the account's own P on "
        f"real rows; it only did on {contradicted}"
    )
    assert any(not s for s in without_outfit), "expected outright-infeasible rows too"


# ---------------------------------------------------------------------------
# 11. The breakdown carries both terms, and the RANKING does not move
# ---------------------------------------------------------------------------


def _demo_team():
    return make_team([make_card(f"c{i}") for i in range(6)])


def test_an_unset_account_leaves_both_terms_none_not_zero():
    """"Not modelled" and "modelled and worth nothing" are different claims and
    the breakdown must be able to say which. `None` is the shipped default."""
    from hbuilder.domain.scoring import score_with_breakdown

    breakdown = score_with_breakdown(_demo_team(), make_scoring_config())

    assert breakdown.memory_bonus is None
    assert breakdown.member_power_up is None


def test_setting_the_account_fills_both_terms_and_moves_no_score():
    """The whole design in one assertion. Both terms are ranking-safe to drop,
    so they are REPORTED and never folded into `total_score` — the ranking
    stays bit-identical to what it is before the player supplies anything."""
    import dataclasses as dc

    from hbuilder.domain.scoring import score_with_breakdown

    team = _demo_team()
    plain = make_scoring_config()
    withacct = dc.replace(
        plain, account=AccountBonuses(posters=22, power_up_permyriad=35)
    )

    before = score_with_breakdown(team, plain)
    after = score_with_breakdown(team, withacct)

    assert after.total_score == before.total_score, (
        "supplying an account scalar moved the score — these two terms are "
        "reported, never summed into it"
    )
    assert after.memory_bonus is not None and after.memory_bonus > 0
    assert after.member_power_up is not None and after.member_power_up > 0
    # And they are the documented forms over the documented bases. Memory is
    # the BIT-EXACT per-member per-stat form (`memory_bonus_per_stat`), fed each
    # member's `Card.stats` — the same decomposition the scoring path passes it.
    per_member = [
        (m.stats.performance, m.stats.technique, m.stats.sense)
        for m in team.members
    ]
    assert after.memory_bonus == memory_bonus_per_stat(per_member, 44)
    # The exact form is at or above the whole-deck approximation, which only
    # ever runs low.
    assert after.memory_bonus >= memory_bonus(before.base_stat_sum, 44)
    # Member Power-Up is paid its FIVE per-slot ceilings (one per member),
    # NOT one ceiling over the aggregate — the server pays five and so does this
    # (`_score_team` plumbs each member's MemberParameter+Outfit+Passive+Board
    # subtotal). This demo team is five 300-stat members with no stat skills
    # and no board, so each member base is 300. HAND-COMPUTED:
    #   5 x ceil(300 * 35 / 10000) = 5 x ceil(1.05) = 5 x 2 = 10.
    per_member_bases = [
        float(m.stats.performance + m.stats.technique + m.stats.sense)
        for m in team.members
    ]
    assert after.member_power_up == member_power_up(per_member_bases, 35) == 10
    # The one-aggregate form is strictly lower:
    #   ceil(1500 * 35 / 10000) = ceil(5.25) = 6.
    assert member_power_up([before.effective_stat_sum], 35) == 6
    assert after.member_power_up > member_power_up([before.effective_stat_sum], 35)


def test_a_zero_account_is_reported_as_zero_not_as_absent():
    """The negative control for the `None` test: a player who says they have
    none must read as 0, distinct from never having said."""
    import dataclasses as dc

    from hbuilder.domain.scoring import score_with_breakdown

    zeroed = dc.replace(
        make_scoring_config(), account=AccountBonuses(posters=0, power_up_permyriad=0)
    )
    breakdown = score_with_breakdown(_demo_team(), zeroed)

    assert breakdown.memory_bonus == 0
    assert breakdown.member_power_up == 0
