"""`solver/parallel_refine.RefinePool`: the refine's exact stage across
processes must return the SAME floats the in-process path computes, in order,
and fall back to serial when the pool cannot stand up.

Uses the real dumps (skilltree + master + charts, gitignored) and SKIPS when
they are absent — the same first-class-absence contract the develop routes
have. Small on purpose: three teams, two workers, one chart."""
from __future__ import annotations

import dataclasses

import pytest

from hbweb import config
from hbuilder.inputs import apply_collection, load_inputs
from hbuilder.domain.board_plan import node_effect_ids_from_master
from hbuilder.domain.song import load_charts
from hbuilder.domain.team import Team
from hbuilder.solver.parallel_refine import RefineContext, RefinePool


def _context():
    skilltree = config.read_json(config.SKILLTREE_PATH)
    node_master = config.read_json(config.NODE_MASTER_PATH)
    library = load_charts(str(config.CHARTS_PATH))
    if not skilltree or not node_master or library is None:
        pytest.skip("skilltree/master/charts dumps absent (gitignored)")
    chart = next((library.get(m) for m in library.music_ids), None)
    if chart is None:
        pytest.skip("no chart in the library")
    cards, owned, cfg, _ = load_inputs(
        config.CARDS_PATH, config.OWNED_PATH)
    cards, cb = apply_collection(
        cards, None, owned_ids=[c.id for c in owned], maxed=True)
    if cb.bonuses is not None:
        cfg = dataclasses.replace(cfg, account=cb.bonuses)
    ctx = RefineContext(
        cards=tuple(cards), cfg=dataclasses.replace(cfg, board=None),
        charts=(chart,), skilltree=skilltree, node_master=node_master,
        node_effect_ids=node_effect_ids_from_master(node_master),
        connect_cards=None, bench_characters=None,
        name_by_chr={"chr-" + c.id.split("-")[1]: c.member for c in cards},
        flat_budget=453, granted={})
    by_id = {c.id: c for c in cards}
    five = [c for c in cards if c.rarity.startswith("5")]
    teams = []
    for k in range(3):
        leader = five[k]
        members = []
        for c in five[k + 1:]:
            if c.member != leader.member and c.member not in {m.member for m in members}:
                members.append(c)
            if len(members) == 5:
                break
        teams.append(Team(leader=leader, members=tuple(members)))
    return ctx, teams


def test_pool_scores_match_the_serial_path_in_order():
    ctx, teams = _context()
    with RefinePool(ctx, workers=1) as serial:
        expect = serial.batch_score(teams)
    with RefinePool(ctx, workers=2) as pool:
        assert pool._pool is not None      # the pool really stood up
        got = pool.batch_score(teams)
    assert len(got) == len(teams) == 3
    assert got == expect            # same floats, same order — not approx
    assert all(v > 0 for v in got)


def test_pool_that_cannot_start_falls_back_to_serial(monkeypatch):
    ctx, teams = _context()
    import multiprocessing

    def boom(*_a, **_k):
        raise RuntimeError("no pool for you")

    monkeypatch.setattr(multiprocessing, "get_context", boom)
    notes = []
    with RefinePool(ctx, workers=4, on_fallback=notes.append) as pool:
        got = pool.batch_score(teams)
    with RefinePool(ctx, workers=1) as serial:
        expect = serial.batch_score(teams)
    assert got == expect
    assert notes and "failed to start" in notes[0]


@pytest.mark.parametrize("use_cache", [False, True])
@pytest.mark.parametrize("refine", [False, True])
def test_develop_team_board_scores_to_the_leaf_float(use_cache, refine):
    """`develop_team` (the DETAIL path: board, plan, sockets) must be the board
    behind `evaluate`'s float, or the detail view shows another number than the
    row it explains."""
    from hbuilder.domain.song_rank import score_team_on_chart
    from hbuilder.solver.parallel_refine import develop_team, evaluate, new_plan_cache

    ctx, teams = _context()
    team = teams[0]
    if use_cache:
        ctx = dataclasses.replace(ctx, use_plan_cache=True, plan_cache_seed=(
            team.leader.id, tuple(m.id for m in team.members)))
    ctx = dataclasses.replace(ctx, refine_activation=refine)
    with RefinePool(ctx, workers=1) as pool:
        by_id = pool._by_id
        cache = new_plan_cache(ctx, by_id) if use_cache else None
        leaf = evaluate(ctx, by_id, pool._tree_for, pool._budget_for,
                        (team.leader.id, tuple(m.id for m in team.members)), plan_cache=cache)
        bt = develop_team(ctx, team, pool._tree_for, pool._budget_for, ctx.charts[0],
                          plan_cache=cache)
    got = score_team_on_chart(team, ctx.charts[0], dataclasses.replace(ctx.cfg, board=bt.board))
    assert got.total_score == leaf


@pytest.mark.parametrize("refine", [False, True])
def test_develop_team_plans_the_off_team_singers_boards_as_the_leaf_does(refine):
    """With every board planned (`plan_singer_bench`, Unlimited Cubes and Hexas) the leaf
    plans the song's off-team singers' boards for the song, so their yellows pay on it;
    the detail must develop the same bench, or it shows a lower number than its row
    (-3.6 % on m0342)."""
    from hbuilder.domain.song_rank import score_team_on_chart
    from hbuilder.solver.parallel_refine import develop_team, evaluate, new_plan_cache

    ctx, teams = _context()
    team = teams[0]
    on_team = {"chr-" + c.id.split("-")[1] for c in (team.leader, *team.members)}
    library = load_charts(str(config.CHARTS_PATH))
    chart = next((library.get(m) for m in sorted(library.music_ids)
                  if set(library.get(m).singers or ()) - on_team), None)
    if chart is None:
        pytest.skip("no chart sung by a holomem off the team")
    ctx = dataclasses.replace(
        ctx, charts=(chart,), refine_activation=refine, use_plan_cache=True,
        plan_cache_seed=(team.leader.id, tuple(m.id for m in team.members)),
        bench_characters={"(no captured board)": []}, plan_singer_bench=True)
    ids = (team.leader.id, tuple(m.id for m in team.members))
    with RefinePool(ctx, workers=1) as pool:
        by_id = pool._by_id
        cache = new_plan_cache(ctx, by_id)
        leaf = evaluate(ctx, by_id, pool._tree_for, pool._budget_for, ids, plan_cache=cache)
        unplanned = evaluate(dataclasses.replace(ctx, plan_singer_bench=False), by_id,
                             pool._tree_for, pool._budget_for, ids, plan_cache=cache)
        bt = develop_team(ctx, team, pool._tree_for, pool._budget_for, chart, plan_cache=cache)
    assert leaf > unplanned, "the case must have an off-team singer whose yellows pay"
    got = score_team_on_chart(team, chart, dataclasses.replace(ctx.cfg, board=bt.board))
    assert got.total_score == leaf


@pytest.mark.parametrize("refine", [False, True])
def test_the_ceiling_is_the_all_fire_score_on_the_leaf_board(refine):
    """`evaluate`'s ceiling mode (a ranking sorted by Max): the run if every ACTIVE fires,
    at the slot order the expectation plays, on the very board the expectation is scored
    on -- what the row detail shows as Max -- and never below the expectation."""
    from hbuilder.domain.song_rank import score_team_ceiling, score_team_on_chart
    from hbuilder.solver.parallel_refine import develop_team, evaluate

    ctx, teams = _context()
    ctx = dataclasses.replace(ctx, refine_activation=refine)
    with RefinePool(ctx, workers=1) as pool:
        by_id = pool._by_id
        for team in teams[:3]:
            ids = (team.leader.id, tuple(m.id for m in team.members))
            leaf = evaluate(ctx, by_id, pool._tree_for, pool._budget_for, ids)
            ceiling = evaluate(ctx, by_id, pool._tree_for, pool._budget_for,
                               (*ids, None, refine, True, None))
            bt = develop_team(ctx, team, pool._tree_for, pool._budget_for, ctx.charts[0])
            cfg = dataclasses.replace(ctx.cfg, board=bt.board)
            order = score_team_on_chart(team, ctx.charts[0], cfg).member_order
            assert ceiling == score_team_ceiling(team, ctx.charts[0], cfg, order)
            assert ceiling >= leaf
            assert pool.batch_ceiling([team], [0], [refine], [None]) == [ceiling]


def test_two_contexts_on_one_shared_pool_score_as_their_own_pools():
    """Two engines' contexts on ONE set of workers (`SharedPool`), batches interleaved so
    each worker switches between them: the same floats, in order, as each context alone."""
    from hbuilder.solver.parallel_refine import SharedPool

    ctx, teams = _context()
    library = load_charts(str(config.CHARTS_PATH))
    chart = next(library.get(m) for m in sorted(library.music_ids)
                 if library.get(m).music_id != ctx.charts[0].music_id)
    other = dataclasses.replace(ctx, charts=(chart,))
    with RefinePool(ctx, workers=1) as a1, RefinePool(other, workers=1) as b1:
        expect_a, expect_b = a1.batch_score(teams), b1.batch_score(teams)
        per_chart_b = b1.batch_score_per_chart(teams)
    assert expect_a != expect_b
    shared = SharedPool(2)
    try:
        with RefinePool(ctx, shared=shared) as a, RefinePool(other, shared=shared) as b:
            assert a._pool is not None and a._pool is b._pool
            for _ in range(2):
                assert a.batch_score(teams) == expect_a
                assert b.batch_score(teams) == expect_b
            assert b.batch_score_per_chart(teams) == per_chart_b
        # a view closed leaves the workers to the next context
        with RefinePool(other, shared=shared) as c:
            assert c._pool is shared._pool and c.batch_score(teams) == expect_b
    finally:
        shared.close()


def test_a_shared_pool_stops_one_contexts_tasks_only():
    """`stop_through` on one context marks its own slot: another context's batch of a
    later or earlier generation runs on; a slot handed out again starts at the current
    generation, so an old batch of the context that held it before stays stopped."""
    from hbuilder.solver.parallel_refine import SharedPool

    ctx, _teams = _context()
    shared = SharedPool(2)
    try:
        with RefinePool(ctx, shared=shared) as a, RefinePool(ctx, shared=shared) as b:
            ga, gb = a.new_generation(), b.new_generation()
            a.stop_through(ga)
            slot_a, slot_b = a._token[1], b._token[1]
            assert shared._stop[slot_a] >= ga and shared._stop[slot_b] < gb
            assert gb > ga        # generations are counted across the contexts
        with RefinePool(ctx, shared=shared) as c:
            assert shared._stop[c._token[1]] >= gb
    finally:
        shared.close()
