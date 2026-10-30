"""The unrecorded-gate seam: a skill whose `condition` was never
transcribed must not be scored as if it were affirmatively ungated.

`condition` is three-state — a real `GateCondition`, a structurally
`None` (ungated) slot, and `UNKNOWN` for "never populated". `gates.py`'s
`_resolve_one_skill` deliberately collapses the last two to `gate = None`,
because an `UNKNOWN` condition must never reach `predicate_from_condition`.
Disambiguating them again is the *scoring* layer's job: otherwise an
unrecorded gate falls through the gate check, passes the known-fields check
(which only covers the EV arithmetic inputs), and contributes at full value.
That is an **over**estimate, and it breaks the `>=` lower-bound guarantee in
the one direction the notation cannot express (the non-negativity legs rest
on it).

Sibling coverage: `tests/test_lower_bound_invariant.py` pins the other three
non-negativity legs (card numerics, gate encoding, config weights). This file
pins the consumer-side leg — that a state recorded by `domain/gates.py` is
actually read by `domain/scoring.py`.
"""

from __future__ import annotations

from hbuilder.domain.card import GateCondition
from hbuilder.domain import scoring
from hbuilder.domain.scoring import _resolve_skill, score_card_set, score_with_breakdown
from hbuilder.domain.team import Team
from hbuilder.domain.unknown import UNKNOWN

from conftest import make_card, make_resolved, make_scoring_config, make_skill, make_team

# A fully-known ACTIVE slot, so `condition` is the only field under test.
_DURATION = 8.0
_INTERVAL = 10.0
_TIER = "Medium"
_MAGNITUDE = 20.0

# Every `make_card` default is type "Cute", so a 6-card team meets this.
_FIRING_CONDITION = GateCondition(kind="type", target="Cute", count=3)


def _active_slot(condition: object) -> object:
    """The same ACTIVE slot in every test, varying only `condition` — so a
    difference in outcome can only be attributable to that field."""
    return make_skill(
        "ACTIVE",
        duration=_DURATION,
        interval=_INTERVAL,
        probability_tier=_TIER,
        magnitude=_MAGNITUDE,
        condition=condition,
    )


def _team_whose_one_carrier_has_condition(condition: object):
    """A team in which exactly one MEMBER carries a scoring skill. Every other
    card is skill-less, so the team total moves only with that one skill's
    contribution. The carrier has to be a member: a leader's own ACTIVE slot
    is not emitted at all, so parked there it would
    contribute nothing whatever its condition said, and this file would pass
    while testing nothing."""
    carrier = make_card("carrier_01", active=_active_slot(condition))
    fillers = [make_card(f"filler_{i:02d}") for i in range(5)]
    return make_team([fillers[4], carrier, *fillers[:4]])


# --- Test 1: the per-skill path -------------------------------------------


def test_unknown_condition_resolves_to_data_missing_naming_the_condition_field():
    resolved = make_resolved("carrier_01", "ACTIVE", _active_slot(UNKNOWN), gate=None)
    cfg = make_scoring_config()

    assert _resolve_skill(resolved, cfg, 0.0) == (0.0, "data_missing", "condition"), (
        "CR-01: a skill whose gate condition was never transcribed must "
        "resolve to data_missing at rate 0.0, naming 'condition' as the "
        "missing field. Returning 'applied' with a non-zero rate scores an "
        "unrecorded gate as if it were an affirmatively ungated skill, "
        "which inflates the reported total — the one direction the '>=' "
        "lower-bound marker cannot express (D-00/D-01/D-03)"
    )


# --- Test 2: the control — the ungated case must not move ------------------


def test_structurally_ungated_condition_still_applies_at_full_value():
    resolved = make_resolved("carrier_01", "ACTIVE", _active_slot(None), gate=None)
    cfg = make_scoring_config()

    rate, state, missing_field = _resolve_skill(resolved, cfg, 0.0)

    assert state == "applied", (
        "an affirmatively ungated skill (condition structurally None, not "
        "UNKNOWN) must keep contributing at full value — CR-01's fix must "
        "narrow to the unrecorded case only, never to every gate-less skill"
    )
    assert missing_field is None
    assert rate > 0.0, (
        "the ungated control must produce a real non-zero rate, otherwise "
        "test 1 above would pass for the wrong reason"
    )


# --- Test 3: the end-to-end lower-bound comparison (the tracer) ------------


def test_team_with_unrecorded_condition_never_outscores_the_same_team_once_it_is_filled_in():
    cfg = make_scoring_config()
    assert cfg.family_weights["score_support"] > 0.0, (
        "fixture precondition: the carrier's family must carry a non-zero "
        "weight, or both teams below would score identically for a reason "
        "unrelated to CR-01"
    )

    unrecorded = score_with_breakdown(_team_whose_one_carrier_has_condition(UNKNOWN), cfg)
    filled_in = score_with_breakdown(
        _team_whose_one_carrier_has_condition(_FIRING_CONDITION), cfg
    )

    assert unrecorded.total_score < filled_in.total_score, (
        "CR-01, end to end: the same team must score strictly lower while "
        "its carrier's gate is unrecorded than once that gate is filled in "
        "and fires. Equality is exactly the defect — it means the "
        "unrecorded gate was counted at full value, so the reported total "
        "was an overestimate rather than the lower bound the '>=' marker "
        "promises (D-00/D-00b)"
    )
    assert unrecorded.incomplete_skills, (
        "the unrecorded-condition team must be tainted via "
        "Card.unknown_fields(), so the '>=' marker is actually rendered "
        "alongside the reduced total (D-05/D-06/D-20)"
    )


# --- Test A: the team-rate-fold leg ---------------------------------------


def _rate_up_slot(condition: object) -> object:
    """An ACTIVE `activation_rate_up` slot — the family `_fold_activation_
    rate_up` folds into the team-wide modifier rather than into any score
    pool."""
    return make_skill(
        "ACTIVE",
        family="activation_rate_up",
        effect_type="activation_rate",
        duration=_DURATION,
        interval=_INTERVAL,
        probability_tier=_TIER,
        magnitude=_MAGNITUDE,
        condition=condition,
    )


def _scorer_card():
    """A plain ungated ACTIVE score skill — the bystander whose rate must
    not be inflated by an unrecorded rate-up skill elsewhere on the team."""
    return make_card("scorer_01", active=_active_slot(None))


def _team_with_and_without_rate_up(condition: object):
    """The same six-card team twice: once carrying an `activation_rate_up`
    skill with the given condition, once with that carrier swapped for a
    skill-less filler. Everything else is identical."""
    # Both the rate-up carrier and the scorer are MEMBERS; the outfit slot
    # takes a skill-less card so it contributes nothing either way.
    with_rate_up = make_team(
        [
            make_card("outfit_01"),
            make_card("rateup_01", active=_rate_up_slot(condition)),
            _scorer_card(),
            *[make_card(f"filler_{i:02d}") for i in range(3)],
        ]
    )
    without_rate_up = make_team(
        [
            make_card("outfit_01"),
            make_card("rateup_01"),
            _scorer_card(),
            *[make_card(f"filler_{i:02d}") for i in range(3)],
        ]
    )
    return with_rate_up, without_rate_up


def _contribution_for(breakdown, card_id: str):
    matches = [c for c in breakdown.contributions if c.card_id == card_id]
    assert len(matches) == 1, f"expected exactly one contribution for {card_id!r}"
    return matches[0]


def test_unrecorded_condition_on_a_rate_up_skill_inflates_no_other_skill_on_the_team():
    cfg = make_scoring_config()
    with_rate_up, without_rate_up = _team_with_and_without_rate_up(UNKNOWN)

    folded = score_with_breakdown(with_rate_up, cfg)
    baseline = score_with_breakdown(without_rate_up, cfg)

    assert folded.team_rate_up == 0.0, (
        "CR-01 leg 2: an activation_rate_up skill whose gate was never "
        "transcribed must be skipped by _fold_activation_rate_up, leaving "
        "team_rate_up exactly at the value it would have had without that "
        "skill — compared with == because D-17 makes this a structural "
        "skip, not a numeric approximation"
    )
    assert _contribution_for(folded, "scorer_01").rate == (
        _contribution_for(baseline, "scorer_01").rate
    ), (
        "the bystander assertion, and the one that actually proves the "
        "defect is gone: an unrecorded rate-up gate must leave EVERY other "
        "ACTIVE skill's rate untouched. Checking only that the rate-up "
        "skill's own contribution reads zero would miss the inflation it "
        "spreads across the rest of the team through team_rate_up"
    )

    rate_up_contribution = _contribution_for(folded, "rateup_01")
    assert rate_up_contribution.state == "data_missing"
    assert rate_up_contribution.missing_field == "condition"


# --- Test B: the solver's batch entry point -----------------------------


def test_batch_path_and_breakdown_path_agree_bit_for_bit_on_an_unrecorded_condition():
    cfg = make_scoring_config()
    with_rate_up, _ = _team_with_and_without_rate_up(UNKNOWN)
    members = with_rate_up.members
    leaders = (with_rate_up.leader, *members)

    leader_scores = score_card_set(members, leaders, cfg)

    for leader_index, leader in enumerate(leaders):
        team = Team(leader=leader, members=members)
        assert leader_scores[leader_index].total_score == (
            score_with_breakdown(team, cfg).total_score
        ), (
            "score_card_set is what the solver's hot loop actually calls, "
            "so a CR-01 fix that only held on the score_with_breakdown "
            f"path would leave every ranked run still wrong (leader "
            f"{leader.id!r}). Compared with == and never math.isclose: "
            "D-18 makes SCOR-09 parity a structural guarantee, and a "
            "tolerance comparison would silently convert it into a "
            "numeric one"
        )


# --- Test C: the structural pin on the shared oracle ----------------------


def test_condition_stays_in_the_shared_required_known_fields_oracle():
    assert "condition" in scoring._REQUIRED_KNOWN_FIELDS, (
        "removing 'condition' from _REQUIRED_KNOWN_FIELDS re-opens CR-01. "
        "Both _resolve_skill and _fold_activation_rate_up reach their "
        "known-fields decision through this one list, so a skill whose "
        "gate was never transcribed would once again be scored as if it "
        "were affirmatively ungated — inflating the reported total in the "
        "one direction the '>=' lower-bound marker cannot express"
    )
