"""Per-card collection state — level, bloom and limit break — and the stat
formula that turns them into numbers.

Entry points: `collection_stats` (a collection document -> each card's stats plus a
report), `resolve_card_state`, `effective_stats` / `StatBasis.stats_at`,
`restate_card_skills` (skills at the level the bloom bought), and the account-level terms
(`memory_bonus_per_stat`, `member_power_up`, `derived_member_power_up_permyriad`).

Guarantees: `"max"` resolves per rarity and per limit break (a 5★ at `limit_break: 0` is
level 40, not 80); a partial entry inherits the rest from the default (`{"bloom": 3}` is
legal); out-of-range values are clamped and reported; an unknown card id is warned about;
and with no `card_caps.json`, `"max"` resolves to `None` and says so instead of inventing
a number (which is why `CardLevelCaps` is an input here).

## The stat formula

    stat_i = ceil(ceil(ratio_i * (level_multiplier / 1000)) * bloom_multiplier)

evaluated in IEEE-754 binary32 at every step, in that operand order, as the client's
`CardStatus` constructor does (scalar single-precision ops throughout; the only widening
to double is for `Math.Ceiling`). Details and the two-ceiling rule: `effective_stats`.

- `ceil`, not `round`: `round` puts 32 of 50 transcribed SSR cards 1 low.
- Do not evaluate in binary64 and snap before `ceil`: the client's f32 rounding pushes a
  mathematically integral product up. Takane Lui "Strategic Prodigy" at level 2 (permil
  400, `parameter_base_value` 4695) is 1878.0 exactly, which the client computes as
  1878.0001220703125 and ceils to 1879; a snapped binary64 version is wrong on 12 of the
  211,320 (card, level, bloom, stat) points in the master data.
- The operand order matters on its own: only `permil * (parameter/1000)` reproduces that
  case; the other orders give exactly 1878.0 in f32.

`bloom_multiplier` comes from master `CardPotential` via `card_caps.json`, a CUMULATIVE
array indexed by bloom (3★: `[0, 0, 50, 50, 50, 150]`, bloom 5 = 1.15). The per-level
multiplier comes through `StatBasis` from `dumps/card_level_curve.json` (master
`CardLevel`, whole curve). `effective_stats` takes the multiplier as an argument so a
caller holding one level's figure cannot apply it at another, and `StatBasis.stats_at`
returns `None` for a level the table does not define rather than the nearest one.

The check that matters is against the server's own `card_potential_parameter` on captured
decks (`test_member_parameter_reproduces_the_servers_own_figure`): `build_cards_dataset.py`
shares this arithmetic, so agreeing with its `stats_max` only catches drift between the two.

## What `data/cards.json` assumes

`data/cards.json` carries the LEVEL-2 magnitude for ACTIVE, SPECIAL and PASSIVE alike and
fully-maxed `stats`: it describes a card at max level and bloom >= 4. Bloom buys the skill
levels one step at a time (master `CardPotential`, `SKILL_BLOOM_STEP`), so a card below
bloom 4 has at least one skill kind overstated (`CardState.overstated`, `scorable` false;
`restate_card_skills` replaces those slots). Bloom 5 raises the Connect bonus to level 2
only on 4★/5★ (`connect_bonus_level`; `domain/board.py` takes `connect_multiplier` as an
input). Bloom 0 is represented, never refused: it is a real account state, just never
`scorable`.
"""

from __future__ import annotations

import dataclasses
import math
import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from .card import SKILL_SLOT_KINDS, Card, SkillSlot, skill_slot_from_record

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
    "StatBasis",
    "stat_basis_from_mapping",
    "CollectionReport",
    "collection_stats",
    "caps_by_rarity_from_mapping",
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


# What each bloom step buys, from master `CardPotential`:
#
#   bloom | effect_type | 3*                  | 4* / 5*
#   ------+-------------+---------------------+---------------------
#     1   |      1      | ACTIVE  -> level 2  | ACTIVE  -> level 2
#     2   |      4      | all params +50 permil | all params +100 permil
#     3   |      3      | SPECIAL -> level 2  | SPECIAL -> level 2
#     4   |      2      | PASSIVE -> level 2  | PASSIVE -> level 2
#     5   |    4 / 5    | all params +100 more | CONNECT bonus -> level 2
#
# The `value` on every skill row is `2`, the skill level it raises to.
SKILL_BLOOM_STEP: dict[str, int] = {"ACTIVE": 1, "SPECIAL": 3, "PASSIVE": 4}

# `data/cards.json` carries the level-2 magnitude for ACTIVE, SPECIAL and PASSIVE
# alike (master `LiveActiveSkillLevel` field 4, `LiveSpecialSkillLevel` field 3,
# `LivePassiveSkillLevel` field 4), so it describes a card at bloom >= 4.
CARDS_JSON_SKILL_LEVEL: int = 2
FULLY_SCORABLE_BLOOM: int = max(SKILL_BLOOM_STEP.values())

# Bloom 5 raises the CONNECT bonus to level 2 only on 4* and 5*; a 3* spends it on
# stats (+100 permil) and keeps a level-1 connect bonus.
CONNECT_BLOOM_STEP: int = 5
CONNECT_UPGRADE_RARITIES: frozenset[str] = frozenset({"4", "5"})


def overstated_skills(bloom: int | None) -> tuple[str, ...]:
    """Which skill kinds `data/cards.json` OVERSTATES at this bloom.

    Any kind whose bloom step this card has not reached would be scored with a
    level-2 magnitude it does not have. Returned in bloom-step order (the order the
    player unlocks them)."""
    if bloom is None:
        return ()
    return tuple(
        kind for kind, step in sorted(SKILL_BLOOM_STEP.items(), key=lambda kv: kv[1]) if bloom < step
    )


def connect_bonus_level(bloom: int | None, rarity: str | None) -> int | None:
    """The level of this card's Connect bonus when placed on a board socket.

    2 at bloom 5 on a 4*/5*, 1 otherwise, `None` if the bloom is unknown."""
    if bloom is None:
        return None
    if bloom >= CONNECT_BLOOM_STEP and rarity in CONNECT_UPGRADE_RARITIES:
        return 2
    return 1


# ---------------------------------------------------------------------------
# The skill ladder: what a card's skills actually read below their bloom step
# ---------------------------------------------------------------------------
#
# `overstated_skills` names which kinds are overstated; this section replaces them
# with the level-1 rows (e.g. a bloom-0 Ina'nis carries a 32 % `all_stats` PASSIVE
# the game scores at 24 %).
#
# A level is not a magnitude scale: 38 of 170 ACTIVE ids change `duration_ms`
# between levels 1 and 2, 2 change the activation probability, and 55 of 169
# SPECIALs change duration (Ina'nis' 5★ ACTIVE is 450 permil at both levels, 13 s
# against 14 s). So the level-1 row replaces the slot whole.
SKILL_LADDER_SCHEMA_VERSION: int = 1


def skill_level_at_bloom(kind: str, bloom: int | None) -> int | None:
    """The level this card's `kind` skill actually HAS at `bloom`.

    `None` when the bloom is unknown (not 1: a card the collection does not describe
    keeps `data/cards.json`'s value and is reported). Raises `KeyError` on a kind with
    no bloom step (the COSTUME slot: `LiveLeaderSkill` has no level column)."""
    step = SKILL_BLOOM_STEP[kind]
    if bloom is None:
        return None
    return CARDS_JSON_SKILL_LEVEL if bloom >= step else 1


def skill_ladder_from_mapping(
    raw: Mapping | None,
) -> dict[str, dict[str, dict[int, SkillSlot]]]:
    """`dumps/skill_ladder.json` -> `{card_id: {kind: {level: SkillSlot}}}`.

    `None` gives `{}`: no export means nothing to restate. An unknown `schema_version`
    is refused, since a silent shape change would be scored, not caught."""
    if raw is None:
        return {}
    version = raw.get("schema_version")
    if version != SKILL_LADDER_SCHEMA_VERSION:
        raise CollectionError(
            f"skill ladder schema_version {version!r}, expected {SKILL_LADDER_SCHEMA_VERSION}"
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
                by_kind[kind] = slots  # type: ignore[assignment]
        ladder[card_id] = by_kind
    return ladder


def restate_card_skills(
    card: Card,
    bloom: int | None,
    ladder: Mapping[str, Mapping[int, SkillSlot]],
) -> tuple[Card, tuple[str, ...]]:
    """`card` with every skill set to the level its `bloom` actually bought,
    plus one note per slot that changed or could not be changed.

    Returns `card` itself (identity, not a copy) when nothing moves. `costume_skill`
    is never touched: bloom buys nothing on the outfit."""
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
        replacement = (ladder.get(kind) or {}).get(level)  # type: ignore[arg-type]
        if replacement is None:
            # Reported, never guessed.
            notes.append(f"{kind}: no level-{level} row exported, left at level {CARDS_JSON_SKILL_LEVEL}")
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

    `level_by_limit_break` is indexed by limit break 0..4 (20/30/40/50/60 for 3*,
    30/40/50/60/70 for 4*, 40/50/60/70/80 for 5*). `bloom_mult_permil` is CUMULATIVE
    and indexed by bloom 0..5."""

    level_by_limit_break: tuple[int, ...]
    max_bloom: int
    bloom_mult_permil: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class CardState:
    """One card's resolved state. Every field may be `None`, meaning "not known"
    rather than zero (as in `domain/unknown.py`)."""

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
        """Both level and bloom present: the stat formula needs both."""
        return self.level is not None and self.bloom is not None

    @property
    def overstated(self) -> tuple[str, ...]:
        """The skill kinds `data/cards.json` overstates at this card's bloom."""
        return overstated_skills(self.bloom)

    @property
    def scorable(self) -> bool:
        """Known and at a bloom where `data/cards.json`'s magnitudes are the card's
        real ones: bloom >= 4 (the level-2 ACTIVE, SPECIAL and PASSIVE unlock at
        bloom 1, 3 and 4)."""
        return self.known and not self.overstated


def level_cap_for(caps: CardLevelCaps | None, limit_break: int | None) -> int | None:
    """The level ceiling at a given limit break, or `None` with no caps file.

    `limit_break=None` means "whatever the stated level requires": the top row, the
    LAST entry. An explicit `limit_break: 0` on a 5* caps at 40, not 80."""
    if caps is None or not caps.level_by_limit_break:
        return None
    if limit_break is None:
        return caps.level_by_limit_break[-1]
    index = max(0, min(int(limit_break), len(caps.level_by_limit_break) - 1))
    return caps.level_by_limit_break[index]


def bloom_multiplier(caps: CardLevelCaps | None, bloom: int | None) -> float | None:
    """`1 + mult_permil[bloom]/1000`, or `None` when either input is absent.

    From master `CardPotential`. The array is CUMULATIVE (3★: `[0, 0, 50, 50, 50,
    150]`), so it is indexed, never summed. `None` rather than 1.0 for an absent caps
    file: 1.0 means "bloom contributes nothing", not "unknown"."""
    if caps is None or bloom is None or not caps.bloom_mult_permil:
        return None
    if not 0 <= bloom < len(caps.bloom_mult_permil):
        return None
    # the client builds the multiplier as `1.0f + permil/1000.0f` in single precision
    permil = caps.bloom_mult_permil[bloom]
    return _f32(_F32_ONE + _f32(_f32(permil) / _F32_THOUSAND))


def _f32(value: float) -> float:
    """Round a Python float (binary64) to IEEE-754 binary32, as a scalar single SSE
    op does: every step of the client's stat chain is one."""
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

    Evaluated in single precision, in the client's operand order (module docstring).

    Two ceilings, not one: the level scaling is rounded up to an integer before the
    bloom multiplier applies, and the product is rounded up again. A single ceiling is
    short by 1 wherever the intermediate is fractional and `bloom_permil != 0`; against
    the server's own `card_potential_parameter` on captured decks it reproduces under
    half of them, two ceilings all but known-stale captures. (A dependence on level is
    a confound: at bloom 0-1 the multiplier is 1.0 and the second ceiling is a no-op.)
    The client's `CardStatus` constructor does one ceiling with the bloom bonus at 0;
    the bloom bonus (`ALL_PARAMETER_UP_PERMIL_UP`, its own potential effect type) applies
    over the resulting parameter. Rarity 5 at full bloom shares rarity 4's
    `[0,0,100,100,100,100]` array, which the captured decks cover.

    `None` on a missing input, never a partial answer."""
    if level_multiplier is None or bloom_mult is None:
        return None
    if ratio_permil is None or len(ratio_permil) != 3:
        return None
    if any(r is None for r in ratio_permil):
        return None
    # the parameter/1000 quotient is computed once and reused across the three
    # stats, as the client does
    per_mille = _f32(_f32(level_multiplier) / _F32_THOUSAND)
    mult = _f32(bloom_mult)
    values = tuple(
        # two single-precision multiplies, widened to double only for the ceiling;
        # the inner ceiling makes the level-scaled parameter an integer first
        math.ceil(_f32(_f32(math.ceil(_f32(_f32(int(r)) * per_mille))) * mult))  # type: ignore[arg-type]
        for r in ratio_permil
    )
    return (values[0], values[1], values[2])


def _normalise(entry: object) -> dict:
    """`"max"` / `"unknown"` / a dict / `None` -> a dict. An unrecognised string reads
    as `unknown`, so a typo does not silently inherit the default."""
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
        return int(value)  # type: ignore[call-overload]
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
    document's `card_default`; a key absent from `entry` falls through to `default`.
    """
    warnings: list[str] = []
    resolved = _normalise(entry)
    fallback = _normalise(default)
    for key in ("level", "bloom", "limit_break"):
        if key not in resolved and key in fallback:
            resolved[key] = fallback[key]

    limit_break = resolved.get("limit_break")
    if limit_break == MAX:
        # The top limit break for this card's rarity, so a "what-if" level is not
        # clamped by a real account's low limit break. `None` with no card_caps.json.
        limit_break = (
            len(caps.level_by_limit_break) - 1 if caps is not None and caps.level_by_limit_break else None
        )
    elif limit_break is not None:
        limit_break = _coerce_int(limit_break, "limit_break", card_id, warnings)
        if limit_break is not None and caps is not None:
            span = len(caps.level_by_limit_break) - 1
            if not 0 <= limit_break <= span:
                warnings.append(f"{card_id}: limit_break {limit_break} outside 0..{span}, clamped")
                limit_break = max(0, min(limit_break, span))

    level_cap = level_cap_for(caps, limit_break)
    max_bloom = None if caps is None else caps.max_bloom

    level = resolved.get("level")
    if level == MAX:
        level = level_cap
        if level is None:
            warnings.append(
                f"{card_id}: 'max' level with no card_caps.json — run "
                f"tools/refresh/card_caps.py. Left unknown rather than guessed"
            )
    elif level is not None:
        level = _coerce_int(level, "level", card_id, warnings)
        if level is not None:
            if level < 1:
                warnings.append(f"{card_id}: level {level} below 1, raised to 1")
                level = 1
            elif level_cap is not None and level > level_cap:
                where = (
                    f" at limit_break {limit_break}" if limit_break is not None else " at maximum limit break"
                )
                warnings.append(f"{card_id}: level {level} exceeds the cap {level_cap}{where}, clamped")
                level = level_cap

    bloom = resolved.get("bloom")
    if bloom == MAX:
        bloom = max_bloom
        if bloom is None:
            warnings.append(
                f"{card_id}: 'max' bloom with no card_caps.json — run "
                f"tools/refresh/card_caps.py. Left unknown rather than guessed"
            )
    elif bloom is not None:
        bloom = _coerce_int(bloom, "bloom", card_id, warnings)
        if bloom is not None:
            if bloom < 0:
                warnings.append(f"{card_id}: bloom {bloom} below 0, raised to 0")
                bloom = 0
            elif max_bloom is not None and bloom > max_bloom:
                warnings.append(f"{card_id}: bloom {bloom} exceeds the maximum {max_bloom}, clamped")
                bloom = max_bloom

    behind = overstated_skills(bloom)
    if behind:
        # representable, but reported as not fully scorable
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


@dataclass(frozen=True, slots=True)
class StatBasis:
    """Everything needed to turn `(card_id, level, bloom)` into three stats.

    Built from `dumps/card_level_curve.json` (`tools/refresh/card_level_curve.py`): master
    `CardLevel`'s per-level multiplier (27 groups x 60-80 levels) plus each card's level
    group, potential group and stat ratios.
    """

    level_mult: Mapping[str, Mapping[int, int]]
    bloom_permil: Mapping[str, tuple[int, ...]]
    cards: Mapping[str, tuple[str, str, tuple[int, ...]]]
    # `CardLevel` field 5 by (group, level): the Member Power-Up ladder. Empty when
    # the curve export lacks it, which `max_member_power_up_permyriad` reports as `None`.
    power_up_permyriad: Mapping[str, Mapping[int, int]] = field(default_factory=dict)

    def level_multiplier(self, card_id: str, level: int | None) -> int | None:
        """The multiplier at `level` for this card's group, or `None`.

        `None` for a level the table does not define, never the nearest one."""
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
        # single precision, as the module-level `bloom_multiplier`
        return _f32(_F32_ONE + _f32(_f32(array[bloom]) / _F32_THOUSAND))

    def stats_at(self, card_id: str, level: int | None, bloom: int | None) -> tuple[int, int, int] | None:
        """`(performance, technique, sense)` at that level and bloom, or `None` if anything
        needed is absent."""
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

    Level keys (JSON strings) become `int` here, once."""
    if not isinstance(raw, Mapping):
        raise CollectionError(f"card_level_curve must be an object, got {type(raw).__name__}")

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
    """What applying a collection did, per card, so a run can report it."""

    applied: tuple[str, ...]
    unchanged: tuple[str, ...]
    not_scorable: tuple[str, ...]
    warnings: tuple[str, ...]
    # The resolved state per card, here rather than in the stats mapping because a
    # card with no restatable stats still has a bloom (`restate_card_skills`) and a
    # level (`derived_member_power_up_permyriad`).
    states: Mapping[str, CardState] = field(default_factory=dict)


def _rarity_of(level_group: str | None) -> str | None:
    """`grp-card-level-rarity_5-03` -> `"5"`.

    The level group, not `Card.rarity`, is the authority here: it is the key the master
    tables join on. The caps lookup is keyed by rarity alone because the three attribute
    groups of a rarity in `card_caps.json` carry identical cap arrays."""
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

    A document of `card_default: "max"` reproduces `data/cards.json`'s stats exactly
    (they are the fully-maxed ones; `tests/test_collection.py` pins it). A card this
    cannot restate is omitted and named in the report, so the caller keeps
    `data/cards.json`'s value. Bloom 0 is applied for stats and flagged (not scorable).
    """
    if basis is None:
        return {}, CollectionReport(
            (),
            tuple(card_ids),
            (),
            (
                "no card_level_curve.json — run tools/refresh/card_level_curve.py; "
                "every card keeps data/cards.json's stats",
            ),
        )

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
            warnings.append(f"the collection names a card that is not in the dataset: {card_id}")

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
# The two account-level terms of Overall Power: Memory Bonus and Member Power-Up.
# Each is one scalar the player can read off their own screen.
# --------------------------------------------------------------------------

# Memory Bonus. Master `PosterCollectEffect`: 50 rows, thresholds 1..50,
# `permil(t) = min(2t, t+30)` on every row.
MEMORY_BONUS_MAX_POSTERS: int = 50


def memory_bonus_permil(posters: int | None) -> int | None:
    """The Memory Bonus rate for a poster count, or `None` if unknown.

    `min(2t, t+30)`, clamped to the table's top threshold. `None` ("not stated") and
    `0` ("stated none") are different answers; a UI must not collapse them."""
    if posters is None:
        return None
    count = max(0, min(int(posters), MEMORY_BONUS_MAX_POSTERS))
    if count == 0:
        return 0
    return min(2 * count, count + 30)


# Member Power-Up. 50 % is the game's declared cap; what is reachable is bounded by
# the roster and grows with every card, so `max_member_power_up_permyriad` derives it
# (a 167-card roster reaches 958 permyriad, 9.58 %).
MEMBER_POWER_UP_CAP_PERMYRIAD: int = 5000  # = 50.0 %, the declared ceiling


def max_member_power_up_permyriad(basis: StatBasis | None) -> int | None:
    """The largest `P` the CURRENT roster can produce: every card owned, every
    one at its maximum level.

    `P` is an account-wide sum over owned cards of a (rarity, level) ladder (master
    `CardLevel.live_deck_power_permyriad_up`, per-card maxima 4 / 5 / 8 for 3*/4*/5*),
    so the reachable maximum grows with the card list. `None` with no curve export;
    never above `MEMBER_POWER_UP_CAP_PERMYRIAD`."""
    if basis is None or not basis.power_up_permyriad:
        return None
    total = 0
    for _card_id, entry in basis.cards.items():
        ladder = basis.power_up_permyriad.get(entry[0])
        if not ladder:
            continue
        total += ladder[max(ladder)]
    return min(total, MEMBER_POWER_UP_CAP_PERMYRIAD)


# The account level ladder: master `PlayerLevel` has 50 rows and grants no board
# points. Player level gates node availability instead (`cd-player_level-N`, N in
# {10, 15, 20, 25, 30}); the highest gate is 30, but an account past 30 must still
# be able to state its level, so the cap is 50.
PLAYER_LEVEL_MAX: int = 50
PLAYER_LEVEL_MIN: int = 1

# The sentinel that turns `player.power_up_permyriad` from a number the player
# maintains into one the tool derives. See `DerivedMemberPowerUp`.
POWER_UP_AUTO: str = "auto"


@dataclass(frozen=True, slots=True)
class AccountBonuses:
    """The account-level scalars the player supplies by hand, and nothing else.

    Every field is `None` until the player sets it: `None` means "not stated", `0`
    means "stated they have none".

    `level` is not a bonus and never enters a score: it gates which board nodes the
    account may take. It lives here because it is read from the same hand-edited
    `player` block."""

    posters: int | None = None
    power_up_permyriad: int | None = None
    level: int | None = None
    # `player.power_up_permyriad: "auto"`: derive it from the account's own cards
    # every run (`derived_member_power_up_permyriad`) instead of a typed number that
    # goes stale when a card levels.
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
        # Refused rather than clamped: clamping a typo like 500 to 50 would unlock
        # the whole board on a mistake.
        raise CollectionError(f"player.level must be {PLAYER_LEVEL_MIN}..{PLAYER_LEVEL_MAX}, got {level}")

    # `"auto"` is checked before `read`, which would refuse it as a non-number.
    # Absent is not auto: absent means "unset, do not score this term".
    raw_power_up = player.get("power_up_permyriad")
    is_auto = isinstance(raw_power_up, str) and raw_power_up.strip().lower() == POWER_UP_AUTO
    power_up = None if is_auto else read("power_up_permyriad")
    if power_up is not None and power_up > MEMBER_POWER_UP_CAP_PERMYRIAD:
        # Only catches order-of-magnitude typos (951 for a true 39 passes);
        # `derived_member_power_up_permyriad` catches the rest by computing the
        # value from the account's cards and reporting a disagreement.
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

    Master `CardLevel.live_deck_power_permyriad_up`: an account-wide sum, over every
    card the player owns, of a cumulative (rarity, level) ladder whose per-card maxima
    are 4 / 5 / 8 for 3*/4*/5* (the server ships it as
    `CardGetParametersResponse.LiveDeckPowerPermyriadUpByCardLevelUp`). The 5* ladder is
    not linear: +1 permyriad at Lv20/40/50/60/70 and +3 at Lv80.

    Only a level the player recorded enters the total: an owned card with no
    `state/collection.json` entry under `card_default: "max"` resolves to the cap, and
    would feed the top rung. Such cards are `skipped`, and the two bounds bracket the
    truth: `permyriad` credits the recorded cards only, `upper` also credits every skipped
    card its ladder's top rung, so a partial derivation can still refute a wrong value."""

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

        `None` (unset) never disagrees; inside `[permyriad, upper]` it cannot tell."""
        if typed is None:
            return False
        return not self.permyriad <= typed <= self.upper


def derived_member_power_up_permyriad(
    basis: StatBasis | None,
    states: Mapping[str, CardState],
    owned_ids: Sequence[str],
) -> DerivedMemberPowerUp | None:
    """`P` for this account, or `None` when the curve export cannot answer.

    `None` rather than `0`: no ladder means unknown, not "earned none"."""
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
        # `"max"` and `"default"` are this module's fallbacks, not the account
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
    return DerivedMemberPowerUp(permyriad=total, upper=upper, counted=counted, skipped=tuple(skipped))


def maxed_account_bonuses(basis: StatBasis | None, posters: int | None = None) -> AccountBonuses:
    """The account-level bonuses a FULLY MAXED account would carry.

    The cards need no work: `data/cards.json` holds fully-maxed stats (and at bloom 5
    every skill is at `CARDS_JSON_SKILL_LEVEL`), so a maxed run simply does not apply the
    collection. The Holomem Board is not included: a member's best branches depend on
    the song, the cast and the event, so the account's real board stays in place and a
    maxed run says so.

    `power_up_permyriad` is the roster ceiling (`max_member_power_up_permyriad`).
    `posters` is how many posters the game has (master `Poster` rows): a maxed account
    holds every one, and `None` leaves the Memory Bonus unset."""
    return AccountBonuses(
        posters=None if posters is None else max(0, min(int(posters), MEMORY_BONUS_MAX_POSTERS)),
        power_up_permyriad=max_member_power_up_permyriad(basis),
        level=PLAYER_LEVEL_MAX,
        # never auto: that would have `apply_collection` overwrite the roster ceiling
        # with the real account's figure
        power_up_permyriad_is_auto=False,
    )


def memory_bonus(member_parameter_total: float, permil: int | None) -> int | None:
    """The Memory Bonus summand of Overall Power, or `None` if unset.

    The whole-deck approximation of `memory_bonus_per_stat` (one ceiling instead of
    fifteen): always low, by about 7-8 points (0.006-0.013 % of Overall Power), for
    callers without the per-member, per-stat split. The base is the Member Parameter
    line alone."""
    if permil is None:
        return None
    return math.ceil(round(member_parameter_total * permil / 1000, 6))


def memory_bonus_per_stat(per_member_stats: Sequence[tuple[int, int, int]], permil: int | None) -> int | None:
    """The Memory Bonus summand of Overall Power, BIT-EXACT — or `None` if unset.

    The server rounds up once per member and per stat (fifteen ceilings for five
    members):

        memory = SUM over the members, over {performance, technique, sense}
                     of ceil( memberParameter_stat * R / 1000 )

    `R` is `memory_bonus_permil` (`permil` here); each `memberParameter_stat` is the
    member's leveled and bloomed base stat alone (no board, outfit or passive), i.e.
    `StatBasis.stats_at` / `Card.stats`. Bit-exact against the server's own
    `power.memory` on every captured deck. Integer arithmetic: `-(-stat * R // 1000)`
    is the exact ceiling."""
    if permil is None:
        return None
    total = 0
    for stats in per_member_stats:
        for stat in stats:
            total += -(-int(stat) * permil // 1000)
    return total


def member_power_up(per_member_bases: Sequence[float], permyriad: int | None) -> int | None:
    """The Member Power-Up summand of Overall Power, or `None` if unset.

        SUM over the 5 member slots of ceil( base_m * P / 10000 )

    `base_m` is that member's `MemberParameter + PassiveSkill + OutfitSkill +
    HolomemBoard`. The Memory Bonus is excluded (it applies to the parameter line; the
    power-up to the pre-memory subtotal). The Outfit Skill line is included: the call
    site passes `effective_stat_sum + board_power`, which carries the costume's stat
    effect; without it the captured decks admit no single `P`
    (`test_member_power_up_base_includes_the_outfit_skill_line`). Five ceilings, one per
    member, is the only count consistent with the captured gains."""
    if permyriad is None:
        return None
    return sum(math.ceil(round(base * permyriad / 10000, 6)) for base in per_member_bases)


def caps_by_rarity_from_mapping(raw: Mapping) -> dict[str, CardLevelCaps]:
    """`card_caps.json` keyed by rarity, which is all the caps depend on (`_rarity_of`)."""
    out: dict[str, CardLevelCaps] = {}
    for group, caps in (raw.get("level_cap_by_limit_break") or {}).items():
        rarity = group.split("-", 1)[0].removeprefix("rarity_")
        potential = (raw.get("potential_by_rarity") or {}).get(rarity) or {}
        out[rarity] = CardLevelCaps(
            level_by_limit_break=tuple(int(v) for v in caps),
            max_bloom=potential.get("max"),  # type: ignore[arg-type]
            bloom_mult_permil=tuple(int(v) for v in (potential.get("mult_permil") or ())),
        )
    return out
