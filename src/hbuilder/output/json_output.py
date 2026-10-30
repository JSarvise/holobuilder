"""`--json`'s payload builder and serializer — the surface
other things diff against. Once a key name is emitted here, every stored
prior run and every downstream consumer is pinned to it, which is why the
shape below is a deliberate flat envelope that stays stable.

Six sorted top-level keys, chosen so a diff of two runs localises cleanly
to the section that changed: `schema_version` (this output contract's own
version, distinct from `cards.json`'s `SCHEMA_VERSION` — the latter is
also stamped into the payload, under `completeness.cards_schema_version`,
so a consumer can tell which on-disk card contract produced the run
without a second top-level key), `config`, `completeness`, `pool`,
`ordering`, `teams`.

Floats are emitted as native JSON numbers, never as strings. CPython's
`json` encoder formats a float with `float.__repr__`, so `json.dumps(v)`
is already byte-identical to `repr(v)` for every finite float — exactly
round-trip exact. `tests/test_json_diffability.py`
pins this empirically over an adversarial float set, named for the
defect it guards: if a future CPython release changes the encoder's
float formatting, that test fails loudly rather than floats silently
losing precision across a diff.

The lower-bound claim rides on the data, not on a rendering flourish:
every team object carries `is_lower_bound` and `incomplete_fields` as
mandatory keys, so a machine consumer diffing two runs can never mistake a
lower bound for a settled score.

`--explain` selects depth, `--json` selects format, and neither
constrains the other — `explain_rank` here only ever adds a
`contributions` key to exactly one team object; every other team object,
and every other section of the payload, is unaffected.

No file I/O, no `argparse`, never `print`s (the `solver/`- and
`output/`-layer rules in ARCHITECTURE.md) — this module returns a string;
the CLI wires it to the `--json` flag and any `--output` path.
"""

from __future__ import annotations

import json
import math
from typing import Sequence

from hbuilder.domain.card import Card
from hbuilder.domain.loader import completeness_report
from hbuilder.domain.schema import RUN_STATE_DIMENSIONS, SCHEMA_VERSION
from hbuilder.domain.scoring import (
    ScoreBreakdown,
    SkillContribution,
    deck_screen_unit_score,
)
from hbuilder.domain.scoring_config import ScoringConfig
from hbuilder.domain.team import MEMBER_COUNT
from hbuilder.output.table import exact_row_count
from hbuilder.solver.exact import RankedTeam

# The `--json` output-contract version (every payload traces to
# the contract that produced it). Deliberately separate from
# `cards.json`'s own `SCHEMA_VERSION` (imported above, stamped under
# `completeness.cards_schema_version`) — the two version a different
# file with a different evolution path, and conflating them into one
# number would make "did the card format change" and "did the JSON
# contract change" indistinguishable from the outside.
OUTPUT_SCHEMA_VERSION: str = "1"


def _require_finite(value: float, path: str) -> float:
    """Raise `ValueError` naming `path` when `value` is `nan` or `inf`.
    A non-finite float would serialise to the bare tokens
    `NaN`/`Infinity`, which are not valid JSON — `json.dumps` emits them
    anyway unless `allow_nan=False`, so every float that reaches the
    payload is routed through here first, converting a latent
    invalid-output path into a named error a consumer fails to *build*
    rather than silently fails to *parse*."""
    if not math.isfinite(value):
        raise ValueError(
            f"{path}: value {value!r} is not finite (nan/inf cannot be "
            f"encoded as valid JSON) — this indicates a scoring defect, "
            f"not a data-completeness gap (which is already carried "
            f"separately as is_lower_bound/incomplete_fields)"
        )
    return value


def _build_contribution_object(
    index: int, contribution: SkillContribution, team_path: str
) -> dict:
    """One `SkillContribution` in full — every field of that dataclass,
    under its own name, so `--json --explain <rank>` carries the same
    attribution `output/explain.py`'s text rendering does (depth is a
    parameter, format is the module).

    `rider_family`/`rider_effect_type`/`rider_magnitude` are included,
    deliberately, so a `--json` consumer can detect a rider exactly as
    `--explain`'s text rendering does, like `target_kind`/`target_card_ids`."""
    path = f"{team_path}.contributions[{index}]"
    return {
        "card_id": contribution.card_id,
        "slot_kind": contribution.slot_kind,
        "source": contribution.source,
        "family": contribution.family,
        "effect_type": contribution.effect_type,
        "state": contribution.state,
        "pool": contribution.pool,
        "rate": _require_finite(contribution.rate, f"{path}.rate"),
        "weight": _require_finite(contribution.weight, f"{path}.weight"),
        "weighted_rate": _require_finite(
            contribution.weighted_rate, f"{path}.weighted_rate"
        ),
        "missing_field": contribution.missing_field,
        "gate_dimension": contribution.gate_dimension,
        "gate_value": contribution.gate_value,
        "gate_threshold": contribution.gate_threshold,
        "gate_actual_count": contribution.gate_actual_count,
        "gate_fired": contribution.gate_fired,
        "target_kind": contribution.target_kind,
        "target_card_ids": (
            list(contribution.target_card_ids)
            if contribution.target_card_ids is not None
            else None
        ),
        "override_fired": contribution.override_fired,
        "rider_family": contribution.rider_family,
        "rider_effect_type": contribution.rider_effect_type,
        "rider_magnitude": contribution.rider_magnitude,
    }


def build_team_object(
    rank: int,
    ranked_team: RankedTeam,
    breakdown: ScoreBreakdown,
    *,
    with_contributions: bool,
) -> dict:
    """One `teams[]` entry. `score` is `breakdown.total_score` as a float.
    `is_lower_bound` is `bool(breakdown.incomplete_skills)` and
    `incomplete_fields` is that tuple as a list — the lower-
    bound claim reads directly off the breakdown, never recomputing
    `team_taint` a second time. `gates` lists every gated
    `SkillContribution` (`gate_fired is not None`, mirroring
    `output/table.py`'s own filter) with its dimension, value, threshold
    and actual team count, so the count that fired a gate survives into
    JSON exactly as it does into the table. `contributions`,
    added only when `with_contributions` is true, is one object per
    `SkillContribution` in `breakdown.contributions` order — already
    `Team.effective_skill_instances()` order, never reordered."""
    path = f"teams[{rank}]"
    gated = [
        contribution
        for contribution in breakdown.contributions
        if contribution.gate_fired is not None
    ]
    # `gates` is a narrower per-GATE projection (dimension/
    # value/threshold/actual_count/fired) — deliberately not extended with
    # `target_kind`/`target_card_ids`. Which cards a targeted stat effect
    # *reached* is an unrelated fact from which cards' presence made a
    # *gate* fire; a consumer wanting both already gets `target_kind`/
    # `target_card_ids` from the full `contributions[]` projection
    # (`--explain`) above, so duplicating them here would only restate
    # data already in the payload, not add anything new about the gate
    # itself.
    gates = [
        {
            "card_id": contribution.card_id,
            "slot_kind": contribution.slot_kind,
            "dimension": contribution.gate_dimension,
            "value": contribution.gate_value,
            "threshold": contribution.gate_threshold,
            "actual_count": contribution.gate_actual_count,
            "fired": contribution.gate_fired,
        }
        for contribution in gated
    ]

    team_object: dict = {
        "rank": rank,
        "leader_id": ranked_team.leader_id,
        "member_ids": list(ranked_team.member_ids),
        "score": _require_finite(breakdown.total_score, f"{path}.score"),
        "is_lower_bound": bool(breakdown.incomplete_skills),
        "incomplete_fields": list(breakdown.incomplete_skills),
        "gates": gates,
    }
    if with_contributions:
        team_object["contributions"] = [
            _build_contribution_object(index, contribution, path)
            for index, contribution in enumerate(breakdown.contributions)
        ]
    return team_object


def build_score_terms(breakdown: ScoreBreakdown, *, board_modelled: bool) -> dict:
    """The term-level decomposition of one team's score, in the game's own
    two-column shape — for a UI panel, never for a ranking.

    **This is NOT the game's Unit Score Details panel and must not be labelled
    as one.** `total_score` is a HYBRID — the deck-screen power assembly
    wearing the in-run score stage — and is therefore neither the deck
    screen's Unit Score nor the in-run score exactly. The deck screen ADDS the
    board score bonus where this ranking MULTIPLIES it, which overstates by
    roughly 2–12 % and is team-dependent. `label` says "our ranking score" for
    that reason, and `is_unit_score` is a hard `False` so a consumer cannot
    quietly relabel it.

    **Why the bonus column has one combined row and not four.**
    The game splits the score bonus five ways by source; this model collapses
    ACTIVE / SPECIAL / PASSIVE / Outfit into a single `combined_score_rate`
    because the pool is `(1 + B) x max` per bucket, not a sum.
    `ScoreBreakdown`'s own docstring records that the per-skill rows do
    **not** sum to the pool. So splitting the number into the game's five lines
    would not be a reporting change — **it would change the number.** The row
    is emitted as one honest `combined` entry rather than four fabricated ones.
    That limit is structural; it is not a gap waiting to be filled.

    Every row carries its `unit`, because power rows are absolute and bonus
    rows are permil — a renderer that guesses is wrong by three orders of
    magnitude. `basis` is one of:

      `computed`     calculated here and reliable
      `combined`     real, but merged with others and not separable (see above)
      `folded`       inside another line and not extractable without changing it
      `not-modelled` not computed; the value is `None`, never `0`

    `memory_bonus` and `member_power_up` are reported OUTSIDE the total, since
    both are ranking-safe to drop and `total_score` does not include them.
    `None` there means the player has supplied no account scalar — never
    `0`."""
    stat_rate = breakdown.combined_stat_rate
    overall_power = breakdown.effective_stat_sum + breakdown.board_power

    power_rows = [
        {
            "field": "card_potential_parameter",
            "label": "Member Parameter",
            "value": breakdown.base_stat_sum,
            "unit": "power",
            "basis": "computed",
        },
        {
            "field": "up_by_live_passive_skill",
            "label": "Passive / Outfit stat effects",
            "value": breakdown.effective_stat_sum - breakdown.base_stat_sum,
            "unit": "power",
            "basis": "folded",
            "note": (
                f"the stat-effect rate (+{stat_rate * 100:.2f} %) applied to the "
                "line above. The game bills Passive and Outfit separately; ours "
                "is one rate over the whole stat sum and is not separable into "
                "them (DECK-SCORE.md §2a)"
            ),
        },
        {
            "field": "up_by_skill_tree",
            "label": "Holomem Board",
            "value": breakdown.board_power if board_modelled else None,
            "unit": "power",
            "basis": "computed" if board_modelled else "not-modelled",
        },
    ]

    bonus_rows = [
        {
            "field": "combined_score_rate",
            "label": "Active + Special + Passive + Outfit",
            "value": breakdown.combined_score_rate * 1000.0,
            "unit": "permil",
            "basis": "combined",
            "note": (
                "the game splits this four ways by source; the pool is "
                "(1 + B) x max per bucket, not a sum, so splitting it would "
                "change the number rather than merely reporting it "
                "(DECK-SCORE.md §2b)"
            ),
        },
        {
            "field": "by_skill_tree",
            "label": "Holomem Board",
            "value": breakdown.board_score_rate * 1000.0 if board_modelled else None,
            "unit": "permil",
            "basis": "computed" if board_modelled else "not-modelled",
        },
    ]

    # The deck screen's own quantity, in its ADDITIVE shape, beside the
    # ranking score: a separate number, never a change to `total_score`. Its
    # `caveat` travels with it because the FORMULA being exact (+/-1) does not
    # make the NUMBER right — the base still carries an error of roughly
    # +/-13 %, which is larger than the shape error this removes.
    deck_screen = deck_screen_unit_score(breakdown)

    return {
        # A hard false, not an omission: see the docstring.
        "is_unit_score": False,
        "total_label": "our ranking score",
        "total_score": breakdown.total_score,
        "deck_screen": {
            # Rounded HERE, at the display boundary. `domain/scoring.py` keeps
            # the arithmetic exact on purpose (its no-rounding guard). The rule
            # that reproduces the game is `ceil` (checked against captured
            # Live/GetDeck decks in `tests/test_deck_screen_unit_score.py`);
            # `round` is 0-or-+1 off.
            "unit_score": math.ceil(deck_screen.unit_score),
            "overall_power": deck_screen.overall_power,
            "score_bonus_permil": deck_screen.score_bonus_permil,
            "complete": deck_screen.complete,
            "missing": list(deck_screen.missing),
            "caveat": (
                "The deck screen's own additive shape. The FORMULA is exact — "
                "it reproduces all 10 captured decks to within ±1. The BASE is "
                "not: no stat source on disk reproduces the game's Member "
                "Parameter line (MATH.md §2.6, −12.9…+13.7 %), so this will "
                "NOT match the number on your deck screen. It removes the "
                "shape error, which isolates the rest to one known cause."
            ),
        },
        "power": power_rows,
        "overall_power": overall_power,
        "bonus": bonus_rows,
        # Reported, deliberately not summed into the total.
        "outside_total": [
            {
                "field": "up_by_poster_collect",
                "label": "Memory Bonus",
                "value": breakdown.memory_bonus,
                "unit": "power",
                "basis": "computed" if breakdown.memory_bonus is not None else "not-modelled",
            },
            {
                "field": "card_training_evaluation",
                "label": "Member Power-Up",
                "value": breakdown.member_power_up,
                "unit": "power",
                "basis": (
                    "computed" if breakdown.member_power_up is not None else "not-modelled"
                ),
            },
        ],
    }


def _arrangement_count(candidate_count: int) -> int:
    """`C(candidate_count, 6) * 6` (the unconstrained formula) —
    `0` when fewer than `MEMBER_COUNT` candidates are available, mirroring
    `solve_exact`'s own "never raises, yields nothing" behavior at that
    boundary. It does not apply the `--require`/`--leader`/`--exclude`
    reduction; `selectors` is accepted and embedded verbatim, and a caller
    that applied selectors passes its own `arrangement_count`."""
    if candidate_count < MEMBER_COUNT:
        return 0
    return math.comb(candidate_count, MEMBER_COUNT) * candidate_count


def build_payload(
    ranked: Sequence[RankedTeam],
    breakdowns: Sequence[ScoreBreakdown],
    cfg: ScoringConfig,
    cards: tuple[Card, ...],
    selectors: dict,
    *,
    explain_rank: int | None = None,
    owned_count: int | None = None,
    arrangement_count: int | None = None,
) -> dict:
    """Assemble the six top-level keys.

    `cards` is the candidate pool this run actually searched (what
    `solve_exact` was called with) — used for `completeness`
    (`completeness_report(cards)`) and for `pool.candidate_count`, which
    always means "how many cards were searched".

    `owned_count` and `arrangement_count` answer different questions and
    must therefore be supplied by the caller that knows the answers:

    - `owned_count` is how many cards are actually owned. It differs from
      `candidate_count` as soon as `--include-unowned` widens the searched
      pool or `--exclude` narrows it.
    - `arrangement_count` is how many arrangements the solver actually
      enumerated, which `--require`/`--leader` reduce. The CLI passes the
      very number it prints in its stderr `PROJECTION:` line, so the two
      cannot drift apart — recomputing it here from `len(cards)` would let
      the JSON claim 6 while the solver evaluated 1.

    Both default to the **unconstrained** reading (`len(cards)` and
    `C(len(cards), 6) * 6`), which is the correct answer when no selector
    is in play — the case the schema tests exercise. They are not a
    fallback for a caller that has selectors and forgets to pass them.

    `selectors` is an already-built, JSON-serializable mapping describing
    whatever selector state produced `cards` (e.g. `--require`/`--leader`/
    `--exclude`/`--include-unowned`); this module does not interpret its
    shape, only embeds it under `pool.selectors` — selector *parsing* is
    the CLI's job, not this payload builder's.

    `ordering.exact_through_rank` calls `table.exact_row_count` — the
    same function the text table's lower-bound footer uses — so the JSON
    boundary and the text footer can never drift apart (single source of
    truth).

    When `explain_rank` is not `None`, only that rank's team object
    carries `contributions`; every other team object is unchanged from a
    call with `explain_rank=None` (depth is a parameter, format is
    the module, and neither constrains the other)."""
    config_object = {
        "stacking_model": cfg.stacking_model.name,
        "activation_rate_model": cfg.activation_rate_model.name,
        "probability_tiers": {
            tier: _require_finite(value, f"config.probability_tiers.{tier}")
            for tier, value in cfg.probability_tiers.items()
        },
        "family_weights": {
            family: _require_finite(value, f"config.family_weights.{family}")
            for family, value in cfg.family_weights.items()
        },
        "expected_fires_per_run": _require_finite(
            cfg.expected_fires_per_run, "config.expected_fires_per_run"
        ),
        "assumed_run_state": {
            dim: int(getattr(cfg.run_state, dim)) for dim in RUN_STATE_DIMENSIONS
        },
        "provenance": dict(cfg.provenance),
    }

    report = completeness_report(cards)
    incomplete_ids = {entry.card_id for entry in report}
    unknown_fields = [
        f"{entry.card_id}.{field}" for entry in report for field in entry.unknown_fields
    ]
    completeness_object = {
        "cards_total": len(cards),
        "cards_fully_populated": len(cards) - len(incomplete_ids),
        "unknown_fields": unknown_fields,
        # cards.json's own contract version, distinct from
        # this module's OUTPUT_SCHEMA_VERSION (which is a top-level key
        # in its own right) — nested here because it names which on-disk
        # card contract produced this run's completeness figures.
        "cards_schema_version": SCHEMA_VERSION,
    }

    pool_object = {
        "owned_count": len(cards) if owned_count is None else owned_count,
        "candidate_count": len(cards),
        "arrangement_count": (
            _arrangement_count(len(cards))
            if arrangement_count is None
            else arrangement_count
        ),
        "selectors": selectors,
    }

    ordering_object = {"exact_through_rank": exact_row_count(breakdowns)}

    teams = [
        build_team_object(
            rank,
            candidate,
            breakdown,
            with_contributions=(explain_rank is not None and rank == explain_rank),
        )
        for rank, (candidate, breakdown) in enumerate(zip(ranked, breakdowns), start=1)
    ]

    return {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "config": config_object,
        "completeness": completeness_object,
        "pool": pool_object,
        "ordering": ordering_object,
        "teams": teams,
    }


def dumps(payload: dict) -> str:
    """`json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2)`
    plus a trailing newline. `sort_keys=True` is what makes the output
    diff cleanly regardless of dict construction order.
    `ensure_ascii=False` keeps Japanese generation names readable in the
    diff rather than escaped. Never `default=str` — a value the encoder
    cannot handle must fail loudly rather than be stringified into a
    shape a consumer will misread."""
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2) + "\n"
