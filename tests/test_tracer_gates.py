"""End-to-end tracer: the shipped tests/fixtures/cards.json on disk ->
validated Card objects -> an assembled Team -> a TeamAggregates snapshot ->
gate predicates resolved against that snapshot. One path, verified whole —
not per-layer unit tests.
"""

from __future__ import annotations

import json

import pytest

from hbuilder.domain.gates import RunState, aggregate, resolve_team_gates
from hbuilder.domain.loader import load_cards
from hbuilder.domain.schema import SCHEMA_VERSION, SchemaError
from hbuilder.domain.team import MEMBER_COUNT, build_team

from conftest import FIXTURE_CARDS_PATH

CARDS_PATH = FIXTURE_CARDS_PATH

LEADER_ID = "nene_01"
MEMBER_IDS = ["fubuki_01", "mio_01", "matsuri_01", "polka_01", "lamy_01"]

# Composition-gate tests in this file don't exercise run-state
# resolution — an explicit RunState is still required by resolve_team_gates.
_RUN_STATE = RunState(life=1000, combo=100)


def _load_team():
    cards = load_cards(CARDS_PATH)
    team = build_team(cards, LEADER_ID, MEMBER_IDS)
    return cards, team


def test_all_generations_are_frozensets():
    cards, _ = _load_team()
    for card in cards:
        assert isinstance(card.generations, frozenset)


def test_gamers_and_1kisei_gates_both_fire_from_the_same_card():
    _, team = _load_team()
    resolved = resolve_team_gates(team, _RUN_STATE)

    fubuki_gates = [r for r in resolved if r.card_id == "fubuki_01" and r.gate is not None]
    assert len(fubuki_gates) == 2

    by_value = {r.gate.predicate.value: r for r in fubuki_gates}
    assert "1期生" in by_value
    assert "ゲーマーズ" in by_value

    for resolved_skill in fubuki_gates:
        assert resolved_skill.gate.fired
        assert resolved_skill.gate.actual_count == 2


def test_costume_skill_present_as_team_wide_effect_alongside_normal_slots():
    _, team = _load_team()
    resolved = resolve_team_gates(team, _RUN_STATE)

    costume_entries = [r for r in resolved if r.source == "costume"]
    assert len(costume_entries) == 1
    assert costume_entries[0].card_id == LEADER_ID

    # The leader contributes its outfit skill and NOTHING else. Its own
    # normal slots must not appear.
    normal_leader_entries = [
        r for r in resolved if r.card_id == LEADER_ID and r.source == "normal"
    ]
    assert normal_leader_entries == [], (
        "the leader's own SPECIAL/ACTIVE/PASSIVE must not be emitted; only "
        "its Outfit Skill is a team-wide effect (MODEL-REVISION §23)"
    )


def test_generation_counts_sum_matches_dual_generation_rule():
    cards, team = _load_team()
    agg = aggregate(team.members)

    # Counted over the MEMBERS, not the whole loaded collection: only they
    # feed the aggregate a gate resolves against.
    multi_generation_members = sum(
        1 for card in team.members if len(card.generations) > 1
    )
    assert (
        sum(agg.generation_counts.values()) == MEMBER_COUNT + multi_generation_members
    )


def test_type_counts_sum_to_the_five_members():
    """The leader is an outfit slot outside the five and
    contributes nothing to the counts a gate reads."""
    _, team = _load_team()
    agg = aggregate(team.members)
    assert sum(agg.type_counts.values()) == MEMBER_COUNT


def test_empty_generation_array_raises_schema_error_naming_card_and_field(tmp_path):
    bad_doc = {
        "schema_version": SCHEMA_VERSION,
        "cards": [
            {
                "id": "bad_card_01",
                "member": "Test Member",
                "generation": [],
                "type": "Cute",
                "rarity": "SSR",
                "costume_name": "Test Costume",
                "stats": {"performance": 100, "technique": 100, "sense": 100},
                "skills": {"SPECIAL": None, "ACTIVE": None, "PASSIVE": None},
                "costume_skill": None,
            }
        ],
    }
    bad_path = tmp_path / "bad_cards.json"
    bad_path.write_text(json.dumps(bad_doc), encoding="utf-8")

    with pytest.raises(SchemaError) as exc_info:
        load_cards(bad_path)

    message = str(exc_info.value)
    assert "bad_card_01" in message
    assert "generation" in message
