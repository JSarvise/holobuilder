"""Leader dominance: most cards can never be the best outfit choice, and which
ones is decidable before any search runs.

**The argument.** A leader contributes *only its outfit skill* — no stats at
all: the five member power values sum to Overall Power exactly. So for a fixed
member set, two leaders whose outfit skills agree on family, effect type, gate
condition, target and override differ in exactly one number: the magnitude.
The one with the smaller magnitude cannot score higher. It is a **dominance**
relation, not a shortlist: it provably cannot discard the optimum, whereas a
"keep the 32 strongest outfits" heuristic can.

**⚠ Never shortlist leaders by rarity instead.** 3★ outfits are all
`+15 %` all-stats, but the **4★ median magnitude is 100.0 against the 5★ median
of 50.0**, and six of the strongest outfits are 4★ at `+100 %`. A rarity filter
discards genuinely optimal leaders. This module keys on the effect, never on the
card.

## Classes versus surviving cards

Counting *classes* overstates the reduction (`91 owned -> 16 signatures -> 25
(signature, magnitude) -> 16 survive`, 5.7x). Collapsing a class to one
representative is only sound when its maximum is attained by one card, and it
is not: **2 of the 16 owned signatures have their maximum TIED across several
cards.** Tied leaders score identically, so they tie in the ranking — and
`exact._heap_key` breaks that tie on the lexicographically smallest leader id.
Keeping one arbitrary representative would therefore change which leader the
tool NAMES at rank 1, and would drop the tied arrangements out of a Top-10 the
exhaustive search puts there.

So this module keeps **every** card tied at its class maximum, and the
reduction is:

```
167 cards -> 35 signatures -> 59 surviving cards   2.83x
 91 owned -> 16 signatures -> 19 surviving cards   4.79x
```

**4.79x, not 5.7x.** That is the trade this module takes deliberately.

`bench/validate_search.py --leader-dedup` reports these numbers through this
module and checks the best score against unpruned enumeration.

## ⚠ What "exact" means here — three separate claims, measured separately

Over **40 random real sub-pools at N = 12, 14, 16, 18**, full enumeration
against pruned enumeration:

| | | |
|---|---|---|
| **A** | the best SCORE is identical | **40/40** |
| **B** | the best ARRANGEMENT is identical (leader id and member ids) | **40/40** |
| **C** | the whole Top-10 is identical | **36/40** |

**C fails, it is supposed to fail, and anyone using this must know that.** A
dominated leader still forms a perfectly valid team; it just is not the best
one. The exhaustive Top-10 contains such teams, and pruning removes them, so
the pruned Top-10 is *the best ten arrangements among surviving leaders* — a
defensible answer for a user (why list a team whose outfit is strictly worse
than an available alternative?) but **not the exhaustive Top-10**. Both
observed failures were at rank 10, none above it.

**So: use this to find the best team. Do not use it to produce a reference
Top-N for validating a heuristic's top-10 overlap.** That measurement needs the
unpruned search.

**B is measured, not proved, and the residual is named.** It can only fail if a
dominated leader scores EXACTLY equal to its dominator — which needs the
outfit's contribution to be swamped by a `max`-aggregation or priced at zero.
The zero-weight half is detectable statically and is guarded (`_is_weightless`);
the swamped half is not, and did not occur in 40 trials. If it ever matters, the
cheap fix is to re-score the winning member set against the whole pool
afterwards — 91 evaluations — rather than to prune less.

## What makes it exact HERE, and what would break it

Three conditions, each checked rather than assumed:

1. **The leader must reach the score only through its outfit.** For a fixed
   member set, `score_card_set` returns the identical float for every leader
   sharing a `(signature, magnitude)` pair — `tests/test_leader_dominance.py`
   asserts it over the real pool. Leader-scope red board nodes break this, so
   with a board configured the key gains the holomem (`board_discriminates`).
   Any other path from leader to score would make this pruning unsound.

2. **An outfit that targets `self` is not comparable.** Its recipient is the
   leader, which is a member on some sets and nobody on others, so its
   contribution is not a function of `(signature, magnitude)` at all. **All
   167 outfit targets in `data/cards.json` are team-wide**, so the case does
   not arise in practice — and `outfit_signature` gives such a card a unique,
   unprunable signature rather than trusting that it stays that way.

3. **A tainted field is never grouped.** An `UNKNOWN` magnitude, condition,
   target or override makes the card its own class. Grouping on a field that
   was never populated is the absent-vs-zero merge the whole schema layer
   exists to refuse.

**It prunes the LEADER ROLE only.** A dominated card is still a fully eligible
*member* — it carries stats and skills that have nothing to do with its
outfit — so `dominant_leader_ids` is passed to the leader vector and never to
the member enumeration.
"""

from __future__ import annotations

from typing import Iterable

from hbuilder.domain.card import Card, EffectTarget, GateCondition, SlotOverride
from hbuilder.domain.unknown import is_known


def _condition_key(condition: object, card_id: str) -> object:
    """A gate's identity, or an unprunable marker.

    `None` is the structurally-ungated value and groups with other ungated
    outfits; `UNKNOWN` is "never populated" and must not group with anything.
    """
    if condition is None:
        return None
    if isinstance(condition, GateCondition):
        # `operator` is part of the gate's identity: a life-gte-950 and a
        # life-lte-950 gate resolve oppositely, so they must never share a
        # dedup key. Included unconditionally (it defaults to "gte").
        return (
            "gate",
            condition.kind,
            condition.target,
            condition.count,
            condition.operator,
        )
    return ("unprunable", card_id)


def _target_key(target: object, card_id: str) -> object:
    """A target's identity, or an unprunable marker.

    `kind == "self"` is deliberately unprunable rather than a key component:
    two `self`-targeting outfits reach *different cards*, and whether that card
    is even among the five depends on the member set, so their scores are not
    ordered by magnitude at all.
    """
    if target is None:
        return None
    if isinstance(target, EffectTarget):
        if target.kind == "self":
            return ("unprunable", card_id)
        return ("target", target.kind, target.value, target.count)
    return ("unprunable", card_id)


def _override_key(override: object, card_id: str) -> object:
    """The override's GATE, not its magnitude.

    Its magnitude is a second number that moves with the card, so two outfits
    sharing a base magnitude and differing on the override magnitude are NOT
    comparable on the base alone. Including the override magnitude in the key
    rather than in the comparison keeps such cards in separate classes, which
    is the conservative direction: it prunes less, never wrongly.
    """
    if override is None:
        return None
    if isinstance(override, SlotOverride):
        if not is_known(override.magnitude):
            return ("unprunable", card_id)
        return (
            "override",
            override.magnitude,
            _condition_key(override.condition, card_id),
        )
    return ("unprunable", card_id)


def _rider_key(rider: object, card_id: str) -> object:
    """A rider is a SECOND, differently-typed effect on the same slot. Two
    outfits that agree on everything else but carry different riders are not
    ordered by the principal magnitude at all — the rider moves with the card.

    **8 of the 167 outfits in `data/cards.json` carry a rider.** Without this
    key, a rider-carrying card sharing a signature with a higher-magnitude
    rider-less one would be pruned, discarding an effect the survivor does not
    have. A costume's `score_support` rider IS scored
    (`card.scored_costume_rider_slot`, `gates.resolve_leader_costume_skills`),
    so two outfits that differ only by that rider genuinely score differently
    and MUST stay in different classes (`tests/test_costume_support_rider.py`).
    """
    if rider is None:
        return None
    if not is_known(rider):
        return ("unprunable", card_id)
    # Riders carry their own family/effect_type/magnitude/condition/target. Any
    # difference at all makes the two outfits incomparable, so the whole rider
    # goes in the key rather than a summary of it.
    return ("rider", repr(rider))


def outfit_signature(card: Card, board_discriminates: bool = False) -> tuple:
    """Everything about a card's outfit EXCEPT its magnitude.

    Two cards sharing a signature are comparable; the one with the larger
    magnitude dominates. A card whose outfit carries an unknown or
    non-comparable field gets a signature unique to itself, so it forms a class
    of one and is never pruned.

    `board_discriminates` puts the card's **holomem** back in the key. The
    Holomem Board is per-character and its red nodes are leader-scoped, so once
    a board is configured two leaders with identical outfits are no longer
    interchangeable — they bring different red nodes. Derived from `cfg.board`
    by `dominant_leaders`, never assumed: with no board configured the flag is
    `False`, and the moment a board exists the key tightens automatically
    instead of becoming silently unsound.
    """
    slot = card.costume_skill
    if slot is None:
        # No outfit at all. These are interchangeable with each other and with
        # nothing else; `outfit_magnitude` reports them as the class's floor.
        base: tuple = ("no-outfit",)
        return base + (card.member,) if board_discriminates else base
    if not is_known(slot.magnitude):
        return ("unprunable", card.id)
    signature = (
        "outfit",
        slot.family,
        slot.effect_type,
        _condition_key(slot.condition, card.id),
        _target_key(slot.target, card.id),
        _override_key(slot.override, card.id),
        _rider_key(slot.rider, card.id),
        # `probability_tier` is UNKNOWN on all 167 costume slots, so it
        # contributes nothing — but it is a per-slot field that would change how
        # the magnitude is priced, so it belongs in the key like the rider.
        slot.probability_tier if is_known(slot.probability_tier) else ("unknown-tier",),
    )
    return signature + (card.member,) if board_discriminates else signature


def outfit_magnitude(card: Card) -> float:
    """The one number a signature class is ordered by. A card with no outfit
    contributes nothing, so it sits at `0.0` — and since it is alone in the
    `("no-outfit",)` class, the value is only ever compared with itself."""
    slot = card.costume_skill
    if slot is None or not is_known(slot.magnitude):
        return 0.0
    return float(slot.magnitude)


def _is_weightless(card: Card, cfg: object) -> bool:
    """True when `cfg` prices this card's outfit family at zero.

    A zero-weighted family makes every magnitude in the class score the same,
    so the class is not ordered by magnitude at all — it is one big tie. Keeping
    the largest magnitude would then change which leader the ranking NAMES
    (`exact._heap_key` breaks a tie on the smallest leader id, which need not be
    the largest magnitude), so such a class is left whole.

    `config.json` prices `activation_rate_up`, `life_recovery` and
    `judgment_enhancement` at **0.0**. No outfit uses any of them — all 167
    are `stat_up` (157) or `score_support` (10), both at 1.0 — so this guard
    is normally inert. It exists because if an outfit family is re-weighted to
    zero, the alternative is a silently wrong leader id rather than a failing
    test.
    """
    weights = getattr(cfg, "family_weights", None)
    if weights is None:
        return False
    slot = card.costume_skill
    if slot is None:
        return False
    return weights.get(slot.family, 1.0) == 0.0


def dominant_leaders(
    pool: Iterable[Card], cfg: object | None = None
) -> tuple[Card, ...]:
    """The cards that could still be the best outfit choice, ascending by id.

    Every card tied at its class maximum survives — see this module's header
    for why keeping only one representative would change the leader the tool
    names on a tie.

    `cfg`, when supplied, is consulted for one thing only: a family the config
    prices at zero cannot be ordered by magnitude, so its whole class is kept.
    Omitting `cfg` prunes on the effect shape alone, which is what the pure
    signature reporting wants and is never *less* correct for any config that
    prices every outfit family above zero.
    """
    board_discriminates = getattr(cfg, "board", None) is not None
    groups: dict[tuple, list[Card]] = {}
    for card in pool:
        groups.setdefault(outfit_signature(card, board_discriminates), []).append(card)

    survivors: list[Card] = []
    for group in groups.values():
        if cfg is not None and any(_is_weightless(card, cfg) for card in group):
            survivors.extend(group)
            continue
        best = max(outfit_magnitude(card) for card in group)
        survivors.extend(
            card for card in group if outfit_magnitude(card) == best
        )
    return tuple(sorted(survivors, key=lambda card: card.id))


def dominant_leader_ids(
    pool: Iterable[Card], cfg: object | None = None
) -> frozenset[str]:
    """`dominant_leaders` as the id set `exact.iter_candidates` takes."""
    return frozenset(card.id for card in dominant_leaders(pool, cfg))


def dominated_by(
    pool: Iterable[Card], cfg: object | None = None
) -> dict[str, tuple[str, ...]]:
    """For reporting: each surviving leader id -> the ids it dominates.

    A pruning that cannot say WHAT it discarded is a pruning nobody can check.
    This is what a `--json` run or a bench report prints so the operator can
    see, per class, that the discarded cards really do share a signature.
    """
    board_discriminates = getattr(cfg, "board", None) is not None
    groups: dict[tuple, list[Card]] = {}
    for card in pool:
        groups.setdefault(outfit_signature(card, board_discriminates), []).append(card)

    report: dict[str, tuple[str, ...]] = {}
    for group in groups.values():
        best = max(outfit_magnitude(card) for card in group)
        winners = sorted(
            (card for card in group if outfit_magnitude(card) == best),
            key=lambda card: card.id,
        )
        losers = tuple(
            sorted(
                card.id for card in group if outfit_magnitude(card) != best
            )
        )
        for winner in winners:
            report[winner.id] = losers
    return report
