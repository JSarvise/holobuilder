"""The deck screen's Score Bonus lines for a TEAM the solver built.

`deck_panel.deck_panel` computes the panel's five Score Bonus lines (Active,
Outfit, Passive, Holomem Board, Special) from per-card integers. Its recon
adapter (`tools/recon/proxy/deck_panel_params.py`) fills those integers from a
CAPTURED server panel; this module fills them from the domain's own objects —
a `Team`, the `ScoringConfig` it is scored on (its board included) and the slot
order — so any hypothetical deck the optimiser proposes gets the split too.

Every number comes from the same resolution the scorer uses: the gates
(`gates.resolve_card_set_gates` / `resolve_leader_costume_skills`), the support
recipients (`song._support_state`, the per-member targeting the ranking
drivers use) and the board's own terms (`BoardModel.skill_effect_up_for`,
`uptime_for`). Nothing here re-derives what a card is worth.

Where this adapter follows a RULE rather than a measurement it says so:

* run-state overrides (LIFE / COMBO) are taken outright, a COMBO one with its
  base magnitude before t = 20 s — `deck_panel`'s own law (it matches the
  server's panels) and the recon adapter's rule;
* the board's effect-up is the song-less, unconditional leader-scope term
  (`skill_effect_up_for`), the quantity `deck_panel_params` derives from the
  account's board; singer-conditional reds are not on a song-less panel;
* activation-rate effects other than the board's and a SPECIAL rider's
  (`special_rate_rider_permil`) have no field in `PanelCard`; a team carrying
  one is NAMED in `unmodelled` rather than silently priced.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from hbuilder.domain.card import SlotRider
from hbuilder.domain.deck_panel import DeckPanel, PanelCard, deck_panel
from hbuilder.domain.gates import resolve_card_set_gates, resolve_leader_costume_skills
from hbuilder.domain.scoring import (
    _effective_magnitude,
    _fold_activation_rate_up,
    _pool_for,
    _resolve_skill,
)
from hbuilder.domain.scoring_config import ScoringConfig
from hbuilder.domain.song import _support_state
from hbuilder.domain.team import Team

__all__ = ["TeamPanel", "panel_cards_for_team", "team_deck_panel"]

# The family a SPECIAL's activation-rate rider is transcribed as (data/cards.json).
_RATE_RIDER_FAMILY = "activation_rate_up"


def _permil(value: object) -> int:
    """A percentage-point magnitude (25.0 = +25 %) as whole permil."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return round(float(value) * 10.0)


def _seconds(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return round(float(value))


@dataclass(frozen=True, slots=True)
class TeamPanel:
    """`panel` (the five lines) plus the per-card inputs it was computed from,
    in slot order, and the effects the model has no field for."""

    panel: DeckPanel
    cards: tuple[PanelCard, ...]
    card_ids: tuple[str, ...]
    unmodelled: tuple[str, ...]


def _active_fields(resolved, cfg: ScoringConfig, team_rate_up: float) -> dict:
    """The ACTIVE's magnitude / probability / cooldown / duration."""
    _rate, state, _missing = _resolve_skill(resolved, cfg, team_rate_up)
    slot = resolved.skill
    if state != "applied":
        return {}
    base = _permil(slot.magnitude)
    magnitude = _permil(_effective_magnitude(resolved))
    combo_base = None
    override = slot.override
    condition = getattr(override, "condition", None)
    if condition is not None and getattr(condition, "kind", None) == "run_state":
        # the panel takes a run-state override outright; a COMBO one starts at
        # its base magnitude (deck_panel's combo-gate onset law)
        magnitude = max(base, _permil(override.magnitude))
        if str(getattr(condition, "target", "")).lower() == "combo":
            combo_base = base
    tier = slot.probability_tier
    probability = cfg.probability_tiers.get(tier) if isinstance(tier, str) else None
    return {
        "magnitude_permil": magnitude,
        "probability_permil": round(float(probability) * 1000.0) if probability else 0,
        "cooldown_s": _seconds(slot.interval),
        "duration_s": _seconds(slot.duration),
        "combo_base_magnitude_permil": combo_base,
    }


def _rider_permil(resolved) -> int:
    rider = resolved.skill.rider
    if not isinstance(rider, SlotRider) or rider.family != _RATE_RIDER_FAMILY:
        return 0
    if resolved.rider_gate is not None and not resolved.rider_gate.fired:
        return 0
    return _permil(rider.magnitude)


def panel_cards_for_team(
    team: Team, cfg: ScoringConfig, member_order: Sequence[str] | None = None
) -> TeamPanel:
    """The five `PanelCard`s of `team` on `cfg` (board included), in
    `member_order` (card ids; default: `team.members` order), and the panel."""
    members = tuple(team.members)
    by_id = {card.id: card for card in members}
    order = tuple(member_order) if member_order else tuple(c.id for c in members)
    if sorted(order) != sorted(by_id):
        raise ValueError(f"member_order {list(order)} is not a permutation of the members")
    gates = resolve_card_set_gates(members, cfg.run_state)
    normal = [r for card in members for r in gates.normal_by_card_id[card.id]]
    team_rate_up = _fold_activation_rate_up(tuple(normal), cfg)
    unmodelled: list[str] = []

    outfit = {cid: 0 for cid in by_id}
    passive = {cid: 0 for cid in by_id}
    special: dict[str, tuple[int, int, int]] = {}
    active: dict[str, dict] = {}

    def _give(bucket: dict, resolved, state: str) -> None:
        support_state, recipients = _support_state(resolved, members, cfg, state)
        if support_state != "applied":
            return
        value = _permil(_effective_magnitude(resolved))
        for cid in (recipients if recipients is not None else by_id):
            if cid in bucket:
                bucket[cid] += value

    for resolved in normal:
        pool = _pool_for(resolved)
        kind = resolved.skill.kind
        _rate, state, _missing = _resolve_skill(resolved, cfg, team_rate_up)
        if kind == "ACTIVE":
            active[resolved.card_id] = _active_fields(resolved, cfg, team_rate_up)
        elif kind == "SPECIAL":
            if pool == "support" and state == "applied":
                special[resolved.card_id] = (
                    _permil(_effective_magnitude(resolved)),
                    _seconds(resolved.skill.duration),
                    _rider_permil(resolved))
        elif pool == "support":
            _give(passive, resolved, state)
        elif pool == "none" and state == "applied":
            unmodelled.append(f"{resolved.card_id} {kind} activation_rate")
    for costume in resolve_leader_costume_skills(team.leader, gates.aggregates, cfg.run_state):
        if _pool_for(costume) == "support":
            _rate, state, _missing = _resolve_skill(costume, cfg, team_rate_up)
            _give(outfit, costume, state)

    board = cfg.board
    board_up = 0
    if board is not None:
        board_up = round(board.skill_effect_up_for(members, team.leader.member) * 1000.0)

    cards: list[PanelCard] = []
    for cid in order:
        card = by_id[cid]
        act = active.get(cid) or {}
        spec = special.get(cid, (0, 0, 0))
        rate_up, shorten = board.uptime_for(card.member) if board is not None else (0.0, 0.0)
        cards.append(PanelCard(
            magnitude_permil=act.get("magnitude_permil", 0),
            probability_permil=act.get("probability_permil", 0),
            cooldown_s=act.get("cooldown_s", 0) or 1,
            duration_s=act.get("duration_s", 0),
            outfit_effect_up_permil=outfit[cid],
            passive_effect_up_permil=passive[cid],
            board_effect_up_permil=board_up,
            board_probability_up_permil=round(rate_up * 1000.0),
            board_cooldown_shorten_permil=round(shorten * 1000.0),
            special_magnitude_permil=spec[0],
            special_duration_s=spec[1],
            special_rate_rider_permil=spec[2],
            combo_base_magnitude_permil=act.get("combo_base_magnitude_permil"),
        ))
    panel = deck_panel(cards)
    return TeamPanel(panel=panel, cards=tuple(cards), card_ids=order,
                     unmodelled=tuple(unmodelled))


def song_bonus_permil(cfg: ScoringConfig, chart) -> int:
    """`eff3`: the song's per-song board bonus in permil -- the same figure the song
    scorer uses (`BoardModel.song_score_bonus_for` over the song's own cast and
    scope, capped); 0 without a board or a cast."""
    if chart is None or cfg.board is None or not getattr(chart, "singers", None):
        return 0
    return round(1000 * cfg.board.song_score_bonus_for(chart.singers, scope=chart.singer_type))


def team_deck_panel(
    team: Team, cfg: ScoringConfig, member_order: Sequence[str] | None = None,
    *, overall_power: float | None = None, chart=None,
) -> TeamPanel:
    """`panel_cards_for_team` with the Unit Score filled from `overall_power`
    (the deck screen's Overall Power, an integer on the panel). With `chart`, the
    Board line carries that song's per-song bonus, as the deck screen of a chosen
    song shows it (`deck_panel(song_bonus_permil=...)`)."""
    got = panel_cards_for_team(team, cfg, member_order)
    if overall_power is None:
        return got
    panel = deck_panel(got.cards, overall_power=round(overall_power),
                       song_bonus_permil=song_bonus_permil(cfg, chart))
    return TeamPanel(panel=panel, cards=got.cards, card_ids=got.card_ids,
                     unmodelled=got.unmodelled)
