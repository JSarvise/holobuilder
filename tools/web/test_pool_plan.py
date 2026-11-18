"""A Highlight row on the whole pool (`row_detail.pool_detail`): ONE board and ONE set of
Connect cards play whatever song is drawn, so the plan and the cards are valued over the
pool, not on one of its songs.

`pool_connect` and `pool_yellow` are checked on fakes (no account data). The planner's
Oracle over a pool is checked on the real account, and SKIPS when it is absent.
Read-only (test_no_writes.py)."""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pytest
import row_detail


@dataclasses.dataclass(frozen=True)
class _Ctx:
    """The fields `pool_connect` reads and replaces of a `RefineContext`."""

    deals: dict  # singer type of the chart dealt for -> {(chr, socket): card}
    board_state: dict
    bench_characters: dict
    name_by_chr: dict
    skilltree: dict
    connect_cards: dict
    board_state_slots: bool = False
    socket_bench: bool = True
    bench_sockets: dict | None = None
    cfg: object = None


def _team():
    card = lambda n: SimpleNamespace(id=f"card-0000{n}-5-uniq-0000-00", member=f"M{n}")  # noqa: E731
    return SimpleNamespace(leader=card(1), members=(card(1), card(2)))


def _chart(music_id, singers, singer_type):
    return SimpleNamespace(music_id=music_id, singers=tuple(singers), singer_type=singer_type)


def _placements(deal):
    return tuple(SimpleNamespace(chr=c, socket=s, card=card) for (c, s), card in sorted(deal.items()))


def _fake_develop(monkeypatch, value_of):
    """`develop_team` returning the deal `ctx` holds: the dealt one for the chart's singer
    type, or the stated one (`board_state_slots`). `value_of(deal)` is its pool score."""
    import hbuilder.solver.parallel_refine as pr

    def develop(ctx, team, tree_for, budget_for, chart, **_kw):
        if ctx.board_state_slots:
            deal = {(c, s): card for c, v in ctx.board_state.items() for s, card in v["slots"].items()}
            deal.update(ctx.stated_bench)
        else:
            deal = dict(ctx.deals[chart.singer_type])
        return SimpleNamespace(board=dict(deal), sockets=_placements(deal))

    def dealt(ctx, team_state, deal, team_chrs):
        state = {
            c: {"nodes": list(v["nodes"]), "slots": {s: card for (cc, s), card in deal.items() if cc == c}}
            for c, v in team_state.items()
        }
        fixed = dataclasses.replace(ctx, board_state=state, board_state_slots=True, socket_bench=False)
        object.__setattr__(fixed, "stated_bench", {k: v for k, v in deal.items() if k[0] not in team_chrs})
        return fixed

    monkeypatch.setattr(pr, "develop_team", develop)
    monkeypatch.setattr(row_detail, "_dealt_ctx", dealt)
    return lambda board: value_of(board)


def _ctx(deals):
    return _Ctx(
        deals=deals,
        board_state={"chr-00001": {"nodes": ["R-001"]}, "chr-00002": {"nodes": ["B-001"]}},
        bench_characters={},
        name_by_chr={},
        skilltree={},
        connect_cards={},
    )


def test_pool_connect_adds_a_bench_singers_card_to_the_best_cast_deal(monkeypatch):
    """Three solo songs and one group song with a bench co-singer: the solo deal is the best
    single deal, and the group deal's card on the co-singer's board (free in the solo deal)
    is added to it; the group deal's card on the LEADER's board would cost the solo songs
    more than it gives and is not taken."""
    solo = {("chr-00001", "S-004"): "card-solo", ("chr-00009", "S-001"): "card-green"}
    group = {("chr-00001", "S-004"): "card-group", ("chr-00007", "S-004"): "card-co"}

    def value(deal):
        v = 0.0
        v += 300.0 if deal.get(("chr-00001", "S-004")) == "card-solo" else 0.0  # three solo songs
        v += 60.0 if deal.get(("chr-00001", "S-004")) == "card-group" else 0.0  # the group song
        v += 80.0 if deal.get(("chr-00007", "S-004")) == "card-co" else 0.0  # the co-singer's yellow
        v += 10.0 if deal.get(("chr-00009", "S-001")) == "card-green" else 0.0
        return v

    pool_sum = _fake_develop(monkeypatch, value)
    charts = [
        _chart("m1", ["chr-00001"], "solo"),
        _chart("m2", ["chr-00001"], "solo"),
        _chart("m3", ["chr-00001"], "solo"),
        _chart("m4", ["chr-00001", "chr-00007"], "group"),
    ]
    bt, deal = row_detail.pool_connect(
        _ctx({"solo": solo, "group": group}), _team(), charts, None, None, pool_sum
    )
    assert deal == {**solo, ("chr-00007", "S-004"): "card-co"}
    assert pool_sum(bt.board) == 390.0 > max(value(solo), value(group))


def test_pool_connect_moves_a_card_only_when_the_pool_gains(monkeypatch):
    """The other cast wants a card the best deal already uses elsewhere: it is moved (and the
    two swapped) only when the pool's score rises; otherwise the best single deal stands and
    no mixed deal is reported."""
    solo = {("chr-00001", "S-004"): "card-x", ("chr-00009", "S-001"): "card-y"}
    group = {("chr-00007", "S-004"): "card-y"}

    def worth(on_bench_green, on_co_singer):
        def value(deal):
            v = 300.0 if deal.get(("chr-00001", "S-004")) == "card-x" else 0.0
            v += on_bench_green if deal.get(("chr-00009", "S-001")) == "card-y" else 0.0
            v += on_co_singer if deal.get(("chr-00007", "S-004")) == "card-y" else 0.0
            return v

        return value

    charts = [_chart("m1", ["chr-00001"], "solo"), _chart("m4", ["chr-00001", "chr-00007"], "group")]
    # the green is worth more than the co-singer's yellow: nothing moves
    pool_sum = _fake_develop(monkeypatch, worth(50.0, 20.0))
    bt, deal = row_detail.pool_connect(
        _ctx({"solo": solo, "group": group}), _team(), charts, None, None, pool_sum
    )
    assert deal is None and pool_sum(bt.board) == 350.0
    # the yellow is worth more: the card leaves the green for it
    pool_sum = _fake_develop(monkeypatch, worth(20.0, 50.0))
    bt, deal = row_detail.pool_connect(
        _ctx({"solo": solo, "group": group}), _team(), charts, None, None, pool_sum
    )
    assert deal == {("chr-00001", "S-004"): "card-x", ("chr-00007", "S-004"): "card-y"}
    assert pool_sum(bt.board) == 350.0


def test_pool_connect_with_one_cast_is_the_develop(monkeypatch):
    solo = {("chr-00001", "S-004"): "card-solo"}
    pool_sum = _fake_develop(monkeypatch, lambda deal: float(len(deal)))
    charts = [_chart("m1", ["chr-00001"], "solo"), _chart("m2", ["chr-00001"], "solo")]
    bt, deal = row_detail.pool_connect(_ctx({"solo": solo}), _team(), charts, None, None, pool_sum)
    assert deal is None and bt.board == solo


def test_pool_yellow_names_every_song_and_shares_the_valuation(monkeypatch):
    """One entry per pool song, in music id order: its yellow slope over the pool's size (0
    without a cast) and its share of the mean the plan values the other nodes on -- every
    song of a small pool, `POOL_PLAN_CHARTS` of a larger one, the shares summing to 1."""
    monkeypatch.setattr(
        row_detail, "score_team_on_chart", lambda team, chart, cfg: SimpleNamespace(total_score=2_100_000.0)
    )
    board = SimpleNamespace(song_score_bonus_for=lambda singers, scope: 0.05)
    cfg = SimpleNamespace(board=board)

    def chart(k, singers=("chr-00001",), singer_type="solo"):
        return SimpleNamespace(
            music_id=f"m{k:04d}", singers=tuple(singers), singer_type=singer_type, notes=(0,) * (100 + k)
        )

    small = [chart(3), chart(1), chart(2, (), "")]
    got = row_detail.pool_yellow(None, small, cfg)
    assert [e["music_id"] for e in got] == ["m0001", "m0002", "m0003"]
    assert [e["share"] for e in got] == [pytest.approx(1 / 3)] * 3
    assert got[0]["slope"] == pytest.approx(2_100_000.0 / (1000 * 1.05) / 3)
    assert got[1]["slope"] == 0.0 and got[1]["singers"] == []
    large = [chart(k) for k in range(1, 16)]
    got = row_detail.pool_yellow(None, large, cfg)
    scored = [e for e in got if e["share"] > 0]
    assert len(got) == 15 and len(scored) == row_detail.POOL_PLAN_CHARTS
    assert sum(e["share"] for e in got) == pytest.approx(1.0)
    assert all(e["slope"] > 0 for e in got)  # every song's yellows count, scored or not


def test_the_planners_oracle_scores_a_pool_as_the_shares_sum_of_its_songs():
    """`tree_optimise.Oracle(pool_songs=...)`: one team under one board is worth the shares'
    sum of its score on each pool song, so a node's value is its value over the pool."""
    pytest.importorskip("highspy")
    import board_plan_solver as bps
    import tree_optimise as to

    assert bps.pool_shares(None) == ()
    assert bps.pool_shares(
        [
            {"music_id": "m0002", "share": 0.25, "slope": 1.0},
            {"music_id": "m0001", "share": 0.75},
            {"music_id": "m0003", "share": 0.0},
            {"singers": ["chr-00001"], "singer_type": "solo", "slope": 2.0},
        ]
    ) == (("m0001", 0.75), ("m0002", 0.25))
    if not (to.DUMPS / "charts.json").exists() or not (to.DUMPS / "board_state.json").exists():
        pytest.skip("dumps/ not on this checkout")
    probe = to.Oracle(pool_size=1, scenario="account", quiet=True, uptime_model="finite")
    songs = ["m0034", "m0156"]  # a solo song and a group song
    team: list = []
    for card in probe.pool:  # six cards of six holomems
        if all(card.id.split("-")[1] != held.split("-")[1] for held in team):
            team.append(card.id)
        if len(team) == 6:
            break
    if len(team) < 6:
        pytest.skip("the account owns cards of fewer than six holomems")
    state = probe._board_state()
    try:
        single = [
            to.Oracle(pool_size=1, scenario="account", quiet=True, uptime_model="finite", song=m).score_team(
                team[0], team[1:], state
            )
            for m in songs
        ]
    except to.MissingInput:
        pytest.skip("the charts of the probe songs are not on disk")
    pooled = to.Oracle(
        pool_size=1,
        scenario="account",
        quiet=True,
        uptime_model="finite",
        song=songs[0],
        pool_songs=((songs[0], 0.25), (songs[1], 0.75)),
    ).score_team(team[0], team[1:], state)
    assert pooled == pytest.approx(0.25 * single[0] + 0.75 * single[1])
    assert single[0] != single[1]
