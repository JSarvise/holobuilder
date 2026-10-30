"""The per-song scorer: *what will this team actually score on THIS chart* —
a second scoring entry point, never a parameter on the first.

`scoring.py` answers "which team is best" and is deliberately free of any
song-length or per-song term (correctly so: `CalcScore` divides by
`NotesPerfectCoefficientSum`, the chart-wide sum, so length genuinely cancels
and a per-song scalar cannot reorder two teams). This module answers the other
question — the absolute one — and it needs a **time axis**, because three
things do NOT cancel:

1. `LiveScoreMusicCoefficient = 1 + (Music.liveScoreCoefficient
   Permil/1000) x (difficultyLevel - 5)`, range **0.968 .. 1.200** over the
   real charts. Easy charts are score-*penalised*, expert rewarded.
2. SPECIAL fire times are written into the chart — channel
   `#mmm0B`, one cue per deck slot, exactly five on every real chart. They are
   not probabilistic and not uniformly spread.
3. A SPECIAL Score UP is a `ScoreUpBoostSkillEffect`
   (`scoring._score_up_implements_boost`): it multiplies the ACTIVE maximum and
   is worth **nothing** where no ACTIVE is live. That is a statement about
   *when*, and it is uncomputable without a time axis. `scoring._score_up_pool`
   says so itself and points here.

**The governing premise is full-perfect play** (LIFE never leaves 1000, every
note PERFECT). That removes judgement variance entirely and makes the chart
deterministic; what remains stochastic is the ACTIVE activation roll, and this
module takes its **exact expectation** rather than sampling — see `_ratio_at`.

**What this module deliberately does not do:** it does not touch
`scoring.score_card_set`, `score_fast` or `_score_active`. Adding a song
parameter to those would silently add a per-song term for every existing caller.
`score_song` mirrors `score_card_set`'s positional signature exactly so a solver
need not know which of the two it is calling.

The model reproduces a fully-recorded run (801087 on `m0001` easy) to within
the five-run scatter, and rejects the alternative "SPECIAL adds" reading at +4.4
standard errors **at its own floor**, i.e. at every possible skill level.
"""

from __future__ import annotations

import dataclasses
import itertools
import json
import math
import os
import sys
from bisect import bisect_left
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from hbuilder.domain import targets
from hbuilder.domain.card import Card, SlotRider
from hbuilder.domain.gates import (
    ResolvedSkill,
    resolve_card_set_gates,
    resolve_leader_costume_skills,
)
from hbuilder.domain.schema import STAT_NAMES
from hbuilder.domain.scoring_config import (
    DEFAULT_PLATFORM as _DEFAULT_PLATFORM,
    PLATFORMS as _PLATFORMS,
    ScoringConfig,
)

# Every name below is `scoring.py`'s, imported rather than re-implemented, for
# exactly the reason `_score_up_pool` and `_board_bonus` are shared inside
# that module: one arithmetic definition, several readers, so the per-song
# scorer and the ranking scorer cannot silently drift apart. If one of these is
# renamed this module fails at IMPORT time — loudly, and before any number is
# produced — which is the intended failure mode.
#
# ⚠ This must be `_board_bonus`, not the narrower `_board_score_rate`.
# `_board_score_rate` reads ONLY the `cfg.board_score_bonus` dict; `_board_bonus`
# dispatches to `cfg.board` when a real `BoardModel` is set and returns a THIRD
# term. Calling the narrower one would leave two of the board's three channels
# silently zero here while `score_card_set` applies both. See
# `test_song_board_skill_effect_up.py`.
from hbuilder.domain.scoring import (
    STAT_NAMES_BY_EFFECT_TYPE,
    LeaderScore,
    ScoringError,
    _board_bonus,
    _bucket_entry,
    _effective_stat_sum,
    _FIXED_TARGET,
    _as_rate,
    _fold_activation_rate_up,
    _is_score_up_effect,
    _order_varying_indices,
    _ordered_target_card_ids,
    _pool_for,
    _resolve_skill,
    _score_up_implements_boost,
    _score_up_pool,
    _support_targets,
    _stat_sum,
    deck_screen_unit_score,
    score_with_breakdown,
)
from hbuilder.domain.unknown import is_known

__all__ = [
    "ChartNote",
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
    "overall_power_for",
    "score_song",
    "score_song_detailed",
]


class SongError(Exception):
    """Raised when a `SongChart` is structurally invalid, or when a team is the
    wrong shape for one. Mirrors `SchemaError`/`ScoringError`'s discipline: the
    message always names the field and the count, so a malformed chart surfaces
    as a named failure and never as a plausible wrong number."""


# `Setting.live_deck_evaluation_coefficient_permil_multiply = 2300`, from the
# game's master data. It is `_scoreCoefficient` @ `this+0x10` in
# `Vision.LiveCore!LiveScoreCalculator`.
#
# ⚠ Deliberately NOT a `config.json` key, for the same reason as
# `REFERENCE_RUN_SECONDS`: it is read out of the game's own master data, so there
# is no choice for a user to make. Adding it would report a defaulted value with
# `"file"` provenance for every existing config, which is the failure
# `load_scoring_config` exists to prevent.
#
# It is also **not** the `K = 2.0373445` of `Unit Score = Overall Power x
# (1 + Score Bonus) x K`. Unit Score (the deck screen) and the in-run score are
# different computations, and `2.0373445 / 2.300 = 0.885802` is unexplained; the
# two must not be conflated.
SCORE_COEFFICIENT = 2.300

# The `LiveCombo` master table, group `live_combo-1`, all 11 rows:
# `(combo_count_from, permil)`. `CalcScore`'s `(1 + ComboCoefficient)` reads the
# highest tier the running combo has reached, so a full combo on a 136-note
# chart never leaves the first two tiers.
LIVE_COMBO_TIERS: tuple[tuple[int, int], ...] = (
    (0, 0),
    (100, 10),
    (200, 20),
    (300, 30),
    (400, 40),
    (500, 50),
    (600, 60),
    (700, 70),
    (800, 80),
    (900, 90),
    (1000, 100),
)

# Channel `#mmm0B` carries exactly five cues on every real chart, with cell
# values `01 02 03 04 05` in strictly ascending time order — zero exceptions.
# The only charts that differ are the tutorial song `m9999`'s four
# difficulties, which carry two cues each. So the slot -> cue binding is
# unambiguous: slot i fires the i-th cue in time.
SPECIAL_CUE_COUNT = 5

# The one cell of the master's `LiveNote` PERFECT row that differs between the
# mobile and PC columns: FLICK is 1050 permil on mobile, 1000 on PC
# (`main_LiveNote.json`). `ChartNote.perfect_permil` is the PC
# base; `ChartNote.perfect_permil_for(platform)` applies this. Re-exported from
# `scoring_config` so `ScoringConfig.platform` and the chart agree on the names.
FLICK_MOBILE_PERMIL = 1050
PLATFORMS = _PLATFORMS
DEFAULT_PLATFORM = _DEFAULT_PLATFORM


@dataclass(frozen=True, slots=True)
class ChartNote:
    """One scored note. `perfect_permil` is already
    `map[PERFECT][ConvertNoteTypeToLiveNoteType(rawNoteType)]` — the lookup is
    done by whoever builds the chart, not here, because two corrections make it
    easy to get wrong and both belong at the boundary:

    - the map is indexed `[judgement][noteType]`, not the other way round,
      and **a missing pair scores 0.0, not a default** (both lookups are
      `TryGetValue` and both failure paths fall to `0.0f`). So a GHOST note,
      which has no PERFECT row, contributes `0` — not `1000`.
    - the PC (Steam) client reads the **PC** column, `LiveNote +0x2c`
      `pcScoreCoefficientPermilMultiply_`, where a FLICK is **1000 permil**.
      The mobile client's column (`scoreCoefficientPermilMultiply`) differs in
      exactly one cell of the 38: FLICK at PERFECT is **1050**
      (`main_LiveNote.json`; the mobile binary itself was not decompiled).
      `perfect_permil` therefore holds the **PC-column base** and `flick` marks
      the one note kind whose weight is platform-dependent;
      `perfect_permil_for(platform)` resolves it at scoring time
      (`ScoringConfig.platform`, default mobile), so the chart files never
      bake a platform in.

    `time` is seconds from measure 0 of the beatmap — the same origin the ACTIVE
    cooldown lattice uses, which is why the two
    can share one axis with no offset."""

    time: float
    perfect_permil: int
    flick: bool = False

    def perfect_permil_for(self, platform: str = DEFAULT_PLATFORM) -> int:
        """`map[PERFECT][type]` in the given client's column. Only a FLICK
        moves: 1050 on mobile, the PC base otherwise. A LONG_FLICK_END is 1000
        in both columns and is NOT flagged `flick`."""
        if self.flick and platform == "mobile":
            return FLICK_MOBILE_PERMIL
        return self.perfect_permil

    @property
    def auto_permil(self) -> int:
        """`map[AUTO=100][type]` — the numerator an AUTO-play run scores with.

        `LiveScoreCalculator::CalcScore` looks
        up `JudgementCoefficient` by the note's ACTUAL judgement. On AUTO that is
        `LIVE_NOTE_JUDGEMENT_TYPE_AUTO = 100`, whose PC column weights a
        tap/flick/long-start/long-end at **800 permil (0.80x** of PERFECT's 1000)
        and leaves hold-ticks at 100; a no-row note stays 0. Since `perfect_permil`
        already holds the PC-column PERFECT value — 1000 (taps) / 100 (ticks) /
        0 (ghost) — the AUTO map is exactly `1000 -> 800`, else identity.
        Platform-independent: the AUTO row is 800 in BOTH columns for every
        tap kind, FLICK included (`main_LiveNote.json` note_type 2 judgement
        100: 800/800), so a mobile flick loses its +50 on AUTO."""
        return 800 if self.perfect_permil == 1000 else self.perfect_permil


@dataclass(frozen=True, slots=True)
class SongChart:
    """One chart, reduced to exactly what `CalcScore` reads. Pure data, built by
    hand in tests and by `tools/recon/proxy/` from a `.sus` plus the master
    tables — `src/` never reads `dumps/`, which is gitignored.

    `difficulty_level` is `MusicDifficulty` field 3 and
    `live_score_coefficient_permil` is `Music` field 17; the two are kept as
    separate fields rather than one pre-multiplied coefficient so that
    `music_coefficient()`'s expression stays visible and testable in `src/`."""

    music_id: str
    difficulty: str
    difficulty_level: int
    live_score_coefficient_permil: int
    notes: tuple[ChartNote, ...]
    special_cues: tuple[float, ...]
    combo_tiers: tuple[tuple[int, int], ...] = LIVE_COMBO_TIERS
    # The song's CAST, and the kind of song it is. Not read by `CalcScore` —
    # these are the inputs to the two Holomem Board terms that are functions of
    # who sings rather than of the chart: the singer-conditional
    # `skill_effect_up` (e.g. 41 permil on Aki's own song, 0 without one) and
    # `song_score_bonus` at `solo`/`group` scope.
    #
    # `singer_type` is the game's `musicSingerType_`: 3 whole / 2 unit / 1 solo,
    # spelled here as `all` / `group` / `solo` to match `BoardEffect.song_scope`.
    # Both default to "absent", and absent means no singer-conditional board
    # term applies, so a chart without a cast scores exactly as a cast-blind
    # one.
    singers: tuple[str, ...] = ()
    singer_type: str = ""

    def __post_init__(self) -> None:
        if not self.notes:
            raise SongError(f"chart {self.music_id}/{self.difficulty} has no notes")
        previous = self.notes[0].time
        for note in self.notes[1:]:
            if note.time < previous:
                raise SongError(
                    f"chart {self.music_id}/{self.difficulty} notes are not in "
                    f"ascending time order ({note.time} after {previous})"
                )
            previous = note.time
        if len(self.special_cues) != SPECIAL_CUE_COUNT:
            raise SongError(
                f"chart {self.music_id}/{self.difficulty} must carry exactly "
                f"{SPECIAL_CUE_COUNT} `#mmm0B` SPECIAL cues, one per deck slot; "
                f"got {len(self.special_cues)}"
            )
        # The cast is validated rather than normalised, because every way of
        # getting it wrong produces a PLAUSIBLE number instead of an error. A
        # `solo` scope holding MANY singers silently pools their boards and clamps
        # to a total that looks reasonable (e.g. 120 permil where the real answer
        # is 40, from pooling every character). A genuine DUO act (FUWAMOCO:
        # chr-04016 + chr-04017) is a real 2-singer `solo`, which sums both members' `solo`-scope nodes under ONE cap
        # (`CalculateCurrentEffectValue` aggregates over the song's own singers,
        # filtered by `musicSingerType_`), which is exactly what
        # `song_score_bonus_for` does — so 1 OR 2 singers is admissible and 3+ is
        # the mislabel guard.
        if self.singer_type not in ("", "solo", "group", "all"):
            raise SongError(
                f"chart {self.music_id}/{self.difficulty} has singer_type "
                f"{self.singer_type!r}; expected one of 'solo', 'group', 'all' "
                f"(the game's musicSingerType_ 1/2/3) or '' for an unknown cast"
            )
        if self.singer_type and not self.singers:
            raise SongError(
                f"chart {self.music_id}/{self.difficulty} declares singer_type "
                f"{self.singer_type!r} but names no singers"
            )
        if self.singers and not self.singer_type:
            raise SongError(
                f"chart {self.music_id}/{self.difficulty} names singers but no "
                f"singer_type; the scope decides which board effects apply and "
                f"guessing it from the count is not the game's rule"
            )
        if self.singer_type == "solo" and not 1 <= len(set(self.singers)) <= 2:
            raise SongError(
                f"chart {self.music_id}/{self.difficulty} is singer_type 'solo' "
                f"but names {len(set(self.singers))} distinct singers; 'solo' admits "
                f"one, or two for a duo act (e.g. FUWAMOCO); got {self.singers}"
            )
        if list(self.special_cues) != sorted(self.special_cues):
            raise SongError(
                f"chart {self.music_id}/{self.difficulty} SPECIAL cues must be "
                f"in ascending time order — slot i fires the i-th cue "
                f"(MEASURED on 704/704 real charts); got {self.special_cues}"
            )
        if self.notes_perfect_coefficient_sum() <= 0.0:
            raise SongError(
                f"chart {self.music_id}/{self.difficulty} has a zero "
                f"NotesPerfectCoefficientSum — every note would score "
                f"infinitely; its note weights are wrong"
            )

    def music_coefficient(self) -> float:
        """`1 + (permil/1000) x (level - 5)`, read verbatim from
        `LiveScoreCalculator`'s ctor. `level - 5` is NEGATIVE below level 5, so
        an easy chart is score-*penalised*; over the real charts the value spans
        **0.968 .. 1.200** and never reaches zero."""
        return 1.0 + (self.live_score_coefficient_permil / 1000.0) * (
            self.difficulty_level - 5
        )

    def notes_perfect_coefficient_sum(self, platform: str = DEFAULT_PLATFORM) -> float:
        """`(1/1000) x SUM over notes of map[PERFECT][type]`,
        in the given client's column (`platform`; default mobile, where a FLICK
        weighs 1050 — see `ChartNote.perfect_permil_for`).

        The game accumulates **int32 permil** and calls `FloatFromPermil` once
        at the end, not per note, so this sums ints and divides once — a
        float-per-note version is not wrong but is a different rounding path.

        This is the divisor that makes chart length cancel in the ranking."""
        return sum(note.perfect_permil_for(platform) for note in self.notes) / 1000.0

    def flick_count(self) -> int:
        """How many notes carry the platform-dependent FLICK weight."""
        return sum(1 for note in self.notes if note.flick)

    def combo_coefficient(self, combo: int) -> float:
        """`FloatFromPermil` of the highest `LiveCombo` tier the running combo
        has reached. Under the full-perfect premise the combo never resets, so
        the caller passes `note_index + 1`."""
        permil = 0
        for threshold, tier_permil in self.combo_tiers:
            if combo >= threshold:
                permil = tier_permil
        return permil / 1000.0


@dataclass(frozen=True, slots=True)
class SongScore:
    """One leader's per-song answer, with the two things a `LeaderScore` cannot
    carry: the slot order that achieved it, and the deck power it was computed
    against.

    `member_order` is never `None` here, unlike `ScoreBreakdown.member_order`.
    The SPECIAL cue assignment binds on **every** team — a chart always has five
    cues and the members' SPECIALs differ in magnitude and duration — so there is
    always an order to report, and a tool which recommends a team without its slot order is handing the player the
    median case instead of the best one."""

    leader_id: str
    total_score: float
    member_order: tuple[str, ...]
    deck_power: float


# ---------------------------------------------------------------------------
# The timeline
# ---------------------------------------------------------------------------
#
# A `_Term` is one effect placed on the time axis: the same `(bucket, magnitude,
# occupancy)` triple `scoring._bucket_entry` builds, with the run-average
# occupancy REPLACED by an instantaneous one, plus the windows it is live in.
#
# `magnitude` comes from `_bucket_entry` rather than being recomputed, so the
# family weight, the override gate and the `state != "applied"` zeroing are all
# the shared implementations and cannot drift. **Only the occupancy differs, and
# that difference is the whole of this module:**
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
    # `scoring._score_up_pool`'s per-member targeting (deck panel `.3.100.6`):
    # the card that owns a ratio
    # term, and — for a `score_support` — `None` when it is team-wide or the
    # RESOLVED recipient set when it is count-targeted.
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


def _active_windows(
    resolved: ResolvedSkill, cfg: ScoringConfig, song_seconds: float
) -> tuple[tuple[float, float], ...]:
    """Every window an ACTIVE skill can open, on its cooldown lattice.

    The clock starts at **measure 0
    of the beatmap**, not at the first note, so activation check `k` sits at
    `k * interval` for `k = 1, 2, ...` — there is no check at `t = 0`.

    Only the Holomem Board shortens a cooldown, and it DIVIDES
    (`interval / (1 + reduction)`), keyed by HOLOMEM. That is the same
    expression `scoring._active_occupancy` uses; it is spelled out again here
    rather than imported because that function returns `min(1, duration /
    interval) * p` — a run average with the lattice already collapsed into it,
    and the lattice is precisely what a time axis needs back.

    Duration never reaches the cooldown in the real data, so a window cannot
    overlap the next check of the same skill; `scoring._score_active`'s
    self-overlap clamp is the run-average analogue of that fact and has no
    counterpart here.

    ⚠ The window end is deliberately **not** clamped to `song_seconds`, even
    though `tools/recon/proxy/chart_timeline.py` clamps it for display. Windows
    are half-open `[start, end)`, so clamping would push the song's LAST note
    out of a window that in fact covers it — a silent one-note loss on every
    skill still live at the end of the chart, and the last note is the one
    carrying the highest combo multiplier. A window running past the end simply
    covers no further notes."""
    # The Holomem Board's Act.CD nodes shorten this holomem's cooldown too, exactly
    # as `scoring._active_occupancy` folds them in — read the board's `uptime_for`
    # cooldown term ALONGSIDE the hand-supplied reduction, or the per-song scorer
    # values every board cooldown node at 0 and per-song plans under-develop them
    # (the general scorer already adds it; the two must not disagree on the interval).
    board_cooldown = cfg.board.uptime_for(resolved.member)[1] if cfg.board is not None else 0.0
    reduction = cfg.active_cooldown_reduction.get(resolved.member, 0.0) + board_cooldown
    interval = float(resolved.skill.interval) / (1.0 + reduction)
    duration = float(resolved.skill.duration)
    return _lattice_windows(interval, duration, song_seconds)


# `(interval, duration, song_seconds) -> the windows`, one tuple OBJECT per
# lattice. The windows are a function of those three numbers alone, and a
# search asks for the same few lattices millions of times; returning the same
# object is also what lets `_window_note_ranges` key on identity.
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
            tuple(
                (bisect_left(times, start), bisect_left(times, end))
                for start, end in windows
            ),
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
    # The fourth field — `ActiveTerm`'s `exclusive` — is READ AND DISCARDED, on
    # purpose. It says "over a whole run these five SPECIAL windows do not
    # collide", a fact `scoring._windows_are_mutually_exclusive` measures across
    # the chart corpus and which lets the run-average path sum the SPECIAL
    # bucket instead of discounting it for collisions that never happen. This
    # module does not need it and must not use it: it has the actual cue times
    # of the actual chart on an actual axis, so on the 0.64 % of cue pairs a
    # long SPECIAL really can bridge it SEES the overlap, and the maximum has to
    # be taken for real. `_ratio_at` therefore emits `exclusive=False` for every
    # term. A corpus-level fact must never overrule ground truth.
    bucket, magnitude, _run_average_occupancy, _interval, _corpus_exclusive = _bucket_entry(
        resolved, cfg, team_rate_up, weight, state
    )
    is_boost = _pool_for(resolved) == "support" or _score_up_implements_boost(resolved)
    if state != "applied":
        # `_bucket_entry` already zeroed the magnitude; the occupancy is zeroed
        # too, for the reason it states — a zero magnitude alone would still
        # consume the `E[max]` walk's remaining probability, letting a gate that
        # did not fire suppress a skill that did.
        return _Term(bucket, 0.0, 0.0, is_boost, (), None, 0.0, resolved.card_id, targets)

    kind = resolved.skill.kind
    if kind == "ACTIVE":
        # Fold the board's activation-rate (Act.Prob) term into the proc chance too,
        # mirroring `scoring._active_occupancy` (`apply(base_prob, team_rate_up +
        # board_rate)`). Without it the per-song scorer ignores every board rate node.
        board_rate = cfg.board.uptime_for(resolved.member)[0] if cfg.board is not None else 0.0
        base_probability = cfg.probability_tiers[resolved.skill.probability_tier]
        probability = cfg.activation_rate_model.apply(
            base_probability, team_rate_up + board_rate)
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
        # Occupancy `1.0`, not `cfg.expected_fires_per_run * duration /
        # REFERENCE_RUN_SECONDS`: the chart states the fire time, so on a
        # concrete song a SPECIAL is not an expectation at all. This is exactly
        # the term `scoring._special_occupancy` warns is uncomputable without a
        # time axis.
        return _Term(
            bucket,
            magnitude,
            1.0,
            is_boost,
            None,
            member_index,
            float(resolved.skill.duration),
            resolved.card_id,
            targets,
        )
    # PASSIVE and COSTUME: live for the whole run.
    return _Term(bucket, magnitude, 1.0, is_boost, None, None, 0.0, resolved.card_id, targets)


def _combo_split(resolved: ResolvedSkill, cfg: ScoringConfig, term: _Term,
                 times: tuple[float, ...]) -> list[_Term]:
    """An ACTIVE term whose override is COMBO-gated ("With a Combo of 40 or more, Score
    UP 100 %"), split by window: the override's magnitude on the checks the chart's own
    combo has reached (the notes before the check, full-combo premise), the base on the
    rest. The value snapshots at the check, as the game's effect is registered then.
    `gates` resolves the override once from the static `run_state.combo` (100), which
    would grant it from the first check, so the combo is re-read at each check here.
    AUTO keeps the static premise -- the combo never accumulates there, so the
    override never fires. The
    two parts never share a note (a skill's windows do not overlap), so the walk's
    `E[max]` is unchanged in kind."""
    override = resolved.skill.override
    condition = getattr(override, "condition", None)
    if (
        cfg.song_auto
        or cfg.run_state.combo <= 0          # a premise of no combo at all, as AUTO
        or resolved.skill.kind != "ACTIVE"
        or not term.windows
        or getattr(condition, "kind", None) != "run_state"
        or condition.target != "combo"
        or not is_known(override.magnitude)
    ):
        return [term]
    base, high = float(resolved.skill.magnitude), max(float(override.magnitude), float(resolved.skill.magnitude))
    applied = high if (resolved.override_gate is not None and resolved.override_gate.fired) else base
    if applied <= 0.0 or high == base:
        return [term]

    def reached(start: float) -> bool:
        combo = bisect_left(times, start)
        return combo <= condition.count if condition.operator == "lte" else combo >= condition.count
    hit = tuple(w for w in term.windows if reached(w[0]))
    miss = tuple(w for w in term.windows if not reached(w[0]))
    out = []
    if hit:
        out.append(dataclasses.replace(term, magnitude=term.magnitude * high / applied, windows=hit))
    if miss:
        out.append(dataclasses.replace(term, magnitude=term.magnitude * base / applied, windows=miss))
    return out


def _rider_gate_at(resolved: ResolvedSkill, combo: int) -> bool:
    """Whether a SPECIAL's rider fires at a cue reached with `combo` notes behind
    it: a COMBO gate against the chart's own count there (full-combo premise), any
    other gate as resolved for the run (`gates`, the static run state)."""
    condition = resolved.skill.rider.condition
    if getattr(condition, "kind", None) == "run_state" and condition.target == "combo":
        if condition.operator == "lte":
            return combo <= condition.count
        return combo >= condition.count
    return resolved.rider_gate is None or resolved.rider_gate.fired


def _rider_terms(
    normal_resolved,
    member_index_by_id: Mapping[str, int],
    member_terms: list[_Term],
    cfg: ScoringConfig,
    chart: SongChart,
    times: tuple[float, ...],
) -> list[_Term]:
    """The SPECIAL activation-rate riders, on the time axis.

    About 30 cards' SPECIAL carries a second
    in-run effect, `live_active_skill_activation_probability_up_permil_up` (350-550
    permil), live for the SPECIAL's own duration. Every ACTIVE check in that window
    rolls `trunc(base x (1 + (maxACTIVE + maxSPECIAL + extra)/1000))`: the rider is
    `maxSPECIAL`, deck-wide (it raises EVERY member's ACTIVE) and a maximum (two live
    riders do not stack). The run-average path folds it time-averaged
    (`scoring._fold_rider_activation_rate_up`); on a chart the cue times are known.

    A boosted check is the SAME roll with `p'` for `p`. Each becomes one extra term:
    the ACTIVE term's bucket, magnitude and owner, occupancy `q = (p' - p)/(1 - p)`,
    live on that window only when the rider's host sits in a slot whose SPECIAL
    window holds the check. In `E[max]` two terms of one magnitude and bucket with
    `p` and `q` are exactly one term with `1 - (1-p)(1-q) = p'` (the walk's survival
    product over an equal-magnitude block is order-free), so this is exact, and it
    is slot-dependent like a SPECIAL -- `special_member` = the host, which the
    120-order walk and the C kernel already handle. One term per (ACTIVE, host,
    k-th covered window), since a term's coverage is one range per slot.

    Two riders live on one check would combine as `1 - (1-p)(1-q1)(1-q2)` here, not
    the game's maximum: SPECIAL windows almost never overlap (about 0.64 % of cue
    pairs), so the difference is treated as negligible."""
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
            continue                          # the SPECIAL itself is gated off
        hosts.append(resolved)
    if not hosts:
        return []
    out: list[_Term] = []
    cues = chart.special_cues
    for host in hosts:
        member = member_index_by_id[host.card_id]
        duration = float(host.skill.duration)
        rate = _as_rate(float(host.skill.rider.magnitude))
        spans = [
            (cue, cue + duration) if _rider_gate_at(host, bisect_left(times, cue)) else None
            for cue in cues
        ]
        for term in member_terms:
            if term.active_base is None or not term.windows or term.occupancy <= 0.0:
                continue
            boosted = cfg.activation_rate_model.apply(term.active_base, term.active_rate + rate)
            if term.occupancy >= 1.0 or boosted <= term.occupancy:
                continue
            q = (boosted - term.occupancy) / (1.0 - term.occupancy)
            per_slot = [
                [w for w in term.windows if span is not None and span[0] <= w[0] < span[1]]
                for span in spans
            ]
            for k in range(max(len(ws) for ws in per_slot)):
                out.append(_Term(
                    term.bucket, term.magnitude, q, term.is_boost, None, member, 0.0,
                    term.owner, term.targets,
                    slot_cover=tuple(ws[k] if k < len(ws) else None for ws in per_slot),
                ))
    return out


def _ratio_at(
    live_ratio: list[_Term],
    live_boost: list[_Term],
    cfg: ScoringConfig,
    skill_effect_up: float = 0.0,
) -> float:
    """`SkillScoreUpRatio` at one instant, through `scoring._score_up_pool`.

    The whole per-song model is this one substitution: `_score_up_pool` is
    handed the SAME `(bucket, magnitude, occupancy)` triples the ranking scorer
    builds, with `occupancy` now meaning *"is this effect live at this tick, and
    with what probability"* instead of *"what fraction of the run is it live"*.
    Everything downstream — both `E[max]` calls, the `B` multiplier, the SPECIAL
    collapse — is `scoring.py`'s, unchanged and unforked. Swapping
    `cfg.active_stacking_model` to the alternative `SummedActiveSkills` therefore
    still works here, and still means the same thing.

    Two consequences, and they are what make this exact rather than a
    simulation:

    - **The ACTIVE roll's expectation is closed-form.** At an instant each live
      ACTIVE is an independent Bernoulli(p) and `ExpectedMaximumActiveSkill`
      computes `E[max]` over them exactly. No sampling, no seed, no variance.
    - **Expectation is linear and the score is linear in the ratio**, so summing
      the per-note expectations gives the exact expectation of the run total
      (modulo the per-note `ceil`, see `score_song_detailed`). Neighbouring notes
      share rolls, which matters for a variance estimate and not for a mean.

    `skill_effect_up` is the Holomem Board's `SkillEffectUpPermilUp`, passed
    through to exactly the slot `CalcScoreUpRatio` puts it in — INSIDE the
    `(1 + SkillEffectUpPermilUp/1000 + B)` parenthesis that multiplies the
    ACTIVE pool, never as a summand of the combined rate. It defaults to `0.0`
    because that IS the answer for a team with no board — which also means a
    test without board data cannot tell whether it is applied.

    **And it is why `exclusive` is `False` on every term this builds.** That flag
    is the run-average path's licence to sum a bucket instead of maximising it,
    earned by the chart corpus's cue geometry. Here the caller has already
    filtered `live_ratio`/`live_boost` to the effects live AT THIS INSTANT and
    each carries occupancy `1.0` (SPECIAL) or `P(tier)` (ACTIVE), so
    `ExpectedMaximumActiveSkill`'s ordinary walk takes the true maximum: two
    SPECIALs live together contribute `mag_1 * 1.0 + mag_2 * 1.0 * (1 - 1.0)`,
    i.e. the bigger one and nothing else. Passing `exclusive=True` here would
    SUM them, which is right on the 99.4 % of cue pairs that cannot overlap and
    silently double-counts on the rest — the one case this module exists to get
    right."""
    # `interval` is `0` on every term: the per-song scorer places effects on a
    # real time axis and always runs under the default `max` combiner, which
    # ignores the element. It emits `exclusive=False` for the reason the
    # docstring gives; the lattice group key is irrelevant here and 0 keeps the
    # 5-tuple shape `_score_up_pool` unpacks.
    return _score_up_pool(
        [(term.bucket, term.magnitude, term.occupancy, 0, False) for term in live_ratio],
        [(term.bucket, term.magnitude, term.occupancy, 0, False) for term in live_boost],
        cfg,
        skill_effect_up,
        [term.owner for term in live_ratio],
        [term.targets for term in live_boost],
        # The instant-model switch. `None` (the default) hands `_score_up_pool`
        # no model at all, so it uses `cfg.active_stacking_model` — the same
        # object, the same call. Set to `ACTIVE_INSTANT_MODELS["exclusive"]`
        # the co-live ACTIVEs share one roll instead of each rolling
        # independently.
        # It is passed HERE and only here because this is the one place in the
        # codebase where `occupancy` is an instantaneous probability: the
        # run-average scorer's occupancies are fractions of a run and the
        # exclusive law is meaningless against them.
        cfg.active_instant_model,
    )


def _fixed_liveness(term: _Term, times: tuple[float, ...]) -> list[bool]:
    """Per-note liveness of an order-INVARIANT term."""
    if term.windows is None:
        return [True] * len(times)
    if not term.windows:
        return [False] * len(times)
    return [
        any(start <= time < end for start, end in term.windows) for time in times
    ]


# ---------------------------------------------------------------------------
# Deck power for the ABSOLUTE path
# ---------------------------------------------------------------------------


def overall_power_for(
    members: tuple[Card, ...], leader: Card, cfg: ScoringConfig
) -> float:
    """The deck screen's own **Overall Power** for one (leader, members) pair —
    the right `deck_power` for an ABSOLUTE per-song score.

    `score_song_detailed`'s default is `stat_sum + board_power`, the modelled
    stat pool. Overall Power additionally carries the two account scalars (the
    Memory Bonus and the power-up permyriad) and uses the float32-ceiling stat
    block, and it is the quantity the game multiplies. It matches the server
    bit-exact, so this DELEGATES rather than re-deriving: a private
    second copy would drift out of that agreement with nothing to notice.

    ⚠ **It is a function of the LEADER**, which is why it takes one rather than
    a whole `leaders` tuple — the leader's own board power and the leader-scoped
    stat effects are both in it. Pass it to `score_song_detailed` as the
    per-leader callable:

        score_song_detailed(members, leaders, chart, cfg,
                            deck_power=lambda ldr: overall_power_for(members, ldr, cfg))

    NOT as a scalar computed once for the first leader: that is invisible, since
    every leader then scales by the same wrong factor and only the absolute
    numbers are wrong."""
    from hbuilder.domain.team import Team

    return deck_screen_unit_score(
        score_with_breakdown(Team(leader=leader, members=members), cfg)
    ).overall_power


# ---------------------------------------------------------------------------
# The entry point
# ---------------------------------------------------------------------------


def score_song(
    members: tuple[Card, ...],
    leaders: tuple[Card, ...],
    chart: SongChart,
    cfg: ScoringConfig,
    *,
    deck_power: float | Callable[[Card], float] | None = None,
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

    maximised over the 120 slot orders (jointly with the count-limited passive
    term when one binds — see `score_song_detailed`).

    **The `ceil` is applied per note, which is the only place it belongs**:
    `CalcScore` computes ONE note's score and the run
    total is the sum of per-note `ceil`s. It is deliberately NOT applied to a
    ranking quantity, where it would quantise the ordering and hand the
    tie-break to a card id.

    `deck_power` defaults to the same `_effective_stat_sum` `score_card_set`
    uses. ⚠ **That is the modelled stat pool, not the game's `DeckPower`**,
    which additionally carries the passive-skill uplift, the Holomem Board and
    the Memory Bonus, and which `data/cards.json` does not hold. For an
    absolute number that means anything, pass the deck screen's own Overall
    Power. For a *comparison* between teams on one song the default is the right
    quantity and the absolute scale is arbitrary.

    Use `score_song_detailed` when the slot order matters to the caller — on a
    real recommendation it always does."""
    return tuple(
        LeaderScore(leader_id=score.leader_id, total_score=score.total_score)
        for score in score_song_detailed(
            members, leaders, chart, cfg, deck_power=deck_power, auto=auto
        )
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

    ⛔ **It reuses `_active_windows` and `chart.special_cues` rather than
    recomputing either.** A display that derives its own windows is a SECOND
    definition of when a skill is live, and the first time the two disagree the
    picture becomes an argument against the score instead of an explanation of
    it. Everything here is read off the same functions `score_song` scores with.

    `member_order` is the slot order, so lane `i` is the member the chart's
    `i`-th cue fires (cue values `01..05` in ascending time on every real
    chart), and that binding is the whole reason the order matters.

    Returns plain data — no rendering decisions, which belong to whatever draws
    it. Times are seconds from measure 0, and windows are half-open `[start,
    end)`, exactly as `_active_windows` produces them: NOT clamped to the song's
    end, because clamping would push the last note out of a window that covers
    it, and the last note carries the highest combo multiplier."""
    by_id = {card.id: card for card in members}
    gates = resolve_card_set_gates(members, cfg.run_state)
    song_seconds = chart.notes[-1].time if chart.notes else 0.0

    lanes = []
    for slot, card_id in enumerate(member_order):
        card = by_id.get(card_id)
        if card is None:
            continue
        cue = chart.special_cues[slot] if slot < len(chart.special_cues) else None
        active: list[dict] = []
        special: list[dict] = []
        for resolved in gates.normal_by_card_id.get(card_id, ()):  # type: ignore[union-attr]
            kind = resolved.skill.kind
            if kind == "ACTIVE":
                for start, end in _active_windows(resolved, cfg, song_seconds):
                    active.append({
                        "start": start,
                        "end": end,
                        # The probability is why an ACTIVE window is a CHANCE and
                        # a SPECIAL window is a fact. Drawing them identically
                        # would state something the model does not.
                        "probability_base": _activation_probability(resolved, cfg),
                        "magnitude": float(resolved.skill.magnitude or 0.0),
                    })
            elif kind == "SPECIAL" and cue is not None:
                duration = float(resolved.skill.duration or 0.0)
                special.append({
                    "start": cue,
                    "end": cue + duration,
                    "magnitude": float(resolved.skill.magnitude or 0.0),
                })
        lanes.append({
            "slot": slot + 1,
            "card_id": card_id,
            "member": card.member,
            "cue": cue,
            "active": active,
            "special": special,
        })

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


def active_coverage(drawing: Mapping) -> dict:
    """Two visibility numbers for a ranking row, read off `timeline()`'s OWN
    windows so they cannot disagree with the picture:

    * `active_cover_pct` — share of the song `[0, song_seconds)` with at least
      one ACTIVE window live (union, clamped to the song's end).
    * `active_overlap_loss_pct` — how much of the SUMMED active value the
      engine's MAX rule discards: `1 - ∫E[max] / ∫Σ(mag·p)`, with independent
      per-window proc chances `probability_base` (`None` counts as certain).
      0 when actives never overlap; grows as they stack on top of each other.
    * `active_dead_start_s` — seconds from measure 0 to the first ACTIVE window
      (structural: no ACTIVE fires before its own first interval).

    ⚠ These are DISPLAY numbers built from `probability_base` (the tier alone,
    a lower bound on the real chance — see `_activation_probability`); the
    ranking's own metric is the exact per-song score. Not a second scorer."""
    song_seconds = float(drawing.get("song_seconds") or 0.0)
    windows: list[tuple[float, float, float, float]] = []
    for lane in drawing.get("lanes", ()):
        for w in lane.get("active", ()):
            start = float(w["start"])
            end = min(float(w["end"]), song_seconds)
            if end <= start or start >= song_seconds:
                continue
            p = w.get("probability_base")
            windows.append((start, end, float(w.get("magnitude") or 0.0),
                            1.0 if p is None else float(p)))
    if song_seconds <= 0.0:
        return {"active_cover_pct": 0.0, "active_overlap_loss_pct": 0.0,
                "active_dead_start_s": 0.0}
    if not windows:
        return {"active_cover_pct": 0.0, "active_overlap_loss_pct": 0.0,
                "active_dead_start_s": song_seconds}
    points = sorted({0.0, song_seconds, *(x for w in windows for x in w[:2])})
    covered = 0.0
    e_max = 0.0
    e_sum = 0.0
    for a, b in zip(points, points[1:]):
        mid = (a + b) / 2.0
        live = [(m, p) for s, e, m, p in windows if s <= mid < e]
        if not live:
            continue
        covered += b - a
        live.sort(reverse=True)
        acc = 1.0
        em = 0.0
        for m, p in live:
            em += m * p * acc
            acc *= 1.0 - p
        e_max += em * (b - a)
        e_sum += sum(m * p for m, p in live) * (b - a)
    loss = 0.0 if e_sum <= 0.0 else 100.0 * (1.0 - e_max / e_sum)
    return {
        "active_cover_pct": 100.0 * covered / song_seconds,
        "active_overlap_loss_pct": loss,
        "active_dead_start_s": min(s for s, _e, _m, _p in windows),
    }


# An optional compiled replacement for the separable fast path's note walk in
# `score_song_detailed`. INJECTED by the web/recon layer (`tools/web/kernel`),
# never imported here, so this package keeps `dependencies = []`. It is called as
#
#     kernel(note_weights, liveness, constant, terms, score_rates,
#            board_skill_effect_up, special_terms, cover_range, member_ids, cfg)
#
# where `liveness` is `(always_mask, ((bit, note-index ranges), ...))` — the
# per-note live-term bitmask in the form it is known BEFORE it is spread over
# the notes (`_window_note_ranges`), so a kernel spreads it itself —
# and returns `(total, permutation)` — or `None` to decline, in which case the
# Python below runs. The contract is BIT-IDENTITY with that Python, held by
# `tools/web/kernel/test_notewalk.py`; a kernel that approximates is a bug.
NOTE_WALK_KERNEL: Callable | None = None

# The same hook for WORKER PROCESSES. On Windows a pool spawns fresh interpreters,
# so a kernel assigned to `NOTE_WALK_KERNEL` in the parent is not there in the
# children — but the environment is inherited. `HBUILDER_NOTE_WALK_KERNEL` names
# a Python FILE and an attribute, `<dir>/notewalk.py:walk`; it is loaded by path
# on first use, so this package still imports nothing it does not ship. A kernel
# that fails to load is reported once on stderr and the Python path is used.
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
            module_spec = importlib.util.spec_from_file_location(
                "_hbuilder_note_walk_kernel", file_name
            )
            module = importlib.util.module_from_spec(module_spec)
            module_spec.loader.exec_module(module)
            kernel = getattr(module, attribute)
        except Exception as exc:  # noqa: BLE001 - any failure means "use Python"
            print(
                f"note-walk kernel {spec!r} could not be loaded ({exc!r}); "
                "scoring continues on the Python path",
                file=sys.stderr,
            )
    _env_kernel_state = (spec, kernel)
    return kernel

# `(note_weights, perfect_sum)` per chart, platform and judgement column. Both
# are functions of the chart alone, and rebuilding them costs ~7 % of every call
# (~2,900 calls per team into `combo_coefficient` / `perfect_permil_for`). Keyed
# by object identity with the chart kept alive in the entry, so a recycled `id()` can never serve another chart's weights.
_chart_weights_cache: dict[tuple[int, str, bool], tuple] = {}

# The chart's note times as one tuple, per chart — the same identity-keyed
# shape as the weights above, and for the same reason: a function of the chart
# alone that every call would otherwise rebuild note by note.
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
        note_weights = tuple(
            ((note.auto_permil if auto else note.perfect_permil_for(platform)) / 1000.0)
            * (1.0 + chart.combo_coefficient(index + 1))
            for index, note in enumerate(chart.notes)
        )
        if len(_chart_weights_cache) > 4096:
            _chart_weights_cache.clear()
        hit = (chart, note_weights, chart.notes_perfect_coefficient_sum(platform))
        _chart_weights_cache[key] = hit
    return hit[1], hit[2]


def _activation_probability(resolved, cfg: ScoringConfig) -> float:
    """The ACTIVE's per-check proc chance, or `None` when it cannot be stated.

    ⚠ The BASE tier only. `scoring._score_active` also folds in `team_rate_up`
    and the board's `uptime_for` rate through `cfg.activation_rate_model`; both
    need the whole team's resolved rate-up fold, which this function does not
    take. So this is a LOWER bound on the real chance and the caller must label
    it as one rather than print it as the number.

    `None` and not a default when the tier is missing: a lane drawn at a
    made-up probability would look like a measurement."""
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
    deck_power: float | Callable[[Card], float] | None = None,
    auto: bool | None = None,
    order_totals: dict | None = None,
) -> tuple[SongScore, ...]:
    """`score_song`, plus the slot order and the deck power each total used.

    `order_totals` (a dict, opt-in): every leader's 120 slot orders are walked by the
    verbatim reference walk below -- the one the fast paths are proven bit-identical to --
    and each order's total is recorded as `order_totals[(leader_id, member_order)]`. The
    returned scores are the same; only the per-order totals are new. For a team played
    on many songs with ONE slot order (the event's Highlight), never inside a search.

    **The slot order is a JOINT maximum, not two separate ones.** Slot order
    binds on two independent mechanisms — which chart cue fires each member's
    SPECIAL (always), and which members receive a count-limited passive stat
    buff (only when a target's matchers outnumber its own `count`,
    `scoring._order_varying_indices`). Maximising the two separately would report
    an order achieving both optima at once where no single deck can, i.e. an
    **upper** bound — the one direction the `>=` lower-bound marker cannot
    express. So when the passive term binds, both are evaluated
    inside the same 120-permutation walk. When it does not — the common case,
    and the O(5) precondition — the stat sum is order-invariant and the joint
    maximum reduces exactly to the cue assignment, at no extra cost.

    Cost is `O(leaders x 120 x notes)` dict lookups plus `O(distinct live sets)`
    calls to `_score_up_pool`. That is a per-song answer, not a hot loop: call it
    on a shortlist the ranking solver produced, never inside `C(N,6)`."""
    if len(members) != SPECIAL_CUE_COUNT:
        raise SongError(
            f"score_song needs exactly {SPECIAL_CUE_COUNT} members — one per "
            f"deck slot, because the chart carries one `#mmm0B` cue per slot; "
            f"got {len(members)}"
        )
    times = _chart_times(chart)
    song_seconds = times[-1]
    card_set_gates = resolve_card_set_gates(members, cfg.run_state)

    normal_resolved: list[ResolvedSkill] = []
    for card in members:
        normal_resolved.extend(card_set_gates.normal_by_card_id[card.id])
    team_rate_up = _fold_activation_rate_up(tuple(normal_resolved), cfg)

    # --- the member half, shared by every candidate leader ------------------
    member_index_by_id = {card.id: index for index, card in enumerate(members)}
    member_terms: list[_Term] = []
    member_score_rates: list[float] = []
    stat_entries: list = []

    for resolved in normal_resolved:
        pool = _pool_for(resolved)
        if pool == "none":
            continue
        rate, state, _missing = _resolve_skill(resolved, cfg, team_rate_up)
        weight = cfg.family_weights[resolved.family]
        index = member_index_by_id[resolved.card_id]
        if pool == "stat":
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
            support_state, support_targets = _support_state(
                resolved, members, cfg, state
            )
            member_terms.extend(_combo_split(resolved, cfg,
                _term_for(
                    resolved,
                    cfg,
                    team_rate_up,
                    support_state,
                    song_seconds,
                    index,
                    support_targets,
                ), times))
        elif _is_score_up_effect(resolved):
            member_terms.extend(_combo_split(
                resolved, cfg,
                _term_for(resolved, cfg, team_rate_up, state, song_seconds, index), times))
        else:
            # `life` / `judgment` effects, and PASSIVE/COSTUME Score UPs. They
            # stay individually summed exactly as `_score_team` sums them:
            # whether a PASSIVE Score UP is in the game's effect collection at
            # all is not known (`scoring._is_score_up_effect`), and between two
            # unverified readings the one that changes nothing wins.
            member_score_rates.append(rate * weight)

    # the SPECIAL activation-rate riders, slot-dependent extra rolls on the ACTIVE checks
    member_terms.extend(_rider_terms(
        normal_resolved, member_index_by_id, member_terms, cfg, chart, times))

    # --- the leader half ----------------------------------------------------
    # The principal, then the outfit's scored `score_support` rider
    # (`gates.resolve_leader_costume_skills`) — walked per leader below in the
    # same order `score_card_set` walks them.
    leader_costumes: dict[str, tuple[ResolvedSkill, ...]] = {}
    for card in leaders:
        costume_skills = resolve_leader_costume_skills(
            card, card_set_gates.aggregates, cfg.run_state
        )
        if costume_skills:
            leader_costumes[card.id] = costume_skills

    music_coefficient = chart.music_coefficient()
    # The client's column: `cfg.platform` (mobile by default) decides whether a
    # FLICK weighs 1050 or 1000, in BOTH the divisor and the per-note numerator.
    platform = cfg.platform

    # `w_n x (1 + combo_n)` — the note's whole chart-side weight. Order- and
    # leader-invariant, so it is built once. The JUDGEMENT numerator swaps to the
    # AUTO column for an auto-play run; the NPCS DIVISOR (`perfect_sum`, above)
    # STAYS on PERFECT — in `CalcScore` the Judgement is read by the
    # note's actual judgement while GetNotesPerfectCoefficientSum is frozen at the
    # Perfect column.
    # `auto` None: the config's own premise (`cfg.song_auto`), so a caller that does
    # not pass it (e.g. `parallel_refine`) still scores an AUTO config on the AUTO column
    if auto is None:
        auto = bool(cfg.song_auto)
    note_weights, perfect_sum = _chart_weights(chart, platform, auto)

    # ⚠ The stat entries are PER LEADER, because a leader's stat outfit is one
    # of them. They cannot be hoisted out of the loop below: hoisting them would
    # score every leader with the same outfit.
    member_stat_entries = stat_entries
    orderings_cache: dict = {}

    # `deck_power` may be a per-leader CALLABLE (see `overall_power_for`), and it
    # is resolved ONCE per leader, here — never inside the note walk or the
    # 120-order loop, which run millions of times on a real chart and would pay
    # a caller's real work (scoring a whole team) every iteration.
    resolve_power = deck_power if callable(deck_power) else None

    results: list[SongScore] = []
    for leader in leaders:
        leader_power = (
            float(resolve_power(leader)) if resolve_power is not None
            else (None if deck_power is None else float(deck_power))
        )
        # ⚠ Read PER LEADER, and it cannot be hoisted. `skill_effect_up` is
        # leader-scoped — a red node applies to the whole team, but only while
        # its own holomem leads — so hoisting it out of this loop would hand
        # every leader the first one's board and erase the one board channel
        # that can reorder leaders at all. (`score_bonus_rate` IS leader-
        # invariant; it is returned from the same call rather than computed
        # separately so the two cannot drift.)
        #
        # The power term joins the modelled stat pool only when the caller
        # passes no `deck_power`; a caller's Overall Power already carries it.
        board_power, board_score_rate, board_skill_effect_up = _board_bonus(
            leader, members, cfg
        )
        # the singer-conditional stat power below, kept apart so it survives a
        # caller's Overall Power (`leader_power`), which has no song cast
        cast_power = 0.0
        # The two terms that are functions of the SONG'S CAST rather than of the
        # chart. Both are no-ops for a chart with no cast.
        if cfg.board is not None and chart.singers:
            # `song_score_bonus` at this song's own scope SUPERSEDES the pooled
            # `all` bucket `_board_bonus` returned. Pooling is exact for a
            # whole-roster song and over-counts `solo` and `group` — `all` is
            # the only scope where the two agree, and there they agree exactly.
            board_score_rate = cfg.board.song_score_bonus_for(
                chart.singers, scope=chart.singer_type
            )
            # ...and the conditional `skill_effect_up` joins the unconditional
            # one. They are the SAME addend in `CalcScoreUpRatio`'s parenthesis,
            # so they add here rather than entering separately.
            board_skill_effect_up += cfg.board.conditional_skill_effect_up_for(
                members, None if leader is None else leader.member, chart.singers
            )
            # ...and the singer-conditional STAT power (R-044/062/063: "+stat% to
            # all when this holomem sings") joins the board's Overall-Power summand,
            # since `_board_bonus`/`_terms` leave it out as conditional. A no-op for a
            # cast none of whose singers hold such a node.
            #
            # ⚠ It also rides ON TOP of a caller's `deck_power` (the deck screen's
            # Overall Power, the leaf's default): that figure has no song cast, so
            # it cannot carry these nodes, and taking it in place of `board_power`
            # would drop them (an R-044 node would score +0.00 % instead of about
            # +2.58 %).
            cast_power = cfg.board.conditional_stat_power_for(
                members, None if leader is None else leader.member, chart.singers
            )
            board_power += cast_power
        terms = list(member_terms)
        score_rates = list(member_score_rates)
        leader_stat_entry = None
        for costume in leader_costumes.get(leader.id, ()):
            pool = _pool_for(costume)
            if pool != "none":
                rate, state, _missing = _resolve_skill(costume, cfg, team_rate_up)
                weight = cfg.family_weights[costume.family]
                if pool == "support":
                    support_state, support_targets = _support_state(
                        costume, members, cfg, state
                    )
                    terms.append(
                        _term_for(
                            costume,
                            cfg,
                            team_rate_up,
                            support_state,
                            song_seconds,
                            None,
                            support_targets,
                        )
                    )
                elif pool == "score":
                    if _is_score_up_effect(costume):
                        terms.append(
                            _term_for(
                                costume, cfg, team_rate_up, state, song_seconds, None
                            )
                        )
                    else:
                        score_rates.append(rate * weight)
                elif pool == "stat":
                    # ⚠ Without this branch the whole per-song score is
                    # LEADER-BLIND. The leader contributes no stats OF ITS OWN
                    # CARD, but its OUTFIT skill raises the MEMBERS' stats, and
                    # `score_card_set` builds `stat_entries` **per leader**,
                    # starting with exactly this entry (`scoring.py`, the
                    # `ordered_resolved` loop), before calling the same
                    # `_effective_stat_sum`. Almost every leader outfit is
                    # `stat_up`, so skipping it would flatten every ranking.
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

        # The leader's outfit goes FIRST, which is `Team.effective_skill_
        # instances()`'s order and the order `score_card_set` builds
        # `ordered_resolved` in. It is not cosmetic: `_order_varying_indices`
        # returns INDICES into this list and `_stat_sum` aligns `recipients`
        # with it positionally, so a different order silently mis-assigns
        # recipients.
        stat_entries = (
            [leader_stat_entry, *member_stat_entries]
            if leader_stat_entry is not None
            else member_stat_entries
        )
        varying = _order_varying_indices(stat_entries, members)
        order_binds_stats = bool(varying) and cfg.slot_order_rule == "best_order"
        fixed_stat_sum = 0.0
        if not order_binds_stats:
            # Provably order-invariant, so one call answers every permutation —
            # and it is `_effective_stat_sum`'s own call, so the number is
            # bit-identical to `score_card_set`'s.
            fixed_stat_sum, _order = _effective_stat_sum(
                members, stat_entries, cfg, orderings_cache
            )

        # Liveness as a bitmask per note, so the 120-permutation walk is dict
        # lookups rather than interval arithmetic. Bit `i` is `terms[i]`.
        # Built with `bisect` over the ascending note times rather than a
        # per-note scan. `times` is ascending (`SongChart.__post_init__`
        # enforces it), so `[bisect_left(times, start), bisect_left(times, end))`
        # is exactly `{i : start <= times[i] < end}` — `_fixed_liveness`'s own
        # predicate. Index logic only, and `_fixed_liveness` is kept as the
        # reference the tests compare this against. A per-note scan costs ~31 %
        # of the fast path.
        note_count = len(times)
        # The always-live terms first, so every note STARTS from their mask
        # instead of having it OR-ed in by a second pass over the notes. Bitwise
        # OR on ints: the order the bits arrive in cannot change the result.
        always_mask = 0
        for bit, term in enumerate(terms):
            if term.special_member is None and term.windows is None:
                always_mask |= 1 << bit          # live for the whole run
        special_terms: list[int] = []
        window_ranges: list[tuple[int, tuple]] = []
        for bit, term in enumerate(terms):
            if term.special_member is not None:
                special_terms.append(bit)
                continue
            if term.windows is None:
                continue
            # `windows == []` falls through here and sets nothing, which is
            # `_fixed_liveness`'s "never live" case.
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

        # `cover_range[bit][slot]` — the half-open note-INDEX range a SPECIAL
        # covers when its member sits in that slot. Same membership as the list
        # it replaces (`cue <= t < cue + duration`), five slots, computed once.
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
                live_ratio = [
                    term
                    for bit, term in enumerate(terms)
                    if live >> bit & 1 and not term.is_boost
                ]
                live_boost = [
                    term
                    for bit, term in enumerate(terms)
                    if live >> bit & 1 and term.is_boost
                ]
                combined = cfg.stacking_model.combine(
                    [
                        *score_rates,
                        _ratio_at(
                            live_ratio, live_boost, cfg, board_skill_effect_up
                        ),
                    ]
                )
                pool_cache[live] = combined
            return combined

        # ------------------------------------------------------------------
        # The 5x5 assignment decomposition, and the two conditions it needs.
        # ------------------------------------------------------------------
        # The permutation-dependent part of the total is a 5x5 (member -> slot)
        # cost matrix, i.e. a linear assignment, under exactly two conditions,
        # and both are CHECKED here rather than assumed —
        # which is what makes the fast path provably exact rather than
        # optimistically so.
        #
        # 1. Power must be permutation-invariant. When the count-limited
        #    passive term binds, `power` varies with the order and multiplies
        #    every note INSIDE the `ceil`, so no note's value is
        #    order-independent and the joint maximum this function's docstring
        #    warns about is real. An explicit `deck_power` makes it moot: the
        #    walk discards the per-permutation stat sum anyway.
        # 2. No note may be reachable from two DIFFERENT slots' cue windows by
        #    two DIFFERENT members' SPECIALs. Two members in one slot cannot
        #    happen; one member across two slots cannot either. But two members
        #    in two slots covering one note makes the per-note combine a `max`
        #    across them, which does not split into independent per-slot
        #    contributions. This happens on about 3 % of (team, chart) pairs.
        #
        # When both hold, every note is touched by at most one assigned member:
        #
        #     total(perm) = fixed_total + SUM over slots s of delta[perm[s]][s]
        #
        # Every per-note value is `math.ceil(...)` of the identical expression
        # the walk computes, so every summand is an integer-valued float far
        # below 2^53 and re-associating the sum is EXACT — the 120 totals are
        # bit-identical to the walk's, so the argmax and the strict-`>`
        # tie-break pick the identical `member_order`.
        separable_power = ((leader_power is not None) or not order_binds_stats) and order_totals is None
        coupled = False
        if separable_power and special_terms:
            spans = [
                (slot, terms[bit].special_member, lo, hi)
                for bit in special_terms
                for slot, (lo, hi) in enumerate(cover_range[bit])
                if lo < hi
            ]
            for index, (slot_a, member_a, lo_a, hi_a) in enumerate(spans):
                for slot_b, member_b, lo_b, hi_b in spans[index + 1:]:
                    if (
                        slot_a != slot_b
                        and member_a != member_b
                        and lo_a < hi_b
                        and lo_b < hi_a
                    ):
                        coupled = True
                        break
                if coupled:
                    break

        if separable_power and not coupled:
            power = (fixed_stat_sum + board_power) if leader_power is None else leader_power + cast_power
            # The identical expression in the identical order as the walk's.
            constant = (
                power
                * SCORE_COEFFICIENT
                * music_coefficient
                / perfect_sum
                * (1.0 + board_score_rate)
            )
            kernel_result = None
            note_walk_kernel = _note_walk_kernel()
            if note_walk_kernel is not None:
                kernel_result = note_walk_kernel(
                    note_weights, (always_mask, window_ranges), constant, terms, score_rates,
                    board_skill_effect_up, special_terms, cover_range,
                    tuple(card.id for card in members), cfg,
                )
            if kernel_result is not None:
                best_total, best_permutation = kernel_result
                results.append(
                    SongScore(
                        leader_id=leader.id,
                        total_score=best_total,
                        member_order=tuple(members[i].id for i in best_permutation),
                        deck_power=power,
                    )
                )
                continue
            fixed_bits = _spread_liveness()
            base_values = [0.0] * note_count
            fixed_total = 0.0
            for note_index in range(note_count):
                value = math.ceil(
                    constant
                    * note_weights[note_index]
                    * (1.0 + _combined(fixed_bits[note_index]))
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

            # The 120 sums are enumerated rather than solved with Hungarian /
            # Jonker-Volgenant, and that is deliberate: at n=5 the argmax is
            # ~600 additions against a cost-matrix build that IS the whole
            # cost, and enumeration reproduces the walk's strict-`>`
            # first-permutation tie-break for free where a solver would have to
            # re-derive it.
            best_total: float | None = None
            best_permutation: tuple[int, ...] | None = None
            for permutation in itertools.permutations(range(SPECIAL_CUE_COUNT)):
                total = fixed_total
                for slot, owner in enumerate(permutation):
                    total += delta[owner][slot]
                if best_total is None or total > best_total:
                    best_total = total
                    best_permutation = permutation
            assert best_permutation is not None  # 120 is never an empty walk
            results.append(
                SongScore(
                    leader_id=leader.id,
                    total_score=best_total,
                    member_order=tuple(members[i].id for i in best_permutation),
                    deck_power=power,
                )
            )
            continue

        if separable_power and coupled:
            # The coupled case with an order-invariant power: the kernel walks the
            # 120 orders itself (`hd_walk_general`), bit-identical to the walk below.
            # A SPECIAL rider's boosted ACTIVE window running into the next slot's
            # SPECIAL makes this common (e.g. 134 of 200 random teams on m0334),
            # and the Python walk is ~100x the kernel.
            note_walk_kernel = _note_walk_kernel()
            if note_walk_kernel is not None:
                power = (fixed_stat_sum + board_power) if leader_power is None else leader_power + cast_power
                constant = (
                    power
                    * SCORE_COEFFICIENT
                    * music_coefficient
                    / perfect_sum
                    * (1.0 + board_score_rate)
                )
                kernel_result = note_walk_kernel(
                    note_weights, (always_mask, window_ranges), constant, terms, score_rates,
                    board_skill_effect_up, special_terms, cover_range,
                    tuple(card.id for card in members), cfg, general=True,
                )
                if kernel_result is not None:
                    best_total, best_permutation = kernel_result
                    results.append(
                        SongScore(
                            leader_id=leader.id,
                            total_score=best_total,
                            member_order=tuple(members[i].id for i in best_permutation),
                            deck_power=power,
                        )
                    )
                    continue

        # The verbatim 120-permutation walk, kept as the fallback for the two
        # cases the decomposition cannot serve. It is unchanged apart from
        # reading `cover_range` and the shared `_combined` — so the fast path
        # can be compared against it directly, forever, by a test.
        best: SongScore | None = None
        if fixed_bits is None:
            fixed_bits = _spread_liveness()
        for permutation in itertools.permutations(range(SPECIAL_CUE_COUNT)):
            # `permutation[s]` is the index (into `members`) of the card in slot
            # `s`, so `slot_of[i]` is the slot member `i` occupies.
            slot_of = [0] * SPECIAL_CUE_COUNT
            for slot, member in enumerate(permutation):
                slot_of[member] = slot

            if order_binds_stats:
                ordered = tuple(members[i] for i in permutation)
                recipients = [
                    _ordered_target_card_ids(
                        entry[3], entry[4], ordered, cfg.target_under_supply
                    )
                    if index in varying
                    else entry[2]
                    for index, entry in enumerate(stat_entries)
                ]
                stat_sum = _stat_sum(members, stat_entries, cfg, recipients)
            else:
                stat_sum = fixed_stat_sum
            power = (stat_sum + board_power) if leader_power is None else leader_power + cast_power

            bits = list(fixed_bits)
            for bit in special_terms:
                mask = 1 << bit
                low, high = cover_range[bit][slot_of[terms[bit].special_member]]
                for note_index in range(low, high):
                    bits[note_index] |= mask

            constant = (
                power
                * SCORE_COEFFICIENT
                * music_coefficient
                / perfect_sum
                * (1.0 + board_score_rate)
            )
            total = 0.0
            for note_index in range(len(times)):
                total += math.ceil(
                    constant
                    * note_weights[note_index]
                    * (1.0 + _combined(bits[note_index]))
                )
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
        results.append(best)

    return tuple(results)


def _support_state(
    resolved: ResolvedSkill,
    members: tuple[Card, ...],
    cfg: ScoringConfig,
    state: str,
) -> tuple[str, frozenset[str] | None]:
    """`scoring._bucket_term`'s support rule, restated for the timeline: a
    support whose resolved target set is EMPTY never enters the effect
    collection and contributes nothing.

    Returned as a *state* rather than as a separate flag so that `_term_for`
    zeroes both the magnitude and the occupancy through the one shared
    `_bucket_entry` path — the reason that function gives for zeroing both.

    The second element is `scoring._support_targets`' answer — `None` for a
    team-wide support, the resolved recipient set for a count-targeted one —
    so `_ratio_at` can hand `_score_up_pool` the same per-member targeting
    the ranking drivers use."""
    if state != "applied":
        return state, None
    target_ids = targets.resolve_target_card_ids(
        resolved.skill.target,
        resolved.card_id,
        members,
        STAT_NAMES,
        cfg.target_selection_model,
        cfg.target_under_supply,
    )
    if not target_ids:
        return "gate_not_met", None
    return "applied", _support_targets(resolved, target_ids)


# ---------------------------------------------------------------------------
# Loading real charts
# ---------------------------------------------------------------------------
_CHARTS_SCHEMA = (
    "\nexpected {\"schema_version\": \"charts-2\", \"charts\": "
    "{\"<music_id>/<DIFFICULTY>\": {\"music_id\": \"m0001\", "
    "\"difficulty\": \"EXPERT\", \"difficulty_level\": 26, "
    "\"live_score_coefficient_permil\": 4, "
    "\"notes\": [[time, permil], [time, permil, flick01], ...], "
    "\"special_cues\": [t, ...]}}}"
)


@dataclass(frozen=True, slots=True)
class ChartLibrary:
    """Every chart a run may score against, keyed `"<music_id>/<DIFFICULTY>"`.

    A library rather than a bare dict because two questions are asked of it
    constantly and both should have one answer: *which songs are held?* and
    *which chart is this song's, at this difficulty?* Doing that at the call
    site invites two spellings of the key.

    ⚠ **`src/` never reads `dumps/`** — this takes a PATH, exactly as
    `load_board` does, and the default the CLI passes happens to point there.
    The file is built by `tools/recon/proxy/build_charts.py`."""

    charts: Mapping[str, SongChart]
    source: str = "<mapping>"

    @staticmethod
    def key(music_id: str, difficulty: str) -> str:
        return f"{music_id}/{difficulty.upper()}"

    def get(self, music_id: str, difficulty: str = "EXPERT") -> SongChart | None:
        return self.charts.get(self.key(music_id, difficulty))

    def at(self, difficulty: str = "EXPERT") -> tuple[SongChart, ...]:
        """Every chart at one difficulty, in music-id order.

        EXPERT is the default because the ranking mode is *always* Expert —
        a mode that silently averaged four difficulties
        would answer a question nobody asked."""
        wanted = difficulty.upper()
        return tuple(
            chart for _key, chart in sorted(self.charts.items())
            if chart.difficulty.upper() == wanted
        )

    def select(
        self, music_ids: Sequence[str] | None, difficulty: str = "EXPERT"
    ) -> tuple[SongChart, ...]:
        """The charts a run is ranked on: `music_ids` at `difficulty`, in
        music-id order. `None` or empty means every song, i.e. `at()`.

        This exists so "which songs, at which difficulty" is resolved in ONE
        place. `at()` answers "every song, one difficulty"; a player plays a
        handful of songs and not always on Expert.

        **A song it cannot serve is a NAMED refusal, never a shorter answer.**
        Dropping one silently turns "the mean over the five songs you play"
        into a mean over four, with nothing in the output to say so — the same
        class of failure as a chart filed under the wrong key, and the reason
        that one is checked at load. The message separates the two causes,
        because they have different fixes: a song the library has never heard
        of is a typo, and a song with no chart AT THIS DIFFICULTY means the
        library is the EXPERT-only `charts.json` and the caller wants
        `charts_all.json` (`build_charts.py --all-out`).

        A repeated song collapses rather than being weighted twice."""
        wanted = difficulty.upper()
        if not music_ids:
            return self.at(wanted)

        ordered: list[str] = []
        for music_id in music_ids:
            if music_id not in ordered:
                ordered.append(music_id)

        known = set(self.music_ids)
        unknown = [m for m in ordered if m not in known]
        missing = [m for m in ordered if m in known and self.get(m, wanted) is None]
        if unknown or missing:
            parts = []
            if unknown:
                parts.append(
                    f"no song {', '.join(sorted(unknown))} in {self.source} "
                    f"({len(known)} songs)"
                )
            if missing:
                parts.append(
                    f"no {wanted} chart for {', '.join(sorted(missing))} — this "
                    f"library holds "
                    f"{', '.join(sorted({c.difficulty for c in self.charts.values()}))}; "
                    f"build every difficulty with "
                    f"`build_charts.py --all-out dumps/charts_all.json`"
                )
            raise SongError("; ".join(parts))

        return tuple(
            chart
            for chart in (self.get(m, wanted) for m in sorted(ordered))
            if chart is not None
        )

    @property
    def music_ids(self) -> tuple[str, ...]:
        return tuple(sorted({chart.music_id for chart in self.charts.values()}))

    def __len__(self) -> int:
        return len(self.charts)


def _chart_note(entry: object) -> ChartNote:
    """One `notes` row of the chart file, either shape (see `charts_from_mapping`)."""
    if not isinstance(entry, (list, tuple)) or len(entry) not in (2, 3):
        raise ValueError(f"note {entry!r} is not [time, permil] or [time, permil, flick]")
    time, permil = entry[0], entry[1]
    flick = bool(int(entry[2])) if len(entry) == 3 else False
    return ChartNote(time=float(time), perfect_permil=int(permil), flick=flick)


def charts_from_mapping(raw: object, *, source: str = "<mapping>") -> ChartLibrary:
    """The parsing half of `load_charts`, split out so a caller holding an
    already-decoded mapping (a recon tool, a test) need not write a file first.

    Every failure names the chart it came from. A chart library is thousands of
    numbers produced by a pipeline, and "invalid chart" without a key is not a
    diagnosis — it is a search."""
    if not isinstance(raw, dict):
        raise SongError(f"{source}: chart file must be an object{_CHARTS_SCHEMA}")
    charts_raw = raw.get("charts")
    if not isinstance(charts_raw, dict):
        raise SongError(f"{source}: missing object key 'charts'{_CHARTS_SCHEMA}")

    charts: dict[str, SongChart] = {}
    for key, row in charts_raw.items():
        if not isinstance(row, dict):
            raise SongError(f"{source}: charts[{key!r}] must be an object")
        try:
            # `[time, permil]` (charts-1) or `[time, permil, flick]` (charts-2):
            # `permil` is the PC-column base and `flick` (0/1) marks the notes
            # whose weight is platform-dependent. A two-element row reads as
            # flick-less, i.e. it scores on PC weights.
            notes = tuple(_chart_note(entry) for entry in row["notes"])
            chart = SongChart(
                music_id=str(row["music_id"]),
                difficulty=str(row["difficulty"]).upper(),
                difficulty_level=int(row["difficulty_level"]),
                live_score_coefficient_permil=int(row["live_score_coefficient_permil"]),
                notes=notes,
                special_cues=tuple(float(cue) for cue in row.get("special_cues", ())),
                # Both default to absent, and absent is the honest state for a
                # chart built without a cast: the singer-conditional board
                # terms then behave as they do for a song whose cast is not
                # known, rather than as if the song
                # had none. `SongChart.__post_init__` refuses the half-filled
                # combinations, so a partially-populated file fails loudly.
                singers=tuple(str(c) for c in row.get("singers", ())),
                singer_type=str(row.get("singer_type", "")),
            )
        except SongError as exc:
            # SongChart's own __post_init__ checks (no notes, times out of
            # order). Re-raised with the key so the message locates the chart.
            raise SongError(f"{source}: charts[{key!r}]: {exc}") from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise SongError(
                f"{source}: charts[{key!r}] is malformed: {exc}{_CHARTS_SCHEMA}"
            ) from exc

        expected = ChartLibrary.key(chart.music_id, chart.difficulty)
        if key != expected:
            raise SongError(
                f"{source}: charts[{key!r}] describes {expected!r} — a key that "
                f"disagrees with its own contents would make `get` miss it"
            )
        charts[key] = chart
    return ChartLibrary(charts=charts, source=source)


def load_charts(path: str | Path) -> ChartLibrary | None:
    """A `ChartLibrary` from a chart file, or `None` if the file is absent.

    `None` for an absent file is the same first-class-state contract
    `load_board` and `load_scoring_config` have: `data/` ships no charts
    because they are ~3 MB of extracted game data, and a run without them
    simply cannot answer a per-song question — which is a refusal to make,
    not an error to raise here."""
    file_path = Path(path)
    if not file_path.exists():
        return None
    try:
        raw = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SongError(f"{file_path}: unreadable chart file: {exc}") from exc
    return charts_from_mapping(raw, source=str(file_path))
