"""The domain adapter from a solver `Team` to the deck screen's Score Bonus lines
(`hbuilder.domain.deck_panel_team`). Its accuracy against the server's own
panels is measured separately; these pin the wiring: which skill feeds which `PanelCard` field, slot order,
and the arithmetic identity of the lines."""

from __future__ import annotations

import pytest
from conftest import FIXTURE_CARDS_PATH, make_scoring_config

from hbuilder.domain.deck_panel import deck_panel
from hbuilder.domain.deck_panel_team import panel_cards_for_team, team_deck_panel
from hbuilder.domain.loader import load_cards
from hbuilder.domain.team import Team


@pytest.fixture(scope="module")
def cards():
    return load_cards(FIXTURE_CARDS_PATH)


@pytest.fixture(scope="module")
def cfg():
    return make_scoring_config()


def _team(cards):
    leader = next(c for c in cards if c.id == "nene_01")
    members = tuple(c for c in cards if c.id != "nene_01")[:5]
    if len(members) < 5:
        members = tuple(cards[1:6])
    return Team(leader=leader, members=members)


def test_cards_follow_the_slot_order_and_the_lines_add_up(cards, cfg):
    team = _team(cards)
    order = [m.id for m in reversed(team.members)]
    got = panel_cards_for_team(team, cfg, order)
    assert got.card_ids == tuple(order)
    p = got.panel
    assert p.score_bonus_permil == p.active + p.outfit + p.passive + p.board + p.special
    # the same card carries the same inputs whatever slot it sits in
    plain = panel_cards_for_team(team, cfg)
    by_id = dict(zip(plain.card_ids, plain.cards))
    for cid, card in zip(got.card_ids, got.cards):
        assert card == by_id[cid]


def test_an_active_is_read_off_the_card_in_permil(cards, cfg):
    team = _team(cards)
    got = panel_cards_for_team(team, cfg)
    checked = 0
    for card, panel_card in zip(team.members, got.cards):
        active = next((s for s in card.skills if s is not None and s.kind == "ACTIVE"), None)
        if active is None or not isinstance(active.magnitude, (int, float)):
            continue
        assert panel_card.probability_permil == round(
            cfg.probability_tiers[active.probability_tier] * 1000)
        assert panel_card.cooldown_s == round(active.interval)
        assert panel_card.duration_s == round(active.duration)
        assert panel_card.magnitude_permil >= round(active.magnitude * 10)
        checked += 1
    assert checked >= 1


def test_no_board_means_no_board_terms(cards, cfg):
    got = panel_cards_for_team(_team(cards), cfg)
    assert cfg.board is None
    for card in got.cards:
        assert card.board_effect_up_permil == 0
        assert card.board_probability_up_permil == 0
        assert card.board_cooldown_shorten_permil == 0
    assert got.panel.board == 0


def test_unit_score_uses_the_overall_power_given(cards, cfg):
    got = team_deck_panel(_team(cards), cfg, overall_power=100000)
    assert got.panel.unit_score == deck_panel(got.cards, overall_power=100000).unit_score
    assert got.panel.unit_score > 0


def test_member_order_must_be_a_permutation(cards, cfg):
    with pytest.raises(ValueError):
        panel_cards_for_team(_team(cards), cfg, ["nope"] * 5)
