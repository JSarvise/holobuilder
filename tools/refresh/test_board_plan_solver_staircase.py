"""`board_plan_solver._staircase`: the joint cooldown counts of the member boards.

A count that is each board's worst on its own can win jointly: the score at each instant is
the strongest live ACTIVE, so one member giving up its frequency nodes while another takes
more can gain. Ranking each board's counts alone and keeping the best few (the off-song
path) never tries such a vector; on a song every vector is screened first.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
for _p in (HERE, REPO / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import board_plan_solver as bps  # noqa: E402

CDS = {"a": ["a-cd1", "a-cd2", "a-cd3"], "b": ["b-cd1", "b-cd2", "b-cd3"]}
KIND = {n: "Act.CD" for nodes in CDS.values() for n in nodes}


def _counts(state):
    return tuple(sum(1 for n in state[c] if n in KIND) for c in ("a", "b"))


def _joint(ka, kb):
    # "a" alone loses most at 0 (the per-board tables rank it last), but with "b" at 3
    # it scores best of all: the two members' windows stop overlapping
    return 100.0 * ka + 10.0 * kb + (1000.0 if (ka, kb) == (0, 3) else 0.0)


def _plan(screen):
    def full_state(board):
        return {c: set(board[c]["nodes"]) for c in board}

    def levels(state):
        return {c: (sum(1 for n in state[c] if n in KIND), 0) for c in ("a", "b")}

    return SimpleNamespace(
        act_chars=["a", "b"],
        act_kind=KIND,
        graphs={c: {n: () for n in CDS[c]} for c in CDS},
        team_score=lambda state: _joint(*_counts(state)),
        full_state=full_state,
        levels=levels,
        leader_id="leader",
        member_ids=("a", "b"),
        oracle=SimpleNamespace(screen_cooldown_counts=screen),
    )


def _tables():
    # each board's own table around the round-0 plan (a = 2, b = 1), the other held
    return {
        "a": {(k, 0): _joint(k, 1) for k in range(4)},
        "b": {(k, 0): _joint(2, k) for k in range(4)},
    }


def _run(monkeypatch, screen):
    def solve_pinned(_plan, pinned, warm=None):
        return {c: {"nodes": CDS[c][: next(iter(lv))[0]]} for c, lv in pinned.items()}

    monkeypatch.setattr(bps, "_solve_milp", solve_pinned)
    board0 = {"a": {"nodes": CDS["a"][:2]}, "b": {"nodes": CDS["b"][:1]}}
    plan = _plan(screen)
    board, best = bps._staircase(plan, board0, plan.team_score(plan.full_state(board0)), _tables())
    return _counts(plan.full_state(board)), best


def test_on_a_song_every_joint_count_is_screened(monkeypatch):
    def screen(_leader, _members, _state, cooldown_nodes, vectors):
        assert set(cooldown_nodes) == {"a", "b"}
        return [_joint(*kv) for kv in vectors]

    counts, best = _run(monkeypatch, screen)
    assert counts == (0, 3)
    assert best == _joint(0, 3)


def test_the_screen_keeps_vectors_within_its_margin_for_the_exact_score(monkeypatch):
    # an estimate that rates the true best a hair below another vector still sends it to
    # the exact score, as long as it is within ACT_SCREEN_MARGIN of the top estimate
    def screen(_leader, _members, _state, _cooldown_nodes, vectors):
        top = max(_joint(*kv) for kv in vectors)
        return [top * (1 + bps.ACT_SCREEN_MARGIN / 2) if kv == (3, 3) else _joint(*kv) for kv in vectors]

    counts, _best = _run(monkeypatch, screen)
    assert counts == (0, 3)


def test_off_a_song_the_staircase_keeps_each_boards_best_counts(monkeypatch):
    # no screen (the general scorer has none): each board's ACT_JOINT_KEEP best counts on
    # its own table, as before -- "a" at 0 is never tried
    counts, _best = _run(monkeypatch, lambda *_args: None)
    assert counts[0] != 0


def test_without_a_screen_kernel_the_oracle_does_not_screen(monkeypatch):
    # with no kernel the screen would score all 1,024 vectors exactly (~30 s); None sends
    # the staircase to the off-song path instead
    import tree_optimise

    from hbuilder.domain import song

    monkeypatch.setattr(song, "COOLDOWN_SCREEN_KERNEL", None)
    monkeypatch.delenv(song.COOLDOWN_SCREEN_KERNEL_ENV, raising=False)
    oracle = SimpleNamespace(_chart=object(), _pool=())
    assert tree_optimise.Oracle.screen_cooldown_counts(oracle, "l", [], {}, {}, [(0,)]) is None


def test_over_a_pool_the_screen_is_the_shares_sum_of_its_charts(monkeypatch):
    # a Highlight pool plan scores the shares' sum over the pool (`Oracle.score_team`); a
    # screen on `song` alone would order another score and could drop the pool's best split
    import dataclasses

    import tree_optimise

    from hbuilder.domain import song

    @dataclasses.dataclass(frozen=True)
    class Cfg:
        board: object = None
        song_absolute_deck_power: bool = False
        song_auto: bool = False

    board = SimpleNamespace(name_by_chr={"a": "A"}, uptime_for=lambda _name: (1.0, 0.0))
    per_chart = {"x": [1.0, 2.0], "y": [10.0, 0.0]}
    monkeypatch.setattr(song, "COOLDOWN_SCREEN_KERNEL", lambda _spec: None)
    monkeypatch.setattr(
        song,
        "screen_song_cooldown_variants",
        lambda _members, _leader, chart, _cfg, variants, **_kw: per_chart[chart][: len(variants)],
    )

    def screen(pool):
        oracle = SimpleNamespace(
            _chart="x",
            _pool=pool,
            base_cfg=Cfg(),
            _by_id={"l": "L", "a": "A"},
            board_for=lambda _state: board,
            state_from=lambda nodes: nodes,
        )
        return tree_optimise.Oracle.screen_cooldown_counts(
            oracle, "l", ["a"], {"a": set()}, {"a": ["a-cd1"]}, [(0,), (1,)]
        )

    assert screen(()) == [1.0, 2.0]  # on a song: that song's screen, as before
    assert screen((("x", 0.25), ("y", 0.75))) == [0.25 * 1.0 + 0.75 * 10.0, 0.25 * 2.0 + 0.75 * 0.0]
