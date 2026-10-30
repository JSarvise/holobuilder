"""The skill-slot contract: the six parsed fields, the six skill families,
and the family-to-effect_type relation that makes a stat-scaling skill
address one named stat.

`validate_skill_slot` reports; it never rewrites. Every test here checks
either an accepted slot round-trips unchanged, or a rejected slot's error
text names the card id, the slot kind, and the offending field.
"""

from __future__ import annotations

import dataclasses

from hbuilder.domain.card import skill_slot_from_record
from hbuilder.domain.gates import RunState
from hbuilder.domain.schema import (
    CARD_TYPES,
    CONDITION_KINDS,
    COSTUME_SLOT_KIND,
    EFFECT_TYPES_BY_FAMILY,
    FAMILY_REQUIRES_SLOT_KIND,
    GENERATIONS,
    NOT_APPLICABLE_FIELDS_BY_SLOT_KIND,
    PROBABILITY_TIERS,
    RUN_STATE_DIMENSIONS,
    SKILL_FAMILIES,
    SKILL_SLOT_KINDS,
    STAT_NAMES,
    validate_skill_slot,
)

from conftest import FIXTURE_CARDS_PATH


def _valid_run_state_slot(**overrides: object) -> dict:
    slot = {
        "family": "conditional_score_up",
        "effect_type": "score",
        "duration": None,
        "interval": None,
        "probability_tier": None,
        "magnitude": 50,
        "condition": {"kind": "run_state", "target": "life", "count": 600},
        "target": {"kind": "all"},
        "override": {"kind": "none"},
        "rider": {"kind": "none"},
    }
    slot.update(overrides)
    return slot


def _valid_active_slot(**overrides: object) -> dict:
    slot = {
        "family": "conditional_score_up",
        "effect_type": "score",
        "duration": 8,
        "interval": 15,
        "probability_tier": "Medium",
        "magnitude": 18,
        "condition": {"kind": "generation", "target": "5期生", "count": 2},
        "target": {"kind": "all"},
        "override": {"kind": "none"},
        "rider": {"kind": "none"},
    }
    slot.update(overrides)
    return slot


def _valid_special_slot(**overrides: object) -> dict:
    # "condition": null is schema-valid — it means the gate is not known,
    # not "ungated" — so validate_skill_slot(...) == [] holds.
    slot = {
        "family": "score_support",
        "effect_type": "score",
        "duration": None,
        "interval": None,
        "probability_tier": None,
        "magnitude": 10,
        "condition": None,
        "target": {"kind": "all"},
        "override": {"kind": "none"},
        "rider": {"kind": "none"},
    }
    slot.update(overrides)
    return slot


def _valid_passive_slot(**overrides: object) -> dict:
    # See _valid_special_slot's comment — condition: null is schema-valid
    # and means the gate is not known.
    slot = {
        "family": "life_recovery",
        "effect_type": "life",
        "duration": None,
        "interval": None,
        "probability_tier": None,
        "magnitude": 5,
        "condition": None,
        "target": {"kind": "all"},
        "override": {"kind": "none"},
        "rider": {"kind": "none"},
    }
    slot.update(overrides)
    return slot


# --- SKILL_FAMILIES / EFFECT_TYPES_BY_FAMILY / NOT_APPLICABLE sanity ------


def test_skill_families_are_six_distinct_values_in_dom08_order():
    assert SKILL_FAMILIES == (
        "score_support",
        "conditional_score_up",
        "stat_up",
        "activation_rate_up",
        "life_recovery",
        "judgment_enhancement",
    )
    assert len(set(SKILL_FAMILIES)) == 6


def test_effect_types_by_family_matches_the_contract():
    assert EFFECT_TYPES_BY_FAMILY["score_support"] == {"score"}
    assert EFFECT_TYPES_BY_FAMILY["conditional_score_up"] == {"score"}
    assert EFFECT_TYPES_BY_FAMILY["stat_up"] == {
        "performance",
        "technique",
        "sense",
        "all_stats",
    }
    assert EFFECT_TYPES_BY_FAMILY["activation_rate_up"] == {"activation_rate"}
    assert EFFECT_TYPES_BY_FAMILY["life_recovery"] == {"life"}
    assert EFFECT_TYPES_BY_FAMILY["judgment_enhancement"] == {"judgment"}


def test_not_applicable_fields_by_slot_kind_matches_the_contract():
    assert NOT_APPLICABLE_FIELDS_BY_SLOT_KIND["ACTIVE"] == frozenset()
    assert NOT_APPLICABLE_FIELDS_BY_SLOT_KIND["SPECIAL"] == {
        "interval",
        "probability_tier",
    }
    assert NOT_APPLICABLE_FIELDS_BY_SLOT_KIND["PASSIVE"] == {
        "duration",
        "interval",
        "probability_tier",
    }
    assert NOT_APPLICABLE_FIELDS_BY_SLOT_KIND[COSTUME_SLOT_KIND] == {
        "duration",
        "interval",
        "probability_tier",
    }


# --- family / effect_type behavior ----------------------------------------


def test_family_outside_six_accepted_values_is_rejected():
    slot = _valid_active_slot(family="score_bonus_extreme")
    errors = validate_skill_slot(slot, "card_family_01", "ACTIVE")
    assert any(
        "card_family_01" in e and "ACTIVE" in e and "family" in e for e in errors
    )


def test_stat_up_effect_type_score_is_rejected_and_lists_stat_names():
    slot = _valid_active_slot(family="stat_up", effect_type="score")
    errors = validate_skill_slot(slot, "card_stat_01", "ACTIVE")
    joined = " ".join(errors)
    assert "score" in joined
    for stat_name in ("performance", "technique", "sense"):
        assert stat_name in joined


def test_stat_up_effect_type_technique_is_accepted_and_reads_back():
    slot = _valid_active_slot(family="stat_up", effect_type="technique")
    assert validate_skill_slot(slot, "card_stat_02", "ACTIVE") == []
    built = skill_slot_from_record(slot, "ACTIVE")
    assert built.effect_type == "technique"


# --- FAMILY_REQUIRES_SLOT_KIND: activation_rate_up is ACTIVE-only ---------


def test_activation_rate_up_family_on_a_passive_slot_is_rejected_naming_the_required_active_slot_kind():
    slot = _valid_passive_slot(family="activation_rate_up", effect_type="activation_rate")
    errors = validate_skill_slot(slot, "card_rateup_01", "PASSIVE")
    assert any(
        e.startswith("family") and "activation_rate_up" in e and "ACTIVE" in e
        for e in errors
    )


def test_activation_rate_up_family_on_a_special_slot_is_rejected_naming_the_required_active_slot_kind():
    slot = _valid_special_slot(family="activation_rate_up", effect_type="activation_rate")
    errors = validate_skill_slot(slot, "card_rateup_02", "SPECIAL")
    assert any(
        e.startswith("family") and "activation_rate_up" in e and "ACTIVE" in e
        for e in errors
    )


def test_activation_rate_up_family_on_a_costume_slot_is_rejected_naming_the_required_active_slot_kind():
    slot = _valid_passive_slot(family="activation_rate_up", effect_type="activation_rate")
    errors = validate_skill_slot(slot, "card_rateup_03", COSTUME_SLOT_KIND)
    assert any(
        e.startswith("family") and "activation_rate_up" in e and "ACTIVE" in e
        for e in errors
    )


def test_activation_rate_up_family_on_an_active_slot_validates_clean():
    slot = _valid_active_slot(family="activation_rate_up", effect_type="activation_rate")
    assert validate_skill_slot(slot, "card_rateup_04", "ACTIVE") == []


def test_family_requires_slot_kind_keys_are_skill_families_and_values_are_slot_kinds():
    assert all(key in SKILL_FAMILIES for key in FAMILY_REQUIRES_SLOT_KIND)
    assert all(value in SKILL_SLOT_KINDS for value in FAMILY_REQUIRES_SLOT_KIND.values())


# --- interval / duration / magnitude behavior -----------------------------


def test_interval_zero_is_rejected_naming_interval():
    slot = _valid_active_slot(interval=0)
    errors = validate_skill_slot(slot, "card_interval_01", "ACTIVE")
    assert any("interval" in e and "card_interval_01" in e for e in errors)


def test_interval_one_is_accepted():
    slot = _valid_active_slot(interval=1)
    assert validate_skill_slot(slot, "card_interval_02", "ACTIVE") == []


def test_duration_equals_interval_is_accepted_and_round_trips_exactly():
    slot = _valid_active_slot(duration=15, interval=15)
    assert validate_skill_slot(slot, "card_duration_01", "ACTIVE") == []
    built = skill_slot_from_record(slot, "ACTIVE")
    assert built.duration == 15
    assert built.interval == 15


# --- not-applicable-field behavior (SPECIAL / PASSIVE) --------------------


def test_special_slot_probability_tier_medium_is_rejected():
    slot = _valid_special_slot(probability_tier="Medium")
    errors = validate_skill_slot(slot, "card_special_01", "SPECIAL")
    assert any(
        "SPECIAL" in e and "probability_tier" in e and "card_special_01" in e
        for e in errors
    )


def test_special_slot_non_null_interval_is_rejected():
    slot = _valid_special_slot(interval=10)
    errors = validate_skill_slot(slot, "card_special_02", "SPECIAL")
    assert any("SPECIAL" in e and "interval" in e for e in errors)


def test_passive_slot_duration_5_is_rejected():
    slot = _valid_passive_slot(duration=5.0)
    errors = validate_skill_slot(slot, "card_passive_01", "PASSIVE")
    assert any(
        "PASSIVE" in e and "duration" in e and "card_passive_01" in e for e in errors
    )


# --- probability_tier behavior --------------------------------------------


def test_probability_tier_outside_enum_is_rejected():
    slot = _valid_active_slot(probability_tier="Ultra")
    errors = validate_skill_slot(slot, "card_tier_01", "ACTIVE")
    assert any("probability_tier" in e for e in errors)
    assert set(PROBABILITY_TIERS) == {"Low", "Medium", "High"}


# --- condition behavior ----------------------------------------------------


def test_condition_count_zero_is_rejected_naming_condition_count():
    slot = _valid_active_slot(
        condition={"kind": "type", "target": "Cute", "count": 0}
    )
    errors = validate_skill_slot(slot, "card_condition_01", "ACTIVE")
    assert any("condition.count" in e for e in errors)


def test_condition_count_one_is_accepted():
    slot = _valid_active_slot(
        condition={"kind": "type", "target": "Cute", "count": 1}
    )
    assert validate_skill_slot(slot, "card_condition_02", "ACTIVE") == []
    assert "Cute" in CARD_TYPES
    assert "5期生" in GENERATIONS


# --- run-state condition validation --------------------------------------


def test_run_state_condition_with_misspelled_target_is_rejected_naming_condition_target():
    slot = _valid_run_state_slot(
        condition={"kind": "run_state", "target": "life_at_or_above_60", "count": 1}
    )
    errors = validate_skill_slot(slot, "card_run_state_01", "PASSIVE")
    assert any(
        "condition.target" in e and "card_run_state_01" in e for e in errors
    )


def test_run_state_condition_with_valid_target_is_accepted():
    slot = _valid_run_state_slot(
        condition={"kind": "run_state", "target": "combo", "count": 100}
    )
    assert validate_skill_slot(slot, "card_run_state_02", "PASSIVE") == []


def test_run_state_condition_count_is_a_threshold_not_pinned_to_one():
    """`count` on a run_state condition is the real numeric threshold, not a
    flag, so it must accept values above 1: 600 and 1000 are both real LIFE
    thresholds, on different skills. Pinning it to 1 would make every
    run-state gate unrepresentable.

    See `tests/test_run_state_thresholds.py`.
    """
    slot = _valid_run_state_slot(
        condition={"kind": "run_state", "target": "life", "count": 600}
    )
    errors = validate_skill_slot(slot, "card_run_state_03", "PASSIVE")
    count_errors = [e for e in errors if "condition.count" in e]
    assert count_errors == [], (
        "a run_state threshold above 1 must now validate; got "
        f"{count_errors}"
    )


def test_condition_kind_outside_enum_is_rejected_naming_condition_kind_and_listing_all_three():
    slot = _valid_run_state_slot(
        condition={"kind": "generatoin", "target": "1期生", "count": 1}
    )
    errors = validate_skill_slot(slot, "card_condition_kind_01", "PASSIVE")
    matching = [
        e for e in errors if "condition.kind" in e and "card_condition_kind_01" in e
    ]
    assert matching
    assert all(kind in matching[0] for kind in CONDITION_KINDS)


def test_run_state_fields_match_run_state_flags_element_for_element():
    assert tuple(field.name for field in dataclasses.fields(RunState)) == RUN_STATE_DIMENSIONS


# --- empty-object / missing-field behavior --------------------------------


def test_empty_object_names_every_missing_required_field():
    errors = validate_skill_slot({}, "card_empty_01", "ACTIVE")
    for field in (
        "family",
        "effect_type",
        "duration",
        "interval",
        "probability_tier",
        "magnitude",
        "condition",
        "target",
        "override",
        "rider",
    ):
        assert any(field in e for e in errors), f"missing field {field!r} not reported"


def test_missing_magnitude_names_card_slot_and_field():
    slot = _valid_active_slot()
    del slot["magnitude"]
    errors = validate_skill_slot(slot, "card_missing_01", "ACTIVE")
    assert any(
        "card_missing_01" in e and "ACTIVE" in e and "magnitude" in e for e in errors
    )


def test_null_slot_is_valid_and_means_slot_absent():
    assert validate_skill_slot(None, "card_null_01", "ACTIVE") == []


# --- all_parameter_up: the all_stats effect_type --------------------------


def test_all_parameter_up_is_accepted_as_effect_type_all_stats_on_the_stat_up_family():
    slot = _valid_passive_slot(family="stat_up", effect_type="all_stats")
    assert validate_skill_slot(slot, "card_all_stats_01", "PASSIVE") == []
    built = skill_slot_from_record(slot, "PASSIVE")
    assert built.effect_type == "all_stats"


def test_all_parameter_up_effect_type_all_stats_is_rejected_on_score_support_naming_the_family():
    slot = _valid_special_slot(family="score_support", effect_type="all_stats")
    errors = validate_skill_slot(slot, "card_all_stats_02", "SPECIAL")
    joined = " ".join(errors)
    assert "score_support" in joined
    assert "all_stats" in joined
    assert "score" in joined


def test_all_parameter_up_effect_type_all_stats_is_rejected_on_conditional_score_up_naming_the_family():
    slot = _valid_active_slot(family="conditional_score_up", effect_type="all_stats")
    errors = validate_skill_slot(slot, "card_all_stats_03", "ACTIVE")
    joined = " ".join(errors)
    assert "conditional_score_up" in joined
    assert "all_stats" in joined
    assert "score" in joined


def test_all_parameter_up_effect_type_all_stats_is_rejected_on_activation_rate_up_naming_the_family():
    slot = _valid_active_slot(family="activation_rate_up", effect_type="all_stats")
    errors = validate_skill_slot(slot, "card_all_stats_04", "ACTIVE")
    joined = " ".join(errors)
    assert "activation_rate_up" in joined
    assert "all_stats" in joined
    assert "activation_rate" in joined


def test_all_parameter_up_effect_type_all_stats_is_rejected_on_life_recovery_naming_the_family():
    slot = _valid_passive_slot(family="life_recovery", effect_type="all_stats")
    errors = validate_skill_slot(slot, "card_all_stats_05", "PASSIVE")
    joined = " ".join(errors)
    assert "life_recovery" in joined
    assert "all_stats" in joined
    assert "life" in joined


def test_all_parameter_up_effect_type_all_stats_is_rejected_on_judgment_enhancement_naming_the_family():
    slot = _valid_passive_slot(family="judgment_enhancement", effect_type="all_stats")
    errors = validate_skill_slot(slot, "card_all_stats_06", "PASSIVE")
    joined = " ".join(errors)
    assert "judgment_enhancement" in joined
    assert "all_stats" in joined
    assert "judgment" in joined


def test_every_stat_pool_effect_type_declares_the_stats_it_raises():
    """A future effect_type routed to the stat pool without a STAT_NAMES_BY_EFFECT_TYPE entry goes red at import, not
    at score time."""
    from hbuilder.domain.scoring import POOL_BY_EFFECT_TYPE, STAT_NAMES_BY_EFFECT_TYPE

    stat_pool_effect_types = {
        effect_type
        for effect_type, pool in POOL_BY_EFFECT_TYPE.items()
        if pool == "stat"
    }
    assert set(STAT_NAMES_BY_EFFECT_TYPE) == stat_pool_effect_types
    for stat_names in STAT_NAMES_BY_EFFECT_TYPE.values():
        assert all(stat_name in STAT_NAMES for stat_name in stat_names)


# --- key-order independence -----------------------------------------------


# --- the shipped fixture carries an explicit target on every slot --------


def test_every_populated_slot_in_the_shipped_cards_json_carries_an_explicit_target():
    """A change that reintroduces "a missing target means team-wide" goes
    red here immediately, rather than silently overstating."""
    import json

    doc = json.loads(FIXTURE_CARDS_PATH.read_text(encoding="utf-8"))
    for card in doc["cards"]:
        for slot_kind in SKILL_SLOT_KINDS:
            slot = card["skills"][slot_kind]
            if slot is not None:
                assert "target" in slot, (
                    f"card {card['id']!r} {slot_kind} slot is missing an "
                    f"explicit 'target' key — a missing target must never "
                    f"mean team-wide"
                )
        costume = card["costume_skill"]
        if costume is not None:
            assert "target" in costume, (
                f"card {card['id']!r} costume_skill is missing an explicit "
                f"'target' key"
            )


def test_key_order_independence_constructs_equal_skill_slot():
    ordered = _valid_active_slot()
    reordered = {key: ordered[key] for key in reversed(list(ordered.keys()))}

    assert validate_skill_slot(ordered, "card_order_01", "ACTIVE") == []
    assert validate_skill_slot(reordered, "card_order_01", "ACTIVE") == []

    slot_a = skill_slot_from_record(ordered, "ACTIVE")
    slot_b = skill_slot_from_record(reordered, "ACTIVE")
    assert slot_a == slot_b


# --- override validation -------------------------------------------------


def _populated_override(**overrides: object) -> dict:
    override = {
        "kind": "override",
        "magnitude": 70.0,
        "condition": {"kind": "type", "target": "Pure", "count": 2},
    }
    override.update(overrides)
    return override


def test_a_populated_override_validates_clean():
    slot = _valid_active_slot(override=_populated_override())
    assert validate_skill_slot(slot, "card_override_01", "ACTIVE") == []


def test_an_override_missing_magnitude_is_rejected_naming_override_magnitude():
    override = _populated_override()
    del override["magnitude"]
    slot = _valid_active_slot(override=override)
    errors = validate_skill_slot(slot, "card_override_02", "ACTIVE")
    assert any("override.magnitude" in e and "card_override_02" in e for e in errors)


def test_an_override_with_null_magnitude_is_rejected_naming_override_magnitude():
    slot = _valid_active_slot(override=_populated_override(magnitude=None))
    errors = validate_skill_slot(slot, "card_override_03", "ACTIVE")
    assert any("override.magnitude" in e and "card_override_03" in e for e in errors)


def test_an_override_with_negative_magnitude_is_rejected_naming_override_magnitude():
    slot = _valid_active_slot(override=_populated_override(magnitude=-5.0))
    errors = validate_skill_slot(slot, "card_override_04", "ACTIVE")
    assert any("override.magnitude" in e and "card_override_04" in e for e in errors)


def test_an_override_with_bool_magnitude_is_rejected_naming_override_magnitude():
    slot = _valid_active_slot(override=_populated_override(magnitude=True))
    errors = validate_skill_slot(slot, "card_override_05", "ACTIVE")
    assert any("override.magnitude" in e and "card_override_05" in e for e in errors)


def test_an_override_missing_condition_is_rejected_naming_override_condition():
    override = _populated_override()
    del override["condition"]
    slot = _valid_active_slot(override=override)
    errors = validate_skill_slot(slot, "card_override_06", "ACTIVE")
    assert any("override.condition" in e and "card_override_06" in e for e in errors)


def test_an_override_with_null_condition_is_rejected_naming_override_condition():
    # A null (unknown) condition on a POPULATED override is not the same
    # thing as the whole override being null (untranscribed) -- the
    # override itself claims to exist, so its own gate must be known and
    # populated, never a second unknown hiding one level down.
    slot = _valid_active_slot(override=_populated_override(condition=None))
    errors = validate_skill_slot(slot, "card_override_07", "ACTIVE")
    assert any("override.condition" in e and "card_override_07" in e for e in errors)


def test_an_override_with_ungated_condition_is_rejected_naming_override_condition():
    # An override with no gate at all is not an override, it is simply a
    # different base magnitude.
    slot = _valid_active_slot(
        override=_populated_override(condition={"kind": "ungated"})
    )
    errors = validate_skill_slot(slot, "card_override_08", "ACTIVE")
    assert any("override.condition" in e and "card_override_08" in e for e in errors)


def test_a_none_kind_override_with_magnitude_present_is_rejected_naming_override_magnitude():
    slot = _valid_active_slot(override={"kind": "none", "magnitude": 70.0})
    errors = validate_skill_slot(slot, "card_override_09", "ACTIVE")
    assert any("override.magnitude" in e and "card_override_09" in e for e in errors)


def test_a_none_kind_override_with_condition_present_is_rejected_naming_override_condition():
    slot = _valid_active_slot(
        override={
            "kind": "none",
            "condition": {"kind": "type", "target": "Pure", "count": 2},
        }
    )
    errors = validate_skill_slot(slot, "card_override_10", "ACTIVE")
    assert any("override.condition" in e and "card_override_10" in e for e in errors)


def test_a_slots_override_carries_no_family_or_effect_type_of_its_own():
    """A populated override carrying either `family` or `effect_type` is
    rejected -- by definition it overrides the principal's magnitude, and
    a second copy of the family could only ever disagree with the first."""
    slot_with_family = _valid_active_slot(
        override=_populated_override(family="stat_up")
    )
    errors_family = validate_skill_slot(slot_with_family, "card_override_11", "ACTIVE")
    assert any(
        "override.family" in e and "card_override_11" in e for e in errors_family
    )

    slot_with_effect_type = _valid_active_slot(
        override=_populated_override(effect_type="performance")
    )
    errors_effect_type = validate_skill_slot(
        slot_with_effect_type, "card_override_12", "ACTIVE"
    )
    assert any(
        "override.effect_type" in e and "card_override_12" in e
        for e in errors_effect_type
    )


def test_a_null_override_taints_the_card_naming_the_dotted_path():
    from hbuilder.domain.card import card_from_record

    record = {
        "id": "override_taint_01",
        "member": "Test Member",
        "generation": ["5期生"],
        "type": "Cute",
        "rarity": "SSR",
        "costume_name": "Test Costume",
        "stats": {"performance": 100, "technique": 100, "sense": 100},
        "skills": {
            "SPECIAL": None,
            "ACTIVE": _valid_active_slot(override=None),
            "PASSIVE": None,
        },
        "costume_skill": None,
    }
    assert validate_skill_slot(record["skills"]["ACTIVE"], "override_taint_01", "ACTIVE") == []
    card = card_from_record(record)
    assert "skills.ACTIVE.override" in card.unknown_fields()


# --- rider validation ----------------------------------------------------
# Every test name below carries the literal "second_family" -- the token a
# `-k second_family` filter selects on, and the game-side concept these
# tests cover: "a second, different family in one slot".


def _populated_second_family_rider(**overrides: object) -> dict:
    rider = {
        "kind": "rider",
        "family": "life_recovery",
        "effect_type": "life",
        "magnitude": 15.0,
        "condition": {"kind": "ungated"},
        "target": {"kind": "all"},
    }
    rider.update(overrides)
    return rider


def test_a_second_family_rider_on_a_special_slot_is_retained_not_dropped():
    slot = _valid_special_slot(rider=_populated_second_family_rider())
    assert validate_skill_slot(slot, "card_second_family_01", "SPECIAL") == []
    built = skill_slot_from_record(slot, "SPECIAL")
    assert built.rider.family == "life_recovery"
    assert built.rider.effect_type == "life"
    assert built.rider.magnitude == 15.0


def test_a_second_familys_effect_type_outside_its_familys_accepted_set_is_rejected_naming_rider_effect_type():
    slot = _valid_special_slot(
        rider=_populated_second_family_rider(family="life_recovery", effect_type="score")
    )
    errors = validate_skill_slot(slot, "card_second_family_02", "SPECIAL")
    assert any(
        "rider.effect_type" in e and "card_second_family_02" in e for e in errors
    )


def test_a_second_familys_family_outside_skill_families_is_rejected_naming_rider_family():
    slot = _valid_special_slot(
        rider=_populated_second_family_rider(family="score_bonus_extreme")
    )
    errors = validate_skill_slot(slot, "card_second_family_03", "SPECIAL")
    assert any("rider.family" in e and "card_second_family_03" in e for e in errors)


def test_a_second_familys_malformed_condition_is_rejected_naming_the_nested_rider_condition_path():
    slot = _valid_special_slot(
        rider=_populated_second_family_rider(
            condition={"kind": "type", "target": "Cute", "count": 0}
        )
    )
    errors = validate_skill_slot(slot, "card_second_family_04", "SPECIAL")
    assert any(
        "rider.condition.count" in e and "card_second_family_04" in e for e in errors
    )


def test_a_second_familys_malformed_target_is_rejected_naming_the_nested_rider_target_path():
    slot = _valid_special_slot(
        rider=_populated_second_family_rider(target={"kind": "all", "value": "Cute"})
    )
    errors = validate_skill_slot(slot, "card_second_family_05", "SPECIAL")
    assert any(
        "rider.target.value" in e and "card_second_family_05" in e for e in errors
    )


def test_a_none_kind_second_family_rider_with_a_payload_field_present_is_rejected_naming_that_field():
    slot = _valid_special_slot(rider={"kind": "none", "family": "life_recovery"})
    errors = validate_skill_slot(slot, "card_second_family_06", "SPECIAL")
    assert any("rider.family" in e and "card_second_family_06" in e for e in errors)


def test_a_null_second_family_rider_taints_the_card_naming_the_dotted_path():
    from hbuilder.domain.card import card_from_record

    record = {
        "id": "second_family_taint_01",
        "member": "Test Member",
        "generation": ["1期生"],
        "type": "Cute",
        "rarity": "SR",
        "costume_name": "Test Costume",
        "stats": {"performance": 100, "technique": 100, "sense": 100},
        "skills": {
            "SPECIAL": _valid_special_slot(rider=None),
            "ACTIVE": None,
            "PASSIVE": None,
        },
        "costume_skill": None,
    }
    assert validate_skill_slot(record["skills"]["SPECIAL"], "second_family_taint_01", "SPECIAL") == []
    card = card_from_record(record)
    assert "skills.SPECIAL.rider" in card.unknown_fields()


# --- the guard is scoped to the slot's PRINCIPAL family only -- a
# second_family rider is never checked against FAMILY_REQUIRES_SLOT_KIND,
# only the slot's own family is. Every test name below also carries the
# literal "second_family".


def test_a_second_familys_principal_on_a_special_slot_is_still_rejected_naming_the_required_active_slot_kind():
    """The guard is SCOPED, not weakened: a SPECIAL slot whose own
    (principal) family is activation_rate_up is still rejected --
    the scoping changes what the rider may carry, never what the
    principal may be."""
    slot = _valid_special_slot(family="activation_rate_up", effect_type="activation_rate")
    errors = validate_skill_slot(slot, "card_second_family_07", "SPECIAL")
    assert any(
        e.startswith("family") and "activation_rate_up" in e and "ACTIVE" in e
        for e in errors
    )


def test_a_second_family_rider_naming_activation_rate_up_on_a_special_slot_validates_clean():
    """The point of the scoping: a SPECIAL slot's RIDER may carry
    activation_rate_up and load, because FAMILY_REQUIRES_SLOT_KIND is
    never consulted against a rider's family -- only the slot's own
    (principal) family."""
    slot = _valid_special_slot(
        rider=_populated_second_family_rider(
            family="activation_rate_up", effect_type="activation_rate"
        )
    )
    assert validate_skill_slot(slot, "card_second_family_08", "SPECIAL") == []
    built = skill_slot_from_record(slot, "SPECIAL")
    assert built.rider.family == "activation_rate_up"


def test_a_second_family_rider_naming_activation_rate_up_on_a_passive_slot_validates_clean():
    """The same holds for PASSIVE: FAMILY_REQUIRES_SLOT_KIND scopes to the
    principal family regardless of which slot kind carries the rider."""
    slot = _valid_passive_slot(
        rider=_populated_second_family_rider(
            family="activation_rate_up", effect_type="activation_rate"
        )
    )
    assert validate_skill_slot(slot, "card_second_family_09", "PASSIVE") == []
    built = skill_slot_from_record(slot, "PASSIVE")
    assert built.rider.family == "activation_rate_up"


def test_a_second_family_rider_naming_activation_rate_up_on_a_costume_slot_validates_clean():
    """And for COSTUME: a costume slot's rider is unchecked against
    FAMILY_REQUIRES_SLOT_KIND exactly like any other slot kind's rider."""
    slot = _valid_passive_slot(
        rider=_populated_second_family_rider(
            family="activation_rate_up", effect_type="activation_rate"
        )
    )
    assert validate_skill_slot(slot, "card_second_family_10", COSTUME_SLOT_KIND) == []
    built = skill_slot_from_record(slot, COSTUME_SLOT_KIND)
    assert built.rider.family == "activation_rate_up"
