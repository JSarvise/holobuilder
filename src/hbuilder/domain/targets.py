"""Effect-target resolution — a sibling of gate resolution
(`domain/gates.py`), not a variant of it. Target resolution depends only on
the assembled 6-card set, exactly like `TeamAggregates` — it is therefore
leader-invariant, the same property the batch API and the solver hot loop
depend on.

`resolve_target_card_ids` is the one place a `card.EffectTarget` clause
becomes a concrete `frozenset[str]` of card ids. Both scoring drivers
(`scoring._score_team` and `scoring.score_card_set`) call this once per
`pool == "stat"` skill instance, never resolving a target themselves.
"""

from __future__ import annotations

from hbuilder.domain.card import Card, EffectTarget
from hbuilder.domain.scoring_models import TargetSelectionModel


def resolve_target_card_ids(
    target: object,
    self_card_id: str,
    cards: tuple[Card, ...],
    stat_names: tuple[str, ...],
    model: TargetSelectionModel,
    under_supply: str,
) -> frozenset[str]:
    """Resolve `target` against the assembled 6-card `cards` set.

    - `target is None` (the structurally team-wide marker) -> every id in
      `cards`.
    - `target.kind == "self"` -> `frozenset({self_card_id})` — always the
      source card, never resolved against `cards` at all.
    - `target.kind == "type"` / `"generation"` -> the qualifying cards
      (matching `card.type` / `target.value in card.generations`) are
      ranked and `model.select(...)` picks `target.count` of them.

    Ranking value: the card's BASE value of the stat(s) the effect raises —
    `stat_names`, summed. A single-stat buff ranks its matchers by that one
    stat; an all-stats effect (and a score effect, whose callers pass all
    three names) ranks by total parameter. Checked against 690 server
    `Live/GetDeck` rows (the deck-screen Overall Power as the target), this
    rule is bit-exact on 690 of 690; ranking every effect by TOTAL parameter
    is exact on only 510 of 690, and on the other 180 the server visibly paid
    a different member. Eleven other rules were scored on the same rows and
    none passes 81 %: total ascending, slot order either way, emitter first
    or excluded, total plus board power, the displayed per-card total, level,
    rarity, bloom.

    Under-supply (fewer qualifying cards than `target.count`): the
    configured `under_supply` rule decides, never a hardcoded choice —
    `"apply_to_available"` returns every qualifying card,
    `"do_not_apply"` returns an empty frozenset.

    `target` must never be the `UNKNOWN` sentinel: `scoring.py` is
    responsible for routing an untranscribed target to `data_missing`
    before resolution is ever attempted (via `_REQUIRED_KNOWN_FIELDS`).
    Reaching this function with anything other than `None` or an
    `EffectTarget` raises `ValueError` naming the card, because fabricating
    a plausible resolution for an unrecorded target would be strictly
    worse than raising.
    """
    if target is None:
        return frozenset(card.id for card in cards)

    if not isinstance(target, EffectTarget):
        raise ValueError(
            f"resolve_target_card_ids requires a resolved target (None or "
            f"EffectTarget), never UNKNOWN — card {self_card_id!r} reached "
            f"this function with an untranscribed target; scoring.py must "
            f"route an unknown target to data_missing before resolution is "
            f"ever attempted"
        )

    if target.kind == "self":
        return frozenset({self_card_id})

    if target.kind == "type":
        qualifying = [card for card in cards if card.type == target.value]
    elif target.kind == "generation":
        qualifying = [card for card in cards if target.value in card.generations]
    else:  # pragma: no cover - schema.TARGET_KINDS is exhaustive at load time
        raise ValueError(
            f"resolve_target_card_ids: unrecognised target kind "
            f"{target.kind!r} for card {self_card_id!r}"
        )

    # Rank by the base value of the stat(s) the effect raises; see the docstring for why
    # this rule and not total parameter. Ties keep deck slot order: `candidates` is built in `cards` order and
    # `TopNByBuffedStat.select` is a stable sort.
    candidates = [
        (card.id, sum(getattr(card.stats, name) for name in stat_names))
        for card in qualifying
    ]

    if len(candidates) < target.count:
        if under_supply == "do_not_apply":
            return frozenset()
        # "apply_to_available": the effect applies to every qualifying
        # card, since fewer qualify than the target's own count.
        return frozenset(card_id for card_id, _stat_value in candidates)

    selected = model.select(candidates, target.count)
    return frozenset(selected)
