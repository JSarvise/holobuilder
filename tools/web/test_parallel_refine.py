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


def test_scores_over_several_singer_benches_do_not_depend_on_how_many_are_held(monkeypatch):
    """With every board planned, a team scored on several charts develops one board per
    singer context, each on its own bench (`_with_singer_bench`). Holding those benches
    (and their contexts) between charts and teams is a memo of pure functions: the scores
    are the ones a cache of ONE bench gives, which rebuilds each bench as it comes, and a
    second pass finds every context again instead of copying it."""
    from hbuilder.domain import board_plan
    from hbuilder.solver import parallel_refine as pr

    ctx, teams = _context()
    library = load_charts(str(config.CHARTS_PATH))
    charts, seen = [], set()
    for music_id in sorted(library.music_ids):
        chart = library.get(music_id)
        for team in teams:
            on_team = {"chr-" + c.id.split("-")[1] for c in (team.leader, *team.members)}
            key = (frozenset(on_team & set(chart.singers or ())), chart.singer_type)
            if key[0] and key not in seen:
                seen.add(key)
                charts.append(chart)
                break
        if len(charts) == 6:
            break
    if len(charts) < 3:
        pytest.skip("fewer than three charts sung by these teams' holomems")
    team = teams[0]
    ctx = dataclasses.replace(
        ctx, charts=tuple(charts), board_per_chart=True, use_plan_cache=True,
        plan_cache_seed=(team.leader.id, tuple(m.id for m in team.members)),
        bench_characters={"(no captured board)": []}, plan_singer_bench=True)
    ids = [(t.leader.id, tuple(m.id for m in t.members)) for t in teams]

    def scores(held: int):
        monkeypatch.setattr(board_plan, "_BENCHES_HELD", held)
        monkeypatch.setattr(pr, "SINGER_CONTEXTS_HELD", held)
        for memo in (board_plan._SINGER_BENCH, board_plan._BENCH_BOUGHT,
                     board_plan._GREEN_HOLDERS, pr._SINGER_CTX):
            memo.clear()
        with RefinePool(ctx, workers=1) as pool:
            cache = pr.new_plan_cache(ctx, pool._by_id)
            out = [pr.evaluate(ctx, pool._by_id, pool._tree_for, pool._budget_for, team_ids,
                               plan_cache=cache, per_chart=True)
                   for _ in range(2) for team_ids in ids]
            counts = len(cache._benches), len(pr._SINGER_CTX)
        # a closed pool's context takes the entries that hold it with it, and a discarded
        # plan cache its singer benches (`forget_context`)
        assert not pr._SINGER_CTX
        assert any(v[1] is cache for v in board_plan._SINGER_BENCH.values())
        pr.forget_context(ctx, cache)
        assert not any(v[1] is cache for v in board_plan._SINGER_BENCH.values())
        return (out, *counts)

    one, benches_one, contexts_one = scores(1)
    many, benches_many, contexts_many = scores(128)
    assert many == one                                  # the same floats, chart by chart
    assert many[:len(ids)] == many[len(ids):]           # and the same on the second pass
    assert benches_one == contexts_one == 1
    # one bench and one context per singer context met, each made once (the bench as
    # captured is one more when some chart has no singer on some team)
    assert contexts_many >= 3 and benches_many in (contexts_many, contexts_many + 1)


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


def _sung_charts(teams, count: int = 6):
    """Charts sung by some holomem of `teams`, one per (team singers, scope) met: the singer
    contexts a team's boards are developed for."""
    library = load_charts(str(config.CHARTS_PATH))
    charts, seen = [], set()
    for music_id in sorted(library.music_ids):
        chart = library.get(music_id)
        for team in teams:
            on_team = {"chr-" + c.id.split("-")[1] for c in (team.leader, *team.members)}
            key = (frozenset(on_team & set(chart.singers or ())), chart.singer_type)
            if key[0] and key not in seen:
                seen.add(key)
                charts.append(chart)
                break
        if len(charts) == count:
            break
    if len(charts) < 3:
        pytest.skip("fewer than three charts sung by these teams' holomems")
    return charts


def test_one_overall_power_serves_the_boards_that_agree_on_what_it_reads(monkeypatch):
    """A team scored on several charts has one board per singer context; the boards that
    agree on `overall_power_board_key` share one Overall Power. The scores are the floats
    of a run that computes every board's own, and two boards with one key have one power."""
    from hbuilder.domain import song
    from hbuilder.solver import parallel_refine as pr

    ctx, teams = _context()
    charts = _sung_charts(teams)
    team = teams[0]
    # ... and one chart per scope that none of the first team's holomems sings: contexts
    # that differ in the scope alone, whose boards are the same plans
    library = load_charts(str(config.CHARTS_PATH))
    on_team = {"chr-" + c.id.split("-")[1] for c in (team.leader, *team.members)}
    unsung: dict = {}
    for music_id in sorted(library.music_ids):
        chart = library.get(music_id)
        if not on_team & set(chart.singers or ()):
            unsung.setdefault(chart.singer_type, chart)
    if len(unsung) < 2:
        pytest.skip("fewer than two scopes among the charts this team does not sing")
    charts = [*charts, *unsung.values()]
    ctx = dataclasses.replace(
        ctx, charts=tuple(charts), board_per_chart=True, use_plan_cache=True,
        plan_cache_seed=(team.leader.id, tuple(m.id for m in team.members)),
        bench_characters={"(no captured board)": []})
    ids = [(t.leader.id, tuple(m.id for m in t.members)) for t in teams]
    seen: list = []            # (team ids, key, power) of every Overall Power computed
    real_power, real_key = pr.overall_power_for, song.overall_power_board_key

    def counted(members, leader, cfg):
        power = real_power(members, leader, cfg)
        seen.append(((leader.id, *(m.id for m in members)), real_key(members, leader, cfg.board),
                     (power.value, power.power_up_bases)))
        return power
    monkeypatch.setattr(pr, "overall_power_for", counted)

    def scores():
        seen.clear()
        with RefinePool(ctx, workers=1) as pool:
            cache = pr.new_plan_cache(ctx, pool._by_id)
            return [pr.evaluate(ctx, pool._by_id, pool._tree_for, pool._budget_for, team_ids,
                                plan_cache=cache, per_chart=True) for team_ids in ids]

    shared = scores()
    computed_shared = len(seen)
    # no two boards share: every board's own Overall Power
    monkeypatch.setattr(pr, "overall_power_board_key", lambda members, leader, board: object())
    own = scores()
    assert shared == own                                  # the same floats, chart by chart
    assert computed_shared < len(seen)                    # and fewer Overall Powers computed
    by_key: dict = {}
    for team_ids, key, power in seen:
        assert by_key.setdefault((team_ids, key), power) == power


def test_a_teams_board_off_point_is_computed_once_for_all_its_boards(monkeypatch):
    """`_cached_board` assembles every board of a team at the team scored with no board;
    given the caller's `off_point` list it computes that once and assembles the same boards."""
    from hbuilder.domain.board_plan import SongCast
    from hbuilder.solver import parallel_refine as pr

    ctx, teams = _context()
    charts = _sung_charts(teams)
    team = teams[0]
    ctx = dataclasses.replace(
        ctx, use_plan_cache=True, bench_characters={"(no captured board)": []},
        plan_cache_seed=(team.leader.id, tuple(m.id for m in team.members)))
    calls = []
    real = pr.score_with_breakdown
    monkeypatch.setattr(pr, "score_with_breakdown",
                        lambda t, cfg: (calls.append(cfg.board), real(t, cfg))[1])
    with RefinePool(ctx, workers=1) as pool:
        cache = pr.new_plan_cache(ctx, pool._by_id)
        on_team = frozenset("chr-" + c.id.split("-")[1] for c in (team.leader, *team.members))
        casts = [SongCast(on_team & frozenset(c.singers or ()), c.singer_type) for c in charts]
        each = [pr._cached_board(ctx, team, pool._tree_for, pool._budget_for, cache, cast)
                for cast in casts]
        assert len(calls) == len(casts)
        calls.clear()
        point: list = []
        once = [pr._cached_board(ctx, team, pool._tree_for, pool._budget_for, cache, cast, point)
                for cast in casts]
    assert calls == [None]                                # one board-off score for them all
    assert once == each


def test_a_team_prepared_once_scores_each_chart_as_a_fresh_call_does():
    """`score_song_detailed(team_setup=...)`: the part of the team no chart moves is prepared
    by the first call with a config and read back by the next ones. Same scores, same slot
    orders, with a board and without, one entry per config."""
    from hbuilder.domain.song import overall_power_for, score_song_detailed
    from hbuilder.solver import parallel_refine as pr

    ctx, teams = _context()
    charts = _sung_charts(teams)
    with RefinePool(ctx, workers=1) as pool:
        for team in teams:
            board = pr.develop_team(ctx, team, pool._tree_for, pool._budget_for, charts[0]).board
            setup: dict = {}
            for cfg in (ctx.cfg, dataclasses.replace(ctx.cfg, board=board)):
                power = overall_power_for(tuple(team.members), team.leader, cfg)
                for chart in charts:
                    fresh = score_song_detailed(team.members, (team.leader,), chart, cfg,
                                                deck_power=power)
                    held = score_song_detailed(team.members, (team.leader,), chart, cfg,
                                               deck_power=power, team_setup=setup)
                    assert held == fresh
                assert setup[id(cfg)][0] is cfg and team.leader.id in setup[id(cfg)][3]
            assert len(setup) == 2


def _per_chart_context(charts, team):
    ctx, _teams = _context()
    return dataclasses.replace(
        ctx, charts=tuple(charts), board_per_chart=True, use_plan_cache=True,
        plan_cache_seed=(team.leader.id, tuple(m.id for m in team.members)),
        bench_characters={"(no captured board)": []})


def test_counts_chosen_per_chart_are_scores_of_real_boards_never_below_the_plain_ones():
    """`evaluate` with COUNTS: each chart's score with the members' activation-frequency
    node counts chosen for that chart. Every such score is the exact score of a board that
    is the plain develop's with frequency nodes dropped (nothing else moved), or the plain
    score itself; the mean is the mean of those; COUNTS_BOTH is `(plain mean, that mean)`."""
    from hbuilder.domain.board_plan import SongCast, cooldown_node_ids
    from hbuilder.domain.song import overall_power_for, score_song_detailed
    from hbuilder.solver import parallel_refine as pr

    _ctx, teams = _context()
    charts = _sung_charts(teams)
    ctx = _per_chart_context(charts, teams[0])
    family = cooldown_node_ids(ctx.skilltree)
    lifted = 0
    with RefinePool(ctx, workers=1) as pool:
        cache = pr.new_plan_cache(ctx, pool._by_id)
        for team in teams:
            ids = (team.leader.id, tuple(m.id for m in team.members))
            run = lambda *sel, **kw: pr.evaluate(  # noqa: E731
                ctx, pool._by_id, pool._tree_for, pool._budget_for, (*ids, *sel),
                plan_cache=cache, **kw)
            plain = run(per_chart=True)
            counted = run(None, pr.COUNTS, per_chart=True)
            assert all(c >= p for p, c in zip(plain, counted))
            assert run(None, pr.COUNTS) == sum(counted) / len(counted)
            assert run(None, pr.COUNTS_BOTH) == (sum(plain) / len(plain), sum(counted) / len(counted))
            # chart by chart: the plain develop's board and plan, then the choice
            on_team = frozenset("chr-" + c.id.split("-")[1] for c in (team.leader, *team.members))
            for chart, p, c in zip(charts, plain, counted):
                cast = SongCast(on_team & frozenset(chart.singers or ()), chart.singer_type)
                plan, board, assemble = pr._planned_board(
                    ctx, team, pool._tree_for, pool._budget_for, cache, cast)
                cfg = dataclasses.replace(ctx.cfg, board=board)
                power_of = lambda cfg_b: overall_power_for(tuple(team.members), team.leader, cfg_b)  # noqa: E731
                chooser = pr.ChartCounts(team, cfg, plan, assemble, tree_for=pool._tree_for,
                                         budget_for=pool._budget_for, skilltree=ctx.skilltree,
                                         power_of=power_of)
                assert chooser.choose(chart)[:2] == (p, c)
                combo = chooser.choose(chart)[2]
                if combo is None:
                    assert c == p
                    continue
                lifted += 1
                assert c > p
                cfg_v = chooser.config_at(combo)
                exact = score_song_detailed(team.members, (team.leader,), chart, cfg_v,
                                            deck_power=power_of(cfg_v))[0].total_score
                assert c == exact
                chosen = pr.plan_with_counts(plan, chooser.axes, combo,
                                             {x.id: x for x in (team.leader, *team.members)},
                                             pool._tree_for)
                for card_id, before in plan.per_character.items():
                    after = chosen.per_character[card_id].selected
                    assert after <= before.selected and before.selected - after <= family
    if not lifted:
        pytest.skip("no chart of these teams gains from a frequency count drop")


def test_develop_team_with_counts_is_the_board_the_leaf_scores_the_chart_on():
    """A row served with its frequency counts chosen per chart opens its detail on a song
    with `refine="counts"`: `develop_team` then returns the board `evaluate` scores that one
    chart on in that mode (the plain develop's when no count drop scores higher)."""
    from hbuilder.domain.song_rank import score_team_on_chart
    from hbuilder.solver import parallel_refine as pr

    _ctx, teams = _context()
    charts = _sung_charts(teams)
    moved = 0
    for chart in charts[:3]:
        ctx = _per_chart_context([chart], teams[0])
        with RefinePool(ctx, workers=1) as pool:
            cache = pr.new_plan_cache(ctx, pool._by_id)
            for team in teams:
                ids = (team.leader.id, tuple(m.id for m in team.members))
                leaf = pr.evaluate(ctx, pool._by_id, pool._tree_for, pool._budget_for,
                                   (*ids, None, pr.COUNTS), plan_cache=cache)
                plain = pr.evaluate(ctx, pool._by_id, pool._tree_for, pool._budget_for, ids,
                                    plan_cache=cache)
                bt = pr.develop_team(ctx, team, pool._tree_for, pool._budget_for, chart,
                                     plan_cache=cache, refine=pr.COUNTS)
                got = score_team_on_chart(team, chart, dataclasses.replace(ctx.cfg, board=bt.board))
                assert got.total_score == leaf >= plain
                moved += leaf > plain
    if not moved:
        pytest.skip("no chart of these teams gains from a frequency count drop")


def test_the_count_axes_are_the_ones_the_level_two_refinement_tries():
    """`cooldown_count_axes` is `refine_cooldown_counts`' own first step: with `drops_only`
    a member's counts are those of its states that only drop nodes, the same boards; and a
    memo changes nothing."""
    from hbuilder.domain.board_plan import cooldown_count_axes, cooldown_node_ids
    from hbuilder.solver import parallel_refine as pr

    ctx, teams = _context()
    chart = ctx.charts[0]
    with RefinePool(ctx, workers=1) as pool:
        for team in teams:
            bt = pr.develop_team(ctx, team, pool._tree_for, pool._budget_for, chart)
            cards = {c.id: c for c in (team.leader, *team.members)}
            kw = dict(tree_for=pool._tree_for, budget_for=pool._budget_for,
                      cooldown_nodes=cooldown_node_ids(ctx.skilltree),
                      member_ids=frozenset(m.id for m in team.members))
            every = cooldown_count_axes(bt.plan, cards, **kw)
            memo: dict = {}
            drops = cooldown_count_axes(bt.plan, cards, drops_only=True, memo=memo, **kw)
            assert drops == cooldown_count_axes(bt.plan, cards, drops_only=True, memo=memo, **kw)
            assert drops == cooldown_count_axes(bt.plan, cards, drops_only=True, **kw)
            full = {card_id: (dict(states), here) for card_id, states, here in every}
            for card_id, states, here in drops:
                selected = bt.plan.per_character[card_id].selected
                assert here == full[card_id][1] == max(count for count, _sel in states)
                for count, nodes in states:
                    assert nodes <= selected
                    assert full[card_id][0][count] == nodes
