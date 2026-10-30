"""Per-card collection state — level, bloom and limit break — and the stat
formula that turns them into numbers.

Behaviours this module guarantees:

* `"max"` resolves against the game's real ceilings **per rarity and per limit
  break** — `max` with `limit_break: 0` on a 5★ is **40**, not 80;
* a partial entry inherits the rest from the default (`{"bloom": 3}` is legal);
* out-of-range values are **clamped and REPORTED**, never silently corrected;
* an id naming a card outside the dataset is **warned about**, not ignored;
* and the one that matters most: with no `card_caps.json`, `"max"` resolves to
  `None` and says so, instead of inventing a number.

That last behaviour is why `CardLevelCaps` is an optional input here rather
than a lookup this module performs for itself.


## The stat formula

    stat_i = ceil(ceil(ratio_i * (level_multiplier / 1000)) * bloom_multiplier)

evaluated in **IEEE-754 binary32 at every step**, in that operand order.

**Two ceilings.** The level-scaled parameter is rounded up to an integer
BEFORE the bloom multiplier applies. A single-ceiling form is short by exactly
1 wherever the intermediate is fractional AND `bloom_permil != 0` — see
`effective_stats` for the evidence and for the level/bloom confound that hides
it.

`ceil` is not a rounding choice made here: with `round()`, 32 of the 50
transcribed SSR cards come out exactly 1 low on their two smaller stats and
exact on the largest. `ceil` reproduces all three exactly.

**Single precision is read from the client** — `CardStatus::.ctor`
(`Vision.Common.Core`, RVA `0x52acff0`), whose arithmetic is scalar single
throughout:

    movss    xmm7, [0x75ceef0]   ; 00 00 7a 44 = 1000.0f
    cvtsi2ss xmm6, rbx           ; CardLevel.parameter_base_value
    divss    xmm6, xmm7          ;   -> parameter / 1000.0f, computed ONCE
    movss    xmm0, [0x75cebd4]   ; 00 00 80 3f = 1.0f
    cvtsi2ss xmm10, rdx
    divss    xmm10, xmm7
    addss    xmm10, xmm0         ;   -> 1.0f + bonus_permil / 1000.0f
    cvtsi2ss xmm0, ebx           ; the stat's permil multiplier
    mulss    xmm0, xmm6
    mulss    xmm0, xmm10
    cvtps2pd xmm0                ; the ONE widening, and it is the last step
    call     0x759c510           ; Math.Ceiling(double)
    cvttsd2si eax

`cvtsi2ss` / `divss` / `mulss` / `addss` — not one `sd` op in the chain. The
`cvtps2pd` exists only because C#'s `Math.Ceiling` takes a `double`; reading
that widening as evidence of double-precision arithmetic is a trap.

**Do not snap before `ceil`.** Evaluating in binary64 and snapping the product
to a few decimals (so a mathematically-integral product is not pushed up by
representation error) is wrong: the client has no such snap, and its own f32
rounding pushes the product up on exactly those inputs. Takane Lui "Strategic
Prodigy" at level 2 (permil 400, `parameter_base_value` 4695) is
0.400 x 4695 = 1878.0 exactly, which the client computes as 1878.0001220703125
and ceils to **1879**. Over every (card, level, bloom, stat) point in the
master data — 211,320 of them — a snapped binary64 version differs on **12**,
all of that shape, across 3 cards, and is wrong on all 12.

The operand order is load-bearing on its own: `(permil/1000) * parameter` and
`permil * parameter / 1000` both give exactly 1878.0 in f32 and would hide the
divergence. Only the client's `permil * (parameter/1000)` reproduces it.

`test_collection.py` checks this module against `build_cards_dataset.py`'s
output on every card rather than trusting the two to stay in step — maxed
stats are unaffected by the f32 detail, so only a sub-max check can catch it.

`bloom_multiplier` comes from master `CardPotential`, exported into
`card_caps.json` as a CUMULATIVE `mult_permil` array indexed by bloom:
rarity 3 is `[0, 0, 50, 50, 50, 150]`, so bloom 5 gives 1.15 — which is exactly
the `stats_max.at.bloom_mult` the dataset builder records independently.

The **per-LEVEL** multiplier arrives through `StatBasis`, from
`dumps/card_level_curve.json`. `build_cards_dataset.py` reads the same master
`CardLevel` table but keeps only each group's value at its MAXIMUM level,
because maxed stats are all it needs; `tools/recon/proxy/card_level_curve.py`
exports the whole curve (27 groups, 1,890 (group, level) rows, levels
1..60/70/80, multipliers 3432..15444 on the first group).

`effective_stats` takes the multiplier as an argument rather than looking it
up, so a caller holding a curve for one level cannot silently apply it at
another. `StatBasis.stats_at` is the lookup, and it returns `None` for a level
the table does not define rather than the nearest one — the game's curve is the
only authority on what a level is worth.

**The `stats_max` comparison against `build_cards_dataset.py` is circular.**
That builder's chain is kept identical to this module's, so both sides
implement the same rounding assumption and the comparison passes whatever the
rule is. It can catch a DRIFT between the two implementations; it can never
catch a shared wrong assumption.

**The non-circular check: 29 of 32 captured decks reproduce the SERVER's own
`card_potential_parameter` exactly** (`dumps/deck_oracle.json`; under one
ceiling it is 15 of 32, every residual negative). The three that do not are
stale captures, and their residuals are +5,457 / +1,246 / -833 — a different
magnitude and sign from any rounding effect.
`test_member_parameter_reproduces_the_servers_own_figure` pins it. Prefer that
comparison to anything computed on this side of the wire: two pipelines that
share a line of arithmetic prove nothing about it.


## What `data/cards.json` assumes, and what a low bloom breaks

`data/cards.json` carries the LEVEL-2 magnitude for ACTIVE, SPECIAL and
PASSIVE alike (against master `LiveActiveSkillLevel` field 4,
`LiveSpecialSkillLevel` field 3, `LivePassiveSkillLevel` field 4 — no card
matches level 1 on any kind). And its `stats` are the fully-maxed ones.

So the dataset describes a card at **max level and bloom >= 4**.

Bloom buys those upgrades one step at a time — from master `CardPotential`,
every row of all three rarity groups:

| bloom | 3* | 4* / 5* |
|---|---|---|
| 1 | ACTIVE -> level 2 | ACTIVE -> level 2 |
| 2 | all parameters +50 permil | all parameters +100 permil |
| 3 | SPECIAL -> level 2 | SPECIAL -> level 2 |
| 4 | PASSIVE -> level 2 | PASSIVE -> level 2 |
| 5 | all parameters +100 permil more | CONNECT bonus -> level 2 |

Two consequences this module encodes:

1. **A card below bloom 4 has at least one skill kind OVERSTATED** by the
   dataset. `CardState.overstated` names which, and `scorable` is false while
   any remain. Bloom 1 upgrades only the ACTIVE; SPECIAL and PASSIVE need
   blooms 3 and 4.
2. **A 3* never reaches Connect bonus level 2.** Its bloom 5 buys stats, not the
   connect upgrade — only 4* and 5* get that. `connect_bonus_level` is the rule,
   and it is what `domain/board.py` needs: that module records
   `connect_multiplier` as an INPUT because "the placed card's Bloom level is
   not recoverable from the captured state", and a collection document makes it
   recoverable.

Bloom 0 is still represented rather than refused — it is a real account state
and refusing to record it would misdescribe the collection — it is simply
never `scorable`.
"""

from __future__ import annotations

import dataclasses
import math
import struct
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from .card import Card, SkillSlot, SKILL_SLOT_KINDS, skill_slot_from_record

__all__ = [
    "MAX",
    "UNKNOWN",
    "CardLevelCaps",
    "CardState",
    "CollectionError",
    "bloom_multiplier",
    "effective_stats",
    "level_cap_for",
    "overstated_skills",
    "connect_bonus_level",
    "resolve_card_state",
    "resolve_collection",
    "StatBasis",
    "stat_basis_from_mapping",
    "CollectionReport",
    "collection_stats",
    "caps_by_rarity_from_mapping",
    "MEMORY_BONUS_CAP_PERMIL",
    "MEMORY_BONUS_MAX_POSTERS",
    "MEMBER_POWER_UP_CAP_PERMYRIAD",
    "PLAYER_LEVEL_MAX",
    "PLAYER_LEVEL_MIN",
    "memory_bonus_permil",
    "max_member_power_up_permyriad",
    "AccountBonuses",
    "account_bonuses_from_mapping",
    "POWER_UP_AUTO",
    "maxed_account_bonuses",
    "DerivedMemberPowerUp",
    "derived_member_power_up_permyriad",
    "memory_bonus",
    "memory_bonus_per_stat",
    "member_power_up",
    "SKILL_LADDER_SCHEMA_VERSION",
    "skill_level_at_bloom",
    "skill_ladder_from_mapping",
    "restate_card_skills",
]


class CollectionError(ValueError):
    """A collection document that cannot be interpreted at all — not a value out
    of range, which is clamped and reported, but a shape this module refuses to
    guess at."""


MAX = "max"
UNKNOWN = "unknown"

# The number of bloom steps the game defines. From master `CardPotential`:
# every rarity carries upgrade_count 1..5.
BLOOM_STEPS: int = 6

# What each bloom step buys, from master `CardPotential` (every row of all
# three rarity groups):
#
#   bloom | effect_type | 3*                  | 4* / 5*
#   ------+-------------+---------------------+---------------------
#     1   |      1      | ACTIVE  -> level 2  | ACTIVE  -> level 2
#     2   |      4      | all params +50 permil | all params +100 permil
#     3   |      3      | SPECIAL -> level 2  | SPECIAL -> level 2
#     4   |      2      | PASSIVE -> level 2  | PASSIVE -> level 2
#     5   |    4 / 5    | all params +100 more | CONNECT bonus -> level 2
#
# The `value` on every skill row is literally `2` — the skill LEVEL — which is
# what ties this table to the one below.
SKILL_BLOOM_STEP: dict[str, int] = {"ACTIVE": 1, "SPECIAL": 3, "PASSIVE": 4}

# `data/cards.json` carries the LEVEL-2 magnitude for ACTIVE, SPECIAL and
# PASSIVE alike — checked against master `LiveActiveSkillLevel` field 4,
# `LiveSpecialSkillLevel` field 3 and `LivePassiveSkillLevel` field 4, every
# card, none matching level 1.
#
# So `data/cards.json` describes a card at **bloom >= 4**, and a card below that
# has at least one skill kind OVERSTATED by the dataset. Bloom >= 1 is not
# enough: SPECIAL needs bloom 3 and PASSIVE needs bloom 4.
CARDS_JSON_SKILL_LEVEL: int = 2
FULLY_SCORABLE_BLOOM: int = max(SKILL_BLOOM_STEP.values())

# Bloom 5 upgrades the card's CONNECT bonus to level 2 — but only on 4* and 5*.
# A 3* spends its bloom 5 on stats instead (effect_type 4, +100 permil) and its
# connect bonus stays at level 1 forever. From the same master rows.
# `domain/board.py` records `connect_multiplier` as an INPUT because "the placed
# card's Bloom level is not recoverable from the captured state"; a collection
# document makes it recoverable, and this is the rule.
CONNECT_BLOOM_STEP: int = 5
CONNECT_UPGRADE_RARITIES: frozenset[str] = frozenset({"4", "5"})


def overstated_skills(bloom: int | None) -> tuple[str, ...]:
    """Which skill kinds `data/cards.json` OVERSTATES at this bloom.

    `data/cards.json` holds level-2 magnitudes for all three kinds, so any kind whose bloom step this card has not reached is being
    scored with a magnitude it does not have. Returned in bloom-step order so
    the message reads in the order the player unlocks them."""
    if bloom is None:
        return ()
    return tuple(
        kind
        for kind, step in sorted(SKILL_BLOOM_STEP.items(), key=lambda kv: kv[1])
        if bloom < step
    )


def connect_bonus_level(bloom: int | None, rarity: str | None) -> int | None:
    """The level of this card's Connect bonus when placed on a board socket.

    2 at bloom 5 on a 4*/5*, 1 otherwise, `None` if the bloom is unknown. A 3*
    never reaches level 2 — its bloom 5 buys stats instead."""
    if bloom is None:
        return None
    if bloom >= CONNECT_BLOOM_STEP and rarity in CONNECT_UPGRADE_RARITIES:
        return 2
    return 1


# ---------------------------------------------------------------------------
# The skill ladder: what a card's skills actually read below their bloom step
# ---------------------------------------------------------------------------
#
# `overstated_skills` above NAMES which kinds are overstated; this section
# replaces them. Restating only `stats` and leaving the skills at
# `data/cards.json`'s level-2 values is visibly wrong against the game: Ina'nis
# and Takane Lui at bloom 0 carry a 32 % `all_stats` PASSIVE the game scores at
# 24 %, and `32/24 = 1.333` is exactly the +32.5 % by which the Passive Skill
# power term then misses.
#
# ⚠ **A level is not a magnitude scale.** Over all three skill masters, 38 of
# 170 ACTIVE ids change `duration_ms` between levels 1 and 2,
# 2 change the activation probability, and 55 of 169 SPECIALs change duration.
# Ina'nis' own 5★ ACTIVE is one of them — 450 permil at BOTH levels, 13 s at
# level 1 against 14 s at level 2. So the level-1 row REPLACES the slot whole.
# Rescaling `magnitude` would be a no-op on that card while leaving it a second
# of uptime it does not have: right-looking and wrong.
SKILL_LADDER_SCHEMA_VERSION: int = 1


def skill_level_at_bloom(kind: str, bloom: int | None) -> int | None:
    """The level this card's `kind` skill actually HAS at `bloom`.

    `None` when the bloom is unknown — and that is not 1. A card the collection
    does not describe must keep `data/cards.json`'s value and be reported, the
    same absent-is-not-zero contract `AccountBonuses` holds. Raises `KeyError`
    on a kind with no bloom step (the COSTUME slot has none: `LiveLeaderSkill`
    is one row per card with no level column at all)."""
    step = SKILL_BLOOM_STEP[kind]
    if bloom is None:
        return None
    return CARDS_JSON_SKILL_LEVEL if bloom >= step else 1


def skill_ladder_from_mapping(
    raw: Mapping | None,
) -> dict[str, dict[str, dict[int, SkillSlot]]]:
    """`dumps/skill_ladder.json` -> `{card_id: {kind: {level: SkillSlot}}}`.

    `None` gives `{}`, the same first-class-absent contract
    `card_level_curve.json` has: no export means there is nothing to restate,
    never a restatement to zero. A `schema_version` this module does not know
    is refused rather than read optimistically — the slot shape is exactly
    `data/cards.json`'s and a silent shape change would be scored, not caught."""
    if raw is None:
        return {}
    version = raw.get("schema_version")
    if version != SKILL_LADDER_SCHEMA_VERSION:
        raise CollectionError(
            f"skill ladder schema_version {version!r}, expected "
            f"{SKILL_LADDER_SCHEMA_VERSION}"
        )
    cards = raw.get("cards") or {}
    if not isinstance(cards, Mapping):
        raise CollectionError("a skill ladder's 'cards' must be an object")
    ladder: dict[str, dict[str, dict[int, SkillSlot]]] = {}
    for card_id, rungs in cards.items():
        by_kind: dict[str, dict[int, SkillSlot]] = {}
        for kind, by_level in (rungs or {}).items():
            slots = {
                int(level): skill_slot_from_record(record, kind)
                for level, record in (by_level or {}).items()
                if record is not None
            }
            if slots:
                by_kind[kind] = slots
        ladder[card_id] = by_kind
    return ladder


def restate_card_skills(
    card: Card,
    bloom: int | None,
    ladder: Mapping[str, Mapping[int, SkillSlot]],
) -> tuple[Card, tuple[str, ...]]:
    """`card` with every skill set to the level its `bloom` actually bought,
    plus one note per slot that changed or could not be changed.

    Returns `card` ITSELF (identity, not a copy) when nothing moves, so a caller
    can tell "restated" from "unchanged" without comparing field by field.

    `costume_skill` is never touched: the outfit is `LiveLeaderSkill`, one row
    per card with no level column, and bloom buys nothing on it."""
    if bloom is None:
        return card, ()

    notes: list[str] = []
    slots = list(card.skills)
    changed = False
    for index, kind in enumerate(SKILL_SLOT_KINDS):
        slot = slots[index]
        if slot is None:
            continue
        level = skill_level_at_bloom(kind, bloom)
        if level == CARDS_JSON_SKILL_LEVEL:
            continue
        replacement = (ladder.get(kind) or {}).get(level)
        if replacement is None:
            # Reported, never guessed.
            notes.append(
                f"{kind}: no level-{level} row exported, left at level "
                f"{CARDS_JSON_SKILL_LEVEL}"
            )
            continue
        slots[index] = replacement
        notes.append(f"{kind} {CARDS_JSON_SKILL_LEVEL}->{level}")
        changed = True

    if not changed:
        return card, tuple(notes)
    return dataclasses.replace(card, skills=tuple(slots)), tuple(notes)


@dataclass(frozen=True, slots=True)
class CardLevelCaps:
    """The game's own ceilings for one rarity/attribute group, read from
    `card_caps.json` (master `CardLevelLimit` + `CardPotential`).

    `level_by_limit_break` is indexed by limit break 0..4 — in master data
    20/30/40/50/60 for 3*, 30/40/50/60/70 for 4*, 40/50/60/70/80 for 5*.
    `bloom_mult_permil` is CUMULATIVE and indexed by bloom 0..5."""

    level_by_limit_break: tuple[int, ...]
    max_bloom: int
    bloom_mult_permil: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class CardState:
    """One card's resolved state. Every field may be `None`, and `None` means
    "not known" rather than zero — the same distinction `domain/unknown.py`
    draws for card fields, and for the same reason: a fabricated number that
    looks like data is worse than an absent one."""

    card_id: str
    level: int | None
    bloom: int | None
    limit_break: int | None
    level_cap: int | None
    max_bloom: int | None
    source: str  # "manual" | "default" | "max" | "unknown"
    warnings: tuple[str, ...] = ()

    @property
    def known(self) -> bool:
        """Both numbers present. A card with a level and no bloom is NOT known:
        the stat formula needs both, and half of it produces nothing."""
        return self.level is not None and self.bloom is not None

    @property
    def overstated(self) -> tuple[str, ...]:
        """The skill kinds `data/cards.json` overstates at this card's bloom.
        See `overstated_skills` — it is not just ACTIVE, and not just bloom 0."""
        return overstated_skills(self.bloom)

    @property
    def scorable(self) -> bool:
        """Known AND at a bloom where `data/cards.json`'s magnitudes are the
        card's real ones — which is **bloom >= 4**, not bloom >= 1.

        The dataset holds LEVEL-2 magnitudes for ACTIVE,
        SPECIAL and PASSIVE alike, and those unlock at bloom 1, 3 and 4. A card
        at bloom 1 has a correct ACTIVE and an overstated SPECIAL and PASSIVE.
        `overstated` names which."""
        return self.known and not self.overstated


def level_cap_for(caps: CardLevelCaps | None, limit_break: int | None) -> int | None:
    """The level ceiling at a given limit break, or `None` with no caps file.

    `limit_break=None` means "whatever the stated level requires", which is the
    game's own top row — the LAST entry, not the first. Getting this backwards
    is the `max` + `limit_break: 0` case the tests pin: on a 5*
    that is 40 and not 80."""
    if caps is None or not caps.level_by_limit_break:
        return None
    if limit_break is None:
        return caps.level_by_limit_break[-1]
    index = max(0, min(int(limit_break), len(caps.level_by_limit_break) - 1))
    return caps.level_by_limit_break[index]


def bloom_multiplier(caps: CardLevelCaps | None, bloom: int | None) -> float | None:
    """`1 + mult_permil[bloom]/1000`, or `None` when either input is absent.

    From master `CardPotential`. The array is CUMULATIVE — rarity 3 is
    `[0, 0, 50, 50, 50, 150]`, so it is indexed, never summed; summing it would
    give 1.30 where the game gives 1.15, and 1.15 is what `stats_max` was
    independently built with.

    Returns `None` rather than 1.0 for an absent caps file. 1.0 is a real
    multiplier meaning "bloom contributes nothing", and it must not be
    indistinguishable from "it is not known what bloom contributes"."""
    if caps is None or bloom is None or not caps.bloom_mult_permil:
        return None
    if not 0 <= bloom < len(caps.bloom_mult_permil):
        return None
    # `divss` then `addss`, in that order — the ctor builds each stat's
    # multiplier as `1.0f + permil/1000.0f` in single precision.
    permil = caps.bloom_mult_permil[bloom]
    return _f32(_F32_ONE + _f32(_f32(permil) / _F32_THOUSAND))


def _f32(value: float) -> float:
    """Round a Python float (binary64) to IEEE-754 binary32, the way an `ss`
    SSE instruction does. Every step of the client's stat chain is a scalar
    single op, so every step here is wrapped in this."""
    return struct.unpack("<f", struct.pack("<f", value))[0]


_F32_THOUSAND = _f32(1000.0)
_F32_ONE = _f32(1.0)


def effective_stats(
    ratio_permil: Sequence[int | None],
    level_multiplier: float | None,
    bloom_mult: float | None,
) -> tuple[int, int, int] | None:
    """`ceil(ceil(ratio_i * (level_multiplier/1000)) * bloom_mult)` per stat, in
    (performance, technique, sense) order — or `None` if any input is missing.

    Evaluated in SINGLE precision, in the client's own operand order, because
    that is what the client does — see the module docstring.

    ⛔ TWO ceilings, not one. The level scaling is rounded up to an integer
    BEFORE the bloom multiplier is applied, and the product is rounded up
    again. A single-ceiling form is short by 1 on any cell where the
    intermediate is fractional AND `bloom_permil != 0`.

    Evidence, against the wire's own `card_potential_parameter` over the 32
    decks in `dumps/deck_oracle.json`:

        one ceiling  15/32 exact   residuals -1 .. -6, every one NEGATIVE
        two ceilings 29/32 exact   residuals only on idx 18/19/23

    and those three are known stale captures — their residuals are +5,457 /
    +1,246 / -833, a different magnitude and a different sign from any rounding
    effect.

    ⚠ An apparent correlation with LEVEL is a confound. At bloom 0 and 1
    `bloom_permil` is 0, so `mult == 1.0` and the second ceiling is a no-op; in
    the captured account the only high-level cards happen to be bloom 0-1. The
    driver is BLOOM, not level. That is also why every all-level-70 deck reads
    residual 0 under BOTH rules.

    ⚠ Reconciling the `CardStatus::.ctor` disassembly, which does exactly ONE
    ceiling: that ctor is stage one, where `bonus_permil` is 0. The bloom bonus
    is a separate effect — `enums_card_potential_effect_type` carries
    `ALL_PARAMETER_UP_PERMIL_UP` as its own type — applied over the parameter
    rather than as a factor inside it. This is inferred, not read from code.

    ⚠ The max-level-plus-max-bloom case is inferred by transfer, not measured:
    there is no wire observation at rarity 5 with full bloom. The decisive
    `bloom_permil = 100` cells come from rarity 4, which uses the identical
    `[0,0,100,100,100,100]` array.

    `None` on a missing input, never a partial answer: two of three stats is not
    a card, and returning zeros for the third would be a fabricated number."""
    if level_multiplier is None or bloom_mult is None:
        return None
    if ratio_permil is None or len(ratio_permil) != 3:
        return None
    if any(r is None for r in ratio_permil):
        return None
    # `divss xmm6, xmm7` — the parameter/1000 quotient is computed ONCE and
    # reused across the three stats, exactly as the ctor does.
    per_mille = _f32(_f32(level_multiplier) / _F32_THOUSAND)
    mult = _f32(bloom_mult)
    values = tuple(
        # `mulss` twice, then `cvtps2pd` + `Math.Ceiling`: the widening to
        # double happens after the arithmetic, never during it. The inner
        # ceiling is the level-scaled parameter becoming an integer before the
        # bloom effect multiplies it — see the two-ceiling note above.
        math.ceil(_f32(_f32(math.ceil(_f32(_f32(int(r)) * per_mille))) * mult))
        for r in ratio_permil
    )
    return (values[0], values[1], values[2])


def _normalise(entry: object) -> dict:
    """`"max"` / `"unknown"` / a dict / `None` -> a dict, so the resolver deals
    with one shape. An unrecognised string reads as `unknown`, deliberately: a
    typo must not silently inherit the default and look like a real answer."""
    if entry is None:
        return {}
    if isinstance(entry, str):
        if entry.strip().lower() == MAX:
            return {"level": MAX, "bloom": MAX}
        return {"level": None, "bloom": None}
    if isinstance(entry, dict):
        return dict(entry)
    return {}


def _coerce_int(value: object, label: str, card_id: str, warn: list[str]) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        warn.append(f"{card_id}: {label} is not a number ({value!r}), ignored")
        return None


def resolve_card_state(
    card_id: str,
    entry: object,
    default: object,
    caps: CardLevelCaps | None,
) -> CardState:
    """One card's `(level, bloom, limit_break)`, resolved against the game's
    ceilings, with every clamp reported rather than applied silently.

    `entry` is that card's record from the collection document, `default` the
    document's `card_default`. A key absent from `entry` falls through to
    `default` — so `{"bloom": 3}` is a legal entry that takes its level from the
    default, which is the shape a UI produces when only one control is touched.
    """
    warnings: list[str] = []
    resolved = _normalise(entry)
    fallback = _normalise(default)
    for key in ("level", "bloom", "limit_break"):
        if key not in resolved and key in fallback:
            resolved[key] = fallback[key]

    limit_break = resolved.get("limit_break")
    if limit_break == MAX:
        # The top limit break for this card's rarity/attribute, so a "what-if"
        # level assumption is not silently clamped by a real account's low limit
        # break. Mirrors how `level`/`bloom` accept "max" below. `None` when there
        # is no card_caps.json — the same first-class unknown those two report.
        limit_break = (
            len(caps.level_by_limit_break) - 1
            if caps is not None and caps.level_by_limit_break
            else None
        )
    elif limit_break is not None:
        limit_break = _coerce_int(limit_break, "limit_break", card_id, warnings)
        if limit_break is not None and caps is not None:
            span = len(caps.level_by_limit_break) - 1
            if not 0 <= limit_break <= span:
                warnings.append(
                    f"{card_id}: limit_break {limit_break} outside 0..{span}, clamped"
                )
                limit_break = max(0, min(limit_break, span))

    level_cap = level_cap_for(caps, limit_break)
    max_bloom = None if caps is None else caps.max_bloom

    level = resolved.get("level")
    if level == MAX:
        level = level_cap
        if level is None:
            warnings.append(
                f"{card_id}: 'max' level with no card_caps.json — run "
                f"tools/recon/proxy/card_caps.py. Left unknown rather than guessed"
            )
    elif level is not None:
        level = _coerce_int(level, "level", card_id, warnings)
        if level is not None:
            if level < 1:
                warnings.append(f"{card_id}: level {level} below 1, raised to 1")
                level = 1
            elif level_cap is not None and level > level_cap:
                where = (
                    f" at limit_break {limit_break}"
                    if limit_break is not None
                    else " at maximum limit break"
                )
                warnings.append(
                    f"{card_id}: level {level} exceeds the cap {level_cap}{where}, clamped"
                )
                level = level_cap

    bloom = resolved.get("bloom")
    if bloom == MAX:
        bloom = max_bloom
        if bloom is None:
            warnings.append(
                f"{card_id}: 'max' bloom with no card_caps.json — run "
                f"tools/recon/proxy/card_caps.py. Left unknown rather than guessed"
            )
    elif bloom is not None:
        bloom = _coerce_int(bloom, "bloom", card_id, warnings)
        if bloom is not None:
            if bloom < 0:
                warnings.append(f"{card_id}: bloom {bloom} below 0, raised to 0")
                bloom = 0
            elif max_bloom is not None and bloom > max_bloom:
                warnings.append(
                    f"{card_id}: bloom {bloom} exceeds the maximum {max_bloom}, clamped"
                )
                bloom = max_bloom

    behind = overstated_skills(bloom)
    if behind:
        # Representable, and deliberately not silently scorable. See the module
        # docstring.
        warnings.append(
            f"{card_id}: at bloom {bloom}, data/cards.json's level-"
            f"{CARDS_JSON_SKILL_LEVEL} magnitudes OVERSTATE its "
            f"{'/'.join(behind)} (unlocked at bloom "
            f"{'/'.join(str(SKILL_BLOOM_STEP[k]) for k in behind)}). Recorded, "
            f"and not fully scorable"
        )

    if level is None and bloom is None:
        source = UNKNOWN
    elif entry is None:
        source = "default"
    elif isinstance(entry, str) and entry.strip().lower() == MAX:
        source = MAX
    else:
        source = "manual"

    return CardState(
        card_id=card_id,
        level=level,
        bloom=bloom,
        limit_break=limit_break,
        level_cap=level_cap,
        max_bloom=max_bloom,
        source=source,
        warnings=tuple(warnings),
    )


def resolve_collection(
    cards: Sequence[tuple[str, int, int]],
    document: Mapping | None,
    caps_by_group: Mapping[str, CardLevelCaps] | None,
) -> tuple[dict[str, CardState], list[str]]:
    """Every card's state, plus the document-level warnings.

    `cards` is a sequence of `(card_id, rarity_rank, attribute)` triples rather
    than card records, so this module stays independent of whatever shape the
    caller happens to hold.

    An id in the document that names no card in `cards` is **warned about, not
    ignored**: it is how a renamed or removed card shows up, and dropping it
    silently is how a collection rots against `cards.json` without anyone
    noticing.
    """
    if document is None:
        document = {}
    if not isinstance(document, Mapping):
        raise CollectionError(
            f"a collection document must be an object, got {type(document).__name__}"
        )

    entries = document.get("cards") or {}
    if not isinstance(entries, Mapping):
        raise CollectionError("collection document's 'cards' must be an object")
    default = document.get("card_default", UNKNOWN)

    warnings: list[str] = []
    known_ids = {card_id for card_id, _, _ in cards}
    for card_id in entries:
        if card_id not in known_ids:
            warnings.append(
                f"the collection names a card that is not in the dataset: {card_id}"
            )

    states: dict[str, CardState] = {}
    for card_id, rarity_rank, attribute in cards:
        caps = None
        if caps_by_group is not None:
            caps = caps_by_group.get(f"rarity_{rarity_rank}-attribute_{attribute}")
            if caps is None:
                warnings.append(
                    f"{card_id}: no level caps for "
                    f"rarity_{rarity_rank}-attribute_{attribute}"
                )
        states[card_id] = resolve_card_state(card_id, entries.get(card_id), default, caps)

    return states, warnings


@dataclass(frozen=True, slots=True)
class StatBasis:
    """Everything needed to turn `(card_id, level, bloom)` into three stats.

    Built from `dumps/card_level_curve.json` (`tools/recon/proxy/card_level_curve.py`), which
    exports master `CardLevel`'s **per-level** multiplier — 27 groups x 60-80 levels, 1,890
    rows — plus each card's level group, potential group and stat ratios.

    Checked by the exporter against `build_cards_dataset.py`'s `stats_max`: every card
    reproduces exactly. `tests/test_collection.py` re-runs that comparison through THIS class
    rather than trusting the script's own report — a pipeline that reports its own success
    proves nothing.
    """

    level_mult: Mapping[str, Mapping[int, int]]
    bloom_permil: Mapping[str, tuple[int, ...]]
    cards: Mapping[str, tuple[str, str, tuple[int, ...]]]
    # `CardLevel` field 5 by (group, level) — the Member Power-Up ladder. Empty
    # when the curve export predates it, which `max_member_power_up_permyriad`
    # reports as `None` rather than as a zero ceiling.
    power_up_permyriad: Mapping[str, Mapping[int, int]] = field(
        default_factory=dict
    )

    def level_multiplier(self, card_id: str, level: int | None) -> int | None:
        """The multiplier at `level` for this card's group, or `None`.

        `None` for a level the table does not define, never the nearest one: the curve is the
        game's own and a level outside it is a question this module cannot answer."""
        if level is None:
            return None
        entry = self.cards.get(card_id)
        if entry is None:
            return None
        return self.level_mult.get(entry[0], {}).get(int(level))

    def bloom_multiplier(self, card_id: str, bloom: int | None) -> float | None:
        """`1 + permil[bloom]/1000` for this card's potential group, or `None`.

        The array runs to the group's MAX bloom, padded past its last stat step:
        rarities 4 and 5 carry a stat permil only at bloom 2, so blooms 3-5 leave the
        multiplier where it was rather than being undefined."""
        if bloom is None:
            return None
        entry = self.cards.get(card_id)
        if entry is None:
            return None
        array = self.bloom_permil.get(entry[1])
        if not array or not 0 <= bloom < len(array):
            return None
        # `divss` then `addss`, in single precision — the same chain as the
        # module-level `bloom_multiplier`, and for the same reason.
        return _f32(_F32_ONE + _f32(_f32(array[bloom]) / _F32_THOUSAND))

    def stats_at(
        self, card_id: str, level: int | None, bloom: int | None
    ) -> tuple[int, int, int] | None:
        """`(performance, technique, sense)` at that level and bloom, or `None` if anything
        needed is absent. Never a partial answer — see `effective_stats`."""
        entry = self.cards.get(card_id)
        if entry is None:
            return None
        return effective_stats(
            entry[2],
            self.level_multiplier(card_id, level),
            self.bloom_multiplier(card_id, bloom),
        )


def stat_basis_from_mapping(raw: Mapping) -> StatBasis:
    """`card_level_curve.json` into a `StatBasis`.

    The file stores level keys as JSON strings (JSON object keys are always strings); they are
    converted to `int` here, once, rather than at every lookup."""
    if not isinstance(raw, Mapping):
        raise CollectionError(
            f"card_level_curve must be an object, got {type(raw).__name__}"
        )

    level_mult: dict[str, dict[int, int]] = {}
    for group, levels in (raw.get("level_mult") or {}).items():
        level_mult[group] = {int(k): int(v) for k, v in levels.items()}

    bloom: dict[str, tuple[int, ...]] = {}
    for group, entry in (raw.get("bloom_permil_by_group") or {}).items():
        array = entry.get("mult_permil") if isinstance(entry, Mapping) else entry
        bloom[group] = tuple(int(v) for v in (array or ()))

    cards: dict[str, tuple[str, str, tuple[int, ...]]] = {}
    for card_id, entry in (raw.get("cards") or {}).items():
        ratio = entry.get("ratio_permil") or ()
        cards[card_id] = (
            entry.get("level_group"),
            entry.get("potential_group"),
            tuple(ratio),
        )

    power_up: dict[str, dict[int, int]] = {}
    for group, levels in (raw.get("power_up_permyriad") or {}).items():
        power_up[group] = {int(k): int(v) for k, v in levels.items()}

    return StatBasis(
        level_mult=level_mult,
        bloom_permil=bloom,
        cards=cards,
        power_up_permyriad=power_up,
    )


@dataclass(frozen=True, slots=True)
class CollectionReport:
    """What applying a collection did, per card, so a run can SAY it rather
    than quietly returning different numbers than it did yesterday."""

    applied: tuple[str, ...]
    unchanged: tuple[str, ...]
    not_scorable: tuple[str, ...]
    warnings: tuple[str, ...]
    # The resolved state per card. It rides here rather than in the stats
    # mapping because a card can have a known level and bloom and no restatable
    # stats, and both `restate_card_skills` (which needs the bloom) and
    # `derived_member_power_up_permyriad` (which needs the level) still want it
    # in that case.
    states: Mapping[str, CardState] = field(default_factory=dict)

    def summary(self) -> str:
        parts = [f"{len(self.applied)} card(s) restated from the collection"]
        if self.unchanged:
            parts.append(f"{len(self.unchanged)} left at data/cards.json's value")
        if self.not_scorable:
            parts.append(
                f"{len(self.not_scorable)} not fully scorable — stats applied, "
                f"but data/cards.json's level-2 skill magnitudes overstate them "
                f"below bloom 4"
            )
        return "; ".join(parts)


def _rarity_of(level_group: str | None) -> str | None:
    """`grp-card-level-rarity_5-03` -> `"5"`.

    The card's own level group is the authority on its rarity here, rather than
    `Card.rarity`: it is the key the master tables themselves join on, and it
    is already carried per card by the curve export.

    This is why the caps lookup below is keyed by rarity ALONE:
    `card_caps.json` holds three attribute groups per rarity and all three carry
    an IDENTICAL cap array (3*: 20/30/40/50/60, 4*: 30/40/50/60/70, 5*:
    40/50/60/70/80). Attribute does not move the level cap."""
    if not level_group:
        return None
    _, _, tail = level_group.partition("rarity_")
    return tail.split("-", 1)[0] or None


def collection_stats(
    card_ids: Sequence[str],
    document: Mapping | None,
    caps_by_rarity: Mapping[str, CardLevelCaps] | None,
    basis: StatBasis | None,
) -> tuple[dict[str, tuple[int, int, int]], CollectionReport]:
    """`{card_id: (performance, technique, sense)}` for every card the
    collection can restate, plus a report naming every card it could not.

    **A document of `card_default: "max"` reproduces `data/cards.json`'s stats
    exactly.** That is not a coincidence to rely on quietly — every card in
    `data/cards.json` carries its FULLY-MAXED stats (equal to `stats_max` in
    `cards_enriched.json`). So a ranking without a collection assumes a
    fully-maxed collection, and `"max"` is the state that reproduces it.
    `tests/test_collection.py` pins the equality so the wiring cannot silently
    move that answer.

    A card this cannot restate is **omitted from the result and named in the
    report**, never given a substitute number: the caller then leaves
    `data/cards.json`'s value in place, which is the authoritative one.

    Bloom 0 is applied for STATS and flagged, because the stat formula is
    defined there and the skill magnitudes are not — see the module docstring
    and `CardState.scorable`.
    """
    if basis is None:
        return {}, CollectionReport((), tuple(card_ids), (), (
            "no card_level_curve.json — run tools/recon/proxy/card_level_curve.py; "
            "every card keeps data/cards.json's stats",
        ))

    entries = (document or {}).get("cards") or {}
    default = (document or {}).get("card_default", UNKNOWN)

    stats: dict[str, tuple[int, int, int]] = {}
    applied: list[str] = []
    unchanged: list[str] = []
    not_scorable: list[str] = []
    warnings: list[str] = []
    states: dict[str, CardState] = {}

    known = set(basis.cards)
    for card_id in entries:
        if card_id not in known:
            warnings.append(
                f"the collection names a card that is not in the dataset: {card_id}"
            )

    for card_id in card_ids:
        entry = basis.cards.get(card_id)
        rarity = _rarity_of(entry[0]) if entry else None
        caps = (caps_by_rarity or {}).get(rarity) if rarity else None

        state = resolve_card_state(card_id, entries.get(card_id), default, caps)
        warnings.extend(state.warnings)
        states[card_id] = state

        resolved = basis.stats_at(card_id, state.level, state.bloom)
        if resolved is None:
            unchanged.append(card_id)
            continue
        stats[card_id] = resolved
        applied.append(card_id)
        if not state.scorable:
            not_scorable.append(card_id)

    return stats, CollectionReport(
        applied=tuple(applied),
        unchanged=tuple(unchanged),
        not_scorable=tuple(not_scorable),
        warnings=tuple(warnings),
        states=states,
    )


# --------------------------------------------------------------------------
# The two account-level terms the game shows and this project does not compute:
# Memory Bonus and Member Power-Up. Both are ONE scalar the player can read off
# their own screen, so both are inputs here, not derivations.
# --------------------------------------------------------------------------

# Memory Bonus. From master `PosterCollectEffect`: 50 rows, thresholds dense
# 1..50, `permil(t) = min(2t, t+30)` with ZERO violations across all 50.
MEMORY_BONUS_MAX_POSTERS: int = 50
MEMORY_BONUS_CAP_PERMIL: int = 80  # = 8.0 %, at 50 posters


def memory_bonus_permil(posters: int | None) -> int | None:
    """The Memory Bonus rate for a poster count, or `None` if unknown.

    `min(2t, t+30)`, clamped to the table's own top threshold. The closed form
    was checked against all 50 master rows and fits every one.

    ⚠ `None` and `0` are different answers and both are legitimate: `None` is
    "the player has not said", `0` is "the player said they have none".
    A UI must not collapse them."""
    if posters is None:
        return None
    count = max(0, min(int(posters), MEMORY_BONUS_MAX_POSTERS))
    if count == 0:
        return 0
    return min(2 * count, count + 30)


# Member Power-Up. The 50 % ceiling is the game's own declared cap and is where
# the ladder is HEADED as more cards ship. What is reachable at any moment is
# bounded by the roster instead, and that bound moves every time a card is
# added, so it is DERIVED by `max_member_power_up_permyriad` rather than written
# here (a 167-card roster reaches 958 permyriad, 9.58 %).
MEMBER_POWER_UP_CAP_PERMYRIAD: int = 5000  # = 50.0 %, the declared ceiling


def max_member_power_up_permyriad(basis: StatBasis | None) -> int | None:
    """The largest `P` the CURRENT roster can produce: every card owned, every
    one at its maximum level.

    Derived, never typed. `P` is an account-wide sum over owned cards of a
    (rarity, level) ladder — master `CardLevel.live_deck_power_permyriad_up`,
    27 groups collapsing to 3 ladders with per-card maxima 4 / 5 / 8 for
    3*/4*/5* — so the reachable maximum is a property of the card list and grows
    with it. Hardcoding a current figure would silently under-state the control's
    range the day a card ships.

    Returns `None` with no curve export, the same first-class-absent contract
    the rest of this module has. Never exceeds
    `MEMBER_POWER_UP_CAP_PERMYRIAD`: the roster bound is what is reachable, the
    declared cap is what the mechanic allows, and the smaller of the two is the
    honest ceiling for a control."""
    if basis is None or not basis.power_up_permyriad:
        return None
    total = 0
    for _card_id, entry in basis.cards.items():
        ladder = basis.power_up_permyriad.get(entry[0])
        if not ladder:
            continue
        total += ladder[max(ladder)]
    return min(total, MEMBER_POWER_UP_CAP_PERMYRIAD)


# The account level ladder. The `PlayerLevel` master has **50 rows**, and its
# three columns are `level, exp, rewards` -- none of them points, so player level
# funds no board budget. What it does fund is AVAILABILITY: 127 of the 152 node
# groups carry a `cd-player_level-N` gate, N in {10, 15, 20, 25, 30} (per
# `skilltree.json`: 47/26/26/11/17, and the other 25 nodes carry no `req` at all).
# So the highest gate
# that can ever bind is 30 -- but the ladder runs to 50 and an account past 30 must
# still be able to say where it is, so this is the cap, not 30.
PLAYER_LEVEL_MAX: int = 50
PLAYER_LEVEL_MIN: int = 1

# The sentinel that turns `player.power_up_permyriad` from a number the player
# maintains into one the tool derives. See `DerivedMemberPowerUp`.
POWER_UP_AUTO: str = "auto"


@dataclass(frozen=True, slots=True)
class AccountBonuses:
    """The account-level scalars the player supplies by hand, and nothing else.

    Every field is `None` until the player sets it, and `None` is NOT `0`:
    `None` means "not stated", `0` means "stated they have none". Every
    consumer must be able to say which, because reporting an unset slider as a
    measured zero would present a guess as data.

    `level` is the odd one out and is deliberately here anyway: it is not a
    bonus, it grants nothing, and it never enters a score. It gates which board
    nodes the account may take at all, which is a availability question rather
    than a scoring one -- but it is read from the same hand-edited `player`
    block as the other two, so it is validated in the same place."""

    posters: int | None = None
    power_up_permyriad: int | None = None
    level: int | None = None
    # `player.power_up_permyriad: "auto"` -- compute it from the account's own
    # cards every run instead of reading a number that goes stale the next time
    # a card levels. See `derived_member_power_up_permyriad`; a hand-typed value
    # can be wildly wrong and still look plausible.
    power_up_permyriad_is_auto: bool = False

    @property
    def memory_permil(self) -> int | None:
        return memory_bonus_permil(self.posters)


def account_bonuses_from_mapping(document: Mapping | None) -> AccountBonuses:
    """The `player` block of a collection document. Absent keys stay `None`."""
    player = (document or {}).get("player") or {}
    if not isinstance(player, Mapping):
        raise CollectionError("a collection document's 'player' must be an object")

    def read(key: str) -> int | None:
        value = player.get(key)
        if value is None:
            return None
        try:
            return max(0, int(value))
        except (TypeError, ValueError) as exc:
            raise CollectionError(f"player.{key} must be a number, got {value!r}") from exc

    level = read("level")
    if level is not None and not PLAYER_LEVEL_MIN <= level <= PLAYER_LEVEL_MAX:
        # Refused rather than clamped. A level outside the ladder is a typo, and
        # silently pulling 500 down to 50 would unlock the whole board on a
        # mistake -- the one direction where being wrong is invisible.
        raise CollectionError(
            f"player.level must be {PLAYER_LEVEL_MIN}..{PLAYER_LEVEL_MAX}, got {level}"
        )

    # `"auto"` is checked BEFORE `read`, which would otherwise refuse it as a
    # non-number. Absent is deliberately NOT auto: absent has always meant
    # "unset, do not score this term", and quietly starting to score it would
    # move every existing answer on a document nobody edited.
    raw_power_up = player.get("power_up_permyriad")
    is_auto = isinstance(raw_power_up, str) and raw_power_up.strip().lower() == POWER_UP_AUTO
    power_up = None if is_auto else read("power_up_permyriad")
    if power_up is not None and power_up > MEMBER_POWER_UP_CAP_PERMYRIAD:
        # A range check. It only covers the order-of-magnitude class of typo:
        # a hand-typed `951` for a true `39` would pass, because the
        # roster-reachable maximum over a 169-card catalogue is 974 and the
        # declared cap here is 5,000. What catches that is
        # `derived_member_power_up_permyriad` -- the value is computable from
        # the account's own cards, and the tool computes it and says when the
        # file disagrees.
        raise CollectionError(
            f"player.power_up_permyriad must be 0..{MEMBER_POWER_UP_CAP_PERMYRIAD} "
            f"(= {MEMBER_POWER_UP_CAP_PERMYRIAD / 100:.0f} %, the declared "
            f"ceiling), got {power_up}"
        )

    return AccountBonuses(
        posters=read("posters"),
        power_up_permyriad=power_up,
        level=level,
        power_up_permyriad_is_auto=is_auto,
    )


@dataclass(frozen=True, slots=True)
class DerivedMemberPowerUp:
    """`P` computed from the account's own cards instead of typed by hand.

    Master `CardLevel.live_deck_power_permyriad_up`: an account-wide sum,
    over every card the player OWNS, of a cumulative (rarity, level) ladder
    whose per-card maxima are 4 / 5 / 8 for 3*/4*/5*. The server also ships it
    directly as `CardGetParametersResponse.LiveDeckPowerPermyriadUpByCardLevelUp`.

    **Corroborated independently.** A player on the Taiwanese Bahamut board
    (`forum.gamer.com.tw` board 84454, thread `snA=603`) measured the per-card
    contribution BY HAND, in-game, and reported: Lv20 +0.01, Lv40 +0.02, Lv50
    +0.03, Lv60 +0.04, Lv70 +0.05, **Lv80 +0.08** -- a +0.03 step where the
    pattern says +0.01. The 5* ladder read from master data is `Lv20 +1, Lv40
    +1, Lv50 +1, Lv60 +1, Lv70 +1, Lv80 +3` permyriad, i.e. exactly those
    percentages including the irregular top rung.

    Keep the irregular Lv80 rung in mind before "simplifying" this into a linear
    step: it is worth 3 permyriad per maxed 5*, and mismodelling it moves an
    account total by 2-3 permyriad, which propagates into every Overall Power.

    `skipped` is load-bearing, not a diagnostic. An owned card with no
    `state/collection.json` entry, under a `card_default` of `"max"`, has a
    level that RESOLVES (it is not `None`) to the cap, and would feed the
    ladder's top rung -- two such cards can turn a true 38 into 51.
    **Provenance decides, not whether a number came out**: only a level the
    player actually recorded may enter an account total.

    A skipped card has not vanished, though, so this carries BOTH bounds:
    `permyriad` credits the recorded cards only, `upper` additionally credits
    every skipped card its ladder's top rung. That is what lets a partial
    derivation still refute a wrong value instead of shrugging -- e.g. 951
    sits far outside `[38, 51]`."""

    permyriad: int
    upper: int
    counted: int
    skipped: tuple[str, ...]

    @property
    def exact(self) -> bool:
        """Every owned card had a recorded level, so the two bounds meet."""
        return not self.skipped

    def disagrees_with(self, typed: int | None) -> bool:
        """Whether a hand-typed value is outside what the cards can produce.

        `None` never disagrees -- an unset field is a question the player has
        not answered, not a wrong answer. Inside `[permyriad, upper]` this stays
        silent: there it genuinely cannot tell."""
        if typed is None:
            return False
        return not self.permyriad <= typed <= self.upper


def derived_member_power_up_permyriad(
    basis: StatBasis | None,
    states: Mapping[str, CardState],
    owned_ids: Sequence[str],
) -> DerivedMemberPowerUp | None:
    """`P` for this account, or `None` when the curve export cannot answer.

    `None` rather than `0`: no ladder means the tool does not know, which is a
    different claim from "this account has earned none"."""
    if basis is None or not basis.power_up_permyriad:
        return None
    total = 0
    upper = 0
    counted = 0
    skipped: list[str] = []
    for card_id in owned_ids:
        entry = basis.cards.get(card_id)
        ladder = basis.power_up_permyriad.get(entry[0]) if entry else None
        state = states.get(card_id)
        level = state.level if state else None
        # A level the player did not record is not evidence. `"max"` and
        # `"default"` are this module's own fallbacks, not the account.
        recorded = state is not None and state.source == "manual"
        if ladder is None or level is None or not recorded:
            skipped.append(card_id)
            if ladder:
                upper += ladder[max(ladder)]
            continue
        contribution = ladder.get(int(level), 0)
        total += contribution
        upper += contribution
        counted += 1
    return DerivedMemberPowerUp(
        permyriad=total, upper=upper, counted=counted, skipped=tuple(skipped)
    )


def maxed_account_bonuses(basis: StatBasis | None) -> AccountBonuses:
    """The account-level bonuses a FULLY MAXED account would carry.

    The other half of the maxed basis — the cards themselves — needs no work at
    all: `data/cards.json` already holds every card's fully-maxed stats, so a
    maxed run is simply one that does not apply the collection.

    ⚠ **Max bloom means no skill down-levelling**, and that is not a shortcut:
    at bloom 5 every kind IS at `CARDS_JSON_SKILL_LEVEL`, so the catalogue's
    magnitudes are exactly right, so no open question about down-levelled
    ACTIVE/SPECIAL skills can reach this basis.

    ⚠ **The Holomem Board is NOT included and must never be implied.** What a
    maxed board even looks like is undecided: at character level 50 a Leader has
    no choice left, but a MEMBER's best branches depend on the song, the cast
    and whether an event bonus is in play. So the account's real board stays in
    place and every maxed run SAYS so, rather than inventing a build and burying
    the assumption.

    `power_up_permyriad` is the ROSTER ceiling, not the account's: every card at
    its own max level, which is what `max_member_power_up_permyriad` already
    computes and why that function was written to grow with the card list.
    `None` there rather than `0` when no curve is exported, as everywhere."""
    return AccountBonuses(
        posters=MEMORY_BONUS_MAX_POSTERS,
        power_up_permyriad=max_member_power_up_permyriad(basis),
        level=PLAYER_LEVEL_MAX,
        # Never auto: `"auto"` derives from the ACCOUNT's cards, and this basis
        # has no account. Leaving it set would have `apply_collection` overwrite
        # the roster ceiling with the real account's figure.
        power_up_permyriad_is_auto=False,
    )


def memory_bonus(member_parameter_total: float, permil: int | None) -> int | None:
    """The Memory Bonus summand of Overall Power, or `None` if unset.

    ⚠ **This is the whole-deck APPROXIMATION, and the error is measured.** The
    exact form (`memory_bonus_exact`) rounds up per member AND per parameter —
    15 ceilings, not one:

        exact = SUM over 5 members, over {performance, technique, sense}
                    of ceil( memberParameter_ij * R / 1000 )

    That needs a per-member, per-stat decomposition of the parameter line, which
    the ranking path does not carry. `ceil(total * R / 1000)` instead lands
    **-7 to -8 absolute** against the exact figure on the five captured decks —
    0.006–0.013 % of Overall Power. It is stated here
    so nobody quotes this as exact, and so the sign is on the record: the
    approximation is always LOW, never high.

    The base is the Member Parameter line ALONE: measured at 44.097–44.300
    permil of it over five decks, a 0.46 % spread; against MP+Passive it reads
    41.2, against MP+Board 30.1, against Overall Power 27.8 — all refuted."""
    if permil is None:
        return None
    return math.ceil(round(member_parameter_total * permil / 1000, 6))


def memory_bonus_per_stat(
    per_member_stats: Sequence[tuple[int, int, int]], permil: int | None
) -> int | None:
    """The Memory Bonus summand of Overall Power, BIT-EXACT — or `None` if unset.

    This is the exact form the whole-deck `memory_bonus` above only approximates.
    The server rounds up ONCE PER member AND PER stat — fifteen ceilings for a
    five-member deck, not one:

        memory = SUM over the members, over {performance, technique, sense}
                     of ceil( memberParameter_stat * R / 1000 )

    `R` is `memory_bonus_permil` (`permil` here). Each `memberParameter_stat` is
    that member's leveled+bloomed base parameter for one stat — the base card
    potential ALONE (no board, outfit or passive), which is exactly what
    `StatBasis.stats_at` returns and what the ranking path carries as
    `Card.stats` (see `apply_collection`, `stats[card_id] = basis.stats_at(...)`).

    Bit-exact against the server's own `power.memory`: 2989/2989 applicable
    rows of `dumps/bonus_extract_db.jsonl`. The whole-deck
    `ceil(total * R / 1000)` hits 24 of those — it collapses fifteen
    fractional parts into one ceiling and so runs LOW, never high.

    Integer arithmetic: `-(-stat * R // 1000)` is `ceil(stat * R / 1000)`
    exactly for the non-negative integer stats and rate this takes — no float
    rounding to reason about, and it agrees with the reference fit's
    `math.ceil` on every measured row."""
    if permil is None:
        return None
    total = 0
    for stats in per_member_stats:
        for stat in stats:
            total += -(-int(stat) * permil // 1000)
    return total


def member_power_up(
    per_member_bases: Sequence[float], permyriad: int | None
) -> int | None:
    """The Member Power-Up summand of Overall Power, or `None` if unset.

    Exact, not an approximation — the ceiling is per member slot and that is the
    granularity a caller can actually supply:

        SUM over the 5 member slots of ceil( base_m * P / 10000 )

    `base_m` is that member's
    `MemberParameter + PassiveSkill + OutfitSkill + HolomemBoard`.
    **The Memory Bonus is excluded from the base** — measured, and it is what
    orders the two mechanics: memory applies to the parameter line, training to
    the pre-memory subtotal. Including it is refuted at every candidate P.

    ⛔ **The Outfit Skill line is part of the base.** The call site passes
    `effective_stat_sum + board_power`, and `effective_stat_sum` already
    carries the costume's stat effect.

    Checked against the wire, `deck_oracle.json`, all 32 rows: solving each row
    for the integer `P` it admits (`base*P/10000 <= wire < base*P/10000 + 5`)
    gives **P = 39 on rows 0-20 and P = 41 on rows 21-31, with no exceptions**,
    where 39 is exactly what `derived_member_power_up_permyriad` computes from
    `state/collection.json` and the step is the account levelling between the
    collection snapshot and the later captures. Drop the Outfit line from
    the base and the same test is **infeasible on 12 of the 32 rows** — it
    survives only where that row's outfit line is zero. `tests/test_collection.py
    ::test_member_power_up_base_includes_the_outfit_skill_line` pins both halves.

    That the five ceilings are PER MEMBER is inferred, not read from code: the band
    test 0 < gain <= 5 is necessary and not sufficient, and N = 5 is the only
    structural reading inside the admissible band [4, 6]."""
    if permyriad is None:
        return None
    return sum(
        math.ceil(round(base * permyriad / 10000, 6)) for base in per_member_bases
    )


def caps_by_rarity_from_mapping(raw: Mapping) -> dict[str, CardLevelCaps]:
    """`card_caps.json` keyed by RARITY, which is all the caps actually depend
    on — see `_rarity_of` for the measurement."""
    out: dict[str, CardLevelCaps] = {}
    for group, caps in (raw.get("level_cap_by_limit_break") or {}).items():
        rarity = group.split("-", 1)[0].removeprefix("rarity_")
        potential = (raw.get("potential_by_rarity") or {}).get(rarity) or {}
        out[rarity] = CardLevelCaps(
            level_by_limit_break=tuple(int(v) for v in caps),
            max_bloom=potential.get("max"),
            bloom_mult_permil=tuple(int(v) for v in (potential.get("mult_permil") or ())),
        )
    return out


def caps_from_mapping(raw: Mapping) -> dict[str, CardLevelCaps]:
    """`card_caps.json`'s two tables into one lookup keyed the way the file
    keys its level caps (`rarity_<R>-attribute_<A>`).

    The bloom arrays are keyed by rarity ALONE in that file while the level caps
    are keyed by rarity AND attribute, so this join is where the two shapes are
    reconciled — in one place, rather than at every call site."""
    levels = raw.get("level_cap_by_limit_break") or {}
    potentials = raw.get("potential_by_rarity") or {}

    out: dict[str, CardLevelCaps] = {}
    for group, caps in levels.items():
        rarity = group.split("-", 1)[0].removeprefix("rarity_")
        potential = potentials.get(rarity) or {}
        permil = potential.get("mult_permil") or ()
        out[group] = CardLevelCaps(
            level_by_limit_break=tuple(int(v) for v in caps),
            max_bloom=potential.get("max"),
            bloom_mult_permil=tuple(int(v) for v in permil),
        )
    return out
