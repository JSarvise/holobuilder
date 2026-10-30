"""The `hbuilder` controller: argparse subcommand dispatch, the one shared
load-and-guard function every subcommand calls, and the `solve` and `gates`
subcommands.

`gates` is the per-team gate report: it shares the load/validate/guard idiom
(`load_inputs`) with `solve`, and its report renderer is `format_gates_report`.

Exit codes are a public contract, not an accident of control flow:

  0 success (including empty/degenerate-but-valid results and --estimate)
  1 internal error (uncaught exception; never raised deliberately)
  2 RESERVED for argparse's own usage errors only
  3 input-data error (cards.json/owned.json missing, unreadable, malformed)
  4 config error (config.json fails validate_scoring_config)
  5 invalid selector (unknown/conflicting --require/--leader/--exclude id,
    or an unowned id without --include-unowned; also a bad --json output
    path (unwritable directory, permissions) — a command-line-argument
    problem, not cards.json/owned.json data and not an internal bug)
  6 insufficient pool (fewer than MEMBER_COUNT candidates after selectors)
  7 declined confirmation (the isatty-gated confirmation prompt)

6 and 7 must never collapse into one another. An `--acquire`-style loop
reads 6 as "skip this candidate, continue the loop" and 7 as "abort the
loop entirely" — merging them gives that loop either an infinite prompt or
silently skipped candidates.

Registers exactly two subcommands, `solve` and `gates` — no `extract` and
no `fetch`: a subcommand this CLI cannot honor is a promise it will break.

Mode A vs mode B. `--cards` (default `data/cards.json`) ranks on MAXED
stats and is the default. `--real-stats <path>` ranks on the account's
REAL measured stats and is opt-in; a mode-B run announces itself
on stderr, at the head of the human report, and as a `stats_basis` key in
the `--json` payload. This CLI writes neither `data/cards.json` nor
`data/owned.json` on any path, in either mode; the mode-B document is
produced out-of-band by `tools/recon/proxy/build_mode_b.py`.

`solve`'s output surface: `--explain`/`--json` are orthogonal depth/format
choices that never constrain each other; a constrained-pool arrangement
projection prints to stderr before every search, alongside periodic
progress; `--estimate` answers the scale question without enumerating; and
the `isatty`-gated confirmation above `CONFIRM_THRESHOLD_SECONDS` is
`EXIT_DECLINED`'s trigger — a non-interactive run (piped, redirected, or
looped) never blocks on a prompt.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path
from typing import Sequence

from hbuilder.domain.board import BoardError, BoardModel, load_board
from hbuilder.domain.card import Card, Stats
from hbuilder.domain.collection import (
    FULLY_SCORABLE_BLOOM,
    MEMORY_BONUS_MAX_POSTERS,
    CollectionError,
    account_bonuses_from_mapping,
    caps_by_rarity_from_mapping,
    collection_stats,
    derived_member_power_up_permyriad,
    maxed_account_bonuses,
    restate_card_skills,
    skill_ladder_from_mapping,
    stat_basis_from_mapping,
)
from hbuilder.domain.gates import TeamAggregates, aggregate, resolve_team_gates
from hbuilder.domain.marathon import (
    CHANNELS,
    LIVE_CHANNEL,
    DEFAULT_MARATHON_PATH,
    event_bonus_permil,
    load_marathon_event,
    points_scalar,
)
from hbuilder.domain.loader import (
    CardProvenance,
    card_provenance_of,
    completeness_report,
    load_cards,
    load_owned,
    stale_source_note,
)
from hbuilder.domain.schema import GENERATIONS, RUN_STATE_DIMENSIONS, SchemaError
from hbuilder.domain.scoring import score_with_breakdown, deck_screen_unit_score, UNIT_SCORE_K
from hbuilder.domain.board_plan import (
    skill_tree_from_dumps, model_for_character, node_effect_ids_from_master,
    optimize_team_boards, board_points_from_snapshot, power_node_values,
    board_model_from_plan, optimize_and_rerank,
)
from hbuilder.domain.scoring_config import (
    DEFAULT_CONFIG_PATH,
    ScoringConfig,
    load_scoring_config,
)
from hbuilder.domain.song import SongError, load_charts
from hbuilder.domain.song_rank import rank_on_chart
from hbuilder.solver.parallel_songs import rank_across_charts_parallel
from hbuilder.domain.team import MEMBER_COUNT, Team, TeamError, build_team
from hbuilder.output.assumptions import format_assumptions, format_completeness
from hbuilder.output.explain import format_explain
from hbuilder.output.json_output import build_payload, dumps as dumps_payload
from hbuilder.output.song_table import format_song_basis, format_song_rows
from hbuilder.output.table import (
    format_provisional_footer,
    format_team_rows,
    format_unscored_mechanics_footer,
)
from hbuilder.solver.estimate import (
    CONFIRM_THRESHOLD_SECONDS,
    THROUGHPUT_PATH,
    Projection,
    ThroughputSchemaError,
    config_hash,
    load_throughput,
    project_arrangements,
    project_duration,
)
from hbuilder.solver.exact import DEFAULT_TOP_N, solve_exact
from hbuilder.solver.heuristic import (
    HeuristicResult,
    solve_heuristic_labelled,
)
from hbuilder.solver.selectors import (
    CandidatePool,
    SelectorError,
    Selectors,
    constraint_from,
    resolve_selectors,
    set_constraint_from,
)

# The exit-code contract. 2 is deliberately never assigned a second
# meaning here — argparse hardcodes it for its own usage errors, and
# assigning it a second meaning would make that meaning undetectable.
EXIT_OK = 0  # Success, including empty/degenerate-but-valid results.
EXIT_INTERNAL = 1  # Uncaught exception / bug. Never raised deliberately.
# 2 is RESERVED for argparse's own usage errors only (bad flag, missing
# required argument, unregistered subcommand). Never returned explicitly
# from any function in this module.
EXIT_INPUT_DATA = 3  # cards.json / owned.json missing, unreadable, malformed.
EXIT_CONFIG = 4  # config.json fails validate_scoring_config.
EXIT_INVALID_SELECTOR = 5  # Unknown/conflicting selector id, or unowned
# without --include-unowned. Fixable by editing the command line alone —
# nothing on disk is wrong. Also reused for a bad --json output
# path (OSError on write) — the command line named an unwritable
# destination, which is likewise fixable by editing the command line
# alone, not a cards.json/owned.json data problem (EXIT_INPUT_DATA) and
# not an internal bug (EXIT_INTERNAL).
EXIT_INSUFFICIENT_POOL = 6  # Fewer than MEMBER_COUNT candidates remain after
# require/exclude/ownership filtering. Neither success nor failure — the
# inputs were all valid, the search space is just empty.
EXIT_DECLINED = 7  # User declined confirmation at the isatty gate.
# Not a failure — the tool did what was asked.

# Where `tools/recon/proxy/build_board.py` writes by convention. Under
# `dumps/`, which is gitignored: a board is captured ACCOUNT state, so this
# file does not exist in a fresh checkout and the default `solve` then
# scores the board as zero.
DEFAULT_BOARD_PATH = "tools/recon/proxy/dumps/board.json"

# The chart library the per-song modes score against, built by
# `tools/recon/proxy/build_charts.py`. Same contract as the board: absent is a
# first-class state, and the default `solve` never reads it at all.
DEFAULT_CHARTS_PATH = "tools/recon/proxy/dumps/charts.json"

# How many teams the song-agnostic solver hands to the per-song re-ranker.
#
# Measured with `tools/recon/proxy/filter_depth.py --pool 500 --songs 30` on a
# real 91-card pool: the stage-1 rank of the team that actually WINS each chart
# is usually 1-7 — and **61** on one song. So:
#
#   K=25  -> the winner is missed on 1 of 30 songs, and 26 of 150 top-5 slots
#   K=50  -> the winner is missed on 1 of 30 songs, and 3 of 150 top-5 slots
#   K=100 -> nothing missed on this sample; deepest observation is 61
#
# 150 is ~2.5x the deepest observation, and the sample is 30 of ~176 songs, so
# the margin is for the songs not sampled rather than decoration. Cost at 150:
# one chart is ~1.8 s, all ~176 is ~5 min.
#
# Deliberately an ARGUMENT, not a constant: stage 1 is a filter, and the only
# way to know whether it dropped the real winner is to widen this and re-run.
DEFAULT_SONG_POOL = 150

# Where the collection editor writes, and where `--collection` looks by default.
# Nothing in this module writes it; the CLI only ever reads.
DEFAULT_COLLECTION_PATH = "state/collection.json"
# Both gitignored under dumps/: extracted game data.
COLLECTION_CURVE_PATH = "tools/recon/proxy/dumps/card_level_curve.json"
COLLECTION_CAPS_PATH = "tools/recon/proxy/dumps/card_caps.json"
# The per-level skill rows, for the bloom down-level. ⚠ NOT `skill_levels.json`
# -- that path may hold a stale file no current tool generates.
COLLECTION_LADDER_PATH = "tools/recon/proxy/dumps/skill_ladder.json"

# Above this PROJECTED EXACT duration a run switches to the beam
# automatically, and `--exact` overrides it.
#
# Why 300 s, in three parts:
#
# 1. **It must sit above `CONFIRM_THRESHOLD_SECONDS` (30 s), not below.** The
#    confirmation gate is the existing mechanism for "this is going to take a
#    while, are you sure"; a switch threshold under it would make that gate
#    unreachable and silently replace an answer the user could have waited
#    30 s for with an approximate one.
# 2. **The exact search is the reference truth, so it should be preferred
#    whenever it is affordable.** Five minutes is affordable for the answer the
#    whole tool exists to give; at the benchmarked per-set / per-eval costs
#    (`bench/throughput.json`) 300 s is an unconstrained pool in the mid-30s
#    (`HEURISTIC_THRESHOLD_POOL_SIZE`), above the pool sizes the beam has been checked against and
#    comfortably inside the range where the exact solver is routinely run.
# 3. **What it is protecting against is ~29 h, not 6 min.** A real 91-card
#    pool projects at ~100,000 s exact and takes ~11 s on the beam. Anything
#    in the 300 s–100,000 s band is a run nobody completes, so the default
#    there must not be "start it and see".
HEURISTIC_THRESHOLD_SECONDS: float = 300.0

# The fallback when the duration is unprojectable — an absent or rejected
# `bench/throughput.json` (`load_throughput` returns `None` / raises). Without
# it, a rotted side-car file would turn the automatic switch off and hand the
# user a ~30 h run by default, which is the precise outcome the threshold
# exists to prevent.
#
# ⚠ This constant is DERIVED, not chosen by hand.
# `estimate.derive_threshold_pool_size` computes it — the smallest unconstrained
# pool whose projection crosses `HEURISTIC_THRESHOLD_SECONDS` —
# `bench/bench_solver.py` prints what that returns for the rates it has just
# measured, and `test_cli_heuristic.py` asserts this literal equals it. So a
# re-benchmark that moves the boundary fails a test naming the new value
# instead of leaving a stale constant behind.
#
# It stays a literal rather than being computed at import because the pool-size
# rule is exactly the fallback for when `bench/throughput.json` is unreadable:
# a value read from that file would vanish precisely when it is needed.
#
# The argument for deriving it: any change to scoring cost moves the boundary
# (e.g. scoring one leader per OUTFIT class makes per-eval cheaper and per-set
# dearer at once), and a hand-kept literal would silently fire the beam switch
# too early or too late.
HEURISTIC_THRESHOLD_POOL_SIZE: int = 37

# Every code and its one-line meaning, in one place, so an enumerating test
# can pin the whole taxonomy as a contract. 6 and 7 must read as distinctly as their triggers behave — "skip this candidate"
# is not "stop asking me".
EXIT_CODE_MEANINGS: dict[int, str] = {
    EXIT_OK: "Success, including empty/degenerate-but-valid results and --estimate.",
    EXIT_INTERNAL: "Internal error (uncaught exception) — never raised deliberately.",
    2: "Reserved for argparse's own usage errors only.",
    EXIT_INPUT_DATA: "Input data error: cards.json/owned.json missing, unreadable, or malformed.",
    EXIT_CONFIG: "Config error: config.json fails validate_scoring_config.",
    EXIT_INVALID_SELECTOR: (
        "Invalid selector: unknown/conflicting --require/--leader/--exclude "
        "id, or an unowned id without --include-unowned. Also reused for a "
        "bad --json output path (OSError on write) — a command-line "
        "argument problem, not input data or an internal bug."
    ),
    EXIT_INSUFFICIENT_POOL: (
        "Insufficient pool: fewer than 6 candidates remain after selectors "
        "— this candidate is unsolvable, --acquire should skip it and "
        "continue the loop."
    ),
    EXIT_DECLINED: (
        "Declined confirmation: the user said stop — --acquire should "
        "abort the loop entirely, never continue to the next candidate."
    ),
}

# Which load step was in flight when a load-and-guard failure was
# raised, so the shared guard can route to EXIT_INPUT_DATA or EXIT_CONFIG
# without a second load path anywhere in this module.
_STEP_CARDS = "cards"
_STEP_OWNED = "owned"
_STEP_CONFIG = "config"

_TYPE_DISPLAY_ORDER = ("Cute", "Pure", "Happy")


class _LoadFailure(Exception):
    """Internal control-flow exception carrying the exit code
    `load_inputs`'s caller should return. Never escapes this module."""

    def __init__(self, exit_code: int) -> None:
        super().__init__(exit_code)
        self.exit_code = exit_code


def _positive_int(value: str) -> int:
    """`--top`'s `type=` callable: argparse turns a raised
    `ValueError`/`ArgumentTypeError` from a `type=` callable into its own
    usage error (exit 2), so `--top 0` and `--top -1` are rejected the
    same way a non-integer `--top` already would be — no separate
    pre-check needed."""
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError(f"--top must be >= 1, got {parsed}")
    return parsed


def _add_card_source_arguments(subparser: argparse.ArgumentParser) -> None:
    """Register the two mutually exclusive card sources on a subcommand.

    Mode A (`--cards`, default `data/cards.json`) ranks on **maxed** stats —
    the hypothetical "if every card were maxed" answer, and it is the
    default. Mode B (`--real-stats`) ranks on the account's **real,
    measured** stats from a
    document built by `tools/recon/proxy/build_mode_b.py`.

    They are mutually exclusive rather than layered, because a run has
    exactly one stat basis and a command line that names two is a question
    with no answer — argparse rejects it as a usage error (exit 2) instead
    of silently picking one. `--real-stats` additionally *requires* the file
    to declare mode-B provenance (`load_cards_and_provenance`'s
    `require_mode_b`): pointing it at `data/cards.json` fails loudly rather
    than quietly ranking maxed stats under a flag that promised real ones."""
    group = subparser.add_mutually_exclusive_group()
    group.add_argument(
        "--cards",
        default="data/cards.json",
        help="Path to cards.json — MAXED stats (default)",
    )
    group.add_argument(
        "--real-stats",
        default=None,
        metavar="PATH",
        help="Rank on the account's REAL per-card stats from a mode-B "
        "collection file (tools/recon/proxy/build_mode_b.py) instead of the "
        "maxed data/cards.json. The run announces itself as mode B.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hbuilder")
    subparsers = parser.add_subparsers(dest="command", required=True)

    solve_parser = subparsers.add_parser(
        "solve",
        help="Rank every (1 Leader + 5 Members) arrangement of the owned pool",
    )
    _add_card_source_arguments(solve_parser)
    solve_parser.add_argument("--owned", default="data/owned.json", help="Path to owned.json")
    solve_parser.add_argument(
        "--top",
        type=_positive_int,
        default=DEFAULT_TOP_N,
        help=f"Number of ranked teams to print (default {DEFAULT_TOP_N})",
    )
    # Repeatable, never comma-separated — composes with shell loops,
    # no quoting pain, no split-on-comma edge cases.
    solve_parser.add_argument(
        "--require",
        action="append",
        default=[],
        metavar="CARD_ID",
        help="Lock a card into every candidate team as leader or member "
        "(repeatable)",
    )
    solve_parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="CARD_ID",
        help="Bar a card from every candidate team (repeatable)",
    )
    # A separate, stronger flag than --require — locking to the
    # leader slot is not the same constraint as merely requiring the card.
    # Repeatable so that naming two DIFFERENT leaders is reportable as a
    # conflict. Without `append`, argparse keeps only the last
    # occurrence and silently discards the first.
    solve_parser.add_argument(
        "--leader",
        action="append",
        default=[],
        metavar="CARD_ID",
        help="Lock a card to the leader slot specifically (repeatable, but "
        "every occurrence must name the same card)",
    )
    solve_parser.add_argument(
        "--include-unowned",
        action="store_true",
        help="Allow --require/--leader/--exclude to name a card not in "
        "owned.json, widening the candidate pool",
    )
    # --explain selects depth, --json selects format; neither ever
    # restricts the other's availability.
    solve_parser.add_argument(
        "--explain",
        type=int,
        default=None,
        metavar="RANK",
        help="Render full per-skill attribution for the team at this "
        "1-based rank",
    )
    solve_parser.add_argument(
        "--json",
        default=None,
        metavar="PATH",
        help="Write the diffable JSON payload to PATH instead of printing "
        "the human-readable table to stdout",
    )
    # Answers the scale question without enumerating.
    solve_parser.add_argument(
        "--estimate",
        action="store_true",
        help="Print the projected arrangement count and duration, then "
        "exit without enumerating",
    )
    # Bypasses the isatty-gated confirmation prompt explicitly.
    solve_parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt above the duration threshold",
    )
    # Above the threshold the run switches to the beam AUTOMATICALLY, because
    # the alternative default is a ~30 h run started by someone who did not
    # mean to. `--exact` is how you say you did mean to.
    # The Holomem Board is roughly 30-50 % of Overall Power in the game's own
    # `Live/GetDeck` numbers, so a solve without it scores that share as
    # exactly zero. The default path is the
    # conventional output of `tools/recon/proxy/build_board.py`; the file is
    # gitignored account state, so an absent file is the SHIPPED case and stays
    # a first-class, non-error state (`load_board` returns `None`). Which of
    # the two happened is announced on stderr, in the stdout report and in the
    # `--json` payload, because a ranking that silently moved by 30 % between
    # two invocations is the one outcome worse than not scoring the board.
    solve_parser.add_argument(
        "--board",
        default=DEFAULT_BOARD_PATH,
        metavar="PATH",
        help=(
            f"Holomem Board file to score (default {DEFAULT_BOARD_PATH}; build "
            "it with tools/recon/proxy/build_board.py). An absent file is not "
            "an error -- the board then scores as zero and the run says so"
        ),
    )
    solve_parser.add_argument(
        "--no-board",
        action="store_true",
        help="Ignore --board entirely and score the Holomem Board as zero, "
        "which is what every run did before --board existed",
    )
    solve_parser.add_argument(
        "--maxed",
        action="store_true",
        help="Score a fully MAXED account instead of yours: every card owned, "
        "every one at max level and max bloom, Memory Bonus and Member Power-Up "
        "at their ceilings. Answers 'what would this be worth maxed' rather than "
        "'what should I field tonight'. The Holomem Board stays your real one -- "
        "a maxed board is not defined; pass --no-board to drop it. Implies the "
        "whole catalogue as the pool, so --owned is not consulted",
    )
    solve_parser.add_argument(
        "--optimize-board",
        action="store_true",
        help="After ranking, compute the best Holomem Board to DEVELOP for each "
        "top team (which nodes to buy, per character, within --board-budget "
        "points) and re-rank by the board-optimised Unit Score. Prints its own "
        "board-plan table. Needs the skill-tree master data (--skilltree / "
        "--master-nodes, defaults alongside --board)",
    )
    solve_parser.add_argument(
        "--uptime-model",
        choices=("finite", "steady"),
        default=None,
        help="Which ACTIVE-skill uptime to rank by. 'finite' (the config.json "
        "value / in-run truth) counts the finite cooldown window WITH the "
        "start-of-song dead zone. 'steady' uses min(1, duration/interval) with "
        "no dead zone, which is what the game's DECK-SCREEN Unit Score panel "
        "shows: on 71 saved decks it moves the Score-Bonus residual from median "
        "-8.1%% to +2.3%%. Pass 'steady' to rank teams by the panel number you "
        "read in-game (default: whatever config.json's model.active_uptime says)",
    )
    solve_parser.add_argument(
        "--board-budget",
        type=int,
        default=453,
        metavar="POINTS",
        help="Point budget per character for --optimize-board (default 453, the "
        "full tree = 'if every board were maxed'). A lower number answers 'with "
        "this many points, where do they go'. Overridden per character by "
        "--board-points when given",
    )
    solve_parser.add_argument(
        "--board-points",
        default=None,
        metavar="SNAPSHOT",
        help="Account snapshot JSON whose board_points give each character's REAL "
        "owned skill-tree points, used as the per-character --optimize-board "
        "budget instead of the flat --board-budget. Answers 'with the points I "
        "actually have, where do they go'",
    )
    solve_parser.add_argument(
        "--explain-board",
        type=int,
        default=None,
        metavar="RANK",
        help="With --optimize-board, print the per-character, per-node board plan "
        "for the team at this rank (1 = the best): which nodes to unlock, their "
        "cost and the Unit Score each one buys",
    )
    solve_parser.add_argument(
        "--skilltree",
        default="tools/recon/proxy/dumps/skilltree.json",
        help="skilltree.json for --optimize-board (node table + model geometry)",
    )
    solve_parser.add_argument(
        "--master-nodes",
        default="tools/recon/proxy/dumps/master_SkillTreeNode.json",
        help="master SkillTreeNode dump for --optimize-board (node costs + effects)",
    )
    # ---- the per-song modes ------------------------------------------------
    # These do not change how the search runs: the song-agnostic solver still
    # enumerates and still keeps the top `--song-pool`, and those are then
    # RE-RANKED by expected run score on real charts. They print their own
    # table (`output/song_table.py`) rather than `format_team_rows`, because
    # that renderer prints the song-agnostic score and a table sorted by one
    # number while showing another would mislead.
    song_mode = solve_parser.add_mutually_exclusive_group()
    song_mode.add_argument(
        "--song",
        metavar="MUSIC_ID",
        help="Rank by expected run score on ONE chart (e.g. m0001), EXPERT. "
        "Prints its own table, with the slot order that achieved each score",
    )
    song_mode.add_argument(
        "--across-songs",
        action="store_true",
        help="Rank by MEAN expected run score over every EXPERT chart, with a "
        "WINS column counting the charts each team is best on",
    )
    song_mode.add_argument(
        "--songs",
        metavar="IDS",
        help="Rank by MEAN expected run score over JUST these songs, "
        "comma-separated (e.g. m0001,m0303,m0334) — the set you actually play, "
        "rather than one song or all 187. Same WINS column as --across-songs. "
        "A song the chart library cannot serve is named and refused, never "
        "dropped from the mean",
    )
    solve_parser.add_argument(
        "--auto",
        action="store_true",
        help="Rank for an AUTO run instead of manual play (--song/--songs/"
        "--across-songs). Sets BOTH halves of the premise: the AUTO judgement "
        "column AND combo=0, because (seen in a live trace) the combo never "
        "accumulates on AUTO, so a card's 'combo >= N' magnitude override never "
        "fires. Scoring AUTO while still asserting combo would over-value "
        "exactly the cards that carry one",
    )
    solve_parser.add_argument(
        "--difficulty",
        default="EXPERT",
        metavar="NAME",
        help=(
            "Difficulty for --song/--songs/--across-songs (default EXPERT, the "
            "usual ranking mode). Anything else needs the all-difficulty "
            "library: --charts tools/recon/proxy/dumps/charts_all.json, built "
            "by `build_charts.py --all-out`"
        ),
    )
    solve_parser.add_argument(
        "--charts",
        default=DEFAULT_CHARTS_PATH,
        metavar="PATH",
        help=(
            f"Chart library for --song/--across-songs (default "
            f"{DEFAULT_CHARTS_PATH}; build it with "
            f"tools/recon/proxy/build_charts.py)"
        ),
    )
    solve_parser.add_argument(
        "--song-pool",
        type=int,
        default=DEFAULT_SONG_POOL,
        metavar="K",
        help=(
            f"How many teams the song-agnostic search hands to the per-song "
            f"re-ranker (default {DEFAULT_SONG_POOL}). Stage 1 is a FILTER: a "
            f"team outside this top K is never scored on a real chart, so "
            f"widen it and re-run to check whether rank 1 moves"
        ),
    )
    solve_parser.add_argument(
        "--song-workers",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Processes for the --across-songs re-rank (default: every logical "
            "core). The answer is bit-identical at every worker count -- the "
            "workers each score whole charts and the merge is by chart index, "
            "never by completion order. 1 forces the single-process path"
        ),
    )
    # The collection restates each card's stats from the account's real level
    # and bloom. `data/cards.json` holds FULLY-MAXED stats for every card, so
    # without this a run scores a maxed collection. A `card_default: "max"`
    # document reproduces that exactly, so turning this on cannot silently
    # move a maxed answer.
    solve_parser.add_argument(
        "--collection",
        default=DEFAULT_COLLECTION_PATH,
        metavar="PATH",
        help=(
            f"Collection document giving each card's level and bloom (default "
            f"{DEFAULT_COLLECTION_PATH}). An absent file is not an error -- "
            "every card then scores at MAX level and bloom, which is what "
            "data/cards.json holds and what every run did before this existed"
        ),
    )
    solve_parser.add_argument(
        "--no-collection",
        action="store_true",
        help="Ignore --collection entirely and score every card at MAX level "
        "and bloom",
    )
    solve_parser.add_argument(
        "--exact",
        action="store_true",
        help=(
            "Force the exhaustive search regardless of pool size. Without it, "
            f"a run whose exact search projects above "
            f"{HEURISTIC_THRESHOLD_SECONDS:,.0f}s switches to the approximate "
            "beam search and says so"
        ),
    )

    gates_parser = subparsers.add_parser(
        "gates",
        help="Report per-Type/per-Generation counts and which gate "
        "conditions fire for one explicit team",
    )
    _add_card_source_arguments(gates_parser)
    gates_parser.add_argument("--owned", default="data/owned.json", help="Path to owned.json")
    gates_parser.add_argument("--leader", required=True, help="Leader card id")
    gates_parser.add_argument(
        "--members",
        required=True,
        help="Comma-separated list of exactly five member card ids",
    )

    farm_parser = subparsers.add_parser(
        "farm",
        help="Rank the owned collection for MARATHON event-point farming (a "
        "separate objective from the deck-screen Unit Score): the deck whose "
        "cards match the most event bonus rows.",
    )
    _add_card_source_arguments(farm_parser)
    farm_parser.add_argument("--owned", default="data/owned.json", help="Path to owned.json")
    farm_parser.add_argument(
        "--marathon",
        default=str(DEFAULT_MARATHON_PATH),
        help="Event bonus data (data/marathon.json), hand-maintained per event",
    )
    farm_parser.add_argument(
        "--collection",
        default=DEFAULT_COLLECTION_PATH,
        help="Collection file, read for each card's bloom (needed for rarity+bloom "
        "bonus rows). Without it those rows are skipped and reported.",
    )
    farm_parser.add_argument(
        "--no-collection", action="store_true",
        help="Ignore --collection; rarity+bloom bonus rows are then skipped",
    )
    farm_parser.add_argument(
        "--channel", default=LIVE_CHANNEL, choices=CHANNELS,
        help="Play channel whose bonus rows apply (default: live)",
    )
    farm_parser.add_argument(
        "--music", default=None,
        help="Song id in scope, to include its song-gated 'Event Score Up' rows",
    )
    farm_parser.add_argument(
        "--stamina", type=int, default=3,
        help="Stamina spent per play, for the deck-independent points scalar (default 3, the knee)",
    )
    farm_parser.add_argument(
        "--recommended", action="store_true",
        help="Assume the recommended-deck x2.0 in the reported points scalar",
    )
    farm_parser.add_argument(
        "--top", type=int, default=6,
        help="Deck size to fill (default 6 = leader + 5 members)",
    )

    return parser


def _collection_blooms(path: str) -> dict[str, int]:
    """card id -> owned bloom, read straight from the collection file. Best-effort:
    a missing file or a card without a bloom entry simply isn't in the map (its
    rarity+bloom rows are then reported as skipped, never silently zero)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    cards = data.get("cards")
    if not isinstance(cards, dict):
        return {}
    blooms: dict[str, int] = {}
    for card_id, entry in cards.items():
        if isinstance(entry, dict) and isinstance(entry.get("bloom"), int):
            blooms[card_id] = entry["bloom"]
    return blooms


def _card_source(args: argparse.Namespace) -> tuple[Path, bool]:
    """Resolve the one card source a run reads, and whether mode-B
    provenance is mandatory for it. `--cards`/`--real-stats` are mutually
    exclusive at the argparse layer, so this is a choice, never a merge."""
    real_stats = getattr(args, "real_stats", None)
    if real_stats is not None:
        return Path(real_stats), True
    return Path(args.cards), False


def _announce_stats_basis(provenance: CardProvenance | None) -> list[str]:
    """The lines a mode-B run must print so nobody mistakes it for the
    maxed default. Empty in mode A — silence *is* the mode-A signal (the two
    are easy to confuse, so only the non-default one shouts).

    Returned rather than printed so both the stdout report and the stderr
    log render the identical text from one source."""
    if provenance is None:
        return []
    lines = [provenance.banner()]
    note = stale_source_note(provenance, Path.cwd())
    if note is not None:
        lines.append(note)
    return lines


@dataclasses.dataclass(frozen=True, slots=True)
class BoardBasis:
    """Which of the two board states a run is in, and the numbers that prove
    it. Reported rather than inferred: the Holomem Board is roughly 30-50 % of
    Overall Power, so "board loaded" and "board absent" are two different
    answers to the same question and a reader must never have to guess which
    one they are holding."""

    path: str | None
    model: BoardModel | None
    characters: int
    nodes: int

    def lines(self) -> list[str]:
        if self.model is None:
            if self.path is None:
                return [
                    "BOARD: disabled by --no-board -- the Holomem Board scores "
                    "as ZERO (29.4-48.7 % of Overall Power in DECK-SCORE.md)"
                ]
            return [
                f"BOARD: none ({self.path} absent) -- the Holomem Board scores "
                "as ZERO (29.4-48.7 % of Overall Power in DECK-SCORE.md); "
                "build it with tools/recon/proxy/build_board.py"
            ]
        return [
            f"BOARD: loaded {self.path} -- {self.characters} character board(s), "
            f"{self.nodes} unlocked node(s) SCORED"
        ]

    def payload(self) -> dict:
        return {
            "loaded": self.model is not None,
            "path": self.path,
            "characters": self.characters,
            "nodes": self.nodes,
        }


@dataclasses.dataclass(frozen=True, slots=True)
class CollectionBasis:
    """Which of the two collection states a run is in, and what it changed.

    Reported for the same reason `BoardBasis` is: **every card in
    `data/cards.json` carries its FULLY-MAXED stats**, so a ranking without a
    collection assumes a maxed collection. A run scored against the account's
    real levels is a different answer to the same question and must say so."""

    bonuses: object | None
    path: str | None
    applied: int
    unchanged: int
    not_scorable: tuple[str, ...]
    warnings: tuple[str, ...]
    superseded_by_real_stats: bool = False
    # How many cards had a skill slot replaced by the level their bloom bought,
    # and how many wanted one and had no exported row (`not_scorable` above
    # names these cards).
    skills_restated: int = 0
    skills_unavailable: tuple[str, ...] = ()
    # `P` computed from the account's own owned cards, so the hand-typed
    # `power_up_permyriad` can be contradicted instead of trusted.
    derived_power_up: object | None = None
    # Scoring a fully-maxed account instead of the real one — a different
    # question, deliberately available, and never the silent default.
    maxed: bool = False

    @property
    def loaded(self) -> bool:
        return self.applied > 0

    def lines(self) -> list[str]:
        if self.maxed:
            power_up = getattr(self.bonuses, "power_up_permyriad", None)
            return [
                "BASIS: MAXED ACCOUNT -- every card owned, at max level and max "
                "bloom. This is NOT your account; it answers what a team would be "
                "worth maxed, which is the only basis on which an unowned card "
                "can be scored at all",
                "BASIS: max bloom means every skill is at level 2, so no skill is "
                "down-levelled and MODEL.md 4c's open ACTIVE/SPECIAL question "
                "cannot affect this answer either way",
                f"BASIS: Memory Bonus at {MEMORY_BONUS_MAX_POSTERS} posters; Member "
                f"Power-Up at the roster ceiling "
                f"({power_up if power_up is not None else 'unknown -- no card_level_curve.json'})",
                "BASIS: ⚠ the Holomem Board is still YOUR REAL BOARD, not a maxed "
                "one. What a maxed board should buy depends on the song, the cast "
                "and the event, and is not decided -- so it is left real rather "
                "than invented. Pass --no-board to exclude it entirely",
            ]
        if self.superseded_by_real_stats:
            return [
                "COLLECTION: not applied -- --real-stats supersedes it. Those "
                "stats are measured per card; a collection COMPUTES them from "
                "level and bloom, and a measurement outranks a derivation"
            ]
        if self.path is None:
            return [
                "COLLECTION: disabled by --no-collection -- every card scores at "
                "MAX level and bloom, which is what data/cards.json holds"
            ]
        if not self.loaded:
            return [
                f"COLLECTION: none ({self.path} absent or unresolvable) -- every "
                f"card scores at MAX level and bloom, which is what "
                f"data/cards.json holds"
            ]
        lines = [
            f"COLLECTION: loaded {self.path} -- {self.applied} card(s) restated "
            f"from your levels, {self.unchanged} left at data/cards.json's value"
        ]
        if self.skills_restated:
            lines.append(
                f"COLLECTION: {self.skills_restated} card(s) had a skill dropped "
                f"to the level their bloom actually bought -- data/cards.json "
                f"carries the level-2 row for every card"
            )
        if self.skills_unavailable:
            lines.append(
                f"COLLECTION: {len(self.skills_unavailable)} card(s) below their "
                f"bloom step have NO exported level-1 row and stay OVERSTATED -- "
                f"run tools/recon/proxy/import_to_cards.py: "
                f"{', '.join(self.skills_unavailable[:5])}"
                + (" ..." if len(self.skills_unavailable) > 5 else "")
            )
        elif self.not_scorable:
            lines.append(
                f"COLLECTION: {len(self.not_scorable)} card(s) below bloom "
                f"{FULLY_SCORABLE_BLOOM} -- their stats are correct, but "
                f"data/cards.json's level-2 SPECIAL/PASSIVE magnitudes OVERSTATE "
                f"them: {', '.join(self.not_scorable[:5])}"
                + (" ..." if len(self.not_scorable) > 5 else "")
            )
        lines.extend(self._power_up_lines())
        return lines

    def _power_up_lines(self) -> list[str]:
        """What the account's own cards say `power_up_permyriad` should be.

        Reported, never applied. `state/collection.json` is the user's file and
        no automated path here writes it -- so the run states the contradiction
        and the number to type, and scores what the file says."""
        # ASCII only. `main()` reconfigures stdout to UTF-8, but these lines are
        # also read by tools/web and by anything piping stderr, and a report
        # that raises UnicodeEncodeError on a Windows console is a report that
        # does not arrive.
        derived = self.derived_power_up
        if derived is None:
            return []
        typed = getattr(self.bonuses, "power_up_permyriad", None)
        if getattr(self.bonuses, "power_up_permyriad_is_auto", False):
            if derived.exact:
                return [
                    f"COLLECTION: power_up_permyriad = {derived.permyriad}, DERIVED "
                    f"from your {derived.counted} owned cards (player."
                    f"power_up_permyriad is \"auto\" -- it cannot go stale)"
                ]
            return [
                f"COLLECTION: power_up_permyriad is \"auto\" but NOT derivable -- "
                f"{len(derived.skipped)} owned card(s) have no recorded level, so "
                f"the answer is only bounded to {derived.permyriad}..{derived.upper}. "
                f"The term is left unset rather than guessed; refresh with "
                f"tools/recon/proxy/collection_from_snapshot.py"
            ]
        if derived.exact:
            says = f"derive {derived.permyriad}"
            fix = str(derived.permyriad)
        else:
            says = (
                f"derive {derived.permyriad}..{derived.upper} "
                f"({len(derived.skipped)} owned card(s) have no recorded level, "
                f"so this is a bound, not a figure)"
            )
            fix = f"{derived.permyriad} and record the {len(derived.skipped)} missing level(s)"

        if typed is None:
            return [
                f"COLLECTION: your {len(derived.skipped) + derived.counted} owned "
                f"cards {says}; player.power_up_permyriad is unset"
            ]
        if not derived.disagrees_with(typed):
            return []
        return [
            f"COLLECTION: !! player.power_up_permyriad is {typed}, but your "
            f"owned cards {says}"
            + (
                f". The Member Power-Up Bonus is being scored at "
                f"{typed / derived.upper:.0f}x its largest possible size"
                if derived.upper and typed > derived.upper
                else ""
            ),
            f"COLLECTION: state/collection.json is YOURS -- nothing here writes "
            f"it. Set player.power_up_permyriad to {fix}",
        ]

    def payload(self) -> dict:
        return {
            "loaded": self.loaded,
            "superseded_by_real_stats": self.superseded_by_real_stats,
            "path": self.path,
            "cards_restated": self.applied,
            "cards_unchanged": self.unchanged,
            "maxed": self.maxed,
            "not_fully_scorable": list(self.not_scorable),
            "skills_restated": self.skills_restated,
            "skills_unavailable": list(self.skills_unavailable),
        }


def _load_json_if_present(path: Path) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _override_collection_levels(
    document: dict, assume_level: object, assume_bloom: object,
    keep_higher_level: bool = False, keep_higher_bloom: bool = False,
    assume_limit_break: object = None,
) -> dict:
    """`document` with every card entry — and `card_default` — forced to the
    assumed level and/or bloom: the "what if all my cards were at level X / bloom
    Y" filter. Only the assumed field is touched (assume the OTHER stays the
    account's). A level assumption also sets `limit_break` to `"max"`, so a real
    card's low limit break does not silently clamp the hypothetical
    (`resolve_card_state` resolves `"max"` to the rarity's top limit break).

    `keep_higher_level` / `keep_higher_bloom` ("do not delevel / debloom", as in
    the community tool holodori.best): a card already ABOVE a numeric assumption keeps its
    own value (and its own limit break) instead of being lowered to it.

    `assume_limit_break` ("Assumed SP Training", 0..4): every card's
    limit break -- its level CAP by rarity (`card_caps.json`) -- set to that, after
    the level assumption (so "level max" means the cap of that limit break); with
    `keep_higher_level` a card already above it keeps its own."""
    def _higher(own: object, assumed: object) -> bool:
        return (isinstance(own, int) and not isinstance(own, bool)
                and isinstance(assumed, int) and own > assumed)

    def _apply(entry: object) -> dict:
        out = dict(entry) if isinstance(entry, dict) else {}
        if assume_level is not None and not (keep_higher_level
                                             and _higher(out.get("level"), assume_level)):
            out["level"] = assume_level
            out["limit_break"] = "max"
        if assume_bloom is not None and not (keep_higher_bloom
                                             and _higher(out.get("bloom"), assume_bloom)):
            out["bloom"] = assume_bloom
        if assume_limit_break is not None and not (
                keep_higher_level and _higher(entry.get("limit_break") if isinstance(entry, dict)
                                              else None, assume_limit_break)):
            out["limit_break"] = assume_limit_break
        return out

    document = dict(document)
    entries = document.get("cards")
    if isinstance(entries, dict):
        document["cards"] = {cid: _apply(entry) for cid, entry in entries.items()}
    # card_default too, so an owned card with no explicit entry also takes it.
    document["card_default"] = _apply(document.get("card_default"))
    return document


def apply_collection(
    cards: tuple[Card, ...],
    path: str | None,
    *,
    superseded_by_real_stats: bool = False,
    owned_ids: Sequence[str] | None = None,
    maxed: bool = False,
    assume_level: object = None,
    assume_bloom: object = None,
    keep_higher_level: bool = False,
    keep_higher_bloom: bool = False,
    assume_limit_break: object = None,
) -> tuple[tuple[Card, ...], CollectionBasis]:
    """Restate every card's stats from the collection at `path`.

    `path is None` means `--no-collection`. An absent file is NOT an error and
    never changes the exit code — the same first-class-state contract
    `load_board` has. So is an absent `card_level_curve.json`: without the
    per-level multiplier there is nothing to restate, every card keeps
    `data/cards.json`'s value, and the run says so.

    A card the collection cannot restate keeps `data/cards.json`'s stats, which
    are the authoritative ones. Nothing here writes any file."""
    # `superseded_by_real_stats` short-circuits REGARDLESS of `path`. Relying
    # on the caller to also pass `path=None` would make the rule depend on two
    # things being right at every call site instead of one, and the rule --
    # a measurement outranks a derivation -- is not one to leave to a
    # convention.
    # The maxed basis: answer "what would this team be on a maxed account"
    # instead of "what is it on mine". The cards need no work — `data/cards.json`
    # IS the maxed state — so this is the collection deliberately not applied,
    # plus the account-level ceilings. Checked before `superseded_by_real_stats`
    # is irrelevant: `--real-stats` is a MEASUREMENT of the real account, and a
    # maxed run is not about the real account at all, so the two are mutually
    # exclusive and the caller is refused rather than silently given one.
    if maxed:
        if superseded_by_real_stats:
            raise CollectionError(
                "--real-stats measures the real account; a maxed basis describes a "
                "different account. Pick one"
            )
        curve_raw = _load_json_if_present(Path(COLLECTION_CURVE_PATH))
        basis = stat_basis_from_mapping(curve_raw) if curve_raw else None
        return cards, CollectionBasis(
            bonuses=maxed_account_bonuses(basis),
            path=None,
            applied=0,
            unchanged=len(cards),
            not_scorable=(),
            warnings=(),
            maxed=True,
        )

    if superseded_by_real_stats or path is None:
        return cards, CollectionBasis(
            None,
            None if not superseded_by_real_stats else path,
            0,
            len(cards),
            (),
            (),
            superseded_by_real_stats,
        )

    document = _load_json_if_present(Path(path))
    # The level/bloom "what-if" filter: force every card to an assumed level
    # and/or bloom before the stats are restated. Applied only on the collection
    # (account) path — the maxed basis above already IS max, so it never gets here.
    if document is not None and (assume_level is not None or assume_bloom is not None
                                 or assume_limit_break is not None):
        document = _override_collection_levels(document, assume_level, assume_bloom,
                                               keep_higher_level, keep_higher_bloom,
                                               assume_limit_break)
    curve_raw = _load_json_if_present(Path(COLLECTION_CURVE_PATH))
    caps_raw = _load_json_if_present(Path(COLLECTION_CAPS_PATH))
    ladder_raw = _load_json_if_present(Path(COLLECTION_LADDER_PATH))
    basis = stat_basis_from_mapping(curve_raw) if curve_raw else None
    caps = caps_by_rarity_from_mapping(caps_raw) if caps_raw else None
    ladder = skill_ladder_from_mapping(ladder_raw)

    stats, report = collection_stats(
        [card.id for card in cards], document, caps, basis
    )
    # The `player` block rides along on the same document. It reaches
    # `ScoreBreakdown` only -- both terms are ranking-safe to drop, so they are
    # reported and never folded into a score.
    bonuses = account_bonuses_from_mapping(document)

    # Two restatements, and they are independent. STATS come from the level and
    # bloom through the curve; SKILL LEVELS come from the bloom alone, through
    # the exported ladder. A card can get one and not the other -- a card with
    # no curve entry still has a bloom, and a card at full bloom still has
    # stats to restate -- so neither is gated on the other.
    restated: list[Card] = []
    skills_restated = 0
    skills_unavailable: list[str] = []
    for card in cards:
        if card.id in stats:
            card = dataclasses.replace(
                card,
                stats=Stats(
                    performance=stats[card.id][0],
                    technique=stats[card.id][1],
                    sense=stats[card.id][2],
                ),
            )
        state = report.states.get(card.id)
        down_levelled, notes = restate_card_skills(
            card, state.bloom if state else None, ladder.get(card.id, {})
        )
        if down_levelled is not card:
            skills_restated += 1
        if any("no level-" in note for note in notes):
            skills_unavailable.append(card.id)
        restated.append(down_levelled)

    # `power_up_permyriad` is hand-typed and easy to get wrong (e.g. `951` for a
    # true `39`). It is derivable from the account's own cards, so derive it and
    # let the run say when the file disagrees. Nothing here writes
    # `state/collection.json`: the correction is the user's to make.
    derived_power_up = derived_member_power_up_permyriad(
        basis, report.states, owned_ids or ()
    )
    # `"auto"` in the document means "compute it", and this is where it is
    # computed. Only an EXACT derivation may fill it: with an owned card whose
    # level is not recorded the answer is a bound, and scoring the lower end of
    # a bound as though it were the figure is the kind of plausible number this
    # project refuses to invent. A partial derivation leaves the term unset and
    # `lines()` says why.
    if (
        getattr(bonuses, "power_up_permyriad_is_auto", False)
        and derived_power_up is not None
        and derived_power_up.exact
    ):
        bonuses = dataclasses.replace(
            bonuses, power_up_permyriad=derived_power_up.permyriad
        )

    return tuple(restated), CollectionBasis(
        bonuses=bonuses,
        path=path,
        applied=len(report.applied),
        unchanged=len(report.unchanged),
        not_scorable=report.not_scorable,
        warnings=report.warnings,
        skills_restated=skills_restated,
        skills_unavailable=tuple(skills_unavailable),
        derived_power_up=derived_power_up,
    )


def load_board_basis(path: str | None) -> BoardBasis:
    """The board file at `path`, or the explicit "no board" state.

    `path is None` means `--no-board`. An absent file is NOT an error and
    never changes the exit code: `load_board` returns `None`, `cfg.board`
    stays `None`, `scoring._board_bonus` stays the identity, and the run
    scores without a board. A file that
    exists but does not parse IS an error — a board that is silently zero
    looks exactly like a board that is correctly zero, which is the whole
    failure mode `board.BoardError` exists to prevent."""
    if path is None:
        return BoardBasis(path=None, model=None, characters=0, nodes=0)
    model = load_board(path)
    if model is None:
        return BoardBasis(path=path, model=None, characters=0, nodes=0)
    return BoardBasis(
        path=path,
        model=model,
        characters=len(model.characters),
        nodes=sum(len(board.nodes) for board in model.characters.values()),
    )


def load_inputs(
    cards_path: Path, owned_path: Path, require_mode_b: bool = False
) -> tuple[tuple[Card, ...], tuple[Card, ...], ScoringConfig, CardProvenance | None]:
    """The single shared load-and-guard function: every
    `cards.json` / `owned.json` / `config.json` load in this CLI goes
    through here, never through a second, per-command load path. Never
    calls `json.load` itself — every load delegates to `load_cards`,
    `card_provenance_of`, `load_owned` and `load_scoring_config`.

    Returns the loaded card document's `CardProvenance` as a fourth
    element — `None` for the maxed default (mode A), populated for a
    mode-B real-stat document. `require_mode_b=True` (what `--real-stats`
    passes) turns "this file carries no real-stat provenance" into a
    `SchemaError` raised inside the same single guard below, so it exits
    with the same documented input-data code as any other bad card file.

    Raises `_LoadFailure` with the exit code the caller should return:
    `EXIT_INPUT_DATA` for a `load_cards`/`card_provenance_of`/`load_owned` failure,
    `EXIT_CONFIG` for a `load_scoring_config` failure — decided by
    the `current_step` tracking local, which also picks the printed
    message's `current_input`."""
    current_input: str = str(cards_path)
    current_step = _STEP_CARDS
    try:
        cards = load_cards(cards_path)
        # Second read of the same file, on purpose: see `load_cards`'s own
        # docstring — an AST test identifies the shared load path by the
        # name `load_cards`, so the stat basis is a separate call rather than
        # a wider signature. Inside this same guard, so a malformed or
        # non-mode-B provenance block exits as an input-data error like any
        # other bad card file.
        provenance = card_provenance_of(cards_path, require_mode_b=require_mode_b)
        current_input = str(owned_path)
        current_step = _STEP_OWNED
        owned = load_owned(owned_path, cards)
        current_input = str(DEFAULT_CONFIG_PATH)
        current_step = _STEP_CONFIG
        cfg = load_scoring_config()
    except (SchemaError, TeamError) as exc:
        print(str(exc), file=sys.stderr)
        raise _LoadFailure(
            EXIT_CONFIG if current_step == _STEP_CONFIG else EXIT_INPUT_DATA
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        print(f"{current_input}: {exc}", file=sys.stderr)
        raise _LoadFailure(
            EXIT_CONFIG if current_step == _STEP_CONFIG else EXIT_INPUT_DATA
        ) from exc

    return cards, owned, cfg, provenance


def _scoring_config_document(cfg: ScoringConfig) -> dict:
    """Reconstructs the normalized merged config document `config_hash`
    hashes, from an already-parsed `ScoringConfig` — the shape mirrors
    `DEFAULT_CONFIG`'s own top-level keys (minus `schema_version`, which
    `ScoringConfig` does not carry: it names the config *file's* schema,
    not a scoring-model input, so its absence here does not affect
    whether two runs are judged to share the same scoring model). A hash
    computed here is directly comparable to one computed from the same
    reconstruction against a `bench/throughput.json` measurement —
    this function is the one place that reconstruction happens, so the
    live run and a benchmark script cannot independently drift."""
    return {
        "probability_tiers": cfg.probability_tiers,
        "family_weights": cfg.family_weights,
        "special": {"expected_fires_per_run": cfg.expected_fires_per_run},
        "model": {
            "stacking": cfg.stacking_model.name,
            "activation_rate": cfg.activation_rate_model.name,
            # The ACTIVE-uptime reference (finite in-run window vs steady-state
            # deck-panel). It changes the reported Unit Score, so it belongs in
            # the hash `config_hash` compares — otherwise a finite run and a
            # steady run of the same pool share one benchmark/throughput entry.
            "active_uptime": cfg.active_uptime_model,
        },
        # Iterates RUN_STATE_DIMENSIONS rather than naming the dimensions
        # inline, as `output/json_output.py` and `output/assumptions.py` do:
        # hardcoding them here would let a change to the dimensions adapt
        # there and silently keep emitting the old shape here. Pinned by an
        # AST test in tests/test_run_state_thresholds.py.
        "assumed_run_state": {
            dim: getattr(cfg.run_state, dim) for dim in RUN_STATE_DIMENSIONS
        },
        # The board is NOT a `config.json` key, and it is included anyway,
        # because `config_hash` answers "did the scoring model that produced
        # this run change since the benchmark was measured?" — and the board is
        # roughly 30-50 % of Overall Power. Without it a `bench/throughput.json`
        # measured with no board and one measured with a real board would
        # carry the SAME hash, so the staleness warning `project_duration`
        # exists to print could not fire between them.
        #
        # `None` for no board rather than omitting the key: an absent board is
        # a real, distinct scoring model, not an unknown one.
        "board": None if cfg.board is None else cfg.board.fingerprint(),
    }


def should_use_heuristic(projection: Projection, force_exact: bool) -> bool:
    """Switch to the beam above the benchmarked threshold, and let `--exact`
    override it unconditionally.

    Two rules, in this order:

    1. `--exact` wins outright. It is the user saying the exhaustive answer is
       what they came for; the tool must never quietly hand back an approximate
       one instead.
    2. Otherwise the PROJECTED EXACT duration decides, using the benchmarked
       cost model exactly as the confirmation gate does. When that projection
       is unavailable — an absent or rejected `bench/throughput.json` — the
       pool size decides instead. That fallback is the load-bearing half:
       without it a rotted side-car file turns the switch OFF and restores a
       ~30 h default, so the failure
       mode of the benchmark would be the one outcome the threshold exists to
       prevent.
    """
    if force_exact:
        return False
    if projection.seconds_or_none is None:
        return projection.pool_size >= HEURISTIC_THRESHOLD_POOL_SIZE
    return projection.seconds_or_none > HEURISTIC_THRESHOLD_SECONDS


def format_approximation(
    result: HeuristicResult, projection: Projection
) -> list[str]:
    """The approximate-result label: the SPECIFIC
    failure modes this run observed, never a generic "approximate" caveat.

    Every line below names something the search actually did — a partial it
    cut, a card it never reached, a gate it dropped, a swap that improved on
    its own answer. When the run observed nothing, this says so, and that is a
    real (if weak) statement no constant caveat could make.
    """
    lines = [
        "",
        "APPROXIMATE RESULT — this ranking came from a beam search, not an "
        "exhaustive one.",
        f"  searched: {result.arrangements_scored:,} arrangements "
        f"({result.partials_scored:,} partial evaluations), "
        f"beam_width={result.beam_width} branch={result.branch} "
        f"seeds={result.seeds}",
        f"  the exhaustive search over the same pool is "
        + (
            f"{projection.arrangement_count:,} arrangements"
            if projection.seconds_or_none is None
            else f"{projection.arrangement_count:,} arrangements, projected "
            f"{projection.seconds_or_none:,.0f}s"
        ),
    ]
    if not result.observations:
        lines.append(
            "  the search observed none of its known failure modes on this run: "
            "it never cut close, never rescued a partial, never dropped one "
            "carrying a reachable gate, never improved on itself by swapping, "
            "reached every card in the pool, and every seed agreed."
        )
        return lines

    lines.append("  observed failure modes:")
    by_mode: dict[str, list] = {}
    for observation in result.observations:
        by_mode.setdefault(observation.mode.value, []).append(observation)
    for mode in sorted(by_mode):
        found = by_mode[mode]
        lines.append(f"    [{mode}] x{len(found)}")
        # One concrete example per mode, not all of them: a wall of near-
        # identical level-3 truncations buries the one observation that names a
        # gate. The full list rides in the --json payload.
        lines.append(f"      e.g. {found[0].detail}")
    return lines


def emit_progress(evaluated: int) -> None:
    """The `progress` callback `solve_exact` invokes every
    `PROGRESS_INTERVAL_TEAMS` arrangements. Writes to stderr only,
    never stdout, so a `--json` redirect and a piped table both stay
    machine-clean."""
    print(f"progress: {evaluated:,} arrangements evaluated", file=sys.stderr)


def confirm_or_decline(projected_seconds: float) -> bool:
    """The interactivity-gated confirmation prompt. Returns `True` to
    proceed, `False` if the user declined. The prompt text is written to
    stderr (never stdout) via `input()`'s bare form plus an explicit
    stderr `print`, rather than `input()`'s own prompt argument (which
    writes to stdout) — so stdout stays exactly the report, even in a
    real terminal."""
    print(
        f"This run is projected to take approximately "
        f"{projected_seconds:,.1f}s (confirmation threshold: "
        f"{CONFIRM_THRESHOLD_SECONDS:,.0f}s). Proceed? [y/N]: ",
        end="",
        file=sys.stderr,
    )
    answer = input()
    return answer.strip().lower() in ("y", "yes")


def _render_song_ranking(
    args: argparse.Namespace,
    ranked: Sequence[RankedTeam],
    cfg: ScoringConfig,
) -> int:
    """`--song` / `--across-songs`: re-rank the search's top K by expected run
    score on real charts, and print the per-song table.

    Its OWN output path, and that is the point. `format_team_rows` prints the
    song-agnostic `breakdown.total_score`; a table sorted by expected run score
    while showing that number would be wrong in the quietest possible way. The
    two tables also want different columns — this one carries the slot order,
    which the other cannot.

    `--json` and `--explain` are refused rather than half-answered: both are
    shaped around `ScoreBreakdown`, which describes the song-agnostic score, and
    emitting it beside a per-song ranking would invite exactly the confusion the
    separate renderer exists to prevent."""
    for flag, value in (("--json", args.json), ("--explain", args.explain)):
        if value is not None:
            print(
                f"{flag} is not available with --song/--across-songs: it reports "
                f"the song-agnostic ScoreBreakdown, which is not the number this "
                f"ranking sorts by",
                file=sys.stderr,
            )
            return EXIT_INVALID_SELECTOR

    library = load_charts(args.charts)
    if library is None:
        print(
            f"--charts: {args.charts} is not on disk. Build it with "
            f"tools/recon/proxy/build_charts.py",
            file=sys.stderr,
        )
        return EXIT_INPUT_DATA

    teams = [candidate.team for candidate in ranked]

    # A shortlist may span several leaders: `score_song_detailed`'s deck power
    # includes the leader's stat outfit as one of the per-leader
    # `stat_entries`, as `score_card_set` builds them, so leaders do not tie.
    # Pinned by `test_score_song.py::test_the_per_song_leader_ORDER_agrees_
    # with_the_ranking_scorer`.

    difficulty = args.difficulty.upper()
    if getattr(args, "auto", False):
        # BOTH halves, together, or the number describes no real run. See
        # `ScoringConfig.song_auto` for the measurement behind the combo half.
        cfg = dataclasses.replace(
            cfg, song_auto=True, run_state=dataclasses.replace(cfg.run_state, combo=0)
        )
        print(
            "AUTO: scoring the AUTO judgement column with combo=0 — a "
            "'combo >= N' magnitude override never fires on AUTO (seen in a live "
            "trace), so cards carrying one score at their "
            "base magnitude here",
            file=sys.stderr,
        )
    if args.song:
        # One song is `select` of a one-element set: the same resolution, the
        # same refusal message, so `--song` and `--songs` cannot disagree about
        # what a name means or about which difficulties the library holds.
        try:
            chart = library.select((args.song,), difficulty)[0]
        except SongError as exc:
            print(f"--song: {exc}", file=sys.stderr)
            return EXIT_INVALID_SELECTOR
        ranking = rank_on_chart(teams, chart, cfg, filtered_from=len(teams))
    else:
        if args.song_workers is not None and args.song_workers < 1:
            print(
                f"--song-workers: must be >= 1, got {args.song_workers}",
                file=sys.stderr,
            )
            return EXIT_INVALID_SELECTOR
        wanted = (
            [s.strip() for s in args.songs.split(",") if s.strip()]
            if args.songs else None
        )
        try:
            charts = library.select(wanted, difficulty)
        except SongError as exc:
            print(f"--songs: {exc}", file=sys.stderr)
            return EXIT_INVALID_SELECTOR
        if not charts:
            print(
                f"--difficulty {difficulty}: {args.charts} holds no chart at "
                f"that difficulty. Build every difficulty with "
                f"`tools/recon/proxy/build_charts.py --all-out "
                f"tools/recon/proxy/dumps/charts_all.json` and pass --charts.",
                file=sys.stderr,
            )
            return EXIT_INVALID_SELECTOR
        print(
            f"progress: scoring {len(teams)} teams on {len(charts)} {difficulty} "
            # 0.0037 s/eval, measured over ~100,000 evaluations on a desktop
            # PC; re-measure it if the scorer's cost changes.
            f"charts (~{len(teams) * len(charts) * 0.0037:.0f}s single-process)",
            file=sys.stderr,
        )
        ranking = rank_across_charts_parallel(
            teams,
            charts,
            cfg,
            filtered_from=len(teams),
            workers=args.song_workers,
            # The fallback changes the wall time, never the answer -- but a run
            # that silently took 13x longer than promised is a diagnostic
            # withheld, so it is named on stderr.
            on_fallback=lambda message: print(
                f"progress: {message}", file=sys.stderr
            ),
        )

    for line in format_song_basis(ranking):
        print(line, file=sys.stderr)
    for line in format_song_rows(ranking, top=args.top):
        print(line)
    return EXIT_OK


def _render_board_optimized(
    args: argparse.Namespace, ranked: Sequence, cfg: ScoringConfig
) -> int:
    """`--optimize-board`: for each team the search found, choose the best board
    to DEVELOP per character within `--board-budget` points, and re-rank by the
    board-optimised Unit Score. Its own output path — the board-less/current
    ranking is what `format_team_rows` prints; this answers 'and where do the
    board points go'. The search already supplies the top teams, so the
    expensive per-character optimisation only ever
    runs on them, never on the whole pool."""
    try:
        skilltree = json.load(open(args.skilltree, encoding="utf-8"))
        master = json.load(open(args.master_nodes, encoding="utf-8"))
    except OSError as exc:
        print(f"--optimize-board: {exc}", file=sys.stderr)
        return EXIT_INPUT_DATA

    effects = node_effect_ids_from_master(master)
    cfg_off = dataclasses.replace(cfg, board=None)
    tree_cache: dict[str, object] = {}

    # Real per-character budget from the account's owned skill-tree points, or
    # the flat --board-budget when no snapshot is given.
    real_points: dict[str, int] = {}
    if args.board_points is not None:
        try:
            real_points = board_points_from_snapshot(json.load(open(args.board_points, encoding="utf-8")))
        except OSError as exc:
            print(f"--board-points: {exc}", file=sys.stderr)
            return EXIT_INPUT_DATA

    def budget_for(card: Card) -> int:
        # With --board-points, a character absent from the list owns ZERO points
        # (nothing to spend); only without it does the flat budget apply. Falling
        # back to the flat 453 there would hand an un-invested holomem a full
        # board and win the leader slot on points it does not have.
        chr_id = "chr-" + card.id.split("-")[1]
        if real_points:
            return real_points.get(chr_id, 0)
        return args.board_budget

    def tree_for(card: Card):
        model = model_for_character(skilltree, "chr-" + card.id.split("-")[1])
        if model is None:
            return None
        if model not in tree_cache:
            tree_cache[model] = skill_tree_from_dumps(skilltree, master, model)
        return tree_cache[model]

    # The bounded joint pass — board-optimise every candidate and re-rank by the
    # board-optimised Unit Score — lives in `board_plan.optimize_and_rerank` so the
    # web serves the same re-rank rather than a second copy. `rows` keeps its
    # `(opt_us, base_us, plan, team)` shape for the printing/explain code below.
    # SINGER CONTEXT: `sings` is left at its default False. This
    # ranking's objective is the song-agnostic deck-screen Unit Score — there is
    # no chart in scope here, so no leader has a cast to be credited on and the
    # song-agnostic plan (conditional reds and solo/group yellows valued 0) is
    # the correct one. The song-aware plans are the web's song mode and the
    # per-song probes, which pass `sings` explicitly.
    optimised = optimize_and_rerank(
        [candidate.team for candidate in ranked], cfg,
        tree_for=tree_for, budget_for=budget_for,
        node_effect_ids=effects, skilltree=skilltree,
    )
    rows = [(bt.optimised_us, bt.board_off_us, bt.plan, bt.team) for bt in optimised]

    budget_desc = (
        "each character's real owned points" if real_points
        else f"{args.board_budget} points each"
    )
    print(
        f"BOARD-OPTIMISED ranking -- best board to develop per character, "
        f"{budget_desc}. board-off Unit Score -> optimised."
    )
    for rank, (opt, base, plan, team) in enumerate(rows[: args.top], 1):
        members = ",".join(m.member for m in team.members)
        pct = 100.0 * (opt - base) / base if base else 0.0
        points = sum(p.spent for p in plan.per_character.values())
        print(
            f"{rank:>3} {opt:>13,.0f}  (board-off {base:>12,.0f}, +{pct:4.1f}%, "
            f"{points} pts)  {team.leader.member} | {members}"
        )

    if args.explain_board is not None and 1 <= args.explain_board <= len(rows):
        _opt, _base, plan, team = rows[args.explain_board - 1]
        bd = score_with_breakdown(team, cfg_off)
        ds = deck_screen_unit_score(bd)
        bare_pool = bd.active_pool / (1.0 + bd.combined_support_rate)
        print(
            f"\nBOARD PLAN for #{args.explain_board} -- which nodes to unlock, per "
            f"character (node  +Unit Score  cost  effect):"
        )
        seen: set[str] = set()
        for card, is_leader in [(team.leader, True), *[(m, False) for m in team.members]]:
            if card.id in seen:
                continue
            seen.add(card.id)
            cplan = plan.per_character.get(card.id)
            tree = tree_for(card)
            if cplan is None or tree is None or not (cplan.selected - {"S-001"}):
                continue
            values = power_node_values(
                effects, is_leader=is_leader, own_card=card, member_cards=team.members,
                bonus_permil=ds.score_bonus_permil, overall_power=ds.overall_power,
                k_constant=UNIT_SCORE_K, active_pool_rate=bare_pool,
                skilltree=skilltree,
                model_id=model_for_character(skilltree, "chr-" + card.id.split("-")[1]) or "",
                own_chr="chr-" + card.id.split("-")[1],
            )
            role = "leader" if is_leader else "member"
            print(f"  {card.member} ({role}): {cplan.spent} pts -> +{cplan.value:,.0f} Unit Score")
            bought = sorted(
                ((values.get(n, 0.0), n) for n in cplan.selected if n != "S-001"),
                reverse=True,
            )
            for value, node in bought:
                eff = effects.get(node, ["?"])[0]
                family = eff.split("-")[1] if eff.count("-") >= 1 else eff
                tag = "" if value > 0 else "  (connector)"
                print(f"      {node:<7} +{value:>8,.0f}  {tree.nodes[node].cost}pt  {family}{tag}")
    return EXIT_OK


def run_solve(args: argparse.Namespace) -> int:
    """The `solve` subcommand: load, resolve selectors, project, confirm,
    search, render.

    `--require`/`--leader`/`--exclude` are resolved against the
    full `cards` set and the `owned` subset via `resolve_selectors`,
    never a second independent selector-resolution path. A `SelectorError`
    maps to `EXIT_INVALID_SELECTOR`; a resolved pool smaller than
    `MEMBER_COUNT` is reported as `EXIT_INSUFFICIENT_POOL` before any
    enumeration begins. When `--include-unowned` actually widened the
    pool, the run is labelled on stderr naming the admitted ids.

    The constrained-pool projection is printed
    to stderr before every search; `--estimate` returns here without
    enumerating; the confirmation gate fires only when the projected
    duration exceeds `CONFIRM_THRESHOLD_SECONDS` *and* `sys.stdout.isatty()`
    *and* `--yes` was not passed — a non-interactive run never blocks.

    `--explain` (depth) and `--json` (format) never constrain each other.
    The already-selected Top-N is re-scored with `score_with_breakdown` (the
    solver's own hot loop only ever computes the bare `float`
    `RankedTeam.score`), and the boundary footer is printed after the table so a tainted team's `>=` marker is
    never presented without the boundary statement that explains it."""
    cards_path, require_mode_b = _card_source(args)
    try:
        cards, owned, cfg, provenance = load_inputs(
            cards_path, Path(args.owned), require_mode_b
        )
    except _LoadFailure as failure:
        return failure.exit_code

    # Before anything else this run prints, and on stderr as well as in the
    # report below, so a mode-B run is labelled even when stdout is piped
    # into a file or discarded.
    basis_lines = _announce_stats_basis(provenance)
    for line in basis_lines:
        print(line, file=sys.stderr)

    # The board is loaded here rather than inside `load_inputs` because it is
    # not one of the three authoritative inputs that function guards: it is
    # gitignored account state named by a command-line flag, so a bad path is
    # a command-line problem (EXIT_INVALID_SELECTOR), not a cards.json one.
    # --optimize-board COMPUTES the board, so the current one must not be the
    # base: ranking teams by the account's real board biases the search toward
    # characters whose board is already developed (a leader with a strong red
    # branch), which is exactly the team the optimiser is trying to earn from
    # scratch. So it implies board-off for the search and the baseline; the
    # per-character optimisation supplies the board.
    board_off = args.no_board or getattr(args, "optimize_board", False)
    try:
        board_basis = load_board_basis(None if board_off else args.board)
    except (BoardError, OSError) as exc:
        print(f"--board: {exc}", file=sys.stderr)
        return EXIT_INVALID_SELECTOR
    board_lines = board_basis.lines()
    for line in board_lines:
        print(line, file=sys.stderr)
    # `ScoringConfig` is frozen: replace, never mutate, so the config the
    # solver and `score_with_breakdown` see is one object and cannot drift.
    if board_basis.model is not None:
        cfg = dataclasses.replace(cfg, board=board_basis.model)

    # Restated BEFORE the pool is built, so every downstream consumer -- the
    # solver, the projection, the JSON payload -- sees one set of stats. Doing
    # it later would give two definitions of what a card is worth.
    # ⚠ NEVER over a mode-B run. `--real-stats` carries the account's REAL
    # measured per-card stats; the collection COMPUTES them from level and
    # bloom. Both answer the same question and measured beats computed, so
    # applying the collection on top of `--real-stats` would silently
    # overwrite a measurement with a derivation (`test_mode_b.py` checks the
    # two runs differ).
    maxed = getattr(args, "maxed", False)
    try:
        cards, collection_basis = apply_collection(
            cards,
            None if args.no_collection else args.collection,
            superseded_by_real_stats=getattr(args, "real_stats", None) is not None,
            owned_ids=[card.id for card in owned],
            maxed=maxed,
        )
    except CollectionError as exc:
        print(f"--maxed: {exc}", file=sys.stderr)
        return EXIT_INVALID_SELECTOR
    if collection_basis.bonuses is not None:
        cfg = dataclasses.replace(cfg, account=collection_basis.bonuses)
    # `--uptime-model` overrides config.json's model.active_uptime for this run.
    # None means "no flag given, keep the config value". Restated here, before
    # the search, so the solver's hot loop and the Top-N re-score agree.
    if getattr(args, "uptime_model", None) is not None:
        cfg = dataclasses.replace(cfg, active_uptime_model=args.uptime_model)
        print(
            f"Ranking by the {args.uptime_model} ACTIVE uptime "
            f"({'deck-screen panel' if args.uptime_model == 'steady' else 'in-run'}).",
            file=sys.stderr,
        )
    collection_lines = collection_basis.lines()
    for line in collection_lines:
        print(line, file=sys.stderr)
    for warning in collection_basis.warnings[:10]:
        print(f"COLLECTION: {warning}", file=sys.stderr)

    selectors = Selectors(
        require=tuple(args.require),
        leader=tuple(args.leader),
        exclude=tuple(args.exclude),
        include_unowned=args.include_unowned,
    )
    try:
        # A maxed account owns everything by definition, so the ownership set IS
        # the catalogue. Expressed by widening `owned` rather than by setting
        # `include_unowned`: that flag admits only the unowned cards a selector
        # NAMES (`solver/selectors.py` step 6), which would leave a maxed run
        # searching only the owned cards — the basis silently not applied to the
        # one thing it most obviously changes.
        pool: CandidatePool = resolve_selectors(
            cards, cards if maxed else owned, selectors
        )
    except SelectorError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_INVALID_SELECTOR

    if len(pool.cards) < MEMBER_COUNT:
        print(
            f"insufficient pool: {len(pool.cards)} candidate(s) remain, "
            f"{MEMBER_COUNT} required",
            file=sys.stderr,
        )
        return EXIT_INSUFFICIENT_POOL

    if pool.included_unowned_ids:
        print(
            "widened by --include-unowned: "
            + ", ".join(pool.included_unowned_ids),
            file=sys.stderr,
        )

    # Project from the constrained pool, never the raw one.
    pool_size = len(pool.cards)
    locked_count = len(pool.locked_ids)
    leader_fixed = pool.leader_id is not None
    set_count, arrangement_count = project_arrangements(
        pool_size, locked_count, leader_fixed
    )
    # `bench/throughput.json` is an OPTIMISATION input, never data authority:
    # everything it can affect is the advisory duration line and whether the
    # confirmation gate fires. `load_throughput` raises by design so a
    # rotted benchmark output cannot silently project from a schema it no
    # longer satisfies — but raising all the way out of `main` turns a stale
    # side-car file into a crash of the whole solve. Degrade to the same
    # "unknown duration" state an absent file already produces, and say on
    # stderr which file was ignored and why.
    try:
        throughput = load_throughput()
    except (ThroughputSchemaError, json.JSONDecodeError, OSError) as exc:
        throughput = None
        print(
            f"WARNING: ignoring {THROUGHPUT_PATH}: {exc}",
            file=sys.stderr,
        )
        print(
            f"WARNING: re-run bench/bench_solver.py to restore duration "
            f"projection; the search itself is unaffected",
            file=sys.stderr,
        )
    current_hash = config_hash(_scoring_config_document(cfg))
    seconds_or_none, duration_note = project_duration(
        arrangement_count, throughput, current_hash, set_count=set_count
    )
    projection = Projection(
        pool_size=pool_size,
        locked_count=locked_count,
        leader_fixed=leader_fixed,
        set_count=set_count,
        arrangement_count=arrangement_count,
        seconds_or_none=seconds_or_none,
        throughput_warning=duration_note,
    )

    # The projection is printed before enumerating, every run,
    # entirely on stderr.
    print(
        f"PROJECTION: pool={projection.pool_size} "
        f"locked={projection.locked_count} "
        f"leader_fixed={projection.leader_fixed} "
        f"sets={projection.set_count:,} "
        f"arrangements={projection.arrangement_count:,}",
        file=sys.stderr,
    )
    if projection.seconds_or_none is None:
        print(
            f"PROJECTED DURATION: unknown ({projection.throughput_warning})",
            file=sys.stderr,
        )
    else:
        print(
            f"PROJECTED DURATION: {projection.seconds_or_none:,.2f}s",
            file=sys.stderr,
        )
        if projection.throughput_warning is not None:
            print(f"WARNING: {projection.throughput_warning}", file=sys.stderr)

    use_heuristic = should_use_heuristic(projection, args.exact)

    if args.estimate:
        # --estimate answered the question asked. Success, no search.
        # It still says WHICH search it would have run, because the projection
        # printed above is the exact cost and above the threshold that is not
        # the cost the run would actually pay.
        print(
            f"SOLVER: {'heuristic (beam search, approximate)' if use_heuristic else 'exact (exhaustive)'}",
            file=sys.stderr,
        )
        return EXIT_OK

    print(
        f"SOLVER: {'heuristic (beam search, approximate)' if use_heuristic else 'exact (exhaustive)'}",
        file=sys.stderr,
    )

    # Gate confirmation on interactivity, not on the threshold
    # alone. An unmeasurable projection (seconds_or_none is None) is not
    # evidence of a long run, so it never prompts either.
    #
    # The heuristic path never prompts: `projection.seconds_or_none` is the
    # cost of the search that is NOT about to run, so prompting with it would
    # ask the user to approve a duration nothing is going to take. Above the
    # threshold the switch is the answer to "this would take too long", not a
    # question about it.
    needs_confirmation = (
        not use_heuristic
        and projection.seconds_or_none is not None
        and projection.seconds_or_none > CONFIRM_THRESHOLD_SECONDS
        and sys.stdout.isatty()
        and not args.yes
    )
    if needs_confirmation:
        if not confirm_or_decline(projection.seconds_or_none):
            print("declined confirmation; aborting before enumeration", file=sys.stderr)
            return EXIT_DECLINED

    # A per-song mode RE-RANKS what the search found, so the search has to hand
    # it more than the display count -- otherwise stage 2 would only ever
    # reorder the same `--top` rows and could never promote a team into them.
    song_mode = bool(
        getattr(args, "song", None)
        or getattr(args, "across_songs", False)
        or getattr(args, "songs", None)
    )
    search_top = max(args.top, args.song_pool) if song_mode else args.top
    # --optimize-board RE-RANKS the found teams by their board-optimised score, so
    # like a per-song mode it needs more than the display count -- otherwise a
    # team the board would promote is never in the set to promote.
    if getattr(args, "optimize_board", False):
        search_top = max(search_top, 50)

    heuristic_result: HeuristicResult | None = None
    if use_heuristic:
        heuristic_result = solve_heuristic_labelled(
            pool.cards,
            cfg,
            top_n=search_top,
            progress=emit_progress,
            constraint=constraint_from(pool),
            set_constraint=set_constraint_from(pool),
            locked_ids=pool.locked_ids,
        )
        ranked = heuristic_result.teams
        # The count the beam ACTUALLY scored, not `projection.arrangement_count`
        # — that is the exact search's number, and reporting it here would
        # claim the beam had evaluated 4.2 billion arrangements.
        print(
            f"progress: {heuristic_result.arrangements_scored:,} arrangements "
            f"scored, {heuristic_result.partials_scored:,} partial evaluations "
            f"(complete)",
            file=sys.stderr,
        )
        for line in format_approximation(heuristic_result, projection):
            print(line, file=sys.stderr)
    else:
        ranked = solve_exact(
            pool.cards,
            cfg,
            top_n=search_top,
            progress=emit_progress,
            constraint=constraint_from(pool),
            # The set-level half, without which a locked run costs exactly what
            # an unconstrained one does and the PROJECTION line above is a
            # promise the solver never keeps.
            set_constraint=set_constraint_from(pool),
            # Lets the enumeration BUILD only the sets that can qualify instead
            # of walking every C(N,5) to reject them. set_constraint stays as
            # the correctness net; this is the one that makes a locked run
            # cheap.
            locked_ids=pool.locked_ids,
            # `locked_ids` cannot say WHICH lock a family-B set may defer to the
            # outfit slot; only the pin can. Without this the enumeration builds
            # all k deferrals and `constraint` rejects k-1 of them one set at a
            # time (on a typical mode-B pool, ~220,000 sets and ~0.3 s of
            # ~24 s). `None` when no --leader was given.
            leader_id=pool.leader_id,
        )
        # A short run never hits iter_candidates' own periodic cadence — this
        # final line is what keeps progress visible for it too.
        print(
            f"progress: {projection.arrangement_count:,}/"
            f"{projection.arrangement_count:,} arrangements evaluated (complete)",
            file=sys.stderr,
        )

    if song_mode:
        return _render_song_ranking(args, ranked, cfg)

    if getattr(args, "optimize_board", False):
        return _render_board_optimized(args, ranked, cfg)

    if args.explain is not None and not (1 <= args.explain <= len(ranked)):
        print(
            f"--explain: rank must be between 1 and {len(ranked)}, got "
            f"{args.explain}",
            file=sys.stderr,
        )
        return EXIT_INVALID_SELECTOR

    breakdowns = tuple(score_with_breakdown(candidate.team, cfg) for candidate in ranked)

    if args.json is not None:
        selectors_payload = {
            "require": list(selectors.require),
            # The RESOLVED leader, not the raw flag occurrences: `--leader` is
            # repeatable but collapses to at most one id, and the payload's
            # contract is `str | None`. Serialising `selectors.leader` here
            # would silently change the value type to a list.
            "leader": pool.leader_id,
            "exclude": list(selectors.exclude),
            "include_unowned": selectors.include_unowned,
        }
        payload = build_payload(
            ranked,
            breakdowns,
            cfg,
            pool.cards,
            selectors_payload,
            explain_rank=args.explain,
            # The counts the solver actually worked with, not a recomputation:
            # `projection.arrangement_count` is the same value printed in the
            # stderr PROJECTION line above, so the two can never disagree.
            owned_count=len(owned),
            arrangement_count=projection.arrangement_count,
        )
        # The stat basis rides along in the payload itself, added here rather
        # than inside build_payload so a mode-A payload stays byte-identical
        # across runs (payloads must stay diffable) while a mode-B payload can
        # never be mistaken for one: the key exists only when the run was
        # mode B, and it names the source and the timestamp.
        # The approximate-result label, machine-readable half. Added here
        # rather than inside `build_payload` for exactly the reason
        # `stats_basis` is: an EXACT run's payload stays byte-identical (the
        # diffability contract), while an approximate one can
        # never be mistaken for one — the key exists only when the beam ran,
        # and it carries every observation, not the one example the table
        # shows.
        if heuristic_result is not None:
            payload["search"] = {
                "mode": "heuristic",
                "approximate": True,
                "beam_width": heuristic_result.beam_width,
                "branch": heuristic_result.branch,
                "seeds": heuristic_result.seeds,
                "arrangements_scored": heuristic_result.arrangements_scored,
                "partials_scored": heuristic_result.partials_scored,
                "exhaustive_arrangement_count": projection.arrangement_count,
                "exhaustive_projected_seconds": projection.seconds_or_none,
                "failure_modes": sorted(
                    mode.value for mode in heuristic_result.modes()
                ),
                "observations": [
                    {
                        "mode": observation.mode.value,
                        "detail": observation.detail,
                        "level": observation.level,
                        "margin": observation.margin,
                        "seed": observation.seed,
                    }
                    for observation in heuristic_result.observations
                ],
                # A tool that recommends a team without its slot order hands
                # the player the median case. `None` means
                # no count-limited effect binds and every order scores alike.
                "member_orders": {
                    ",".join(member_ids): (
                        list(order) if order is not None else None
                    )
                    for member_ids, order in heuristic_result.member_orders.items()
                },
            }
        # ALWAYS present, unlike `search`/`stats_basis` above, and deliberately
        # so: those two are absent-means-default, but "the board scored as
        # zero" and "there is no board key" are not the same statement, and a 30 % swing in the ranking
        # must never be a difference a reader has to infer from a missing key.
        payload["board"] = board_basis.payload()
        payload["collection"] = collection_basis.payload()
        if provenance is not None:
            payload["stats_basis"] = {
                "mode": provenance.mode,
                "basis": provenance.stats_basis,
                "generated_at": provenance.generated_at,
                "stats_source": provenance.stats_source,
                "skills_source": provenance.skills_source,
                "card_count": provenance.card_count,
                "notes": basis_lines,
            }
        # A bad --json output path (unwritable directory, permissions)
        # is a command-line-argument problem, not cards.json/owned.json data
        # and not an internal bug — give it the same clean stderr-message +
        # documented-exit-code treatment every other user-input failure mode
        # already gets, rather than letting it fall through to a raw
        # traceback under the undifferentiated default interpreter exit code.
        try:
            Path(args.json).write_text(dumps_payload(payload), encoding="utf-8")
        except OSError as exc:
            print(f"--json: could not write to {args.json!r}: {exc}", file=sys.stderr)
            return EXIT_INVALID_SELECTOR
        return EXIT_OK

    for line in basis_lines:
        print(line)
    # On stdout as well as stderr, and above the table, for the same reason
    # the approximation caveat below is: a caveat printed only to stderr is
    # invisible the moment stdout is piped to a file.
    for line in board_lines:
        print(line)
    # Same rule for the collection: a run scored against the account's real
    # levels is a different answer from the maxed one, and the table must
    # carry which it is.
    for line in collection_lines:
        print(line)
    # On stdout as well as stderr, and BEFORE the table: a ranking a reader
    # scrolls straight to must carry its own caveat, and a caveat printed only
    # to stderr is invisible the moment stdout is piped to a file.
    if heuristic_result is not None:
        for line in format_approximation(heuristic_result, projection):
            print(line)
    for line in format_assumptions(cfg):
        print(line)
    for line in format_completeness(pool.cards):
        print(line)
    for line in format_team_rows(ranked, breakdowns, cfg):
        print(line)
    for line in format_provisional_footer(breakdowns):
        print(line)
    # Printed immediately after the >= lower-
    # bound footer so both provisional-ness signals appear together and
    # neither can be mistaken for the other (missing data vs. an unscored
    # mechanic).
    for line in format_unscored_mechanics_footer(breakdowns):
        print(line)
    if args.explain is not None:
        for line in format_explain(args.explain, ranked[args.explain - 1], breakdowns[args.explain - 1]):
            print(line)
    return EXIT_OK


def format_gates_report(
    team: Team,
    resolved_skills,
    agg: TeamAggregates,
    cards: tuple[Card, ...],
    owned: tuple[Card, ...],
    owned_total: int,
    owned_path: str,
) -> list[str]:
    """The `gates` subcommand's report renderer: per-Type and per-Generation counts, which gate conditions fire, and
    the collection-level completeness report."""
    lines: list[str] = []

    type_parts = [
        f"{type_name}={agg.type_count(type_name)}" for type_name in _TYPE_DISPLAY_ORDER
    ]
    lines.append("TYPE COUNTS: " + " ".join(type_parts))

    generation_parts = [
        f"{generation}={agg.generation_count(generation)}"
        for generation in GENERATIONS
        if agg.generation_count(generation) > 0
    ]
    lines.append("GENERATION COUNTS: " + " ".join(generation_parts))

    if team.leader.costume_skill is not None:
        lines.append(
            f"COSTUME SKILL: {team.leader.id} {team.leader.costume_skill.family} "
            f"(team-wide)"
        )

    for resolved in resolved_skills:
        if resolved.gate is None:
            continue
        predicate = resolved.gate.predicate
        status = "GATE FIRED" if resolved.gate.fired else "GATE NOT FIRED"
        lines.append(
            f"{status}: {resolved.card_id}/{resolved.slot_kind} "
            f"{predicate.dimension} {predicate.value} >= {predicate.threshold} "
            f"(actual {resolved.gate.actual_count})"
        )

    lines.append(
        f"OWNED: {len(owned)}/{owned_total} owned ids resolved from {owned_path}"
    )

    report = completeness_report(cards)
    incomplete_ids = {entry.card_id for entry in report}
    fully_populated = len(cards) - len(incomplete_ids)
    unknown_parts = [
        f"{entry.card_id}.{field}" for entry in report for field in entry.unknown_fields
    ]
    lines.append(
        f"COMPLETENESS: {fully_populated}/{len(cards)} cards fully populated; "
        f"unknown: {' '.join(unknown_parts)}"
    )

    return lines


def run_gates(args: argparse.Namespace) -> int:
    """The `gates` subcommand: same shared
    `load_inputs`, same `format_gates_report`, no second independent load
    path."""
    cards_path, require_mode_b = _card_source(args)
    try:
        cards, owned, cfg, provenance = load_inputs(
            cards_path, Path(args.owned), require_mode_b
        )
    except _LoadFailure as failure:
        return failure.exit_code

    basis_lines = _announce_stats_basis(provenance)
    for line in basis_lines:
        print(line, file=sys.stderr)

    member_ids = [member_id.strip() for member_id in args.members.split(",")]
    try:
        team = build_team(cards, args.leader, member_ids)
    except TeamError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_INPUT_DATA

    agg = aggregate(team.members)
    resolved_skills = resolve_team_gates(team, cfg.run_state)

    for line in basis_lines:
        print(line)
    for line in format_gates_report(
        team, resolved_skills, agg, cards, owned, len(owned), args.owned
    ):
        print(line)

    return EXIT_OK


def run_farm(args: argparse.Namespace) -> int:
    """The `farm` subcommand: rank the owned collection for Marathon event-point
    farming. This is a DIFFERENT objective from the deck-screen Unit Score — event
    points are dominated by which cards match the event's bonus rows, not by stats
    or board. Because the bonus is a SUM of per-card-independent permil, the optimal farming deck is exact and trivial: the highest-permil card of
    each holomem, top N — no combinatorial search."""
    cards_path, require_mode_b = _card_source(args)
    try:
        _cards, owned, _cfg, _prov = load_inputs(
            cards_path, Path(args.owned), require_mode_b
        )
    except _LoadFailure as failure:
        return failure.exit_code

    try:
        event = load_marathon_event(Path(args.marathon))
    except SchemaError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_INPUT_DATA

    blooms = {} if args.no_collection else _collection_blooms(args.collection)

    # Per-card event-bonus permil — independent of the rest of the deck, so computed
    # once per owned card. Keep the best card of each holomem (one card per
    # holomem in a deck), then take the top N by permil.
    skipped = 0
    best_by_member: dict[str, tuple[int, Card]] = {}
    for card in owned:
        result = event_bonus_permil(
            [card], event, channel=args.channel, blooms=blooms, music_id=args.music
        )
        permil = result.per_card_permil[0]
        skipped += result.bloom_rows_skipped
        current = best_by_member.get(card.member)
        if current is None or permil > current[0]:
            best_by_member[card.member] = (permil, card)

    deck = sorted(best_by_member.values(), key=lambda pc: pc[0], reverse=True)[: args.top]
    total_permil = sum(permil for permil, _ in deck)

    print(f"MARATHON FARMING deck — event '{event.name}', channel {args.channel}"
          + (f", song {args.music}" if args.music else ""))
    print(f"(objective: total event-bonus permil; a separate ranking from Unit Score)")
    if not any(permil for permil, _ in deck):
        print("  no owned card matches any bonus row for this event/channel — "
              "farming EV is flat here.")
    for permil, card in deck:
        parts = card.id.split("-")
        chr_id = f"chr-{parts[1]}" if len(parts) > 1 else card.id
        mark = "" if permil else "  (no event bonus)"
        print(f"  +{permil / 10:5.1f}%  {card.member:<20} {card.id}  [{chr_id}]{mark}")
    print(f"  = deck total +{total_permil / 10:.1f}% event score ({total_permil} permil)")

    scalar = points_scalar(
        stamina=args.stamina, recommended=args.recommended, channel=args.channel
    )
    print(f"  x{scalar:g} deck-independent points scalar "
          f"(stamina {args.stamina}"
          + (", recommended-deck x2.0" if args.recommended else "")
          + ") — scales absolute points, does NOT re-order decks; "
          "the absolute composition is not confirmed yet")
    if skipped:
        print(f"  note: {skipped} rarity+bloom row check(s) skipped for want of a known "
              f"bloom — supply --collection to include them", file=sys.stderr)
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    # The projection/progress/warning lines on stderr carry non-ASCII
    # characters (e.g. the em dash in project_duration's staleness
    # warning) -- without this, Windows' default cp1252 console encoding
    # raises/mangles on write, exactly the failure mode stdout's own
    # reconfigure prevents.
    sys.stderr.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "solve":
        return run_solve(args)
    if args.command == "gates":
        return run_gates(args)
    if args.command == "farm":
        return run_farm(args)

    raise AssertionError(f"unhandled command: {args.command!r}")  # pragma: no cover


if __name__ == "__main__":
    raise SystemExit(main())
