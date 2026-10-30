""""In general" — the best team over many songs (`two_stage.chart_sample`,
`two_stage.search_general`) — and the leaf it stands on: one pool scoring a
chosen subset of its charts with the board rebuilt per song
(`RefineContext.board_per_chart`, `RefinePool.batch_score(chart_indices=)`).

The synthetic tests hold the property that makes the sample safe to use: it
only decides which candidates exist; every reported score is the full
objective's. The real-data test holds the leaf to `==` against single-song
contexts."""
from __future__ import annotations

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

import two_stage  # noqa: E402
from test_two_stage import _leader_score, _leaders, _legal, _pool, _scorer  # noqa: E402


def test_chart_sample_is_proportional_spread_and_deterministic():
    charts = ([SimpleNamespace(music_id=f"s{i:03d}", singer_type="solo", notes=[0] * (300 + 7 * i))
               for i in range(158)]
              + [SimpleNamespace(music_id=f"g{i:03d}", singer_type="group", notes=[0] * (400 + 11 * i))
                 for i in range(43)]
              + [SimpleNamespace(music_id=f"n{i:03d}", singer_type=None, notes=[0] * (200 + i))
                 for i in range(14)])
    random.Random(3).shuffle(charts)
    sample = two_stage.chart_sample(charts, 16)
    kinds = [c.singer_type for c in sample]
    assert len(sample) == len({c.music_id for c in sample}) == 16
    assert (kinds.count("solo"), kinds.count("group"), kinds.count(None)) == (12, 3, 1)
    solo = sorted(len(c.notes) for c in sample if c.singer_type == "solo")
    assert solo[0] < 300 + 7 * 20 and solo[-1] > 300 + 7 * 137     # both ends of the range
    again = list(charts)
    random.Random(4).shuffle(again)
    assert [c.music_id for c in two_stage.chart_sample(again, 16)] == [c.music_id for c in sample]
    assert two_stage.chart_sample(charts[:5], 16) == charts[:5]     # nothing to sample


def test_search_general_reports_only_full_scores_and_scores_the_extra_candidates():
    cards = _pool(26, seed=91)
    legal = _legal(cards)
    score, _sets, _calls = _scorer(cards, seed=92)
    other, _s2, _c2 = _scorer(cards, seed=93)
    leaders = _leaders(cards, seed=94)
    of = _leader_score(cards, score)
    of_other = _leader_score(cards, other)
    base = SimpleNamespace(id="bare", member="Nobody", gate=None, flat=1.0, gated=1.0, sings=1.0)

    def full(leader, s) -> float:                     # two "songs": the mean of both
        return (of(leader, s) + of_other(leader, s)) / 2.0

    full_calls = []

    def score_full(leader, sets):
        full_calls.append((leader.id, list(sets)))
        return [full(leader, s) for s in sets]

    planted = (leaders[3], legal[-1])
    result = two_stage.search_general(
        cards, leaders, lambda leader, sets: [of(leader, s) for s in sets],   # the sample: song 1 only
        score_full, base_leader=base, gate_of=lambda leader: leader.gate, keep=40, extra=[planted],
        initial=800, step=300, triage_top=100, tier=40, max_searched=4)
    rows = result["rows"]
    assert len(rows) == len(leaders)
    assert [r["best"] for r in rows] == sorted((r["best"] for r in rows), reverse=True)
    for row in rows:
        assert row["best"] == full(row["leader"], row["members"])            # never the sample's number
    assert all(value == full(leader, members) for value, leader, members in result["ranking"])
    assert (full(*planted), planted[0], planted[1]) in result["ranking"]     # `extra` was scored
    scored = {(leader_id, s) for leader_id, sets in full_calls for s in sets}
    assert len(scored) == result["stats"]["full_evals"] == len(result["ranking"])
    agreement = result["stats"]["sample_agreement"]
    # the sample is ONE of the two songs: how well it ranked the mean is reported, not assumed
    assert agreement["candidates"] > 40 and agreement["rank_of_full_best_in_sample_order"] >= 1
    assert agreement["sample_best_over_full_best"] <= 1.0


def test_one_pool_scores_a_chosen_song_exactly_as_that_songs_own_context_does():
    """`batch_score(chart_indices=)` on a many-chart context with the board
    rebuilt per song, against a context holding just that chart; and the
    many-chart mean against the mean of the single-song scores."""
    import argparse
    import dataclasses
    import importlib.util

    repo = HERE.parents[1]
    proxy_dir = repo / "tools" / "recon" / "proxy"
    if str(proxy_dir) not in sys.path:
        sys.path.insert(0, str(proxy_dir))
    try:
        spec = importlib.util.spec_from_file_location(
            "_mfp_two_stage_general_test", repo / "tools/recon/probes/members_first_2026_08_19.py")
        mfp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mfp)
        built = mfp.build(argparse.Namespace(song="m0334", budget=453, uptime="finite",
                                             platform="mobile"), mfp.PROXIES["nonsinger"])
        library = built["ex"].load_charts(str(built["ex"].CHARTS_PATH))
        charts = tuple(c for c in (library.get(m) for m in ("m0334", "m0144", "m0001")) if c)
    except Exception as exc:  # noqa: BLE001 - no dumps on this machine
        pytest.skip(f"the real inputs are not available here: {exc!r}")
    if len(charts) != 3:
        pytest.skip("the three charts this test uses are not all in the library")
    from hbuilder.domain.team import Team
    from hbuilder.solver import parallel_refine as pr

    reps, proxy = built["reps"], built["proxy"]
    rng = random.Random(7)
    singer = next(c for c in reps if "chr-" + c.id.split("-")[1] in charts[0].singers)
    teams = []
    while len(teams) < 24:
        picked = rng.sample(reps, 5)
        if len({c.member for c in picked}) == 5:
            teams.append(Team(leader=rng.choice([proxy, singer, picked[0]]), members=tuple(picked)))
    assert any(t.leader is singer for t in teams)     # a leader in a different singer context per song
    many = pr.RefinePool(dataclasses.replace(built["ctx"], charts=charts, board_per_chart=True),
                         workers=1)
    alone = [pr.RefinePool(dataclasses.replace(built["ctx"], charts=(chart,)), workers=1).batch_score(teams)
             for chart in charts]
    for k in range(3):
        assert many.batch_score(teams, chart_indices=(k,)) == alone[k]
    means = [(alone[0][t] + alone[1][t] + alone[2][t]) / 3 for t in range(len(teams))]
    assert many.batch_score(teams) == means
    assert many.batch_score(teams, chart_indices=(0, 1, 2)) == means
    # without the flag the same context is the plain across objective: one board, sings=False
    old = pr.RefinePool(dataclasses.replace(built["ctx"], charts=charts), workers=1).batch_score(teams)
    assert old != means


def test_per_chart_scores_are_the_charts_own_and_average_to_the_batch_score():
    """`batch_score_per_chart`: each chart's score, in chart order, the same floats
    a one-chart selection gives, and their mean is exactly `batch_score`'s."""
    import argparse
    import dataclasses
    import importlib.util

    repo = HERE.parents[1]
    proxy_dir = repo / "tools" / "recon" / "proxy"
    if str(proxy_dir) not in sys.path:
        sys.path.insert(0, str(proxy_dir))
    try:
        spec = importlib.util.spec_from_file_location(
            "_mfp_two_stage_general_test2", repo / "tools/recon/probes/members_first_2026_08_19.py")
        mfp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mfp)
        built = mfp.build(argparse.Namespace(song="m0334", budget=453, uptime="finite",
                                             platform="mobile"), mfp.PROXIES["nonsinger"])
        library = built["ex"].load_charts(str(built["ex"].CHARTS_PATH))
        charts = tuple(c for c in (library.get(m) for m in ("m0334", "m0144", "m0001")) if c)
    except Exception as exc:  # noqa: BLE001 - no dumps on this machine
        pytest.skip(f"the real inputs are not available here: {exc!r}")
    if len(charts) != 3:
        pytest.skip("the three charts this test uses are not all in the library")
    from hbuilder.domain.team import Team
    from hbuilder.solver import parallel_refine as pr

    reps = built["reps"]
    rng = random.Random(11)
    teams = []
    while len(teams) < 12:
        picked = rng.sample(reps, 6)
        if len({c.member for c in picked}) == 6:
            teams.append(Team(leader=picked[0], members=tuple(picked[1:])))
    ctx = dataclasses.replace(built["ctx"], charts=charts, board_per_chart=True)
    one = pr.RefinePool(ctx, workers=1)
    per = one.batch_score_per_chart(teams)
    assert [len(v) for v in per] == [3] * len(teams)
    for k in range(3):
        assert [v[k] for v in per] == one.batch_score(teams, chart_indices=(k,))
    assert [sum(v) / 3 for v in per] == one.batch_score(teams)
    with pr.RefinePool(ctx, workers=2) as two:              # the worker path: the same floats
        assert two.batch_score_per_chart(teams) == per
