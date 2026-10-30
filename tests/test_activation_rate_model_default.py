"""The shipped `activation_rate` model is `multiplicative`, and `pp` is REFUTED.

`Vision.LiveCore!ActiveSkillEffectDataCollection::CalcActivationProbabilityPermil`
@ rva `0x5efcf00` ends in

    addss xmm7,1000.0 ; addss xmm7,bonuses ; mulss xmm7,xmm0 ; divss xmm7,1000.0

— the accumulated bonuses are added to 1000 per-mille and the sum then
**multiplies** the tier held in `xmm0` before the divide by 1000. The bonus
*scales* the tier. It is not added in percentage points.

Three things are pinned here, all with numbers rather than directions:

1. **The default is `multiplicative`**, in `config.json` and in
   `scoring_config.DEFAULT_CONFIG` alike, and it is not `pp`. Both
   halves are asserted, so flipping either one to `pp` fails.
2. **The switch is a numerical no-op on today's `data/cards.json`**, and it is
   a no-op for a *reason that can stop being true*: the collection holds
   **zero** ACTIVE-kind `activation_rate_up` principal slots, so
   `scoring._fold_activation_rate_up` returns exactly `0.0` for every team and
   both models are handed a rate-up of 0.0, where they agree identically.
   Verified by scoring real teams under both models, not assumed.
3. **The divergence the switch will produce once the SPECIAL rider IS folded**
   — 60 of 334 `LiveSpecialSkillLevel` rows over 30 cards, at 350-550
   per-mille. The two models differ by exactly
   `1 / tier` on the SIZE of the bonus: `pp` overstates it **2.7027x at Low
   0.37** and **1.8182x at High 0.55**. A tier-dependent error REORDERS teams;
   it does not merely offset them. That reorder is pinned below with an
   explicit worked example so it is documented rather than discovered.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from hbuilder.domain.scoring import score_fast
from hbuilder.domain.scoring_config import DEFAULT_CONFIG, load_scoring_config
from hbuilder.domain.scoring_models import ACTIVATION_RATE_MODELS

from conftest import make_card, make_scoring_config, make_skill, make_team

REPO_ROOT = Path(__file__).resolve().parents[1]

# The three tiers from the game binary (`LiveActiveSkillLevel.f8` / 1000).
LOW, MEDIUM, HIGH = 0.37, 0.46, 0.55


def test_shipped_default_activation_rate_model_is_multiplicative_not_pp():
    """Pins both the in-code default and `config.json`. Asserting that `"pp"`
    is absent is what makes a silent revert fail rather than pass."""
    assert DEFAULT_CONFIG["model"]["activation_rate"] == "multiplicative"
    assert DEFAULT_CONFIG["model"]["activation_rate"] != "pp"

    shipped = json.loads((REPO_ROOT / "config.json").read_text(encoding="utf-8"))
    assert shipped["model"]["activation_rate"] == "multiplicative"
    assert shipped["model"]["activation_rate"] != "pp"

    cfg = load_scoring_config(REPO_ROOT / "config.json")
    assert cfg.activation_rate_model.name == "multiplicative"


def test_pp_overstates_the_bonus_by_exactly_one_over_the_tier():
    """The divergence, by hand, at every real tier.

    `pp.apply(tier, r)  = min(1, tier + r)`      -> bonus = r
    `mul.apply(tier, r) = min(1, tier * (1+r))`  -> bonus = tier * r

    so `pp_bonus / mul_bonus = 1 / tier`, independent of `r` while neither
    clamps. `1/0.37 = 2.7027027...`, `1/0.46 = 2.1739130...`,
    `1/0.55 = 1.8181818...`.

    Worked at r = 0.35, the smallest real SPECIAL rider (350 per-mille):

        Low   : pp 0.37+0.35 = 0.7200   mul 0.37*1.35 = 0.4995
        Medium: pp 0.46+0.35 = 0.8100   mul 0.46*1.35 = 0.6210
        High  : pp 0.55+0.35 = 0.9000   mul 0.55*1.35 = 0.7425
    """
    pp = ACTIVATION_RATE_MODELS["pp"]
    mul = ACTIVATION_RATE_MODELS["multiplicative"]

    rate_up = 0.35
    assert round(pp.apply(LOW, rate_up), 10) == 0.72
    assert round(mul.apply(LOW, rate_up), 10) == 0.4995
    assert round(pp.apply(MEDIUM, rate_up), 10) == 0.81
    assert round(mul.apply(MEDIUM, rate_up), 10) == 0.621
    assert round(pp.apply(HIGH, rate_up), 10) == 0.9
    assert round(mul.apply(HIGH, rate_up), 10) == 0.7425

    for tier, expected_ratio in (
        (LOW, 2.7027027027027026),
        (MEDIUM, 2.1739130434782608),
        (HIGH, 1.8181818181818181),
    ):
        for r in (0.35, 0.40, 0.45):
            pp_bonus = pp.apply(tier, r) - tier
            mul_bonus = mul.apply(tier, r) - tier
            assert abs(pp_bonus / mul_bonus - expected_ratio) < 1e-12, (
                f"tier={tier} rate_up={r}"
            )


def test_the_tier_dependent_error_reorders_two_skills_rather_than_offsetting_them():
    """A reorder, not an offset — the reason the default matters.

    Two ACTIVE skills, one Low-tier with magnitude 90 and one High-tier with
    magnitude 60, under a team rate-up of 0.45:

        pp  : Low  min(1, 0.37+0.45) = 0.82 -> 0.82*90 = 73.80
              High min(1, 0.55+0.45) = 1.00 -> 1.00*60 = 60.00   Low WINS
        mul : Low  0.37*1.45 = 0.5365       -> 0.5365*90 = 48.285
              High 0.55*1.45 = 0.7975       -> 0.7975*60 = 47.850  Low still wins,
                                                                   by 0.435 not 13.80

    and at magnitude 62 for the High skill the two models disagree on the
    ORDER outright:

        pp  : 0.82*90 = 73.80  vs 1.00*62 = 62.00   -> Low
        mul : 0.5365*90 = 48.285 vs 0.7975*62 = 49.445 -> High
    """
    pp = ACTIVATION_RATE_MODELS["pp"]
    mul = ACTIVATION_RATE_MODELS["multiplicative"]
    rate_up = 0.45

    low_ev_pp = pp.apply(LOW, rate_up) * 90
    high_ev_pp = pp.apply(HIGH, rate_up) * 62
    low_ev_mul = mul.apply(LOW, rate_up) * 90
    high_ev_mul = mul.apply(HIGH, rate_up) * 62

    assert round(low_ev_pp, 10) == 73.8
    assert round(high_ev_pp, 10) == 62.0
    assert round(low_ev_mul, 10) == 48.285
    assert round(high_ev_mul, 10) == 49.445

    assert low_ev_pp > high_ev_pp  # pp ranks the Low-tier skill first
    assert low_ev_mul < high_ev_mul  # multiplicative ranks the High-tier first


def _cfg(model_key: str):
    return dataclasses.replace(
        make_scoring_config(), activation_rate_model=ACTIVATION_RATE_MODELS[model_key]
    )


def _active_scorer(card_id: str, member: str, tier: str, magnitude: float):
    """One ACTIVE Score UP at `tier`, duty 10/10 so uptime is exactly 1.0 and
    the score is `apply(tier, rate_up) * _as_rate(magnitude)` with nothing
    else in the way."""
    return make_card(
        card_id,
        member=member,
        active=make_skill(
            "ACTIVE",
            family="conditional_score_up",
            effect_type="score",
            duration=10.0,
            interval=10.0,
            probability_tier=tier,
            magnitude=magnitude,
        ),
    )


def _rate_up_carrier(card_id: str, member: str, magnitude: float):
    """A principal ACTIVE `activation_rate_up` slot — the only shape
    `_fold_activation_rate_up` reads. `data/cards.json` holds none of these
    (0 principals, 75 slots carrying an `activation_rate_up` RIDER, which the
    fold deliberately does not read).
    Built synthetically here because `tests/` may not couple to `data/`
    (`test_fixture_isolation.py`)."""
    return make_card(
        card_id,
        member=member,
        active=make_skill(
            "ACTIVE",
            family="activation_rate_up",
            effect_type="activation_rate",
            duration=10.0,
            interval=10.0,
            probability_tier="Medium",
            magnitude=magnitude,
        ),
    )


def test_the_two_models_are_bit_identical_when_no_card_folds_a_rate_up():
    """The no-op, and the exact condition under which it holds.

    With no principal `activation_rate_up` slot on the team,
    `_fold_activation_rate_up` returns `0.0`, both models compute
    `apply(tier, 0.0) == tier`, and the totals agree bit-for-bit. This is
    why the choice of model moves no ranking on the current catalogue: 3,000
    random legal teams built from a real 167-card collection score
    bit-identically under both.
    """
    team = make_team(
        [
            _active_scorer("lead", "Ina", "Low", 40.0),
            _active_scorer("m1", "Nene", "Low", 90.0),
            _active_scorer("m2", "Marine", "High", 62.0),
            make_card("m3", member="Lui"),
            make_card("m4", member="Calli"),
            make_card("m5", member="Aki"),
        ]
    )
    pp_total = score_fast(team, _cfg("pp"))
    mul_total = score_fast(team, _cfg("multiplicative"))
    assert pp_total == mul_total
    assert pp_total > 0.0


def test_once_a_rate_up_is_folded_the_two_models_disagree_and_reorder():
    """The same two teams, scored end to end, once a rate-up IS folded.

    A carrier granting a 45 % rate-up sits on both teams; the teams differ
    only in whether the Score UP beside it is Low/90 or High/62 — the
    magnitudes `test_the_tier_dependent_error_reorders_...` picked because the
    two models order them oppositely. The end-to-end totals must therefore
    order the two TEAMS oppositely as well, which is the whole content of
    "tier-dependent error reorders rather than offsets".
    """
    carrier = _rate_up_carrier("carrier", "Nene", 45.0)
    filler = (make_card("f1", member="Lui"), make_card("f2", member="Calli"))
    low_team = make_team(
        [
            make_card("lead", member="Ina"),
            carrier,
            _active_scorer("low", "Marine", "Low", 90.0),
            *filler,
            make_card("f3", member="Aki"),
        ]
    )
    high_team = make_team(
        [
            make_card("lead", member="Ina"),
            carrier,
            _active_scorer("high", "Marine", "High", 62.0),
            *filler,
            make_card("f3", member="Aki"),
        ]
    )

    pp_low = score_fast(low_team, _cfg("pp"))
    pp_high = score_fast(high_team, _cfg("pp"))
    mul_low = score_fast(low_team, _cfg("multiplicative"))
    mul_high = score_fast(high_team, _cfg("multiplicative"))

    # The models genuinely differ here — this is what the no-op test above
    # would look like the moment the SPECIAL rider is folded.
    assert pp_low != mul_low
    assert pp_high != mul_high

    # ... and they order the two teams OPPOSITELY.
    assert pp_low > pp_high
    assert mul_low < mul_high
