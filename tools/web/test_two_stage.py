"""The two-stage member search (`two_stage.py`) and its compiled screen
(`kernel/surrogate.py`).

The screen is a heuristic and is NOT held to any reference number — only to
what the search relies on: it visits exactly the legal sets, never proposes a
scored one, returns its candidates in its own model's order, and is
deterministic. The search is held to the property that makes it safe: every
score it returns is the exact scorer's, and on pools small enough to brute-force
it finds the true best. Synthetic pools only; the recall measured on the real
enumerations is in `tools/recon/TWO-STAGE.md`.
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

import surrogate  # noqa: E402
import two_stage  # noqa: E402

TYPES = ["Cute", "Cool", "Pure"]
GENERATIONS = ["Myth", "3期生", "holoX", "ReGLOSS", "ID1"]


def _pool(count: int, seed: int, holomems: int | None = None):
    """`count` cards over `holomems` characters (so some characters have two
    skins and the distinct-member rule bites)."""
    rng = random.Random(seed)
    holomems = holomems or max(5, count * 3 // 4)
    return [SimpleNamespace(
        id=f"card-{i:03d}", member=f"Chr{i % holomems}", type=rng.choice(TYPES),
        generations=frozenset(rng.sample(GENERATIONS, rng.choice([1, 1, 2]))),
        strength=rng.uniform(0.6, 1.4)) for i in range(count)]


def _legal(cards):
    return [s for s in itertools.combinations(range(len(cards)), 5)
            if len({cards[i].member for i in s}) == 5]


def _scorer(cards, seed: int):
    """A score with the real one's shape: a product over the cards, gates on
    type / generation counts, pair synergies, and a term no screen feature
    explains — so the model is useful and wrong, as it is on the real leaf."""
    rng = random.Random(seed)
    synergy = {pair: rng.choice([0.0, 0.0, 0.0, 0.08, 0.2])
               for pair in itertools.combinations(range(len(cards)), 2)}
    calls = []

    def score(s) -> float:
        value = 100_000.0
        for i in s:
            value *= cards[i].strength
        for kind in TYPES:
            if sum(cards[i].type == kind for i in s) >= 3:
                value *= 1.25
        for generation in GENERATIONS[:2]:
            if sum(generation in cards[i].generations for i in s) >= 2:
                value *= 1.12
        for pair in itertools.combinations(s, 2):
            value *= 1.0 + synergy[pair]
        value *= 1.0 + 0.03 * math.sin(sum((k + 1) * 7919 * i for k, i in enumerate(s)))
        return float(math.ceil(value))

    def score_sets(sets):
        calls.append(list(sets))
        return [score(s) for s in sets]

    return score, score_sets, calls


def _model_prediction(screen, cards, s) -> float:
    kinds = screen.kinds
    types = sorted({c.type for c in cards})
    generations = sorted({g for c in cards for g in c.generations})
    counts = [0] * kinds
    for i in s:
        counts[types.index(cards[i].type)] += 1
        for g in cards[i].generations:
            counts[len(types) + generations.index(g)] += 1
    value = screen._model.bias + sum(screen._card[i] for i in s)
    value += sum(screen._pair[a * screen.count + b] for a, b in itertools.combinations(s, 2))
    for kind, count in enumerate(counts):
        for threshold in range(2, min(count, 5) + 1):
            value += screen._gate[(threshold - 2) * kinds + kind]
    return value


def test_the_scan_visits_exactly_the_legal_sets_and_never_returns_a_seen_one():
    cards = _pool(22, seed=1, holomems=15)
    legal = _legal(cards)
    assert len(legal) < math.comb(22, 5)          # the distinct-member rule really excludes some
    screen = surrogate.Screen(cards)
    sets, _preds, n_legal = screen.best(len(legal) + 50)
    assert n_legal == len(legal)
    assert sets == legal                          # an unfitted model is flat: lexicographic order
    seen = random.Random(2).sample(legal, 500)
    sets, _preds, _n = screen.best(len(legal), seen)
    assert set(sets) == set(legal) - set(seen) and len(sets) == len(legal) - 500


def test_the_scan_returns_the_models_own_best_sets_in_the_models_own_order():
    cards = _pool(24, seed=3)
    legal = _legal(cards)
    score, _score_sets, _calls = _scorer(cards, seed=4)
    train = random.Random(5).sample(legal, 3000)
    screen = surrogate.Screen(cards)
    mae = screen.fit(train, [math.log(score(s)) for s in train])
    assert 0.0 <= mae < 0.05                      # it does learn the planted structure
    reference = {s: _model_prediction(screen, cards, s) for s in legal}
    want = 400
    sets, preds, _n = screen.best(want, train[:1000])
    assert len(sets) == want and len(set(sets)) == want
    assert not set(sets) & set(train[:1000])
    assert all(a >= b for a, b in zip(preds, preds[1:]))
    assert all(abs(p - reference[s]) < 1e-9 for s, p in zip(sets, preds))
    allowed = sorted((v for s, v in reference.items() if s not in set(train[:1000])), reverse=True)
    assert preds[-1] >= allowed[want - 1] - 1e-9   # nothing better was left out
    # deterministic: the same fit and the same scan give the same answer
    again = surrogate.Screen(cards)
    again.fit(train, [math.log(score(s)) for s in train])
    assert again.best(want, train[:1000])[:2] == (sets, preds)


def test_random_legal_sets_are_legal_distinct_and_respect_the_exclusions():
    cards = _pool(20, seed=6, holomems=12)
    holomems = [c.member for c in cards]
    legal = set(_legal(cards))
    first = two_stage.random_legal_sets(holomems, 300, random.Random(7))
    assert len(first) == len(set(first)) == 300 and set(first) <= legal
    more = two_stage.random_legal_sets(holomems, 300, random.Random(8), exclude=first)
    assert not set(more) & set(first)
    # a pool with fewer legal sets than asked for returns what there is
    tiny = [c.member for c in _pool(6, seed=9, holomems=6)]
    assert len(two_stage.random_legal_sets(tiny, 50, random.Random(1))) == 6


@pytest.mark.parametrize("seed", [1, 2, 3, 4])
def test_the_search_finds_the_true_best_and_reports_only_exact_scores(seed):
    cards = _pool(34, seed=10 + seed)
    legal = _legal(cards)
    score, score_sets, calls = _scorer(cards, seed=20 + seed)
    truth = sorted(((score(s), s) for s in legal), key=lambda r: (-r[0], r[1]))
    result = two_stage.search(cards, score_sets, initial=1500, step=600, max_rounds=12, seed=seed)
    stats = result["stats"]
    assert stats["stop"] == "stable" and stats["legal_sets"] == len(legal)
    assert stats["exact_evals"] < len(legal) / 15            # a search, not an enumeration
    ranking = result["ranking"]
    assert ranking[0] == truth[0]
    found = {s for _v, s in ranking}
    assert sum(s in found for _v, s in truth[:10]) >= 9
    # every reported score is the scorer's own, every set legal and scored exactly once
    assert all(v == score(s) for v, s in ranking)
    asked = [s for batch in calls for s in batch]
    assert len(asked) == len(set(asked)) == stats["exact_evals"] and set(asked) <= set(legal)
    assert [r for r in ranking] == sorted(ranking, key=lambda r: (-r[0], r[1]))


def test_the_search_is_deterministic_scores_its_seeds_and_honours_its_stops():
    cards = _pool(26, seed=31)
    legal = _legal(cards)
    score, score_sets, calls = _scorer(cards, seed=32)
    incumbent = legal[-1]
    a = two_stage.search(cards, score_sets, initial=800, step=300, seed=5, seeds=[incumbent])
    b = two_stage.search(cards, score_sets, initial=800, step=300, seed=5, seeds=[incumbent])
    assert a["ranking"] == b["ranking"] and a["rounds"][-1]["round"] == b["rounds"][-1]["round"]
    assert incumbent in calls[0] and incumbent in {s for _v, s in a["ranking"]}
    assert two_stage.search(cards, score_sets, initial=800, step=300, seed=6)["ranking"][0] == a["ranking"][0]

    capped = two_stage.search(cards, score_sets, initial=800, step=300, max_rounds=1, stable_rounds=99)
    assert capped["stats"]["stop"] == "max_rounds" and capped["stats"]["exact_evals"] == 1100
    late = two_stage.search(cards, score_sets, initial=800, step=300, deadline=0.0)
    assert late["stats"]["stop"] == "deadline" and late["stats"]["exact_evals"] == 800
    # a pool it can exhaust: every legal set ends up scored and the search says so
    small = _pool(9, seed=33, holomems=9)
    small_score, small_sets, _c = _scorer(small, seed=34)
    done = two_stage.search(small, small_sets, initial=40, step=60, stable_rounds=99)
    assert done["stats"]["stop"] == "exhausted"
    assert done["stats"]["exact_evals"] == len(_legal(small)) == math.comb(9, 5)
    assert done["ranking"][0][0] == max(small_score(s) for s in _legal(small))


def test_a_set_the_scorer_values_at_zero_is_kept_out_of_the_fit_not_out_of_the_books():
    cards = _pool(20, seed=41, holomems=20)
    legal = _legal(cards)
    score, _score_sets, _calls = _scorer(cards, seed=42)
    dead = set(random.Random(1).sample(legal, len(legal) // 3))

    def score_sets(sets):
        return [0.0 if s in dead else score(s) for s in sets]

    result = two_stage.search(cards, score_sets, initial=600, step=300, seed=3)
    assert result["ranking"][0][0] == max(score(s) for s in legal if s not in dead)
    assert any(v == 0.0 for v, _s in result["ranking"])


# --- search_song: the best (leader, members) without a search per leader ------

def _leaders(cards, seed: int):
    """Leaders with the three ways a real one moves the score: a flat outfit
    factor, an outfit gated on the members' type count, and a `sings` factor."""
    rng = random.Random(seed)
    out = []
    for i, card in enumerate(cards):
        gate = ("type", rng.choice(TYPES), 3) if rng.random() < 0.4 else None
        out.append(SimpleNamespace(id=f"lead-{i:03d}", member=card.member, gate=gate,
                                   flat=rng.uniform(1.0, 1.05), gated=rng.uniform(1.05, 1.2),
                                   sings=1.0))
    return out


def _leader_score(cards, score):
    def of(leader, s) -> float:
        value = score(s) * leader.flat * leader.sings
        if leader.gate and sum(cards[i].type == leader.gate[1] for i in s) >= leader.gate[2]:
            value *= leader.gated
        if any(cards[i].member == leader.member for i in s):
            value *= 1.03                              # leader also a member
        return float(math.ceil(value))
    return of


def test_search_song_finds_the_best_leader_and_labels_how_each_row_was_obtained():
    cards = _pool(30, seed=51)
    legal = _legal(cards)
    score, _sets, _calls = _scorer(cards, seed=52)
    leaders = _leaders(cards, seed=53)
    for singer in leaders[4:6]:
        singer.sings = 1.15                            # two leaders sing this song
    of = _leader_score(cards, score)
    truth = {l.id: max(of(l, s) for s in legal) for l in leaders}
    base = SimpleNamespace(id="bare", member="Nobody", gate=None, flat=1.0, gated=1.0, sings=1.0)
    result = two_stage.search_song(
        cards, leaders, lambda leader, sets: [of(leader, s) for s in sets],
        base_leader=base, always=leaders[4:6], gate_of=lambda l: l.gate,
        initial=1500, step=600, triage_top=150, tier=60)
    rows = result["rows"]
    best = max(truth.values())
    assert rows[0]["best"] == best and truth[rows[0]["leader"].id] == best
    assert [r["best"] for r in rows] == sorted((r["best"] for r in rows), reverse=True)
    searched = [r for r in rows if r["status"] == "searched"]
    assert {l.id for l in leaders[4:6]} <= {r["leader"].id for r in searched}
    assert 2 <= len(searched) <= 12 and result["stats"]["searched"] == len(searched)
    for row in rows:
        # every number is an exact score of the set it names, and never above the truth
        assert row["best"] == of(row["leader"], row["members"]) <= truth[row["leader"].id]
        assert row["status"] in ("searched", "triage") and row["triage_best"] <= row["best"]
    assert sum(r["best"] == truth[r["leader"].id] for r in searched) >= len(searched) - 1


def test_search_song_caps_a_flat_field_and_keeps_triage_rows_past_the_deadline():
    cards = _pool(26, seed=61)
    score, _sets, _calls = _scorer(cards, seed=62)
    leaders = _leaders(cards, seed=63)
    of = _leader_score(cards, score)
    base = SimpleNamespace(id="bare", member="Nobody", gate=None, flat=1.0, gated=1.0, sings=1.0)

    def run(**options):
        return two_stage.search_song(
            cards, leaders, lambda leader, sets: [of(leader, s) for s in sets],
            base_leader=base, gate_of=lambda l: l.gate, initial=800, step=300,
            triage_top=100, tier=40, **options)

    wide = run(margin=1.0, max_searched=3)             # everyone is "within margin"
    assert wide["stats"]["within_margin"] == len(leaders) and wide["stats"]["searched"] == 3
    forced = run(margin=0.0, max_searched=1, always=leaders[:4])
    assert {r["leader"].id for r in forced["rows"] if r["status"] == "searched"} >=         {l.id for l in leaders[:4]}                    # `always` is never cut by the cap
    late = run(margin=1.0, deadline=0.0)
    assert late["stats"]["searched"] == 0 and late["stats"]["not_reached"] == 12
    assert all(r["status"] == "triage" for r in late["rows"])


def test_a_bad_seed_or_a_non_finite_score_is_refused():
    cards = _pool(20, seed=71, holomems=12)
    score, score_sets, _calls = _scorer(cards, seed=72)
    twin = next(s for s in itertools.combinations(range(20), 5)
                if len({cards[i].member for i in s}) < 5)
    for bad in ([(0, 1, 2, 3, 99)], [(0, 1, 2, 3)], [twin], [(0, 0, 1, 2, 3)]):
        with pytest.raises(ValueError, match="not a legal member set"):
            two_stage.search(cards, score_sets, initial=200, step=100, seeds=bad)
    for poison in (float("nan"), float("inf")):
        with pytest.raises(ValueError, match="the scorer returned"):
            two_stage.search(cards, lambda sets, poison=poison: [poison] * len(sets),
                             initial=200, step=100)


def test_neighbours_reach_a_set_the_screen_cannot_explain():
    """A needle: one set scored far above anything its cards, gates and pairs
    predict. The screen never proposes it; the one-swap expansion of the exact
    leaders does, when the needle is one swap from the true best."""
    cards = _pool(34, seed=81, holomems=34)
    legal = _legal(cards)
    score, _sets, _calls = _scorer(cards, seed=82)
    best = max(legal, key=score)
    # the WORST one-swap neighbour of the best: as far down the screen's order
    # as a set next to the best can be
    needle = min((s for s in legal if len(set(s) & set(best)) == 4), key=score)
    assert score(needle) < 0.7 * score(best)
    top = 1.05 * score(best)

    def score_sets(sets):
        return [top if s == needle else score(s) for s in sets]

    plain = two_stage.search(cards, score_sets, initial=800, step=300, seed=1)
    local = two_stage.search(cards, score_sets, initial=800, step=300, seed=1, neighbours=5)
    assert plain["ranking"][0][1] != needle            # the screen alone misses it
    assert local["ranking"][0] == (top, needle)
    assert local["stats"]["neighbour_evals"] > 0 and plain["stats"]["neighbour_evals"] == 0


def test_the_net_gives_every_card_its_share_of_proposals_and_off_changes_nothing():
    """`net` = a per-card quota on what the screen proposes each round. Off (the
    default) the search is the one it was; on, every round's batch holds at
    least `net` sets for every card, the extra ones counted in the stats."""
    cards = _pool(34, seed=91, holomems=34)
    score, _sets, _calls = _scorer(cards, seed=92)
    batches: list[list] = []

    def score_sets(sets):
        batches.append(list(sets))
        return [score(s) for s in sets]

    plain = two_stage.search(cards, score_sets, initial=400, step=60, seed=3, max_rounds=3)
    again = two_stage.search(cards, score_sets, initial=400, step=60, seed=3, max_rounds=3, net=0)
    assert plain["ranking"] == again["ranking"]
    assert plain["stats"]["net_evals"] == 0
    assert any(min(sum(c in s for s in batch) for c in range(len(cards))) == 0
               for batch in batches[1:4])               # the screen alone leaves cards out
    batches.clear()
    wide = two_stage.search(cards, score_sets, initial=400, step=60, seed=3, max_rounds=3, net=4)
    for batch in batches[1:]:
        assert len(batch) == len(set(batch)) > 60
        assert min(sum(c in s for s in batch) for c in range(len(cards))) >= 4
    assert wide["stats"]["net_evals"] == sum(len(b) - 60 for b in batches[1:]) > 0
    assert all(value == score(members) for value, members in wide["ranking"])


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_the_event_objective_inside_the_search_finds_the_bonus_team_a_rerank_misses(seed):
    """Event Points: bonus cards are weak by score, so the best EP team sits far down
    the score ranking. Re-sorting the score search's list cannot reach it; ranking BY
    the objective inside the search (`objective_scorer`) does."""
    cards = _pool(34, seed=40 + seed)
    for k, c in enumerate(cards):
        if k % 5 == 0:                                   # every fifth card: weak, +500 live
            c.strength = 0.75
    bonus = [(500 if k % 5 == 0 else 0, 0) for k in range(len(cards))]
    legal = _legal(cards)
    score, _score_sets, _calls = _scorer(cards, seed=50 + seed)
    plain = lambda _leader, sets: [score(s) for s in sets]          # noqa: E731
    objective = two_stage.objective_scorer(plain, bonus, "event_points")
    truth = max(legal, key=lambda s: (objective(None, [s])[0], s))

    by_score = two_stage.search(cards, lambda sets: plain(None, sets), initial=1500, step=600,
                                max_rounds=12, seed=seed)
    reranked = max((s for _v, s in by_score["ranking"][:200]),
                   key=lambda s: objective(None, [s])[0])
    assert reranked != truth                             # the post-hoc rerank misses it

    found = two_stage.search(cards, lambda sets: objective(None, sets), initial=1500, step=600,
                             max_rounds=12, seed=seed)
    assert found["ranking"][0][1] == truth
    assert found["ranking"][0][0] == pytest.approx(objective(None, [truth])[0])


def test_the_score_objective_passes_the_scores_through():
    wrapped = two_stage.objective_scorer(lambda _l, sets: [7.0 for _ in sets], [(900, 100)] * 5, "score")
    assert wrapped(None, [(0, 1, 2, 3, 4)]) == [7.0]
    song = two_stage.objective_scorer(lambda _l, sets: [1000.0 for _ in sets],
                                      [(0, 0), (0, 100), (0, 50), (0, 0), (0, 0)], "event_song")
    assert song(None, [(0, 1, 2, 3, 4)]) == [pytest.approx(1100.0)]       # the MAX card, not the sum


def test_fitted_chart_sample_picks_the_charts_that_track_every_teams_full_mean():
    # 12 charts: three "typical" ones (each team scores its own mean there) and nine
    # whose score swings with the team (a singer song: +-30 % depending on the team)
    rng = random.Random(4)
    scores = []
    for _ in range(40):
        base = rng.uniform(1e6, 2e6)
        swing = [base * rng.uniform(0.7, 1.3) for _ in range(9)]
        typical = sum(swing) / 9
        scores.append(swing[:4] + [typical] + swing[4:7] + [typical, typical] + swing[7:])
    assert sorted(two_stage.fitted_chart_sample(scores, 3)) == [4, 8, 9]
    # deterministic, never repeats a chart, and every chart when asked for as many
    assert two_stage.fitted_chart_sample(scores, 5) == two_stage.fitted_chart_sample(scores, 5)
    assert len(set(two_stage.fitted_chart_sample(scores, 7))) == 7
    assert two_stage.fitted_chart_sample(scores, 12) == list(range(12))
    with pytest.raises(ValueError):
        two_stage.fitted_chart_sample([[1.0, 2.0], [1.0]], 1)
