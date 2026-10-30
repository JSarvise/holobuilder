"""The per-run assumptions header (every unverified constant that
produced a ranking, plus its origin — `config.json` or the shipped
default) and the collection-level data-completeness report. Both
functions are `list[str]`-builders; neither prints, following
`output/table.py`'s shape.

The word "unverified" is load-bearing: a value this module reports either
came from the user's own `config.json` (a claim the user made) or is a
shipped default nobody has verified against
the real game, and the reader must never have to guess which. Provenance
records *where* a value came from, never whether it differs from the
default — a hand-authored `config.json` byte-identical to `DEFAULT_CONFIG`
is still reported `(config.json)`.
"""

from __future__ import annotations

from hbuilder.domain.card import Card
from hbuilder.domain.loader import completeness_report
from hbuilder.domain.schema import (
    PROBABILITY_TIERS,
    RUN_STATE_DIMENSIONS,
    SKILL_FAMILIES,
)
from hbuilder.domain.scoring import ScoreBreakdown
from hbuilder.domain.scoring_config import DEFAULT_CONFIG, ScoringConfig

# The config's own declared top-level key order — read from
# `DEFAULT_CONFIG`'s own dict-literal order rather than re-declaring it
# here, so the two orderings cannot silently drift apart. `schema_version`
# is excluded: it names a schema, not an unverified modeling constant.
# Iterating this fixed tuple (rather than `cfg.provenance`'s own dict
# order, which is itself built by iterating this same tuple in
# `load_scoring_config`) is what makes two calls against the same config
# produce identical output.
_ASSUMPTION_KEYS: tuple[str, ...] = tuple(
    key for key in DEFAULT_CONFIG if key != "schema_version"
)


def _origin(provenance_value: str) -> str:
    """`"file"` -> `config.json`; anything else -> the literal phrase
    `unverified default`, which must appear on every defaulted value —
    never silently trusted."""
    return "config.json" if provenance_value == "file" else "unverified default"


def format_assumptions(
    cfg: ScoringConfig, breakdown_or_none: ScoreBreakdown | None = None
) -> list[str]:
    """One line per unverified constant, each suffixed with its origin
    read from `cfg.provenance`. Covers every top-level key: all three
    `probability_tiers`, every `family_weights` entry, `special
    .expected_fires_per_run`, `model.stacking`, `model.activation_rate`,
    and both `assumed_run_state` flags.

    Iterates `_ASSUMPTION_KEYS` — the config's own declared top-level key
    order — rather than a dict's insertion order, so two runs against the
    same config produce byte-identical text.

    `breakdown_or_none` is accepted but unread: this header is a property
    of `cfg` alone and is identical regardless of which ranked team is
    being displayed alongside it. The parameter is reserved for a future
    CLI surface that renders this header next to a single team's
    `--explain` output without needing a second call shape."""
    del breakdown_or_none  # reserved for future CLI wiring

    lines: list[str] = []
    for key in _ASSUMPTION_KEYS:
        origin = _origin(cfg.provenance[key])
        if key == "probability_tiers":
            tiers = " ".join(
                f"{tier}={cfg.probability_tiers[tier]!r}" for tier in PROBABILITY_TIERS
            )
            lines.append(f"PROBABILITY_TIERS: {tiers} ({origin})")
        elif key == "family_weights":
            weights = " ".join(
                f"{family}={cfg.family_weights[family]!r}" for family in SKILL_FAMILIES
            )
            lines.append(f"FAMILY_WEIGHTS: {weights} ({origin})")
        elif key == "special":
            lines.append(
                f"SPECIAL.EXPECTED_FIRES_PER_RUN: {cfg.expected_fires_per_run!r} "
                f"({origin})"
            )
        elif key == "model":
            lines.append(f"MODEL.STACKING: {cfg.stacking_model.name} ({origin})")
            lines.append(
                f"MODEL.ACTIVATION_RATE: {cfg.activation_rate_model.name} ({origin})"
            )
            lines.append(
                f"MODEL.ACTIVE_UPTIME: {cfg.active_uptime_model} ({origin})"
            )
        elif key == "target_selection":
            lines.append(
                f"TARGET_SELECTION: strategy={cfg.target_selection_model.name} "
                f"under_supply={cfg.target_under_supply} ({origin})"
            )
        elif key == "assumed_run_state":
            flags = " ".join(
                f"{dim}={getattr(cfg.run_state, dim)!r}" for dim in RUN_STATE_DIMENSIONS
            )
            lines.append(f"ASSUMED_RUN_STATE: {flags} ({origin})")
        else:  # pragma: no cover - import-time key set is fixed and known
            raise AssertionError(f"format_assumptions: unhandled config key {key!r}")
    return lines


def format_completeness(cards: tuple[Card, ...]) -> list[str]:
    """The collection-level half of the header: how complete `cards` is,
    following `cli.format_gates_report`'s existing `COMPLETENESS:`
    line convention. A fully populated collection still emits the line,
    reporting an explicit `0` unknown fields rather than omitting the
    line — the completeness indicator appears in every output mode,
    whether or not a gap actually exists."""
    report = completeness_report(cards)
    incomplete_ids = {entry.card_id for entry in report}
    fully_populated = len(cards) - len(incomplete_ids)
    unknown_parts = [
        f"{entry.card_id}.{field}" for entry in report for field in entry.unknown_fields
    ]
    line = (
        f"COMPLETENESS: {fully_populated}/{len(cards)} cards fully populated; "
        f"{len(unknown_parts)} unknown field(s)"
    )
    if unknown_parts:
        line += f": {' '.join(unknown_parts)}"
    return [line]
