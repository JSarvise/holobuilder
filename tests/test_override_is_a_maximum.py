"""A fired override does not REPLACE the base magnitude — it competes with it,
and the game keeps the larger.

A live trace of the client shows that a skill carrying a `SlotOverride`
registers **two effect objects**, both LIVE at the same notes, each returning
its own fixed magnitude, and the binary's
`ActiveSkillEffectDataCollection::CalcScoreUpRatio` combines them with
`MathF.Max`. Two independent decks show it as equal call counts for one
skill's two magnitudes:

    Nene   0.6 / 1.2   486 calls each
    Botan  0.35 / 0.65 388 calls each

On every card the game ships today the override is an UPGRADE, so replacing
the base and maximising give the same number. They stop agreeing the moment an
override is worth LESS than its base — then the game keeps paying the base
(both effects are live, `MathF.Max` picks the bigger), and a model that
replaced the base would quietly pay the smaller one and under-value the card.

This is written from the measurement rather than from a card that exists,
because a rule read off the game's own behaviour should not wait for the
catalogue to ship a counter-example before it is correct.
"""

from __future__ import annotations

from hbuilder.domain.card import GateCondition, SlotOverride
from hbuilder.domain.gates import GateResolution, predicate_from_condition
from hbuilder.domain.scoring import _effective_magnitude

from conftest import make_resolved, make_skill

_MET = GateCondition(kind="run_state", target="life", count=600, operator="gte")


def _resolved(base: float, override: float | None, *, fired: bool = True):
    skill = make_skill(
        "ACTIVE",
        family="conditional_score_up",
        effect_type="score",
        duration=10.0,
        interval=20.0,
        probability_tier="High",
        magnitude=base,
        override=None if override is None
        else SlotOverride(magnitude=override, condition=_MET),
    )
    gate = None
    if override is not None:
        gate = GateResolution(
            predicate=predicate_from_condition(_MET),
            fired=fired,
            actual_count=1000 if fired else 0,
        )
    return make_resolved("card", "ACTIVE", skill, override_gate=gate)


def test_an_override_that_beats_its_base_is_the_one_paid():
    """The ordinary case, and every card the game ships today."""
    assert _effective_magnitude(_resolved(50.0, 100.0)) == 100.0


def test_no_override_pays_the_base():
    assert _effective_magnitude(_resolved(50.0, None)) == 50.0


def test_an_unfired_override_pays_the_base():
    assert _effective_magnitude(_resolved(50.0, 100.0, fired=False)) == 50.0


def test_an_override_WORSE_than_its_base_does_not_lower_the_card():
    """The one the measurement forces. Both effects are live at once and the
    combine is `MathF.Max`, so a fired override worth LESS than the base cannot
    take the card below its base — the base effect is still there paying 50.

    Under an "the override replaces the base" reading this would return 30 and
    under-value the card by 40 %."""
    assert _effective_magnitude(_resolved(50.0, 30.0)) == 50.0


def test_an_override_equal_to_its_base_is_a_no_op():
    assert _effective_magnitude(_resolved(50.0, 50.0)) == 50.0
