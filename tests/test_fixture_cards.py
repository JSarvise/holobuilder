"""Coverage assertions over the shipped `tests/fixtures/cards.json` fixture.

These tests exist so a later hand-edit that quietly drops one of the
fixture's deliberate coverage cases fails the suite immediately, rather
than silently degrading the format's only unambiguous documentation. The
fixture is claimed to cover: all three skill slot kinds, all three gate
condition kinds, all six skill families, one card in two generations that
fires two distinct generation gates from that single team slot, a gate
that fires with actual count exactly equal to its threshold, a gate that
does not fire one short of its threshold, a gate whose target is entirely
absent from the team (actual count 0), one explicitly unknown field, and
one explicit known zero.

The fixture's ungated slots (`nene_01` PASSIVE, `mio_01` ACTIVE,
`matsuri_01` ACTIVE, `lamy_01` PASSIVE) use the affirmative
`{"kind": "ungated"}` encoding, since `"condition": null` means UNKNOWN.
`mio_01`'s ACTIVE.magnitude is the fixture's one deliberately unknown
field.
"""

from __future__ import annotations

import pytest

from hbuilder.domain.gates import RunState, resolve_team_gates
from hbuilder.domain.loader import load_cards
from hbuilder.domain.schema import (
    CONDITION_KINDS,
    SCHEMA_VERSION,
    SKILL_FAMILIES,
    SKILL_SLOT_KINDS,
)
from hbuilder.domain.team import build_team

from conftest import FIXTURE_CARDS_PATH, FIXTURE_OWNED_PATH

CARDS_PATH = FIXTURE_CARDS_PATH

LEADER_ID = "nene_01"
MEMBER_IDS = ("fubuki_01", "mio_01", "matsuri_01", "polka_01", "lamy_01")

# Matches config.json's assumed_run_state defaults exactly, so this fixture
# resolves the shipped run-state condition (matsuri_01's PASSIVE, `life >= 600`)
# the same way a freshly loaded ScoringConfig would. It resolves FIRED: LIFE
# starts at its 1000 maximum and only decreases on missed notes, so under the
# perfect-play premise it never drops below 600.
_RUN_STATE = RunState(life=1000, combo=100)


@pytest.fixture(scope="module")
def cards():
    return load_cards(CARDS_PATH)


@pytest.fixture(scope="module")
def team(cards):
    return build_team(cards, LEADER_ID, MEMBER_IDS)


@pytest.fixture(scope="module")
def resolved(team):
    return resolve_team_gates(team, _RUN_STATE)


def _all_slots(cards):
    """Every non-null SkillSlot across every card: normal slots plus the
    costume skill."""
    slots = []
    for card in cards:
        for slot_kind in SKILL_SLOT_KINDS:
            slot = card.skill(slot_kind)
            if slot is not None:
                slots.append(slot)
        if card.costume_skill is not None:
            slots.append(card.costume_skill)
    return slots


def test_ships_exactly_six_cards(cards):
    assert len(cards) == 6


def test_all_three_slot_kinds_appear(cards):
    kinds_present = {
        slot_kind
        for card in cards
        for slot_kind in SKILL_SLOT_KINDS
        if card.skill(slot_kind) is not None
    }
    assert kinds_present == set(SKILL_SLOT_KINDS)


def test_all_three_condition_kinds_appear(cards):
    kinds_present = {
        slot.condition.kind for slot in _all_slots(cards) if slot.condition is not None
    }
    assert kinds_present == set(CONDITION_KINDS)


def test_all_six_skill_families_appear(cards):
    families_present = {slot.family for slot in _all_slots(cards)}
    assert families_present == set(SKILL_FAMILIES)


def test_exactly_one_card_has_more_than_one_generation(cards):
    multi = [card for card in cards if len(card.generations) > 1]
    assert len(multi) == 1
    assert multi[0].id == "fubuki_01"


def test_dual_generation_card_fires_two_distinct_generation_gates(resolved):
    fubuki_generation_gates = [
        r
        for r in resolved
        if r.card_id == "fubuki_01"
        and r.gate is not None
        and r.gate.predicate.dimension == "generation"
    ]
    assert len(fubuki_generation_gates) == 2
    assert {r.gate.fired for r in fubuki_generation_gates} == {True}
    assert {r.gate.predicate.value for r in fubuki_generation_gates} == {
        "1期生",
        "ゲーマーズ",
    }


def test_a_gate_fires_with_actual_count_equal_to_threshold(resolved):
    matches = [
        r
        for r in resolved
        if r.gate is not None
        and r.gate.fired
        and r.gate.actual_count == r.gate.predicate.threshold
    ]
    assert matches, "expected at least one gate resolving fired at exactly threshold"


def test_a_gate_does_not_fire_one_short_of_threshold(resolved):
    matches = [
        r
        for r in resolved
        if r.gate is not None
        and not r.gate.fired
        and r.gate.actual_count == r.gate.predicate.threshold - 1
    ]
    assert matches, "expected at least one gate resolving not-fired one short"


def test_a_gate_does_not_fire_with_actual_count_zero(resolved):
    matches = [
        r
        for r in resolved
        if r.gate is not None and not r.gate.fired and r.gate.actual_count == 0
    ]
    assert matches, "expected at least one gate whose target is absent from the team"


def test_mio_active_magnitude_is_unknown(cards):
    mio = next(card for card in cards if card.id == "mio_01")
    assert "skills.ACTIVE.magnitude" in mio.unknown_fields()


def test_lamy_sense_stat_reads_back_as_zero_and_is_not_reported_unknown(cards):
    lamy = next(card for card in cards if card.id == "lamy_01")
    assert lamy.stat("sense") == 0
    assert not any("sense" in field for field in lamy.unknown_fields())


def test_owned_json_holds_all_six_shipped_ids():
    import json

    doc = json.loads(FIXTURE_OWNED_PATH.read_text(encoding="utf-8"))
    assert doc["schema_version"] == SCHEMA_VERSION
    assert set(doc["owned"]) == {
        "nene_01",
        "fubuki_01",
        "mio_01",
        "matsuri_01",
        "polka_01",
        "lamy_01",
    }
