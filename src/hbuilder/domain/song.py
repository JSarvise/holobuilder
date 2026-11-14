"""The per-song scorer: what a team scores on one chart, as an absolute run score.

A second scoring entry point, never a parameter on `scoring.score_card_set` (that would
add a per-song term for every caller). `scoring.py` ranks with no song-length term:
`CalcScore` divides by `NotesPerfectCoefficientSum`, so length cancels. This module needs
a time axis, because three things do not cancel:

1. `LiveScoreMusicCoefficient = 1 + (Music.liveScoreCoefficientPermil/1000) x
   (difficultyLevel - 5)`, 0.968 .. 1.200 over the real charts.
2. SPECIAL fire times are written into the chart (channel `#mmm0B`, one cue per deck
   slot, five on every real chart): not probabilistic, not uniformly spread.
3. A SPECIAL Score UP is a `ScoreUpBoostSkillEffect` (`scoring._score_up_implements_boost`):
   it multiplies the ACTIVE maximum and is worth nothing where no ACTIVE is live.

The premise is full-perfect play (LIFE never leaves 1000, every note PERFECT), or the
AUTO column (`cfg.song_auto`). What remains random is the ACTIVE activation roll, whose
exact expectation is taken (`_ratio_at`). The whole model is in `docs/score-model.md`.

Entry points: `score_song` (`score_card_set`'s positional signature, so a solver need not
know which it calls), `score_song_detailed` (with the slot order and the deck power),
`overall_power_for`, `timeline`, and `load_charts` / `ChartLibrary` (defined, with the
chart data types, in `song_chart` and re-exported here).

The model reproduces a fully recorded run (801,087 on `m0001` easy) within the five-run
scatter, and rejects the alternative "SPECIAL adds" reading at +4.4 standard errors.
"""

from __future__ import annotations

import dataclasses
import itertools
import math
import os
import sys
from bisect import bisect_left
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hbuilder.domain import targets
from hbuilder.domain.card import Card, EffectTarget, SlotRider
from hbuilder.domain.collection import member_power_up
from hbuilder.domain.gates import (
    ResolvedSkill,
    resolve_card_set_gates,
    resolve_leader_costume_skills,
)
from hbuilder.domain.schema import STAT_NAMES

# `scoring.py`'s own arithmetic, imported rather than re-implemented, so the
# per-song scorer and the ranking scorer cannot drift apart (a rename fails at
# import time).
#
# This must be `_board_bonus`, not the narrower `_board_score_rate`, which reads
# only `cfg.board_score_bonus`: `_board_bonus` dispatches to `cfg.board` and
# returns all three board channels (`test_song_board_skill_effect_up.py`).
from hbuilder.domain.scoring import (
    _FIXED_TARGET,
    STAT_NAMES_BY_EFFECT_TYPE,
    LeaderScore,
    ScoringError,
    _as_rate,
    _board_bonus,
    _bucket_entry,
    _effective_stat_sum,
    _fold_activation_rate_up,
    _is_score_up_effect,
    _pool_for,
    _resolve_skill,
    _score_up_implements_boost,
    _score_up_pool,
    _support_targets,
    active_magnitude_by_card,
    deck_screen_unit_score,
    score_with_breakdown,
)
from hbuilder.domain.scoring_config import (
    ScoringConfig,
)

# The chart data types and chart loading live in `song_chart`; re-exported here so
# `song.X` keeps working for every importer and stays a module global for the hot code.
from hbuilder.domain.song_chart import _CHARTS_SCHEMA as _CHARTS_SCHEMA
from hbuilder.domain.song_chart import DEFAULT_PLATFORM as DEFAULT_PLATFORM
from hbuilder.domain.song_chart import FLICK_MOBILE_PERMIL as FLICK_MOBILE_PERMIL
from hbuilder.domain.song_chart import HIDDEN_POINT_PERMIL as HIDDEN_POINT_PERMIL
from hbuilder.domain.song_chart import LIVE_COMBO_TIERS as LIVE_COMBO_TIERS
from hbuilder.domain.song_chart import PLATFORMS as PLATFORMS
from hbuilder.domain.song_chart import SCORE_COEFFICIENT as SCORE_COEFFICIENT
from hbuilder.domain.song_chart import SPECIAL_CUE_COUNT as SPECIAL_CUE_COUNT
from hbuilder.domain.song_chart import ChartLibrary as ChartLibrary
from hbuilder.domain.song_chart import ChartNote as ChartNote
from hbuilder.domain.song_chart import SongChart as SongChart
from hbuilder.domain.song_chart import SongError as SongError
from hbuilder.domain.song_chart import SongScore as SongScore
from hbuilder.domain.song_chart import _chart_note as _chart_note
from hbuilder.domain.song_chart import charts_from_mapping as charts_from_mapping
from hbuilder.domain.song_chart import load_charts as load_charts
from hbuilder.domain.unknown import is_known

if TYPE_CHECKING:
    from hbuilder.domain.board import BoardModel

__all__ = [
    "ChartNote",
    "HIDDEN_POINT_PERMIL",
    "LIVE_COMBO_TIERS",
    "LeaderScore",
    "SCORE_COEFFICIENT",
    "SPECIAL_CUE_COUNT",
    "SongChart",
    "SongError",
    "SongScore",
    "ChartLibrary",
    "charts_from_mapping",
    "load_charts",
    "OverallPower",
    "cast_power_up",
    "overall_power_for",
    "song_overall_power",
    "score_song",
    "score_song_detailed",
]


# ---------------------------------------------------------------------------
# The timeline
# ---------------------------------------------------------------------------
#
# A `_Term` is one effect placed on the time axis: the same `(bucket, magnitude,
# occupancy)` triple `scoring._bucket_entry` builds (family weight, override gate
# and `state != "applied"` zeroing shared), with the run-average occupancy replaced
# by an instantaneous one, plus the windows it is live in:
#
#   ACTIVE   `p` while a window is open, `0` otherwise, where `p` is the
#            activation probability — so the per-instant `E[max]` is an exact
#            expectation (see `_ratio_at`), not a sample.
#   SPECIAL  `1.0` inside its cue window. Deterministic: the chart says when.
#   PASSIVE  `1.0` for the whole run.
#   COSTUME  `1.0` for the whole run.


@dataclass(frozen=True, slots=True)
class _Term:
    bucket: str
    magnitude: float
    occupancy: float
    # Whether this term joins `B` (the boost maximum) or the ratio maximum.
    # `scoring._score_up_implements_boost`'s decision for a Score UP; always
    # `True` for a `score_support`.
    is_boost: bool
    # `None` = live for the whole run. `()` = never live (gate not met / data
    # missing). Otherwise the ACTIVE cooldown lattice's windows.
    windows: tuple[tuple[float, float], ...] | None
    # For a SPECIAL slot: the member's index in `members`, and the effect's
    # duration. Its window is `[cue[slot_of[member]], cue + duration)`, so it is
    # the one term whose liveness depends on the slot order. `None` otherwise.
    special_member: int | None
    special_duration: float
    # `scoring._score_up_pool`'s per-member targeting: the card that owns a ratio
    # term, and for a `score_support` `None` when it is team-wide or the resolved
    # recipient set when it is count-targeted.
    owner: str | None = None
    targets: frozenset[str] | None = None
    # An ACTIVE term's activation probability, kept apart: the tier's base and the
    # rate it was raised by (`team_rate_up` + the board's Act.Prob), so a SPECIAL
    # rider can raise it further on the checks it covers (`_rider_terms`).
    active_base: float | None = None
    active_rate: float = 0.0
    # A rider term's coverage, per SPECIAL slot: the `[start, end)` seconds it is
    # live for when its host sits in that slot, or `None` (not live there).
    slot_cover: tuple[tuple[float, float] | None, ...] | None = None
    # A rider term's ACTIVE term (its index among the terms): two riders of one ACTIVE
    # term live at one note cover the same check, and only the stronger counts
    # (`_strongest_riders`)
    rider_base: int | None = None


def _active_windows(
    resolved: ResolvedSkill, cfg: ScoringConfig, song_seconds: float
) -> tuple[tuple[float, float], ...]:
    """Every window an ACTIVE skill can open, on its cooldown lattice.

    The clock starts at measure 0 of the beatmap, not at the first note, so check `k`
    sits at `k * interval` for `k = 1, 2, ...` (no check at `t = 0`).

    The Holomem Board shortens a cooldown by dividing (`interval / (1 + reduction)`),
    keyed by holomem, as `scoring._active_occupancy` does; that function returns a run
    average with the lattice collapsed, so the expression is restated here. Duration
    never reaches the cooldown in the real data, so a window cannot overlap the next
    check of the same skill.

    The window end is not clamped to `song_seconds` (unlike the display in
    `tools/refresh/chart_timeline.py`): windows are half-open `[start, end)`, so
    clamping would push the song's last note, the one with the highest combo
    multiplier, out of a window that covers it."""
    return _lattice_windows(*_active_lattice(resolved, cfg), song_seconds)


def _active_lattice(resolved: ResolvedSkill, cfg: ScoringConfig) -> tuple[float, float]:
    """`(interval, duration)` of an ACTIVE's cooldown lattice (`_active_windows`): no chart
    moves either."""
    # the board's Act.CD nodes (`uptime_for`) add to the hand-supplied reduction, as
    # in `scoring._active_occupancy`, so the two scorers agree on the interval
    board_cooldown = cfg.board.uptime_for(resolved.member)[1] if cfg.board is not None else 0.0
    reduction = cfg.active_cooldown_reduction.get(resolved.member, 0.0) + board_cooldown
    interval = float(resolved.skill.interval) / (1.0 + reduction)  # type: ignore[arg-type]
    duration = float(resolved.skill.duration)  # type: ignore[arg-type]
    return interval, duration


# `(interval, duration, song_seconds) -> the windows`, one tuple OBJECT per
# lattice: a search asks for the same few lattices millions of times, and the
# shared object lets `_window_note_ranges` key on identity.
_lattice_windows_cache: dict[tuple[float, float, float], tuple] = {}


def _lattice_windows(
    interval: float, duration: float, song_seconds: float
) -> tuple[tuple[float, float], ...]:
    key = (interval, duration, song_seconds)
    hit = _lattice_windows_cache.get(key)
    if hit is None:
        windows: list[tuple[float, float]] = []
        k = 1
        while k * interval <= song_seconds:
            start = k * interval
            windows.append((start, start + duration))
            k += 1
        if len(_lattice_windows_cache) > 65536:
            _lattice_windows_cache.clear()
        hit = tuple(windows)
        _lattice_windows_cache[key] = hit
    return hit


# `(times, windows) -> ((low, high), ...)`: the half-open note-INDEX range each
# window covers on one chart — the per-(skill lattice, song) table. Keyed by
# the identity of both tuples, each kept alive in the entry so a recycled
# `id()` cannot serve another chart's or another lattice's ranges.
_window_note_ranges_cache: dict[tuple[int, int], tuple] = {}


def _window_note_ranges(
    times: tuple[float, ...], windows: tuple[tuple[float, float], ...]
) -> tuple[tuple[int, int], ...]:
    key = (id(times), id(windows))
    hit = _window_note_ranges_cache.get(key)
    if hit is None or hit[0] is not times or hit[1] is not windows:
        if len(_window_note_ranges_cache) > 65536:
            _window_note_ranges_cache.clear()
        hit = (
            times,
            windows,
            tuple((bisect_left(times, start), bisect_left(times, end)) for start, end in windows),
        )
        _window_note_ranges_cache[key] = hit
    return hit[2]


def _term_for(
    resolved: ResolvedSkill,
    cfg: ScoringConfig,
    team_rate_up: float,
    state: str,
    song_seconds: float,
    member_index: int | None,
    targets: frozenset[str] | None = None,
) -> _Term:
    """One resolved Score UP or Score Support, placed on the time axis."""
    weight = cfg.family_weights[resolved.family]
    # The fourth field, `ActiveTerm`'s `exclusive`, is discarded on purpose: it is
    # the run-average path's corpus-level licence to sum the SPECIAL bucket. Here
    # the actual cue times are on the axis, so on the 0.64 % of cue pairs a long
    # SPECIAL can bridge, the overlap is seen and the maximum taken (`_ratio_at`
    # emits `exclusive=False` for every term).
    bucket, magnitude, _run_average_occupancy, _interval, _corpus_exclusive = _bucket_entry(
        resolved, cfg, team_rate_up, weight, state
    )
    is_boost = _pool_for(resolved) == "support" or _score_up_implements_boost(resolved)
    if state != "applied":
        # `_bucket_entry` already zeroed the magnitude; the occupancy is zeroed too,
        # or the zero term would still consume the `E[max]` walk's remaining
        # probability and let a gate that did not fire suppress a skill that did.
        return _Term(bucket, 0.0, 0.0, is_boost, (), None, 0.0, resolved.card_id, targets)

    kind = resolved.skill.kind
    if kind == "ACTIVE":
        # the board's activation-rate (Act.Prob) term joins the proc chance, as in
        # `scoring._active_occupancy` (`apply(base_prob, team_rate_up + board_rate)`)
        board_rate = cfg.board.uptime_for(resolved.member)[0] if cfg.board is not None else 0.0
        base_probability = cfg.probability_tiers[resolved.skill.probability_tier]
        probability = cfg.activation_rate_model.apply(base_probability, team_rate_up + board_rate)
        # `active_all_fire` is the CEILING: every active triggers. Lift the proc
        # chance to a full 1.0 — but never BELOW a rate-boosted probability the
        # model already put over 1.0, so the ceiling can't dip under the estimate.
        if cfg.active_all_fire:
            probability = max(1.0, probability)
        return _Term(
            bucket,
            magnitude,
            probability,
            is_boost,
            _active_windows(resolved, cfg, song_seconds),
            None,
            0.0,
            resolved.card_id,
            targets,
            None if cfg.active_all_fire else base_probability,
            team_rate_up + board_rate,
        )
    if kind == "SPECIAL":
        if member_index is None:
            raise ScoringError(
                "a SPECIAL slot must belong to one of the five members — its "
                "window is a deck slot's chart cue, and the leader has no slot",
                card_id=resolved.card_id,
                slot_kind=kind,
            )
        # Occupancy 1.0, not the run-average `scoring._special_occupancy`: the chart
        # states the fire time, so on a concrete song a SPECIAL is certain.
        return _Term(
            bucket,
            magnitude,
            1.0,
            is_boost,
            None,
            member_index,
            float(resolved.skill.duration),  # type: ignore[arg-type]
            resolved.card_id,
            targets,
        )
    # PASSIVE and COSTUME: live for the whole run.
    return _Term(bucket, magnitude, 1.0, is_boost, None, None, 0.0, resolved.card_id, targets)


def _combo_split(
    resolved: ResolvedSkill, cfg: ScoringConfig, term: _Term, times: tuple[float, ...]
) -> list[_Term]:
    """An ACTIVE term whose override is COMBO-gated ("With a Combo of 40 or more, Score
    UP 100 %"), split by window: the override's magnitude on the checks the chart's own
    combo has reached (the notes before the check, full-combo premise), the base on the
    rest. The value snapshots at the check, as the game's effect is registered then.
    `gates` resolves the override once from the static `run_state.combo` (100), which
    would grant it from the first check, so the combo is re-read at each check here.
    AUTO keeps the static premise (the combo never accumulates there). The two parts
    never share a note (a skill's windows do not overlap)."""
    rule = _combo_override(resolved, cfg) if term.windows else None
    if rule is None:
        return [term]
    high, base, applied, condition = rule

    def reached(start: float) -> bool:
        combo = bisect_left(times, start)
        return combo <= condition.count if condition.operator == "lte" else combo >= condition.count

    windows = term.windows or ()
    hit = tuple(w for w in windows if reached(w[0]))
    miss = tuple(w for w in windows if not reached(w[0]))
    out = []
    if hit:
        out.append(dataclasses.replace(term, magnitude=term.magnitude * high / applied, windows=hit))
    if miss:
        out.append(dataclasses.replace(term, magnitude=term.magnitude * base / applied, windows=miss))
    return out


def _combo_override(resolved: ResolvedSkill, cfg: ScoringConfig) -> tuple[float, float, float, Any] | None:
    """`(high, base, applied, its condition)` of an ACTIVE whose override is COMBO-gated, as
    `_combo_split` splits it (the magnitudes the checks reached and missed take, over the one
    the term carries); None for any other skill, or under AUTO / a no-combo premise."""
    override = resolved.skill.override
    condition = getattr(override, "condition", None)
    if (
        cfg.song_auto
        or cfg.run_state.combo <= 0  # a premise of no combo at all, as AUTO
        or resolved.skill.kind != "ACTIVE"
        or getattr(condition, "kind", None) != "run_state"
        or condition.target != "combo"  # type: ignore[union-attr]
        or not is_known(override.magnitude)  # type: ignore[attr-defined]
    ):
        return None
    base, high = (
        float(resolved.skill.magnitude),  # type: ignore[arg-type]
        max(float(override.magnitude), float(resolved.skill.magnitude)),  # type: ignore[attr-defined, arg-type]
    )
    applied = high if (resolved.override_gate is not None and resolved.override_gate.fired) else base
    if applied <= 0.0 or high == base:
        return None
    return high, base, applied, condition


def _rider_gate_at(resolved: ResolvedSkill, combo: int, cfg: ScoringConfig) -> bool:
    """Whether a SPECIAL's rider fires at a cue reached with `combo` notes behind
    it: a COMBO gate against the chart's own count there (full-combo premise), any
    other gate as resolved for the run (`gates`, the static run state). AUTO, or a
    premise of no combo at all, keeps the static gate for a combo gate too: the combo
    never accumulates there, as `_combo_split` reads it for an ACTIVE override."""
    kind, count = _rider_gate(resolved, cfg)
    if kind == "lte":
        return combo <= count
    if kind == "gte":
        return combo >= count
    return kind == "on"


def _rider_gate(resolved: ResolvedSkill, cfg: ScoringConfig) -> tuple[str, Any]:
    """A SPECIAL rider's gate as no chart moves it: `("lte" | "gte", count)` for a COMBO gate
    read at each cue (`_rider_gate_at`), else `("on" | "off", None)` as resolved for the run."""
    condition = resolved.skill.rider.condition  # type: ignore[attr-defined]
    if (
        getattr(condition, "kind", None) == "run_state"
        and condition.target == "combo"
        and not cfg.song_auto
        and cfg.run_state.combo > 0
    ):
        return ("lte" if condition.operator == "lte" else "gte"), condition.count
    return ("on" if resolved.rider_gate is None or resolved.rider_gate.fired else "off"), None


def _rider_terms(
    normal_resolved,
    member_index_by_id: Mapping[str, int],
    member_terms: list[_Term],
    cfg: ScoringConfig,
    chart: SongChart,
    times: tuple[float, ...],
) -> list[_Term]:
    """The SPECIAL activation-rate riders, on the time axis.

    About 30 cards' SPECIAL carries a second in-run effect,
    `live_active_skill_activation_probability_up_permil_up` (350-550 permil), live for the
    SPECIAL's own duration. Every ACTIVE check in that window rolls
    `trunc(base x (1 + (maxACTIVE + maxSPECIAL + extra)/1000))`: the rider is
    `maxSPECIAL`, deck-wide (it raises every member's ACTIVE) and a maximum (two live
    riders do not stack). The run-average path folds it time-averaged
    (`scoring._fold_rider_activation_rate_up`); on a chart the cue times are known.

    A boosted check is the same roll with `p'` for `p`. Each becomes one extra term:
    the ACTIVE term's bucket, magnitude and owner, occupancy `q = (p' - p)/(1 - p)`,
    live on that window only when the rider's host sits in a slot whose SPECIAL window
    holds the check. In `E[max]` two terms of one magnitude and bucket with `p` and `q`
    are exactly one term with `1 - (1-p)(1-q) = p'`, so this is exact, and slot-dependent
    like a SPECIAL (`special_member` = the host). One term per (ACTIVE, host, k-th
    covered window), since a term's coverage is one range per slot.

    Two riders live on one check do not combine: the game rolls the check with the larger
    (`maxSPECIAL`), so each rider term names its ACTIVE term (`rider_base`) and only the
    strongest live one counts (`_strongest_riders`, `_dominated_riders`). SPECIAL windows
    rarely overlap (about 0.64 % of cue pairs)."""
    out: list[_Term] = []
    for member, rate, spans in _rider_hosts(normal_resolved, member_index_by_id, cfg, chart, times):
        for base, term in enumerate(member_terms):
            if term.active_base is None or not term.windows or term.occupancy <= 0.0:
                continue
            boosted = cfg.activation_rate_model.apply(term.active_base, term.active_rate + rate)
            if term.occupancy >= 1.0 or boosted <= term.occupancy:
                continue
            q = (boosted - term.occupancy) / (1.0 - term.occupancy)
            per_slot = [
                [w for w in term.windows if span is not None and span[0] <= w[0] < span[1]] for span in spans
            ]
            for k in range(max(len(ws) for ws in per_slot)):
                out.append(
                    _Term(
                        term.bucket,
                        term.magnitude,
                        q,
                        term.is_boost,
                        None,
                        member,
                        0.0,
                        term.owner,
                        term.targets,
                        slot_cover=tuple(ws[k] if k < len(ws) else None for ws in per_slot),
                        rider_base=base,
                    )
                )
    return out


def _strongest_riders(live: list[_Term]) -> list[_Term]:
    """`live` with only the strongest SPECIAL rider of each ACTIVE term: the game rolls a
    check at `base x (1 + rate + the LARGEST live rider)` (`CalcActivationProbabilityPermil`
    takes the SPECIALs' maximum). Two rider terms of one ACTIVE term live at one note cover
    the same check, since an ACTIVE's windows never overlap. Ties keep the first."""
    best: dict[int, _Term] = {}
    riders = 0
    for t in live:
        if t.rider_base is None:
            continue
        riders += 1
        held = best.get(t.rider_base)
        if held is None or t.occupancy > held.occupancy:
            best[t.rider_base] = t
    if len(best) == riders:
        return live
    keep = {id(t) for t in best.values()}
    return [t for t in live if t.rider_base is None or id(t) in keep]


def _dominated_riders(terms: list[_Term], slot_of_member: Mapping[int, int]) -> set[int]:
    """The bits of the rider terms another rider of the same ACTIVE term outranks on the same
    window, in one fixed slot order (`slot_of_member`: member index -> slot): there each
    rider term covers one window, and only the strongest rider counts on its check."""
    best: dict[tuple, int] = {}
    live: list[int] = []
    for bit, t in enumerate(terms):
        if t.rider_base is None or t.slot_cover is None or t.occupancy <= 0.0:
            continue
        slot = slot_of_member[t.special_member]  # type: ignore[index]
        span = t.slot_cover[slot] if slot < len(t.slot_cover) else None
        if span is None:
            continue
        live.append(bit)
        key = (t.rider_base, span)
        if key not in best or t.occupancy > terms[best[key]].occupancy:
            best[key] = bit
    keep = set(best.values())
    return {bit for bit in live if bit not in keep}


def _riders_share_a_check(terms: list[_Term]) -> bool:
    """Whether two riders of one ACTIVE term, on different hosts, can cover the same window
    in some slot order (the compiled walk knows no `_strongest_riders`, so such a team is
    walked in Python)."""
    by_base: dict[int, list[_Term]] = {}
    for t in terms:
        if t.rider_base is not None and t.slot_cover is not None:
            by_base.setdefault(t.rider_base, []).append(t)
    for ts in by_base.values():
        for i, a in enumerate(ts):
            for b in ts[i + 1 :]:
                if a.special_member == b.special_member:
                    continue
                for sa, wa in enumerate(a.slot_cover or ()):
                    if wa is not None and any(
                        sb != sa and wb == wa for sb, wb in enumerate(b.slot_cover or ())
                    ):
                        return True
    return False


def _rider_hosts(
    normal_resolved,
    member_index_by_id: Mapping[str, int],
    cfg: ScoringConfig,
    chart: SongChart,
    times: tuple[float, ...],
) -> list[tuple[int, float, list]]:
    """`(host member index, rider rate, [(start, end) or None per cue])` of every SPECIAL
    activation-rate rider that can fire: its SPECIAL ungated, its own gate read at each
    cue (`_rider_gate_at`). The k-th span is live when the host sits in slot k. Shared by
    the scorer (`_rider_terms`) and the timeline, so both say the same about when a
    rider raises the chance."""
    out = []
    for host in _rider_host_skills(normal_resolved):
        duration = float(host.skill.duration)  # type: ignore[arg-type]
        spans = [
            (cue, cue + duration) if _rider_gate_at(host, bisect_left(times, cue), cfg) else None
            for cue in chart.special_cues
        ]
        rider_rate = _as_rate(float(host.skill.rider.magnitude))  # type: ignore[attr-defined]
        out.append((member_index_by_id[host.card_id], rider_rate, spans))
    return out


def _rider_host_skills(normal_resolved) -> list[ResolvedSkill]:
    """The SPECIALs whose activation-rate rider can fire (`_rider_hosts`), in skill order."""
    hosts = []
    for resolved in normal_resolved:
        rider = resolved.skill.rider
        if (
            resolved.skill.kind != "SPECIAL"
            or not isinstance(rider, SlotRider)
            or rider.family != "activation_rate_up"
            or not is_known(rider.magnitude)
            or not is_known(rider.condition)
            or not is_known(resolved.skill.duration)
        ):
            continue
        if resolved.gate is not None and not resolved.gate.fired:
            continue  # the SPECIAL itself is gated off
        hosts.append(resolved)
    return hosts


def _ratio_at(
    live_ratio: list[_Term],
    live_boost: list[_Term],
    cfg: ScoringConfig,
    skill_effect_up: float = 0.0,
) -> float:
    """`SkillScoreUpRatio` at one instant, through `scoring._score_up_pool`.

    The whole per-song model is this one substitution: `_score_up_pool` is handed the
    same `(bucket, magnitude, occupancy)` triples the ranking scorer builds, with
    `occupancy` meaning "is this effect live at this tick, and with what probability"
    instead of "what fraction of the run is it live". Everything downstream (both
    `E[max]` calls, the `B` multiplier, the SPECIAL collapse) is `scoring.py`'s.

    This is exact, not a simulation: at an instant each live ACTIVE is an independent
    Bernoulli(p) and `ExpectedMaximumActiveSkill` computes `E[max]` exactly, and the
    score is linear in the ratio, so summing per-note expectations gives the exact
    expectation of the run total (modulo the per-note `ceil`).

    `skill_effect_up` is the Holomem Board's `SkillEffectUpPermilUp`, in the slot
    `CalcScoreUpRatio` puts it: inside the `(1 + SkillEffectUpPermilUp/1000 + B)`
    parenthesis that multiplies the ACTIVE pool, never a summand of the combined rate.

    `exclusive` is `False` on every term: the caller has already filtered
    `live_ratio`/`live_boost` to the effects live at this instant, so the ordinary walk
    takes the true maximum (two SPECIALs live together give the bigger one), where
    `exclusive=True` would sum them."""
    # `interval` is 0 on every term: the per-song scorer runs under the default
    # `max` combiner, which ignores it; 0 keeps the 5-tuple `_score_up_pool` unpacks.
    return _score_up_pool(
        [(term.bucket, term.magnitude, term.occupancy, 0, False) for term in live_ratio],
        [(term.bucket, term.magnitude, term.occupancy, 0, False) for term in live_boost],
        cfg,
        skill_effect_up,
        [term.owner for term in live_ratio],
        [term.targets for term in live_boost],
    )


# ---------------------------------------------------------------------------
# Deck power for the ABSOLUTE path
# ---------------------------------------------------------------------------


def overall_power_for(members: tuple[Card, ...], leader: Card, cfg: ScoringConfig) -> OverallPower:
    """The deck screen's own **Overall Power** for one (leader, members) pair —
    the right `deck_power` for an ABSOLUTE per-song score.

    `score_song_detailed`'s default is `stat_sum + board_power`, the modelled stat pool.
    Overall Power also carries the two account scalars (Memory Bonus and Member
    Power-Up) and the float32-ceiling stat block; it is the quantity the game multiplies,
    and this delegates to the scorer that matches the server bit-exact.

    It is a function of the leader (her board power and leader-scoped stat effects), so
    pass it per leader, not as a scalar computed for the first one:

        score_song_detailed(members, leaders, chart, cfg,
                            deck_power=lambda ldr: overall_power_for(members, ldr, cfg))

    The song-less figure (the deck screen with no song chosen), as an `OverallPower`
    that also carries the per-member Member Power-Up bases, so `score_song_detailed` can
    raise it to the song's own Overall Power (`song_overall_power`)."""
    from hbuilder.domain.team import Team

    breakdown = score_with_breakdown(Team(leader=leader, members=members), cfg)
    account = getattr(cfg, "account", None)
    return OverallPower(
        deck_screen_unit_score(breakdown).overall_power,
        power_up_bases=breakdown.power_up_bases,
        member_ids=tuple(card.id for card in members),
        power_up_permyriad=None if account is None else account.power_up_permyriad,
    )


def overall_power_board_key(members: tuple[Card, ...], leader: Card, board: BoardModel) -> tuple:
    """Everything of `board` that `overall_power_for(members, leader, cfg)` reads through
    `cfg.board`: two boards with the same key give the team the same `OverallPower`.

    The team scorer reaches the board three ways. Each skill's uptime reads
    `board.uptime_for` of its own holomem (a member's, or the leader's for an outfit skill).
    The Holomem Board line of Overall Power is `board.power_bonus_for(members, leader's
    holomem)`, and each member's Member Power-Up base adds that member's own
    `power_bonus_for`. The board's score bonus and its skill-effect-up enter the score
    rates only, never Overall Power, so they are not part of the key.

    A team is scored on one board per singer context, and those boards often differ only
    in nodes that pay on the song (a singer's own song-score nodes): with this key the
    contexts whose boards agree on the power share one Overall Power."""
    holomem = leader.member
    return (
        tuple(board.uptime_for(card.member) for card in (*members, leader)),
        tuple(board.power_bonus_for((card,), holomem) for card in members),
        board.power_bonus_for(members, holomem),
    )


@dataclass(frozen=True, slots=True)
class OverallPower:
    """A song-less Overall Power and each member's Member Power-Up base.

    `value` is the deck screen's Overall Power. The other fields let a song add
    its singer-conditional board power the way the deck screen does: that power
    joins each member's power-up base and the power-up's per-member ceilings are
    paid again (`cast_power_up`).

    A type of its own, not a float: a bare float carries no bases and would silently
    pay 0 extra Power-Up on a song whose singer holds a conditional stat node.
    `overall_power_for` builds the real one; `OverallPower.fixed` asks for a raw
    figure and declares no bases."""

    value: float
    power_up_bases: tuple[float, ...] | None = None
    member_ids: tuple[str, ...] = ()
    power_up_permyriad: int | None = None

    def __post_init__(self) -> None:
        if isinstance(self.value, bool) or not isinstance(self.value, (int, float)):
            raise TypeError(f"OverallPower.value must be a number, got {self.value!r}")
        object.__setattr__(self, "value", float(self.value))

    @classmethod
    def fixed(cls, value: float) -> OverallPower:
        """A fixed raw deck power with no Member Power-Up bases.

        `cast_power_up` pays 0 on it, so a song's singer-conditional board power
        adds only its own stat power. For tests and studies that score at a
        chosen power on purpose; a real team's power is `overall_power_for`."""
        return cls(value)

    def __float__(self) -> float:
        return self.value


def _not_overall_power(where: str, got: object) -> TypeError:
    """The error for a bare number (or anything else) where an `OverallPower` belongs."""
    return TypeError(
        f"{where} must be an OverallPower, got {type(got).__name__} {got!r}. Pass "
        f"overall_power_for(members, leader, cfg) for a team's deck screen Overall "
        f"Power, or OverallPower.fixed(value) for a fixed raw figure with no Member "
        f"Power-Up bases."
    )


def cast_power_up(
    power: OverallPower,
    members: Sequence[Card],
    leader_holomem: str | None,
    cfg: ScoringConfig,
    singers: Sequence[str],
) -> int:
    """The Member Power-Up a song's singer-conditional board power adds.

    The deck screen pays Member Power-Up per member over
    `MemberParameter + Outfit + Passive + Holomem Board`; on a song whose singer
    holds a conditional stat node (R-044/062/063), that node's power is part of
    the Holomem Board line, so it is part of the base too.
    `power` is an `OverallPower` (from `overall_power_for`) for the same
    members; an `OverallPower.fixed` carries no bases and gets 0, and anything
    that is not an `OverallPower` raises `TypeError`."""
    if not isinstance(power, OverallPower):
        raise _not_overall_power("cast_power_up's power", power)
    bases = power.power_up_bases
    permyriad = power.power_up_permyriad
    if not bases or permyriad is None or cfg.board is None or not singers:
        return 0
    base_by_id = dict(zip(power.member_ids, bases))
    if any(card.id not in base_by_id for card in members):
        return 0
    old: list[float] = []
    new: list[float] = []
    for card in members:
        base = base_by_id[card.id]
        old.append(base)
        new.append(base + cfg.board.conditional_stat_power_for((card,), leader_holomem, singers))
    if new == old:
        return 0
    return (member_power_up(new, permyriad) or 0) - (member_power_up(old, permyriad) or 0)


def song_overall_power(
    members: tuple[Card, ...],
    leader: Card,
    cfg: ScoringConfig,
    chart: SongChart | None,
) -> float:
    """The deck screen's Overall Power with `chart` chosen: the song-less figure
    plus the singers' conditional board power (one ceiling per (card, stat)
    with the unconditional rate) plus the Member Power-Up that power earns.
    The song-less figure for no chart or a chart with no credited cast."""
    power = overall_power_for(members, leader, cfg)
    singers = tuple(getattr(chart, "singers", None) or ()) if chart is not None else ()
    if cfg.board is None or not singers:
        return power.value
    holomem = leader.member
    cast = cfg.board.conditional_stat_power_for(members, holomem, singers)
    return power.value + cast + cast_power_up(power, members, holomem, cfg, singers)


# ---------------------------------------------------------------------------
# The entry point
# ---------------------------------------------------------------------------


def score_song(
    members: tuple[Card, ...],
    leaders: tuple[Card, ...],
    chart: SongChart,
    cfg: ScoringConfig,
    *,
    deck_power: OverallPower | Callable[[Card], OverallPower] | None = None,
    auto: bool | None = None,
) -> tuple[LeaderScore, ...]:
    """The per-song sibling of `scoring.score_card_set`, with the identical
    positional signature so a solver can call either without knowing which.

    Returns one `LeaderScore` per entry in `leaders` order, whose `total_score`
    is an **absolute predicted run score**, not a ranking quantity — the sum
    over the chart's notes of

        ceil( DeckPower
            x ScoreCoefficient                     # 2.300
            x LiveScoreMusicCoefficient            # chart
            / NotesPerfectCoefficientSum           # chart
            x JudgementCoefficient[PERFECT][type]  # full-perfect premise
            x (1 + ComboCoefficient)
            x (1 + SkillScoreUpRatio(t))           # <- the time axis
            x (1 + ScoreUpBonusRatioBySkillTree) )

    maximised over the 120 slot orders, which decide the chart cue that fires each
    member's SPECIAL (see `score_song_detailed`). The `ceil` is per note, as in
    `CalcScore`.

    `deck_power` defaults to the modelled stat pool `score_card_set` uses
    (`_effective_stat_sum`), not the game's deck power: for an absolute number pass
    the deck screen's Overall Power (`overall_power_for`, per leader). A bare number
    raises `TypeError`; a fixed raw figure is `OverallPower.fixed(value)`.

    Use `score_song_detailed` when the slot order matters to the caller."""
    return tuple(
        LeaderScore(leader_id=score.leader_id, total_score=score.total_score)
        for score in score_song_detailed(members, leaders, chart, cfg, deck_power=deck_power, auto=auto)
    )


def timeline(
    members: tuple[Card, ...],
    chart: SongChart,
    cfg: ScoringConfig,
    member_order: tuple[str, ...],
) -> dict:
    """When each of the five members' skills is actually live, on one axis.

    Five lanes, the chart's own SPECIAL cues, and each ACTIVE's cooldown
    lattice.

    Everything is read off the functions `score_song` scores with (`_active_windows`,
    `chart.special_cues`, `_rider_hosts`), so the picture cannot disagree with the
    score. `member_order` is the slot order: lane `i` is the member the chart's `i`-th
    cue fires.

    Returns plain data. Times are seconds from measure 0, and windows are half-open
    `[start, end)`, unclamped, exactly as `_active_windows` produces them."""
    by_id = {card.id: card for card in members}
    gates = resolve_card_set_gates(members, cfg.run_state)
    song_seconds = chart.notes[-1].time if chart.notes else 0.0
    times = _chart_times(chart)
    # each check's chance as the scorer rolls it: the ACTIVE term's own probability (the
    # team's and the board's Activation Rate UP folded in) and, inside a SPECIAL rider's
    # window, the boosted roll -- the same hosts, gates and spans `_rider_terms` scores
    normal = [r for cid in member_order for r in gates.normal_by_card_id.get(cid, ())]
    team_rate_up = _fold_activation_rate_up(tuple(normal), cfg)
    slot_of = {cid: slot for slot, cid in enumerate(member_order)}
    riders = _rider_hosts(normal, slot_of, cfg, chart, times)

    def chances(resolved, card_id):
        _rate, state, _missing = _resolve_skill(resolved, cfg, team_rate_up)
        term = _term_for(resolved, cfg, team_rate_up, state, song_seconds, slot_of[card_id])

        def at(start: float) -> tuple[float, list[str]]:
            p = float(term.occupancy)
            if term.active_base is None or p >= 1.0:
                return p, []
            # the check rolls with the LARGEST live rider (two never combine)
            best, hosts = p, []
            for slot, rate, spans in riders:
                span = spans[slot] if slot < len(spans) else None
                if span is None or not span[0] <= start < span[1]:
                    continue
                boosted = cfg.activation_rate_model.apply(term.active_base, term.active_rate + rate)
                if boosted > best:
                    best, hosts = boosted, [by_id[member_order[slot]].member]
            return best, hosts

        return at

    lanes = []
    for slot, card_id in enumerate(member_order):
        card = by_id.get(card_id)
        if card is None:
            continue
        cue = chart.special_cues[slot] if slot < len(chart.special_cues) else None
        active: list[dict] = []
        special: list[dict] = []
        for resolved in gates.normal_by_card_id.get(card_id, ()):
            kind = resolved.skill.kind
            if kind == "ACTIVE":
                chance_at = chances(resolved, card_id)
                for start, end in _active_windows(resolved, cfg, song_seconds):
                    probability, rider_from = chance_at(start)
                    active.append(
                        {
                            "start": start,
                            "end": end,
                            # An ACTIVE window is a chance, a SPECIAL window a fact.
                            # `probability`: this check's chance as scored (the team's and
                            # the board's rate-up, and `rider_from`'s SPECIAL riders live at
                            # its start); `probability_base`: the card's tier alone.
                            "probability": probability,
                            "probability_base": _activation_probability(resolved, cfg),
                            "rider_from": rider_from,
                            "magnitude": float(resolved.skill.magnitude or 0.0),
                        }
                    )
            elif kind == "SPECIAL" and cue is not None:
                duration = float(resolved.skill.duration or 0.0)
                special.append(
                    {
                        "start": cue,
                        "end": cue + duration,
                        "magnitude": float(resolved.skill.magnitude or 0.0),
                    }
                )
        lanes.append(
            {
                "slot": slot + 1,
                "card_id": card_id,
                "member": card.member,
                "cue": cue,
                "active": active,
                "special": special,
            }
        )

    return {
        "music_id": chart.music_id,
        "difficulty": chart.difficulty,
        "song_seconds": song_seconds,
        "notes": len(chart.notes),
        "special_cues": list(chart.special_cues),
        # where the score is: the chart's own note times binned per second, so a
        # drawing can show the density without a second read of charts.json
        "notes_per_second": notes_per_second(tuple(n.time for n in chart.notes)),
        "lanes": lanes,
    }


def notes_per_second(note_times: Sequence[float]) -> list[int]:
    """Notes per 1-second bin, bin `k` = `[k, k+1)` seconds from measure 0.

    One bin per started second up to the last note, so `len()` is
    `floor(last) + 1` and `sum()` is the chart's note count: no note is dropped
    and none is counted twice. A note before 0 s (none exist; guarded) goes in
    bin 0. An empty chart gives `[]`."""
    if not note_times:
        return []
    bins = [0] * (int(max(note_times)) + 1)
    for t in note_times:
        bins[max(0, int(t))] += 1
    return bins


# An optional compiled replacement for the note walk in `score_song_detailed`,
# injected by the web layer (`tools/web/kernel`), never imported here, so this
# package keeps `dependencies = []`. It is called as
#
#     kernel(note_weights, liveness, constant, terms, score_rates,
#            board_skill_effect_up, special_terms, cover_range, member_ids, cfg)
#
# where `liveness` is `(always_mask, ((bit, note-index ranges), ...))`, the
# live-term bitmask before it is spread over the notes (`_window_note_ranges`),
# and returns `(total, permutation)`, or `None` to decline (the Python below then
# runs). The contract is bit-identity with that Python
# (`tools/web/kernel/test_notewalk.py`); a kernel that approximates is a bug.
NOTE_WALK_KERNEL: Callable | None = None

# The same hook for worker processes: a spawned interpreter does not inherit
# `NOTE_WALK_KERNEL`, but it inherits the environment. `HBUILDER_NOTE_WALK_KERNEL`
# names a Python file and an attribute, `<dir>/notewalk.py:walk`, loaded by path on
# first use. A kernel that fails to load is reported once on stderr and the Python
# path is used.
NOTE_WALK_KERNEL_ENV = "HBUILDER_NOTE_WALK_KERNEL"
_env_kernel_state: tuple[str, Callable | None] = ("", None)


def _note_walk_kernel() -> Callable | None:
    """The injected kernel: the module attribute if set, else the one the
    environment names (resolved once per distinct value)."""
    global _env_kernel_state
    if NOTE_WALK_KERNEL is not None:
        return NOTE_WALK_KERNEL
    spec = os.environ.get(NOTE_WALK_KERNEL_ENV, "")
    if spec == _env_kernel_state[0]:
        return _env_kernel_state[1]
    kernel: Callable | None = None
    if spec:
        try:
            import importlib.util

            file_name, _, attribute = spec.rpartition(":")
            module_spec = importlib.util.spec_from_file_location("_hbuilder_note_walk_kernel", file_name)
            module = importlib.util.module_from_spec(module_spec)  # type: ignore[arg-type]
            module_spec.loader.exec_module(module)  # type: ignore[union-attr]
            kernel = getattr(module, attribute)
        except Exception as exc:  # noqa: BLE001 - any failure means "use Python"
            print(
                f"note-walk kernel {spec!r} could not be loaded ({exc!r}); "
                "scoring continues on the Python path",
                file=sys.stderr,
            )
    _env_kernel_state = (spec, kernel)
    return kernel


# THE COOLDOWN SCREEN: an estimate of `score_song_cooldown_variants`' totals, for ORDERING
# activation-frequency choices (`screen_song_cooldown_variants`). The kernel is handed the
# team's terms once (`_cooldown_screen_spec`) and builds each variant's ACTIVE windows and
# riders itself; it walks the chart by runs of notes instead of note by note, so its totals
# are within a few 1e-5 of the exact ones and are never served. `None` (or a kernel that
# declines) scores every variant exactly. Same injection as the note walk: the attribute in
# this process, `HBUILDER_COOLDOWN_SCREEN_KERNEL` (`<file>.py:<attribute>`) in spawned ones.
COOLDOWN_SCREEN_KERNEL: Callable | None = None
COOLDOWN_SCREEN_KERNEL_ENV = "HBUILDER_COOLDOWN_SCREEN_KERNEL"
_screen_kernel_state: tuple[str, Callable | None] = ("", None)


def _cooldown_screen_kernel() -> Callable | None:
    """The injected cooldown-screen kernel: the module attribute if set, else the one the
    environment names (resolved once per distinct value)."""
    global _screen_kernel_state
    if COOLDOWN_SCREEN_KERNEL is not None:
        return COOLDOWN_SCREEN_KERNEL
    spec = os.environ.get(COOLDOWN_SCREEN_KERNEL_ENV, "")
    if spec == _screen_kernel_state[0]:
        return _screen_kernel_state[1]
    kernel: Callable | None = None
    if spec:
        try:
            import importlib.util

            file_name, _, attribute = spec.rpartition(":")
            module_spec = importlib.util.spec_from_file_location(
                "_hbuilder_cooldown_screen_kernel", file_name
            )
            module = importlib.util.module_from_spec(module_spec)  # type: ignore[arg-type]
            module_spec.loader.exec_module(module)  # type: ignore[union-attr]
            kernel = getattr(module, attribute)
        except Exception as exc:  # noqa: BLE001 - any failure means "score exactly"
            print(
                f"cooldown-screen kernel {spec!r} could not be loaded ({exc!r}); "
                "cooldown variants are scored exactly",
                file=sys.stderr,
            )
    _screen_kernel_state = (spec, kernel)
    return kernel


# THE CHART KERNEL: a leader's whole per-chart step in one compiled call -- the terms placed on
# the chart (ACTIVE windows, combo splits, SPECIAL riders, the outfit's terms), the live masks,
# the cover ranges and the walk -- from the team's terms with everything no chart moves
# (`_chart_kernel_spec`), built once per board. Bit-identical to the Python it replaces; a
# kernel that declines a team (`None`) leaves it to the Python. Same injection as the others:
# the attribute here, `HBUILDER_CHART_KERNEL` (`<file>.py:<attribute>`) in spawned processes.
# `False` turns it off in this process whatever the environment names (a comparison with the
# Python path).
CHART_KERNEL: Callable | None | bool = None
CHART_KERNEL_ENV = "HBUILDER_CHART_KERNEL"
_chart_kernel_state: tuple[str, Callable | None] = ("", None)


def _chart_kernel() -> Callable | None:
    """The injected chart kernel: the module attribute if set (`False`: none), else the one
    the environment names (resolved once per distinct value)."""
    global _chart_kernel_state
    if CHART_KERNEL is not None:
        return CHART_KERNEL or None  # type: ignore[return-value]
    spec = os.environ.get(CHART_KERNEL_ENV, "")
    if spec == _chart_kernel_state[0]:
        return _chart_kernel_state[1]
    kernel: Callable | None = None
    if spec:
        try:
            import importlib.util

            file_name, _, attribute = spec.rpartition(":")
            module_spec = importlib.util.spec_from_file_location("_hbuilder_chart_kernel", file_name)
            module = importlib.util.module_from_spec(module_spec)  # type: ignore[arg-type]
            module_spec.loader.exec_module(module)  # type: ignore[union-attr]
            kernel = getattr(module, attribute)
        except Exception as exc:  # noqa: BLE001 - any failure means "use Python"
            print(
                f"chart kernel {spec!r} could not be loaded ({exc!r}); scoring continues on the Python path",
                file=sys.stderr,
            )
    _chart_kernel_state = (spec, kernel)
    return kernel


# `(note_weights, perfect_sum)` per chart, platform and judgement column: functions
# of the chart alone, which cost ~7 % of every call to rebuild. Keyed by object
# identity with the chart kept alive in the entry, so a recycled `id()` can never
# serve another chart's weights.
_chart_weights_cache: dict[tuple[int, str, bool], tuple] = {}

# The chart's note times as one tuple, per chart, keyed as the weights above.
_chart_times_cache: dict[int, tuple] = {}


def _chart_times(chart: SongChart) -> tuple[float, ...]:
    hit = _chart_times_cache.get(id(chart))
    if hit is None or hit[0] is not chart:
        if len(_chart_times_cache) > 4096:
            _chart_times_cache.clear()
        hit = (chart, tuple(note.time for note in chart.notes))
        _chart_times_cache[id(chart)] = hit
    return hit[1]


def _chart_weights(chart: SongChart, platform: str, auto: bool) -> tuple[tuple[float, ...], float]:
    key = (id(chart), platform, auto)
    hit = _chart_weights_cache.get(key)
    if hit is None or hit[0] is not chart:
        # the n-th note of a full-perfect manual run carries the LiveCombo tier of combo
        # n; on AUTO the combo never accumulates (a live run's trace), so no note does
        note_weights = tuple(
            (note.auto_permil / 1000.0)
            if auto
            else (note.perfect_permil_for(platform) / 1000.0) * (1.0 + chart.combo_coefficient(index + 1))
            for index, note in enumerate(chart.notes)
        )
        if len(_chart_weights_cache) > 4096:
            _chart_weights_cache.clear()
        hit = (chart, note_weights, chart.notes_perfect_coefficient_sum(platform))
        _chart_weights_cache[key] = hit
    return hit[1], hit[2]


def _activation_probability(resolved, cfg: ScoringConfig) -> float | None:
    """The ACTIVE's per-check proc chance, or `None` when it cannot be stated.

    The base tier only: `team_rate_up` and the board's rate need the whole team's
    rate-up fold, so this is a lower bound on the real chance and the caller labels it
    as one. `None`, not a default, when the tier is missing."""
    tier = getattr(resolved.skill, "probability_tier", None)
    if not tier:
        return None
    return cfg.probability_tiers.get(str(tier))


def score_song_detailed(
    members: tuple[Card, ...],
    leaders: tuple[Card, ...],
    chart: SongChart,
    cfg: ScoringConfig,
    *,
    deck_power: OverallPower | Callable[[Card], OverallPower] | None = None,
    auto: bool | None = None,
    order_totals: dict | None = None,
    note_rows: dict | None = None,
    team_setup: dict | None = None,
) -> tuple[SongScore, ...]:
    """`score_song`, plus the slot order and the deck power each total used.

    `team_setup` (a dict the caller keeps, opt-in): what a call prepares of the team that
    no chart moves -- the members' resolved skills, recipes and stat entries, each
    leader's board terms, outfit and stat sum -- is left in it under the config, and a
    later call with the SAME `members` tuple and `cfg` object reads it back instead of
    preparing it again. One team scored on many charts is then prepared once per board
    and only its terms are placed on each chart. The scores are the same floats.

    `deck_power` is `None` (the modelled stat pool), an `OverallPower`, or a
    per-leader callable returning one (`lambda ldr: overall_power_for(members,
    ldr, cfg)`). Anything else raises `TypeError` (a bare number carries no Member
    Power-Up bases); `OverallPower.fixed(value)` asks for a raw figure.

    `order_totals` (a dict, opt-in): every leader's 120 slot orders are walked by the
    reference walk below (the one the fast paths are bit-identical to) and each order's
    total is recorded as `order_totals[(leader_id, member_order)]`. For a team played
    on many songs with one slot order (the event's Highlight), never inside a search.

    `note_rows` (a dict, opt-in): the same reference walk, and for each leader's best
    slot order the note-by-note breakdown, `note_rows[leader_id]` = one `NoteRow` per
    note, whose `expected_value`s add up to that leader's `total_score` exactly (they
    are the walk's own per-note terms). For an export, never inside a search.

    The slot order is the best of the 120 orders, and it decides one thing: which
    chart cue fires each member's SPECIAL. The stat pool does not depend on it (a
    count-limited passive stat buff reaches the members with the highest stat it
    raises, `scoring_config.SLOT_ORDER_RULES`), so the deck power is the same for
    every order and the maximum is the cue assignment."""
    return _score_song(
        members,
        leaders,
        chart,
        cfg,
        deck_power=deck_power,
        auto=auto,
        order_totals=order_totals,
        note_rows=note_rows,
        team_setup=team_setup,
    )


def score_song_cooldown_variants(
    members: tuple[Card, ...],
    leader: Card,
    chart: SongChart,
    cfg: ScoringConfig,
    variants: Sequence[Mapping[str, float]],
    *,
    deck_power: OverallPower | None = None,
    auto: bool | None = None,
) -> list[SongScore]:
    """`score_song_detailed(members, (leader,), chart, cfg_v)[0]` for each variant, where
    `cfg_v`'s board is `cfg.board.with_cooldowns(variant)` (`{holomem: cooldown_shorten}`):
    the same numbers, bit for bit, for many activation-frequency choices at once.

    A cooldown moves only the ACTIVE windows, so everything else -- the stat stage, the
    supports, the board terms, the leader's outfit, the deck power -- is built once and
    only the ACTIVE terms (and the SPECIAL riders that ride on their checks) are rebuilt
    per variant. When a member's ACTIVE feeds anything else (a team activation-rate up, a
    stat or a flat rate, all of which read the cooldown through the run-average uptime),
    each variant is scored whole instead. Needs `cfg.board`."""
    if cfg.board is None:
        raise ValueError("cooldown variants need a board")
    return [
        scores[0]
        for scores in _score_song(
            members,
            (leader,),
            chart,
            cfg,
            deck_power=deck_power,
            auto=auto,
            cooldown_variants=tuple(variants),
        )
    ]


def screen_song_cooldown_variants(
    members: tuple[Card, ...],
    leader: Card,
    chart: SongChart,
    cfg: ScoringConfig,
    variants: Sequence[Mapping[str, float]],
    *,
    deck_power: OverallPower | None = None,
    auto: bool | None = None,
    team_setup: dict | None = None,
) -> list[float]:
    """`score_song_cooldown_variants`' totals ESTIMATED, to order activation-frequency
    choices: each variant's ACTIVE windows and riders are rebuilt exactly, but the chart is
    walked by runs of notes with a constant live set, the per-note `ceil` replaced by its
    mean, so a total is within a few 1e-5 of the exact one. For a screen, never a served
    number. With no kernel (`COOLDOWN_SCREEN_KERNEL`), or a team the kernel does not express
    (an ACTIVE that feeds anything but its own terms, an outfit ACTIVE, more terms than it
    holds), the totals are the exact ones. `team_setup`: `score_song_detailed`'s."""
    if cfg.board is None:
        raise ValueError("cooldown variants need a board")
    return list(
        _score_song(
            members,
            (leader,),
            chart,
            cfg,
            deck_power=deck_power,
            auto=auto,
            cooldown_variants=tuple(variants),
            cooldown_screen=True,
            team_setup=team_setup,
        )
    )


def score_song_and_screen(
    members: tuple[Card, ...],
    leader: Card,
    chart: SongChart,
    cfg: ScoringConfig,
    variants: Sequence[Mapping[str, float]],
    *,
    deck_power: OverallPower | None = None,
    auto: bool | None = None,
    team_setup: dict | None = None,
) -> tuple[SongScore, list[float]]:
    """`(score_song_detailed(members, (leader,), chart, cfg)[0], screen_song_cooldown_variants(
    ..., variants))` from ONE set-up of the team on the chart: the exact score of the board as
    it is and the estimated totals of its cooldown variants (the search's count-aware value
    reads both for every team it scores). `team_setup`: `score_song_detailed`'s."""
    if cfg.board is None:
        raise ValueError("cooldown variants need a board")
    return _score_song(
        members,
        (leader,),
        chart,
        cfg,
        deck_power=deck_power,
        auto=auto,
        cooldown_variants=tuple(variants),
        cooldown_screen="with_plain",
        team_setup=team_setup,
    )


def _member_setup(members: tuple[Card, ...], cfg: ScoringConfig) -> tuple:
    """The member half of `_score_song` that no chart moves: the resolved skills, the team
    activation-rate fold, each term-bearing skill's recipe, the flat score rates and the
    members' stat entries. A function of the members and the config (its board included)
    alone, so one call serves every chart the same members are scored on with that config.
    The lists are read, never written, by `_score_song`."""
    card_set_gates = resolve_card_set_gates(members, cfg.run_state)

    normal_resolved: list[ResolvedSkill] = []
    for card in members:
        normal_resolved.extend(card_set_gates.normal_by_card_id[card.id])
    team_rate_up = _fold_activation_rate_up(tuple(normal_resolved), cfg)
    active_rank = active_magnitude_by_card(normal_resolved)

    # --- the member half, shared by every candidate leader ------------------
    member_index_by_id = {card.id: index for index, card in enumerate(members)}
    # each term-bearing skill as `(resolved, state, member index, targets)`, in walk order:
    # `member_terms_for` places them on the axis, so a cooldown variant rebuilds only the
    # ACTIVE ones
    recipes: list[tuple[ResolvedSkill, str, int, frozenset | None]] = []
    member_score_rates: list[float] = []
    stat_entries: list = []
    # whether a member's ACTIVE reaches past its own terms (a team rate up, a stat, a flat
    # rate: each reads the cooldown through the run-average uptime)
    active_elsewhere = False

    for resolved in normal_resolved:
        pool = _pool_for(resolved)
        if pool == "none":
            continue
        rate, state, _missing = _resolve_skill(resolved, cfg, team_rate_up)
        weight = cfg.family_weights[resolved.family]
        index = member_index_by_id[resolved.card_id]
        if pool == "stat":
            active_elsewhere |= resolved.skill.kind == "ACTIVE"
            target_ids: frozenset = frozenset()
            if state == "applied":
                target_ids = targets.resolve_target_card_ids(
                    resolved.skill.target,
                    resolved.card_id,
                    members,
                    STAT_NAMES_BY_EFFECT_TYPE[resolved.skill.effect_type],
                    cfg.target_selection_model,
                    cfg.target_under_supply,
                )
            stat_entries.append(
                (
                    rate * weight,
                    STAT_NAMES_BY_EFFECT_TYPE[resolved.skill.effect_type],
                    target_ids,
                    resolved.skill.target if state == "applied" else _FIXED_TARGET,
                    resolved.card_id,
                )
            )
        elif pool == "support":
            support_state, support_targets = _support_state(resolved, members, cfg, state, active_rank)
            recipes.append((resolved, support_state, index, support_targets))
        elif _is_score_up_effect(resolved):
            recipes.append((resolved, state, index, None))
        else:
            active_elsewhere |= resolved.skill.kind == "ACTIVE"
            # `life` / `judgment` effects, and PASSIVE/COSTUME Score UPs: summed
            # individually, exactly as `_score_team` sums them
            # (`scoring._is_score_up_effect`).
            member_score_rates.append(rate * weight)

    active_elsewhere |= any(
        r.family == "activation_rate_up" and r.skill.kind == "ACTIVE" for r in normal_resolved
    )
    return (
        card_set_gates,
        normal_resolved,
        team_rate_up,
        active_rank,
        member_index_by_id,
        recipes,
        member_score_rates,
        stat_entries,
        active_elsewhere,
        _support_tie_outcomes(members, recipes, active_rank),
    )


# The 120 slot orders in `itertools.permutations` order (`permutation[slot]` = the index of
# the member in that slot), and each one's index: the walks enumerate them in this order and
# keep the first maximum.
_PERMUTATIONS: tuple[tuple[int, ...], ...] = tuple(itertools.permutations(range(SPECIAL_CUE_COUNT)))
_PERMUTATION_INDEX: dict[tuple[int, ...], int] = {p: i for i, p in enumerate(_PERMUTATIONS)}


def _support_tie_outcomes(
    members: tuple[Card, ...],
    recipes: list,
    active_rank: Mapping[str, float] | None,
) -> tuple[tuple[list, tuple[bool, ...]], ...] | None:
    """The outcomes of a count-limited Score Support tie, or `None` when no tie binds.

    A support with `count` k pays the k qualifiers with the strongest ACTIVE, and an exact
    tie goes to the EARLIER SLOT (`scoring_models.TopNByBuffedStat`, measured 8/8 on the
    server). The slot order is also what places each SPECIAL on its cue, and the walk takes
    the best of the 120 orders; so when the cut falls inside a group of equal magnitudes,
    who is paid depends on the order, and one order decides both. Each outcome is `(the
    recipe list with that outcome's recipients, 120 flags marking the orders that give
    it)`; the walk maximises each outcome over its own orders only. Without a tie every
    order pays the same members and the input order of `members` decides nothing.

    `active_rank` is `None` when an ACTIVE magnitude is not known; the recipients are then
    ranked by total parameter (`targets.resolve_target_card_ids`), and so is the tie."""

    def rank(card: Card) -> float:
        if active_rank is not None:
            return active_rank.get(card.id, 0.0)
        return float(sum(getattr(card.stats, name) for name in STAT_NAMES))

    tied = []
    for k, (resolved, state, _index, support_targets) in enumerate(recipes):
        if state != "applied" or support_targets is None:
            continue
        target = resolved.skill.target
        if (
            not isinstance(target, EffectTarget)
            or not target.count
            or target.kind not in ("type", "generation")
        ):
            continue
        if target.kind == "type":
            qualifying = [i for i, card in enumerate(members) if card.type == target.value]
        else:
            qualifying = [i for i, card in enumerate(members) if target.value in card.generations]
        count = int(target.count)
        if len(qualifying) <= count:
            continue  # every qualifier is paid (or, under-supplied, the configured rule): no tie
        values = sorted((rank(members[i]) for i in qualifying), reverse=True)
        cut = values[count - 1]
        if values[count] != cut:
            continue
        above = frozenset(members[i].id for i in qualifying if rank(members[i]) > cut)
        group = [i for i in qualifying if rank(members[i]) == cut]
        tied.append((k, resolved, above, group, count - len(above)))
    if not tied:
        return None
    flags: dict[tuple, list[bool]] = {}
    for index, permutation in enumerate(_PERMUTATIONS):
        slot_of = [0] * SPECIAL_CUE_COUNT
        for slot, member in enumerate(permutation):
            slot_of[member] = slot
        key = tuple(
            above | frozenset(members[i].id for i in sorted(group, key=slot_of.__getitem__)[:places])
            for _k, _resolved, above, group, places in tied
        )
        flags.setdefault(key, [False] * len(_PERMUTATIONS))[index] = True
    outcomes = []
    for key, allowed in flags.items():
        outcome = list(recipes)
        for (k, resolved, _above, _group, _places), paid in zip(tied, key):
            r, state, index, _old = recipes[k]
            outcome[k] = (r, state, index, _support_targets(resolved, paid))
        outcomes.append((outcome, tuple(allowed)))
    return tuple(outcomes)


def _score_song(
    members: tuple[Card, ...],
    leaders: tuple[Card, ...],
    chart: SongChart,
    cfg: ScoringConfig,
    *,
    deck_power: OverallPower | Callable[[Card], OverallPower] | None = None,
    auto: bool | None = None,
    order_totals: dict | None = None,
    note_rows: dict | None = None,
    cooldown_variants: tuple[Mapping[str, float], ...] | None = None,
    cooldown_screen: bool | str = False,
    team_setup: dict | None = None,
):
    """`score_song_detailed`'s body; with `cooldown_variants`, one result tuple per variant
    (`score_song_cooldown_variants`); with `cooldown_screen` too, one estimated total per
    variant (`screen_song_cooldown_variants`). `team_setup`: `score_song_detailed`'s."""
    if len(members) != SPECIAL_CUE_COUNT:
        raise SongError(
            f"score_song needs exactly {SPECIAL_CUE_COUNT} members — one per "
            f"deck slot, because the chart carries one `#mmm0B` cue per slot; "
            f"got {len(members)}"
        )
    if deck_power is None or isinstance(deck_power, OverallPower):
        resolve_power = None
    elif callable(deck_power):
        resolve_power = deck_power
    else:
        raise _not_overall_power("deck_power", deck_power)
    times = _chart_times(chart)
    song_seconds = times[-1]
    # the member half no chart moves, and each leader's (`leader_base`): built once per
    # call, or held in the caller's `team_setup` for every chart it scores these members
    # on with this config
    held = None if team_setup is None else team_setup.get(id(cfg))
    if held is None or held[0] is not cfg or held[1] is not members:
        # (config, members, the member half, each leader's base, the chart kernel's specs,
        # each leader's outfit skills, each (leader, cast)'s singer-conditional terms)
        held = (cfg, members, _member_setup(members, cfg), {}, {}, {}, {})
        if team_setup is not None:
            team_setup[id(cfg)] = held
    (
        card_set_gates,
        normal_resolved,
        team_rate_up,
        active_rank,
        member_index_by_id,
        recipes,
        member_score_rates,
        stat_entries,
        active_elsewhere,
        support_outcomes,
    ) = held[2]

    variant_terms: dict = {}

    def member_terms_for(
        cfg_terms: ScoringConfig, reuse: list | None = None, recipes_use: list | None = None
    ):
        """The member terms on `cfg_terms`' board, the SPECIAL riders last, and the terms
        of each recipe. `reuse`: a previous call's per-recipe terms, kept for every skill
        that is not an ACTIVE (a cooldown moves nothing else). `recipes_use`: one outcome
        of a recipients tie (`_support_tie_outcomes`) instead of the team's recipes."""
        per_recipe = []
        for k, (resolved, state, index, support_targets) in enumerate(
            recipes if recipes_use is None else recipes_use
        ):
            if reuse is not None and resolved.skill.kind != "ACTIVE":
                per_recipe.append(reuse[k])
                continue
            # a variant's ACTIVE term is a function of its member's uptime alone: built
            # once per (skill, uptime), whichever variant asks first
            key = (k, cfg_terms.board.uptime_for(resolved.member)) if reuse is not None else None  # type: ignore[union-attr]
            hit = variant_terms.get(key) if key is not None else None
            if hit is None:
                hit = _combo_split(
                    resolved,
                    cfg_terms,
                    _term_for(resolved, cfg_terms, team_rate_up, state, song_seconds, index, support_targets),
                    times,
                )
                if key is not None:
                    variant_terms[key] = hit
            per_recipe.append(hit)
        out = [term for terms in per_recipe for term in terms]
        # the SPECIAL activation-rate riders, slot-dependent extra rolls on the ACTIVE checks
        out.extend(_rider_terms(normal_resolved, member_index_by_id, out, cfg_terms, chart, times))
        return out, per_recipe

    # --- the leader half ----------------------------------------------------
    # The principal, then the outfit's scored `score_support` rider
    # (`gates.resolve_leader_costume_skills`), in `score_card_set`'s order.
    # (no chart moves them: resolved once per leader with the member half)
    costumes_held: dict = held[5]
    leader_costumes: dict[str, tuple[ResolvedSkill, ...]] = {}
    for card in leaders:
        costume_skills = costumes_held.get(card.id)
        if costume_skills is None:
            costume_skills = costumes_held[card.id] = resolve_leader_costume_skills(
                card, card_set_gates.aggregates, cfg.run_state
            )
        if costume_skills:
            leader_costumes[card.id] = costume_skills

    music_coefficient = chart.music_coefficient()
    # The client's column: `cfg.platform` (mobile by default) decides whether a
    # FLICK weighs 1050 or 1000, in BOTH the divisor and the per-note numerator.
    platform = cfg.platform

    # `w_n x (1 + combo_n)`, the note's whole chart-side weight, built once. The
    # judgement numerator swaps to the AUTO column for an auto-play run; the divisor
    # (`perfect_sum`) stays on PERFECT, as `GetNotesPerfectCoefficientSum` does.
    # `auto` None: the config's own premise (`cfg.song_auto`), so a caller that does
    # not pass it (e.g. `parallel_refine`) still scores an AUTO config on the AUTO column
    if auto is None:
        auto = bool(cfg.song_auto)
    note_weights, perfect_sum = _chart_weights(chart, platform, auto)

    # The stat entries are per leader (a leader's stat outfit is one of them), so
    # they cannot be hoisted out of the loop below.
    member_stat_entries = stat_entries
    orderings_cache: dict = {}
    leader_bases: dict = held[3]
    cast_terms: dict = held[6]

    def leader_base(leader):
        """One leader's walk without the chart: the board's three terms, the outfit's
        term-bearing skills resolved (`(skill, state, extra arguments of its term)`, in
        walk order), the flat score rates and the stat sum. A function of the leader, the
        members and the config alone, so it is kept with the member half (`team_setup`)."""
        base = leader_bases.get(leader.id)
        if base is not None:
            return base
        # Read per leader: `skill_effect_up` is leader-scoped (a red node applies to
        # the whole team only while its own holomem leads), the board channel that
        # can reorder leaders. The power term joins the modelled stat pool only when
        # the caller passes no `deck_power`; a caller's Overall Power carries it.
        board_bonus = _board_bonus(leader, members, cfg)
        costume_recipes: list[tuple] = []
        score_rates = list(member_score_rates)
        leader_stat_entry = None
        for costume in leader_costumes.get(leader.id, ()):
            pool = _pool_for(costume)
            if pool != "none":
                rate, state, _missing = _resolve_skill(costume, cfg, team_rate_up)
                weight = cfg.family_weights[costume.family]
                if pool == "support":
                    support_state, support_targets = _support_state(costume, members, cfg, state, active_rank)
                    costume_recipes.append((costume, support_state, (support_targets,)))
                elif pool == "score":
                    if _is_score_up_effect(costume):
                        costume_recipes.append((costume, state, ()))
                    else:
                        score_rates.append(rate * weight)
                elif pool == "stat":
                    # The leader contributes no stats of her own card, but her
                    # outfit raises the members' stats: `score_card_set` builds
                    # `stat_entries` per leader starting with this entry, and almost
                    # every outfit is `stat_up`.
                    target_ids = frozenset()
                    if state == "applied":
                        target_ids = targets.resolve_target_card_ids(
                            costume.skill.target,
                            costume.card_id,
                            members,
                            STAT_NAMES_BY_EFFECT_TYPE[costume.skill.effect_type],
                            cfg.target_selection_model,
                            cfg.target_under_supply,
                        )
                    leader_stat_entry = (
                        rate * weight,
                        STAT_NAMES_BY_EFFECT_TYPE[costume.skill.effect_type],
                        target_ids,
                        costume.skill.target if state == "applied" else _FIXED_TARGET,
                        costume.card_id,
                    )

        # The leader's outfit goes first (`Team.effective_skill_instances()`'s and
        # `score_card_set`'s order): `_order_varying_indices` returns indices into
        # this list and `_stat_sum` aligns `recipients` with it positionally.
        stat_entries = (
            [leader_stat_entry, *member_stat_entries]
            if leader_stat_entry is not None
            else member_stat_entries
        )
        # The stat stage is order-invariant (`scoring_config.SLOT_ORDER_RULES`), so one
        # call answers every permutation — `_effective_stat_sum`'s own call, so the
        # number is bit-identical to `score_card_set`'s.
        fixed_stat_sum, _order = _effective_stat_sum(members, stat_entries, cfg, orderings_cache)
        base = leader_bases[leader.id] = (board_bonus, tuple(costume_recipes), score_rates, fixed_stat_sum)
        return base

    # `deck_power` may be a per-leader callable (see `overall_power_for`), resolved
    # and type-checked once per leader here, never inside the note walk or the
    # 120-order loop.
    def prepare_leader(leader, with_costume_terms=True):
        """Everything of one leader's walk that no cooldown moves. `with_costume_terms`
        False: the outfit's terms are left out (the chart kernel places them itself)."""
        if resolve_power is not None:
            raw_power = resolve_power(leader)
            if not isinstance(raw_power, OverallPower):
                raise _not_overall_power("deck_power's per-leader result", raw_power)
        else:
            raw_power = deck_power
        leader_power = None if raw_power is None else raw_power.value
        (
            (board_power, board_score_rate, board_skill_effect_up),
            costume_recipes,
            score_rates,
            fixed_stat_sum,
        ) = leader_base(leader)
        # the singer-conditional stat power below, kept apart so it survives a
        # caller's Overall Power (`leader_power`), which has no song cast
        cast_power = 0.0
        # The two terms that are functions of the SONG'S CAST rather than of the
        # chart. Both are no-ops for a chart with no cast. Kept per (leader, cast, scope)
        # with the member half: the charts a team is scored on share a few casts.
        cast_key = (leader.id, chart.singers, chart.singer_type)
        cast_hit = cast_terms.get(cast_key) if cfg.board is not None and chart.singers else None
        if cast_hit is not None and cast_hit[0] is raw_power:
            _power, board_score_rate, cast_effect_up, cast_power, cast_up = cast_hit
            board_skill_effect_up += cast_effect_up
            board_power += cast_power
            cast_power += cast_up
        elif cfg.board is not None and chart.singers:
            # `song_score_bonus` at this song's own scope supersedes the pooled
            # `all` bucket `_board_bonus` returned (pooling over-counts `solo` and
            # `group`; on `all` the two agree exactly).
            board_score_rate = cfg.board.song_score_bonus_for(chart.singers, scope=chart.singer_type)
            # ...and the conditional `skill_effect_up` joins the unconditional one:
            # the same addend in `CalcScoreUpRatio`'s parenthesis.
            cast_effect_up = cfg.board.conditional_skill_effect_up_for(
                members, None if leader is None else leader.member, chart.singers
            )
            board_skill_effect_up += cast_effect_up
            # ...and the singer-conditional STAT power (R-044/062/063: "+stat% to
            # all when this holomem sings") joins the board's Overall-Power summand,
            # since `_board_bonus`/`_terms` leave it out as conditional. It also rides
            # on top of a caller's `deck_power`, which has no song cast.
            cast_power = cfg.board.conditional_stat_power_for(
                members, None if leader is None else leader.member, chart.singers
            )
            board_power += cast_power
            # On a caller's Overall Power the cast power also joins each member's
            # Member Power-Up base, as on the deck screen with this song chosen;
            # `overall_power_for` carries the bases (`OverallPower`).
            cast_up = 0.0
            if cast_power and leader_power is not None:
                cast_up = cast_power_up(
                    raw_power, members, None if leader is None else leader.member, cfg, chart.singers
                )
            cast_terms[cast_key] = (raw_power, board_score_rate, cast_effect_up, cast_power, cast_up)
            cast_power += cast_up
        # the outfit's terms on this song's axis (its length places their windows)
        costume_terms: list[_Term] = (
            [
                _term_for(costume, cfg, team_rate_up, state, song_seconds, None, *extra)
                for costume, state, extra in costume_recipes
            ]
            if with_costume_terms
            else []
        )
        return (
            leader,
            leader_power,
            cast_power,
            board_power,
            board_score_rate,
            board_skill_effect_up,
            costume_terms,
            score_rates,
            fixed_stat_sum,
        )

    def walk(
        prepared,
        member_terms: list[_Term],
        cfg: ScoringConfig,
        allowed: tuple[bool, ...] | None = None,
        rows_out: dict | None = None,
    ) -> SongScore:
        """One leader's best slot order over `member_terms` (and its outfit's terms).
        `allowed`: the only orders considered (120 flags in `_PERMUTATIONS` order, one
        outcome of a recipients tie); `None` is every order. `rows_out`: where the note rows
        go (`note_rows` by default)."""
        (
            leader,
            leader_power,
            cast_power,
            board_power,
            board_score_rate,
            board_skill_effect_up,
            costume_terms,
            score_rates,
            fixed_stat_sum,
        ) = prepared
        terms = list(member_terms) + costume_terms

        # Liveness as a bitmask per note (bit `i` is `terms[i]`), so the
        # 120-permutation walk is dict lookups. `times` is ascending
        # (`SongChart.__post_init__`), so `[bisect_left(times, start),
        # bisect_left(times, end))` is exactly `{i : start <= times[i] < end}`
        # (`tests/test_score_song.py` keeps the per-note scan as the reference).
        note_count = len(times)
        # The always-live terms first, so every note starts from their mask (bitwise
        # OR: the order the bits arrive in cannot change the result).
        always_mask = 0
        for bit, term in enumerate(terms):
            if term.special_member is None and term.windows is None:
                always_mask |= 1 << bit  # live for the whole run
        special_terms: list[int] = []
        window_ranges: list[tuple[int, tuple]] = []
        for bit, term in enumerate(terms):
            if term.special_member is not None:
                special_terms.append(bit)
                continue
            if term.windows is None:
                continue
            # `windows == []` falls through here and sets nothing: never live.
            window_ranges.append((bit, _window_note_ranges(times, term.windows)))

        def _spread_liveness() -> list[int]:
            """`fixed_bits`: the mask of non-SPECIAL terms live at each note.
            Only the Python walks need it spread out; a kernel is handed
            `(always_mask, window_ranges)` and spreads it itself."""
            bits = [always_mask] * note_count
            for bit, ranges in window_ranges:
                mask = 1 << bit
                for low, high in ranges:
                    for note_index in range(low, high):
                        bits[note_index] |= mask
            return bits

        fixed_bits: list[int] | None = None

        # `cover_range[bit][slot]`: the half-open note-INDEX range a SPECIAL covers
        # when its member sits in that slot (`cue <= t < cue + duration`).
        cover_range = {
            bit: [
                (
                    bisect_left(times, cue),
                    bisect_left(times, cue + terms[bit].special_duration),
                )
                for cue in chart.special_cues
            ]
            if terms[bit].slot_cover is None
            # a rider term: the boosted ACTIVE window its host's slot covers
            else [
                (0, 0) if span is None else (bisect_left(times, span[0]), bisect_left(times, span[1]))
                for span in terms[bit].slot_cover
            ]
            for bit in special_terms
        }

        pool_cache: dict[int, float] = {}

        def _combined(live: int) -> float:
            """The score rate for a live-set bitmask, memoised per leader."""
            combined = pool_cache.get(live)
            if combined is None:
                live_ratio = _strongest_riders(
                    [term for bit, term in enumerate(terms) if live >> bit & 1 and not term.is_boost]
                )
                live_boost = [term for bit, term in enumerate(terms) if live >> bit & 1 and term.is_boost]
                combined = cfg.stacking_model.combine(
                    [
                        *score_rates,
                        _ratio_at(live_ratio, live_boost, cfg, board_skill_effect_up),
                    ]
                )
                pool_cache[live] = combined
            return combined

        # ------------------------------------------------------------------
        # The 5x5 assignment decomposition, and the two conditions it needs.
        # ------------------------------------------------------------------
        # The permutation-dependent part of the total is a 5x5 (member -> slot)
        # cost matrix, i.e. a linear assignment, under two conditions, both
        # checked here:
        #
        # 1. Only the best order is asked for: `order_totals` and `note_rows` need
        #    every order's own walk. (Power never varies with the order: the stat
        #    stage is order-invariant, `scoring_config.SLOT_ORDER_RULES`.)
        # 2. No note is reachable from two different slots' cue windows by two
        #    different members' SPECIALs (the per-note `max` across them would not
        #    split per slot). This happens on about 3 % of (team, chart) pairs.
        #
        # When both hold, every note is touched by at most one assigned member:
        #
        #     total(perm) = fixed_total + SUM over slots s of delta[perm[s]][s]
        #
        # Every per-note value is `math.ceil(...)` of the walk's expression, so every
        # summand is an integer-valued float far below 2^53 and re-associating the
        # sum is exact: the 120 totals are bit-identical to the walk's, and the
        # strict-`>` tie-break picks the same `member_order`.
        separable_power = order_totals is None and note_rows is None
        coupled = False
        if separable_power and special_terms:
            spans = [
                (slot, terms[bit].special_member, lo, hi)
                for bit in special_terms
                for slot, (lo, hi) in enumerate(cover_range[bit])
                if lo < hi
            ]
            for index, (slot_a, member_a, lo_a, hi_a) in enumerate(spans):
                for slot_b, member_b, lo_b, hi_b in spans[index + 1 :]:
                    if slot_a != slot_b and member_a != member_b and lo_a < hi_b and lo_b < hi_a:
                        coupled = True
                        break
                if coupled:
                    break

        if separable_power and not coupled:
            power = (fixed_stat_sum + board_power) if leader_power is None else leader_power + cast_power
            # The identical expression in the identical order as the walk's.
            constant = power * SCORE_COEFFICIENT * music_coefficient / perfect_sum * (1.0 + board_score_rate)
            kernel_result = None
            # (not when two riders can share a check: the kernel would combine them)
            note_walk_kernel = None if _riders_share_a_check(terms) else _note_walk_kernel()
            if note_walk_kernel is not None:
                kernel_result = note_walk_kernel(
                    note_weights,
                    (always_mask, window_ranges),
                    constant,
                    terms,
                    score_rates,
                    board_skill_effect_up,
                    special_terms,
                    cover_range,
                    tuple(card.id for card in members),
                    cfg,
                    **({} if allowed is None else {"allowed": allowed}),
                )
            if kernel_result is not None:
                best_total, best_permutation = kernel_result
                return SongScore(
                    leader_id=leader.id,
                    total_score=best_total,
                    member_order=tuple(members[i].id for i in best_permutation),
                    deck_power=power,
                )
            fixed_bits = _spread_liveness()
            base_values = [0.0] * note_count
            fixed_total = 0.0
            for note_index in range(note_count):
                value = math.ceil(
                    constant * note_weights[note_index] * (1.0 + _combined(fixed_bits[note_index]))
                )
                base_values[note_index] = value
                fixed_total += value

            bits_by_member: dict[int, list[int]] = {}
            for bit in special_terms:
                bits_by_member.setdefault(terms[bit].special_member, []).append(bit)

            delta = [[0.0] * SPECIAL_CUE_COUNT for _ in range(SPECIAL_CUE_COUNT)]
            for owner, owner_bits in bits_by_member.items():
                for slot in range(SPECIAL_CUE_COUNT):
                    note_extra: dict[int, int] = {}
                    for bit in owner_bits:
                        mask = 1 << bit
                        low, high = cover_range[bit][slot]
                        for note_index in range(low, high):
                            note_extra[note_index] = note_extra.get(note_index, 0) | mask
                    change = 0.0
                    for note_index, extra in note_extra.items():
                        change += (
                            math.ceil(
                                constant
                                * note_weights[note_index]
                                * (1.0 + _combined(fixed_bits[note_index] | extra))
                            )
                            - base_values[note_index]
                        )
                    delta[owner][slot] = change

            # The 120 sums are enumerated rather than solved (Hungarian): at n=5 the
            # argmax is ~600 additions against a cost-matrix build that is the whole
            # cost, and enumeration reproduces the walk's strict-`>` tie-break.
            best_total: float | None = None  # type: ignore[no-redef]
            best_permutation: tuple[int, ...] | None = None  # type: ignore[no-redef]
            for index, permutation in enumerate(_PERMUTATIONS):
                if allowed is not None and not allowed[index]:
                    continue
                total = fixed_total
                for slot, owner in enumerate(permutation):
                    total += delta[owner][slot]
                if best_total is None or total > best_total:
                    best_total = total
                    best_permutation = permutation
            assert best_permutation is not None  # 120 is never an empty walk
            return SongScore(
                leader_id=leader.id,
                total_score=best_total,
                member_order=tuple(members[i].id for i in best_permutation),
                deck_power=power,
            )

        if separable_power and coupled:
            # The coupled case with an order-invariant power: the kernel walks the
            # 120 orders itself (`hd_walk_general`), bit-identical to the walk below.
            # A SPECIAL rider's boosted ACTIVE window running into the next slot's
            # SPECIAL makes this common, and the Python walk is ~100x the kernel.
            # (not when two riders can share a check: the kernel would combine them)
            note_walk_kernel = None if _riders_share_a_check(terms) else _note_walk_kernel()
            if note_walk_kernel is not None:
                power = (fixed_stat_sum + board_power) if leader_power is None else leader_power + cast_power
                constant = (
                    power * SCORE_COEFFICIENT * music_coefficient / perfect_sum * (1.0 + board_score_rate)
                )
                kernel_result = note_walk_kernel(
                    note_weights,
                    (always_mask, window_ranges),
                    constant,
                    terms,
                    score_rates,
                    board_skill_effect_up,
                    special_terms,
                    cover_range,
                    tuple(card.id for card in members),
                    cfg,
                    general=True,
                    **({} if allowed is None else {"allowed": allowed}),
                )
                if kernel_result is not None:
                    best_total, best_permutation = kernel_result
                    return SongScore(
                        leader_id=leader.id,
                        total_score=best_total,
                        member_order=tuple(members[i].id for i in best_permutation),
                        deck_power=power,
                    )

        # The reference 120-permutation walk: the fallback for the cases the
        # decomposition cannot serve, and what the tests compare the fast paths to.
        best: SongScore | None = None
        if fixed_bits is None:
            fixed_bits = _spread_liveness()
        for index, permutation in enumerate(_PERMUTATIONS):
            if allowed is not None and not allowed[index]:
                continue
            # `permutation[s]` is the index (into `members`) of the card in slot
            # `s`, so `slot_of[i]` is the slot member `i` occupies.
            slot_of = [0] * SPECIAL_CUE_COUNT
            for slot, member in enumerate(permutation):
                slot_of[member] = slot

            stat_sum = fixed_stat_sum
            power = (stat_sum + board_power) if leader_power is None else leader_power + cast_power

            bits = list(fixed_bits)
            for bit in special_terms:
                mask = 1 << bit
                low, high = cover_range[bit][slot_of[terms[bit].special_member]]
                for note_index in range(low, high):
                    bits[note_index] |= mask

            constant = power * SCORE_COEFFICIENT * music_coefficient / perfect_sum * (1.0 + board_score_rate)
            total = 0.0
            for note_index in range(len(times)):
                total += math.ceil(constant * note_weights[note_index] * (1.0 + _combined(bits[note_index])))
            candidate = SongScore(
                leader_id=leader.id,
                total_score=total,
                member_order=tuple(members[i].id for i in permutation),
                deck_power=power,
            )
            if order_totals is not None:
                order_totals[(leader.id, candidate.member_order)] = total
            # Strict `>`: ties go to the first permutation
            # `itertools.permutations` reaches, and `members` is caller-fixed,
            # so the reported order is deterministic across runs.
            if best is None or candidate.total_score > best.total_score:
                best = candidate
        assert best is not None  # 120 permutations is never an empty walk
        rows = note_rows if rows_out is None else rows_out
        if rows is not None:
            rows[leader.id] = _note_rows(
                best,
                members,
                terms,
                fixed_bits,
                special_terms,
                cover_range,
                times,
                note_weights,
                _combined,
                music_coefficient,
                perfect_sum,
                board_score_rate,
                chart,
                auto,
                platform,
            )
        return best

    def walk_outcomes(prepared, terms_by_outcome: list, cfg_w: ScoringConfig) -> SongScore:
        """`walk` once per outcome of a recipients tie, each over its own orders, and the
        best of them; on an exact tie the earliest order in `_PERMUTATIONS`, as one walk of the
        120 orders (each with its own recipients) keeps the first maximum. Without a tie, the
        one walk."""
        if support_outcomes is None:
            return walk(prepared, terms_by_outcome[0], cfg_w)
        best: SongScore | None = None
        best_index = 0
        best_rows = None
        for (_recipes_o, allowed), terms in zip(support_outcomes, terms_by_outcome):
            rows: dict | None = {} if note_rows is not None else None
            score = walk(prepared, terms, cfg_w, allowed=allowed, rows_out=rows)
            index = _PERMUTATION_INDEX[tuple(member_index_by_id[c] for c in score.member_order)]
            if (
                best is None
                or score.total_score > best.total_score
                or (score.total_score == best.total_score and index < best_index)
            ):
                best, best_index, best_rows = score, index, rows
        if note_rows is not None and best_rows:
            note_rows.update(best_rows)
        assert best is not None  # an outcome always holds at least one order
        return best

    if cooldown_variants is None and order_totals is None and note_rows is None:
        scored = _score_by_chart_kernel(
            members,
            leaders,
            chart,
            cfg,
            held,
            prepare_leader,
            leader_base,
            times,
            note_weights,
            perfect_sum,
            music_coefficient,
        )
        if scored is not None:
            return scored

    leader_preps = [prepare_leader(leader) for leader in leaders]
    outcome_recipes = [None] if support_outcomes is None else [r for r, _allowed in support_outcomes]
    built = [member_terms_for(cfg, recipes_use=r) for r in outcome_recipes]
    terms_by_outcome = [terms for terms, _per in built]
    member_terms, per_recipe = built[0]
    if cooldown_variants is None:
        return tuple(walk_outcomes(prepared, terms_by_outcome, cfg) for prepared in leader_preps)
    costume_active = any(c.skill.kind == "ACTIVE" for cs in leader_costumes.values() for c in cs)
    values = None
    if cooldown_screen:
        kernel = _cooldown_screen_kernel()
        if (
            kernel is not None
            and cooldown_variants
            and len(leaders) == 1
            and not active_elsewhere
            and not costume_active
            and cfg.stacking_model.name == "additive"
            and cfg.active_stacking_model.name == "max"
        ):
            # with a recipients tie, each outcome's estimate over its own orders and the best
            # of them per variant, as the exact walk takes them
            hosts = _rider_hosts(normal_resolved, member_index_by_id, cfg, chart, times)
            allowed_by_outcome = [None] if support_outcomes is None else [a for _r, a in support_outcomes]
            for recipes_o, (_terms_o, per_o), allowed_o in zip(outcome_recipes, built, allowed_by_outcome):
                spec = _cooldown_screen_spec(
                    members,
                    chart,
                    cfg,
                    times,
                    note_weights,
                    perfect_sum,
                    music_coefficient,
                    recipes if recipes_o is None else recipes_o,
                    per_o,
                    team_rate_up,
                    song_seconds,
                    hosts,
                    leader_preps[0],
                    cooldown_variants,
                )
                if spec is not None and allowed_o is not None:
                    spec["allowed"] = allowed_o
                got = kernel(spec) if spec is not None else None
                if got is None:
                    values = None
                    break
                values = list(got) if values is None else [max(a, b) for a, b in zip(values, got)]
    if values is None:
        # every variant exactly (also the screen's answer when the kernel cannot give one)
        out = []
        for cooldowns in cooldown_variants:
            cfg_v = dataclasses.replace(cfg, board=cfg.board.with_cooldowns(cooldowns))  # type: ignore[union-attr]
            if active_elsewhere or costume_active:
                out.append(_score_song(members, leaders, chart, cfg_v, deck_power=deck_power, auto=auto))
                continue
            terms_v = [
                member_terms_for(cfg_v, per, recipes_use=r)[0] for r, (_t, per) in zip(outcome_recipes, built)
            ]
            out.append(tuple(walk_outcomes(prepared, terms_v, cfg_v) for prepared in leader_preps))
        if not cooldown_screen:
            return out
        values = [scores[0].total_score for scores in out]
    if cooldown_screen == "with_plain":
        return walk_outcomes(leader_preps[0], terms_by_outcome, cfg), values
    return values


def _score_by_chart_kernel(
    members: tuple[Card, ...],
    leaders: tuple[Card, ...],
    chart: SongChart,
    cfg: ScoringConfig,
    held: tuple,
    prepare_leader: Callable,
    leader_base: Callable,
    times: tuple[float, ...],
    note_weights: tuple[float, ...],
    perfect_sum: float,
    music_coefficient: float,
) -> tuple[SongScore, ...] | None:
    """`_score_song`'s answer with each leader's per-chart step in the chart kernel
    (`CHART_KERNEL`), the team's terms built once per board (`_chart_kernel_spec`, kept in
    `held` with the member half); None when there is no kernel, a model it does not take, or a
    team it declines -- the caller then walks in Python, which gives the same floats. A
    recipients tie is walked outcome by outcome over each one's own orders and the best kept,
    the earliest order on an exact tie, as `walk_outcomes` does."""
    kernel = _chart_kernel()
    if (
        kernel is None
        or _note_walk_kernel() is None
        or len(chart.special_cues) != SPECIAL_CUE_COUNT
        or cfg.stacking_model.name != "additive"
        or cfg.active_stacking_model.name != "max"
    ):
        return None
    (
        _gates,
        normal_resolved,
        team_rate_up,
        _active_rank,
        member_index_by_id,
        recipes,
        _member_score_rates,
        _stat_entries,
        _active_elsewhere,
        support_outcomes,
    ) = held[2]
    specs = held[4]
    outcomes = [(recipes, None)] if support_outcomes is None else list(support_outcomes)
    song_seconds = times[-1]
    out = []
    for leader in leaders:
        (
            _leader,
            leader_power,
            cast_power,
            board_power,
            board_score_rate,
            board_skill_effect_up,
            _costume_terms,
            score_rates,
            fixed_stat_sum,
        ) = prepare_leader(leader, with_costume_terms=False)
        power = (fixed_stat_sum + board_power) if leader_power is None else leader_power + cast_power
        # the walk's own expression, in its order
        constant = power * SCORE_COEFFICIENT * music_coefficient / perfect_sum * (1.0 + board_score_rate)
        best: tuple[float, tuple[int, ...]] | None = None
        best_index = 0
        for o, (recipes_o, allowed) in enumerate(outcomes):
            key = (o, leader.id)
            spec = specs.get(key, False)
            if spec is False:
                spec = specs[key] = _chart_kernel_spec(
                    members,
                    recipes_o,
                    leader_base(leader)[1],
                    cfg,
                    team_rate_up,
                    normal_resolved,
                    member_index_by_id,
                )
            if spec is None:
                return None
            got = kernel(
                spec,
                times,
                note_weights,
                chart.special_cues,
                song_seconds,
                constant,
                score_rates,
                board_skill_effect_up,
                allowed,
            )
            if got is None:
                return None
            index = _PERMUTATION_INDEX[got[1]]
            if best is None or got[0] > best[0] or (got[0] == best[0] and index < best_index):
                best, best_index = got, index
        assert best is not None  # an outcome always holds at least one order
        out.append(
            SongScore(
                leader_id=leader.id,
                total_score=best[0],
                member_order=tuple(members[i].id for i in best[1]),
                deck_power=power,
            )
        )
    return tuple(out)


def _chart_kernel_spec(
    members: tuple[Card, ...],
    recipes: list,
    costume_recipes: Sequence[tuple],
    cfg: ScoringConfig,
    team_rate_up: float,
    normal_resolved,
    member_index_by_id: Mapping[str, int],
) -> tuple | None:
    """One leader's terms as the chart kernel reads them (`CHART_KERNEL`), with everything no
    chart moves: per term, in the walk's order (the members' recipes, then the outfit's), `(kind,
    bucket, is_boost, owner, has recipients, recipients mask, SPECIAL member, combo gate (0 none,
    1 at least, 2 at most), its count, a member's term, magnitude, occupancy, an ACTIVE's interval
    or a SPECIAL's duration..., the magnitudes a combo-gated override takes on the checks reached
    and missed)`; per term and rider host, the occupancy of the boosted extra roll (-1: none,
    `_rider_terms`); per host `(member, gate (0 off, 1 on, 2 at least, 3 at most), count,
    duration)`. Owners and recipients are indices into `members`; buckets are numbered in order
    of first appearance, as `notewalk.pack_terms` numbers them. None for a team the kernel does
    not take."""
    index = {card.id: i for i, card in enumerate(members)}
    gates = {"off": 0, "on": 1, "gte": 2, "lte": 3}
    hosts = []
    for host in _rider_host_skills(normal_resolved):
        kind, count = _rider_gate(host, cfg)
        if kind in ("gte", "lte") and type(count) is not int:
            return None
        hosts.append(
            (
                member_index_by_id[host.card_id],
                _as_rate(float(host.skill.rider.magnitude)),  # type: ignore[attr-defined]
                gates[kind],
                count or 0,
                float(host.skill.duration),  # type: ignore[arg-type]
            )
        )
    buckets: dict = {}
    rows: list[tuple] = []
    rider_q: list[float] = []

    def place(term: _Term, resolved: ResolvedSkill, state: str, member_term: bool) -> bool:
        interval = duration = hit = miss = 0.0
        combo = count = 0
        if state != "applied":
            kind = 1
        elif resolved.skill.kind == "ACTIVE":
            kind = 2
            interval, duration = _active_lattice(resolved, cfg)
            rule = _combo_override(resolved, cfg) if member_term else None
            if rule is not None:
                high, base, applied, condition = rule
                if type(condition.count) is not int:
                    return False
                combo, count = (2 if condition.operator == "lte" else 1), condition.count
                hit = term.magnitude * high / applied
                miss = term.magnitude * base / applied
        elif term.special_member is not None:
            kind, duration = 3, float(term.special_duration)
        else:
            kind = 0
        targets_ = term.targets
        rows.append(
            (
                kind,
                buckets.setdefault(term.bucket, len(buckets)),
                1 if term.is_boost else 0,
                index.get(term.owner, -1),  # type: ignore[arg-type]
                0 if targets_ is None else 1,
                0 if targets_ is None else sum(1 << index[c] for c in targets_ if c in index),
                -1 if term.special_member is None else term.special_member,
                combo,
                count,
                1 if member_term else 0,
                float(term.magnitude),
                float(term.occupancy),
                interval,
                duration,
                hit,
                miss,
            )
        )
        for _member, rate, _gate, _count, _duration in hosts:
            q = -1.0
            if member_term and term.active_base is not None and term.occupancy > 0.0:
                boosted = cfg.activation_rate_model.apply(term.active_base, term.active_rate + rate)
                if not (term.occupancy >= 1.0 or boosted <= term.occupancy):
                    q = (boosted - term.occupancy) / (1.0 - term.occupancy)
            rider_q.append(q)
        return True

    for resolved, state, member, member_targets in recipes:
        term = _term_for(resolved, cfg, team_rate_up, state, -1.0, member, member_targets)
        if not place(term, resolved, state, True):
            return None
    for costume, state, extra in costume_recipes:
        term = _term_for(costume, cfg, team_rate_up, state, -1.0, None, *extra)
        if not place(term, costume, state, False):
            return None
    return (
        tuple(rows),
        tuple(rider_q),
        tuple((member, gate, count, duration) for member, _rate, gate, count, duration in hosts),
    )


def _cooldown_screen_spec(
    members,
    chart,
    cfg,
    times,
    note_weights,
    perfect_sum,
    music_coefficient,
    recipes,
    per_recipe,
    team_rate_up,
    song_seconds,
    hosts,
    prepared,
    variants,
) -> dict | None:
    """The team as the cooldown-screen kernel reads it (`screen_song_cooldown_variants`):
    the terms no cooldown moves (with their liveness), each ACTIVE skill's lattice and
    combo-gated override, the SPECIAL activation-rate riders on its checks, and each
    variant's per-member cooldown. Term fields are `notewalk.pack_terms`' (owner and
    recipients as member indices). None when a term has a shape the kernel does not model."""
    (
        _leader,
        leader_power,
        cast_power,
        board_power,
        board_score_rate,
        board_skill_effect_up,
        costume_terms,
        score_rates,
        fixed_stat_sum,
    ) = prepared
    power = (fixed_stat_sum + board_power) if leader_power is None else leader_power + cast_power
    constant = power * SCORE_COEFFICIENT * music_coefficient / perfect_sum * (1.0 + board_score_rate)
    index = {card.id: i for i, card in enumerate(members)}
    buckets: dict = {}

    def head(term) -> list[int]:
        targets = term.targets
        return [
            buckets.setdefault(term.bucket, len(buckets)),
            1 if term.is_boost else 0,
            index.get(term.owner, -1),
            0 if targets is None else 1,
            0 if targets is None else sum(1 << index[c] for c in targets if c in index),
        ]

    def cover(term) -> list[int]:
        flat: list[int] = []
        for cue in chart.special_cues:
            flat += (bisect_left(times, cue), bisect_left(times, cue + term.special_duration))
        return flat

    fixed_i: list[int] = []
    fixed_d: list[float] = []

    def add_fixed(term) -> int | None:
        if term.special_member is not None:
            if term.slot_cover is not None or len(chart.special_cues) != SPECIAL_CUE_COUNT:
                return None
            live, member, slots = 2, term.special_member, cover(term)
        elif term.windows is None:
            live, member, slots = 0, -1, [0] * (2 * SPECIAL_CUE_COUNT)
        elif term.windows == ():
            live, member, slots = 1, -1, [0] * (2 * SPECIAL_CUE_COUNT)
        else:
            return None
        fixed_i.extend(head(term) + [live, member] + slots)
        fixed_d.extend((float(term.magnitude), float(term.occupancy)))
        return len(fixed_d) // 2 - 1

    active_i: list[int] = []
    active_d: list[float] = []
    rider_host: list[int] = []
    rider_q: list[float] = []
    order: list[int] = []
    for k, (resolved, state, member, member_targets) in enumerate(recipes):
        if resolved.skill.kind != "ACTIVE":
            for term in per_recipe[k]:
                at = add_fixed(term)
                if at is None:
                    return None
                order += (0, at)
            continue
        if state != "applied":
            continue  # never live: no term to place
        term = _term_for(resolved, cfg, team_rate_up, state, song_seconds, member, member_targets)
        if term.special_member is not None or term.windows is None:
            return None
        # `_combo_split`'s rule, read once: the override's magnitude on the checks reached
        combo, count, single, hit, miss = 0, 0, float(term.magnitude), 0.0, 0.0
        rule = _combo_override(resolved, cfg)
        if rule is not None:
            high, base, applied, condition = rule
            combo = 2 if condition.operator == "lte" else 1
            count = int(condition.count)
            hit = term.magnitude * high / applied
            miss = term.magnitude * base / applied
        first = len(rider_q)
        if term.active_base is not None and 0.0 < term.occupancy < 1.0:
            for h, (_member, rate, _spans) in enumerate(hosts):
                boosted = cfg.activation_rate_model.apply(term.active_base, term.active_rate + rate)
                if boosted > term.occupancy:
                    rider_host.append(h)
                    rider_q.append((boosted - term.occupancy) / (1.0 - term.occupancy))
        active_i.extend(head(term) + [member, combo, count, first, len(rider_q) - first])
        active_d.extend(
            (
                float(term.occupancy),
                float(resolved.skill.interval),
                float(resolved.skill.duration),
                float(cfg.active_cooldown_reduction.get(resolved.member, 0.0)),
                single,
                hit,
                miss,
            )
        )
        order += (1, len(active_d) // 7 - 1)
    host_i: list[int] = []
    host_d: list[float] = []
    for member, _rate, spans in hosts:
        if len(spans) != SPECIAL_CUE_COUNT:
            return None
        host_i.append(member)
        host_i.extend(0 if span is None else 1 for span in spans)
        for span in spans:
            host_d.extend((0.0, 0.0) if span is None else (float(span[0]), float(span[1])))
    costume: list[int] = []
    for term in costume_terms:
        at = add_fixed(term)
        if at is None:
            return None
        costume.append(at)
    base_cd = [cfg.board.uptime_for(card.member)[1] for card in members]
    cd: list[float] = []
    for variant in variants:
        cd.extend(float(variant.get(card.member, base_cd[j])) for j, card in enumerate(members))
    return {
        "times": times,
        "note_weights": note_weights,
        "song_seconds": float(song_seconds),
        "constant": constant,
        "score_rates": [float(r) for r in score_rates],
        "skill_effect_up": float(board_skill_effect_up),
        "fixed_i": fixed_i,
        "fixed_d": fixed_d,
        "active_i": active_i,
        "active_d": active_d,
        "rider_host": rider_host,
        "rider_q": rider_q,
        "host_i": host_i,
        "host_d": host_d,
        "order": order,
        "costume": costume,
        "cd": cd,
        "variants": len(variants),
    }


@dataclass(frozen=True, slots=True)
class NoteRow:
    """One note of a team's run at its best slot order (`score_song_detailed(note_rows=)`).

    `base_value` is the note's score with no skill (`constant x judgement x (1 + combo)`,
    unrounded); `skill_rate` the expected skill multiplier at that instant (the `E[max]` of
    the live ACTIVEs times the live boosts, plus the always-on rates); `expected_value` =
    `ceil(base_value x (1 + skill_rate))`, the term the run's Expected adds up. `actives`:
    the ACTIVE score-up effects live at the note, strongest first, as `(card id, multiplier,
    chance it fired)`; `boosts`: the live SPECIAL / support boosts, `(card id, boost)`."""

    index: int
    time: float
    combo: int
    judgement: float
    combo_bonus: float
    base_value: float
    skill_rate: float
    expected_value: int
    actives: tuple[tuple[str | None, float, float], ...]
    boosts: tuple[tuple[str | None, float], ...]


def _note_rows(
    best,
    members,
    terms,
    fixed_bits,
    special_terms,
    cover_range,
    times,
    note_weights,
    combined,
    music_coefficient,
    perfect_sum,
    board_score_rate,
    chart,
    auto,
    platform,
) -> list[NoteRow]:
    """`best`'s note-by-note terms, rebuilt with the reference walk's own expressions (same
    liveness, same constant, same `ceil`), so they add up to `best.total_score` exactly."""
    index_of = {card.id: i for i, card in enumerate(members)}
    permutation = [index_of[card_id] for card_id in best.member_order]
    slot_of = [0] * SPECIAL_CUE_COUNT
    for slot, member in enumerate(permutation):
        slot_of[member] = slot
    bits = list(fixed_bits)
    for bit in special_terms:
        mask = 1 << bit
        low, high = cover_range[bit][slot_of[terms[bit].special_member]]
        for note_index in range(low, high):
            bits[note_index] |= mask
    constant = (
        best.deck_power * SCORE_COEFFICIENT * music_coefficient / perfect_sum * (1.0 + board_score_rate)
    )
    rows: list[NoteRow] = []
    for note_index, note in enumerate(chart.notes):
        live = bits[note_index]
        rate = combined(live)
        base = constant * note_weights[note_index]
        actives = sorted(
            (
                (term.owner, term.magnitude, term.occupancy)
                for bit, term in enumerate(terms)
                if live >> bit & 1 and not term.is_boost
            ),
            key=lambda a: -a[1],
        )
        boosts = tuple(
            (term.owner, term.magnitude)
            for bit, term in enumerate(terms)
            if live >> bit & 1 and term.is_boost
        )
        combo = 0 if auto else note_index + 1
        rows.append(
            NoteRow(
                index=note_index,
                time=times[note_index],
                combo=combo,
                judgement=(note.auto_permil if auto else note.perfect_permil_for(platform)) / 1000.0,
                combo_bonus=0.0 if auto else chart.combo_coefficient(combo),
                base_value=base,
                skill_rate=rate,
                expected_value=int(math.ceil(base * (1.0 + rate))),
                actives=tuple(actives),
                boosts=boosts,
            )
        )
    return rows


def _support_state(
    resolved: ResolvedSkill,
    members: tuple[Card, ...],
    cfg: ScoringConfig,
    state: str,
    active_rank: dict[str, float] | None,
) -> tuple[str, frozenset[str] | None]:
    """`scoring._bucket_term`'s support rule, restated for the timeline: a support
    whose resolved target set is empty contributes nothing.

    Returned as a state, so `_term_for` zeroes both magnitude and occupancy through
    the shared `_bucket_entry` path. The second element is
    `scoring._support_targets`' answer (`None` for a team-wide support, the resolved
    recipient set for a count-targeted one), the targeting the ranking drivers use."""
    if state != "applied":
        return state, None
    target_ids = targets.resolve_target_card_ids(
        resolved.skill.target,
        resolved.card_id,
        members,
        STAT_NAMES,
        cfg.target_selection_model,
        cfg.target_under_supply,
        rank_values=active_rank,
    )
    if not target_ids:
        return "gate_not_met", None
    return "applied", _support_targets(resolved, target_ids)
