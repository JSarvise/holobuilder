"""Stage three of the three-stage team evaluation (see `domain/gates.py`'s
module docstring): reduce resolved skills into a scored float.

Every EV function here returns an **average per-run rate**, never a point
total and never a point-in-time indicator — no song-length
parameter exists anywhere in this module, and none should ever be added:
song length cancels out of every team-vs-team comparison (see also
`scoring_config.REFERENCE_RUN_SECONDS`).
"""

from __future__ import annotations

import itertools, math  # noqa: E401
from dataclasses import dataclass, replace
from functools import cache
from typing import Callable

from hbuilder.domain import board, targets
from hbuilder.domain.collection import _f32, member_power_up, memory_bonus_per_stat
from hbuilder.domain.card import Card, EffectTarget, SlotRider
from hbuilder.domain.gates import (
    ResolvedSkill,
    RunState,
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
from hbuilder.domain.scoring_config import (
    REFERENCE_RUN_SECONDS,
    SLOT_ORDER_RULES,
    ActiveStackingModel,
    ScoringConfig,
)
from hbuilder.domain.team import Team
from hbuilder.domain.unknown import is_known


class ScoringError(Exception):
    """Raised when scoring is asked to do something structurally invalid —
    e.g. an EV function dispatched against a slot of the wrong kind.
    Mirrors `SchemaError`'s card/field-naming discipline: `str()`
    always names the card id and slot kind whenever known, so a routing
    bug surfaces as a named failure, never as a silently wrong number."""

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
    """The one named percent-to-fraction boundary: on-disk
    `magnitude` is stored in percentage points (25.0 means +25%); every EV
    formula converts through this single helper, never inline, never
    anywhere else in the codebase."""
    return magnitude / 100.0


# Which score "pool" each `effect_type` feeds. `activation_rate` routes to
# "none" — an activation-rate skill's own contribution never joins the
# stat or score pool; it is folded into `team_rate_up` by
# `_fold_activation_rate_up` and structurally never receives the team
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


def _route(effect_type: str) -> str:
    return POOL_BY_EFFECT_TYPE[effect_type]


# `score_support` does NOT add to the score pool.
# It is a MULTIPLIER on the ACTIVE pool — the game's own effect id says so
# (`score_up_EFFECT_up_permil_up` on every SPECIAL, and
# `live_active_skill_EFFECT_up_permil_up` on every Score Support outfit,
# against a plain `score_up_permil_up` on every ACTIVE), and it is measured:
# solving the reference duration from all ten captured decks gives a constant
# to within 6.2 % under the multiplier reading and a 90 % spread under the
# addend reading.
#
# Routing is by FAMILY here, not by `effect_type`, deliberately. The on-disk
# `effect_type` of a `score_support` slot is `"score"` in every real
# instance, and `data/cards.json` is hand-authored and authoritative — no
# code path may rewrite it. The pool is a property of the family
# in this one case, so that is where it is read from.
SUPPORT_FAMILY = "score_support"


def _pool_for(resolved: ResolvedSkill) -> str:
    """The pool a resolved skill feeds. `score_support` overrides its
    `effect_type` routing; everything else routes by `effect_type` exactly as
    `POOL_BY_EFFECT_TYPE` declares."""
    if resolved.family == SUPPORT_FAMILY:
        return "support"
    return POOL_BY_EFFECT_TYPE[resolved.skill.effect_type]


# Import-time structural guarantee: every effect_type any family can carry
# (schema.py's EFFECT_TYPES_BY_FAMILY) must have a pool assignment here, so
# a seventh family added to schema.py without a matching pool entry fails
# at import rather than as a silent KeyError deep inside a solver run.
assert set(POOL_BY_EFFECT_TYPE) == set().union(*EFFECT_TYPES_BY_FAMILY.values()), (
    "POOL_BY_EFFECT_TYPE must cover every effect_type declared in "
    "EFFECT_TYPES_BY_FAMILY — an unrouted effect_type must fail at import, "
    "never at score time"
)


# The single source of "which stats does this effect_type raise" (an
# effect_type may name more than one stat). The three single-stat entries are the
# general form's one-element degenerate case, not a special case —
# `_score_team`/`score_card_set`'s per-`(card, stat_name)` accumulation
# already iterates a tuple of stat names natively, so `all_stats`'s
# three-element tuple needs no new branch anywhere in this module.
STAT_NAMES_BY_EFFECT_TYPE: dict[str, tuple[str, ...]] = {
    "performance": ("performance",),
    "technique": ("technique",),
    "sense": ("sense",),
    "all_stats": STAT_NAMES,
}

# Import-time structural guarantee, same style as the POOL_BY_EFFECT_TYPE
# assert above: every effect_type routed to the "stat" pool must have a
# STAT_NAMES_BY_EFFECT_TYPE entry, and every stat name it declares must be a
# real STAT_NAMES member, so a half-updated three-table edit
# (EFFECT_TYPES_BY_FAMILY / POOL_BY_EFFECT_TYPE / STAT_NAMES_BY_EFFECT_TYPE)
# fails at import, never as a silent KeyError or a wrong-pool score deep
# inside a solver run.
assert set(STAT_NAMES_BY_EFFECT_TYPE) == {
    effect_type for effect_type, pool in POOL_BY_EFFECT_TYPE.items() if pool == "stat"
}, (
    "STAT_NAMES_BY_EFFECT_TYPE must cover every effect_type routed to the "
    "'stat' pool by POOL_BY_EFFECT_TYPE — an effect_type missing its "
    "stat-name declaration must fail at import, never at score time"
)
assert all(
    stat_name in STAT_NAMES
    for stat_names in STAT_NAMES_BY_EFFECT_TYPE.values()
    for stat_name in stat_names
), "STAT_NAMES_BY_EFFECT_TYPE values must only name real STAT_NAMES members"
# `_stat_sum` scatters each entry's rate into one bucket per declared stat
# name. A tuple WITH repeats would place the rate twice, so the no-repeat
# property is asserted here rather than left as an unwritten assumption
# inside the hot loop.
assert all(
    len(set(stat_names)) == len(stat_names)
    for stat_names in STAT_NAMES_BY_EFFECT_TYPE.values()
), "STAT_NAMES_BY_EFFECT_TYPE values must not repeat a stat name — `_stat_sum` scatters"


# The one shared known-fields oracle: "which fields must be known before this
# skill may contribute at all". Not all of them are EV-formula inputs;
# `condition` decides whether the formula runs.
#
# One list serves both consumers: `_resolve_skill` reads it via
# `_first_missing_field` and `_fold_activation_rate_up` reads it via
# `_has_all_known_fields`, so there is exactly one place to edit and no way
# to fix one path and silently leave the other open. Writing the same rule
# twice is how one copy drifts.
#
# `condition` is first so it wins the deterministic first-missing report:
# an unrecorded gate is the reason the skill may not contribute at all,
# which is more informative than whichever arithmetic input is also blank.
# An unrecorded gate is *not* the same thing as an affirmatively
# ungated one. `gates.py::_resolve_one_skill` collapses both to
# `gate = None` (an UNKNOWN condition must never reach
# `predicate_from_condition`), so this is where they are told apart again.
# Scoring the former as the latter inflates the total — the one direction
# the `>=` lower-bound marker cannot express.
#
# `target` follows
# `condition` — an untranscribed target is, like an unrecorded gate, a
# reason the skill may not contribute at its face value at all (reading it
# as team-wide would overstate, the one direction `>=` cannot express), so
# it must produce `data_missing` too, never a silent whole-team
# application at full value.
#
# `override` comes next — an unrecorded override means the transcriber may
# have stopped at the first sentence of the skill text, the most likely
# data-entry error. Reading an
# untranscribed override as "no override" would silently understate a
# fired-override case if a real override exists but was never typed in —
# `data_missing` makes that gap visible instead.
_REQUIRED_KNOWN_FIELDS: tuple[str, ...] = (
    "condition",
    "target",
    "override",
    "duration",
    "interval",
    "probability_tier",
    "magnitude",
)

# The three states a per-skill contribution can land in. `applied` is
# a real (possibly zero) contribution; `gate_not_met` and `data_missing`
# are both zero but for structurally different, distinguishable reasons —
# neither is ever reported as if it were the other.
CONTRIBUTION_STATES: tuple[str, ...] = ("applied", "gate_not_met", "data_missing")


def _first_missing_field(resolved: ResolvedSkill) -> str | None:
    """The name of the first field this skill needs known before it may
    contribute at all but which is `UNKNOWN`, or `None` if every
    needed field is known. That covers both whether the skill is gated
    (`condition`) and the arithmetic inputs the EV formula for this slot's
    kind reads. Fields structurally not-applicable to this slot kind
    (`NOT_APPLICABLE_FIELDS_BY_SLOT_KIND`) are never checked, since their
    absence is not a data gap — never a hardcoded per-kind field list.

    When several fields are unknown the first-listed element of
    `_REQUIRED_KNOWN_FIELDS` wins, so the reported field is deterministic
    rather than dependent on iteration accident."""
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
    """The magnitude an EV formula should actually read. The override is NOT
    additive — the game text names a value, not a bonus — so summing the
    two would double-count a value the card only ever grants once.

    **The fired override does not REPLACE the base: it COMPETES with it, and
    the game keeps the LARGER.** A skill carrying a `SlotOverride` registers
    TWO effect objects, both live at the same notes, each returning its own
    fixed magnitude, and the game's `CalcScoreUpRatio` combines them with
    `MathF.Max`. A live trace shows one skill's two magnitudes called an equal
    number of times.

    When the override is an UPGRADE, "replace" and "maximise" give the same
    number. They part company when an override is worth LESS than its base:
    the game keeps paying the base (that effect is still live) while a
    replacing model would pay the smaller one. So this returns `max`.

    When there is no override, or its gate did not fire, the base is
    returned unchanged — the override's gate never suppresses the whole
    skill, it only decides whether the second magnitude is in play at all."""
    skill = resolved.skill
    if (
        skill.override is not None
        and resolved.override_gate is not None
        and resolved.override_gate.fired
    ):
        return max(skill.override.magnitude, skill.magnitude)
    return skill.magnitude


@cache
def _finite_window_uptime(duration: float, interval: float) -> float:
    """The fraction of a reference run this ACTIVE skill's effect is up, GIVEN it rolls
    successfully -- **counted on the real cooldown lattice, not as a steady-state ratio.**

    The game's cooldown clock starts at measure 0 of
    the beatmap and the first activation check sits one whole cooldown in, so the checks
    are at `k x interval` for `k = 1 .. floor(T / interval)` -- there is no check at
    `t = 0`. A run is therefore not a steady state: it opens with a **dead zone** of one
    whole cooldown in which the skill cannot be up at all, and it closes with a final
    window the end of the run truncates. `min(1, duration / interval)` is the
    `T -> infinity` limit of this expression and has assumed both away.

        uptime = |UNION over k = 1 .. floor(T / interval) of [k x interval,
                  k x interval + duration)  INTERSECT  [0, T]| / T

    written as a UNION and not a sum, which makes self-overlap **structural rather
    than a clamp**: when `duration >= interval` the windows abut or overlap, and a sum
    would count the overlap twice. The union tiles from the first check to the end of
    the run and gives exactly `(T - interval) / T` -- the skill is up for everything
    except its own dead zone, which is the right answer and the one
    `min(1, duration / interval) = 1` was approximating. `_ev_at(30) == _ev_at(20)` at
    `interval = 20` therefore holds.

    **Why the limit is the wrong end to sit at.** Placing the same windows on the real
    time axis of every chart the game ships and weighting them per note exactly as
    `CalcScore` weights, the share of a run a single skill is actually up comes to about
    **83 % .. 95 % of `duration / interval`** across every distinct `(duration, interval)`
    pair `data/cards.json` carries, and never above it. The loss grows with `interval`,
    which is what a one-cooldown dead zone predicts and what no ratio can express: two
    skills with equal duty and different cooldowns are NOT equally up.
    `tools/recon/proxy/active_lattice.py` is the harness.

    ⚠ **This expression lands at about 76 % .. 93 % of the ratio, so on a single skill
    it slightly UNDER-shoots that truth (about 96 % of it on average).** Over a real
    ACTIVE pool the two remaining small biases run opposite ways and very nearly cancel:
    the pool's `E[max]` comes out within about +0.002 of the exact integral over real
    teams and every chart. Correcting the dependence as well lands WORSE (see
    `_score_up_pool`).

    The finite reference window is also supported by the deck screen: jointly with the
    server's board values the fit has a clear rms minimum, and the minimum falls exactly
    on the independently solved `REFERENCE_RUN_SECONDS = 128.8`.

    ⚠ **`floor(T / interval)` can be zero.** A skill whose cooldown exceeds the whole
    reference run never reaches its first check and is up for none of it, so this returns
    `0.0` rather than `duration / interval`, which would credit such a skill in full. No
    slot in `data/cards.json` is anywhere near it -- intervals run 15 .. 35 s against
    T = 128.8 -- so it is a guard, not a live path.

    `min(1, ...)` is kept as an above-100 % guard. Under the union it can no
    longer bind — a subset of `[0, T]` cannot measure more than `T` — which is the
    point: it is a guard against a future change rather than load-bearing
    arithmetic.

    **This is the run-average of exactly the windows `song._active_windows` places on a
    real time axis**, evaluated over a nominal run instead of a named chart, so the two
    scorers cannot drift apart on this term.
    `tests/test_finite_window_uptime.py` pins that identity.

    Cached because the argument pair is a property of the card and the holomem's board,
    not of the team: a `C(N,6)` walk re-asks the same few dozen questions millions of
    times."""
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
    """`ev = _finite_window_uptime(duration, interval) * activation_rate_model.apply(
    P(tier), team_rate_up) * _as_rate(magnitude)`. The union inside
    `_finite_window_uptime` prevents both uptime above 100% and double-counting a
    skill's self-overlap when it re-procs while already active."""
    slot = resolved.skill
    if slot.kind != "ACTIVE":
        raise ScoringError(
            f"_score_active requires an ACTIVE slot, got {slot.kind!r}",
            card_id=resolved.card_id,
            slot_kind=slot.kind,
        )
    # The Holomem Board's `Act.CD` nodes shorten the cooldown, and they DIVIDE
    # rather than multiply (35 s / 1.12 = 31.25 s). Only the board supplies
    # COOLDOWN — no `cool_time` rows exist outside the board — so it is keyed
    # by holomem, and an absent entry means zero.
    #
    # ⚠ Do NOT assume the same holds for probability. Cards DO alter
    # activation probability: some `LiveSpecialSkillLevel` rows carry
    # `live_active_skill_activation_probability_up_permil_up` at 350-550
    # permil. So probability has TWO sources with two different keys — the
    # board per HOLOMEM, the SPECIAL rider per CARD, since different skins of
    # one holomem carry different SPECIAL skills. Keying it by holomem alone
    # silently drops the rider.
    #
    # The real per-member values come from `Live/GetDeck` field 3
    # (`LiveDeckInGameEffect`), which states them per member as floats.
    # Populate this dict from that payload, never from the branch ceilings —
    # those are full-unlock maxima for one character and run ~4x high.
    #
    # It raises uptime, and the clamp still binds: `duration / interval` never
    # reaches 1 in the real data, but a shortened interval moves it closer, and
    # a skill can never be more than fully live.
    return _active_occupancy(resolved, cfg, team_rate_up) * _as_rate(
        float(_effective_magnitude(resolved))
    )


def _active_occupancy(resolved: ResolvedSkill, cfg: ScoringConfig, team_rate_up: float) -> float:
    """The fraction of the run this ACTIVE skill's effect is live:
    `_finite_window_uptime(duration, interval_effective) * P(activation)`. No
    magnitude term — that is the whole reason it is its own function.

    `_score_active` is exactly `_active_occupancy * magnitude_rate`, so the two
    can never drift apart. Both halves are needed separately because the ACTIVE
    pool is `E[max]` over magnitudes (`ExpectedMaximumActiveSkill`), and "the
    highest effect that is live wins" cannot be evaluated once magnitude and
    occupancy have been multiplied together.

    **The uptime is the finite-window count, not the ratio.** `min(1, duration /
    interval)` over-states this occupancy by 5 % to 16 % per skill and the pool's
    `E[max]` by **7 % to 16 %** per team against the exact note-weighted integral
    `song.py` computes over the shipped charts. `_finite_window_uptime`
    carries the derivation. The walk that combines these occupancies is
    discussed in `_score_up_pool`'s closing paragraph.

    See `_score_active` for the cooldown-axis commentary that governs
    `interval_effective`; it is stated once, there. A cooldown reduction moves
    the activation COUNT, an integer, and not just a ratio — which is why the
    board term and the finite window are identifiable only jointly."""
    slot = resolved.skill
    # The structural board supplies both uptime axes for this member's own
    # character, and it replaces nothing -- it ADDS to the hand-supplied
    # `active_cooldown_reduction`, which is the same account state expressed
    # another way. `board.uptime_for` is a per-holomem constant (the scope
    # property is argued there), so this costs a dict lookup in the hot
    # loop and needs neither the team nor the leader.
    #
    # ⚠ The CROSS-SOURCE aggregation is an inference. The board's own nodes
    # ADD within a board and `_fold_activation_rate_up` sums the skill sources
    # into `team_rate_up`, but how a board rate combines with a skill rate is
    # not known directly. They are the SAME effect family
    # (`live_active_skill_activation_probability_up`), so they are pooled into
    # one rate rather than applied as separate factors. If that is wrong it is
    # wrong by the difference between (1+a+b) and (1+a)(1+b), which at
    # realistic magnitudes is under half a point.
    board_rate, board_cooldown = (
        cfg.board.uptime_for(resolved.member) if cfg.board is not None else (0.0, 0.0)
    )
    reduction = cfg.active_cooldown_reduction.get(resolved.member, 0.0) + board_cooldown
    interval = float(slot.interval) / (1.0 + reduction)
    # "finite" (in-run, with the start-of-song dead zone) vs "steady" (the
    # deck-screen panel's song-independent reference). See ScoringConfig
    # .active_uptime_model for what separates them.
    if cfg.active_uptime_model == "steady":
        uptime = min(1.0, float(slot.duration) / interval) if interval > 0.0 else 0.0
    else:
        uptime = _finite_window_uptime(float(slot.duration), interval)
    base_prob = cfg.probability_tiers[slot.probability_tier]
    prob = cfg.activation_rate_model.apply(base_prob, team_rate_up + board_rate)
    return uptime * prob


def _score_special(resolved: ResolvedSkill, cfg: ScoringConfig, team_rate_up: float) -> float:
    """`ev = expected_fires_per_run * (duration / REFERENCE_RUN_SECONDS) *
    _as_rate(magnitude)`. SPECIAL is fired manually
    from a gauge, never on an activation roll: this reads
    neither `interval` nor `probability_tier`, both of which are
    structurally absent from a SPECIAL slot. `team_rate_up` is accepted
    but unread so every `EV_FUNCTIONS` entry shares one call signature.
    Returns an average per-run rate, like every EV function."""
    slot = resolved.skill
    if slot.kind != "SPECIAL":
        raise ScoringError(
            f"_score_special requires a SPECIAL slot, got {slot.kind!r}",
            card_id=resolved.card_id,
            slot_kind=slot.kind,
        )
    # This function scores only the SPECIAL's OWN score contribution
    # (`_special_occupancy` × `_magnitude_rate`). A SPECIAL slot's
    # `activation_rate_up` rider (e.g. "For 10s, ... When
    # LIFE is 1000 or higher, Skill Activation Rate UP 55%.") is a DIFFERENT
    # quantity — it lifts every ACTIVE proc chance team-wide — and is folded
    # into `team_rate_up` by `_fold_rider_activation_rate_up`, which reuses this
    # function's `_special_occupancy` uptime term with the rider's magnitude in
    # place of the SPECIAL's own. It is deliberately NOT added here (that would
    # double-count it into this SPECIAL's score line). Sizing, for reference:
    # 35-55% for ~10s once per run is roughly +4 percentage points on every
    # ACTIVE proc chance, an ~11-16% relative swing on ACTIVE uptime.
    return _special_occupancy(resolved, cfg, team_rate_up) * _magnitude_rate(resolved)


def _special_occupancy(resolved: ResolvedSkill, cfg: ScoringConfig, team_rate_up: float) -> float:
    """`expected_fires_per_run * (duration / REFERENCE_RUN_SECONDS)` — the
    fraction of the run a SPECIAL's effect is live, with no magnitude term.

    Split out for the same reason `_active_occupancy` is: a SPECIAL Score UP
    competes in the SPECIAL bucket's maximum — of `ScoreUpBoostByRatio`, i.e.
    inside `B` (`_score_up_implements_boost`) — and a maximum cannot be taken
    over magnitudes that have already been multiplied by their occupancy.

    ⚠ This occupancy is a **fraction of the run**, and under the collapse it is
    also the one place the un-modelled overlap enters: `B`'s time-average is
    multiplied by the ACTIVE maximum's time-average as if the two were
    independent. They are not. See `_score_up_pool`'s closing paragraph.

    ⚠ Do NOT restore `REFERENCE_RUN_SECONDS` to 90.0. `live_reward_up_music_
    length_bonus_min_playing_seconds = 90` scales REWARDS, not score, and has
    zero client-side consumers. 128.8 is solved
    from ten captured decks (see `scoring_config`)."""
    del team_rate_up  # SPECIAL is gauge-fired, never on an activation roll
    return cfg.expected_fires_per_run * (
        float(resolved.skill.duration) / REFERENCE_RUN_SECONDS
    )


def _always_on_occupancy(
    resolved: ResolvedSkill, cfg: ScoringConfig, team_rate_up: float
) -> float:
    """Occupancy `1.0`: a PASSIVE or COSTUME effect is live for the whole run
    Takes the full `(resolved, cfg, team_rate_up)` signature so
    every `OCCUPANCY_FUNCTIONS` entry shares one call shape, and reads none of
    them."""
    del resolved, cfg, team_rate_up
    return 1.0


def _magnitude_rate(resolved: ResolvedSkill) -> float:
    """The skill's magnitude as a fraction, override applied. The game's
    `ScoreUpByRatio` / `ScoreUpBoostByRatio`, which are
    both `FloatFromPermil(this.Value)` — a magnitude with no occupancy term in
    it, which is exactly why it can be maximised over."""
    return _as_rate(float(_effective_magnitude(resolved)))


def _passive_rate(resolved: ResolvedSkill) -> float:
    """The shared PASSIVE/COSTUME arithmetic: full
    magnitude-as-rate, always. Reads no duration, no interval, no
    probability tier — a PASSIVE (or COSTUME) skill is always on and is
    gated only by its condition, which `_resolve_skill` has already
    evaluated before dispatch, never by an activation roll.
    Takes the `ResolvedSkill` (not a bare `SkillSlot`) so it can see the
    slot's override gate through `_effective_magnitude`."""
    # Occupancy is `_always_on_occupancy`'s 1.0 and is elided rather than
    # written out: `1.0 * x` is exactly `x` in IEEE-754, so spelling it would
    # add a term without adding a fact.
    return _magnitude_rate(resolved)


def _score_passive(resolved: ResolvedSkill, cfg: ScoringConfig, team_rate_up: float) -> float:
    """PASSIVE: `ev = _as_rate(magnitude)` whenever dispatch reaches here
    at all — `_resolve_skill` never dispatches an ungated-but-not-fired
    PASSIVE skill to this function in the first place."""
    slot = resolved.skill
    if slot.kind != "PASSIVE":
        raise ScoringError(
            f"_score_passive requires a PASSIVE slot, got {slot.kind!r}",
            card_id=resolved.card_id,
            slot_kind=slot.kind,
        )
    return _passive_rate(resolved)


def _score_costume(resolved: ResolvedSkill, cfg: ScoringConfig, team_rate_up: float) -> float:
    """COSTUME: delegates to the shared `_passive_rate` arithmetic
    directly, never through `_score_passive`'s own guard, which would
    reject a COSTUME slot. A distinct dispatch entry exists precisely so
    the entry guards stay single-kind and strict."""
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

# The occupancy half of every EV function, dispatched on the same keys. Each
# `EV_FUNCTIONS[k]` is exactly `OCCUPANCY_FUNCTIONS[k] * _magnitude_rate` — one
# arithmetic definition, two readers, so they cannot drift.
#
# The pool builders need the two halves apart because the game maximises over
# magnitudes and a maximum is not recoverable from the
# product. The EV functions keep their per-kind entry guards; these
# do not, because they are only ever reached through a caller that has already
# established the kind.
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

# Import-time structural guarantee: every SKILL_SLOT_KINDS member
# plus COSTUME_SLOT_KIND must have a dispatch entry. Adding a slot kind to
# schema.py without adding an EV function fails here, at import, rather
# than as a KeyError deep inside a solver run.
assert set(EV_FUNCTIONS) == set(SKILL_SLOT_KINDS) | {COSTUME_SLOT_KIND}, (
    "EV_FUNCTIONS must cover every SKILL_SLOT_KINDS member plus "
    "COSTUME_SLOT_KIND — an unmapped slot kind must fail at import, never "
    "at score time"
)


def _fold_activation_rate_up(resolved_skills: tuple[ResolvedSkill, ...], cfg: ScoringConfig) -> float:
    """Pass 1: fold every *fired*, fully-known
    `activation_rate_up` skill into a single team-wide rate modifier, order
    independent — never through `stacking_model.combine`, whose scope is
    the score pool only.

    This fold applies the ACTIVE formula, so every
    `activation_rate_up` skill it folds must be an ACTIVE-kind skill;
    `domain/schema.py`'s `validate_skill_slot` already rejects this family
    on a non-ACTIVE slot at load time, so the guard below is
    defence in depth for any `ResolvedSkill` that bypassed that
    validation — never a dispatch. Do not change this guard into a
    `EV_FUNCTIONS[slot.kind]` dispatch: routing a PASSIVE or SPECIAL
    rate-up through its own formula would return a plausible number for a
    record the schema rejects, and fabricating a value is strictly
    worse than raising. The guard runs before the gate/known-field skips
    so an unfired gate cannot mask a structurally invalid record.

    This loop reads `resolved.family`, which `gates._resolve_one_skill` sets
    from the slot's PRINCIPAL `skill.family` — never a rider's family. A
    SPECIAL slot's `activation_rate_up` rider therefore never enters this loop
    at all, by construction, not by an extra check; it needs rider-shaped
    arithmetic and has its own fold, `_fold_rider_activation_rate_up`.

    This fold never reads `cfg.family_weights` — `family_weights
    ["activation_rate_up"]` is validated to be exactly `0.0` because this
    family stays out of every score pool."""
    total = 0.0
    for resolved in resolved_skills:
        if resolved.family != "activation_rate_up":
            continue
        if resolved.skill.kind != FAMILY_REQUIRES_SLOT_KIND["activation_rate_up"]:
            raise ScoringError(
                "_fold_activation_rate_up applies the ACTIVE formula per "
                "D-17; validate_skill_slot rejects this record at load "
                "time, so reaching this line means the input bypassed "
                "load-time validation",
                card_id=resolved.card_id,
                slot_kind=resolved.skill.kind,
            )
        if resolved.gate is not None and not resolved.gate.fired:
            continue
        if not _has_all_known_fields(resolved):
            continue
        total += _score_active(resolved, cfg, 0.0)
    return total


def _fold_rider_activation_rate_up(
    resolved_skills: tuple[ResolvedSkill, ...], cfg: ScoringConfig
) -> float:
    """Pass 1b: fold every *fired* `activation_rate_up` RIDER into the
    same team-wide rate modifier `_fold_activation_rate_up` builds.

    `_fold_activation_rate_up` reads `resolved.family`, which `gates` sets from
    the slot's PRINCIPAL family, so a SPECIAL slot's `activation_rate_up` rider
    never enters it. This is that rider's own fold — kept a separate
    function, not a wider version of that ACTIVE-only guard, because it needs
    rider-shaped arithmetic. The rider is bounded by its host SPECIAL's own
    duration and fires once per run, gated by the rider's OWN condition
    (`resolved.rider_gate`, e.g. LIFE>=1000), so its contribution is:

        expected_fires_per_run * (duration / REFERENCE_RUN_SECONDS)
            * _as_rate(rider.magnitude)

    i.e. `_special_occupancy`'s uptime term with the rider's magnitude in place of
    the SPECIAL's own. Restricted to SPECIAL slots because that uptime term (once
    per run, bounded by the host's duration) is SPECIAL-specific — the family
    lives only on SPECIAL in the data, and an `activation_rate_up` rider on any
    other slot kind would need its own arithmetic, so it is skipped rather than
    mis-scored. Skips a rider whose magnitude or condition is `UNKNOWN`, or whose
    host duration is `UNKNOWN` (the two inputs the formula reads), and a rider
    whose own gate did not fire — the same known-fields/gate discipline the
    principal fold applies. Reads no target: `team_rate_up` is team-wide by
    construction."""
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
    """The one shared per-skill core, called identically by every scoring
    entry point. Returns `(rate, state, missing_field)`
    so a zero caused by a gate that did not fire, a zero caused by
    an `UNKNOWN` field, and a genuine zero contribution stay distinguishable
    — never coerced through `float(UNKNOWN)`, and never confused
    with each other by a caller that only wants the rate. Dispatches
    through `EV_FUNCTIONS[resolved.skill.kind]`, never `resolved.slot_kind`
    (the lowercase `costume` string `Team.effective_skill_instances()`
    emits, which is not an `EV_FUNCTIONS` key)."""
    slot = resolved.skill
    pool = _pool_for(resolved)
    # An activation-rate skill structurally never receives the team
    # modifier itself — its own contribution was already folded
    # into `team_rate_up` by `_fold_activation_rate_up`.
    effective_team_rate_up = 0.0 if pool == "none" else team_rate_up

    if resolved.gate is not None and not resolved.gate.fired:
        return 0.0, "gate_not_met", None

    missing_field = _first_missing_field(resolved)
    if missing_field is not None:
        return 0.0, "data_missing", missing_field

    rate = EV_FUNCTIONS[slot.kind](resolved, cfg, effective_team_rate_up)
    return rate, "applied", None


# The two buckets `CalcScoreUpRatio` partitions its effect list into.
#
# The game's `ActiveSkillEffectDataCollection::CalcScoreUpRatio`
# @ rva `0x5efdb90` keeps two accumulators, folds each effect into one of them
# with `Math::Max`, and returns their sum (`addss xmm7, xmm8`). The
# discriminator is not a virtual call but a plain byte —
# `LiveSkillEffectData.isSpecialSkill` at unboxed `+0x18`, read as
# `cmp byte [rsp+0x38], 0` at `0x5efddf1`. **The two buckets are ACTIVE and
# SPECIAL.**
#
# ⚠ **The consequence for the solver is NOT "a strong ACTIVE plus a strong
# SPECIAL beats two strong of either kind".** The SPECIAL bucket holds only
# `ScoreUpBoostSkillEffect`s, so it contributes to `B` and never to the ratio:
# **a SPECIAL multiplies the ACTIVE maximum and is worth nothing without one.**
# See `_score_up_implements_boost`.
SCORE_BUCKET_ACTIVE = "ACTIVE"
SCORE_BUCKET_SPECIAL = "SPECIAL"


def _score_bucket(resolved: ResolvedSkill) -> str:
    """Which `CalcScoreUpRatio` accumulator this effect folds into.

    A SPECIAL slot's effects carry `isSpecialSkill == true` and everything else
    `false`. This is a strong inference rather than a traced fact: the field's
    two writers are generic instantiations that take the flag as a parameter.
    Nothing else in the game's vocabulary would make a PASSIVE or an outfit
    skill "special", so the ACTIVE bucket takes them.

    This function is the whole partition. Nothing else in this module reads a
    bucket key."""
    return (
        SCORE_BUCKET_SPECIAL
        if resolved.skill.kind == "SPECIAL"
        else SCORE_BUCKET_ACTIVE
    )


def _score_up_implements_boost(resolved: ResolvedSkill) -> bool:
    """**The SPECIAL collapse.** Which of the two `ScoreUp*ByRatio` virtuals a
    Score UP actually implements — `True` for `ScoreUpBoostSkillEffect` (it
    implements `ScoreUpBoostByRatio`, slot `+0x1d8`, and contributes to `B`),
    `False` for `ScoreUpSkillEffect` (it implements `ScoreUpByRatio`, slot
    `+0x1c8`, and contributes to the ratio).

    **A SPECIAL Score UP is a `ScoreUpBoostSkillEffect`.** It is a *boost*, not
    a ratio, so `max_SPECIAL v(e)` in `_score_up_pool`'s formula is
    structurally zero and the whole expression collapses to

        ratio = (1 + SkillEffectUpPermilUp/1000 + B) x max_ACTIVE ScoreUpByRatio

    **The consequence a solver must not miss: a SPECIAL Score UP with no ACTIVE
    Score UP in the team contributes exactly nothing.** It is a multiplier on
    something that is not there. `tests/test_special_collapse.py` pins that.

    Three independent pieces of evidence from the game binary and data:

    1. A live in-process dump, taken after the one-time class-init guards had
       run, resolves all ten `LiveActiveSkillEffectData::Create` effect types
       to their runtime classes. Type 5 is
       `Vision.LiveCore.ScoreUpBoostSkillEffect`; type 1 (the control) is
       `ScoreUpSkillEffect`.
    2. The two classes' vtables: `ScoreUpBoostSkillEffect` slot `+0x1c8` holds
       `LiveActiveSkillEffectBase::ScoreUpByRatio` — i.e. **not overridden**,
       the shared base stub, whose four bytes at rva `0x144570` are
       `0f 57 c0 c3` = `xorps xmm0,xmm0 ; ret` = a hard `0.0f`. Slot `+0x1d8`
       *is* overridden, to `ScoreUpBoostSkillEffect::ScoreUpBoostByRatio` at
       rva `0x5f06840`. `ScoreUpSkillEffect` is the exact mirror image.
    3. The master data says every SPECIAL is that type. Every
       `LiveSpecialSkillLevel` row carries field 3 =
       `live_special_skill_effect-score_up_effect_up_permil_up-*`, and every one
       of those rows in `LiveActiveSkillEffect` has `type == 5`. The control:
       the `LiveActiveSkillLevel` score rows carry
       `live_active_skill_effect-score_up_permil_up-*`, `type == 1` (the few
       others are skill-tree `judgement_enhance`/`life_recovery` rows, neither a
       score type). **No ACTIVE skill level references a type-5 effect and no
       SPECIAL skill level references a type-1 one** — the two are disjoint.

    The size is pinned separately: solving the reference duration from ten
    captured deck screens under both readings, the multiplier reading
    (`Special line = Active line x SUM(mag x dur) / T`) gives `T` constant to
    about 6 %; the addend reading spreads about 90 %.

    Keyed on `skill.kind`, which is the only axis the schema carries. That is
    exact for the real master data (the disjointness above), and it is where the
    reading would have to change if a SPECIAL slot ever carried a plain
    `score_up_permil_up`. None does."""
    return resolved.skill.kind == SCORE_BUCKET_SPECIAL


def _windows_are_mutually_exclusive(resolved: ResolvedSkill) -> bool:
    """**The overlap half of the SPECIAL collapse** — `ActiveTerm`'s `exclusive`
    flag. `True` when this skill's live window cannot overlap the window of any
    other exclusive term in its own bucket, so the bucket's `E[max]` is the plain
    sum of `magnitude x occupancy` rather than the independence walk.

    A SPECIAL is not probabilistic and does not roll on a cooldown: its fire
    time is written into the chart, channel `#mmm0B`, **one cue per deck slot**,
    and slot `i` fires the `i`-th cue in time. A team has five members, one
    SPECIAL each, so each SPECIAL fires at its own cue and exactly once. Whether
    two of them are ever live together is therefore a pure question of chart
    geometry: cue gap against skill duration.

    Over every chart the game ships that carries five ascending cues (all but
    the tutorial `m9999` difficulties):

        shortest gap anywhere              9.88 s
        SPECIAL durations, all cards       7.0 .. 14.0 s
        gaps shorter than 7.0 s            0 %
        gaps shorter than 14.0 s           about 0.6 %

    So **no pair of adjacent cues can be bridged by the shortest SPECIAL in the
    game, and well under 1 % of them by the longest.** Non-adjacent cues cannot
    overlap at all, since two consecutive gaps sum to at least 19.76 s against a
    14.0 s ceiling on duration. The disjointness is a property of the shipped
    chart corpus, not an assumption.

    What it is worth: `ExpectedMaximumActiveSkill`'s independence walk
    discounts each SPECIAL by the chance a bigger one is already live —
    overlaps that the chart says never happen — and so **understates `B` by
    roughly 10 % to 17 %** on captured decks. Against the deck screen's own
    Special-Skill line the two readings sit at **rms about 3.9 pp
    (independent) and 1.5 pp (exclusive)**.

    ⚠ **This is true of the RUN-AVERAGE occupancy only.** It says "over a whole
    run, these five windows do not collide", which is what
    `_special_occupancy`'s `fires x duration / REFERENCE_RUN_SECONDS` measures.
    It is emphatically NOT a licence to sum SPECIAL magnitudes at an instant:
    `song.py` places the real cue windows on a real time axis, sees the rare
    chart-and-team combinations where two of them genuinely do overlap, and
    passes `exclusive=False` so the maximum is taken for real. A corpus-level
    fact must not overrule ground truth in the module that holds it.

    Keyed on `skill.kind`, exactly like `_score_up_implements_boost` and for the
    same reason: it is the only axis the schema carries, and it is exact for the
    real master data. An ACTIVE rolls on a cooldown lattice against a probability
    and two ACTIVEs collide constantly, so the ACTIVE bucket is never
    exclusive."""
    return resolved.skill.kind == SCORE_BUCKET_SPECIAL


def _is_score_up_effect(resolved: ResolvedSkill) -> bool:
    """True for the skills `CalcScoreUpRatio` maximises: an ACTIVE or SPECIAL
    slot whose effect raises SCORE. Which of the two maxima it joins is
    `_score_up_implements_boost`'s question, not this one's.

    **The kind restriction is deliberate and is the conservative reading.** The
    game's partition names exactly two buckets, ACTIVE and SPECIAL, and
    `isSpecialSkill` is a bool with no third state -- so whether a PASSIVE or an
    outfit Score UP is in that collection at all is not known. Sweeping them
    into the ACTIVE bucket would maximise them against the ACTIVEs; leaving them
    out keeps them summed. Neither reading is confirmed, so the one that changes
    nothing wins.

    Score Support is treated the other way round on purpose -- a COSTUME
    support DOES reach the multiplier regardless of slot kind, because that is
    what captured deck screens show. Each half follows its own evidence rather
    than one tidy rule covering both.

    Deliberately narrower than `pool == "score"`. `POOL_BY_EFFECT_TYPE` also
    routes `life` and `judgment` there, and those are *different effect
    classes* in the game (`LifeRecoverySkillEffect`,
    `JudgementEnhanceSkillEffect`) whose `ScoreUpByRatio` is the shared base
    stub returning a hard `0.0f` (`xorps xmm0,xmm0 ; ret` at `0x144570`). They
    neither join a maximum nor lower one, so they stay individual summed rates
    with their own aggregation, which is not known. Both carry a `0.0`
    `family_weights` default, so this costs nothing by default; it matters if
    one is set nonzero, when silently maximising a life-recovery effect against
    a score effect would be a wrong answer with no symptom."""
    return (
        resolved.skill.kind in (SCORE_BUCKET_ACTIVE, SCORE_BUCKET_SPECIAL)
        and resolved.skill.effect_type == "score"
    )


def _active_interval(resolved: ResolvedSkill) -> int:
    """The ACTIVE cooldown period as `ActiveTerm`'s lattice group key: `int(
    slot.interval)` when the slot has an interval axis, `0` when it does not.

    ACTIVE slots always carry a real numeric `interval` (it is the same field
    `_active_occupancy` reads as `float(slot.interval)`). Slots with no cooldown
    axis leave it absent, and it reaches here in more than one encoding: the
    `UNKNOWN` sentinel (SPECIAL, untranscribed), a raw `None` (a COSTUME rider
    slot copies the outfit's `None` interval verbatim), or simply not-a-number.
    Any non-numeric value maps to `0` — the per-bucket "no lattice" group of
    `ExpectedMaximumActiveSkillByInterval`, which changes nothing for it. Only
    `max_lattice` reads the result; `max`/`sum` ignore the element entirely."""
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
    """One bucket term: `(bucket, weighted_magnitude_rate, occupancy, interval,
    exclusive)`. The single builder both drivers call, for the same parity
    reason
    `_score_up_pool` itself is shared. Used for both the ratio terms and the
    Score Support boost terms -- the game builds them the same way, from the
    same list, with the same partition (`CalcScoreUpBoostByRatioBase` is
    inlined verbatim into `CalcScoreUpRatio`).

    The family weight multiplies the MAGNITUDE, not the finished rate -- under
    a maximum, where a term sits in the ordering has to reflect the magnitude
    that was actually applied. `weight * magnitude * occupancy` is the same
    product the summed model contributed, so nothing about the weight's meaning
    changes.

    A skill that is not `applied` yields occupancy `0.0` as well as magnitude
    `0.0`. Both are needed: zero magnitude alone would still consume the
    `E[max]` walk's remaining probability, letting a gate that did not fire
    suppress a skill that did.

    `exclusive` is `_windows_are_mutually_exclusive`'s answer and is carried even
    on a zero term, so a suppressed SPECIAL still sorts and discounts like a
    SPECIAL rather than silently becoming an independent zero."""
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
    """`score_card_set`'s cache entry: the bucket term for a Score UP or a
    Score Support, `None` for anything else.

    Exists so `score_card_set`'s two cache writers state the routing rule ONCE
    rather than each spelling out the same `pool`/`_is_score_up_effect`/
    empty-target conditions. `_score_team` does not call it — it routes inside
    its own `if pool ==` chain, where the same three conditions are already
    open — so the rule is written twice in total and the parity tests pin that
    the two agree. Collapsing them further would mean `_score_team` computing a
    `target_ids` it does not otherwise need on the hot path."""
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
                _windows_are_mutually_exclusive(resolved), 0.0, 0.0,  # no lattice
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
    ratio_model: ActiveStackingModel | None = None,
) -> float:
    """`SkillScoreUpRatio` -- the team's whole skill-effect Score UP as ONE rate.

    The game's `CalcScoreUpRatio` computes:

        B     = max_ACTIVE ScoreUpBoostByRatio + max_SPECIAL ScoreUpBoostByRatio
        v(e)  = (1 + SkillEffectUpPermilUp/1000 + B) x ScoreUpByRatio(e)
        ratio = max_ACTIVE v(e) + max_SPECIAL v(e)

    Both maxima are computed by the *same* rule, which is why
    `cfg.active_stacking_model.combine` is called twice here rather than the
    boost being combined some other way: the game inlines
    `CalcScoreUpBoostByRatioBase` verbatim into `CalcScoreUpRatio` -- same list,
    same partition, same `Math::Max`, same trailing `addss`.

    **Per-member targeting.** A member PASSIVE `score_support` with
    `{kind:type, count:2}` boosts the ACTIVEs of the TWO targeted members only
    (the deck panel's per-card `active_effect_up` shows it), and that boost is
    the SUM of the supports whose target selects the card. So the multiplier is
    per ACTIVE carrier:

        v(e_i) = (1 + SkillEffectUpPermilUp/1000 + B_shared + T_i) x ScoreUpByRatio(e_i)

    where `B_shared` is the max-per-bucket combine of the supports whose target
    is team-wide (`all`: member PASSIVE `all`, leader COSTUME, SPECIAL) and
    `T_i = sum(magnitude x occupancy)` of the count-targeted supports whose
    RESOLVED target set contains the owner of `e_i`. `ratio_owners[k]` is the
    card id owning `ratio_entries[k]`; `boost_targets[k]` is `None` for a
    team-wide support and the resolved frozenset for a targeted one. Both
    default to `None` (= every support is team-wide), so a caller with no
    targeted support pays and changes nothing. A support whose resolved target
    set is EMPTY never enters the collection and contributes nothing.

    **The general form above is kept verbatim, and the collapse happens in the
    data rather than in this arithmetic.** `max_SPECIAL v(e)` is structurally
    zero because every SPECIAL Score UP is a `ScoreUpBoostSkillEffect`, whose
    `ScoreUpByRatio` is the base stub returning `0.0f`
    (`_score_up_implements_boost`). `_score_team` and `score_card_set`
    therefore hand SPECIAL Score UPs to `boost_entries`, and `ratio_entries`
    carries only ACTIVE-bucket terms. What this function computes is
    consequently

        ratio = (1 + B) x max_ACTIVE ScoreUpByRatio

    **so a SPECIAL Score UP with no ACTIVE Score UP under it is worth exactly
    nothing.** Writing the collapse into the routing rather than into this
    formula is deliberate: if a SPECIAL slot ever carries a type-1 effect this
    function is already correct.

    - **Score Support multiplies rather than adds** (`mulss xmm6, xmm0`). `B`
      multiplies every effect that is in the ratio, which after the collapse
      means the ACTIVE bucket.
    - **Combiner.** Multiple supports combine as a max-of-two-maxima, never
      through `STACKING_MODELS`. Two 30 % supports on ACTIVE skills are worth
      one 30 %, not 60 %.

    **Overlap.** The game's collapse is *instantaneous*: `B` is the max over the
    SPECIALs live at that tick and `A` the max over the ACTIVEs live at that
    tick, and the run integrates their product. This function multiplies a
    time-averaged `B` by a time-averaged `A`, so it differs from the truth by
    `Cov(B, A)`. That gap has parts of different size and kind:

    **1. Within `B` -- real, and implemented.** The five `#mmm0B` cues make
    SPECIAL collisions essentially impossible (see
    `_windows_are_mutually_exclusive`), so those terms carry `exclusive=True`
    and the SPECIAL bucket's maximum is a plain sum. Against the deck screen's
    own Special-Skill line this takes the rms error from about 3.9 pp to about
    1.5 pp.

    **2. Between `B` and `A` -- zero in steady state, and deliberately not
    applied.** Walking the real time axis over every chart, note-weighted as
    `CalcScore` weights:

        rho = E_t[S(t) A(t)] / (E_t[S(t)] E_t[A(t)])

        whole song, lattice origin at measure 0   rho = 1.0747  [1.019 .. 1.107]
        from t = 40 s (past every first check)    rho = 0.9976  +/- 0.0087
        whole song, lattice origin at t = 13.25 s rho = 1.0255

    **In steady state there is no covariance at all** -- the chart's cue times
    and the ACTIVE cooldown lattice are uncorrelated to within about 1 %. The
    whole-song excess is the lattice's start-of-song DEAD ZONE, where no ACTIVE
    has reached its first check at `k = 1` yet and the early cues sit over a
    depressed `A`; most of it disappears if the lattice origin is the countdown
    rather than measure 0, and which origin is right is not firmly established.

    **3. The `E[A]` marginal.** A steady-state duty cycle `min(1,
    duration/interval) x P` over-states the exact note-weighted `E[A]` by 7 % to
    16 % per team. The over-statement is entirely in the MARGINAL, not in the
    dependence (`tools/recon/proxy/active_lattice.py`, a real card pool x every
    chart):

        replace only the marginal (finite window, walk untouched)
            E[A] rms 0.0474 -> 0.0194, mean bias +0.0427 -> -0.0028
        replace only the dependence (exact lattice co-occupancy, marginal untouched)
            E[A] rms 0.0474 -> 0.0449, mean bias +0.0427 -> +0.0400
        replace both
            E[A] rms 0.0474 -> 0.0249, mean bias +0.0427 -> -0.0168   (OVER-corrects)

    Lattice synchronisation is real inside ONE run and it **averages out across
    the corpus**: charts run 88 .. 165 s, so a skill's lattice phase at any given
    note is spread across songs, and the independence walk is right on average.
    Correcting it as well double-counts. So `ExpectedMaximumActiveSkill` is
    untouched -- the fix is `_finite_window_uptime`, one function, upstream of
    the walk.

    **The largest remaining error is NOT this module's to fix.** With `E[A]`
    unbiased, the residual on the TOTAL is `B`'s own denominator:
    `_special_occupancy` divides by `REFERENCE_RUN_SECONDS = 128.8`, while a
    shipped chart's note-weighted denominator averages about **116 s** (cues
    land in note-dense bars), so `B` UNDER-states the exact note-weighted
    `E[B]` by a consistent 10 % to 17 %. 128.8 is the value solved from captured
    deck screens; the deck screen and a real song do not share a time base, and
    reconciling them is a measurement to make rather than a constant to tune.

    **`SkillEffectUpPermilUp`** -- the float that shares this multiplier with
    `B` -- is supplied by the BOARD (`BoardContribution.skill_effect_up`).
    Every board effect carries its own explicit `scope`, and
    `board._recipients` reads it, so the board's term is unambiguous.

    ⚠ It enters HERE, inside the parenthesis, and nowhere else. It is not a
    summand of `combined_score_rate` and not a second board factor: a team with
    no ACTIVE Score UP gains exactly nothing from it, which is the assertion
    that separates this reading from an additive one.

    `ratio_entries` and `boost_entries` are both
    `(bucket, weighted_magnitude_rate, occupancy, interval, exclusive)` in
    `Team.effective_skill_instances()` order. Non-score-up score-pool
    contributions never come here; they stay individual rates combined by
    `stacking_model` in the caller.

    Both scoring drivers call THIS function rather than each re-implementing
    the arithmetic, so bit-identical parity holds by construction rather than
    by two copies staying in step."""
    # `ratio_model` is the RATIO population's combiner and defaults to the
    # config's, so the ordinary call is unchanged down to the object identity.
    # It exists for `song._ratio_at`, which alone holds terms whose `occupancy`
    # is an instantaneous probability and can therefore ask a different question
    # of them (an exclusive co-firing hypothesis). `B` is NOT affected:
    # the boost population keeps the max-of-two-maxima either way,
    # because the hypothesis is about ACTIVEs sharing one activation roll, not
    # about how a Score Support applies.
    combine_ratio = (ratio_model or cfg.active_stacking_model).combine
    # Only the magnitude (index 1) is scaled; `occupancy`, `interval`, `exclusive`
    # and the trailing `(duration, probability)` pair pass via `*rest`.
    if boost_targets is None or all(t is None for t in boost_targets):
        multiplier = (
            1.0 + skill_effect_up + cfg.active_stacking_model.combine(boost_entries)
        )
        return combine_ratio(
            [
                (bucket, magnitude_rate * multiplier, *rest)
                for bucket, magnitude_rate, *rest in ratio_entries
            ]
        )
    assert ratio_owners is not None and len(ratio_owners) == len(ratio_entries)
    assert len(boost_targets) == len(boost_entries)
    shared = 1.0 + skill_effect_up + cfg.active_stacking_model.combine(
        [e for e, t in zip(boost_entries, boost_targets) if t is None]
    )
    targeted = [(e, t) for e, t in zip(boost_entries, boost_targets) if t is not None]
    scaled: list[tuple] = []
    for (bucket, magnitude_rate, *rest), owner in zip(ratio_entries, ratio_owners):
        multiplier = shared + sum(e[1] * e[2] for e, t in targeted if owner in t)
        scaled.append((bucket, magnitude_rate * multiplier, *rest))
    return combine_ratio(scaled)


def _support_targets(
    resolved: ResolvedSkill, target_ids: frozenset[str]
) -> frozenset[str] | None:
    """`_score_up_pool`'s `boost_targets` element for one support: `None`
    when the support is team-wide (structural `None` or `{kind: all}`), the
    RESOLVED recipient set otherwise. Shared by every driver so the
    "team-wide vs targeted" rule is written once."""
    target = resolved.skill.target
    if not isinstance(target, EffectTarget) or target.kind == TEAM_WIDE_TARGET_KIND:
        return None
    return target_ids


# ---------------------------------------------------------------------------
# Deck slot order
# ---------------------------------------------------------------------------
#
# The game's `Vision.LiveCore!LiveSkillUtility::IsSatisfyEffect
# Target{Attribute,Character,CharacterGrouping}Condition` @ rva `0x5f03d70` /
# `0x5f03f50` / `0x5f04140` each walk the deck's card list **in slot order**,
# count how many members match the effect's target predicate up to *and
# including* the candidate, and return `count <= targetCount`
# (`cmp edi,[rsi+0x24] / setg al / xor al,1`). `targetCount == 0` returns true
# before the loop — "no limit".
#
# **In one sentence: a count-limited effect reaches a member iff that member is
# among the first `count` members, in deck-slot order, that match the target.**
#
# It matters on real data: most cards in `data/cards.json` carry a
# count-limited PASSIVE target (`type`/2, `type`/3, `generation`/2).
#
# ## Where order does and does not bite in THIS model
#
# The stat pool is the **only** order-dependent term here, and that is provable
# rather than assumed:
#
# - **Score pool.** `_bucket_entry` reads no target at all for a `pool ==
#   "score"` skill, so a Score UP's bucket term cannot move with slot order.
# - **Score Support.** A support that reaches *nobody* never enters the effect
#   collection; a team-wide one boosts every ACTIVE; a count-targeted one
#   boosts only its resolved recipients' ACTIVEs (`_score_up_pool`).
#   The recipient set is `targets.resolve_target_card_ids`' top-`count` by
#   TOTAL PARAMETER, tie broken by list order, so it is
#   the same set for every ordering of a fixed member set — the per-member `B`
#   is therefore still order-invariant.
# - **`team_rate_up`.** `_fold_activation_rate_up` reads no target.
# - **The board factor.** `_board_score_rate` sums over the member set.
# - **The SPECIAL slot cue** — the *bigger* of the two real
#   order terms in the game — is **structurally absent from this module and
#   deliberately stays absent.** It is a per-note time integral over the chart's
#   five `#mmm0B` cue windows; `_special_occupancy` is `expected_fires_per_run *
#   duration / REFERENCE_RUN_SECONDS`, which has no chart, no cue times and no
#   time axis, and a *ranking* quantity takes no song parameter. It belongs to
#   the absolute-score-on-a-named-song entry point (`song.py`), so this
#   module's SPECIAL term is order-invariant **by construction**, and every
#   ranking it produces omits a cue term worth roughly 1.4 % to 3.1 % of score
#   at the median.
#
# ### The SPECIAL overlap rule is not a per-song term
#
# `_windows_are_mutually_exclusive` is justified by a fact about **charts** — the
# `#mmm0B` cue geometry of every chart the game ships — and it changes what
# `score_fast`/`score_card_set` return, so the question has to be asked
# explicitly rather than assumed away.
#
# A *per-song scalar cannot reorder two teams*, because `CalcScore` divides by
# `NotesPerfectCoefficientSum`, so length cancels and a song-dependent
# multiplier hits every team identically. The overlap rule is neither a scalar
# nor per-song:
#
# - **It takes no song parameter.** No chart is read, no `music_id` reaches
#   this file. `score_song` remains the second entry point.
# - **The rule is uniform over the corpus, not fitted to a song.** It is not
#   "on chart X the windows are disjoint"; it is "on every shipped chart the
#   shortest cue gap (9.88 s) exceeds the shortest SPECIAL, and all but a
#   fraction of a percent of adjacent pairs exceed the longest". A statement
#   true of every song is not a per-song statement. A chart on which it were
#   false would be scored by `song.py`, which is why that module passes
#   `exclusive=False`.
# - **It discriminates between TEAMS, which is the whole point.** It changes how
#   a team's own SPECIALs combine with each other, so a team with five mid
#   SPECIALs beats one with a single big SPECIAL where the independence
#   discount would flatten them. That is a team property, not a song one.
#
# ## The objective, and why it is a maximum
#
# The player chooses the slot order, so the achievable score of a member *set*
# is the **maximum over its 120 orderings**, not the score at some arbitrary
# one. That is the objective `"best_order"` implements.
#
# ## Slot weights are not separable for this term
#
# Precomputing `w[card][slot]` and solving a 5x5 linear assignment is sound for
# the SPECIAL cue term — a card's SPECIAL window depends only on the card and
# the chart. **It is false for the count-limited passive term, and the
# counterexample is two lines:**
#
#     members A(Cute) B(Cute) C(Cool) D(Cool) E(Cool);  effect: type=Cute, count 1
#     A in slot 3 receives it iff B is in slot 4 or 5 -- so `w[A][3]` is not a
#     number at all, it is a function of where B went.
#
# `count <= targetCount` counts *matching members before the candidate*, which
# is a property of the whole prefix, not of the pair (card, slot). The passive
# term has to be evaluated inside the 120-permutation loop.
#
# So this is a **search**, not a table lookup. What keeps it cheap is not
# separability but two facts that are actually true:
#
# 1. **An O(5) precondition.** Order can only matter for an effect whose matcher
#    count *exceeds* its own `count`. When no live effect binds — about half of
#    random 5-member teams over `data/cards.json` — the ordered rule and the free
#    rule provably agree and the search is skipped entirely, bit-identically.
# 2. **The objective depends only on the recipient sets, not on the ordering.**
#    Many of the 120 orderings induce the same tuple of recipient sets, so the
#    expensive `_stat_sum` runs once per *distinct* tuple (typically a handful),
#    not 120 times. The 120-permutation walk that enumerates those tuples is
#    O(120 x varying_entries x 5) of set membership tests, and in
#    `score_card_set` it is hoisted out of the per-leader tail entirely.
#
# ## Which rule is the default
#
# Under the `"free"` rule every effect's recipients are resolved *independently*
# by `targets.resolve_target_card_ids`, which ranks the matchers by the stat(s)
# the effect raises and takes the top `count`. That choice does not depend on
# slot order, and it is what captured deck panels show the game doing, so
# `"free"` is the default.
#
# `"best_order"` (first-`count` matchers in slot order, maximised over the 120
# orderings) reads the slot-order walk above literally. Captured decks do not
# support it as the recipient rule, but it is kept runnable and under test so
# it stays assertable with one `dataclasses.replace`, exactly as
# `SummedActiveSkills` is.

# Marks a `stat_entries` element whose recipients cannot move with slot order,
# so `_ordered_target_card_ids` is never called for it and its already-resolved
# `target_ids` is reused verbatim. Used for every non-`applied` entry: its
# `target` may itself be the `UNKNOWN` sentinel that made it `data_missing`, and
# resolving it would be exactly the fabrication `targets.resolve_target_card_ids`
# refuses.
_FIXED_TARGET = object()

# `(weighted_rate, stat_names_this_effect_raises, resolved_target_card_ids,
#   order_target, self_card_id, slot_kind)`.
#
# The first three the module always carried; the next two re-resolve a recipient
# set under a candidate slot order (`order_target` is the skill's own `target`
# for an `applied` entry, `_FIXED_TARGET` otherwise). `slot_kind` is read ONLY
# by the deck-screen Overall Power stat block (per-source ceilings), never rank.
StatEntry = tuple[float, tuple[str, ...], frozenset, object, str, str]

# `(members, stat_entries, cfg, orderings_cache) -> (stat_sum, member_order)`.
SlotOrderResolver = Callable[
    [tuple, list, ScoringConfig, "dict | None"], "tuple[float, tuple[str, ...] | None]"
]


def _target_matches(target: EffectTarget, card: Card) -> bool:
    """The target predicate the game counts matches against. Only reached for
    the two set-membership kinds — `"self"` and the structurally team-wide
    `None` never get here, because neither can be count-limited."""
    if target.kind == "type":
        return card.type == target.value
    return target.value in card.generations


def _ordered_target_card_ids(
    target: object,
    self_card_id: str,
    ordered_members: tuple[Card, ...],
    under_supply: str,
) -> frozenset[str]:
    """The game's rule: the first `target.count` members of `ordered_members`
    that match `target`, where `ordered_members` is the deck in SLOT ORDER.

    The sibling of `targets.resolve_target_card_ids`, and it deliberately
    reproduces that function's other two contracts unchanged so the two can only
    differ in slot order:

    - `target is None` (team-wide) -> every member; `target.kind == "self"` ->
      the source card. Neither is count-limited, so neither can move with order.
    - `count == 0` -> no limit. That is the game's own early return
      (`cmp [rsi+0x24], 0 -> return TRUE`), and it is why `-all`/`-self` rows
      and every leader-skill effect ref are order-invariant.
    - **Under-supply** (fewer matchers than `count`) keeps honouring
      `cfg.target_under_supply` rather than being decided here.

      ⚠ That last one is a knowingly conservative choice and it is worth naming.
      The game's code contains **no under-supply check at all** — it counts
      matchers and compares, so every matcher passes when there are fewer of
      them than `targetCount`. That is `"apply_to_available"`, the shipped
      default, and it means the machine code **refutes `"do_not_apply"`**.
      Retiring that option would change `config.json` and the assumptions
      header, and must not happen silently as a side effect of slot ordering,
      so the knob stays live and stays honoured on both paths."""
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
    """The O(5) precondition, per entry: the indices of the `stat_entries`
    whose recipients can actually move with slot order.

    An entry binds iff it is a count-limited set-membership target whose matcher
    count *strictly exceeds* its own `count`. When matchers `<= count`, first-N
    in order and top-N by stat both select every matcher (or, under
    `"do_not_apply"`, both select none), so the two rules agree exactly and
    there is nothing to search — which is what makes the no-binding case
    bit-identical rather than merely close."""
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
    """The per-`(card, stat_name)` accumulation, over a supplied list of
    recipient sets parallel to `stat_entries`.

    One arithmetic definition shared by both drivers, so bit-identical parity
    holds by construction rather than by two copies staying in step — the same
    reason `_score_up_pool` and `_board_score_rate` are shared.

    The traversal is `members` order then `STAT_NAMES` order and is **not**
    permuted when a candidate slot order is being evaluated: slot order changes
    *which cards receive what*, never the order the five members' stats are
    summed in. Float addition is not associative, so permuting the summation
    would make the score of a set depend on the ordering even where the
    recipients did not change.

    ## Why it scatters instead of gathering

    A gather would, for each of the 15 `(member, stat_name)` pairs, re-scan
    **every** entry with two membership tests, ~87 tests per arrangement to
    place ~11 rates. This scatters instead — one pass over the ~5.8 entries,
    pushing each rate into its `(card_id, stat_name)` bucket. `_stat_sum` is one
    of the hottest functions in the program, and the scatter is about 1.3x
    faster than the gather at a realistic pool shape, with exactly equal floats.

    **Bit-identicality is structural, not a tolerance.** A bucket is appended to
    in `zip(stat_entries, recipients)` order, which is exactly the order the
    list-comprehension filtered in, so `combine()` receives the identical
    sequence; and the outer summation still runs `members` x `STAT_NAMES` with
    the same `total +=` sequence, so float addition never sees a different
    order. A gather and a scatter over the same predicate differ only in which
    loop is outermost.

    `cfg.stacking_model.combine(())` is called for the empty buckets too — the
    models are only guaranteed to agree with this function where they agree
    with themselves, so skipping the call for a non-matching pair would be a
    behaviour change, not an optimisation."""
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
    """The default rule: every effect keeps the recipients
    `targets.resolve_target_card_ids` picked for it — ranked by the stat(s) the
    effect raises, so the choice is independent of slot order.

    Returns no member order, because it does not model one — that `None` is what
    stops `ScoreBreakdown.member_order` from reporting an order this rule never
    chose. See `scoring_config.SLOT_ORDER_RULES`."""
    del orderings_cache
    return _stat_sum(members, stat_entries, cfg, [entry[2] for entry in stat_entries]), None


def _best_order_stat_sum(
    members: tuple[Card, ...],
    stat_entries: list,
    cfg: ScoringConfig,
    orderings_cache: dict | None = None,
) -> tuple[float, tuple[str, ...] | None]:
    """The alternative rule, kept runnable: `max` over the 120 slot orderings,
    under a first-`count`-in-slot-order recipient rule. Captured decks show the
    game does NOT pick recipients this way (they are chosen by stat, independent
    of slot order). Selected only under `slot_order_rule="best_order"`; the
    default is `free`.

    Returns `(stat_sum, member_order)`, where `member_order` is the member ids
    in the slot order that achieved the maximum — a tool that recommends a team
    without its slot order is handing the player the median case instead of
    the best one. It is `None` when no entry binds, which is the honest report:
    every ordering scores the same, so naming one would imply a recommendation
    the model did not make.

    Ties go to the first ordering `itertools.permutations(members)` reaches
    (strict `>`), and `members` is itself in a caller-fixed order, so the
    reported order is deterministic across runs.

    `orderings_cache`, when supplied, memoises the ordering enumeration by the
    varying entries' `(target, self_card_id)` signature. `score_card_set` passes
    one because its per-leader tail re-enters this function once per candidate
    leader over the *same* member set — and the leader contributes at most its
    own outfit skill, so the signature is nearly always identical across all of
    them. That turns a per-leader 120-permutation walk into a per-set one. The
    cache is created per `score_card_set` call and never outlives it: the
    enumeration depends on `members`, which is fixed inside one call and
    different in the next."""
    varying = _order_varying_indices(stat_entries, members)
    if not varying:
        # Provably identical to `_free_stat_sum`, not merely close — see
        # `_order_varying_indices`. Sharing the call is what makes it
        # bit-identical.
        return _free_stat_sum(members, stat_entries, cfg)

    signature = tuple((stat_entries[index][3], stat_entries[index][4]) for index in varying)
    orderings = None if orderings_cache is None else orderings_cache.get(signature)
    if orderings is None:
        # Distinct recipient-set tuples, each mapped to the FIRST slot ordering
        # that produces it. Many of the 120 orderings induce the same tuple —
        # only the relative order of each predicate's own matchers is read — and
        # the objective is a function of the tuple alone, so `_stat_sum` runs
        # once per distinct tuple rather than 120 times.
        distinct: dict[tuple, tuple[str, ...]] = {}
        for ordering in itertools.permutations(members):
            key = tuple(
                _ordered_target_card_ids(
                    stat_entries[index][3],
                    stat_entries[index][4],
                    ordering,
                    cfg.target_under_supply,
                )
                for index in varying
            )
            if key not in distinct:
                distinct[key] = tuple(card.id for card in ordering)
        orderings = tuple(distinct.items())
        if orderings_cache is not None:
            orderings_cache[signature] = orderings

    base = [entry[2] for entry in stat_entries]
    best_value: float | None = None
    best_order: tuple[str, ...] | None = None
    for recipient_key, member_order in orderings:
        recipients = list(base)
        for slot, index in enumerate(varying):
            recipients[index] = recipient_key[slot]
        value = _stat_sum(members, stat_entries, cfg, recipients)
        if best_value is None or value > best_value:
            best_value = value
            best_order = member_order
    return best_value, best_order


# The one place a slot-order rule name becomes an implementation. `scoring.py`
# never compares `cfg.slot_order_rule` for equality anywhere else;
# `scoring_config.SLOT_ORDER_RULES` names the same two keys and this module
# asserts the two agree at import.
SLOT_ORDER_RESOLVERS: dict[str, SlotOrderResolver] = {
    "best_order": _best_order_stat_sum,
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
    return SLOT_ORDER_RESOLVERS[cfg.slot_order_rule](
        members, stat_entries, cfg, orderings_cache
    )


def _board_score_rate(members: tuple[Card, ...], cfg: ScoringConfig) -> float:
    """The Holomem Board's Score Bonus contribution for this member set — the
    game's own `ScoreUpBonusRatioBySkillTree`.

    The game's `Vision.LiveCore!LiveScoreCalculator::CalcScore`
    @ rva `0x5f01f80` ends in

        ... x (1 + SkillScoreUpRatio) x (1 + ScoreUpBonusRatioBySkillTree)

    — two **separate multiplicative factors**, confirmed by two independent
    disassemblies of the same method. The deck screen *displays* the two
    as summed percentage-point lines, which is what a preview panel does; the
    score computation multiplies them. So this is never added into
    `combined_score_rate`, and both scoring drivers apply it as its own
    `(1 + rate)` stage.

    Both drivers call THIS function rather than each summing the dict
    themselves, for the same reason `_active_pool` exists: bit-identical parity
    should hold by construction, not by two copies staying in step.

    The aggregation is a plain sum over the five members' own board
    contributions, in `members` order. This is an inference: the binary states
    the term, not how a deck's members combine into it. Order is fixed (float
    addition is not associative) so `score_card_set` reproduces this
    bit-identically.

    The leader contributes nothing here. That is consistent with the leader
    slot contributing its outfit skill and nothing else, but it rests on the
    same inferred aggregation, not on an independent fact.

    Returns exactly `0.0` for the shipped default (an empty
    `cfg.board_score_bonus`), so the stage is the identity until board data
    exists."""
    total = 0.0
    for card in members:
        total += cfg.board_score_bonus.get(card.member, 0.0)
    return total


def _board_bonus(
    leader: Card | None, members: tuple[Card, ...], cfg: ScoringConfig
) -> tuple[float, float, float]:
    """`(power_bonus, score_bonus_rate, skill_effect_up)` for this arrangement —
    the single place either driver reads the Holomem Board.

    `power_bonus` is the game's own `up_by_skill_tree` line, a **summand of
    Overall Power** (`Overall Power = Member Parameter + Outfit Skill +
    Holomem Board Bonus + Passive Skill + Memory Bonus + Member Power-Up`,
    as `Live/GetDeck` reports it). It is added to `effective_stat_sum` *before*
    the score factors multiply, because that is where the game puts it — never
    folded into a rate.

    `score_bonus_rate` is `ScoreUpBonusRatioBySkillTree`, the board's OWN
    multiplicative factor, never a summand of `combined_score_rate`.

    `skill_effect_up` is the third term and is neither of the other two: it
    goes INSIDE `_score_up_pool`'s `(1 + SkillEffectUpPermilUp/1000 + B)`
    parenthesis, multiplying the ACTIVE pool. It is returned from here, beside
    the other two, so both drivers keep reading the board through one function
    — the same reason `_board_score_rate` is shared rather than written out
    twice.

    ⚠ Why this matters more than its size suggests: `score_bonus_rate` is a
    team-INVARIANT account constant (it can come from nodes of characters who
    are not on the team), so on its own it cannot move a RANKING.
    `skill_effect_up` is leader- and member-scoped, so it can — omitting it
    would be a selection defect and not a scaling one.

    All three are exactly `(0.0, 0.0, 0.0)` for the shipped default — `cfg.board`
    is `None` and `cfg.board_score_bonus` is empty — so this is the identity
    until board data exists. When `cfg.board` IS set it takes precedence over
    the dict, because a per-holomem float cannot express a red node's "applies
    to the whole team, but only while she leads"."""
    if cfg.board is None:
        return 0.0, _board_score_rate(members, cfg), 0.0
    # Both terms come from `BoardModel`'s memo rather than from a fresh
    # `board.resolve` walk. `resolve` remains the reference implementation of
    # what the board is worth — `tests/test_board.py` measures through it, and
    # `tests/test_board_landing.py` checks this path against it — but it
    # rebuilds every effect on every captured board, and calling it once per
    # ARRANGEMENT dominated the run time of a board-loaded search.
    #
    # `leader.member` is the only thing about the leader `resolve` can read
    # (`board._recipients` compares it and consults nothing else), so passing
    # the holomem rather than the card is not a simplification — it is the
    # exact dependency, which is what makes the memo sound.
    #
    # The dispatch lives HERE, in the one function both drivers read the board
    # through, so `_score_team` and `score_card_set` cannot end up on different
    # sides of it. Same reason `_board_score_rate` is a shared function rather
    # than two summing loops.
    holomem = None if leader is None else leader.member
    return (
        cfg.board.power_bonus_for(members, holomem),
        cfg.board.score_bonus_rate(),
        cfg.board.skill_effect_up_for(members, holomem),
    )


def _score_team(
    team: Team, cfg: ScoringConfig, *, with_breakdown: bool = False
) -> float | ScoreBreakdown:
    """The two-phase driver, and the *only* implementation of
    gate resolution plus EV arithmetic in this module — `score_fast` and
    `score_with_breakdown` both delegate here rather than each
    re-implementing pass 1 then pass 2. Pass 1 folds every
    fired `activation_rate_up` skill into `team_rate_up`; pass 2 scores
    every skill, weights it by `cfg.family_weights[resolved.family]`, and
    sorts it by pool into a stat-rate list (plus a `stat_entries` list), a
    non-ACTIVE score-rate list, an `active_entries` list, or a support list.

    The stat stage is not one multiplier against the whole team's three-stat
    sum. Each `pool == "stat"` skill's resolved target ids (via
    `targets.resolve_target_card_ids`) are carried alongside its weighted rate
    in `stat_entries`, and `effective_stat_sum` is accumulated per
    `(card, stat_name)` pair: `card.stat(stat_name) * (1 + combine(rates naming
    that stat that target that card))`, summed over `team.members` in order,
    then over `schema.STAT_NAMES` in order. A team-wide `stat_up` (target
    `None`) only multiplies the *named* stat across every card — never the
    other two stats. `combined_stat_rate` on `ScoreBreakdown` is the whole-team
    combined stat rate, from the flat `stat_rates` list, and is a report-only
    summary, not the multiplier.

    The score stage is
    `total = effective_stat_sum * (1 + combine([*score_rates, active_pool]))
                                * (1 + board_score_rate)`,
    following the game's own code:

    - `active_pool` — every ACTIVE Score UP collapses into ONE aggregate rate
      (`_active_pool`), because the game takes a MAXIMUM over the live effects
      rather than summing them. Only the non-ACTIVE score-pool rates stay
      individual entries in the `combine()` call.
    - `board_score_rate` — the Holomem Board is its own multiplicative factor
      (`_board_score_rate`), never a summand of the score pool.

    `with_breakdown` is the only difference between the two entry points.
    When `False` (the hot-loop path), an `activation_rate_up` skill (whose
    pool is "none") is skipped before `_resolve_skill` is ever called, and
    no `SkillContribution` is allocated anywhere — the fast
    path allocates nothing extra. When `True`, every resolved skill
    instance is retained as a `SkillContribution` in
    `Team.effective_skill_instances()` order — including an
    `activation_rate_up` skill, whose own contribution was already folded
    into `team_rate_up` and therefore still never joins `stat_rates`/
    `score_rates` even in the breakdown path — and a `ScoreBreakdown` is
    returned instead of a bare float. Neither branch re-resolves gates or
    re-dispatches EV arithmetic a second time; both read from exactly one
    pass over `resolved_skills`."""
    resolved_skills = resolve_team_gates(team, cfg.run_state)
    team_rate_up = _fold_activation_rate_up(resolved_skills, cfg) + _fold_rider_activation_rate_up(
        resolved_skills, cfg
    )
    cards = team.members  # gates and stats are the 5 members'

    stat_rates: list[float] = []
    # See `StatEntry`. The last two slots carry the skill's own target clause,
    # so `_effective_stat_sum` can re-resolve the recipients under a candidate
    # slot order rather than being stuck with the one resolution made here.
    stat_entries: list[StatEntry] = []
    # Parallel to `stat_entries`, and only populated on the breakdown path:
    # each entry's index into `contributions`, so the chosen slot order's
    # recipients can be written back onto the row that reported them.
    stat_contribution_indices: list[int] = []
    # Score-pool rates that are NOT a Score UP — combined individually.
    score_rates: list[float] = []
    # `(bucket, weighted_magnitude_rate, occupancy)`. A Score UP cannot be
    # reduced to one rate here, because its bucket's pool is a maximum over
    # magnitudes and a maximum is not computable once magnitude and occupancy
    # have been multiplied together.
    ratio_entries: list[tuple[str, float, float, int, bool]] = []
    # The Score Support boost terms, same shape and same partition.
    boost_entries: list[tuple[str, float, float, int, bool]] = []
    # Parallel to the two lists above: the owner of each ratio term and the
    # resolved recipient set of each targeted boost (`_score_up_pool`).
    ratio_owners: list[str | None] = []
    boost_targets: list[frozenset[str] | None] = []
    contributions: list[SkillContribution] = []

    for resolved in resolved_skills:
        pool = _pool_for(resolved)
        if pool == "none" and not with_breakdown:
            # Hot-loop path: this skill's own contribution was already
            # folded into team_rate_up by _fold_activation_rate_up; it
            # never joins stat_rates/score_rates, so there is nothing left
            # to compute for it here.
            continue

        rate, state, missing_field = _resolve_skill(resolved, cfg, team_rate_up)
        weight = cfg.family_weights[resolved.family]
        weighted_rate = rate * weight

        # Set alongside stat_entries's own target_ids, from
        # the same resolution -- never re-resolved a second time for the
        # contribution, so the attribution can never disagree with the
        # number that was actually scored. Stay None for every
        # non-"applied"-pool=="stat" contribution (a score-pool row, a
        # pool=="none" row, or a "stat" row that never resolved a target
        # at all -- gate_not_met, or data_missing, whose missing field may
        # itself be `target`).
        target_kind: str | None = None
        target_card_ids: tuple[str, ...] | None = None

        if pool == "stat":
            stat_rates.append(weighted_rate)
            # Only an `applied` skill carries a known target — a
            # `gate_not_met`/`data_missing` skill's weighted_rate is
            # already 0.0 (contributes nothing either way), and its
            # `target` may itself be the very reason it is data_missing
            # (an UNKNOWN target must never reach resolve_target_card_ids).
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
            # Only a Score UP joins a bucket maximum; a life/judgment effect's
            # `ScoreUpByRatio` is the base stub returning 0.0f, so it neither
            # joins a maximum nor lowers one (`_is_score_up_effect`).
            if _is_score_up_effect(resolved):
                entry = _lattice_entry(resolved, cfg, team_rate_up, weight, state)
                # The SPECIAL collapse: a SPECIAL Score UP is a
                # `ScoreUpBoostSkillEffect`, so it joins `B` rather than the
                # ratio (`_score_up_implements_boost`).
                if _score_up_implements_boost(resolved):
                    boost_entries.append(entry)
                    boost_targets.append(None)
                else:
                    ratio_entries.append(entry)
                    ratio_owners.append(resolved.card_id)
            else:
                score_rates.append(weighted_rate)
        elif pool == "support":
            # Never joins the score pool. It becomes part of the `B` term
            # that multiplies the ACTIVE maximum, in `_score_up_pool` — the
            # same term the team's SPECIAL Score UPs join.
            support_target_ids: frozenset[str] = frozenset()
            if state == "applied":
                support_target_ids = targets.resolve_target_card_ids(
                    resolved.skill.target,
                    resolved.card_id,
                    cards,
                    STAT_NAMES,
                    cfg.target_selection_model,
                    cfg.target_under_supply,
                )
                target_kind = (
                    resolved.skill.target.kind
                    if isinstance(resolved.skill.target, EffectTarget)
                    else TEAM_WIDE_TARGET_KIND
                )
                target_card_ids = tuple(sorted(support_target_ids))
            # A support that reaches NOBODY never enters the effect
            # collection; a team-wide one boosts every ACTIVE; a count-targeted
            # one boosts only the ACTIVEs of its resolved recipients
            # (`_score_up_pool`, `_support_targets`).
            boost_entries.append(
                _lattice_entry(resolved, cfg, team_rate_up, weight, state)
                if support_target_ids
                else (
                    _score_bucket(resolved),
                    0.0,
                    0.0,
                    _active_interval(resolved),
                    _windows_are_mutually_exclusive(resolved), 0.0, 0.0,  # no lattice
                )
            )
            boost_targets.append(_support_targets(resolved, support_target_ids))
        # pool == "none": folded into team_rate_up already; never joins a
        # pool list, even in the breakdown path.

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
                        resolved.override_gate.fired
                        if resolved.override_gate is not None
                        else None
                    ),
                    rider_family=(
                        resolved.skill.rider.family
                        if isinstance(resolved.skill.rider, SlotRider)
                        else None
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
    # `combined_support_rate` is exactly the game's `B` — max over the
    # ACTIVE-bucket supports plus max over the SPECIAL-bucket ones — and it IS
    # the multiplier applied to every Score UP, unlike `combined_stat_rate`,
    # which is a report-only summary. It is not a `stacking_model` combine,
    # because the game uses the same max-then-add rule here as for the score
    # pool itself.
    combined_support_rate = cfg.active_stacking_model.combine(boost_entries)

    # The board is its OWN multiplicative factor, never a summand of
    # combined_score_rate (`_board_score_rate`), and its POWER half is a
    # summand of Overall Power, never a rate (`_board_bonus`). Exactly
    # (0.0, 0.0, 0.0) while cfg.board is None and cfg.board_score_bonus is
    # empty, which is the shipped default.
    #
    # Read BEFORE the pool, not after: `board_skill_effect_up` belongs inside
    # `_score_up_pool`'s multiplier, so it has to exist by the time the pool is
    # built. `score_card_set` orders these two the same way for the same reason.
    board_power, board_score_rate, board_skill_effect_up = _board_bonus(
        team.leader, cards, cfg
    )
    # Every Score UP collapses into ONE aggregate contribution
    # (`SkillScoreUpRatio`), appended last so the combine order is fixed rather
    # than interleaved with the non-Score-UP rates — `score_card_set` builds
    # the identical list, which is what keeps the two bit-identical.
    score_up_pool = _score_up_pool(
        ratio_entries,
        boost_entries,
        cfg,
        board_skill_effect_up,
        ratio_owners,
        boost_targets,
    )
    combined_score_rate = cfg.stacking_model.combine([*score_rates, score_up_pool])

    # The per-card, per-stat accumulation, behind the slot-order rule
    # (`_effective_stat_sum`). `member_order` is the slot order that achieved
    # the maximum, or `None` when no count-limited effect binds on this team and
    # every ordering therefore scores identically.
    effective_stat_sum, member_order = _effective_stat_sum(cards, stat_entries, cfg)

    total_score = (
        (effective_stat_sum + board_power)
        * (1.0 + combined_score_rate)
        * (1.0 + board_score_rate)
    )

    if not with_breakdown:
        return total_score

    if member_order is not None:
        # "Resolve once, use twice", kept under a rule that
        # chooses the recipients *after* the rows were built. `target_card_ids`
        # must name the cards the scored number actually reached; leaving it at
        # the free-rule resolution would make the attribution disagree with the
        # total on exactly the teams where slot order mattered — the one case a
        # reader is looking at it for.
        ordered_members = tuple(
            next(card for card in cards if card.id == card_id) for card_id in member_order
        )
        for entry_index, contribution_index in enumerate(stat_contribution_indices):
            entry = stat_entries[entry_index]
            if entry[3] is _FIXED_TARGET:
                continue
            recipients = _ordered_target_card_ids(
                entry[3], entry[4], ordered_members, cfg.target_under_supply
            )
            contributions[contribution_index] = replace(
                contributions[contribution_index],
                target_card_ids=tuple(sorted(recipients)),
            )

    # The two account-level terms the game shows and the ranking deliberately
    # omits. Member Power-Up is a near-uniform x1.0035 across decks and Memory
    # Bonus is a few permil of one line — both are ranking-SAFE to drop, so they
    # are reported and never folded into `total_score` above. Dropping them from
    # the ranking is deliberate; hiding them from the reader is not, and the
    # game's own Unit Score Details panel lists both.
    #
    # Memory Bonus uses the bit-exact per-member per-stat form
    # (`memory_bonus_per_stat`, which matches the server's `power.memory`): the
    # ranking path already carries each member's leveled+bloomed base parameter
    # as `Card.stats`, which is exactly the per-stat decomposition the exact form
    # needs. `member_power_up` here is given one base rather than five, so it
    # pays one ceiling where the game pays five — an under-statement of a few
    # units on a term worth about 0.34 % of Overall Power. Both stay REPORTED,
    # never summed into `total_score`.
    # ---- Overall Power (deck-screen DISPLAY) — the two rounding fixes ----
    # Fix A: the Outfit + Passive stat lines are paid as per-(card, attribute)
    # FLOAT32 ceilings, per member, not one float64 product. Fix B: Member
    # Power-Up is then paid its FIVE per-slot ceilings over each member's
    # (MemberParameter + Outfit + Passive + HolomemBoard) base. Both live ONLY
    # in this DISPLAY assembly; `effective_stat_sum`/`total_score` above stay
    # float64 so the ranking is unmoved. The recipients are the free-rule ones
    # (the default `_free_stat_sum`); the display sum matches the
    # ranking sum's recipient choice on that rule.
    # Recipient tie-break — a count-limited effect ranks its matchers by the stat
    # it raises (`targets.resolve_target_card_ids`) and takes the top `count`; on
    # an exact tie the game keeps DECK SLOT ORDER (the earlier-slot member wins).
    # The RANKING's `top_n_by_stat` (`TopNByBuffedStat`) uses that same
    # slot-order tie-break — a STABLE sort with no card_id key — so `entry[2]`
    # (the free-rule ranking recipients) ALREADY equals the slot-order choice.
    # This re-resolution under `_SLOT_ORDER_TIE_BREAK` is therefore
    # belt-and-suspenders — it reproduces `entry[2]` (`cards` is `team.members`,
    # the deck in slot order, so both stable sorts keep ties in slot order) — and
    # is kept so the DISPLAY assembly stays self-contained and correct even if the
    # ranking model is ever reconfigured.
    op_recipients = [
        entry[2]
        if entry[3] is _FIXED_TARGET
        else targets.resolve_target_card_ids(
            entry[3], entry[4], cards, entry[1], _SLOT_ORDER_TIE_BREAK, cfg.target_under_supply
        )
        for entry in stat_entries
    ]
    op_stat_subtotals = _overall_power_stat_subtotals(
        cards, stat_entries, cfg, op_recipients
    )
    overall_power_stat_sum = float(sum(op_stat_subtotals))
    # Per-member Holomem Board power: `power_bonus_for` sums over the members
    # passed, so one single-member call yields exactly that member's board
    # power (its `_terms` are cached per card, so this is not a re-walk). Summed
    # back, these are bit-identical to `board_power` above — the split the
    # per-slot `member_power_up` base needs, with no new board API.
    _holomem = None if team.leader is None else team.leader.member
    per_member_board_power = (
        [0.0] * len(cards)
        if cfg.board is None
        else [cfg.board.power_bonus_for((c,), _holomem) for c in cards]
    )
    power_up_bases = [
        op_stat_subtotals[i] + per_member_board_power[i] for i in range(len(cards))
    ]

    account = getattr(cfg, "account", None)
    memory_line = (
        None
        if account is None
        else memory_bonus_per_stat(
            [
                (m.stats.performance, m.stats.technique, m.stats.sense)
                for m in team.members
            ],
            account.memory_permil,
        )
    )
    power_up_line = (
        None
        if account is None
        else member_power_up(power_up_bases, account.power_up_permyriad)
    )

    return ScoreBreakdown(
        total_score=total_score,
        memory_bonus=memory_line,
        member_power_up=power_up_line,
        overall_power_stat_sum=overall_power_stat_sum,
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
    return _score_team(team, cfg)


@dataclass(frozen=True, slots=True)
class SkillContribution:
    """One skill instance's contribution to a team's score, reusing the
    vocabulary from `ResolvedSkill`/`GateResolution`/
    `GatePredicate` (`card_id`, `slot_kind`, `source`, `family`) rather
    than inventing parallel names.

    `state` is one of `CONTRIBUTION_STATES`. The `gate_*` fields are set
    from the skill's `GateResolution` and its `GatePredicate` whenever the
    skill is gated, and are all `None` when it is not (an ungated skill).
    A gate whose actual team count exactly equals its threshold is
    recorded `applied` with `gate_actual_count == gate_threshold`; a gate
    one short is recorded `gate_not_met` with both `gate_actual_count` and
    `gate_threshold` present — the breakdown never reports only the
    boolean.

    `effect_type` is retained deliberately: stage one folds every
    stat rate into one pool, and keeping the targeted stat on the
    contribution is what `target_kind`/`target_card_ids`
    actually use to report which cards a targeted stat effect reached.

    `weight` is the `family_weights` value actually applied and
    `weighted_rate` is `rate * weight`, so a family contributing zero by
    an inspectable configured assumption (e.g.
    `life_recovery` at its default `0.0` weight) reads differently from a
    family contributing zero because its gate did not fire.

    `target_kind`/`target_card_ids` name which
    cards a `pool == "stat"` contribution actually reached, resolved once
    by `_score_team`'s own `pool == "stat"` branch and carried here rather
    than re-resolved a second time — resolve once, use twice, so this
    field can never disagree with the number that was actually scored.
    Both are `None` for every other contribution: a `pool ==
    "score"`/`"none"` row, or a `pool == "stat"` row whose `state` is not
    `"applied"` (a gate that did not fire, or an `UNKNOWN` field — which
    may itself be `target`, so resolving it here would be exactly the
    fabrication `targets.resolve_target_card_ids` itself refuses).
    `target_kind` is `schema.TEAM_WIDE_TARGET_KIND` for a team-wide
    (`{"kind": "all"}`) target, never `None` — reaching the whole team is
    an affirmative fact, not an absence. `target_card_ids` is always
    sorted ascending by card id, so two runs over identical inputs
    serialize byte-identically.

    `override_fired` tells a reader
    which of the slot's two possible magnitudes `rate` was actually
    computed from: `True` when the slot carries an override whose own
    gate fired, `False` when it carries an override whose gate did not
    fire (the base magnitude was used), and `None` when the slot carries no
    override at all. Without this field a reader cannot tell a 70 that came
    from a fired override apart from a 70 that was always the base.

    `rider_family`/`rider_effect_type`/`rider_magnitude` are
    attribution-only fields naming the slot's
    second, differently-typed effect when it has one, and `None` for all
    three when it does not. They are set directly from `resolved.skill
    .rider` (a `SlotRider`) whenever the slot carries a populated rider —
    never from `resolved.family`/`rate`/`weighted_rate`, which stay the
    principal's own values unchanged. A rider's own value never appears in
    `rate`, `weight` or `weighted_rate`; these three fields exist so
    `--explain`/`--json` can *say* a row carries a second effect."""

    card_id: str
    slot_kind: str
    source: str
    family: str
    effect_type: str
    state: str
    pool: str
    rate: float
    weight: float
    weighted_rate: float
    missing_field: str | None
    gate_dimension: str | None
    gate_value: str | None
    gate_threshold: int | None
    gate_actual_count: int | None
    gate_fired: bool | None
    target_kind: str | None = None
    target_card_ids: tuple[str, ...] | None = None
    override_fired: bool | None = None
    rider_family: str | None = None
    rider_effect_type: str | None = None
    rider_magnitude: float | None = None


@dataclass(frozen=True, slots=True)
class ScoreBreakdown:
    """The full attribution result. Both stages of the
    two-stage multiplicative formula stay separately inspectable:
    `effective_stat_sum = base_stat_sum * (1 + combined_stat_rate)`, then
    `total_score = effective_stat_sum * (1 + combined_score_rate)
                                      * (1 + board_score_rate)`.

    `contributions` is in `Team.effective_skill_instances()` order, never
    reordered by contribution value — two skills contributing equal
    amounts keep a specified, stable position. `incomplete_skills` is
    `team_taint(team)`, carried on the result rather than reconstructed
    downstream.

    `board_score_rate` is the Holomem Board's own multiplicative stage
    (`scoring.py::_board_score_rate`), reported separately because the game
    keeps it separate: `total_score = effective_stat_sum *
    (1 + combined_score_rate) * (1 + board_score_rate)` — from
    `CalcScore`'s two trailing factors, never one summed Score Bonus. It is
    `0.0` whenever `cfg.board_score_bonus` is empty, which is the shipped
    default, and a reader can tell "the board contributes nothing here" from
    "the board was never modelled" only by looking at that dict — the field
    itself cannot distinguish them.

    `combined_support_rate` is the game's `B`: the maximum
    `ScoreUpBoostByRatio` in the ACTIVE bucket plus the maximum in the SPECIAL
    bucket. Unlike `combined_stat_rate` it is a live multiplier, not a summary
    — the ACTIVE Score UP maximum is multiplied by
    `(1 + combined_support_rate)`.

    ⚠ **It includes the team's SPECIAL Score UPs**, not only its `score_support`
    slots. A SPECIAL Score UP *is* a `ScoreUpBoostSkillEffect`
    (`scoring._score_up_implements_boost`), so it enters `B` rather than the
    ratio, and a reader who expects this field to account only for
    `family == "score_support"` rows will find it larger than that. The SPECIAL
    rows themselves still report their own `weighted_rate` as what they would
    contribute alone.

    ⚠ **The Score UP contributions do not sum to the score pool.** Since the
    pool is `max` per bucket (`ExpectedMaximumActiveSkill`), each Score UP
    row's `weighted_rate` is still that skill's own
    `magnitude x occupancy x weight` — what it would contribute alone, and
    before `B` — but the pool is `(1 + B) x max_ACTIVE` of the boosted values,
    which is strictly less than their sum whenever two same-bucket effects can
    be live at once, and strictly less again for a SPECIAL row, whose own
    `weighted_rate` is a bare `magnitude x occupancy` while what it actually
    buys is that much *of the ACTIVE maximum*. Adding up the rows over-counts
    twice over, and over-counts SPECIAL rows worst. That
    is a property of the mechanic, not a reporting bug: a card's contribution
    genuinely is not separable once the highest live effect in its bucket is the
    only one paid. `combined_score_rate` is the number that was actually used.

    `stacking_model_name`, `activation_rate_model_name`,
    `active_stacking_model_name` and `run_state` make every total traceable to
    the interpretations that produced it."""

    total_score: float
    base_stat_sum: float
    effective_stat_sum: float
    combined_stat_rate: float
    combined_score_rate: float
    combined_support_rate: float
    board_score_rate: float
    team_rate_up: float
    contributions: tuple[SkillContribution, ...]
    incomplete_skills: tuple[str, ...]
    stacking_model_name: str
    activation_rate_model_name: str
    active_stacking_model_name: str
    run_state: RunState
    # Which slot-order rule produced `effective_stat_sum`, alongside the other
    # three model names: a total must be traceable to the interpretations that
    # produced it.
    # The ACTIVE Score-UP pool (`_score_up_pool`'s result): the score-bonus rate
    # the board's active-pool channels (skill_effect_up, activation_rate,
    # cooldown) multiply. Exposed so a board-plan valuer can price those nodes;
    # 0.0 on the batch path that does not build a full breakdown.
    active_pool: float = 0.0
    slot_order_rule: str = SLOT_ORDER_RULES[0]
    # The member ids in the DECK SLOT ORDER that achieved `effective_stat_sum`,
    # or `None` when no count-limited effect binds on this team and every
    # ordering scores identically (recommending a team without its slot order
    # hands the player the median case instead of the best one; reporting an
    # order where none was chosen would be the opposite error, implying a
    # recommendation the model never made). Always `None` under the `"free"`
    # rule, which does not model an order at all.
    member_order: tuple[str, ...] | None = None

    # The two terms the game's Unit Score Details panel lists and the ranking
    # omits. `None` means the player has not supplied the account scalar;
    # `0` means they supplied one and it is zero. A reader must be able to tell
    # "not modelled" from "modelled and worth nothing", so these are never
    # defaulted to 0.0 -- see `domain/collection.AccountBonuses`.
    #
    # NOT summands of `total_score`. Both are ranking-safe to drop, so the
    # ranking is unaffected and these are reported alongside it.
    memory_bonus: int | None = None
    member_power_up: int | None = None

    # The Overall Power (deck-screen DISPLAY) stat block: MemberParameter +
    # Outfit + Passive, summed with the server's per-(card, attribute) FLOAT32
    # ceilings rather than the float64 `effective_stat_sum` the RANKING uses
    # (`_overall_power_stat_subtotals`). `None` on breakdowns that do not carry
    # it (e.g. hand-built test breakdowns) — `deck_screen_unit_score` then
    # falls back to `effective_stat_sum`.
    # This is a DISPLAY quantity and is NOT a summand of `total_score`.
    overall_power_stat_sum: float | None = None

    # The Holomem Board's POWER half — a summand of Overall Power, never a
    # rate (`_board_bonus`), which is the other side of `board_score_rate`
    # above. Carried because the total is
    # `(effective_stat_sum + board_power) * (1 + combined_score_rate) *
    # (1 + board_score_rate)`, so without it a reader can only recover the
    # board's power line by dividing the total back out — arithmetic that is
    # exact but reads as a reconstruction, and that silently attributes any
    # future term to the board. This line is roughly **30–50 % of Overall
    # Power**, so it is the largest single thing a breakdown can show.
    #
    # `0.0` means the board contributed no power — which, exactly as with
    # `board_score_rate`, is indistinguishable from "no board was modelled"
    # by looking at this field alone. `cfg.board is None` is the fact that
    # separates them.
    board_power: float = 0.0


# `Unit Score = ceil( Overall Power x (1 + Score Bonus) x K )`.
#
# K is determined as a FEASIBLE INTERVAL, not a fit, over every captured deck
# screen (two independent capture sets whose intervals INTERSECT):
#
#     K in [2.037339908411 , 2.037340032136]      relative width 6.1e-08
#
# and with `ceil` the residue is **exactly 0 on every observation**, never +/-1.
#
# Each capture constrains K to `(unit-1)/X < K <= unit/X`; the intervals are
# intersected in exact rational arithmetic. An intersection is EMPTY when the
# shape is wrong — which is what makes this evidence and not a fit. Other
# pipeline shapes (floor, round, MultiplyPermil-first, ceiling-before-bonus,
# per-line summation, multiplicative bonus composition) are infeasible for ANY
# K. A `mean(unit / (power x factor))` estimate is biased upward by about half
# a point per sample because the game CEILINGs, so it must not be used.
#
# The shipped point is `2.03734`. It is chosen for STABILITY, not because it is
# special: a midpoint moves every time the interval narrows, a point that stays
# inside does not, and `deck_oracle.py` uses the same value so the two agree by
# construction.
# ⚠ `2.03734` is ALSO the 5-decimal round number, and the interval contains it.
# Do not read that as identification: a random band of this width contains a
# 5-decimal value with probability ~1.2 %. Further captures either keep
# containing it or exclude it.
#
# ⛔ **K is not a constant the game ships.** All `Setting` fields are integers
# under a permil/permyriad convention; `2037` permil and `20373` permyriad both
# fall outside the interval, so no field in the game's own units could express
# it. A sweep of the whole client image at every byte offset for float32,
# float64 and integer encodings returned no aligned hits, as did every master
# table and the constant pool.
#
# **So K is a server-side COMPUTED PRODUCT.** Its factorisation is not known,
# and no further disassembly of the client can produce it. The value below is a
# point inside the interval and may move; treat it as provisional.
UNIT_SCORE_K: float = 2.03734


@dataclass(frozen=True, slots=True)
class DeckScreenScore:
    """The deck screen's own Unit Score, assembled in the game's ADDITIVE
    shape — for display beside a ranking, never as a ranking objective.

    It is a separate function rather than a change to `total_score`, because
    the two quantities are genuinely different arithmetic —
    `CalcScore` (the in-run computation, which `total_score` follows)
    MULTIPLIES the board score bonus as its own trailing factor, while the deck
    screen ADDS it into one summed Score Bonus. Using the in-run shape to
    reproduce the deck-screen number costs the cross term
    `combined_score_rate x board_score_rate`: about **+2 % to +12 %** on
    captured decks, and the spread is TEAM-DEPENDENT, so for this quantity the
    multiplicative form is a *wrong formula* and not a missing constant.

    ## What this reproduces, and what it does not

    **The FORMULA is exact.** Given the game's own term values, the six power
    terms sum to `overall_power`, the five bonus terms sum to
    `score_bonus_permil`, and `overall_power x (1 + permil/1000) x K`
    reproduces `unit_score` (see `UNIT_SCORE_K`).

    ⚠ **The BASE may not be exact, and this number inherits that.** When the
    stat sources on disk do not reproduce the server's Member Parameter line,
    the result will not match the number on the deck screen, and a caller must
    say so rather than presenting it as the game's figure. What it does deliver
    is the right *shape*, which isolates any remaining discrepancy to the base.

    `missing` names any Overall Power summand the account has not supplied —
    the two account scalars are `None` until the player enters them, and a sum
    that silently omits them is not the game's Overall Power. `complete` is
    the one-look version for a renderer.

    ⚠ **`unit_score` is the EXACT float; the game shows an integer.** The
    conversion to an integer happens in the output layer, never here.
    `tests/test_scoring_ev.py` carries a structural guard that forbids every
    such construct anywhere in this module, and it is right to: discarding
    precision inside an expected-value computation is a silent correctness
    bug. So the arithmetic stays exact here and the display boundary decides
    how to show it, in `output/json_output.py`.

    (That guard is blunt enough to fire on a docstring that merely *names* the
    forbidden constructs, which is why this paragraph describes them instead.
    Bluntness is the feature: it cannot be satisfied by renaming.)"""

    unit_score: float
    overall_power: float
    score_bonus_permil: float
    missing: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.missing


def deck_screen_unit_score(
    breakdown: ScoreBreakdown, *, k: float = UNIT_SCORE_K
) -> DeckScreenScore:
    """Assemble `breakdown` into the deck screen's additive Unit Score.

    Overall Power is the sum of the power summands — the stat sum (which
    already carries the Passive/Outfit stat effects), the board's power half,
    and the two account scalars. The Score Bonus is the **plain sum** of the
    bonus lines, board included, which is the whole point: `total_score`
    multiplies them as separate stages and this does not."""
    missing: list[str] = []
    # The stat block is the FLOAT32-ceiling Overall Power sum when the breakdown
    # carries it (`_overall_power_stat_subtotals`); older/hand-built breakdowns
    # leave it `None` and fall back to the float64 `effective_stat_sum`.
    stat_block = (
        breakdown.effective_stat_sum
        if breakdown.overall_power_stat_sum is None
        else breakdown.overall_power_stat_sum
    )
    overall_power = stat_block + breakdown.board_power
    if breakdown.memory_bonus is None:
        missing.append("up_by_poster_collect")
    else:
        overall_power += breakdown.memory_bonus
    if breakdown.member_power_up is None:
        missing.append("card_training_evaluation")
    else:
        overall_power += breakdown.member_power_up

    # ADDITIVE — this is the line that differs from `total_score`.
    permil = (breakdown.combined_score_rate + breakdown.board_score_rate) * 1000.0

    return DeckScreenScore(
        unit_score=overall_power * (1.0 + permil / 1000.0) * k,
        overall_power=overall_power,
        score_bonus_permil=permil,
        missing=tuple(missing),
    )


def score_with_breakdown(team: Team, cfg: ScoringConfig) -> ScoreBreakdown:
    """Attribution path: delegates to the same driver as `score_fast`,
    retaining every intermediate `SkillContribution`."""
    return _score_team(team, cfg, with_breakdown=True)


@dataclass(frozen=True, slots=True)
class LeaderScore:
    """One leader's total score within a scored 6-card set."""

    leader_id: str
    total_score: float


def _leader_class_key(
    leader: Card, member_ids: frozenset[str], board_leader_scoped: frozenset[str]
) -> object:
    """The equivalence class a candidate leader belongs to for ONE member set.

    A leader contributes nothing but its OUTFIT, so two leaders wearing
    the same outfit produce the same score — but "the same outfit" is not the
    whole key, and the three exceptions below are the whole reason this is a
    function rather than `leader.costume_skill`:

    1. **A `self`-targeted costume** resolves to the LEADER'S OWN id
       (`targets.resolve_target_card_ids`: `kind == "self"` returns
       `{self_card_id}`, "never resolved against `cards` at all"), and that id
       also enters `_best_order_stat_sum`'s ordering signature. No shipped card
       has one — every shipped costume target parses to the team-wide `None`
       — so this branch is not reached by real data. It is here because its
       absence would be silent if one ever ships.
    2. **A leader that is also one of the five** interacts with the member set
       through its own id, so it never merges. At most five per call.
    3. **The structural board's red branch applies only while its holomem
       leads** (`BoardModel.leader_scoped_holomem`), so inside that set leaders
       are interchangeable only with leaders of the SAME holomem. Outside it
       the board term is bit-identical to the `leader=None` one *by code path*,
       which is the property the attribute already documents.

    `SkillSlot` is a frozen dataclass, so it is hashable and IS the key — no
    canonical-string round trip, and no chance of two different slots hashing
    alike because they serialise alike.
    """
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
    """The set-level batch entry point the solver calls.

    `members` is the set of 5; `leaders` is the candidate outfit cards, which
    need NOT be drawn from `members` — the leader is a slot outside the five and
    may be any card. Returns one `LeaderScore` per entry in `leaders` order.

    Everything that depends on the member set — gate resolution,
    `team_rate_up`, every member EV, and `base_stat_sum` — is computed once;
    only the leader's own outfit skill and the final `combine()` vary. The
    leader contributes no stats at all, so `base_stat_sum` is genuinely
    set-invariant.

    **A gated outfit cannot satisfy its own condition.** Each candidate leader's
    costume is resolved against the *members'* aggregates, so a Cute-gated
    outfit worn by a Cute card does not thereby count one Cute. That is also why
    the costume is resolved per leader here rather than read from
    `costume_by_card_id`: a leader outside the member set has no entry there.

    **Shared prefix, computed once per set.** `resolve_card_set_gates`
    resolves every card's normal slots and costume slot against one
    `TeamAggregates` snapshot. `base_stat_sum` is the integer sum of
    `card.stat_sum()` over `cards` — integer addition is exact and
    order-independent, so it is identical for every leader.

    `team_rate_up` is folded once, over every card's normal-slot resolved
    skills, through the same `_fold_activation_rate_up` `_score_team` uses.
    This is valid for every leader only because a COSTUME slot can never
    carry family `activation_rate_up`:
    `schema.FAMILY_REQUIRES_SLOT_KIND["activation_rate_up"] == "ACTIVE"`
    and `validate_skill_slot` rejects the family on any other slot kind at
    load time, so the one term that could vary by leader is
    structurally unreachable. This is asserted, not assumed: before
    folding, every card's costume slot is checked for family
    `activation_rate_up`, and a `ScoringError` naming the card is raised if
    one carries it — mirroring `_fold_activation_rate_up`'s own
    non-dispatch guard, since reaching that line would mean the record
    bypassed load-time validation, and fabricating a plausible number for
    it would be strictly worse than raising.

    This leader-invariance argument rests
    on no COSTUME slot carrying `activation_rate_up` as its PRINCIPAL
    family, which `validate_skill_slot` rejects (the check
    below reads `costume.family`, the principal, exactly like
    `_fold_activation_rate_up`'s own guard). A COSTUME slot's rider
    is never `activation_rate_up` and never reaches
    `_fold_activation_rate_up`, so it cannot break this invariance.

    Every other resolved skill instance (each card's normal slots, each
    card's costume slot) is then resolved once through the same shared
    `_resolve_skill`, weighted through `cfg.family_weights[...]`, and — for
    a `pool == "stat"` skill — has its target resolved once via
    `targets.resolve_target_card_ids` (target resolution depends
    only on the 6-card set, never on which card leads, exactly like gate
    resolution). The resulting `(weighted_rate, pool, target_ids,
    active_entry)` is cached keyed by `(card_id, slot_kind, source)` —
    skipping any `pool == "none"` entry, since its own contribution was
    already folded into `team_rate_up`, exactly as `_score_team`'s hot-loop
    branch does. `active_entry` is the ACTIVE Score UP's
    `(bucket, magnitude, occupancy, card_id)` and is `None` for every other
    skill: the E[max] pool is a maximum over magnitudes, so the two halves
    have to survive to the per-leader tail unmultiplied. They are
    leader-invariant for exactly the same reason `weighted_rate` is.

    **Per-leader tail.** For each candidate leader, the cached entries are
    walked in exactly `Team.effective_skill_instances()` order — that
    leader's normal slots in `SKILL_SLOT_KINDS` order, then that leader's
    costume entry if present, then the other five cards' normal slots in
    `cards` order — routed by the cached pool into a stat-rate list, a
    non-ACTIVE score-rate list, an `active_entries` list or a support list,
    exactly as `_score_team` routes them. `cfg.stacking_model.combine(...)`
    is then called over the *full* list, once per leader, **never
    incrementally folded**: both
    stacking models are incrementally composable (additive could cache the
    18-rate sum and add the costume term; multiplicative could cache the
    18-rate product and multiply — O(1) per leader), and that is
    deliberately not done here, because float addition and multiplication
    are not associative, so an incrementally folded total would not be
    bit-identical to recomputing `combine()` over all rates in
    `effective_skill_instances()` order. Cross-run determinism
    would survive incremental folding — same code, same order every time —
    but parity with `score_fast`/`score_with_breakdown` would not, since both
    recompute via `_score_team`. The cost of
    not folding is negligible: `combine()` over 19 floats is not where the
    time goes — the 18 EV computations were, and those are shared here.
    Tolerance-based parity (`math.isclose`) is deliberately not used: it would
    convert a provable structural guarantee into a numeric one and introduce
    an unverified tolerance constant."""
    card_set_gates = resolve_card_set_gates(members, cfg.run_state)

    # Checked over the CANDIDATE LEADERS, since they are the only cards whose
    # costume slot is ever used, and they may be outside the member set.
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
    team_rate_up = _fold_activation_rate_up(
        tuple(normal_resolved), cfg
    ) + _fold_rider_activation_rate_up(tuple(normal_resolved), cfg)

    # Target resolution depends only on the assembled 6-card
    # set (`cards`) and the skill's own target/effect_type — never on which
    # card leads — so it is resolved here, once per skill instance,
    # exactly like the weighted rate and pool it is cached alongside. A
    # `gate_not_met`/`data_missing` skill's weighted_rate is already 0.0,
    # so its cached target_ids is an empty frozenset — harmless either way,
    # since it never contributes.
    # The fourth slot is the `(bucket, magnitude, occupancy, interval, exclusive, duration, probability)` term for
    # a Score UP or a Score Support, and `None` for everything else — built HERE, from
    # the same `_lattice_entry` `_score_team` calls, because occupancy and
    # magnitude have to survive to the per-leader tail separately (a maximum
    # cannot be computed from their product). It is leader-invariant for the
    # same reason `weighted_rate` is.
    # The fifth slot is the entry's `order_target` (see `StatEntry`): the
    # skill's own target clause for an `applied` skill and `_FIXED_TARGET`
    # otherwise, so the per-leader tail can rebuild a `StatEntry` that
    # `_effective_stat_sum` may re-resolve under a candidate slot order. It is
    # leader-invariant for exactly the same reason `weighted_rate` is — a target
    # clause is a property of the skill, and which cards MATCH it is a property
    # of the member set, neither of which the leader touches.
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
            # A support target resolves exactly like a stat target and is
            # ranked by `STAT_NAMES` (i.e. by stat sum), because no single stat
            # is "the" stat a Score Support effect raises — the same reasoning
            # `targets.resolve_target_card_ids` already documents for
            # `all_stats`. Disclosed through TARGET_SELECTION, never silent.
            target_ids = targets.resolve_target_card_ids(
                resolved.skill.target,
                resolved.card_id,
                members,
                STAT_NAMES
                if pool == "support"
                else STAT_NAMES_BY_EFFECT_TYPE[resolved.skill.effect_type],
                cfg.target_selection_model,
                cfg.target_under_supply,
            )
        cache[(resolved.card_id, resolved.slot_kind, resolved.source)] = (
            weighted_rate,
            pool,
            target_ids,
            _bucket_term(resolved, cfg, team_rate_up, weight, state, pool, target_ids),
            resolved.skill.target if state == "applied" else _FIXED_TARGET,
        )
    # One resolved costume per CANDIDATE LEADER, against the members'
    # aggregates. Keyed by leader id: a leader outside the member set has no
    # entry in `costume_by_card_id`, and its gate must still see only the five.
    # ---- leader equivalence classes ------------------------------------
    # On a realistic pool the candidate leaders wear far fewer distinct
    # outfits than there are leaders (e.g. 25 outfits across 94 cards, 53 of
    # them sharing one). Since a leader contributes nothing but its outfit,
    # scoring every leader computes the same number many times. The per-leader
    # term dominates the exact search's cost, so collapsing it to one score per
    # outfit class cuts the evaluation count by roughly 3-4x.
    #
    # The classes are computed BEFORE the costume resolution below, so a
    # representative is also the only leader whose costume is resolved and
    # cached — the saving is the whole per-leader path, not just the tail.
    #
    # ⚠ This is an exact restatement, not an approximation. `_leader_class_key`
    # carries the three ways a leader's identity can still reach the score;
    # `tests/test_scoring.py` pins the expansion against a per-leader walk.
    member_ids = frozenset(card.id for card in members)
    board_leader_scoped_for_key: frozenset[str] = (
        frozenset() if cfg.board is None else cfg.board.leader_scoped_holomem
    )
    class_indices: dict[object, list[int]] = {}
    for index, candidate in enumerate(leaders):
        key = _leader_class_key(candidate, member_ids, board_leader_scoped_for_key)
        class_indices.setdefault(key, []).append(index)
    # Parallel: the representative that will be scored, and — keyed by its id —
    # every position in `leaders` that its answer is written to.
    representatives: list[Card] = []
    positions_by_representative: dict[str, list[int]] = {}
    for found in class_indices.values():
        representative = leaders[found[0]]
        representatives.append(representative)
        positions_by_representative[representative.id] = found

    # A leader's outfit is a TUPLE of resolved effects — the
    # principal, then its scored `score_support` rider when it has one
    # (`gates.resolve_leader_costume_skills`); each is cached under its own
    # `(card_id, slot_kind, source)` key and walked below in emission order.
    leader_costumes: dict[str, tuple[ResolvedSkill, ...]] = {}
    for card in representatives:
        costume_skills = resolve_leader_costume_skills(
            card, card_set_gates.aggregates, cfg.run_state
        )
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
            # Targets resolve over the MEMBERS: they are the only cards
            # carrying stats for an outfit buff to raise, and the only cards
            # carrying an ACTIVE slot for a Score Support outfit to multiply
            # — a leader outside the five contributes no ACTIVE of its
            # own, so its support has nothing of its own to raise.
            target_ids = targets.resolve_target_card_ids(
                costume.skill.target,
                costume.card_id,
                members,
                STAT_NAMES
                if pool == "support"
                else STAT_NAMES_BY_EFFECT_TYPE[costume.skill.effect_type],
                cfg.target_selection_model,
                cfg.target_under_supply,
            )
        cache[(costume.card_id, costume.slot_kind, costume.source)] = (
            weighted_rate,
            pool,
            target_ids,
            _bucket_term(costume, cfg, team_rate_up, weight, state, pool, target_ids),
            costume.skill.target if state == "applied" else _FIXED_TARGET,
        )

    # `_board_score_rate` reads only the five members, so the dict path is
    # leader-invariant and is hoisted out of the per-leader tail exactly like
    # `base_stat_sum` was. Bit-identical to `_score_team`'s own call because it
    # is the same function over the same tuple in the same order.
    #
    # The structural board (`cfg.board`) is NOT leader-invariant — the red
    # branch applies only while its holomem leads, and it is the largest branch
    # — so when one is configured the term is recomputed per leader inside the
    # loop below. `cfg.board is None` is the shipped default, so the hoist is
    # what runs without a board.
    hoisted_board_bonus = (
        (0.0, _board_score_rate(members, cfg), 0.0) if cfg.board is None else None
    )

    # ...but "not leader-invariant" is true of far fewer leaders than it looks.
    # `BoardModel.leader_scoped_holomem` names every holomem who owns a red
    # node; for ANY leader outside it the board term is bit-identical to the
    # `leader=None` one, by code path rather than by tolerance (the property is
    # argued in full on that attribute). So the invariant answer is computed
    # once per member SET here, and the loop below pays a `resolve` only for the
    # leaders who can actually move it — memoised by holomem, because `resolve`
    # reads no other attribute of the leader.
    #
    # On a typical board one character holds every red node, which turns one
    # `resolve` call per candidate leader into two per member set.
    board_leader_scoped: frozenset[str] = (
        frozenset() if cfg.board is None else cfg.board.leader_scoped_holomem
    )
    board_bonus_invariant = (
        None if cfg.board is None else _board_bonus(None, members, cfg)
    )
    board_bonus_by_holomem: dict[str, tuple[float, float, float]] = {}

    # Shared across every candidate leader, and that is the whole point: the
    # slot-order search enumerates orderings of the MEMBERS, which are fixed for
    # this call, so the enumeration is a per-set cost, not a per-leader one. In
    # `solver/exact.py` `leaders` is the entire pool, so without this the
    # 120-permutation walk would run once per pool card per member set.
    # `_best_order_stat_sum` keys it by the varying entries' target signature,
    # so a leader whose outfit is itself count-limited still gets a correct (if
    # separately enumerated) answer rather than a shared wrong one.
    orderings_cache: dict = {}

    # One score per CLASS, then expanded to every leader in it. `scores` is
    # indexed by position in `leaders`, so the returned order is unchanged —
    # callers index this result positionally against the tuple they passed.
    scores: list[float] = [0.0] * len(leaders)
    # ⚠ Still literally `for leader in <candidates>`, and that matters:
    # `test_batched_path_calls_combine_inside_the_per_leader_loop_never_before_it`
    # finds this loop by its target NAME to prove every `combine()` lies inside
    # it. The candidates are one per class instead of one per leader; the
    # containment invariant is unchanged, and it must stay checkable, so the
    # loop keeps its name.
    for leader in representatives:
        stat_rates: list[float] = []
        # See `StatEntry`.
        stat_entries: list[StatEntry] = []
        # Mirroring _score_team's three lists exactly.
        score_rates: list[float] = []
        ratio_entries: list[tuple[str, float, float, int, bool]] = []
        boost_entries: list[tuple[str, float, float, int, bool]] = []
        ratio_owners: list[str | None] = []
        boost_targets: list[frozenset[str] | None] = []

        # Exactly `Team.effective_skill_instances()` order: the leader's
        # outfit skill first, then every member's normal slots in member
        # order. The leader's OWN normal slots are never emitted — if
        # its card is also a member, they arrive through the member loop
        # below, once.
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
                    # The SPECIAL collapse, routed exactly as `_score_team`
                    # routes it and in the same `effective_skill_instances()`
                    # order, which is what keeps the two bit-identical.
                    boost_entries.append(bucket_term)
                    boost_targets.append(None)
                else:
                    ratio_entries.append(bucket_term)
                    ratio_owners.append(resolved.card_id)
            elif pool == "support":
                assert bucket_term is not None  # every support caches a term
                boost_entries.append(bucket_term)
                boost_targets.append(_support_targets(resolved, target_ids))

        # Read BEFORE the pool: `board_skill_effect_up` goes inside
        # `_score_up_pool`'s multiplier, so it must exist first. `_score_team`
        # orders these two the same way, which is what keeps parity.
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

        # The per-card, per-stat accumulation, through the SAME
        # `_effective_stat_sum` `_score_team` calls, so the two cannot diverge.
        # The leader is NOT iterated inside it: it contributes no stats.
        effective_stat_sum, _member_order = _effective_stat_sum(
            members, stat_entries, cfg, orderings_cache
        )

        total_score = (
            (effective_stat_sum + board_power)
            * (1.0 + combined_score_rate)
            * (1.0 + board_score_rate)
        )
        # The class's answer, written onto every leader in it. Assigning the
        # same float object is what makes the expansion bit-exact by
        # construction rather than by re-derivation.
        for index in positions_by_representative[leader.id]:
            scores[index] = total_score

    return tuple(
        LeaderScore(leader_id=leader.id, total_score=scores[index])
        for index, leader in enumerate(leaders)
    )


def team_taint(team: Team) -> tuple[str, ...]:
    """The team-level completeness indicator, derived *without
    scoring*: for every card in `Team.members`, each of that card's
    `Card.unknown_fields()` dotted paths joined to the card id, in card
    order then field order — mirroring `loader.py`'s `completeness_report`
    at team scope rather than reimplementing the taint primitive.

    A non-empty result is data for the output layer, never a score: a team
    this function flags must not be silently scored as if every field were
    known — "this card's data is incomplete" and "do not pull this card" are
    opposite messages, and rendering the former as the latter is the failure
    this exists to prevent. Discarding which field was missing here would make
    that report impossible to build later.

    Called once when building a `ScoreBreakdown`, never inside pass 2, and
    never from `score_fast`."""
    fields: list[str] = []
    for card in team.members:
        for field in card.unknown_fields():
            fields.append(f"{card.id}:{field}")
    return tuple(fields)


# Defined at module end (after `_score_team`, which calls it). Python binds the
# name at call time, so position does not matter.
def _overall_power_stat_subtotals(
    members: tuple[Card, ...],
    stat_entries: list,
    cfg: ScoringConfig,
    recipients: list,
) -> list[float]:
    """The DECK-SCREEN Overall Power stat block, per member, in `members` order.

    This is the DISPLAY twin of `_stat_sum` and it rounds where the server
    rounds. `_stat_sum` accumulates `base * (1 + combined)` in float64 — correct
    for the ranking objective, whose only requirement is a monotone ordering.
    The server's Overall Power instead pays, per (card, attribute):

        member_parameter (the integer base, summed exactly)
        + ceil( f32(combined_rate * base) )   <- the Outfit + Passive stat lines

    with the product formed and rounded in IEEE-754 binary32 — the same
    single-precision discipline `collection._f32` applies to the base-parameter
    chain and `board.power_bonus_for` applies to the Holomem Board's own ceiled
    stat line (which is why the board line reconciles bit-exact). Using the
    float64 sum here understates Overall Power because it folds the per-(card,
    stat) ceilings the server pays into one un-rounded product.

    Returned PER MEMBER so `member_power_up` can be paid its five per-slot
    ceilings (`_score_team`): each subtotal is that member's
    `MemberParameter + Outfit + Passive` stat contribution, to which the caller
    adds the member's Holomem Board power to form the Member Power-Up base.

    ⚠ DISPLAY ONLY. The ranking objective (`_stat_sum` -> `effective_stat_sum`
    -> `total_score`) is deliberately left in float64: it is validated correct
    and moving it would perturb rankings for no ranking benefit."""
    # Grouped by (slot_kind, card, attribute): the rates of one SOURCE LINE
    # (Outfit, Passive) on one card's one stat combine, then that source line's
    # ceiling is paid — because the server reports Outfit Skill and Passive
    # Skill as separate power lines and ceils each. Combining ALL sources into
    # one ceiling understates it (two source ceilings >= one over the sum);
    # ceiling every effect separately overstates it when two effects share a
    # source line. Grouping by source is the middle that reproduces the wire.
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
                    # The product is formed and rounded in single precision —
                    # `_f32` around the whole `rate * base`, the same `ss`-scalar
                    # discipline `collection.effective_stats` uses
                    # (`math.ceil(_f32(x * y))`) — then ceiled.
                    member_total += math.ceil(_f32(combined * base))
        subtotals.append(member_total)
    return subtotals


class _SlotOrderTieBreak:
    """DISPLAY-ONLY recipient selection: rank a count-target's matchers by TOTAL
    parameter and keep the top `count`, breaking an exact tie by SLOT ORDER (the
    order `candidates` were built in, which `targets.resolve_target_card_ids`
    fixes to `cards`/`team.members` order).

    The ranking model `top_n_by_stat` (`scoring_models.TopNByBuffedStat`)
    breaks the same tie the same way — a stable sort with no card_id key — so
    this class and it are behaviourally identical. It is kept distinct only so
    the deck-screen Overall Power assembly stays self-contained: it names its
    own tie-break rather than depending on how `cfg.target_selection_model` is
    configured. `sorted` is stable, so equal totals keep the `candidates` order
    they arrived in."""

    name = "slot_order_tie_break"

    def select(self, candidates: list[tuple[str, int]], count: int) -> list[str]:
        ordered = sorted(candidates, key=lambda candidate: -candidate[1])
        return [card_id for card_id, _total in ordered[:count]]


_SLOT_ORDER_TIE_BREAK = _SlotOrderTieBreak()


def _active_lattice(
    resolved: ResolvedSkill, cfg: ScoringConfig, team_rate_up: float, state: str
) -> tuple[float, float]:
    """`ActiveTerm`'s trailing `(duration, probability)`: the
    effect duration in seconds and the RESOLVED activation probability — the
    same `activation_rate_model.apply(tier, team_rate_up + board_rate)` product
    `_active_occupancy` folds into its occupancy, stated separately because the
    per-instant `normalized_lattice` law needs `P` on its own and the occupancy
    product has destroyed it. `(0.0, 0.0)` for anything that is not an applied
    ACTIVE with a numeric duration: probability 0 is how a term says "I have no
    lattice to place", and `NormalizedLatticeActiveSkill` then treats it exactly
    as `max` would. The `max`/`sum`/`max_lattice` combiners never read
    these two elements."""
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
    """`_bucket_entry` plus the trailing `(duration, probability)` lattice
    elements — the 7-tuple `ActiveTerm` the two pool builders (`_score_team`,
    `score_card_set` via `_bucket_term`) emit. `_bucket_entry` itself keeps its
    5-tuple shape for its other readers (`song.py`, `exact_row.py`, the shape
    tests)."""
    return _bucket_entry(resolved, cfg, team_rate_up, weight, state) + _active_lattice(
        resolved, cfg, team_rate_up, state
    )


# ---------------------------------------------------------------------------
# Per-object memos for two pure helpers the leaf calls ~30 times per team.
#
# They are bound HERE, at the end of the module, on purpose: the reference
# definitions above stay as they are and remain what each memo is checked
# against. Every caller in this module resolves the name at call time, so
# every caller gets the memoised binding.
# ---------------------------------------------------------------------------
_first_missing_field_reference = _first_missing_field
_missing_field_cache: dict[int, tuple] = {}


def _first_missing_field(resolved: ResolvedSkill) -> str | None:  # noqa: F811
    """`_first_missing_field_reference`, answered once per slot OBJECT. The
    answer is a function of the (frozen) slot alone; the slot is kept alive in
    the entry so a recycled `id()` cannot serve another slot's answer."""
    slot = resolved.skill
    hit = _missing_field_cache.get(id(slot))
    if hit is None or hit[0] is not slot:
        if len(_missing_field_cache) > 200_000:
            _missing_field_cache.clear()
        hit = (slot, _first_missing_field_reference(resolved))
        _missing_field_cache[id(slot)] = hit
    return hit[1]


_team_taint_reference = team_taint
_card_taint_cache: dict[int, tuple] = {}


def team_taint(team: Team) -> tuple[str, ...]:  # noqa: F811
    """`_team_taint_reference`, with each card's entries built once per card
    OBJECT — they are a function of the (frozen) card alone."""
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

