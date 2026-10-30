"""The per-card gate table in `gates.resolve_card_set_gates`.

A card's resolved slots are a function of `run_state` and of the few team
counts its own conditions read, so they are resolved once per distinct reading
and handed back afterwards. The reference here is `_resolve_one_skill` called
directly for every slot of every card, on the
fixture catalogue, whose cards carry real conditions, overrides and riders."""

from __future__ import annotations

import random

import pytest

from hbuilder.domain import gates
from hbuilder.domain.card import GateCondition
from hbuilder.domain.gates import (
    RunState, _gate_probes, _resolve_one_skill, aggregate, resolve_card_set_gates,
)
from hbuilder.domain.loader import load_cards
from hbuilder.domain.schema import SKILL_SLOT_KINDS

from conftest import FIXTURE_CARDS_PATH, make_card, make_skill


def _reference(cards, run_state):
    agg = aggregate(cards)
    normal, costume = {}, {}
    for card in cards:
        normal[card.id] = tuple(
            _resolve_one_skill(card.id, card.member, kind, "normal", card.skill(kind), agg, run_state)
            for kind in SKILL_SLOT_KINDS if card.skill(kind) is not None)
        costume[card.id] = (
            _resolve_one_skill(card.id, card.member, "costume", "costume",
                               card.costume_skill, agg, run_state)
            if card.costume_skill is not None else None)
    return agg, normal, costume


@pytest.fixture(scope="module")
def catalogue():
    return load_cards(FIXTURE_CARDS_PATH)


def test_the_table_returns_what_resolving_every_slot_returns(catalogue):
    rng = random.Random(20261013)
    states = [RunState(life=1000, combo=0), RunState(life=400, combo=0),
              RunState(life=1000, combo=500)]
    fired = set()
    for _ in range(3000):
        cards = tuple(rng.sample(catalogue, rng.choice([1, 5, 5, 5, 6])))
        run_state = rng.choice(states)
        got = resolve_card_set_gates(cards, run_state)
        agg, normal, costume = _reference(cards, run_state)
        assert got.aggregates == agg
        assert got.normal_by_card_id == normal
        assert got.costume_by_card_id == costume
        for resolved in (r for slots in normal.values() for r in slots):
            if resolved.gate is not None:
                fired.add(resolved.gate.fired)
    assert fired == {True, False}      # both outcomes were really exercised


def test_the_same_reading_hands_back_the_same_records_and_another_does_not():
    gated = make_skill("PASSIVE", condition=GateCondition(kind="type", target="Cute", count=2))
    card = make_card("card-00001-5-uniq-0000-00", type_="Cool", passive=gated)
    cute = [make_card(f"card-{n:05d}-5-uniq-0000-00", type_="Cute") for n in (2, 3, 4)]
    cool = [make_card(f"card-{n:05d}-5-uniq-0000-00", type_="Cool") for n in (5, 6)]
    state = RunState(life=1000, combo=0)
    assert _gate_probes(card) == (("type", "Cute"),)

    two_cute = resolve_card_set_gates((card, cute[0], cute[1]), state)
    again = resolve_card_set_gates((card, cute[1], cute[2], cool[0]), state)   # other team, 2 Cute
    assert two_cute.normal_by_card_id[card.id] is again.normal_by_card_id[card.id]
    assert two_cute.normal_by_card_id[card.id][0].gate.fired

    one_cute = resolve_card_set_gates((card, cute[0], cool[0]), state)
    record = one_cute.normal_by_card_id[card.id][0]
    assert not record.gate.fired and record.gate.actual_count == 1
    three_cute = resolve_card_set_gates((card, *cute), state)                   # fires, count differs
    assert three_cute.normal_by_card_id[card.id][0].gate.actual_count == 3

    # an equal card that is another OBJECT gets its own table, not this one's
    twin = make_card("card-00001-5-uniq-0000-00", type_="Cool", passive=gated)
    assert gates._card_gate_tables[id(card)][0] is card
    resolve_card_set_gates((twin, cute[0], cute[1]), state)
    assert gates._card_gate_tables[id(twin)][0] is twin


def test_run_state_is_part_of_the_reading():
    comeback = make_skill("PASSIVE", condition=GateCondition(
        kind="run_state", target="life", count=500, operator="lte"))
    card = make_card("card-00009-5-uniq-0000-00", passive=comeback)
    assert _gate_probes(card) == ()
    full = resolve_card_set_gates((card,), RunState(life=1000, combo=0))
    low = resolve_card_set_gates((card,), RunState(life=300, combo=0))
    assert not full.normal_by_card_id[card.id][0].gate.fired
    assert low.normal_by_card_id[card.id][0].gate.fired
