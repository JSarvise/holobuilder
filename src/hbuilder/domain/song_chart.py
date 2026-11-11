"""The chart data the per-song scorer reads (`ChartNote`, `SongChart`, `SongScore`), the
game constants they carry, and chart-file loading (`ChartLibrary`, `load_charts`).

Split out of `song.py`, which re-exports every name here, so importers may use either."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from hbuilder.domain.scoring_config import (
    DEFAULT_PLATFORM as _DEFAULT_PLATFORM,
)
from hbuilder.domain.scoring_config import (
    PLATFORMS as _PLATFORMS,
)


class SongError(Exception):
    """Raised when a `SongChart` is structurally invalid, or when a team is the
    wrong shape for one. The message names the field and the count."""


# `Setting.live_deck_evaluation_coefficient_permil_multiply = 2300` in the game's
# master data (`LiveScoreCalculator._scoreCoefficient`). Not a `config.json` key:
# it is the game's own constant, with no choice for a user to make.
#
# Not the `K = 2.03734` of the deck screen's `Unit Score = Overall Power x
# (1 + Score Bonus) x K`: Unit Score and the in-run score are different computations.
SCORE_COEFFICIENT = 2.300


# A slide's invisible point (hidden relay or curve point) in `GetNotesPerfectCoefficientSum`:
# the client maps `NoteType` 5, 6 and 7 (LongRelayActive / LongRelayDeActive / LongBezierPoint)
# to the LONG_RELAY row, whose PERFECT weight is 100 permil on both columns.
HIDDEN_POINT_PERMIL = 100


# The `LiveCombo` master table, group `live_combo-1`, all 11 rows:
# `(combo_count_from, permil)`. `CalcScore`'s `(1 + ComboCoefficient)` reads the
# highest tier the running combo has reached.
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
# values `01 02 03 04 05` in strictly ascending time order (only the tutorial
# song `m9999` differs, with two). So slot i fires the i-th cue in time.
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
    `map[PERFECT][ConvertNoteTypeToLiveNoteType(rawNoteType)]`, looked up by whoever
    builds the chart:

    - the map is indexed `[judgement][noteType]`, and a missing pair scores 0, not a
      default: a GHOST note, with no PERFECT row, contributes 0.
    - it is the PC column (`pcScoreCoefficientPermilMultiply_`), where a FLICK is 1000
      permil; the mobile column differs in that one cell (1050). `flick` marks the note
      kind whose weight is platform-dependent and `perfect_permil_for(platform)` resolves
      it at scoring time (`ScoringConfig.platform`, default mobile), so chart files never
      bake a platform in.

    `time` is seconds from measure 0 of the beatmap, the origin the ACTIVE cooldown
    lattice uses, so the two share one axis."""

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
        """`map[AUTO=100][type]`: the numerator an AUTO-play run scores with.

        `CalcScore` looks up `JudgementCoefficient` by the note's actual judgement; on
        AUTO (`LIVE_NOTE_JUDGEMENT_TYPE_AUTO = 100`) a tap kind weighs 800 permil and a
        hold tick stays 100, so the map is exactly `1000 -> 800`, else identity. The AUTO
        row is 800 in both columns, FLICK included, so it is platform-independent."""
        return 800 if self.perfect_permil == 1000 else self.perfect_permil


@dataclass(frozen=True, slots=True)
class SongChart:
    """One chart, reduced to what `CalcScore` reads. Pure data, built by hand in tests
    and by `tools/refresh/` from a `.sus` plus the master tables.

    `difficulty_level` is `MusicDifficulty` field 3 and `live_score_coefficient_permil`
    is `Music` field 17, kept apart so `music_coefficient()`'s expression stays visible."""

    music_id: str
    difficulty: str
    difficulty_level: int
    live_score_coefficient_permil: int
    notes: tuple[ChartNote, ...]
    special_cues: tuple[float, ...]
    combo_tiers: tuple[tuple[int, int], ...] = LIVE_COMBO_TIERS
    # The song's CAST and the kind of song it is. Not read by `CalcScore`: the
    # inputs to the Holomem Board terms that depend on who sings (the
    # singer-conditional nodes and `song_score_bonus` at `solo`/`group` scope).
    #
    # `singer_type` is the game's `musicSingerType_` (3 whole / 2 unit / 1 solo),
    # spelled `all` / `group` / `solo` to match `BoardEffect.song_scope`. Absent
    # means no singer-conditional board term applies.
    singers: tuple[str, ...] = ()
    singer_type: str = ""
    # The slides' invisible points: hidden relay points (`LongRelayDeActive`) and curve
    # points (`LongBezierPoint`). They are never judged, so they score nothing and carry no
    # combo, and are not in `notes`; but the client builds a note for each and
    # `GetNotesPerfectCoefficientSum` weighs it as a LONG_RELAY (100 permil) in the divisor.
    hidden_points: int = 0

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
        # The cast is validated rather than normalised: a `solo` scope holding many
        # singers would silently pool their boards into a plausible wrong total. A
        # duo act (FUWAMOCO: chr-04016 + chr-04017) is a real 2-singer `solo`, whose
        # `solo` nodes sum under one cap, so 1 or 2 singers is admissible and 3+ is
        # refused as a mislabel.
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
                f"(true of every real chart); got {self.special_cues}"
            )
        if self.notes_perfect_coefficient_sum() <= 0.0:
            raise SongError(
                f"chart {self.music_id}/{self.difficulty} has a zero "
                f"NotesPerfectCoefficientSum — every note would score "
                f"infinitely; its note weights are wrong"
            )

    def music_coefficient(self) -> float:
        """`1 + (permil/1000) x (level - 5)`, as `LiveScoreCalculator`'s constructor
        computes it: below 1 under level 5, 0.968 .. 1.200 over the real charts."""
        return 1.0 + (self.live_score_coefficient_permil / 1000.0) * (self.difficulty_level - 5)

    def notes_perfect_coefficient_sum(self, platform: str = DEFAULT_PLATFORM) -> float:
        """`(1/1000) x SUM over notes of map[PERFECT][type]`,
        in the given client's column (`platform`; default mobile, where a FLICK
        weighs 1050 — see `ChartNote.perfect_permil_for`).

        The game accumulates int32 permil and calls `FloatFromPermil` once at the end, so
        this sums ints and divides once (the same rounding path).

        The divisor that makes chart length cancel in the ranking. The client sums it over
        every note it built from the chart, the slides' invisible points included
        (`hidden_points`, `HIDDEN_POINT_PERMIL` each): they score nothing, so on a chart
        with many curved slides every real note is worth less (m0528 EXPERT, 194 points:
        2.65 % less)."""
        return (
            sum(note.perfect_permil_for(platform) for note in self.notes)
            + HIDDEN_POINT_PERMIL * self.hidden_points
        ) / 1000.0

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

    `member_order` is never `None` here, unlike `ScoreBreakdown.member_order`: the
    SPECIAL cue assignment binds on every team, so there is always a best order."""

    leader_id: str
    total_score: float
    member_order: tuple[str, ...]
    deck_power: float


# ---------------------------------------------------------------------------
# Loading real charts
# ---------------------------------------------------------------------------
_CHARTS_SCHEMA = (
    '\nexpected {"schema_version": "charts-2", "charts": '
    '{"<music_id>/<DIFFICULTY>": {"music_id": "m0001", '
    '"difficulty": "EXPERT", "difficulty_level": 26, '
    '"live_score_coefficient_permil": 4, '
    '"notes": [[time, permil], [time, permil, flick01], ...], '
    '"special_cues": [t, ...]}}}'
)


@dataclass(frozen=True, slots=True)
class ChartLibrary:
    """Every chart a run may score against, keyed `"<music_id>/<DIFFICULTY>"`.

    A library rather than a bare dict so "which songs are held" and "which chart is
    this song's at this difficulty" each have one answer and one key spelling.
    Loaded from a path (`load_charts`); the file is built by
    `tools/refresh/build_charts.py`."""

    charts: Mapping[str, SongChart]
    source: str = "<mapping>"

    @staticmethod
    def key(music_id: str, difficulty: str) -> str:
        return f"{music_id}/{difficulty.upper()}"

    def get(self, music_id: str, difficulty: str = "EXPERT") -> SongChart | None:
        return self.charts.get(self.key(music_id, difficulty))

    def at(self, difficulty: str = "EXPERT") -> tuple[SongChart, ...]:
        """Every chart at one difficulty, in music-id order.

        EXPERT is the default because the ranking mode is Expert."""
        wanted = difficulty.upper()
        return tuple(
            chart for _key, chart in sorted(self.charts.items()) if chart.difficulty.upper() == wanted
        )

    def select(self, music_ids: Sequence[str] | None, difficulty: str = "EXPERT") -> tuple[SongChart, ...]:
        """The charts a run is ranked on: `music_ids` at `difficulty`, in
        music-id order. `None` or empty means every song, i.e. `at()`.

        A song it cannot serve is a named refusal, never a shorter answer (a mean over
        four songs must not pass for a mean over five). The message separates the two
        causes: an unknown song is a typo; a song with no chart at this difficulty
        means the library is the EXPERT-only `charts.json` and the caller wants
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
                parts.append(f"no song {', '.join(sorted(unknown))} in {self.source} ({len(known)} songs)")
            if missing:
                parts.append(
                    f"no {wanted} chart for {', '.join(sorted(missing))} — this "
                    f"library holds "
                    f"{', '.join(sorted({c.difficulty for c in self.charts.values()}))}; "
                    f"build every difficulty with "
                    f"`build_charts.py --all-out dumps/charts_all.json`"
                )
            raise SongError("; ".join(parts))

        return tuple(chart for chart in (self.get(m, wanted) for m in sorted(ordered)) if chart is not None)

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
    """The parsing half of `load_charts`, for a caller holding an already-decoded
    mapping. Every failure names the chart it came from."""
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
                # Both default to absent for a chart built without a cast (no
                # singer-conditional board term); `SongChart.__post_init__` refuses
                # a half-filled combination.
                singers=tuple(str(c) for c in row.get("singers", ())),
                singer_type=str(row.get("singer_type", "")),
                # absent from a chart file without the field: 0
                hidden_points=int(row.get("hidden_points", 0)),
            )
        except SongError as exc:
            # SongChart's own __post_init__ checks (no notes, times out of
            # order). Re-raised with the key so the message locates the chart.
            raise SongError(f"{source}: charts[{key!r}]: {exc}") from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise SongError(f"{source}: charts[{key!r}] is malformed: {exc}{_CHARTS_SCHEMA}") from exc

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

    `None` for an absent file, as `load_board` and `load_scoring_config`: `data/`
    ships no charts (they are extracted game data), and a run without them cannot
    answer a per-song question, a refusal for the caller to make."""
    file_path = Path(path)
    if not file_path.exists():
        return None
    try:
        raw = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SongError(f"{file_path}: unreadable chart file: {exc}") from exc
    return charts_from_mapping(raw, source=str(file_path))
