"""The shipped `activation_rate` model is `multiplicative`.

`Vision.LiveCore!ActiveSkillEffectDataCollection::CalcActivationProbabilityPermil`
@ rva `0x5efcf00` ends in

    addss xmm7,1000.0 ; addss xmm7,bonuses ; mulss xmm7,xmm0 ; divss xmm7,1000.0

— the accumulated bonuses are added to 1000 per-mille and the sum then
**multiplies** the tier held in `xmm0` before the divide by 1000. The bonus
*scales* the tier: `apply(tier, r) = min(1, tier * (1 + r))`, so the size of
the bonus is `tier * r` and depends on the tier.

Pinned here, with numbers rather than directions:

1. **The default is `multiplicative`**, in `config.json` and in
   `scoring_config.DEFAULT_CONFIG` alike, and it is the only activation-rate
   model the registry carries.
2. **The bonus scales the tier** at every real tier.
3. **A tier-dependent bonus orders skills by `tier * (1 + r) * magnitude`**,
   both by hand and end to end through `score_fast` once a principal
   `activation_rate_up` slot is folded.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from support.builders import make_card, make_scoring_config, make_skill, make_team

from hbuilder.domain.scoring import score_fast
from hbuilder.domain.scoring_config import DEFAULT_CONFIG, load_scoring_config
from hbuilder.domain.scoring_models import ACTIVATION_RATE_MODELS

REPO_ROOT = Path(__file__).resolve().parents[1]

# The three tiers from the game binary (`LiveActiveSkillLevel.f8` / 1000).
LOW, MEDIUM, HIGH = 0.37, 0.46, 0.55


def test_shipped_default_activation_rate_model_is_multiplicative():
    """Pins the in-code default, `config.json` and the registry contents."""
    assert DEFAULT_CONFIG["model"]["activation_rate"] == "multiplicative"
    assert set(ACTIVATION_RATE_MODELS) == {"multiplicative"}

    shipped = json.loads((REPO_ROOT / "config.json").read_text(encoding="utf-8"))
    assert shipped["model"]["activation_rate"] == "multiplicative"

    cfg = load_scoring_config(REPO_ROOT / "config.json")
    assert cfg.activation_rate_model.name == "multiplicative"


def test_the_rate_up_scales_the_tier():
    """`apply(tier, r) = min(1, tier * (1 + r))`, so the bonus is `tier * r`.

    Worked at r = 0.35, the smallest real SPECIAL rider (350 per-mille):

        Low    0.37*1.35 = 0.4995
        Medium 0.46*1.35 = 0.6210
        High   0.55*1.35 = 0.7425
    """
    mul = ACTIVATION_RATE_MODELS["multiplicative"]

    rate_up = 0.35
    assert round(mul.apply(LOW, rate_up), 10) == 0.4995
    assert round(mul.apply(MEDIUM, rate_up), 10) == 0.621
    assert round(mul.apply(HIGH, rate_up), 10) == 0.7425

    for tier in (LOW, MEDIUM, HIGH):
        for r in (0.35, 0.40, 0.45):
            bonus = mul.apply(tier, r) - tier
            assert abs(bonus - tier * r) < 1e-12, f"tier={tier} rate_up={r}"


def test_a_tier_dependent_bonus_orders_two_skills_by_scaled_tier():
    """Two ACTIVE skills, one Low-tier with magnitude 90 and one High-tier with
    magnitude 62, under a team rate-up of 0.45:

        Low  0.37*1.45 = 0.5365 -> 0.5365*90 = 48.285
        High 0.55*1.45 = 0.7975 -> 0.7975*62 = 49.445  -> High ranks first
    """
    mul = ACTIVATION_RATE_MODELS["multiplicative"]
    rate_up = 0.45

    low_ev = mul.apply(LOW, rate_up) * 90
    high_ev = mul.apply(HIGH, rate_up) * 62

    assert round(low_ev, 10) == 48.285
    assert round(high_ev, 10) == 49.445
    assert low_ev < high_ev


def _cfg(model_key: str):
    return dataclasses.replace(make_scoring_config(), activation_rate_model=ACTIVATION_RATE_MODELS[model_key])


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


def test_a_folded_rate_up_orders_two_teams_by_scaled_tier():
    """The same two teams, scored end to end, once a rate-up IS folded.

    A carrier granting a 45 % rate-up sits on both teams; the teams differ
    only in whether the Score UP beside it is Low/90 or High/62 — the
    magnitudes of `test_a_tier_dependent_bonus_orders_...`. The end-to-end
    totals order the two TEAMS the same way the hand computation orders the
    two skills.
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

    mul_low = score_fast(low_team, _cfg("multiplicative"))
    mul_high = score_fast(high_team, _cfg("multiplicative"))

    assert mul_low > 0.0
    assert mul_low < mul_high
