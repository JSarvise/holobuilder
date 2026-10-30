"""`max_lattice` — the interval-partitioned ACTIVE E[max] combiner.

Measured against the server on a few hundred decks, the board-free Active
E[max] obeys a two-level law: ACTIVE score-ups of DIFFERENT cooldown interval
ADD (plain sum of `magnitude * occupancy`, to within ~1 permil), while ACTIVEs
of the SAME interval overlap on their shared cooldown lattice and take the
familiar within-bucket E[max] discount before saturating. The shipped `max`
model (`ExpectedMaximumActiveSkill`) pools EVERY same-bucket ACTIVE term into
one independent free-product walk, so it undershoots a different-interval pair
by the whole discount it should not have applied — the model's largest
systematic error (measured example: int17 + int33, server 138 permil ≈ the
139 permil sum of the two solos, shipped `max` gives 112).

`ExpectedMaximumActiveSkillByInterval` (name `"max_lattice"`) partitions the
terms by `(bucket, interval)`, runs the EXACT existing E[max] walk within each
group, and SUMS the group results — so different intervals add and same-interval
terms discount, which is the measured law. The within-group SATURATION /
truncation the server applies once several same-interval effects pile up
(measured ~2.1–2.7 window-widths, phase-dependent) is a KNOWN UNMODELED
residual: it is deliberately NOT hardcoded here, because it is not bit-exact and
a constant would risk introducing new errors elsewhere.

These are unit tests on the combiner with SYNTHETIC terms — non-circular: the
asserted numbers are derived by hand from the measured law, not read back from
the project's own scorer. They also pin the PARITY guardrail: the tuple-shape change
(adding the `interval` element) must leave `max` and `sum` bit-identical.
"""

from __future__ import annotations

import pytest

from hbuilder.domain.scoring import SCORE_BUCKET_ACTIVE
from hbuilder.domain.scoring_models import ACTIVE_STACKING_MODELS

MAX = ACTIVE_STACKING_MODELS["max"]
SUM = ACTIVE_STACKING_MODELS["sum"]
LATTICE = ACTIVE_STACKING_MODELS["max_lattice"]

A = SCORE_BUCKET_ACTIVE


def test_two_different_interval_actives_add_under_lattice_but_free_product_under_max():
    """Two independent ACTIVE score-ups on DIFFERENT cooldown lattices (int17,
    int33). The measured law says they ADD, because they roll on unsynchronised
    lattices and the server treats their windows as two separate maxima:

        lattice = 0.40 * 0.30  +  0.30 * 0.20  =  0.12 + 0.06  =  0.18   (plain sum)

    The shipped `max` pools both into ONE independent free-product walk and so
    discounts the smaller by the chance the larger is already live:

        max     = 0.40 * 0.30              (= 0.12, free -> 0.70)
                + 0.30 * 0.20 * 0.70       (= 0.042)
                = 0.162

    That 0.162 vs 0.18 gap — 18 permil low on this pair — is exactly the
    systematic undershoot `max_lattice` removes."""
    terms = [
        (A, 0.40, 0.30, 17, False),
        (A, 0.30, 0.20, 33, False),
    ]

    assert LATTICE.combine(terms) == pytest.approx(0.18)
    # The plain sum of the two solos, spelled out — the measured "different
    # intervals add" law.
    assert LATTICE.combine(terms) == pytest.approx(0.40 * 0.30 + 0.30 * 0.20)
    # The shipped model returns the smaller free-product value.
    assert MAX.combine(terms) == pytest.approx(0.162)
    assert MAX.combine(terms) < LATTICE.combine(terms)


def test_two_same_interval_actives_are_identical_under_lattice_and_max():
    """Same cooldown interval (both int17): they DO share a lattice, so the
    within-group E[max] discount applies exactly as the shipped model already
    computes it. `max_lattice` must equal `max` here — the one group it forms is
    the whole bucket:

        both = 0.40 * 0.30  +  0.30 * 0.20 * (1 - 0.30)  =  0.12 + 0.042  =  0.162
    """
    terms = [
        (A, 0.40, 0.30, 17, False),
        (A, 0.30, 0.20, 17, False),
    ]

    assert LATTICE.combine(terms) == pytest.approx(0.162)
    assert LATTICE.combine(terms) == pytest.approx(MAX.combine(terms))


def test_three_terms_across_two_intervals_sum_their_groups():
    """Three ACTIVEs: two share int17, one is alone on int33. The two int17
    terms discount each other; the int33 term adds on top:

        group int17 = 0.50 * 0.40  +  0.30 * 0.20 * (1 - 0.40)
                    = 0.20 + 0.036 = 0.236
        group int33 = 0.40 * 0.25 = 0.10
        lattice     = 0.236 + 0.10 = 0.336

    The shipped `max`, pooling all three into one walk (sorted 0.50/0.40/0.30),
    would give 0.20 + 0.40*0.25*0.60 + 0.30*0.20*0.45 = 0.287 — lower, because it
    discounts the int33 term against the int17 ones it never actually collides
    with."""
    terms = [
        (A, 0.50, 0.40, 17, False),
        (A, 0.30, 0.20, 17, False),
        (A, 0.40, 0.25, 33, False),
    ]

    assert LATTICE.combine(terms) == pytest.approx(0.336)
    assert MAX.combine(terms) == pytest.approx(0.287)
    assert LATTICE.combine(terms) > MAX.combine(terms)


def test_different_intervals_in_different_buckets_still_add():
    """The partition key is `(bucket, interval)`, so a bucket boundary and an
    interval boundary both split a group. Two ACTIVE-bucket terms on distinct
    intervals plus one on a third interval all add — there is no cross-bucket or
    cross-interval discount:

        0.40*0.30 + 0.30*0.20 + 0.25*0.40 = 0.12 + 0.06 + 0.10 = 0.28
    """
    terms = [
        (A, 0.40, 0.30, 17, False),
        (A, 0.30, 0.20, 33, False),
        (A, 0.25, 0.40, 21, False),
    ]

    assert LATTICE.combine(terms) == pytest.approx(0.28)


def test_interval_zero_terms_share_one_group_per_bucket_unchanged():
    """Terms whose interval is 0/None (SPECIAL, always-on) fall in a single
    per-bucket group and keep the shipped within-bucket behaviour. Two int-0
    terms in the ACTIVE bucket therefore discount each other exactly as `max`
    does — `max_lattice` changes nothing for them:

        0.40 * 0.30 + 0.30 * 0.20 * (1 - 0.30) = 0.162
    """
    terms = [
        (A, 0.40, 0.30, 0, False),
        (A, 0.30, 0.20, 0, False),
    ]

    assert LATTICE.combine(terms) == pytest.approx(0.162)
    assert LATTICE.combine(terms) == pytest.approx(MAX.combine(terms))


# --- PARITY: the tuple-shape change must not move `max` or `sum` --------------


def test_max_output_is_unchanged_by_the_interval_element():
    """`ExpectedMaximumActiveSkill` must ignore the `interval` element and
    return the SAME float as without it. Hand-computed against the classic
    independence walk, and asserted for both the 5-tuple shape and the 4-tuple
    other unit tests build.

        classic = 0.40*0.46 + 0.30*0.46*(1-0.46) + 0.22*0.31*... (descending)
    """
    pairs = [(0.40, 0.46), (0.30, 0.46), (0.22, 0.31), (0.55, 0.19)]
    classic = 0.0
    free = 1.0
    for magnitude, occupancy in sorted(pairs, key=lambda pair: -pair[0]):
        classic += magnitude * occupancy * free
        free *= 1.0 - occupancy

    five_tuple = [(A, m, o, 17, False) for m, o in pairs]
    four_tuple = [(A, m, o, False) for m, o in pairs]

    # Same number with or without interval — bit-for-bit (`==`, not approx).
    assert MAX.combine(five_tuple) == classic
    assert MAX.combine(four_tuple) == classic
    # And the interval element genuinely does not perturb the ACTIVE-bucket walk.
    assert MAX.combine(five_tuple) == MAX.combine(four_tuple)


def test_sum_output_is_unchanged_by_the_interval_element():
    """`SummedActiveSkills` sums `magnitude * occupancy` over every term,
    ignoring bucket, exclusivity AND interval. The interval element must not move it:

        0.40*0.46 + 0.30*0.20 + 0.22*0.31 = 0.184 + 0.06 + 0.0682 = 0.3122
    """
    five_tuple = [
        (A, 0.40, 0.46, 17, False),
        (A, 0.30, 0.20, 33, False),
        ("SPECIAL", 0.22, 0.31, 0, True),
    ]
    four_tuple = [(b, m, o, x) for b, m, o, _i, x in five_tuple]

    assert SUM.combine(five_tuple) == pytest.approx(0.3122)
    assert SUM.combine(five_tuple) == SUM.combine(four_tuple)


def test_max_lattice_is_registered_and_default_is_still_max():
    """`max_lattice` is opt-in only: it lives in the registry so a recon path can
    select it, but the ranking default stays `max`."""
    from hbuilder.domain.scoring_models import DEFAULT_ACTIVE_STACKING

    assert ACTIVE_STACKING_MODELS["max_lattice"].name == "max_lattice"
    assert DEFAULT_ACTIVE_STACKING.name == "max"
