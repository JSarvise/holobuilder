"""The `condition` encoding. Real card text puts
the gate in the second sentence of the skill description — "For 9s, Score
UP 50%. ... When LIFE is 600 or higher, Score UP 100%." — so transcribing
sentence one and stopping is the single most likely data-entry error. If
`"condition": null` meant "ungated", the skill would contribute at full
value, and a card whose gate simply hadn't been typed in yet would be
byte-identical to a genuinely ungated one.

So `null` means UNKNOWN (taints the card, exactly like
`duration`/`interval`/`magnitude`), and `{"kind": "ungated"}` is the
affirmative encoding for a skill with no gate at all. `target`/`count`
must be structurally absent for `{"kind": "ungated"}` — present-anyway is
a loud schema failure, not a third state.
"""

from __future__ import annotations

import json

import pytest

from hbuilder.domain.card import card_from_record
from hbuilder.domain.gates import RunState, resolve_team_gates
from hbuilder.domain.loader import load_cards
from hbuilder.domain.schema import (
    CONDITION_KINDS,
    NOT_APPLICABLE_CONDITION_FIELDS_BY_CONDITION_KIND,
    SCHEMA_VERSION,
    SchemaError,
    UNGATED_CONDITION_KIND,
    validate_skill_slot,
)
from hbuilder.domain.scoring import team_taint
from hbuilder.domain.team import MEMBER_COUNT, Team
from hbuilder.domain.unknown import UNKNOWN

_RUN_STATE = RunState(life=1000, combo=100)


def _active_slot(**overrides: object) -> dict:
    slot = {
        "family": "conditional_score_up",
        "effect_type": "score",
        "duration": 8,
        "interval": 15,
        "probability_tier": "Medium",
        "magnitude": 18,
        "condition": None,
        "target": {"kind": "all"},
        "override": {"kind": "none"},
        "rider": {"kind": "none"},
    }
    slot.update(overrides)
    return slot


def _card_record(
    *, card_id: str, active_condition: object, type_: str = "Cute"
) -> dict:
    """A minimal, schema-shaped card record whose ACTIVE slot carries the
    given `condition` value directly (may be `None`, a populated dict, or
    an `{"kind": "ungated", ...}` dict under test)."""
    return {
        "id": card_id,
        # A distinct holomem per card: R1 forbids two skins of one character
        # among the members, so a shared name would make every team invalid
        # for a reason this file is not about.
        "member": f"Member {card_id}",
        "generation": ["5期生"],
        "type": type_,
        "rarity": "SSR",
        "costume_name": "Test Costume",
        "stats": {"performance": 100, "technique": 100, "sense": 100},
        "skills": {
            "SPECIAL": None,
            "ACTIVE": _active_slot(condition=active_condition),
            "PASSIVE": None,
        },
        "costume_skill": None,
    }


def _write_cards_document(tmp_path, *card_records: dict):
    path = tmp_path / "cards.json"
    doc = {"schema_version": SCHEMA_VERSION, "cards": list(card_records)}
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _ungated_filler_card(card_id: str, type_: str = "Pure") -> object:
    return card_from_record(
        _card_record(
            card_id=card_id,
            active_condition={"kind": UNGATED_CONDITION_KIND},
            type_=type_,
        )
    )


# --- null taints, {"kind": "ungated"} does not ------------------------------


def test_null_condition_appears_in_unknown_fields():
    # null means UNKNOWN, not "ungated" — it must taint.
    record = _card_record(card_id="untranscribed_01", active_condition=None)
    assert validate_skill_slot(record["skills"]["ACTIVE"], "untranscribed_01", "ACTIVE") == []
    card = card_from_record(record)
    assert "skills.ACTIVE.condition" in card.unknown_fields()
    assert card.skill("ACTIVE").condition is UNKNOWN


def test_ungated_kind_condition_does_not_appear_in_unknown_fields():
    # The affirmative marker is not tainting — it is a real assertion
    # that the skill has no gate.
    record = _card_record(
        card_id="ungated_01", active_condition={"kind": UNGATED_CONDITION_KIND}
    )
    assert validate_skill_slot(record["skills"]["ACTIVE"], "ungated_01", "ACTIVE") == []
    card = card_from_record(record)
    assert "skills.ACTIVE.condition" not in card.unknown_fields()
    assert card.skill("ACTIVE").condition is None


def test_null_condition_taints_the_team(tmp_path):
    path = _write_cards_document(
        tmp_path, _card_record(card_id="untranscribed_01", active_condition=None)
    )
    cards = load_cards(path)
    # The card under test must be a MEMBER: taint, gates and EV are all
    # computed over the five members, and a leader's own slots are never
    # emitted.
    members = (
        cards[0],
        *(_ungated_filler_card(f"filler_{i}") for i in range(2, MEMBER_COUNT + 1)),
    )
    team = Team(leader=_ungated_filler_card("outfit_01"), members=members)

    assert "untranscribed_01:skills.ACTIVE.condition" in team_taint(team)


# --- target/count structurally absent for ungated ---------------------------


def test_ungated_with_target_present_is_rejected_naming_condition_target():
    record = _card_record(
        card_id="bad_ungated_01",
        active_condition={"kind": UNGATED_CONDITION_KIND, "target": "Cute"},
    )
    errors = validate_skill_slot(record["skills"]["ACTIVE"], "bad_ungated_01", "ACTIVE")
    assert any("condition.target" in e and "bad_ungated_01" in e for e in errors)


def test_ungated_with_count_present_is_rejected_naming_condition_count():
    record = _card_record(
        card_id="bad_ungated_02",
        active_condition={"kind": UNGATED_CONDITION_KIND, "count": 2},
    )
    errors = validate_skill_slot(record["skills"]["ACTIVE"], "bad_ungated_02", "ACTIVE")
    assert any("condition.count" in e and "bad_ungated_02" in e for e in errors)


def test_ungated_with_count_one_raises_schema_error_through_load_cards_naming_condition_count(
    tmp_path,
):
    # Behavioral, end-to-end: a hand-built record with a stray count on an
    # ungated condition must raise SchemaError, not merely fail validate_*.
    path = _write_cards_document(
        tmp_path,
        _card_record(
            card_id="bad_ungated_03",
            active_condition={"kind": UNGATED_CONDITION_KIND, "count": 1},
        ),
    )
    with pytest.raises(SchemaError) as exc_info:
        load_cards(path)
    assert "condition.count" in str(exc_info.value)


def test_condition_key_absent_entirely_is_still_a_schema_error():
    slot = _active_slot()
    del slot["condition"]
    errors = validate_skill_slot(slot, "card_missing_condition_01", "ACTIVE")
    assert any(
        "condition" in e and "card_missing_condition_01" in e for e in errors
    )


# --- real condition kinds still resolve and fire exactly as before ----------


def test_type_kind_condition_still_resolves_and_fires_exactly_as_before():
    condition = {"kind": "type", "target": "Cute", "count": 2}
    leader = card_from_record(
        _card_record(card_id="gated_carrier", active_condition=condition, type_="Cute")
    )
    members = (
        leader,                                    # carrier + m1 = 2 Cute -> fires
        _ungated_filler_card("m1", type_="Cute"),
        _ungated_filler_card("m2", type_="Pure"),
        _ungated_filler_card("m3", type_="Pure"),
        _ungated_filler_card("m4", type_="Pure"),
    )
    # The outfit card is Pure and OUTSIDE the five, so it cannot be mistaken
    # for the second Cute the gate needs.
    team = Team(leader=_ungated_filler_card("outfit_01", type_="Pure"), members=members)

    resolved = resolve_team_gates(team, _RUN_STATE)
    gated = next(r for r in resolved if r.card_id == leader.id and r.slot_kind == "ACTIVE")
    assert gated.gate is not None
    assert gated.gate.fired
    assert gated.gate.actual_count == 2


# --- import-time structural assertion ---------------------------------------


def test_not_applicable_condition_fields_key_set_equals_condition_kinds_plus_ungated():
    assert set(NOT_APPLICABLE_CONDITION_FIELDS_BY_CONDITION_KIND) == set(CONDITION_KINDS) | {
        UNGATED_CONDITION_KIND
    }
