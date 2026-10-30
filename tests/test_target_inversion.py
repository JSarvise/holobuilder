"""The `target` inversion, the sibling of the `condition` inversion. An
effect may reach fewer than all six cards: a targeted-but-gateless PASSIVE
("Grants Technique UP 14% to 3 Cute Type Members", no gate) must not be
byte-identical on disk to a genuinely team-wide skill, or it would be scored
as though it reached the whole team.

`target` therefore carries the same three-state contract `condition`
carries: `null` is UNKNOWN and taints (`Card.unknown_fields()`,
`data_missing`, never a whole-team contribution at full value —
overstating is the one direction the `>=` lower-bound marker cannot
express), `{"kind": "all"}` is the affirmative whole-team marker (never
inferred from a missing key), and any other populated dict is a real
`EffectTarget`.
"""

from __future__ import annotations

import json

import pytest

from hbuilder.domain.card import EffectTarget, card_from_record
from hbuilder.domain.loader import load_cards
from hbuilder.domain.schema import (
    NOT_APPLICABLE_TARGET_FIELDS_BY_TARGET_KIND,
    SCHEMA_VERSION,
    SchemaError,
    TARGET_KINDS,
    validate_skill_slot,
)
from hbuilder.domain.scoring import _resolve_skill, team_taint
from hbuilder.domain.team import MEMBER_COUNT, Team
from hbuilder.domain.unknown import UNKNOWN

from conftest import make_resolved, make_scoring_config, make_skill


def _passive_slot(**overrides: object) -> dict:
    """A schema-valid PASSIVE `stat_up` slot: `duration`/`interval`/
    `probability_tier` are all `null` because
    `NOT_APPLICABLE_FIELDS_BY_SLOT_KIND["PASSIVE"]` requires it, and both
    `condition` and `target` are explicit so a test can vary either one
    without touching the other."""
    slot = {
        "family": "stat_up",
        "effect_type": "performance",
        "duration": None,
        "interval": None,
        "probability_tier": None,
        "magnitude": 14.0,
        "condition": {"kind": "ungated"},
        "target": {"kind": "all"},
        "override": {"kind": "none"},
        "rider": {"kind": "none"},
    }
    slot.update(overrides)
    return slot


def _card_record(
    *,
    card_id: str,
    target: object,
    type_: str = "Cute",
    generations: tuple[str, ...] = ("1期生",),
) -> dict:
    """A minimal, schema-shaped card record whose PASSIVE slot carries the
    given `target` value directly (may be `None`, a populated dict, or an
    `{"kind": "all"}` dict under test)."""
    return {
        "id": card_id,
        # A distinct holomem per card: R1 forbids two skins of one character
        # among the members, so a shared name would make every team invalid
        # for a reason this file is not about.
        "member": f"Member {card_id}",
        "generation": list(generations),
        "type": type_,
        "rarity": "SSR",
        "costume_name": "Test Costume",
        "stats": {"performance": 100, "technique": 100, "sense": 100},
        "skills": {
            "SPECIAL": None,
            "ACTIVE": None,
            "PASSIVE": _passive_slot(target=target),
        },
        "costume_skill": None,
    }


def _write_cards_document(tmp_path, *card_records: dict):
    path = tmp_path / "cards.json"
    doc = {"schema_version": SCHEMA_VERSION, "cards": list(card_records)}
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _all_target_filler_card(card_id: str, type_: str = "Pure") -> object:
    return card_from_record(
        _card_record(card_id=card_id, target={"kind": "all"}, type_=type_)
    )


# --- null taints, {"kind": "all"} does not ----------------------------------


def test_null_target_appears_in_unknown_fields():
    # null means UNKNOWN — the transcriber has not recorded who this
    # effect reaches, so it must taint, exactly like an untranscribed
    # condition.
    record = _card_record(card_id="untranscribed_target_01", target=None)
    assert validate_skill_slot(record["skills"]["PASSIVE"], "untranscribed_target_01", "PASSIVE") == []
    card = card_from_record(record)
    assert "skills.PASSIVE.target" in card.unknown_fields()
    assert card.skill("PASSIVE").target is UNKNOWN


def test_all_kind_target_does_not_appear_in_unknown_fields():
    # The affirmative marker is not tainting — it is a real
    # assertion that the effect reaches the whole team.
    record = _card_record(card_id="all_target_01", target={"kind": "all"})
    assert validate_skill_slot(record["skills"]["PASSIVE"], "all_target_01", "PASSIVE") == []
    card = card_from_record(record)
    assert "skills.PASSIVE.target" not in card.unknown_fields()
    assert card.skill("PASSIVE").target is None


def test_null_target_taints_the_team(tmp_path):
    path = _write_cards_document(
        tmp_path, _card_record(card_id="untranscribed_target_01", target=None)
    )
    cards = load_cards(path)
    # The card under test must be a MEMBER: taint, gates and EV are all
    # computed over the five members, and a leader's own slots are never
    # emitted.
    members = (
        cards[0],
        *(_all_target_filler_card(f"filler_{i}") for i in range(2, MEMBER_COUNT + 1)),
    )
    leader = _all_target_filler_card("outfit_01")
    team = Team(leader=leader, members=members)

    assert "untranscribed_target_01:skills.PASSIVE.target" in team_taint(team)


# --- value/count structurally absent for self and all ----------------------


def test_all_target_with_value_present_is_rejected_naming_target_value():
    record = _card_record(
        card_id="bad_all_target_01", target={"kind": "all", "value": "Cute"}
    )
    errors = validate_skill_slot(record["skills"]["PASSIVE"], "bad_all_target_01", "PASSIVE")
    assert any("target.value" in e and "bad_all_target_01" in e for e in errors)


def test_all_target_with_count_present_is_rejected_naming_target_count():
    record = _card_record(
        card_id="bad_all_target_02", target={"kind": "all", "count": 2}
    )
    errors = validate_skill_slot(record["skills"]["PASSIVE"], "bad_all_target_02", "PASSIVE")
    assert any("target.count" in e and "bad_all_target_02" in e for e in errors)


def test_self_target_with_count_present_is_rejected_naming_target_count():
    # "self" reaches exactly one card by definition — a count is not a
    # second way to say it, the same structurally-absent discipline "all"
    # carries.
    record = _card_record(
        card_id="bad_self_target_01", target={"kind": "self", "count": 1}
    )
    errors = validate_skill_slot(record["skills"]["PASSIVE"], "bad_self_target_01", "PASSIVE")
    assert any("target.count" in e and "bad_self_target_01" in e for e in errors)


def test_target_key_absent_entirely_is_still_a_schema_error():
    slot = _passive_slot()
    del slot["target"]
    errors = validate_skill_slot(slot, "card_missing_target_01", "PASSIVE")
    assert any("target" in e and "card_missing_target_01" in e for e in errors)


# --- enum and range rejections -----------------------------------------------


def test_type_target_with_unknown_type_value_is_rejected_naming_target_value():
    record = _card_record(
        card_id="bad_type_target_01",
        target={"kind": "type", "value": "Sour", "count": 2},
    )
    errors = validate_skill_slot(record["skills"]["PASSIVE"], "bad_type_target_01", "PASSIVE")
    assert any("target.value" in e and "bad_type_target_01" in e for e in errors)


def test_generation_target_with_unknown_generation_value_is_rejected_naming_target_value():
    record = _card_record(
        card_id="bad_generation_target_01",
        target={"kind": "generation", "value": "6期生", "count": 2},
    )
    errors = validate_skill_slot(
        record["skills"]["PASSIVE"], "bad_generation_target_01", "PASSIVE"
    )
    assert any("target.value" in e and "bad_generation_target_01" in e for e in errors)


def test_unknown_target_kind_is_rejected_naming_target_kind_and_listing_target_kinds():
    record = _card_record(
        card_id="bad_kind_target_01",
        target={"kind": "coven", "value": "Cute", "count": 2},
    )
    errors = validate_skill_slot(record["skills"]["PASSIVE"], "bad_kind_target_01", "PASSIVE")
    matches = [e for e in errors if "target.kind" in e and "bad_kind_target_01" in e]
    assert matches
    assert all(str(list(TARGET_KINDS)) in e for e in matches)


@pytest.mark.parametrize("bad_count", [0, -1, True, 7])
def test_target_count_out_of_range_is_rejected_naming_target_count(bad_count):
    record = _card_record(
        card_id="bad_count_target_01",
        target={"kind": "type", "value": "Cute", "count": bad_count},
    )
    errors = validate_skill_slot(record["skills"]["PASSIVE"], "bad_count_target_01", "PASSIVE")
    assert any("target.count" in e and "bad_count_target_01" in e for e in errors)


# --- real target kinds still resolve, unknown target resolves to
# data_missing ---------------------------------------------------------------


def test_generation_kind_target_converts_to_effect_target_carrying_kind_value_and_count():
    record = _card_record(
        card_id="gen_target_01",
        target={"kind": "generation", "value": "1期生", "count": 2},
    )
    assert validate_skill_slot(record["skills"]["PASSIVE"], "gen_target_01", "PASSIVE") == []
    card = card_from_record(record)
    target = card.skill("PASSIVE").target
    assert isinstance(target, EffectTarget)
    assert target.kind == "generation"
    assert target.value == "1期生"
    assert target.count == 2


def test_unknown_target_resolves_to_data_missing_state_naming_the_target_field_and_contributes_zero():
    # The target equivalent of test_unknown_condition_is_data_missing.py's
    # pin: an untranscribed target must never reach
    # resolve_target_card_ids and must never be scored as if it were
    # affirmatively team-wide — that would overstate, the one direction
    # the `>=` lower-bound marker cannot express.
    skill = make_skill(
        "PASSIVE",
        family="stat_up",
        effect_type="performance",
        magnitude=14.0,
        condition=None,
        target=UNKNOWN,
    )
    resolved = make_resolved("carrier_01", "PASSIVE", skill, gate=None)
    cfg = make_scoring_config()

    assert _resolve_skill(resolved, cfg, 0.0) == (0.0, "data_missing", "target"), (
        "an untranscribed target must resolve to data_missing at rate "
        "0.0, naming 'target' as the missing field — never a full-value "
        "whole-team contribution"
    )


# --- import-time structural assertion ---------------------------------------


def test_not_applicable_target_fields_key_set_equals_target_kinds():
    assert set(NOT_APPLICABLE_TARGET_FIELDS_BY_TARGET_KIND) == set(TARGET_KINDS)
