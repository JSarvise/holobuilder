"""The joint (leader, members) search (`joint_search.py`).

Held to what makes it safe and useful, on synthetic pools small enough to
brute-force every (leader, set) pair: every score it returns is the exact
scorer's, it finds the true best team, the filters (`require`, `allowed`, a
pinned leader list) hold for every reported team, and it is deterministic.
Recall on a real account (agreement with `two_stage.search_song`) needs real
data and is not tested here.
"""
from __future__ import annotations

import itertools
import math
import random
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
for extra in (HERE, HERE / "kernel"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

pytestmark = pytest.mark.skipif(shutil.which("gcc") is None, reason="no gcc to build the screen")

import joint_search  # noqa: E402

TYPES = ["Cute", "Cool", "Pure"]
GENERATIONS = ["Myth", "3期生", "holoX", "ReGLOSS", "ID1"]


def _world(count: int, seed: int):
    """Cards (some characters with two skins), a gate per card, and an exact
    score of (leader, set): a product over the members, type/generation gates,
    pair synergies, the leader's gated boost, a leader-character x set term no
    model feature explains, and noise."""
    rng = random.Random(seed)
    holomems = max(6, count * 3 // 4)
    cards = [SimpleNamespace(
        id=f"card-{i:03d}", member=f"Chr{i % holomems}", type=rng.choice(TYPES),
        generations=frozenset(rng.sample(GENERATIONS, rng.choice([1, 1, 2]))),
        strength=rng.uniform(0.6, 1.4), boost=rng.uniform(0.0, 0.3),
        gate=rng.choice([None, ("type", rng.choice(TYPES), 3),
                         ("generation", rng.choice(GENERATIONS), 2)]))
        for i in range(count)]
    synergy = {p: rng.choice([0.0, 0.0, 0.0, 0.08, 0.2])
               for p in itertools.combinations(range(count), 2)}
    affinity = {(li, i): rng.uniform(-0.04, 0.04) for li in range(count) for i in range(count)}

    def fires(gate, s):
        kind, target, need = gate
        if kind == "type":
            return sum(cards[i].type == target for i in s) >= need
        return sum(target in cards[i].generations for i in s) >= need

    def exact(li: int, s) -> float:
        value = 100_000.0
        for i in s:
            value *= cards[i].strength
        for kind in TYPES:
            if sum(cards[i].type == kind for i in s) >= 3:
                value *= 1.25
        for pair in itertools.combinations(s, 2):
            value *= 1.0 + synergy[pair]
        leader = cards[li]
        if leader.gate is None or fires(leader.gate, s):
            value *= 1.0 + leader.boost
        for i in s:
            value *= 1.0 + affinity[(li, i)]
        value *= 1.0 + 0.02 * math.sin(li * 31 + sum((k + 1) * 7919 * i for k, i in enumerate(s)))
        return float(math.ceil(value))

    calls: list[list] = []

    def score_pairs(pairs):
        calls.append(list(pairs))
        return [exact(li, s) for li, s in pairs]

    legal = [s for s in itertools.combinations(range(count), 5)
             if len({cards[i].member for i in s}) == 5]
    return cards, exact, score_pairs, legal, calls


def _gate_of(card):
    return card.gate


def _truth(cards, exact, legal, leaders, ok=lambda li, s: True):
    return max((exact(li, s), li, s) for li in leaders for s in legal if ok(li, s))


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_finds_the_true_best_team_and_every_score_is_exact(seed):
    cards, exact, score_pairs, legal, _calls = _world(18, seed)
    out = joint_search.search(cards, cards, score_pairs, gate_of=_gate_of, seed=seed,
                              initial_sets=300, step=200, polish=10)
    best_value, best_li, best_set = _truth(cards, exact, legal, range(len(cards)))
    assert out["ranking"][0][0] == best_value
    assert (out["ranking"][0][1], out["ranking"][0][2]) == (best_li, best_set)
    for value, li, s in out["ranking"]:
        assert value == exact(li, s)
        assert len({cards[i].member for i in s}) == 5
    # far fewer than every pair
    assert out["stats"]["exact_evals"] < 0.5 * len(legal) * len(cards)


def test_required_cards_are_in_every_reported_team_and_the_best_of_them_is_found():
    cards, exact, score_pairs, legal, _calls = _world(18, 4)
    require = (2, 7)
    out = joint_search.search(cards, cards, score_pairs, gate_of=_gate_of, require=require,
                              initial_sets=300, step=200, polish=10)
    assert all(set(require) <= set(s) for _v, _li, s in out["ranking"])
    best = _truth(cards, exact, legal, range(len(cards)), lambda li, s: set(require) <= set(s))
    assert out["ranking"][0][0] == best[0]


def test_allowed_filter_and_a_pinned_leader_list():
    cards, exact, score_pairs, legal, _calls = _world(18, 5)

    def no_overlap(li, s):
        return all(cards[i].member != cards[li].member for i in s)

    out = joint_search.search(cards, cards, score_pairs, gate_of=_gate_of, allowed=no_overlap,
                              initial_sets=300, step=200, polish=10)
    assert all(no_overlap(li, s) for _v, li, s in out["ranking"])
    assert out["ranking"][0][0] == _truth(cards, exact, legal, range(len(cards)), no_overlap)[0]

    pinned = [cards[3]]                                   # one leader: index 0 of `leaders`
    out = joint_search.search(cards, pinned, lambda pairs: [exact(3, s) for _li, s in pairs],
                              gate_of=_gate_of, initial_sets=300, step=200, polish=10)
    assert {li for _v, li, _s in out["ranking"]} == {0}
    assert out["ranking"][0][0] == max(exact(3, s) for s in legal)


def test_deterministic_and_scores_each_pair_once():
    cards, _exact, score_pairs, _legal, calls = _world(16, 6)
    a = joint_search.search(cards, cards, score_pairs, gate_of=_gate_of, seed=9,
                            initial_sets=200, step=150, polish=5)
    pairs = [p for batch in calls for p in batch]
    assert len(pairs) == len(set(pairs)) == a["stats"]["exact_evals"]
    b = joint_search.search(cards, cards, score_pairs, gate_of=_gate_of, seed=9,
                            initial_sets=200, step=150, polish=5)
    assert a["ranking"] == b["ranking"]


def test_a_non_finite_score_is_refused():
    cards, _exact, _score_pairs, _legal, _calls = _world(12, 7)
    with pytest.raises(ValueError):
        joint_search.search(cards, cards, lambda pairs: [float("nan")] * len(pairs),
                            gate_of=_gate_of, initial_sets=50, step=50)


@pytest.mark.parametrize("count, seed", [(20, 8), (24, 15), (28, 2), (28, 5), (28, 12)])
def test_every_set_among_the_best_teams_was_scored_under_every_leader(count, seed):
    # the climb's swaps keep the leader; a set it reaches must still be tried under
    # every leader, or the served team can carry the wrong one (e.g. a set served
    # under a leader 0.75 % worse than its best). A short search, so the climb is
    # what finds the best sets: each of these seeds exposes the failure
    cards, _exact, score_pairs, _legal, _calls = _world(count, seed)
    polish = 4
    out = joint_search.search(cards, cards, score_pairs, gate_of=_gate_of, seed=seed,
                              initial_sets=60, step=40, polish=polish, max_rounds=2)
    leaders_of: dict = {}
    for _v, li, s in out["ranking"]:
        leaders_of.setdefault(s, set()).add(li)
    for _v, _li, s in out["ranking"][:polish]:
        assert leaders_of[s] == set(range(len(cards))), s


def test_climb_scores_exactly_improves_on_its_start_and_tries_every_leader_on_its_best_sets():
    cards, exact, score_pairs, legal, _calls = _world(20, 9)
    rng = random.Random(9)
    start = [(rng.randrange(len(cards)), s) for s in rng.sample(legal, 6)]
    out = joint_search.climb(cards, cards, score_pairs, start, neighbours=3, polish=4, rounds=10)
    ranking = out["ranking"]
    assert all(v == exact(li, s) for v, li, s in ranking)
    assert ranking == sorted(ranking, key=lambda r: (-r[0], r[1], r[2]))
    assert ranking[0][0] > max(exact(li, s) for li, s in start)
    leaders_of: dict = {}
    for _v, li, s in ranking:
        leaders_of.setdefault(s, set()).add(li)
    for _v, _li, s in ranking[:4]:
        assert leaders_of[s] == set(range(len(cards)))
    # the filters hold for every reported pair
    out = joint_search.climb(cards, cards, score_pairs, start, require=(start[0][1][0],),
                             neighbours=3, polish=4)
    assert all(start[0][1][0] in s for _v, _li, s in out["ranking"])


@pytest.mark.parametrize("count, seed", [(20, 2), (20, 24), (24, 23), (24, 24)])
def test_a_gated_leader_is_tried_where_its_gate_would_fire(count, seed):
    # a leader whose outfit needs 2 cards of a RARE generation and then boosts a lot:
    # random sets almost never fire it, so the model learns it as weak, and a one-card
    # swap never fires it. The gate repair scores the best sets made to fire it
    # (on real data such a leader can lead very few scored teams and still own the
    # true #1). Each case fails without the repair
    cards, exact, score_pairs, legal, _calls = _world(count, seed)
    for i in (count - 1, count - 2):
        cards[i].generations = frozenset({"RARE"})
    cards[0].gate, cards[0].boost = ("generation", "RARE", 2), 0.9
    out = joint_search.search(cards, cards, score_pairs, gate_of=_gate_of, seed=seed,
                              initial_sets=60, step=40, polish=4, max_rounds=2, gate_samples=0)
    assert out["ranking"][0][0] == _truth(cards, exact, legal, range(len(cards)))[0]


def test_paired_swaps_reach_a_team_two_swaps_away_that_no_single_swap_improves():
    # cards 8 and 9 pay only together (each alone costs): a one-swap climb from
    # (0,1,2,3,4) never takes either step. Real teams show it too: swapping two cards at
    # once (3,959,021 -> 3,991,755) where neither single swap helps
    cards = [SimpleNamespace(id=f"c{i}", member=f"H{i}") for i in range(12)]
    leaders = cards[:1]

    def value(s):
        v = 100.0 + sum(0.1 * i for i in s if i < 8)
        if 8 in s and 9 in s:
            v += 30.0
        elif 8 in s or 9 in s:
            v -= 0.01
        return v

    def score_pairs(pairs):
        return [value(s) for _li, s in pairs]

    start = [(0, (0, 1, 2, 3, 4))]
    plain = joint_search.climb(cards, leaders, score_pairs, start, neighbours=1, polish=1)
    assert not {8, 9} <= set(plain["ranking"][0][2])
    paired = joint_search.climb(cards, leaders, score_pairs, start, neighbours=1, polish=1,
                                pair_moves=20)
    best = paired["ranking"][0]
    assert {8, 9} <= set(best[2]) and best[0] == max(value(s) for s in itertools.combinations(range(12), 5))
