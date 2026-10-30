"""`solver/parallel_refine.RefinePool`: the refine's exact stage across
processes must return the SAME floats the in-process path computes, in order,
and fall back to serial when the pool cannot stand up.

Uses the real dumps (skilltree + master + charts, gitignored) and SKIPS when
they are absent — the same first-class-absence contract the develop routes
have. Small on purpose: three teams, two workers, one chart."""
from __future__ import annotations

import dataclasses

import pytest

import server as web_server
from hbuilder.domain.board_plan import node_effect_ids_from_master
from hbuilder.domain.song import load_charts
from hbuilder.domain.team import Team
from hbuilder.solver.parallel_refine import RefineContext, RefinePool


def _context():
    skilltree = web_server._read_json(web_server.SKILLTREE_PATH)
    node_master = web_server._read_json(web_server.NODE_MASTER_PATH)
    library = load_charts(str(web_server.CHARTS_PATH))
    if not skilltree or not node_master or library is None:
        pytest.skip("skilltree/master/charts dumps absent (gitignored)")
    chart = next((library.get(m) for m in library.music_ids), None)
    if chart is None:
        pytest.skip("no chart in the library")
    cards, owned, cfg, _ = web_server.load_inputs(
        web_server.CARDS_PATH, web_server.OWNED_PATH, False)
    cards, cb = web_server.apply_collection(
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
