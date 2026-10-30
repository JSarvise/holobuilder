"""The batch API pins.

`resolve_card_set_gates`/`score_card_set` are the sole, leader-agnostic
implementations of gate resolution and per-skill EV arithmetic that the
solver's hot loop (and the heuristic's local-swap loop) is allowed to call.
The defect this file exists to catch: an incremental `combine()` fold is
O(1) per leader and looks like a free win, but float addition and
multiplication are not associative, so a folded total is not bit-identical
to the recomputed one -- batch/single-team parity would silently degrade
from a structural guarantee to an approximate one while every cross-run
determinism test stayed green, since that guarantee only requires the same
code to produce the same output on repeated runs, not that two
independently-recomputing entry points agree bit for bit.

Every parity assertion in this file uses plain `==`, never a
closeness-tolerant comparison -- tolerance-based parity would convert a
provable structural guarantee into a numeric one and introduce a new
unverified tolerance constant.
"""

from __future__ import annotations

import ast
import itertools
from pathlib import Path

import pytest

from hbuilder.domain.card import EffectTarget, GateCondition, SlotRider, Stats
from hbuilder.domain.gates import resolve_card_set_gates
from hbuilder.domain.loader import load_cards
from hbuilder.domain.scoring import (
    ScoringError,
    _leader_class_key,
    score_card_set,
    score_fast,
    score_with_breakdown,
)
from hbuilder.domain.scoring_config import load_scoring_config
from hbuilder.domain.scoring_models import ACTIVATION_RATE_MODELS, STACKING_MODELS
from hbuilder.domain.team import Team, build_team
from hbuilder.domain.unknown import UNKNOWN

import dataclasses

from conftest import FIXTURE_CARDS_PATH, make_card, make_scoring_config, make_skill

REPO_ROOT = Path(__file__).resolve().parent.parent
SCORING_PATH = REPO_ROOT / "src" / "hbuilder" / "domain" / "scoring.py"
GATES_PATH = REPO_ROOT / "src" / "hbuilder" / "domain" / "gates.py"

_FIXTURE_MEMBER_IDS = ["fubuki_01", "mio_01", "matsuri_01", "polka_01", "lamy_01"]

_MODEL_COMBINATIONS = [
    ("additive", "pp"),
    ("additive", "multiplicative"),
    ("multiplicative", "pp"),
    ("multiplicative", "multiplicative"),
]


def _cfg_for(stacking_key: str, activation_rate_key: str):
    cfg = load_scoring_config()
    return dataclasses.replace(
        cfg,
        stacking_model=STACKING_MODELS[stacking_key],
        activation_rate_model=ACTIVATION_RATE_MODELS[activation_rate_key],
    )


def _shipped_pool():
    return tuple(load_cards(FIXTURE_CARDS_PATH))


def _synthetic_pool():
    """A 7-card pool exercising every family/pool combination and a
    leader-varying COSTUME gate, but deliberately holding exactly one
    `activation_rate_up` carrier (`synth_00`) -- with two or more such
    carriers, which of them is "leader" changes the summation order
    `_fold_activation_rate_up` walks, and float addition is not
    associative across reorderings, so a synthetic pool built to stress
    every other axis is deliberately not built to stress that one too.

    `synth_01`'s PASSIVE `stat_up` skill's `target`
    names a real, populated `{"kind": "type", "value": "Cute",
    "count": 2}` clause -- a strict subset of the pool's three Cute cards
    (`synth_00`/`synth_01`/`synth_02`) -- rather than the team-wide
    default, so this pool's existing bit-identical parity sweep also
    covers a targeted stat effect. Every card's stats are deliberately
    non-uniform (unlike `conftest.STATS`'s evenly-split baseline) so the
    targeted subset's stat contribution is distinguishable from the whole
    set's -- with uniform stats, targeted and team-wide arithmetic would
    agree by coincidence and this parity assertion would pin nothing
    about targeting at all.

    `synth_03`'s SPECIAL slot additionally
    carries an `activation_rate_up` RIDER (never a principal -- this pool
    still holds exactly one `activation_rate_up` PRINCIPAL, `synth_00`'s
    ACTIVE skill). A rider carrier does not count against that one-carrier
    invariant: `_fold_activation_rate_up` reads `resolved.family`, the
    principal, so a rider never enters that fold regardless of which
    family it names, and cannot introduce a second summation-order-
    sensitive term."""
    activation_rate_skill = make_skill(
        "ACTIVE",
        family="activation_rate_up",
        effect_type="activation_rate",
        duration=8.0,
        interval=10.0,
        probability_tier="Medium",
        magnitude=15.0,
    )
    gated_stat_skill = make_skill(
        "PASSIVE",
        family="stat_up",
        effect_type="performance",
        magnitude=12.0,
        condition=GateCondition(kind="type", target="Cute", count=3),
        target=EffectTarget(kind="type", value="Cute", count=2),
    )
    gated_costume_skill = make_skill(
        "COSTUME",
        family="score_support",
        effect_type="score",
        magnitude=25.0,
        condition=GateCondition(kind="type", target="Cute", count=3),
    )
    active_score_skill = make_skill(
        "ACTIVE",
        family="score_support",
        effect_type="score",
        duration=9.0,
        interval=12.0,
        probability_tier="High",
        magnitude=20.0,
    )
    special_skill = make_skill(
        "SPECIAL",
        family="conditional_score_up",
        effect_type="score",
        duration=6.0,
        magnitude=18.0,
        rider=SlotRider(
            family="activation_rate_up",
            effect_type="activation_rate",
            magnitude=55.0,
            condition=GateCondition(kind="run_state", target="life", count=1000),
            target=None,
        ),
    )
    data_missing_skill = make_skill(
        "ACTIVE",
        duration=10.0,
        interval=10.0,
        probability_tier="Medium",
        magnitude=UNKNOWN,
    )
    judgment_skill = make_skill(
        "PASSIVE", family="judgment_enhancement", effect_type="judgment", magnitude=8.0
    )
    life_skill = make_skill("PASSIVE", family="life_recovery", effect_type="life", magnitude=5.0)

    return (
        make_card(
            "synth_00",
            type_="Cute",
            stats=Stats(performance=130, technique=140, sense=150),
            active=activation_rate_skill,
        ),
        make_card(
            "synth_01",
            type_="Cute",
            stats=Stats(performance=200, technique=110, sense=120),
            passive=gated_stat_skill,
            costume_skill=gated_costume_skill,
        ),
        make_card(
            "synth_02",
            type_="Cute",
            stats=Stats(performance=160, technique=150, sense=130),
            active=active_score_skill,
        ),
        make_card(
            "synth_03",
            type_="Pure",
            stats=Stats(performance=90, technique=95, sense=100),
            special=special_skill,
        ),
        make_card(
            "synth_04",
            type_="Pure",
            stats=Stats(performance=80, technique=85, sense=90),
            active=data_missing_skill,
        ),
        make_card(
            "synth_05",
            type_="Happy",
            stats=Stats(performance=70, technique=75, sense=80),
            passive=judgment_skill,
        ),
        make_card(
            "synth_06",
            type_="Happy",
            stats=Stats(performance=60, technique=65, sense=70),
            passive=life_skill,
        ),
    )


def _undersupplied_pool():
    """A dedicated 7-card pool holding only two Happy-type cards
    (`under_00`/`under_01`), so a stat_up target naming `{"kind": "type",
    "value": "Happy", "count": 3}` always undersupplies: every 6-card
    combination drawn from this pool retains at most 2 qualifying cards,
    never the 3 the target asks for. This exercises the configured
    `target_selection.under_supply` rule (`apply_to_available`/
    `do_not_apply`), never `TopNByBuffedStat`'s ordinary selection path,
    and holds no `activation_rate_up` carrier at all -- the under-supply
    edge does not need to stress that axis too."""
    undersupplied_skill = make_skill(
        "PASSIVE",
        family="stat_up",
        effect_type="performance",
        magnitude=10.0,
        target=EffectTarget(kind="type", value="Happy", count=3),
    )
    return (
        make_card(
            "under_00",
            type_="Happy",
            stats=Stats(performance=150, technique=100, sense=90),
            passive=undersupplied_skill,
        ),
        make_card(
            "under_01", type_="Happy", stats=Stats(performance=120, technique=110, sense=95)
        ),
        make_card(
            "under_02", type_="Cute", stats=Stats(performance=100, technique=140, sense=100)
        ),
        make_card(
            "under_03", type_="Cute", stats=Stats(performance=90, technique=120, sense=110)
        ),
        make_card(
            "under_04", type_="Pure", stats=Stats(performance=80, technique=100, sense=130)
        ),
        make_card(
            "under_05", type_="Pure", stats=Stats(performance=70, technique=90, sense=140)
        ),
        make_card(
            "under_06", type_="Pure", stats=Stats(performance=60, technique=80, sense=150)
        ),
    )


def _all_arrangements(pool: tuple):
    """Every (members, leader_index, leader, members, team) arrangement over
    `pool`: 5 members drawn
    from the pool, times every candidate leader — which is any card in the
    pool, including one of those five, because the outfit slot sits outside
    the five. `leader_index` indexes `pool`, matching `score_card_set`'s
    `leaders` argument."""
    for members in itertools.combinations(pool, 5):
        if len({card.member for card in members}) != 5:
            continue  # R1: a holomem cannot repeat among the members
        for leader_index, leader in enumerate(pool):
            team = Team(leader=leader, members=members)
            yield members, leader_index, leader, members, team


# --- Tests 1-2: bit-identical three-way parity ------------------------------


@pytest.mark.parametrize("stacking_key,activation_rate_key", _MODEL_COMBINATIONS)
def test_score_card_set_is_bit_identical_to_score_fast_for_every_leader_of_every_set(
    stacking_key, activation_rate_key
):
    cfg = _cfg_for(stacking_key, activation_rate_key)
    for pool in (_shipped_pool(), _synthetic_pool()):
        for combo, leader_index, leader, members, team in _all_arrangements(pool):
            leader_scores = score_card_set(members, pool, cfg)
            actual = leader_scores[leader_index].total_score
            expected = score_fast(team, cfg)
            assert actual == expected, (
                "D-18: score_card_set must be bit-identical to score_fast, "
                "not merely close, for every leader of every set -- a "
                f"mismatch for leader {leader.id!r} under "
                f"stacking={stacking_key!r} activation_rate={activation_rate_key!r} "
                "means an incremental combine() fold (or some other order "
                "change) crept into the batched path, silently trading a "
                "provable structural guarantee for a numeric one via float "
                "non-associativity"
            )


@pytest.mark.parametrize("stacking_key,activation_rate_key", _MODEL_COMBINATIONS)
def test_score_card_set_is_bit_identical_to_score_with_breakdown_total(
    stacking_key, activation_rate_key
):
    cfg = _cfg_for(stacking_key, activation_rate_key)
    for pool in (_shipped_pool(), _synthetic_pool()):
        for combo, leader_index, leader, members, team in _all_arrangements(pool):
            leader_scores = score_card_set(members, pool, cfg)
            actual = leader_scores[leader_index].total_score
            expected = score_with_breakdown(team, cfg).total_score
            assert actual == expected, (
                "D-18: score_card_set must be bit-identical to "
                "score_with_breakdown().total_score, closing the third "
                f"side of the SCOR-09 triangle -- mismatch for leader "
                f"{leader.id!r} under stacking={stacking_key!r} "
                f"activation_rate={activation_rate_key!r}"
            )


# --- Test 2a-2b: a targeted stat_up effect, and its under-supply edge ------


@pytest.mark.parametrize("stacking_key,activation_rate_key", _MODEL_COMBINATIONS)
def test_targeted_stat_up_parity_is_bit_identical_across_every_leader_arrangement(
    stacking_key, activation_rate_key
):
    """Bit-identical parity extended to a targeted stat_up effect: the
    per-card/per-stat stat-pool arithmetic must stay bit-identical between
    `score_card_set` and both `score_fast`/`score_with_breakdown` even
    when a skill's `target` selects a strict subset of the assembled set,
    and even though which cards qualify shifts with which card each
    6-card combination excludes -- parity here is not an accident of the
    target happening to always select the leader."""
    cfg = _cfg_for(stacking_key, activation_rate_key)
    pool = _synthetic_pool()
    for combo, leader_index, leader, members, team in _all_arrangements(pool):
        leader_scores = score_card_set(members, pool, cfg)
        actual = leader_scores[leader_index].total_score
        assert actual == score_fast(team, cfg), (
            "a targeted stat_up effect must stay bit-identical between "
            f"score_card_set and score_fast for leader {leader.id!r} under "
            f"stacking={stacking_key!r} activation_rate={activation_rate_key!r}"
        )
        assert actual == score_with_breakdown(team, cfg).total_score, (
            "a targeted stat_up effect must stay bit-identical between "
            f"score_card_set and score_with_breakdown().total_score for "
            f"leader {leader.id!r} under stacking={stacking_key!r} "
            f"activation_rate={activation_rate_key!r}"
        )


@pytest.mark.parametrize("stacking_key,activation_rate_key", _MODEL_COMBINATIONS)
def test_under_supplied_target_count_parity_is_bit_identical_across_every_leader_arrangement(
    stacking_key, activation_rate_key
):
    """The under-supply edge (the configured
    `target_selection.under_supply` assumption): a targeted stat_up
    skill's `target.count` (3) permanently exceeds the number of
    qualifying Happy-type cards (at most 2) in every 6-card combination
    drawn from `_undersupplied_pool()`, and `score_card_set` must still
    score bit-identically to `score_fast`/`score_with_breakdown` -- an
    under-supply divergence between the two entry points would be the
    worst possible way to discover a configured assumption disagrees with
    itself."""
    cfg = _cfg_for(stacking_key, activation_rate_key)
    pool = _undersupplied_pool()
    for combo, leader_index, leader, members, team in _all_arrangements(pool):
        leader_scores = score_card_set(members, pool, cfg)
        actual = leader_scores[leader_index].total_score
        assert actual == score_fast(team, cfg), (
            "an under-supplied target must stay bit-identical between "
            f"score_card_set and score_fast for leader {leader.id!r} under "
            f"stacking={stacking_key!r} activation_rate={activation_rate_key!r}"
        )
        assert actual == score_with_breakdown(team, cfg).total_score, (
            "an under-supplied target must stay bit-identical between "
            f"score_card_set and score_with_breakdown().total_score for "
            f"leader {leader.id!r} under stacking={stacking_key!r} "
            f"activation_rate={activation_rate_key!r}"
        )


# --- Test 3: the batched path must never fold combine() incrementally ------


def _find_function(tree: ast.AST, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{SCORING_PATH} must define a {name}() function")


# --- the leader-outfit collapse -------------------------------------------
# `score_card_set` scores one leader per EQUIVALENCE CLASS and expands the
# answer, because a leader contributes nothing but its outfit. A 94-card pool
# wears only 25 distinct outfits, so the per-leader term of the exact search's
# cost — 120,016 s of 126,195 s — collapses with them.
#
# The two tests above already pin the RESULT: they compare every leader of
# every set against `score_fast`, so a wrong merge fails them. These pin the
# two things those cannot see — that the collapse is still HAPPENING (a silent
# regression to one class per leader is invisible in output), and that the
# three identity exceptions still refuse to merge.


def test_leader_classes_collapse_the_pool_rather_than_scoring_every_leader():
    """A regression to one class per leader is bit-identical and therefore
    invisible to every parity test in this file — it just costs 3x.

    Built here rather than taken from `_shipped_pool()`: that fixture is six
    cards, five of which are the member set, so it cannot show a collapse at
    all. The shape below is the real pool's in miniature — several leaders
    sharing one outfit (53 of a real 94-card pool do), plus one wearing another,
    plus one wearing none.
    """
    shared = make_skill("costume", family="stat_up", effect_type="all_stats")
    other = make_skill("costume", family="stat_up", effect_type="performance")
    members = tuple(make_card(f"member_{i}") for i in range(5))
    leaders = (
        *members,
        *(make_card(f"shared_{i}", costume_skill=shared) for i in range(6)),
        make_card("other", costume_skill=other),
        make_card("bare", costume_skill=None),
    )
    member_ids = frozenset(card.id for card in members)
    keys = [_leader_class_key(card, member_ids, frozenset()) for card in leaders]

    assert len(set(keys)) < len(leaders), (
        "the leader collapse is not happening: every candidate leader landed "
        "in its own class, so `score_card_set` is paying the full per-leader "
        "cost the collapse exists to remove"
    )
    # 5 members (each its own class, they never merge) + shared + other + bare.
    assert len(set(keys)) == 5 + 3
    # and the six shared-outfit leaders really are ONE class, not six
    assert len({_leader_class_key(c, member_ids, frozenset())
                for c in leaders if c.id.startswith("shared_")}) == 1


def test_a_leader_that_is_also_a_member_never_merges_with_another_leader():
    """Its own id reaches the score through target re-resolution and through
    `_best_order_stat_sum`'s ordering signature, so it is not interchangeable
    even with a card wearing an identical outfit."""
    costume = make_skill("costume", family="stat_up", effect_type="all_stats")
    inside = make_card("inside", costume_skill=costume)
    outside = make_card("outside", costume_skill=costume)
    assert inside.costume_skill == outside.costume_skill
    member_ids = frozenset({inside.id})
    assert _leader_class_key(inside, member_ids, frozenset()) != _leader_class_key(
        outside, member_ids, frozenset()
    )


def test_leaders_of_a_board_scoped_holomem_never_merge_across_holomem():
    """`BoardModel.leader_scoped_holomem` names the holomem who own a red
    node, and the red branch applies only while its holomem leads. Two cards
    wearing one outfit are interchangeable only OUTSIDE that set."""
    costume = make_skill("costume", family="stat_up", effect_type="all_stats")
    red = make_card("red", costume_skill=costume, member="Scoped Holomem")
    other_red = make_card("red2", costume_skill=costume, member="Scoped Holomem")
    plain = make_card("plain", costume_skill=costume, member="Plain Holomem")
    scoped = frozenset({"Scoped Holomem"})
    empty = frozenset()

    key = lambda card, s: _leader_class_key(card, frozenset(), s)  # noqa: E731
    # inside the scoped set: same holomem merges, different holomem does not
    assert key(red, scoped) == key(other_red, scoped)
    assert key(red, scoped) != key(plain, scoped)
    # with no board configured they all merge (the default path)
    assert key(red, empty) == key(plain, empty) == key(other_red, empty)


def test_a_self_targeted_costume_never_merges():
    """`targets.resolve_target_card_ids` returns `{self_card_id}` for
    `kind == "self"` — "never resolved against `cards` at all" — so the leader's
    own id is in the answer. No shipped card has one; this pins the guard so
    that stays true by test rather than by luck."""
    self_costume = make_skill(
        "costume",
        family="stat_up",
        effect_type="all_stats",
        target=EffectTarget(kind="self", value=None, count=None),
    )
    a = make_card("self_a", costume_skill=self_costume)
    b = make_card("self_b", costume_skill=self_costume)
    assert a.costume_skill == b.costume_skill
    assert _leader_class_key(a, frozenset(), frozenset()) != _leader_class_key(
        b, frozenset(), frozenset()
    )


def test_batched_path_calls_combine_inside_the_per_leader_loop_never_before_it():
    tree = ast.parse(SCORING_PATH.read_text(encoding="utf-8"), filename=str(SCORING_PATH))
    score_card_set_def = _find_function(tree, "score_card_set")

    leader_loops = [
        node
        for node in ast.walk(score_card_set_def)
        if isinstance(node, ast.For)
        and isinstance(node.target, ast.Name)
        and node.target.id == "leader"
    ]
    assert len(leader_loops) == 1, (
        "score_card_set must contain exactly one per-leader loop (`for "
        "leader in cards:`) -- this test identifies it by that loop "
        "variable name to check combine() call containment"
    )
    leader_loop = leader_loops[0]
    loop_descendant_ids = {id(node) for node in ast.walk(leader_loop)}

    combine_calls = [
        node
        for node in ast.walk(score_card_set_def)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "combine"
    ]
    assert combine_calls, "score_card_set must call combine() at least once"
    for call in combine_calls:
        assert id(call) in loop_descendant_ids, (
            "every combine() call inside score_card_set must lie within "
            "the per-leader loop's body -- a combine() call hoisted above "
            "the loop is exactly the incremental fold D-18 forbids: float "
            "addition and multiplication are not associative, so caching a "
            "fold across leaders would silently break bit-identical "
            "SCOR-09 parity"
        )


# --- Test 4: resolve_team_gates must hold no resolution loop of its own ----


def test_resolve_team_gates_delegates_and_holds_no_second_resolution_loop():
    tree = ast.parse(GATES_PATH.read_text(encoding="utf-8"), filename=str(GATES_PATH))
    resolve_team_gates_def = _find_function(tree, "resolve_team_gates")

    call_names = [
        node.func.id
        for node in ast.walk(resolve_team_gates_def)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]
    assert "resolve_card_set_gates" in call_names, (
        "resolve_team_gates must delegate to resolve_card_set_gates (D-17) "
        "rather than re-implementing gate resolution"
    )

    resolve_attr_calls = [
        node
        for node in ast.walk(resolve_team_gates_def)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "resolve"
    ]
    assert resolve_attr_calls == [], (
        "resolve_team_gates must hold no resolution loop of its own -- a "
        "`.resolve(` call here means a future edit reintroduced the "
        "singular per-team assumption D-17 promoted away from, re-opening "
        "the two-co-equal-implementations problem D-17 closed"
    )


# --- Test 5: resolve_card_set_gates is leader-order-independent ------------


def _gate_signature(resolved):
    """`None` covers both "no gate" (an ungated skill) and "no skill at
    all" (a card with no costume skill) -- either way there is nothing to
    compare beyond identity of absence."""
    if resolved is None or resolved.gate is None:
        return None
    return (resolved.gate.fired, resolved.gate.actual_count)


def test_resolve_card_set_gates_is_independent_of_which_card_leads():
    cards = tuple(load_cards(FIXTURE_CARDS_PATH))
    run_state = load_scoring_config().run_state

    baseline = resolve_card_set_gates(cards, run_state)

    for rotation in range(len(cards)):
        rotated = tuple(cards[rotation:] + cards[:rotation])
        result = resolve_card_set_gates(rotated, run_state)

        assert result.aggregates == baseline.aggregates, (
            "D-16: TeamAggregates must be identical regardless of the "
            "order cards are handed to resolve_card_set_gates -- it is a "
            "function of the 6-card set, not the arrangement"
        )

        for card in cards:
            base_normal = [_gate_signature(r) for r in baseline.normal_by_card_id[card.id]]
            rotated_normal = [_gate_signature(r) for r in result.normal_by_card_id[card.id]]
            assert base_normal == rotated_normal, (
                f"D-16: card {card.id!r}'s normal-slot gate outcomes must "
                "not depend on which card is passed first (i.e. which "
                "card would lead)"
            )

            base_costume = baseline.costume_by_card_id[card.id]
            rotated_costume = result.costume_by_card_id[card.id]
            assert _gate_signature(base_costume) == _gate_signature(rotated_costume), (
                f"D-16: card {card.id!r}'s costume gate outcome must not "
                "depend on which card is passed first"
            )


# --- Test 6: a costume slot carrying activation_rate_up must raise --------


def test_a_costume_slot_carrying_activation_rate_up_is_rejected_rather_than_silently_folded():
    # validate_skill_slot rejects family 'activation_rate_up' on any slot
    # but ACTIVE at load time (schema.FAMILY_REQUIRES_SLOT_KIND); building
    # the Card directly, bypassing card_from_record/validate_skill_slot, is
    # exactly how a record could reach score_card_set with this bypassed.
    bad_costume = make_skill(
        "COSTUME",
        family="activation_rate_up",
        effect_type="activation_rate",
        magnitude=10.0,
    )
    leader = make_card("bad_leader", costume_skill=bad_costume)
    members = tuple(make_card(f"bad_member_{i}") for i in range(5))
    cfg = make_scoring_config()

    # The guard now runs over the CANDIDATE LEADERS, which is where a costume
    # slot is actually read — the offending card need not be a member at all.
    with pytest.raises(ScoringError) as exc_info:
        score_card_set(members, (leader,), cfg)

    assert "bad_leader" in str(exc_info.value), (
        "the guard must name the offending card -- this is the fact that "
        "keeps team_rate_up's leader-invariance an asserted guarantee "
        "rather than a silent assumption"
    )


# --- Test 7: contribution ordering is unaffected by the batch refactor ----


def test_contribution_ordering_is_unchanged_by_the_batch_refactor():
    cards = load_cards(FIXTURE_CARDS_PATH)
    team = build_team(cards, "nene_01", _FIXTURE_MEMBER_IDS)
    cfg = load_scoring_config()

    breakdown = score_with_breakdown(team, cfg)
    instances = team.effective_skill_instances()

    assert [(c.card_id, c.slot_kind, c.source) for c in breakdown.contributions] == [
        (i.card_id, i.slot_kind, i.source) for i in instances
    ], (
        "SCOR-09: contribution ordering must still exactly match "
        "Team.effective_skill_instances() order after the D-16/D-17 batch "
        "refactor -- the batched path shares gate resolution and EVs, but "
        "must never reorder the attribution the owner reads"
    )
