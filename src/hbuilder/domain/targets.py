"""Effect-target resolution, the sibling of gate resolution (`domain/gates.py`): turns a
`card.EffectTarget` into the concrete set of card ids an effect reaches. It depends only
on the assembled card set, so it is leader-invariant. Both scoring drivers call
`resolve_target_card_ids` for every targeted skill and never resolve a target themselves."""

from __future__ import annotations

from collections.abc import Mapping

from hbuilder.domain.card import Card, EffectTarget
from hbuilder.domain.scoring_models import TargetSelectionModel


def resolve_target_card_ids(
    target: object,
    self_card_id: str,
    cards: tuple[Card, ...],
    stat_names: tuple[str, ...],
    model: TargetSelectionModel,
    under_supply: str,
    rank_values: Mapping[str, float] | None = None,
) -> frozenset[str]:
    """Resolve `target` against the assembled card set `cards`.

    - `None` (team-wide) -> every id in `cards`.
    - `kind == "self"` -> `{self_card_id}`.
    - `kind == "type"` / `"generation"` -> the qualifying cards (matching `card.type` /
      `target.value in card.generations`), ranked, and `model.select` picks `target.count`.

    Ranking value: `rank_values[card id]` when given (a Score Support passes each member's
    ACTIVE magnitude, `scoring.active_magnitude_by_card`: it raises no stat, and the game pays
    the qualifiers with the strongest ACTIVE); otherwise the card's base value of the stat(s)
    the effect raises (`stat_names`, summed), so an all-stats or score effect ranks by total
    parameter. Against 690 server `Live/GetDeck` rows (deck-screen Overall Power) this rule
    is exact on 690; ranking every effect by total parameter is exact on 510, and eleven
    other rules (total ascending, slot order either way, emitter first or excluded, total
    plus board power, the displayed per-card total, level, rarity, bloom) stay under 81 %.

    Under-supply (fewer qualifiers than `target.count`) follows `under_supply`:
    `"apply_to_available"` returns every qualifier, `"do_not_apply"` none.

    Raises `ValueError` for any `target` other than `None` or an `EffectTarget`: scoring
    routes an `UNKNOWN` target to `data_missing` first (`_REQUIRED_KNOWN_FIELDS`)."""
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
            f"resolve_target_card_ids: unrecognised target kind {target.kind!r} for card {self_card_id!r}"
        )

    # Rank by the base value of the stat(s) raised (see the docstring). Ties keep deck slot
    # order: `candidates` follows `cards` and `TopNByBuffedStat.select` is a stable sort.
    candidates = [
        (
            card.id,
            rank_values.get(card.id, 0.0)
            if rank_values is not None
            else sum(getattr(card.stats, name) for name in stat_names),
        )
        for card in qualifying
    ]

    if len(candidates) < target.count:  # type: ignore[operator]
        if under_supply == "do_not_apply":
            return frozenset()
        # "apply_to_available": every qualifier receives it.
        return frozenset(card_id for card_id, _stat_value in candidates)

    selected = model.select(candidates, target.count)  # type: ignore[arg-type]
    return frozenset(selected)
