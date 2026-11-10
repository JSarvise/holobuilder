"""The compiled board assembly (`connect.assemble`, injected as `board_plan.BOARD_KERNEL`)
must equal the Python it replaces, to the bit.

Three layers: the kernel's `sum()` against the built-in one (the capped matching's yellow
lift goes through it); the leaf on real teams and real charts -- solo, group and whole-roster
songs, the song cap binding before the matching, after it and not at all, and lower caps
that make the capped matching's bisection choose -- every float `==` the Python leaf's; and
the assembled boards themselves, `==`, with their named sockets.
"""

from __future__ import annotations

import dataclasses
import random
import shutil
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

pytestmark = pytest.mark.skipif(shutil.which("gcc") is None, reason="no gcc to build the kernel")

import connect  # noqa: E402

from hbuilder.domain import board_plan as bp  # noqa: E402

REPO = HERE.parents[2]
# m0342 (a group song whose bench singers' yellows pass the cap before any matching) and
# m0334 (Calli's solo NIGHTBREAK) always; the rest drawn from the library
FIXED_SONGS = ("m0342", "m0334")


@pytest.fixture(autouse=True)
def _no_injected_kernels(monkeypatch):
    """Every test starts and ends with no kernel injected, in the module or the environment."""
    for attr, env in (("CONNECT_KERNEL", bp.CONNECT_KERNEL_ENV), ("BOARD_KERNEL", bp.BOARD_KERNEL_ENV)):
        monkeypatch.setattr(bp, attr, None)
        monkeypatch.setenv(env, "")


def test_the_kernel_sums_floats_as_the_builtin_sum_does():
    rng = random.Random(20261106)
    pools = [
        lambda: rng.choice([0.0, 0.1, 0.2, 0.3, 1e-9, 7.5, 12.0, 1e16, -1e16, -0.0]),
        lambda: rng.uniform(0, 30) * rng.choice([1.0, 1.05, 1.1, 1.153]),
        lambda: rng.choice([0.0, 0.0, 1.0]) * rng.uniform(-1, 1) * 10 ** rng.randrange(-8, 17),
    ]
    for _ in range(20_000):
        draw = rng.choice(pools)
        values = [draw() for _ in range(rng.randrange(0, 30))]
        got, want = connect.py_sum(values), sum(values)
        assert got == want and str(got) == str(float(want)), values
    assert connect.py_sum([0.1] * 10) == 1.0  # the compensated sum, not the naive 0.9999...
    assert str(connect.py_sum([-0.0])) == "0.0"  # the int 0 start: 0 + -0.0 is +0.0


# ---------------------------------------------------------------------------
# real teams on real charts (skipped without the recon dumps)
# ---------------------------------------------------------------------------
class Counting:
    """`connect.assemble` that counts its calls, its declines and the branches it took."""

    def __init__(self):
        self.calls = self.declined = 0
        self.bound = {0: 0, 1: 0, 2: 0, 3: 0}  # the check that bound: none / before / after / no checks
        self.path = {0: 0, 1: 0, 2: 0}  # 0 uncapped, 1 capped first kept, 2 scaled kept
        self.incumbent = self.capped_incumbent = 0  # calls with the account's placements

    def __call__(self, *args, **kwargs):
        self.calls += 1
        out = connect.assemble(*args, **kwargs)
        if out is None:
            self.declined += 1
        else:
            path, _matchings, bound = connect.LAST_INFO
            self.path[path] += 1
            self.bound[bound] += 1
            if kwargs.get("incumbent"):
                self.incumbent += 1
                self.capped_incumbent += bound > 0
        return out


def _built(basis: str):
    for extra in (REPO / "tools" / "web", REPO / "tools" / "refresh"):
        if str(extra) not in sys.path:
            sys.path.insert(0, str(extra))
    try:
        import leaf_inputs

        return leaf_inputs.leaf_inputs(basis, "EXPERT")
    except Exception as exc:  # noqa: BLE001 - no dumps on this machine
        pytest.skip(f"the {basis} leaf inputs are not available here: {exc!r}")


@pytest.fixture(scope="module")
def maxed():
    return _built("maxed")


@pytest.fixture(scope="module")
def account():
    built = _built("account")
    if not built["ctx"].bench_sockets:
        pytest.skip("the account's boards hold no Connect cards here")
    return built


@pytest.fixture
def held_views(monkeypatch):
    """How many plans the kernel saw with cards held on off-team sockets taken out."""
    seen = {"views": 0}
    view = bp._held_view

    def counting(asm, held):
        seen["views"] += 1
        return view(asm, held)

    monkeypatch.setattr(bp, "_held_view", counting)
    return seen


def _charts(count: int, seed: int):
    import leaf_inputs

    from hbuilder.domain.song import load_charts

    charts = load_charts(str(leaf_inputs.charts_path("EXPERT"))).at("EXPERT")
    by_song = {c.music_id: c for c in charts}
    fixed = [by_song[s] for s in FIXED_SONGS if s in by_song]
    rest = [c for c in charts if c.music_id not in FIXED_SONGS]
    random.Random(seed).shuffle(rest)
    # every scope the scorer pays, and a song without a cast, among the drawn ones
    picked, kinds = [], set()
    for c in rest:
        kind = c.singer_type or "none"
        if kind not in kinds or len(picked) < count:
            picked.append(c)
            kinds.add(kind)
        if len(picked) >= count and len(kinds) >= 3:
            break
    return tuple(fixed + picked)


def _teams(built, count: int, seed: int):
    from hbuilder.domain.team import Team, TeamError

    reps = list(built["reps"])
    rng = random.Random(seed)
    teams = []
    while len(teams) < count:
        members = rng.sample(reps, 5)
        # a leader who also plays as a member now and then (one board, both roles)
        leader = rng.choice(members) if rng.random() < 0.15 else rng.choice(reps)
        try:
            teams.append(Team(leader=leader, members=tuple(members)))
        except TeamError:
            continue
    return teams


def _per_chart(ctx, teams, kernel):
    from hbuilder.solver import parallel_refine as pr

    bp.BOARD_KERNEL = kernel
    try:
        return pr.RefinePool(ctx, workers=1).batch_score_per_chart(teams)
    finally:
        bp.BOARD_KERNEL = None


@pytest.mark.parametrize("cap", [None, 25.0], ids=["the game's cap", "cap 25"])
@pytest.mark.parametrize("basis", ["maxed", "account"])
def test_the_leaf_scores_real_teams_identically_with_the_board_kernel(
    request, monkeypatch, held_views, basis, cap
):
    """The leaf on 20 teams, maxed and with the account's own cards and boards (cards held on
    off-team sockets, the account's placements winning ties): one search per song (the song's whole cast, so off-team
    singers' yellows count) on five charts, and one search over ten charts (one board per
    team-singer context). The floats with the kernel are the Python's. The game's cap binds
    before the matching on m0342 and after it on solo songs; the lower caps (the same
    constant for both paths) send nearly every team through the bisection (cap 0, no room at
    all, is the board test's)."""
    built = request.getfixturevalue(basis)
    if cap is not None:
        monkeypatch.setattr(bp, "SONG_ALL_CAP_PERMIL", cap)
    teams = _teams(built, 20, 20261107)
    counting = Counting()
    contexts = [(chart,) for chart in _charts(3, 20261108)] + [_charts(8, 20261107)]
    for charts in contexts:
        ctx = dataclasses.replace(built["ctx"], charts=charts, board_per_chart=True)
        before = counting.calls
        want = _per_chart(ctx, teams, None)
        got = _per_chart(ctx, teams, counting)
        assert got == want, [c.music_id for c in charts]
        assert counting.calls - before >= len(teams), [c.music_id for c in charts]
    assert counting.declined == 0, vars(counting)
    if basis == "account":
        # every call carried the account's placements, the capped ones too, and plans with
        # held cards taken out reached the kernel
        assert counting.incumbent == counting.calls and counting.capped_incumbent > 0, vars(counting)
        assert held_views["views"] > 0
    if cap is None:
        assert counting.bound[1] + counting.bound[2] > 0 and counting.bound[0] > 0, vars(counting)
        if basis == "maxed":
            assert counting.bound[1] > 0 and counting.bound[2] > 0 and counting.path[2] > 0, vars(counting)
    else:
        assert counting.path[2] > 0 and counting.path[1] > 0, vars(counting)


@pytest.mark.parametrize("basis", ["maxed", "account"])
def test_the_assembled_boards_and_their_sockets_are_the_pythons(request, monkeypatch, basis):
    """The `BoardModel` itself and the named placements (a row's detail, where the account's
    own placements win ties from the first matching on), team by team and chart by chart, on
    the plan cache with and without the kernel."""
    from hbuilder.domain.board_plan import board_model_from_plan, cached_team_plan, song_cast
    from hbuilder.domain.scoring import deck_screen_unit_score, score_with_breakdown
    from hbuilder.solver import parallel_refine as pr

    built = request.getfixturevalue(basis)
    ctx = built["ctx"]
    pool = pr.RefinePool(ctx, workers=1)
    cache = pr.new_plan_cache(ctx, built["by_id"])
    cfg_off = dataclasses.replace(ctx.cfg, board=None)
    counting = Counting()
    compared = 0
    for cap in (None, 0.0):
        if cap is not None:
            monkeypatch.setattr(bp, "SONG_ALL_CAP_PERMIL", cap)
        for team in _teams(built, 15, 7):
            bd = score_with_breakdown(team, cfg_off)
            ds = deck_screen_unit_score(bd)
            bare = bd.active_pool / (1.0 + bd.combined_support_rate)
            for chart in _charts(4, 7):
                sings = song_cast(chart)
                plan = cached_team_plan(
                    team.leader,
                    team.members,
                    cache=cache,
                    tree_for=pool._tree_for,
                    budget_for=pool._budget_for,
                    node_effect_ids=ctx.node_effect_ids,
                    skilltree=ctx.skilltree,
                    sings=sings,
                )
                out = []
                for kernel in (None, counting):
                    bp.BOARD_KERNEL = kernel
                    placed: list = []
                    try:
                        out.append(
                            (
                                board_model_from_plan(
                                    plan,
                                    team.leader,
                                    team.members,
                                    ctx.skilltree,
                                    ctx.node_effect_ids,
                                    overall_power=ds.overall_power,
                                    bonus_permil=ds.score_bonus_permil,
                                    k_constant=bp.UNIT_SCORE_K,
                                    active_pool_rate=bare,
                                    connect_cards=ctx.connect_cards,
                                    placements_out=placed,
                                    bench_characters=ctx.bench_characters,
                                    name_by_chr=ctx.name_by_chr,
                                    sings=sings,
                                    plan_cache=cache,
                                    bench_sockets=ctx.bench_sockets,
                                ),
                                [dataclasses.astuple(p) for p in placed],
                            )
                        )
                    finally:
                        bp.BOARD_KERNEL = None
                (want, want_placed), (got, got_placed) = out
                assert got == want
                assert got_placed == want_placed
                compared += 1
    assert counting.declined == 0 and counting.calls == compared, vars(counting)
    assert counting.path[2] > 0 and counting.bound[1] > 0, vars(counting)
    if basis == "account":
        assert counting.incumbent == compared, vars(counting)


def _refined(ctx, teams, kernel):
    from hbuilder.solver import parallel_refine as pr

    bp.BOARD_KERNEL = kernel
    try:
        return pr.RefinePool(ctx, workers=1).batch_score(teams, refine_activation=True)
    finally:
        bp.BOARD_KERNEL = None


@pytest.mark.parametrize("basis", ["maxed", "account"])
def test_the_activation_refinement_scores_identically_with_the_board_kernel(request, basis):
    """The search's refine stage (`batch_score(refine_activation=True)`): every board it
    tries is a plan the cache did not make, assembled by its nodes (`selected_plan_key`) and
    matched as the plain path matches it (on the account the incumbents then win ties from
    the first matching on, cap or not) -- the floats are the Python's, on a group song where
    the cap binds before the matching, a solo song and a drawn one."""
    built = request.getfixturevalue(basis)
    teams = _teams(built, 6, 11)
    counting = Counting()
    for chart in _charts(1, 11)[:3]:
        ctx = dataclasses.replace(built["ctx"], charts=(chart,), board_per_chart=True)
        want = _refined(ctx, teams, None)
        got = _refined(ctx, teams, counting)
        assert got == want, chart.music_id
    # far more assemblies than teams: each refined team tries dozens of boards, each a plan
    # the cache did not make, matched the plain path's way (no cap checks)
    assert counting.declined == 0 and counting.calls > 10 * len(teams) * 3, vars(counting)
    assert counting.bound[3] > counting.calls // 2, vars(counting)
    if basis == "account":
        assert counting.incumbent == counting.calls, vars(counting)


@pytest.mark.parametrize("basis", ["maxed", "account"])
def test_a_refined_row_is_developed_identically_with_the_board_kernel(request, basis):
    """A row's detail re-develops its team with the refinement (`develop_team`): the same
    plan, moves, board, Unit Score and named sockets with the kernel."""
    from hbuilder.solver import parallel_refine as pr

    built = request.getfixturevalue(basis)
    counting = Counting()
    refined = 0
    for chart in _charts(1, 13)[:2]:
        ctx = dataclasses.replace(built["ctx"], charts=(chart,), board_per_chart=True, refine_activation=True)
        pool = pr.RefinePool(ctx, workers=1)
        for team in _teams(built, 3, 13):
            out = []
            for kernel in (None, counting):
                bp.BOARD_KERNEL = kernel
                try:
                    out.append(
                        pr.develop_team(
                            ctx,
                            team,
                            pool._tree_for,
                            pool._budget_for,
                            chart,
                            plan_cache=pr.new_plan_cache(ctx, built["by_id"]),
                            refine=True,
                        )
                    )
                finally:
                    bp.BOARD_KERNEL = None
            want, got = out
            assert got.plan == want.plan and got.board == want.board
            assert got.optimised_us == want.optimised_us and got.board_off_us == want.board_off_us
            assert [dataclasses.astuple(p) for p in got.sockets] == [
                dataclasses.astuple(p) for p in want.sockets
            ]
            refined += got.plan.plan_keys == {}
    assert counting.declined == 0 and counting.calls > 0, vars(counting)
    assert refined > 0  # some rows moved a node: their boards are the refined plan's


def test_a_spawned_worker_picks_the_board_kernel_up_from_the_environment():
    import os
    import subprocess

    connect.load()
    code = (
        "from hbuilder.domain import board_plan as bp;"
        "k = bp._board_kernel(); c = bp._connect_kernel();"
        "print('KERNEL', None if k is None else k.__name__, bp.BOARD_KERNEL,"
        " k is not None and c is not None and k.__module__ == c.__module__)"
    )
    env = dict(os.environ, PYTHONPATH=str(REPO / "src"))
    env[bp.BOARD_KERNEL_ENV] = f"{HERE / 'connect.py'}:assemble"
    env[bp.CONNECT_KERNEL_ENV] = f"{HERE / 'connect.py'}:assign"
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    # both seams load ONE module (one set of packing caches)
    assert "KERNEL assemble None True" in out.stdout, (out.stdout, out.stderr)

    env[bp.BOARD_KERNEL_ENV] = f"{HERE / 'no_such_file.py'}:assemble"
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert "KERNEL None None False" in out.stdout and "could not be loaded" in out.stderr, (
        out.stdout,
        out.stderr,
    )


def test_enable_injects_both_seams(monkeypatch):
    assert connect.enable()
    assert bp.CONNECT_KERNEL is connect.assign and bp.BOARD_KERNEL is connect.assemble
    import os

    assert os.environ[bp.BOARD_KERNEL_ENV].endswith("connect.py:assemble")
