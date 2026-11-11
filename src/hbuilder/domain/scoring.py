"""Stage three of the three-stage team evaluation (see `domain/gates.py`'s
module docstring): reduce resolved skills into a scored float.

The song-agnostic scorers (`score_fast`, `score_card_set`, `score_with_breakdown`,
`deck_screen_unit_score`) return an **average per-run rate**, never a point
total: song length cancels out of every team-vs-team comparison. The one
duration they use is the finite ACTIVE reference window
(`_finite_window_uptime`, `scoring_config.REFERENCE_RUN_SECONDS`). The
per-song scorer that does take a chart is `song.py`.
"""

from __future__ import annotations

import math  # noqa: E401
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from functools import cache

from hbuilder.domain import targets
from hbuilder.domain.card import Card, EffectTarget, SlotRider
from hbuilder.domain.collection import _f32, member_power_up, memory_bonus_per_stat
from hbuilder.domain.gates import (
    ResolvedSkill,
    resolve_card_set_gates,
    resolve_leader_costume_skills,
    resolve_team_gates,
)
from hbuilder.domain.schema import (
    COSTUME_SLOT_KIND,
    EFFECT_TYPES_BY_FAMILY,
    FAMILY_REQUIRES_SLOT_KIND,
    NOT_APPLICABLE_FIELDS_BY_SLOT_KIND,
    SKILL_SLOT_KINDS,
    STAT_NAMES,
    TEAM_WIDE_TARGET_KIND,
)

# The result types live in `score_breakdown`; re-exported so `scoring.X` keeps working.
from hbuilder.domain.score_breakdown import UNIT_SCORE_K as UNIT_SCORE_K
from hbuilder.domain.score_breakdown import DeckScreenScore as DeckScreenScore
from hbuilder.domain.score_breakdown import ScoreBreakdown as ScoreBreakdown
from hbuilder.domain.score_breakdown import SkillContribution as SkillContribution
from hbuilder.domain.score_breakdown import deck_screen_unit_score as deck_screen_unit_score
from hbuilder.domain.scoring_config import (
    REFERENCE_RUN_SECONDS,
    SLOT_ORDER_RULES,
    ScoringConfig,
)
from hbuilder.domain.team import Team
from hbuilder.domain.unknown import is_known


class ScoringError(Exception):
    """Scoring asked to do something structurally invalid (e.g. an EV function on a slot of
    the wrong kind). `str()` names the card id and slot kind when known."""

    def __init__(
        self,
        message: str,
        *,
        card_id: str | None = None,
        slot_kind: str | None = None,
    ) -> None:
        self.card_id = card_id
        self.slot_kind = slot_kind
        location_parts = []
        if card_id is not None:
            location_parts.append(f"card={card_id}")
        if slot_kind is not None:
            location_parts.append(f"slot_kind={slot_kind}")
        full_message = message
        if location_parts:
            full_message = f"{message} ({', '.join(location_parts)})"
        super().__init__(full_message)


def _as_rate(magnitude: float) -> float:
    """Percentage points to a fraction (25.0 means +25 %). Every EV formula converts
    on-disk `magnitude` through this helper, never inline."""
    return magnitude / 100.0


# Which score "pool" each `effect_type` feeds. `activation_rate` feeds no pool: it is
# folded into `team_rate_up` by `_fold_activation_rate_up` and never receives the team
# modifier itself.
POOL_BY_EFFECT_TYPE: dict[str, str] = {
    "performance": "stat",
    "technique": "stat",
    "sense": "stat",
    "all_stats": "stat",
    "score": "score",
    "life": "score",
    "judgment": "score",
    "activation_rate": "none",
}


# `score_support` does not add to the score pool: it MULTIPLIES the ACTIVE pool. The
# game's effect ids say so (`score_up_EFFECT_up_permil_up` on every SPECIAL,
# `live_active_skill_EFFECT_up_permil_up` on every Score Support outfit, against a plain
# `score_up_permil_up` on every ACTIVE), and solving the reference duration from ten
# captured decks gives a constant within 6.2 % under the multiplier reading against a
# 90 % spread under the addend reading.
#
# Routing is by FAMILY here, not `effect_type`: the on-disk `effect_type` of a
# `score_support` slot is `"score"`, and `data/cards.json` is authoritative and never
# rewritten by code.
SUPPORT_FAMILY = "score_support"


def _pool_for(resolved: ResolvedSkill) -> str:
    """The pool a resolved skill feeds: `support` for the `score_support` family,
    otherwise `POOL_BY_EFFECT_TYPE[effect_type]`."""
    if resolved.family == SUPPORT_FAMILY:
        return "support"
    return POOL_BY_EFFECT_TYPE[resolved.skill.effect_type]


# Every effect_type any family can carry must have a pool, so an effect_type added to
# schema.py without one fails at import, not as a KeyError inside a solver run.
assert set(POOL_BY_EFFECT_TYPE) == set().union(*EFFECT_TYPES_BY_FAMILY.values()), (
    "POOL_BY_EFFECT_TYPE must cover every effect_type declared in "
    "EFFECT_TYPES_BY_FAMILY — an unrouted effect_type must fail at import, "
    "never at score time"
)


# Which stats each effect_type raises. The accumulation iterates the tuple, so
# `all_stats` needs no branch of its own.
STAT_NAMES_BY_EFFECT_TYPE: dict[str, tuple[str, ...]] = {
    "performance": ("performance",),
    "technique": ("technique",),
    "sense": ("sense",),
    "all_stats": STAT_NAMES,
}

# Every "stat"-pool effect_type must name real stats, so a half-updated edit of the
# three tables fails at import.
assert set(STAT_NAMES_BY_EFFECT_TYPE) == {
    effect_type for effect_type, pool in POOL_BY_EFFECT_TYPE.items() if pool == "stat"
}, (
    "STAT_NAMES_BY_EFFECT_TYPE must cover every effect_type routed to the "
    "'stat' pool by POOL_BY_EFFECT_TYPE — an effect_type missing its "
    "stat-name declaration must fail at import, never at score time"
)
assert all(
    stat_name in STAT_NAMES for stat_names in STAT_NAMES_BY_EFFECT_TYPE.values() for stat_name in stat_names
), "STAT_NAMES_BY_EFFECT_TYPE values must only name real STAT_NAMES members"
# `_stat_sum` scatters a rate into one bucket per stat name: a repeat would count it twice.
assert all(len(set(stat_names)) == len(stat_names) for stat_names in STAT_NAMES_BY_EFFECT_TYPE.values()), (
    "STAT_NAMES_BY_EFFECT_TYPE values must not repeat a stat name — `_stat_sum` scatters"
)


# The fields that must be known before a skill may contribute at all (`condition`
# decides whether the formula runs; the rest are its inputs). One list serves both
# `_resolve_skill` (via `_first_missing_field`) and `_fold_activation_rate_up` (via
# `_has_all_known_fields`), so the two paths cannot drift.
#
# Order sets the first-missing report:
# - `condition` first: `gates._resolve_one_skill` collapses an unrecorded gate and an
#   ungated skill both to `gate = None`, and this is where they are told apart again.
#   Scoring an unrecorded gate as ungated inflates the total, the one direction the
#   `>=` lower-bound marker cannot express.
# - `target`: an untranscribed target read as team-wide would overstate the same way.
# - `override`: a transcriber who stopped at the first sentence of the skill text leaves
#   it blank; reading that as "no override" would silently understate.
_REQUIRED_KNOWN_FIELDS: tuple[str, ...] = (
    "condition",
    "target",
    "override",
    "duration",
    "interval",
    "probability_tier",
    "magnitude",
)


def _first_missing_field(resolved: ResolvedSkill) -> str | None:
    """The first `_REQUIRED_KNOWN_FIELDS` entry that is `UNKNOWN` on this skill, or
    `None`. Fields not applicable to the slot kind
    (`NOT_APPLICABLE_FIELDS_BY_SLOT_KIND`) are skipped: their absence is not a data gap."""
    slot = resolved.skill
    not_applicable = NOT_APPLICABLE_FIELDS_BY_SLOT_KIND.get(slot.kind, frozenset())
    for field in _REQUIRED_KNOWN_FIELDS:
        if field in not_applicable:
            continue
        if not is_known(getattr(slot, field)):
            return field
    return None


def _has_all_known_fields(resolved: ResolvedSkill) -> bool:
    """True unless a field the EV formula needs for this slot's kind is
    `UNKNOWN`."""
    return _first_missing_field(resolved) is None


def _effective_magnitude(resolved: ResolvedSkill) -> object:
    """The magnitude an EV formula reads: `max(override, base)` when the override's gate
    fired, else the base.

    The override is not additive (the game text names a value, not a bonus) and does not
    replace the base: a skill with a `SlotOverride` registers two effect objects live at
    the same notes, and the game's `CalcScoreUpRatio` combines them with `MathF.Max`. So
    an override worth less than its base still pays the base. The override's gate never
    suppresses the skill; it only decides whether the second magnitude is in play."""
    skill = resolved.skill
    if skill.override is not None and resolved.override_gate is not None and resolved.override_gate.fired:
        return max(skill.override.magnitude, skill.magnitude)  # type: ignore[call-overload, attr-defined]
    return skill.magnitude


def active_magnitude_by_card(resolved_skills: Iterable[ResolvedSkill]) -> dict[str, float] | None:
    """Each card's ACTIVE magnitude as the game ranks the recipients of a count-limited
    Score Support: the principal ACTIVE's magnitude at the card's own skill level, with
    a fired override competing with it (`_effective_magnitude`). A support raises no
    stat, and the game pays the qualifying members whose ACTIVE is the strongest (ties
    to the earlier slot), whatever their parameter. A card with no ACTIVE ranks at 0.
    `None` when a magnitude is not known: the caller then ranks by total parameter
    instead of guessing an order."""
    out: dict[str, float] = {}
    for resolved in resolved_skills:
        if resolved.slot_kind != "ACTIVE" or resolved.source != "normal" or resolved.card_id in out:
            continue
        magnitude = _effective_magnitude(resolved)
        if isinstance(magnitude, bool) or not isinstance(magnitude, (int, float)):
            return None
        out[resolved.card_id] = float(magnitude)
    return out


@cache
def _finite_window_uptime(duration: float, interval: float) -> float:
    """The fraction of a reference run this ACTIVE skill's effect is up, GIVEN it rolls
    successfully, counted on the real cooldown lattice rather than as a steady-state ratio.

    The game's cooldown clock starts at measure 0 and the first activation check sits one
    whole cooldown in, so checks are at `k x interval` for `k = 1 .. floor(T / interval)`.
    A run opens with a dead zone of one cooldown and closes with a window the end truncates:

        uptime = |UNION over k of [k x interval, k x interval + duration)  INTERSECT  [0, T]| / T

    A union, not a sum, so self-overlap (`duration >= interval`) is structural rather than a
    clamp: the windows tile from the first check to the end and give `(T - interval) / T`.
    `min(1, duration / interval)` is the `T -> infinity` limit and assumes the dead zone
    away. Weighted per note over every shipped chart, a single skill is actually up 83-95 %
    of `duration / interval`, losing more as `interval` grows (equal duty, different
    cooldowns, is not equal uptime). This expression lands at 76-93 % of the ratio, slightly
    under that truth on one skill; over a real ACTIVE pool the remaining biases nearly cancel
    (the pool's `E[max]` is within about +0.002 of the exact integral; see `_score_up_pool`).
    The deck screen supports the window too: fitted jointly with the server's board values,
    its rms minimum falls on the independently solved `REFERENCE_RUN_SECONDS = 128.8`.

    Returns 0.0 when `floor(T / interval)` is zero (the skill never reaches its first check);
    real intervals run 15-35 s, so that is a guard. `min(1, ...)` is a guard too: a subset of
    `[0, T]` cannot exceed `T`.

    This is the run-average of exactly the windows `song._active_windows` places on a real
    time axis (pinned by `tests/test_finite_window_uptime.py`). Cached: the argument pair
    belongs to the card and the holomem's board, and a team walk asks the same few dozen
    pairs millions of times."""
    live = 0.0
    covered_to = 0.0
    check = 1
    while check * interval <= REFERENCE_RUN_SECONDS:
        start = max(check * interval, covered_to)
        end = min(check * interval + duration, REFERENCE_RUN_SECONDS)
        if end > start:
            live += end - start
            covered_to = end
        check += 1
    return min(1.0, live / REFERENCE_RUN_SECONDS)


def _score_active(resolved: ResolvedSkill, cfg: ScoringConfig, team_rate_up: float) -> float:
    """`_active_occupancy * _as_rate(magnitude)`: an ACTIVE skill's average per-run rate."""
    slot = resolved.skill
    if slot.kind != "ACTIVE":
        raise ScoringError(
            f"_score_active requires an ACTIVE slot, got {slot.kind!r}",
            card_id=resolved.card_id,
            slot_kind=slot.kind,
        )
    # The Holomem Board's `Act.CD` nodes shorten the cooldown by DIVIDING it
    # (35 s / 1.12 = 31.25 s). Only the board supplies cooldown (no `cool_time` rows
    # exist elsewhere), so it is keyed by holomem; an absent entry means zero.
    #
    # Probability is different: it has two sources with two keys, the board per HOLOMEM
    # and the SPECIAL rider per CARD (`LiveSpecialSkillLevel` rows carry
    # `live_active_skill_activation_probability_up_permil_up` at 350-550 permil, and
    # skins of one holomem carry different SPECIALs). Keying it by holomem drops the rider.
    #
    # Real per-member values come from `Live/GetDeck` field 3 (`LiveDeckInGameEffect`),
    # never from the branch ceilings, which are full-unlock maxima and run ~4x high.
    return _active_occupancy(resolved, cfg, team_rate_up) * _as_rate(float(_effective_magnitude(resolved)))  # type: ignore[arg-type]


def _active_occupancy(resolved: ResolvedSkill, cfg: ScoringConfig, team_rate_up: float) -> float:
    """The fraction of the run this ACTIVE skill's effect is live:
    `_finite_window_uptime(duration, effective interval) * P(activation)`, no magnitude.

    Split from `_score_active` because the ACTIVE pool is `E[max]` over magnitudes (the
    game's `ExpectedMaximumActiveSkill`: the highest live effect wins), which cannot be taken
    once magnitude and occupancy are multiplied. A cooldown reduction moves the activation
    COUNT, an integer, which is why the board term and the finite window are identifiable
    only jointly."""
    slot = resolved.skill
    # The board supplies both uptime axes for this member's character and ADDS to the
    # hand-supplied `active_cooldown_reduction`. `board.uptime_for` is a per-holomem
    # constant, so this is a dict lookup that needs neither the team nor the leader.
    #
    # Board rate and skill rate are the same effect family
    # (`live_active_skill_activation_probability_up`), so they are pooled into one rate
    # rather than applied as separate factors. How the game combines the two sources is
    # not observed directly; the alternative, (1+a)(1+b) against (1+a+b), differs by
    # under half a point at real magnitudes.
    board_rate, board_cooldown = (
        cfg.board.uptime_for(resolved.member) if cfg.board is not None else (0.0, 0.0)
    )
    reduction = cfg.active_cooldown_reduction.get(resolved.member, 0.0) + board_cooldown
    interval = float(slot.interval) / (1.0 + reduction)  # type: ignore[arg-type]
    # "finite" (in-run, with the start-of-song dead zone) vs "steady" (the
    # deck-screen panel's song-independent reference). See ScoringConfig
    # .active_uptime_model for what separates them.
    if cfg.active_uptime_model == "steady":
        uptime = min(1.0, float(slot.duration) / interval) if interval > 0.0 else 0.0  # type: ignore[arg-type]
    else:
        uptime = _finite_window_uptime(float(slot.duration), interval)  # type: ignore[arg-type]
    base_prob = cfg.probability_tiers[slot.probability_tier]
    prob = cfg.activation_rate_model.apply(base_prob, team_rate_up + board_rate)
    return uptime * prob


def _score_special(resolved: ResolvedSkill, cfg: ScoringConfig, team_rate_up: float) -> float:
    """`_special_occupancy * _magnitude_rate`: a SPECIAL's own score contribution.

    SPECIAL fires manually from a gauge, never on a roll, so it reads neither `interval` nor
    `probability_tier`; `team_rate_up` is unread and kept for the shared signature."""
    slot = resolved.skill
    if slot.kind != "SPECIAL":
        raise ScoringError(
            f"_score_special requires a SPECIAL slot, got {slot.kind!r}",
            card_id=resolved.card_id,
            slot_kind=slot.kind,
        )
    # A SPECIAL's `activation_rate_up` rider ("For 10s, ... When LIFE is 1000 or higher,
    # Skill Activation Rate UP 55%.") lifts every ACTIVE proc chance team-wide; it is
    # folded into `team_rate_up` by `_fold_rider_activation_rate_up` (same uptime term,
    # rider magnitude) and not added here, which would double-count it. 35-55 % for ~10 s
    # once per run is roughly +4 points on every proc chance.
    return _special_occupancy(resolved, cfg, team_rate_up) * _magnitude_rate(resolved)


def _special_occupancy(resolved: ResolvedSkill, cfg: ScoringConfig, team_rate_up: float) -> float:
    """`expected_fires_per_run * (duration / REFERENCE_RUN_SECONDS)`: the fraction of the run a
    SPECIAL's effect is live, no magnitude.

    Split out because a SPECIAL Score UP competes in the SPECIAL bucket's maximum (inside `B`,
    see `_score_up_implements_boost`), which cannot be taken over products. `B`'s time-average
    is multiplied by the ACTIVE maximum's as if the two were independent; see `_score_up_pool`.

    `REFERENCE_RUN_SECONDS` is 128.8, solved from ten captured decks (see `scoring_config`);
    `live_reward_up_music_length_bonus_min_playing_seconds = 90` scales rewards, not score."""
    del team_rate_up  # SPECIAL is gauge-fired, never on an activation roll
    return cfg.expected_fires_per_run * (float(resolved.skill.duration) / REFERENCE_RUN_SECONDS)  # type: ignore[arg-type]


def _always_on_occupancy(resolved: ResolvedSkill, cfg: ScoringConfig, team_rate_up: float) -> float:
    """Occupancy `1.0`: a PASSIVE or COSTUME effect is live for the whole run. Takes the shared
    `OCCUPANCY_FUNCTIONS` signature and reads none of it."""
    del resolved, cfg, team_rate_up
    return 1.0


def _magnitude_rate(resolved: ResolvedSkill) -> float:
    """The skill's magnitude as a fraction, override applied: the game's `ScoreUpByRatio` /
    `ScoreUpBoostByRatio` (`FloatFromPermil(this.Value)`), with no occupancy term, which is
    why it can be maximised over."""
    return _as_rate(float(_effective_magnitude(resolved)))  # type: ignore[arg-type]


def _passive_rate(resolved: ResolvedSkill) -> float:
    """PASSIVE/COSTUME arithmetic: the full magnitude as a rate, always. The skill is always on
    and gated only by its condition, which `_resolve_skill` has already evaluated."""
    # Occupancy 1.0 is elided: `1.0 * x` is exactly `x` in IEEE-754.
    return _magnitude_rate(resolved)


def _score_passive(resolved: ResolvedSkill, cfg: ScoringConfig, team_rate_up: float) -> float:
    """PASSIVE: `_as_rate(magnitude)`. `_resolve_skill` never dispatches an unfired gated skill."""
    slot = resolved.skill
    if slot.kind != "PASSIVE":
        raise ScoringError(
            f"_score_passive requires a PASSIVE slot, got {slot.kind!r}",
            card_id=resolved.card_id,
            slot_kind=slot.kind,
        )
    return _passive_rate(resolved)


def _score_costume(resolved: ResolvedSkill, cfg: ScoringConfig, team_rate_up: float) -> float:
    """COSTUME: the shared `_passive_rate`, with its own single-kind entry guard."""
    slot = resolved.skill
    if slot.kind != COSTUME_SLOT_KIND:
        raise ScoringError(
            f"_score_costume requires a {COSTUME_SLOT_KIND} slot, got {slot.kind!r}",
            card_id=resolved.card_id,
            slot_kind=slot.kind,
        )
    return _passive_rate(resolved)


EV_FUNCTIONS: dict[str, Callable[[ResolvedSkill, ScoringConfig, float], float]] = {
    "SPECIAL": _score_special,
    "ACTIVE": _score_active,
    "PASSIVE": _score_passive,
    COSTUME_SLOT_KIND: _score_costume,
}

# The occupancy half of every EV function: `EV_FUNCTIONS[k]` is exactly
# `OCCUPANCY_FUNCTIONS[k] * _magnitude_rate`. The pool builders need the halves apart
# because the game maximises over magnitudes. No entry guards here: every caller has
# already established the kind.
OCCUPANCY_FUNCTIONS: dict[str, Callable[[ResolvedSkill, ScoringConfig, float], float]] = {
    "SPECIAL": _special_occupancy,
    "ACTIVE": _active_occupancy,
    "PASSIVE": _always_on_occupancy,
    COSTUME_SLOT_KIND: _always_on_occupancy,
}

assert set(OCCUPANCY_FUNCTIONS) == set(EV_FUNCTIONS), (
    "OCCUPANCY_FUNCTIONS must cover exactly the EV_FUNCTIONS slot kinds — a "
    "kind that can be scored but whose occupancy cannot be computed would "
    "silently drop out of the bucket maxima"
)

# A slot kind added to schema.py without an EV function fails here, at import.
assert set(EV_FUNCTIONS) == set(SKILL_SLOT_KINDS) | {COSTUME_SLOT_KIND}, (
    "EV_FUNCTIONS must cover every SKILL_SLOT_KINDS member plus "
    "COSTUME_SLOT_KIND — an unmapped slot kind must fail at import, never "
    "at score time"
)


def _fold_activation_rate_up(resolved_skills: tuple[ResolvedSkill, ...], cfg: ScoringConfig) -> float:
    """Pass 1: sum every fired, fully known `activation_rate_up` skill into one team-wide rate
    modifier (order-independent, never through `stacking_model.combine`, which is for the
    score pool only).

    The fold applies the ACTIVE formula. `schema.validate_skill_slot` rejects this family on
    any other slot kind at load, so the guard below raises for a record that bypassed
    validation rather than dispatching it through another kind's formula, which would
    fabricate a plausible number. The guard runs before the gate skip so an unfired gate
    cannot mask an invalid record.

    `resolved.family` is the slot's PRINCIPAL family, so a SPECIAL's `activation_rate_up`
    rider never enters here; it has its own fold, `_fold_rider_activation_rate_up`.
    `cfg.family_weights["activation_rate_up"]` is validated to be 0.0 and is not read."""
    total = 0.0
    for resolved in resolved_skills:
        if resolved.family != "activation_rate_up":
            continue
        if resolved.skill.kind != FAMILY_REQUIRES_SLOT_KIND["activation_rate_up"]:
            raise ScoringError(
                "_fold_activation_rate_up applies the ACTIVE formula, and only "
                "an ACTIVE slot may carry activation_rate_up; validate_skill_slot "
                "rejects this record at load time, so reaching this line means "
                "the input bypassed load-time validation",
                card_id=resolved.card_id,
                slot_kind=resolved.skill.kind,
            )
        if resolved.gate is not None and not resolved.gate.fired:
            continue
        if not _has_all_known_fields(resolved):
            continue
        total += _score_active(resolved, cfg, 0.0)
    return total


def _fold_rider_activation_rate_up(resolved_skills: tuple[ResolvedSkill, ...], cfg: ScoringConfig) -> float:
    """Pass 1b: add every fired `activation_rate_up` RIDER on a SPECIAL to the same team-wide
    modifier `_fold_activation_rate_up` builds.

    The rider lives inside its host SPECIAL's window and fires once per run, gated by its own
    condition (`resolved.rider_gate`, e.g. LIFE>=1000), so it contributes

        expected_fires_per_run * (duration / REFERENCE_RUN_SECONDS) * _as_rate(rider.magnitude)

    (`_special_occupancy` with the rider's magnitude). Riders on other slot kinds would need
    other arithmetic and are skipped (none exist in the data), as are riders whose magnitude,
    condition or host duration is `UNKNOWN`, or whose gate did not fire."""
    total = 0.0
    for resolved in resolved_skills:
        rider = resolved.skill.rider
        if not isinstance(rider, SlotRider):
            continue
        if rider.family != "activation_rate_up":
            continue
        if resolved.skill.kind != "SPECIAL":
            continue
        if not is_known(rider.magnitude) or not is_known(rider.condition):
            continue
        if not is_known(resolved.skill.duration):
            continue
        if resolved.rider_gate is not None and not resolved.rider_gate.fired:
            continue
        total += _special_occupancy(resolved, cfg, 0.0) * _as_rate(float(rider.magnitude))
    return total


def _resolve_skill(
    resolved: ResolvedSkill, cfg: ScoringConfig, team_rate_up: float
) -> tuple[float, str, str | None]:
    """The shared per-skill core every scoring entry point calls.

    Returns `(rate, state, missing_field)`, so a gate that did not fire (`gate_not_met`), an
    `UNKNOWN` field (`data_missing`) and a genuine zero (`applied`) stay distinguishable.
    Dispatches on `resolved.skill.kind`, never `resolved.slot_kind` (the lowercase `costume`
    is not an `EV_FUNCTIONS` key)."""
    slot = resolved.skill
    pool = _pool_for(resolved)
    # An activation-rate skill is already folded into `team_rate_up`; it never receives it.
    effective_team_rate_up = 0.0 if pool == "none" else team_rate_up

    if resolved.gate is not None and not resolved.gate.fired:
        return 0.0, "gate_not_met", None

    missing_field = _first_missing_field(resolved)
    if missing_field is not None:
        return 0.0, "data_missing", missing_field

    rate = EV_FUNCTIONS[slot.kind](resolved, cfg, effective_team_rate_up)
    return rate, "applied", None


# The two buckets `CalcScoreUpRatio` partitions its effect list into. The game's
# `ActiveSkillEffectDataCollection::CalcScoreUpRatio` (rva `0x5efdb90`) keeps two
# accumulators, folds each effect into one with `Math::Max` and returns their sum; the
# discriminator is the byte `LiveSkillEffectData.isSpecialSkill` (unboxed `+0x18`).
# The SPECIAL bucket holds only `ScoreUpBoostSkillEffect`s, so it feeds `B`, never the
# ratio: a SPECIAL multiplies the ACTIVE maximum and is worth nothing without one (see
# `_score_up_implements_boost`).
SCORE_BUCKET_ACTIVE = "ACTIVE"
SCORE_BUCKET_SPECIAL = "SPECIAL"


def _score_bucket(resolved: ResolvedSkill) -> str:
    """Which `CalcScoreUpRatio` accumulator this effect folds into: SPECIAL for a SPECIAL slot,
    ACTIVE for everything else.

    A SPECIAL's effects carry `isSpecialSkill == true`. Nothing in the game would make a
    PASSIVE or outfit skill "special" (the field's two writers take the flag as a
    parameter), so the ACTIVE bucket takes them. This is the whole partition."""
    return SCORE_BUCKET_SPECIAL if resolved.skill.kind == "SPECIAL" else SCORE_BUCKET_ACTIVE


def _score_up_implements_boost(resolved: ResolvedSkill) -> bool:
    """The SPECIAL collapse: `True` when a Score UP is a `ScoreUpBoostSkillEffect` (implements
    `ScoreUpBoostByRatio`, vtable slot `+0x1d8`, contributes to `B`), `False` for a
    `ScoreUpSkillEffect` (`ScoreUpByRatio`, slot `+0x1c8`, contributes to the ratio).

    Every SPECIAL Score UP is a boost, so `max_SPECIAL v(e)` in `_score_up_pool`'s formula is
    zero and the expression collapses to

        ratio = (1 + SkillEffectUpPermilUp/1000 + B) x max_ACTIVE ScoreUpByRatio

    so a SPECIAL Score UP with no ACTIVE Score UP in the team contributes nothing
    (`tests/test_special_collapse.py`). The evidence, from the game binary and data:

    1. A live in-process dump resolves the `LiveActiveSkillEffectData::Create` effect types
       to runtime classes: type 5 is `ScoreUpBoostSkillEffect`, type 1 `ScoreUpSkillEffect`.
    2. Vtables: `ScoreUpBoostSkillEffect` leaves slot `+0x1c8` as the base stub at rva
       `0x144570` (`xorps xmm0,xmm0 ; ret`, a hard `0.0f`) and overrides `+0x1d8` (rva
       `0x5f06840`); `ScoreUpSkillEffect` is the mirror image.
    3. Master data: every `LiveSpecialSkillLevel` row's field 3 is a
       `live_special_skill_effect-score_up_effect_up_permil_up-*` effect of `type == 5`;
       the `LiveActiveSkillLevel` score rows are `score_up_permil_up`, `type == 1`. The two
       are disjoint.

    Solving the reference duration from ten captured deck screens, the multiplier reading
    (`Special line = Active line x SUM(mag x dur) / T`) gives `T` constant to about 6 %; the
    addend reading spreads about 90 %. Keyed on `skill.kind`, the only axis the schema
    carries; exact while no SPECIAL carries a plain `score_up_permil_up`."""
    return resolved.skill.kind == SCORE_BUCKET_SPECIAL


def _windows_are_mutually_exclusive(resolved: ResolvedSkill) -> bool:
    """`ActiveTerm`'s `exclusive` flag: `True` when this skill's live window cannot overlap any
    other exclusive term in its bucket, so the bucket's `E[max]` is the plain sum of
    `magnitude x occupancy` instead of the independence walk.

    A SPECIAL does not roll: its fire time is in the chart (channel `#mmm0B`, one cue per deck
    slot, slot `i` fires cue `i`), once per run. Over every shipped chart with five ascending
    cues the shortest gap is 9.88 s against SPECIAL durations of 7.0-14.0 s; no gap is below
    7.0 s and about 0.6 % are below 14.0 s, and two consecutive gaps sum to at least 19.76 s.
    The independence walk would understate `B` by roughly 10-17 % on captured decks (rms
    against the deck screen's Special-Skill line: 3.9 pp independent, 1.5 pp exclusive).

    This holds for the RUN-AVERAGE occupancy only. `song.py` places the real cue windows on a
    time axis and passes `exclusive=False`, so the rare real overlaps are maximised there.
    ACTIVEs roll on a cooldown lattice and collide constantly, so the ACTIVE bucket is never
    exclusive."""
    return resolved.skill.kind == SCORE_BUCKET_SPECIAL


def _is_score_up_effect(resolved: ResolvedSkill) -> bool:
    """True for the skills `CalcScoreUpRatio` maximises: an ACTIVE or SPECIAL slot whose effect
    raises SCORE (`_score_up_implements_boost` says which maximum it joins).

    The kind restriction is the conservative reading: the game's partition has exactly two
    buckets and whether a PASSIVE or outfit Score UP is in that collection at all is not
    known, so they stay summed, which changes nothing. Score Support is the other way round:
    a COSTUME support does reach the multiplier, because captured deck screens show it.

    Narrower than `pool == "score"` on purpose: `life` and `judgment` are different effect
    classes (`LifeRecoverySkillEffect`, `JudgementEnhanceSkillEffect`) whose `ScoreUpByRatio`
    is the `0.0f` base stub, so they never join a maximum. Their default `family_weights` is
    0.0; maximising them against score effects if one were set would be silently wrong."""
    return (
        resolved.skill.kind in (SCORE_BUCKET_ACTIVE, SCORE_BUCKET_SPECIAL)
        and resolved.skill.effect_type == "score"
    )


def _active_interval(resolved: ResolvedSkill) -> int:
    """The ACTIVE cooldown as `ActiveTerm`'s lattice element: `int(slot.interval)`, or `0` when
    the slot has no numeric interval (`UNKNOWN` on a SPECIAL, `None` on a COSTUME rider slot).
    The shipped `max` combiner does not read it."""
    raw = resolved.skill.interval
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return 0
    return int(raw)


def _bucket_entry(
    resolved: ResolvedSkill,
    cfg: ScoringConfig,
    team_rate_up: float,
    weight: float,
    state: str,
) -> tuple[str, float, float, int, bool]:
    """One bucket term: `(bucket, weighted_magnitude_rate, occupancy, interval, exclusive)`.

    Shared by every driver, for both ratio terms and Score Support boost terms: the game
    builds them from the same list with the same partition (`CalcScoreUpBoostByRatioBase` is
    inlined into `CalcScoreUpRatio`). The family weight scales the MAGNITUDE, so the term
    sorts by the magnitude actually applied under a maximum.

    A skill that is not `applied` gets occupancy 0.0 as well as magnitude 0.0: a zero
    magnitude alone would still consume the `E[max]` walk's remaining probability and let an
    unfired gate suppress a skill that did fire. `exclusive` is kept on a zero term so a
    suppressed SPECIAL still sorts like a SPECIAL."""
    bucket = _score_bucket(resolved)
    exclusive = _windows_are_mutually_exclusive(resolved)
    interval = _active_interval(resolved)
    if state != "applied":
        return (bucket, 0.0, 0.0, interval, exclusive)
    occupancy = OCCUPANCY_FUNCTIONS[resolved.skill.kind](resolved, cfg, team_rate_up)
    return (bucket, weight * _magnitude_rate(resolved), occupancy, interval, exclusive)


def _bucket_term(
    resolved: ResolvedSkill,
    cfg: ScoringConfig,
    team_rate_up: float,
    weight: float,
    state: str,
    pool: str,
    target_ids: frozenset,
) -> tuple[str, float, float, int, bool, float, float] | None:
    """`score_card_set`'s cache entry: the bucket term for a Score UP or a Score Support, `None`
    for anything else. `_score_team` routes the same three conditions inline in its own
    `if pool ==` chain (it does not need `target_ids` there); the parity tests pin that the
    two agree."""
    if pool == "score":
        if not _is_score_up_effect(resolved):
            return None
        return _lattice_entry(resolved, cfg, team_rate_up, weight, state)
    if pool == "support":
        # A support that reaches nobody never enters the effect collection.
        if not target_ids:
            return (
                _score_bucket(resolved),
                0.0,
                0.0,
                _active_interval(resolved),
                _windows_are_mutually_exclusive(resolved),
                # no lattice
                0.0,
                0.0,
            )
        return _lattice_entry(resolved, cfg, team_rate_up, weight, state)
    return None


def _score_up_pool(
    ratio_entries: list[tuple],
    boost_entries: list[tuple],
    cfg: ScoringConfig,
    skill_effect_up: float = 0.0,
    ratio_owners: list[str | None] | None = None,
    boost_targets: list[frozenset[str] | None] | None = None,
) -> float:
    """`SkillScoreUpRatio`: the team's whole skill-effect Score UP as one rate.

    The game's `CalcScoreUpRatio` computes

        B     = max_ACTIVE ScoreUpBoostByRatio + max_SPECIAL ScoreUpBoostByRatio
        v(e)  = (1 + SkillEffectUpPermilUp/1000 + B) x ScoreUpByRatio(e)
        ratio = max_ACTIVE v(e) + max_SPECIAL v(e)

    with both maxima taken by the same rule (`CalcScoreUpBoostByRatioBase` is inlined into
    `CalcScoreUpRatio`), hence `cfg.active_stacking_model.combine` twice. Score Support
    multiplies (`mulss`), and several supports combine as max-of-two-maxima, never through
    `STACKING_MODELS`: two 30 % supports are worth one 30 %.

    Per-member targeting: a member PASSIVE `score_support` with `{kind:type, count:2}` boosts
    only its recipients' ACTIVEs (the deck panel's per-card `active_effect_up` shows it), and
    targeted supports reaching one card add up:

        v(e_i) = (1 + SkillEffectUpPermilUp/1000 + B_shared + T_i) x ScoreUpByRatio(e_i)

    `B_shared` combines the team-wide supports (member PASSIVE `all`, leader COSTUME,
    SPECIAL); `T_i = sum(magnitude x occupancy)` of the targeted supports whose resolved set
    contains the owner of `e_i`. `ratio_owners[k]` is the card owning `ratio_entries[k]`;
    `boost_targets[k]` is `None` for a team-wide support, else its resolved set. Both default
    to `None` (every support team-wide). A support that reaches nobody never enters.

    The collapse happens in the routing, not here: every SPECIAL Score UP is a
    `ScoreUpBoostSkillEffect` (`_score_up_implements_boost`), so the drivers hand SPECIAL
    Score UPs to `boost_entries` and this computes `ratio = (1 + B) x max_ACTIVE`. A SPECIAL
    Score UP with no ACTIVE Score UP under it is worth nothing.

    Overlap. The game's product is instantaneous (`B` and the ACTIVE maximum `A` over the
    skills live at each tick); this multiplies time averages, so it misses `Cov(B, A)`:
    1. Within `B`: SPECIAL cues practically never collide, so their terms are `exclusive`
       (`_windows_are_mutually_exclusive`).
    2. Between `B` and `A`: note-weighted over every chart,
       `rho = E[S A] / (E[S] E[A])` is 1.0747 over the whole song but 0.9976 +/- 0.0087 from
       t = 40 s on; the excess is the ACTIVE lattice's start-of-song dead zone (the game's
       lattice starts at measure 0, measured in play). Not applied: the song scorers walk the
       real windows instead.
    3. The `E[A]` marginal: a steady-state duty cycle overstates the exact `E[A]` by 7-16 %
       per team. Replacing only the marginal (finite window) takes `E[A]` rms 0.0474 ->
       0.0194 (bias +0.0427 -> -0.0028); replacing only the dependence (exact lattice
       co-occupancy) 0.0474 -> 0.0449; both over-corrects (0.0249, bias -0.0168). Lattice
       synchronisation averages out across charts of 88-165 s, so the walk stays and the fix
       is `_finite_window_uptime`, upstream of it.

    The largest remaining error is `B`'s denominator: `_special_occupancy` divides by
    `REFERENCE_RUN_SECONDS = 128.8` (solved from deck screens), while a chart's note-weighted
    denominator averages about 116 s (cues land in note-dense bars), so `B` understates the
    in-song `E[B]` by 10-17 %. The deck screen and a song do not share a time base.

    `skill_effect_up` is the board's `SkillEffectUpPermilUp` (`BoardContribution
    .skill_effect_up`). It enters only inside the parenthesis: a team with no ACTIVE Score UP
    gains nothing from it.

    Entries are `(bucket, weighted_magnitude_rate, occupancy, interval, exclusive, ...)` in
    `resolve_team_gates` order; other score-pool rates are combined by the caller. Every
    driver calls this one function, so parity is bit-identical by construction."""
    combine_ratio = cfg.active_stacking_model.combine
    # Only the magnitude (index 1) is scaled; `occupancy`, `interval`, `exclusive`
    # and the trailing `(duration, probability)` pair pass via `*rest`.
    if boost_targets is None or all(t is None for t in boost_targets):
        multiplier = 1.0 + skill_effect_up + cfg.active_stacking_model.combine(boost_entries)
        return combine_ratio(
            [(bucket, magnitude_rate * multiplier, *rest) for bucket, magnitude_rate, *rest in ratio_entries]
        )
    assert ratio_owners is not None and len(ratio_owners) == len(ratio_entries)
    assert len(boost_targets) == len(boost_entries)
    shared = (
        1.0
        + skill_effect_up
        + cfg.active_stacking_model.combine([e for e, t in zip(boost_entries, boost_targets) if t is None])
    )
    targeted = [(e, t) for e, t in zip(boost_entries, boost_targets) if t is not None]
    scaled: list[tuple] = []
    for (bucket, magnitude_rate, *rest), owner in zip(ratio_entries, ratio_owners):
        multiplier = shared + sum(e[1] * e[2] for e, t in targeted if owner in t)
        scaled.append((bucket, magnitude_rate * multiplier, *rest))
    return combine_ratio(scaled)


def _support_targets(resolved: ResolvedSkill, target_ids: frozenset[str]) -> frozenset[str] | None:
    """`_score_up_pool`'s `boost_targets` element for one support: `None` when it is team-wide
    (no target, or `{kind: all}`), else the resolved recipient set."""
    target = resolved.skill.target
    if not isinstance(target, EffectTarget) or target.kind == TEAM_WIDE_TARGET_KIND:
        return None
    return target_ids


# ---------------------------------------------------------------------------
# Deck slot order
# ---------------------------------------------------------------------------
#
# The game's `LiveSkillUtility::IsSatisfyEffectTarget{Attribute,Character,
# CharacterGrouping}Condition` (rva `0x5f03d70` / `0x5f03f50` / `0x5f04140`) walk the
# deck's cards in slot order, count the members matching the effect's target up to and
# including the candidate, and return `count <= targetCount`; `targetCount == 0` returns
# true before the loop ("no limit"). Read literally, a count-limited effect reaches the
# first `count` matchers in slot order. Most cards carry a count-limited PASSIVE target
# (`type`/2, `type`/3, `generation`/2), so the rule matters.
#
# Captured deck panels show the game paying the top-`count` matchers by the stat the
# effect raises instead (`targets.resolve_target_card_ids`), which does not depend on slot
# order. That is the `"free"` rule, the only entry of `scoring_config.SLOT_ORDER_RULES`,
# so the stat pool is order-invariant. The other terms are too:
#
# - Score pool: `_bucket_entry` reads no target.
# - Score Support: a count-targeted support boosts its recipients' ACTIVEs, chosen by
#   ACTIVE magnitude (`active_magnitude_by_card`), not by slot.
# - `team_rate_up` reads no target; `_board_score_rate` sums over the member set.
# - The SPECIAL slot cue (slot `i` fires the chart's `i`-th `#mmm0B` cue) is a per-note
#   time integral that needs a chart, so it lives in `song.py`; this module's SPECIAL
#   term is order-invariant by construction, and its rankings omit a cue term worth
#   roughly 1.4-3.1 % of score at the median.
#
# `_windows_are_mutually_exclusive` rests on the cue geometry of every shipped chart but
# is not a per-song term: it reads no chart, holds on the whole corpus (a chart where it
# failed would be scored by `song.py`, which passes `exclusive=False`), and changes how a
# team's own SPECIALs combine, which is a team property.
#
# The slot-order reading is kept as `_ordered_target_card_ids` (tested), and
# `_order_varying_indices` gives the O(5) test of whether order can matter at all: only
# an effect whose matcher count exceeds its `count`. No scoring path calls them under the
# `"free"` rule: `_score_team` re-resolves recipients with the first only when a rule
# returns a member order, and the song scorers' 120-order walk varies only the SPECIAL
# cues. A per-(card, slot) weight table cannot express the slot-order rule, since whether
# a card in slot 3 receives a `count 1` effect depends on where the other matcher sits.

# Marks a `stat_entries` element whose recipients never move with slot order, so its
# resolved `target_ids` is reused as is. Every non-`applied` entry carries it: its
# `target` may be the `UNKNOWN` sentinel that made it `data_missing`.
_FIXED_TARGET = object()


# `(weighted_rate, stat_names_this_effect_raises, resolved_target_card_ids,
#   order_target, self_card_id, slot_kind)`. `order_target` and `self_card_id` re-resolve
# the recipients under a slot order (`order_target` is the skill's `target` for an
# `applied` entry, `_FIXED_TARGET` otherwise). `slot_kind` is read only by the Overall
# Power display (`_overall_power_stat_subtotals`), which rounds each source line on its own.
StatEntry = tuple[float, tuple[str, ...], frozenset, object, str, str]
# `score_card_set`'s entries: the same without `slot_kind`, which only `_score_team` reads.
CardSetStatEntry = tuple[float, tuple[str, ...], frozenset, object, str]

# `(members, stat_entries, cfg, orderings_cache) -> (stat_sum, member_order)`.
SlotOrderResolver = Callable[
    [tuple, list, ScoringConfig, "dict | None"], "tuple[float, tuple[str, ...] | None]"
]


def _target_matches(target: EffectTarget, card: Card) -> bool:
    """The target predicate the game counts matches against, for the `type` and `generation`
    kinds (`self` and team-wide are never count-limited)."""
    if target.kind == "type":
        return card.type == target.value
    return target.value in card.generations


def _ordered_target_card_ids(
    target: object,
    self_card_id: str,
    ordered_members: tuple[Card, ...],
    under_supply: str,
) -> frozenset[str]:
    """The slot-order reading: the first `target.count` members of `ordered_members` (the deck
    in slot order) that match `target`.

    Same contracts as `targets.resolve_target_card_ids` otherwise: `None` -> every member,
    `self` -> the source card, `count == 0` -> no limit (the game's own early return, why
    `-all`/`-self` rows and leader-skill refs are order-invariant), and under-supply follows
    `cfg.target_under_supply`. The game's code has no under-supply check (every matcher passes
    when there are fewer than `targetCount`), which is the default `"apply_to_available"`;
    `"do_not_apply"` stays a configurable option."""
    if target is None:
        return frozenset(card.id for card in ordered_members)
    if not isinstance(target, EffectTarget):  # pragma: no cover - guarded upstream
        raise ValueError(
            f"_ordered_target_card_ids requires a resolved target (None or "
            f"EffectTarget), never UNKNOWN — card {self_card_id!r}"
        )
    if target.kind == "self":
        return frozenset({self_card_id})
    matched = [card.id for card in ordered_members if _target_matches(target, card)]
    count = target.count
    if not count:
        # `targetCount == 0` is the game's own "no limit" early return.
        return frozenset(matched)
    if len(matched) < count and under_supply == "do_not_apply":
        return frozenset()
    return frozenset(matched[:count])


def _order_varying_indices(stat_entries: list, members: tuple[Card, ...]) -> list[int]:
    """The indices of `stat_entries` whose recipients can move with slot order: count-limited
    set-membership targets whose matcher count strictly exceeds `count`. With matchers
    `<= count`, first-N in order and top-N by stat select the same cards (all, or none under
    `"do_not_apply"`), so the rules agree bit-identically."""
    varying: list[int] = []
    for index, entry in enumerate(stat_entries):
        target = entry[3]
        if not isinstance(target, EffectTarget) or target.kind == "self":
            continue
        count = target.count
        if not count:
            continue
        matched = 0
        for card in members:
            if _target_matches(target, card):
                matched += 1
        if matched > count:
            varying.append(index)
    return varying


def _stat_sum(
    members: tuple[Card, ...],
    stat_entries: list,
    cfg: ScoringConfig,
    recipients: list,
) -> float:
    """The per-`(card, stat_name)` accumulation over `recipients`, a list of recipient sets
    parallel to `stat_entries`. Shared by both drivers so parity holds by construction.

    The traversal is `members` x `STAT_NAMES` order and never permuted: float addition is not
    associative, so a different summation order would change the result even with the same
    recipients.

    It scatters (one pass over the ~6 entries into `(card_id, stat_name)` buckets) instead of
    gathering (15 pairs x every entry): about 1.3x faster in one of the hottest functions,
    with equal floats, because each bucket is appended in `stat_entries` order and the outer
    sum runs in the same order. `combine(())` is still called for empty buckets: skipping it
    would assume every stacking model returns 0.0 there."""
    buckets: dict[tuple[str, str], list[float]] = {}
    for entry, target_ids in zip(stat_entries, recipients):
        if not target_ids:
            continue
        rate = entry[0]
        for stat_name in entry[1]:
            for card_id in target_ids:
                key = (card_id, stat_name)
                bucket = buckets.get(key)
                if bucket is None:
                    buckets[key] = [rate]
                else:
                    bucket.append(rate)

    total = 0.0
    empty: list[float] = []
    combine = cfg.stacking_model.combine
    for card in members:
        card_id = card.id
        stats = card.stats
        for stat_name in STAT_NAMES:
            combined = combine(buckets.get((card_id, stat_name), empty))
            total += getattr(stats, stat_name) * (1.0 + combined)
    return total


def _free_stat_sum(
    members: tuple[Card, ...],
    stat_entries: list,
    cfg: ScoringConfig,
    orderings_cache: dict | None = None,
) -> tuple[float, tuple[str, ...] | None]:
    """The `"free"` rule: every effect keeps the recipients `targets.resolve_target_card_ids`
    picked (ranked by the stat it raises, independent of slot order). Returns no member order,
    since the rule does not choose one (`ScoreBreakdown.member_order` stays `None`)."""
    del orderings_cache
    return _stat_sum(members, stat_entries, cfg, [entry[2] for entry in stat_entries]), None


# The one place a slot-order rule name becomes an implementation; asserted at import to
# match `scoring_config.SLOT_ORDER_RULES`.
SLOT_ORDER_RESOLVERS: dict[str, SlotOrderResolver] = {
    "free": _free_stat_sum,
}

assert set(SLOT_ORDER_RESOLVERS) == set(SLOT_ORDER_RULES), (
    "SLOT_ORDER_RESOLVERS must implement exactly the rules "
    "scoring_config.SLOT_ORDER_RULES declares — a rule that validates but has "
    "no implementation must fail at import, never at score time"
)


def _effective_stat_sum(
    members: tuple[Card, ...],
    stat_entries: list,
    cfg: ScoringConfig,
    orderings_cache: dict | None = None,
) -> tuple[float, tuple[str, ...] | None]:
    """The single dispatch point both drivers call for the stat stage."""
    return SLOT_ORDER_RESOLVERS[cfg.slot_order_rule](members, stat_entries, cfg, orderings_cache)


def _board_score_rate(members: tuple[Card, ...], cfg: ScoringConfig) -> float:
    """The Holomem Board's Score Bonus for this member set, the game's
    `ScoreUpBonusRatioBySkillTree`, from the per-holomem `cfg.board_score_bonus` dict.

    `LiveScoreCalculator::CalcScore` (rva `0x5f01f80`) ends in

        ... x (1 + SkillScoreUpRatio) x (1 + ScoreUpBonusRatioBySkillTree)

    two separate factors (the deck screen displays them as summed lines; the score
    multiplies), so this is never added into `combined_score_rate`. The aggregation, a sum of
    the five members' own values in `members` order (fixed, for bit-identical parity), is
    not stated by the binary; the leader contributes nothing. Returns 0.0 for an empty dict."""
    total = 0.0
    for card in members:
        total += cfg.board_score_bonus.get(card.member, 0.0)
    return total


def _board_bonus(
    leader: Card | None, members: tuple[Card, ...], cfg: ScoringConfig
) -> tuple[float, float, float]:
    """`(power_bonus, score_bonus_rate, skill_effect_up)` for this arrangement: the one place
    every driver reads the Holomem Board.

    - `power_bonus`: the game's `up_by_skill_tree`, a summand of Overall Power
      (`Member Parameter + Outfit Skill + Holomem Board Bonus + Passive Skill + Memory Bonus
      + Member Power-Up`, as `Live/GetDeck` reports it), added to the stat sum before the
      score factors multiply.
    - `score_bonus_rate`: `ScoreUpBonusRatioBySkillTree`, the board's own factor.
    - `skill_effect_up`: goes inside `_score_up_pool`'s `(1 + SkillEffectUpPermilUp/1000 + B)`.

    `score_bonus_rate` is a team-invariant account constant and cannot move a ranking;
    `skill_effect_up` is leader- and member-scoped, so omitting it would be a selection
    error. With `cfg.board` set it takes precedence over `cfg.board_score_bonus`, since a
    per-holomem float cannot express a red node's "whole team, while she leads". Both unset
    gives `(0.0, 0.0, 0.0)`."""
    if cfg.board is None:
        return 0.0, _board_score_rate(members, cfg), 0.0
    # From `BoardModel`'s memo, not a fresh `board.resolve` walk: `resolve` is the
    # reference (`tests/test_board_landing.py` checks this path against it) but rebuilds
    # every effect, too slow per arrangement. `leader.member` is the only thing about the
    # leader `resolve` reads (`board._recipients`), which is what makes the memo sound.
    holomem = None if leader is None else leader.member
    return (
        cfg.board.power_bonus_for(members, holomem),
        cfg.board.score_bonus_rate(),
        cfg.board.skill_effect_up_for(members, holomem),
    )


def _score_team(team: Team, cfg: ScoringConfig, *, with_breakdown: bool = False) -> float | ScoreBreakdown:
    """The two-pass team driver: the only implementation of gate resolution plus EV arithmetic
    behind `score_fast` and `score_with_breakdown`.

    Pass 1 folds every fired `activation_rate_up` skill into `team_rate_up`. Pass 2 scores
    each skill, weights it by `cfg.family_weights[family]` and routes it by pool: stat
    entries (with their resolved recipients), non-Score-UP score rates, Score UP ratio/boost
    bucket terms, or Score Support boost terms. Then

        total = (effective_stat_sum + board_power)
                x (1 + combine([*score_rates, score_up_pool])) x (1 + board_score_rate)

    `effective_stat_sum` is accumulated per `(card, stat_name)` as
    `stat x (1 + combine(rates that name that stat and reach that card))`, so a team-wide
    stat buff raises only its named stat. `score_up_pool` (`_score_up_pool`) is one rate
    because the game takes a maximum over live Score UPs; the board is its own factor.
    `ScoreBreakdown.combined_stat_rate` is a report-only summary, not the multiplier.

    `with_breakdown=False` is the hot path: it skips `activation_rate_up` skills before
    `_resolve_skill` and allocates no `SkillContribution`. `True` keeps every resolved skill
    as a contribution in `resolve_team_gates` order and returns a `ScoreBreakdown`. Both read
    the one pass over `resolved_skills`."""
    resolved_skills = resolve_team_gates(team, cfg.run_state)
    team_rate_up = _fold_activation_rate_up(resolved_skills, cfg) + _fold_rider_activation_rate_up(
        resolved_skills, cfg
    )
    cards = team.members  # gates and stats are the 5 members'
    active_rank = active_magnitude_by_card(resolved_skills)

    stat_rates: list[float] = []
    stat_entries: list[StatEntry] = []
    # Breakdown path only: each stat entry's index into `contributions`, so recipients
    # chosen under a slot order can be written back onto the row that reported them.
    stat_contribution_indices: list[int] = []
    # Score-pool rates that are not a Score UP, combined individually.
    score_rates: list[float] = []
    # Score UP bucket terms (`_bucket_entry` shape): kept apart because the pool is a
    # maximum over magnitudes.
    ratio_entries: list[tuple[str, float, float, int, bool]] = []
    # SPECIAL Score UP and Score Support boost terms, same shape and partition.
    boost_entries: list[tuple[str, float, float, int, bool]] = []
    # Parallel to the two lists above: the owner of each ratio term and the
    # resolved recipient set of each targeted boost (`_score_up_pool`).
    ratio_owners: list[str | None] = []
    boost_targets: list[frozenset[str] | None] = []
    contributions: list[SkillContribution] = []

    for resolved in resolved_skills:
        pool = _pool_for(resolved)
        if pool == "none" and not with_breakdown:
            # Already folded into team_rate_up; nothing left to compute on the hot path.
            continue

        rate, state, missing_field = _resolve_skill(resolved, cfg, team_rate_up)
        weight = cfg.family_weights[resolved.family]
        weighted_rate = rate * weight

        # Taken from the same resolution the score uses, so the attribution cannot
        # disagree with it. `None` for rows that resolved no target.
        target_kind: str | None = None
        target_card_ids: tuple[str, ...] | None = None

        if pool == "stat":
            stat_rates.append(weighted_rate)
            # Only an `applied` skill has a known target; another's rate is 0.0 and its
            # target may be the UNKNOWN that made it data_missing.
            if state == "applied":
                target_ids = targets.resolve_target_card_ids(
                    resolved.skill.target,
                    resolved.card_id,
                    cards,
                    STAT_NAMES_BY_EFFECT_TYPE[resolved.skill.effect_type],
                    cfg.target_selection_model,
                    cfg.target_under_supply,
                )
                target_kind = (
                    resolved.skill.target.kind
                    if isinstance(resolved.skill.target, EffectTarget)
                    else TEAM_WIDE_TARGET_KIND
                )
                target_card_ids = tuple(sorted(target_ids))
            else:
                target_ids = frozenset()
            stat_entries.append(
                (
                    weighted_rate,
                    STAT_NAMES_BY_EFFECT_TYPE[resolved.skill.effect_type],
                    target_ids,
                    resolved.skill.target if state == "applied" else _FIXED_TARGET,
                    resolved.card_id,
                    resolved.skill.kind,
                )
            )
            if with_breakdown:
                stat_contribution_indices.append(len(contributions))
        elif pool == "score":
            # Only a Score UP joins a bucket maximum (`_is_score_up_effect`).
            if _is_score_up_effect(resolved):
                entry = _lattice_entry(resolved, cfg, team_rate_up, weight, state)
                # A SPECIAL Score UP joins `B`, not the ratio (`_score_up_implements_boost`).
                if _score_up_implements_boost(resolved):
                    boost_entries.append(entry)  # type: ignore[arg-type]
                    boost_targets.append(None)
                else:
                    ratio_entries.append(entry)  # type: ignore[arg-type]
                    ratio_owners.append(resolved.card_id)
            else:
                score_rates.append(weighted_rate)
        elif pool == "support":
            # Joins `B`, the multiplier on the ACTIVE maximum (`_score_up_pool`).
            support_target_ids: frozenset[str] = frozenset()
            if state == "applied":
                support_target_ids = targets.resolve_target_card_ids(
                    resolved.skill.target,
                    resolved.card_id,
                    cards,
                    STAT_NAMES,
                    cfg.target_selection_model,
                    cfg.target_under_supply,
                    rank_values=active_rank,
                )
                target_kind = (
                    resolved.skill.target.kind
                    if isinstance(resolved.skill.target, EffectTarget)
                    else TEAM_WIDE_TARGET_KIND
                )
                target_card_ids = tuple(sorted(support_target_ids))
            # A support reaching nobody never enters the collection; a team-wide one
            # boosts every ACTIVE, a targeted one its recipients' (`_support_targets`).
            boost_entries.append(
                _lattice_entry(resolved, cfg, team_rate_up, weight, state)  # type: ignore[arg-type]
                if support_target_ids
                else (
                    _score_bucket(resolved),
                    0.0,
                    0.0,
                    _active_interval(resolved),
                    _windows_are_mutually_exclusive(resolved),
                    # no lattice
                    0.0,
                    0.0,
                )
            )
            boost_targets.append(_support_targets(resolved, support_target_ids))
        # pool == "none": already in team_rate_up; never joins a pool list.

        if with_breakdown:
            gate = resolved.gate
            contributions.append(
                SkillContribution(
                    card_id=resolved.card_id,
                    slot_kind=resolved.slot_kind,
                    source=resolved.source,
                    family=resolved.family,
                    effect_type=resolved.skill.effect_type,
                    state=state,
                    pool=pool,
                    rate=rate,
                    weight=weight,
                    weighted_rate=weighted_rate,
                    missing_field=missing_field,
                    gate_dimension=gate.predicate.dimension if gate is not None else None,
                    gate_value=gate.predicate.value if gate is not None else None,
                    gate_threshold=gate.predicate.threshold if gate is not None else None,
                    gate_actual_count=gate.actual_count if gate is not None else None,
                    gate_fired=gate.fired if gate is not None else None,
                    target_kind=target_kind,
                    target_card_ids=target_card_ids,
                    override_fired=(
                        resolved.override_gate.fired if resolved.override_gate is not None else None
                    ),
                    rider_family=(
                        resolved.skill.rider.family if isinstance(resolved.skill.rider, SlotRider) else None
                    ),
                    rider_effect_type=(
                        resolved.skill.rider.effect_type
                        if isinstance(resolved.skill.rider, SlotRider)
                        else None
                    ),
                    rider_magnitude=(
                        resolved.skill.rider.magnitude
                        if isinstance(resolved.skill.rider, SlotRider)
                        else None
                    ),
                )
            )

    combined_stat_rate = cfg.stacking_model.combine(stat_rates)
    # The game's `B` (max per bucket, then added), the multiplier on every Score UP; the
    # same max-then-add rule as the score pool, not a `stacking_model` combine.
    combined_support_rate = cfg.active_stacking_model.combine(boost_entries)  # type: ignore[arg-type]

    # Read before the pool: `board_skill_effect_up` goes inside its multiplier.
    board_power, board_score_rate, board_skill_effect_up = _board_bonus(team.leader, cards, cfg)
    # Every Score UP collapses into one rate (`SkillScoreUpRatio`), appended last so the
    # combine order matches `score_card_set`'s list bit for bit.
    score_up_pool = _score_up_pool(
        ratio_entries,
        boost_entries,
        cfg,
        board_skill_effect_up,
        ratio_owners,
        boost_targets,
    )
    combined_score_rate = cfg.stacking_model.combine([*score_rates, score_up_pool])

    # `member_order` is the slot order the rule chose, `None` when it chooses none.
    effective_stat_sum, member_order = _effective_stat_sum(cards, stat_entries, cfg)

    total_score = (effective_stat_sum + board_power) * (1.0 + combined_score_rate) * (1.0 + board_score_rate)

    if not with_breakdown:
        return total_score

    if member_order is not None:
        # A rule that picks recipients by slot order chooses them after the rows were
        # built: rewrite `target_card_ids` to the cards the scored number reached.
        ordered_members = tuple(
            next(card for card in cards if card.id == card_id) for card_id in member_order
        )
        for entry_index, contribution_index in enumerate(stat_contribution_indices):
            entry = stat_entries[entry_index]  # type: ignore[assignment]
            if entry[3] is _FIXED_TARGET:
                continue
            recipients = _ordered_target_card_ids(
                entry[3],
                entry[4],  # type: ignore[arg-type]
                ordered_members,
                cfg.target_under_supply,
            )
            contributions[contribution_index] = replace(
                contributions[contribution_index],
                target_card_ids=tuple(sorted(recipients)),
            )

    # Two account-level terms the game's Unit Score Details panel shows and the ranking
    # omits: Member Power-Up (a near-uniform x1.0035 across decks) and Memory Bonus (a
    # few permil of one line). Both are ranking-safe to drop, so they are reported only.
    # Memory Bonus uses the bit-exact per-member per-stat form (`memory_bonus_per_stat`,
    # the server's `power.memory`) on `Card.stats`, the leveled+bloomed base parameter.
    #
    # Overall Power (deck-screen display), with the game's rounding: the Outfit + Passive
    # stat lines are per-(card, attribute) float32 ceilings, and Member Power-Up pays five
    # per-slot ceilings over each member's (Member Parameter + Outfit + Passive + Holomem
    # Board) base. Display only: `effective_stat_sum`/`total_score` stay float64.
    #
    # Recipients: on an exact stat tie the game keeps deck slot order. The ranking's
    # `TopNByBuffedStat` is a stable sort over `cards` (the deck in slot order), so
    # `entry[2]` already equals this; re-resolving under `_SLOT_ORDER_TIE_BREAK` keeps the
    # display correct whatever the ranking's selection model is.
    op_recipients = [
        entry[2]
        if entry[3] is _FIXED_TARGET
        else targets.resolve_target_card_ids(
            entry[3], entry[4], cards, entry[1], _SLOT_ORDER_TIE_BREAK, cfg.target_under_supply
        )
        for entry in stat_entries
    ]
    op_stat_lines: dict[str, float] = {}
    op_stat_subtotals = _overall_power_stat_subtotals(cards, stat_entries, cfg, op_recipients, op_stat_lines)
    overall_power_stat_sum = float(sum(op_stat_subtotals))
    # Per-member board power for the per-slot Member Power-Up base: `power_bonus_for` sums
    # over the members passed, so a single-member call is that member's share (cached per
    # card, not a re-walk).
    _holomem = None if team.leader is None else team.leader.member
    per_member_board_power = (
        [0.0] * len(cards)
        if cfg.board is None
        else [cfg.board.power_bonus_for((c,), _holomem) for c in cards]
    )
    power_up_bases = [op_stat_subtotals[i] + per_member_board_power[i] for i in range(len(cards))]

    account = getattr(cfg, "account", None)
    memory_line = (
        None
        if account is None
        else memory_bonus_per_stat(
            [(m.stats.performance, m.stats.technique, m.stats.sense) for m in team.members],
            account.memory_permil,
        )
    )
    power_up_line = None if account is None else member_power_up(power_up_bases, account.power_up_permyriad)

    return ScoreBreakdown(
        total_score=total_score,
        memory_bonus=memory_line,
        member_power_up=power_up_line,
        overall_power_stat_sum=overall_power_stat_sum,
        overall_power_stat_lines=op_stat_lines,
        power_up_bases=tuple(power_up_bases),
        base_stat_sum=float(team.base_stat_sum()),
        effective_stat_sum=effective_stat_sum,
        combined_stat_rate=combined_stat_rate,
        combined_score_rate=combined_score_rate,
        combined_support_rate=combined_support_rate,
        active_pool=score_up_pool,
        board_score_rate=board_score_rate,
        board_power=board_power,
        team_rate_up=team_rate_up,
        contributions=tuple(contributions),
        incomplete_skills=team_taint(team),
        stacking_model_name=cfg.stacking_model.name,
        activation_rate_model_name=cfg.activation_rate_model.name,
        active_stacking_model_name=cfg.active_stacking_model.name,
        run_state=cfg.run_state,
        slot_order_rule=cfg.slot_order_rule,
        member_order=member_order,
    )


def score_fast(team: Team, cfg: ScoringConfig) -> float:
    """Hot-loop path: a bare float, no attribution allocation."""
    return _score_team(team, cfg)  # type: ignore[return-value]


def score_with_breakdown(team: Team, cfg: ScoringConfig) -> ScoreBreakdown:
    """Attribution path: delegates to the same driver as `score_fast`,
    retaining every intermediate `SkillContribution`."""
    return _score_team(team, cfg, with_breakdown=True)  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class LeaderScore:
    """One leader's total score within a scored 6-card set."""

    leader_id: str
    total_score: float


def _leader_class_key(
    leader: Card, member_ids: frozenset[str], board_leader_scoped: frozenset[str]
) -> object:
    """The equivalence class of a candidate leader for ONE member set: leaders in one class
    score identically.

    A leader contributes only its outfit, so the key is the outfit (`SkillSlot` is a frozen
    dataclass and hashes as itself), except:
    1. a `self`-targeted costume resolves to the leader's own id, so it keys by id (no
       shipped costume has one; the branch keeps a future one from merging silently);
    2. a leader that is also one of the five interacts through its own id, so it keys by id;
    3. a leader whose holomem has leader-scoped board effects (`board_leader_scoped`, the
       red branch that applies only while she leads) merges only with leaders of the same
       holomem and outfit."""
    costume = leader.costume_skill
    if costume is not None and getattr(costume.target, "kind", None) == "self":
        return ("id", leader.id)
    if leader.id in member_ids:
        return ("id", leader.id)
    if leader.member in board_leader_scoped:
        return ("holomem", leader.member, costume)
    return ("costume", costume)


def score_card_set(
    members: tuple[Card, ...], leaders: tuple[Card, ...], cfg: ScoringConfig
) -> tuple[LeaderScore, ...]:
    """The set-level batch entry point the solver calls: one `LeaderScore` per entry of
    `leaders`, in order.

    `members` is the set of 5; `leaders` are candidate outfit cards and need not be drawn
    from `members` (the leader slot is outside the five). Everything that depends only on
    the member set is computed once: gate resolution against one `TeamAggregates` snapshot,
    `team_rate_up`, every member skill's rate, pool, recipients and bucket term (cached by
    `(card_id, slot_kind, source)`). Only the leader's outfit, the board term for
    leader-scoped holomems and the final combines vary per leader. Each costume is resolved
    against the members' aggregates, so a gated outfit cannot satisfy its own condition.

    `team_rate_up` is leader-invariant because no COSTUME slot can carry
    `activation_rate_up` as its principal family (`schema.FAMILY_REQUIRES_SLOT_KIND`,
    enforced at load by `validate_skill_slot`); the check below raises `ScoringError` for a
    record that bypassed validation. A costume rider is never `activation_rate_up`.

    Per leader, the cached entries are walked in `resolve_team_gates` order and routed
    exactly as `_score_team` routes them, and every `combine()` runs over the full list,
    never folded incrementally: float addition and multiplication are not associative, and
    parity with `score_fast`/`score_with_breakdown` is bit-identical by construction, not by
    tolerance. `combine()` over ~19 floats is cheap; the shared EV work is what is saved."""
    card_set_gates = resolve_card_set_gates(members, cfg.run_state)

    # Checked over the candidate leaders: only their costume slots are used, and they may
    # be outside the member set.
    for card in leaders:
        costume_slot = card.costume_skill
        if costume_slot is not None and costume_slot.family == "activation_rate_up":
            raise ScoringError(
                "score_card_set assumes team_rate_up is leader-invariant "
                "because validate_skill_slot rejects family "
                "'activation_rate_up' on any slot but ACTIVE at load time "
                "(a COSTUME slot can never carry it); reaching this line "
                "means the record bypassed load-time validation",
                card_id=card.id,
                slot_kind=COSTUME_SLOT_KIND,
            )

    normal_resolved: list[ResolvedSkill] = []
    for card in members:
        normal_resolved.extend(card_set_gates.normal_by_card_id[card.id])
    team_rate_up = _fold_activation_rate_up(tuple(normal_resolved), cfg) + _fold_rider_activation_rate_up(
        tuple(normal_resolved), cfg
    )
    active_rank = active_magnitude_by_card(normal_resolved)

    # Per skill instance, leader-invariant (gates, targets and occupancy depend only on the
    # member set): `(weighted_rate, pool, target_ids, bucket_term, order_target)`.
    # `target_ids` is empty for a skill that is not `applied` (its rate is 0.0).
    # `bucket_term` is the `_lattice_entry` term for a Score UP or Score Support, else
    # `None`; magnitude and occupancy must reach the per-leader tail apart, since a maximum
    # cannot be taken over their product. `order_target` is as in `StatEntry`.
    cache: dict[
        tuple[str, str, str],
        tuple[float, str, frozenset, tuple | None, object],
    ] = {}
    for resolved in normal_resolved:
        pool = _pool_for(resolved)
        if pool == "none":
            continue
        rate, state, _missing_field = _resolve_skill(resolved, cfg, team_rate_up)
        weight = cfg.family_weights[resolved.family]
        weighted_rate = rate * weight
        target_ids: frozenset = frozenset()
        if state == "applied" and pool in ("stat", "support"):
            # A support ranks its qualifiers by ACTIVE magnitude (`active_magnitude_by_card`):
            # it raises no stat. A stat target ranks by the stat it raises.
            target_ids = targets.resolve_target_card_ids(
                resolved.skill.target,
                resolved.card_id,
                members,
                STAT_NAMES if pool == "support" else STAT_NAMES_BY_EFFECT_TYPE[resolved.skill.effect_type],
                cfg.target_selection_model,
                cfg.target_under_supply,
                rank_values=active_rank if pool == "support" else None,
            )
        cache[(resolved.card_id, resolved.slot_kind, resolved.source)] = (
            weighted_rate,
            pool,
            target_ids,
            _bucket_term(resolved, cfg, team_rate_up, weight, state, pool, target_ids),
            resolved.skill.target if state == "applied" else _FIXED_TARGET,
        )
    # Leader equivalence classes (`_leader_class_key`). A leader contributes only its
    # outfit, and a realistic pool wears far fewer outfits than it has leaders (e.g. 25
    # across 94 cards), so scoring one representative per class cuts the per-leader work,
    # costume resolution included, by roughly 3-4x. Exact, not an approximation;
    # `tests/test_scoring.py` pins the expansion against a per-leader walk.
    member_ids = frozenset(card.id for card in members)
    board_leader_scoped_for_key: frozenset[str] = (
        frozenset() if cfg.board is None else cfg.board.leader_scoped_holomem
    )
    class_indices: dict[object, list[int]] = {}
    for index, candidate in enumerate(leaders):
        key = _leader_class_key(candidate, member_ids, board_leader_scoped_for_key)
        class_indices.setdefault(key, []).append(index)
    # The representative scored for each class, and every position in `leaders` its
    # answer is written to.
    representatives: list[Card] = []
    positions_by_representative: dict[str, list[int]] = {}
    for found in class_indices.values():
        representative = leaders[found[0]]
        representatives.append(representative)
        positions_by_representative[representative.id] = found

    # One resolved outfit per representative leader, against the members' aggregates
    # (its gate sees only the five; a leader outside them has no `costume_by_card_id`
    # entry). An outfit is a tuple: the principal, then its scored `score_support` rider
    # (`gates.resolve_leader_costume_skills`), each cached under its own key.
    leader_costumes: dict[str, tuple[ResolvedSkill, ...]] = {}
    for card in representatives:
        costume_skills = resolve_leader_costume_skills(card, card_set_gates.aggregates, cfg.run_state)
        if costume_skills:
            leader_costumes[card.id] = costume_skills
    for costume in (c for skills in leader_costumes.values() for c in skills):
        pool = _pool_for(costume)
        if pool == "none":
            continue
        rate, state, _missing_field = _resolve_skill(costume, cfg, team_rate_up)
        weight = cfg.family_weights[costume.family]
        weighted_rate = rate * weight
        target_ids = frozenset()
        if state == "applied" and pool in ("stat", "support"):
            # Targets resolve over the MEMBERS: only they carry stats to raise and ACTIVEs to
            # multiply.
            target_ids = targets.resolve_target_card_ids(
                costume.skill.target,
                costume.card_id,
                members,
                STAT_NAMES if pool == "support" else STAT_NAMES_BY_EFFECT_TYPE[costume.skill.effect_type],
                cfg.target_selection_model,
                cfg.target_under_supply,
                rank_values=active_rank if pool == "support" else None,
            )
        cache[(costume.card_id, costume.slot_kind, costume.source)] = (
            weighted_rate,
            pool,
            target_ids,
            _bucket_term(costume, cfg, team_rate_up, weight, state, pool, target_ids),
            costume.skill.target if state == "applied" else _FIXED_TARGET,
        )

    # Without a structural board, `_board_score_rate` reads only the five members, so it is
    # hoisted out of the per-leader tail (same function, same tuple, same order as
    # `_score_team`). With one, the red branch applies only while its holomem leads, so
    # the term is per leader below.
    hoisted_board_bonus = (0.0, _board_score_rate(members, cfg), 0.0) if cfg.board is None else None

    # For a leader outside `BoardModel.leader_scoped_holomem` (the holomems owning a red
    # node) the board term is bit-identical to the `leader=None` one by code path, so it
    # is computed once per member set; leaders who can move it pay one `_board_bonus`,
    # memoised by holomem (the only attribute of the leader it reads).
    board_leader_scoped: frozenset[str] = (
        frozenset() if cfg.board is None else cfg.board.leader_scoped_holomem
    )
    board_bonus_invariant = None if cfg.board is None else _board_bonus(None, members, cfg)
    board_bonus_by_holomem: dict[str, tuple[float, float, float]] = {}

    # Handed to the slot-order resolver, shared across leaders: the members, and so any
    # ordering of them, are fixed for this call.
    orderings_cache: dict = {}

    # One score per class, expanded to every leader in it; `scores` is indexed by position
    # in `leaders`, so the result stays positional.
    scores: list[float] = [0.0] * len(leaders)
    # The loop variable stays `leader`:
    # `test_batched_path_calls_combine_inside_the_per_leader_loop_never_before_it` finds this
    # loop by that name to prove every `combine()` lies inside it.
    for leader in representatives:
        stat_rates: list[float] = []
        # See `StatEntry`.
        stat_entries: list[CardSetStatEntry] = []
        # Mirroring `_score_team`'s lists.
        score_rates: list[float] = []
        ratio_entries: list[tuple[str, float, float, int, bool]] = []
        boost_entries: list[tuple[str, float, float, int, bool]] = []
        ratio_owners: list[str | None] = []
        boost_targets: list[frozenset[str] | None] = []

        # `resolve_team_gates` order: the leader's outfit effects, then every member's
        # normal slots in member order. A leader that is also a member contributes its
        # normal slots once, through the member loop.
        ordered_resolved: list[ResolvedSkill] = []
        ordered_resolved.extend(leader_costumes.get(leader.id, ()))
        for card in members:
            ordered_resolved.extend(card_set_gates.normal_by_card_id[card.id])

        for resolved in ordered_resolved:
            cached = cache.get((resolved.card_id, resolved.slot_kind, resolved.source))
            if cached is None:
                continue
            weighted_rate, pool, target_ids, bucket_term, order_target = cached
            if pool == "stat":
                stat_rates.append(weighted_rate)
                stat_entries.append(
                    (
                        weighted_rate,
                        STAT_NAMES_BY_EFFECT_TYPE[resolved.skill.effect_type],
                        target_ids,
                        order_target,
                        resolved.card_id,
                    )
                )
            elif pool == "score":
                if bucket_term is None:
                    score_rates.append(weighted_rate)
                elif _score_up_implements_boost(resolved):
                    # The SPECIAL collapse, routed as in `_score_team`.
                    boost_entries.append(bucket_term)
                    boost_targets.append(None)
                else:
                    ratio_entries.append(bucket_term)
                    ratio_owners.append(resolved.card_id)
            elif pool == "support":
                assert bucket_term is not None  # every support caches a term
                boost_entries.append(bucket_term)
                boost_targets.append(_support_targets(resolved, target_ids))

        # Read before the pool: `board_skill_effect_up` goes inside its multiplier.
        if hoisted_board_bonus is not None:
            board_power, board_score_rate, board_skill_effect_up = hoisted_board_bonus
        elif leader.member in board_leader_scoped:
            cached_board = board_bonus_by_holomem.get(leader.member)
            if cached_board is None:
                cached_board = _board_bonus(leader, members, cfg)
                board_bonus_by_holomem[leader.member] = cached_board
            board_power, board_score_rate, board_skill_effect_up = cached_board
        else:
            assert board_bonus_invariant is not None  # cfg.board is not None
            board_power, board_score_rate, board_skill_effect_up = board_bonus_invariant

        score_up_pool = _score_up_pool(
            ratio_entries,
            boost_entries,
            cfg,
            board_skill_effect_up,
            ratio_owners,
            boost_targets,
        )
        combined_score_rate = cfg.stacking_model.combine([*score_rates, score_up_pool])

        # The same `_effective_stat_sum` `_score_team` calls; the leader contributes no stats.
        effective_stat_sum, _member_order = _effective_stat_sum(members, stat_entries, cfg, orderings_cache)

        total_score = (
            (effective_stat_sum + board_power) * (1.0 + combined_score_rate) * (1.0 + board_score_rate)
        )
        # The class's answer, written onto every leader in it (the same float object).
        for index in positions_by_representative[leader.id]:
            scores[index] = total_score

    return tuple(
        LeaderScore(leader_id=leader.id, total_score=scores[index]) for index, leader in enumerate(leaders)
    )


# Defined after `_score_team`, which calls it; the name binds at call time.
def _overall_power_stat_subtotals(
    members: tuple[Card, ...],
    stat_entries: list,
    cfg: ScoringConfig,
    recipients: list,
    by_source: dict[str, float] | None = None,
) -> list[float]:
    """The deck-screen Overall Power stat block, per member, in `members` order.

    The display twin of `_stat_sum`, rounding where the server rounds. Per (card, attribute)
    the server pays

        member_parameter (the integer base, summed exactly)
        + ceil( f32(combined_rate * base) )   <- the Outfit + Passive stat lines

    with the product in IEEE-754 binary32, as `collection._f32` and `board.power_bonus_for`
    do. Returned per member so `member_power_up` can pay its five per-slot ceilings; the
    caller adds each member's board power to form that base. `by_source`, when given, gets
    each source line's total (`{slot_kind: sum of its ceilings}`: Outfit Skill
    `COSTUME_SLOT_KIND`, Passive Skill `"PASSIVE"`).

    Display only: the ranking (`_stat_sum` -> `total_score`) stays float64, where only a
    monotone ordering matters."""
    # Grouped by (source line, card, attribute): the server reports Outfit Skill and
    # Passive Skill as separate power lines and ceils each. One ceiling over all sources
    # understates; one per effect overstates when two effects share a line.
    buckets: dict[tuple[str, str, str], list[float]] = {}
    for entry, target_ids in zip(stat_entries, recipients):
        if not target_ids:
            continue
        rate = entry[0]
        slot_kind = entry[5]
        for stat_name in entry[1]:
            for card_id in target_ids:
                key = (slot_kind, card_id, stat_name)
                bucket = buckets.get(key)
                if bucket is None:
                    buckets[key] = [rate]
                else:
                    bucket.append(rate)

    # Which source lines touch each card, so only the live groups are ceiled.
    sources_by_card: dict[str, set[str]] = {}
    for slot_kind, card_id, _stat in buckets:
        sources_by_card.setdefault(card_id, set()).add(slot_kind)

    empty: list[float] = []
    combine = cfg.stacking_model.combine
    subtotals: list[float] = []
    for card in members:
        card_id = card.id
        stats = card.stats
        # MemberParameter: the integer base parameters, summed exactly.
        member_total = float(stats.performance + stats.technique + stats.sense)
        for slot_kind in sources_by_card.get(card_id, ()):
            for stat_name in STAT_NAMES:
                combined = combine(buckets.get((slot_kind, card_id, stat_name), empty))
                if combined:
                    base = getattr(stats, stat_name)
                    # Product rounded in single precision, then ceiled (as
                    # `collection.effective_stats` does).
                    paid = math.ceil(_f32(combined * base))
                    member_total += paid
                    if by_source is not None:
                        by_source[slot_kind] = by_source.get(slot_kind, 0.0) + paid
        subtotals.append(member_total)
    return subtotals


class _SlotOrderTieBreak:
    """Display-only recipient selection: the top `count` candidates by value, an exact tie
    broken by slot order (the order `targets.resolve_target_card_ids` builds `candidates`
    in, i.e. `cards`). `sorted` is stable, so this behaves like `TopNByBuffedStat`; it is a
    separate class so the Overall Power assembly does not depend on
    `cfg.target_selection_model`."""

    name = "slot_order_tie_break"

    def select(self, candidates: Sequence[tuple[str, float]], count: int) -> list[str]:
        ordered = sorted(candidates, key=lambda candidate: -candidate[1])
        return [card_id for card_id, _total in ordered[:count]]


_SLOT_ORDER_TIE_BREAK = _SlotOrderTieBreak()


def _active_lattice(
    resolved: ResolvedSkill, cfg: ScoringConfig, team_rate_up: float, state: str
) -> tuple[float, float]:
    """`ActiveTerm`'s trailing `(duration, probability)`: the effect duration in seconds and the
    resolved activation probability (the `activation_rate_model.apply(tier, team_rate_up +
    board_rate)` that `_active_occupancy` folds into occupancy), kept apart for a
    per-instant lattice law, which needs `P` alone. `(0.0, 0.0)` for anything but an applied
    ACTIVE with a numeric duration. The shipped `max` combiner ignores both elements."""
    slot = resolved.skill
    if state != "applied" or slot.kind != "ACTIVE":
        return (0.0, 0.0)
    duration = slot.duration
    if isinstance(duration, bool) or not isinstance(duration, (int, float)):
        return (0.0, 0.0)
    board_rate = cfg.board.uptime_for(resolved.member)[0] if cfg.board is not None else 0.0
    base_prob = cfg.probability_tiers[slot.probability_tier]
    return (
        float(duration),
        cfg.activation_rate_model.apply(base_prob, team_rate_up + board_rate),
    )


def _lattice_entry(
    resolved: ResolvedSkill,
    cfg: ScoringConfig,
    team_rate_up: float,
    weight: float,
    state: str,
) -> tuple[str, float, float, int, bool, float, float]:
    """`_bucket_entry` plus the trailing `(duration, probability)`: the 7-tuple `ActiveTerm`
    the pool builders emit. `_bucket_entry` keeps its 5-tuple for its other readers
    (`song.py`, shape tests)."""
    return _bucket_entry(resolved, cfg, team_rate_up, weight, state) + _active_lattice(
        resolved, cfg, team_rate_up, state
    )


# ---------------------------------------------------------------------------
# Per-object memos for two pure helpers the leaf calls ~30 times per team. Bound at the
# end of the module so the reference definitions above stay what each memo is checked
# against; every caller resolves the name at call time and gets the memo.
# ---------------------------------------------------------------------------
_first_missing_field_reference = _first_missing_field
_missing_field_cache: dict[int, tuple] = {}


def _first_missing_field(resolved: ResolvedSkill) -> str | None:  # type: ignore[no-redef]  # noqa: F811
    """`_first_missing_field_reference`, memoised per slot OBJECT (the answer depends on the
    frozen slot alone; the slot is kept in the entry so a recycled `id()` cannot hit)."""
    slot = resolved.skill
    hit = _missing_field_cache.get(id(slot))
    if hit is None or hit[0] is not slot:
        if len(_missing_field_cache) > 200_000:
            _missing_field_cache.clear()
        hit = (slot, _first_missing_field_reference(resolved))
        _missing_field_cache[id(slot)] = hit
    return hit[1]


_card_taint_cache: dict[int, tuple] = {}


def team_taint(team: Team) -> tuple[str, ...]:
    """The team's completeness indicator, derived without scoring: every member's
    `Card.unknown_fields()` paths prefixed with the card id, in card then field order.
    Non-empty means data the output layer reports. Memoised per card OBJECT."""
    fields: list[str] = []
    for card in team.members:
        hit = _card_taint_cache.get(id(card))
        if hit is None or hit[0] is not card:
            if len(_card_taint_cache) > 200_000:
                _card_taint_cache.clear()
            hit = (card, tuple(f"{card.id}:{field}" for field in card.unknown_fields()))
            _card_taint_cache[id(card)] = hit
        fields.extend(hit[1])
    return tuple(fields)
