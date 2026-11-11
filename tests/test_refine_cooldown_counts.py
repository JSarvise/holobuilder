"""`board_refine.refine_cooldown_counts` — every combination of the members'
activation-frequency (`Act.CD`) node counts, scored on the real score.

Why it exists: a shorter cooldown moves every later window of that member, and the
score at each instant is the strongest live ACTIVE, so dropping a frequency node on
each of two strong members can gain while either drop alone loses. The move-by-move
refinement stops before such a pair; trying every combination of counts does not.
The tests hold it to: finds the joint optimum, never worse, never over budget, never
disconnected, members only."""

from __future__ import annotations

import itertools

from hbuilder.domain.board_optimizer import BoardPlan
from hbuilder.domain.board_plan import TeamBoardPlan
from hbuilder.domain.board_refine import cooldown_node_ids, refine_activation_nodes, refine_cooldown_counts
from hbuilder.domain.skilltree import ROOT_NODE, SkillTree, TreeNode, neighbours

# A line board: S-001 - B-001 - B-013 - B-020 , and a side branch S-001 - G-001 - B-031
#                (x=0)   (x=1)   (x=2)   (x=3)                     (0,1)   (0,2)
POSITIONS = {
    ROOT_NODE: (0, 0),
    "B-001": (1, 0),
    "B-013": (2, 0),
    "B-020": (3, 0),
    "G-001": (0, 1),
    "B-031": (0, 2),
}
COSTS = {ROOT_NODE: 0, "B-001": 3, "B-013": 5, "B-020": 5, "G-001": 2, "B-031": 5}
COOLDOWN = frozenset({"B-013", "B-020", "B-031"})
FULL = {ROOT_NODE, "B-001", "B-013", "B-020", "G-001", "B-031"}


def _tree() -> SkillTree:
    nodes = {n: TreeNode(node_id=n, branch=n[0], cost=c, player_level=None) for n, c in COSTS.items()}
    return SkillTree(nodes=nodes, positions=POSITIONS, adjacency=neighbours(POSITIONS))


TREE = _tree()


class _Card:
    def __init__(self, card_id: str) -> None:
        self.id = card_id


def _plan(selected_by_card: dict[str, set[str]]) -> TeamBoardPlan:
    return TeamBoardPlan(
        total_gain=0.0,
        per_character={
            cid: BoardPlan(
                selected=frozenset(sel), spent=TREE.cost_of(frozenset(sel) - {ROOT_NODE}), value=0.0
            )
            for cid, sel in selected_by_card.items()
        },
    )


def _counts(plan: TeamBoardPlan) -> dict[str, int]:
    return {cid: len(p.selected & COOLDOWN) for cid, p in plan.per_character.items()}


def _run(plan, score, members=("m1", "m2"), budget=30):
    cards = {cid: _Card(cid) for cid in plan.per_character}
    return refine_cooldown_counts(
        plan,
        cards,
        tree_for=lambda _c: TREE,
        budget_for=lambda _c: budget,
        cooldown_nodes=COOLDOWN,
        member_ids=frozenset(members),
        score=score,
    )


def _connected(plan: TeamBoardPlan) -> bool:
    return all(TREE.reachable(frozenset(p.selected)) == p.selected for p in plan.per_character.values())


# Two members at 3 frequency nodes each. Dropping one node on EITHER alone loses;
# dropping one on BOTH gains (their windows stop landing on each other).
def _pair_score(plan: TeamBoardPlan) -> float:
    c = _counts(plan)
    a, b = c["m1"], c["m2"]
    if (a, b) == (2, 2):
        return 110.0
    return 100.0 - 3.0 * ((a != 3) + (b != 3)) - (abs(a - 2) + abs(b - 2) if (a, b) != (3, 3) else 0)


def test_a_pair_no_single_move_reaches_is_found():
    start = _plan({"m1": FULL, "m2": FULL})
    # the move-by-move refinement stops at the start: every single drop loses
    single, single_value, single_moves = refine_activation_nodes(
        start,
        {"m1": _Card("m1"), "m2": _Card("m2")},
        tree_for=lambda _c: TREE,
        budget_for=lambda _c: 30,
        activation_nodes=COOLDOWN,
        score=_pair_score,
    )
    assert single_moves == [] and single_value == 100.0
    plan, value, changes = _run(start, _pair_score)
    assert _counts(plan) == {"m1": 2, "m2": 2}
    assert value == _pair_score(plan) == 110.0
    assert sorted(changes) == ["m1 Act.CD 3->2", "m2 Act.CD 3->2"]
    assert _connected(plan)


def test_the_answer_is_the_best_of_every_combination():
    def score(plan):
        c = _counts(plan)
        return 50.0 + 7 * c["m1"] - 2 * c["m1"] ** 2 + 5 * c["m2"] - c["m2"] ** 2 + c["m1"] * c["m2"] * 0.5

    start = _plan({"m1": FULL, "m2": FULL})
    plan, value, _changes = _run(start, score)
    best = max(
        50.0 + 7 * a - 2 * a * a + 5 * b - b * b + a * b * 0.5
        for a, b in itertools.product(range(4), repeat=2)
    )
    assert value == best == score(plan)


def test_never_worse_and_unchanged_on_ties():
    start = _plan({"m1": FULL, "m2": FULL})
    plan, value, changes = _run(start, lambda _p: 42.0)
    assert plan is start and value == 42.0 and changes == []


def test_a_node_others_hang_from_is_never_dropped():
    # B-013 carries B-020: dropping it alone would cut B-020 off, so the only counts
    # with B-013 gone also drop B-020 (count 1 = B-031 kept, count 0 = none)
    start = _plan({"m1": FULL})
    seen = []

    def score(plan):
        seen.append(plan.per_character["m1"].selected)
        return 1.0

    _run(start, score, members=("m1",))
    assert seen and all(TREE.reachable(s) == s for s in seen)
    assert all(not ("B-020" in s and "B-013" not in s) for s in seen)


def test_an_added_node_brings_its_path_and_respects_the_budget():
    start = _plan({"m1": {ROOT_NODE}})

    def score(plan):
        return float(_counts(plan)["m1"])  # more frequency nodes, higher score

    plan, value, _ = _run(start, score, members=("m1",), budget=30)
    assert _counts(plan)["m1"] == 3 and _connected(plan)
    assert plan.per_character["m1"].spent == TREE.cost_of(
        frozenset(plan.per_character["m1"].selected) - {ROOT_NODE}
    )
    # 3 + 5 (B-001, B-013) = 8 for one node; the full set costs 20
    tight, _v, _ = _run(start, score, members=("m1",), budget=10)
    assert _counts(tight)["m1"] == 1 and tight.per_character["m1"].spent <= 10


def test_only_members_boards_are_tried():
    start = _plan({"leader": FULL, "m1": FULL})
    tried = set()

    def score(plan):
        tried.add(plan.per_character["leader"].selected)
        return float(-_counts(plan)["m1"])

    plan, _value, _ = _run(start, score, members=("m1",))
    assert tried == {frozenset(FULL)}
    assert plan.per_character["leader"].selected == frozenset(FULL)
    assert _counts(plan)["m1"] == 0


def test_cooldown_node_ids_reads_only_the_frequency_family():
    skilltree = {
        "nodes": {
            "B-013": {"e": {"t": "Act.CD"}},
            "B-021": {"e": {"t": "Act.Prob"}},
            "B-001": {"e": {"t": "Stat"}},
            "S-001": {},
        }
    }
    assert cooldown_node_ids(skilltree) == frozenset({"B-013"})


def _screened(start, score, screen_error=None, verify=4, members=("m1", "m2")):
    """The screened path: `cooldown_of` reads each member's count as 0.04 per node,
    `screen` scores the counts it is handed (optionally with an error)."""
    calls = []

    def exact(plan):
        calls.append(plan)
        return score(plan)

    def cooldown_of(plan):
        return {cid: 0.04 * n for cid, n in _counts(plan).items()}

    def screen_one(cds):
        counts = {cid: round(cd / 0.04) for cid, cd in cds.items()}
        fake = _plan({cid: _state_with(counts.get(cid, 3)) for cid in start.per_character})
        v = score(fake)
        return v + (screen_error(counts) if screen_error else 0.0)

    def screen(variants):
        return [screen_one(cds) for cds in variants]

    cards = {cid: _Card(cid) for cid in start.per_character}
    out = refine_cooldown_counts(
        start,
        cards,
        tree_for=lambda _c: TREE,
        budget_for=lambda _c: 30,
        cooldown_nodes=COOLDOWN,
        member_ids=frozenset(members),
        score=exact,
        cooldown_of=cooldown_of,
        screen=screen,
        verify=verify,
    )
    return out, calls


def _state_with(count):
    return {
        0: {ROOT_NODE, "B-001", "G-001"},
        1: {ROOT_NODE, "B-001", "B-013", "G-001"},
        2: {ROOT_NODE, "B-001", "B-013", "B-020", "G-001"},
        3: FULL,
    }[count]


def test_the_screen_picks_and_only_the_best_few_are_scored_exactly():
    start = _plan({"m1": FULL, "m2": FULL})
    (plan, value, _changes), calls = _screened(start, _pair_score, verify=4)
    assert _counts(plan) == {"m1": 2, "m2": 2} and value == 110.0 == _pair_score(plan)
    assert len(calls) <= 4 + 3  # the verified best + the member states read for cooldowns
    assert _connected(plan)


def test_a_screen_error_is_corrected_by_the_exact_scores_it_hands_over():
    # the screen ranks (1, 1) just above the true best (2, 2); both reach the exact score
    start = _plan({"m1": FULL, "m2": FULL})
    (plan, value, _), _calls = _screened(
        start, _pair_score, screen_error=lambda c: 50.0 if c == {"m1": 1, "m2": 1} else 0.0
    )
    assert _counts(plan) == {"m1": 2, "m2": 2} and value == 110.0
