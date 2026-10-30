"""Shared fixture builders for the scoring test modules
(`tests/test_scoring_ev.py`, and any sibling `tests/test_scoring_*.py`).

Consolidates the `_STATS`/`_skill`/`_card` builder pattern independently
duplicated in `tests/test_gates.py` and `tests/test_team.py`, extended for
scoring's needs: those gates/team builders deliberately leave `duration`,
`interval`, `probability_tier` and `magnitude` as `None` because gate
resolution never reads them, whereas every EV formula in
`hbuilder.domain.scoring` does. `make_skill()` below exposes all four as
explicit keyword arguments so a test can pin any of them — including to
`UNKNOWN`, the live taint case — without touching the gates or
team test modules' local builders.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from hbuilder.domain.card import Card, GateCondition, SkillSlot, Stats
from hbuilder.domain.gates import GateResolution, ResolvedSkill, RunState
from hbuilder.domain.scoring_config import ScoringConfig, load_scoring_config
from hbuilder.domain.team import Team

# A plain, evenly-split stat baseline — matches the style of test_gates.py's
# and test_team.py's own `_STATS` module constant.
STATS = Stats(performance=100, technique=100, sense=100)

# A fixed RunState fixture for any test that doesn't itself exercise
# run-state gate resolution (mirrors test_gates.py's `_RUN_STATE`).
DEFAULT_RUN_STATE = RunState(life=1000, combo=100)

# Guaranteed never to exist on disk — passing this to load_scoring_config
# always takes the FileNotFoundError -> DEFAULT_CONFIG fallback path,
# giving make_scoring_config() a known-default starting point to override.
_NONEXISTENT_CONFIG_PATH = Path("tests/__scoring_conftest_nonexistent_config__.json")

# The sole definition of where the schema-coverage fixture lives. Every
# test module that needs the shipped 6-card fixture imports these two
# constants rather than hardcoding a path — a future relocation of the
# fixture is one edit here, not fifteen.
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
FIXTURE_CARDS_PATH = FIXTURES_DIR / "cards.json"
FIXTURE_OWNED_PATH = FIXTURES_DIR / "owned.json"


def make_skill(
    kind: str,
    *,
    # The default is `conditional_score_up`, NOT `score_support`.
    # `score_support` is a multiplier on the ACTIVE pool rather than a plain
    # score-pool family, and the tests using this default exercise score-pool
    # plumbing, not support semantics. Tests that mean support say
    # `family="score_support"` explicitly.
    family: str = "conditional_score_up",
    effect_type: str = "score",
    duration: object = None,
    interval: object = None,
    probability_tier: object = None,
    magnitude: object = 10.0,
    condition: GateCondition | None = None,
    target: object = None,
    override: object = None,
    rider: object = None,
) -> SkillSlot:
    """Builds a `SkillSlot` with every EV-relevant field explicit. Pass
    `hbuilder.domain.unknown.UNKNOWN` for any of `duration`/`interval`/
    `probability_tier`/`magnitude` to build the taint case directly —
    scoring tests build `SkillSlot` by hand rather than going through
    `card_from_record`'s JSON-null-to-`UNKNOWN` conversion, so nothing here
    coerces `None` for you. The same applies to `condition` —
    `condition=None` here is always the structurally-ungated Python-level
    value, never JSON null/UNKNOWN; pass `GateCondition(...)` or
    `hbuilder.domain.unknown.UNKNOWN` explicitly to build the gated or
    tainted cases respectively.

    `target` follows the identical three-state
    contract — `target=None` (the default) is always the structurally
    team-wide Python-level value (mirrors the on-disk affirmative
    `{"kind": "all"}` marker), never JSON null/UNKNOWN; pass
    `hbuilder.domain.card.EffectTarget(...)` or
    `hbuilder.domain.unknown.UNKNOWN` explicitly to build the targeted or
    tainted cases respectively.

    `override`/`rider` follow the identical
    three-state contract — `override=None`/`rider=None` (the defaults) are
    always the structurally "no second effect" Python-level value (mirrors
    the on-disk affirmative `{"kind": "none"}` marker), never JSON
    null/UNKNOWN; pass `hbuilder.domain.card.SlotOverride(...)`/
    `hbuilder.domain.card.SlotRider(...)` or
    `hbuilder.domain.unknown.UNKNOWN` explicitly to build the populated or
    tainted cases respectively."""
    return SkillSlot(
        kind=kind,
        family=family,
        effect_type=effect_type,
        duration=duration,
        interval=interval,
        probability_tier=probability_tier,
        magnitude=magnitude,
        condition=condition,
        target=target,
        override=override,
        rider=rider,
    )


def make_card(
    card_id: str,
    *,
    generations: frozenset[str] = frozenset({"1期生"}),
    type_: str = "Cute",
    stats: Stats = STATS,
    special: SkillSlot | None = None,
    active: SkillSlot | None = None,
    passive: SkillSlot | None = None,
    costume_skill: SkillSlot | None = None,
    member: str | None = None,
) -> Card:
    # `member` is the holomem, and two cards may share one (two skins of the
    # same character). It defaults to a per-id value so distinct ids stay
    # distinct holomem unless a test deliberately says otherwise — R1 forbids
    # the same holomem twice among the 5 members.
    return Card(
        id=card_id,
        member=member if member is not None else f"Member {card_id}",
        generations=generations,
        type=type_,
        rarity="SSR",
        costume_name=f"Costume {card_id}",
        stats=stats,
        skills=(special, active, passive),
        costume_skill=costume_skill,
    )


def make_team(cards: list[Card]) -> Team:
    """A six-card `Team` from a list of per-card specifications built via
    `make_card`: `cards[0]` is the Leader, `cards[1:6]` are the five
    Members, in order."""
    if len(cards) != 6:
        raise ValueError(f"make_team requires exactly 6 cards, got {len(cards)}")
    return Team(leader=cards[0], members=tuple(cards[1:6]))


def make_resolved(
    card_id: str,
    slot_kind: str,
    skill: SkillSlot,
    *,
    source: str = "normal",
    family: str | None = None,
    gate: GateResolution | None = None,
    override_gate: GateResolution | None = None,
    member: str | None = None,
) -> ResolvedSkill:
    """A bare `ResolvedSkill`, bypassing `resolve_team_gates` entirely —
    for tests that exercise one EV function or `_resolve_skill` in
    isolation against a hand-built skill/gate combination (e.g. handing an
    ACTIVE-shaped `ResolvedSkill` to `_score_special` to pin the
    entry-guard mismatch). `override_gate` follows the
    same pattern as `gate` — pass a `GateResolution` explicitly to build
    the fired/unfired override cases; the default `None` matches a skill
    with no override at all."""
    return ResolvedSkill(
        card_id=card_id,
        member=member if member is not None else f"Member {card_id}",
        slot_kind=slot_kind,
        source=source,
        family=family if family is not None else skill.family,
        skill=skill,
        gate=gate,
        override_gate=override_gate,
    )


def make_scoring_config(
    *,
    probability_tiers: dict | None = None,
    family_weights: dict | None = None,
    expected_fires_per_run: float | None = None,
    run_state: RunState | None = None,
) -> ScoringConfig:
    """A `ScoringConfig` built from `DEFAULT_CONFIG` (via
    `load_scoring_config`'s absent-file fallback), with any of the given
    fields overridden — e.g. to pin a probability tier to exactly 1.0
    without editing the repo-root `config.json`."""
    cfg = load_scoring_config(_NONEXISTENT_CONFIG_PATH)
    overrides: dict = {}
    if probability_tiers is not None:
        overrides["probability_tiers"] = probability_tiers
    if family_weights is not None:
        overrides["family_weights"] = family_weights
    if expected_fires_per_run is not None:
        overrides["expected_fires_per_run"] = expected_fires_per_run
    if run_state is not None:
        overrides["run_state"] = run_state
    return dataclasses.replace(cfg, **overrides) if overrides else cfg
