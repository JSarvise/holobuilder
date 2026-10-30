"""`BoardDigest`: the per-board half of `BoardModel`'s walks, kept
alive across the models a search builds so only a team's own boards are walked.

It is a re-arrangement of work, so every test here is a "same floats" test, and
every comparison is `==`. The reference is not the digest path reporting on
itself: `_reference` below is the plain nested `characters -> nodes -> effects`
walk of `BoardModel.__post_init__` and `_terms`, written out again here, and
`resolve` is the module's own reference implementation.
Values are deliberately not dyadic (0.1 steps, x1.55 multipliers) so an addition
made in another order shows up in the last bits instead of cancelling."""

from __future__ import annotations

import math
import random

import pytest

from hbuilder.domain.board import (
    GROUPING_ALL_CAP, SONG_ALL_CAP_PERMIL, STAT_NAMES, BoardBase, BoardDigest, BoardEffect, BoardError,
    BoardModel, BoardNode, CharacterBoard, _reaches, resolve,
)
from hbuilder.domain.board_plan import PlanCache
from hbuilder.domain.card import Stats

from conftest import make_card, make_scoring_config
from test_fast_leaf import BENCH, CONNECT, _both_boards, _team

_HOLOMEM = [f"Chr{n}" for n in range(1, 13)]
_GROUPS = ["grp-myth", "grp-gen_3", "Myth"]
_STATS = [Stats(performance=1000 + 137 * n, technique=900 + 71 * n, sense=1100 + 53 * n)
          for n in range(12)]


# `_FRACTIONAL[0]` makes some flat stat values fractional (77.5): a model
# holding one must take `_terms`' addend-by-addend path, not the summed one.
_FRACTIONAL = [False]


@pytest.fixture(params=[False, True], ids=["whole-flats", "fractional-flats"])
def fractional(request):
    _FRACTIONAL[0] = request.param
    yield request.param
    _FRACTIONAL[0] = False


def _random_effect(rng: random.Random) -> BoardEffect:
    kind = rng.choice(["stat", "stat", "rate", "rate", "skill_effect_up",
                       "song_score_bonus", "activation_rate_up", "cooldown_shorten",
                       "ignored"])
    if kind in ("activation_rate_up", "cooldown_shorten"):
        scope = "member"
    elif kind in ("song_score_bonus", "ignored"):
        scope = "account"
    else:
        scope = rng.choice(["leader", "member", "grouping", "account"])
    return BoardEffect(
        family=f"fam-{kind}",
        kind=kind,
        stat=rng.choice(["all", *STAT_NAMES]) if kind in ("stat", "rate") else "",
        value=(rng.randrange(5, 120) + (0.5 if _FRACTIONAL[0] and rng.random() < 0.3 else 0.0)
               if kind == "stat"
               else rng.randrange(1, 300) / 1000.0),
        scope=scope,
        grouping=rng.choice(_GROUPS) if scope == "grouping" else None,
        song_scope=(rng.choice(["all", "solo", "group", None])
                    if kind == "song_score_bonus" else None),
        conditional=kind != "song_score_bonus" and rng.random() < 0.2,
    )


def _random_board(rng: random.Random, holomem: str) -> CharacterBoard:
    return CharacterBoard(holomem=holomem, nodes=tuple(
        BoardNode(node_id=f"{rng.choice('RGBY')}-{i:03d}",
                  effects=tuple(_random_effect(rng) for _ in range(rng.choice([1, 1, 2]))),
                  connect_multiplier=rng.choice([1.0, 1.0, 1.55, 2.4, 1.3]))
        for i in range(rng.randrange(0, 14))))


def _random_model(rng: random.Random) -> BoardModel:
    names = rng.sample(_HOLOMEM, rng.randrange(1, len(_HOLOMEM) + 1))
    return BoardModel(characters={name: _random_board(rng, name) for name in names})


def _cards():
    return [make_card(f"card-{n:05d}-5-uniq-0000-00", member=_HOLOMEM[n],
                      stats=_STATS[n],
                      generations=frozenset({["Myth", "3期生", "grp-gen_3", "holoX"][n % 4]}))
            for n in range(12)]


def _reference(model: BoardModel) -> dict:
    """The derived fields, by the plain nested walks."""
    leader_scoped = frozenset(
        holomem for holomem, board in model.characters.items()
        if any(e.scope == "leader" for node in board.nodes for e in node.effects))
    song: dict[str, float] = {}
    seu, cond_seu, cond_stat = [], [], []
    by_holomem: dict[str, dict[str, float]] = {}
    uptime: dict[str, tuple[float, float]] = {}
    for holomem, board in model.characters.items():
        rate = cooldown = 0.0
        for node in board.nodes:
            for raw in node.effects:
                effect = raw.scaled(node.connect_multiplier)
                if effect.kind == "song_score_bonus":
                    key = effect.song_scope or "all"
                    song[key] = song.get(key, 0.0) + effect.value
                    per = by_holomem.setdefault(holomem, {})
                    per[key] = per.get(key, 0.0) + effect.value
                if effect.kind == "skill_effect_up":
                    (cond_seu if effect.conditional else seu).append((holomem, effect))
                if effect.kind in ("stat", "rate") and effect.conditional:
                    cond_stat.append((holomem, effect))
                if raw.kind in ("activation_rate_up", "cooldown_shorten") and not raw.conditional:
                    if effect.kind == "activation_rate_up":
                        rate += effect.value
                    else:
                        cooldown += effect.value
        if rate or cooldown:
            uptime[holomem] = (rate, cooldown)
    return {
        "_leader_scoped": leader_scoped,
        "_score_bonus_rate": min(song.get("all", 0.0), SONG_ALL_CAP_PERMIL / 1000.0),
        "_skill_effect_up_terms": tuple(seu),
        "_conditional_skill_effect_up_terms": tuple(cond_seu),
        "_conditional_stat_terms": tuple(cond_stat),
        "_song_score_bonus_by_holomem": by_holomem,
        "_uptime": uptime,
    }


def _reference_terms(model: BoardModel, card, leader_holomem):
    """`_terms`, by the plain nested walk."""
    flat: dict[str, float] = {}
    rate: dict[str, float] = {}
    grouping: float | None = None
    for holomem, board in model.characters.items():
        for node in board.nodes:
            for raw in node.effects:
                effect = raw.scaled(node.connect_multiplier)
                if effect.kind in ("ignored", "song_score_bonus", "skill_effect_up"):
                    continue
                if effect.scope == "leader":
                    if leader_holomem is None or leader_holomem != holomem:
                        continue
                elif not _reaches(effect, holomem, card):
                    continue
                if effect.conditional:
                    continue
                if effect.kind == "stat":
                    bucket = flat
                elif effect.kind == "rate":
                    bucket = rate
                else:
                    continue
                if effect.kind == "stat" and effect.scope == "grouping" and effect.stat == "all":
                    grouping = (grouping or 0.0) + effect.value
                    continue
                for stat in (STAT_NAMES if effect.stat == "all" else (effect.stat,)):
                    bucket[stat] = bucket.get(stat, 0.0) + effect.value
    if grouping is not None:
        # the grouping greens a card receives are capped, then reach every stat
        for stat in STAT_NAMES:
            flat[stat] = flat.get(stat, 0.0) + min(grouping, float(GROUPING_ALL_CAP))
    return flat, rate


def test_every_derived_field_equals_the_nested_walk_on_random_boards():
    rng = random.Random(20261013)
    for _ in range(300):
        model = _random_model(rng)
        for name, value in _reference(model).items():
            assert getattr(model, name) == value, name


def test_terms_and_power_bonus_equal_the_nested_walk_and_resolve_on_random_boards(fractional):
    rng = random.Random(1013)
    cards = _cards()
    exact = set()
    for _ in range(150):
        model = _random_model(rng)
        exact.add(model._flat_exact)
        leader = rng.choice([None, *cards])
        leader_holomem = leader.member if leader is not None else None
        members = tuple(rng.sample(cards, 5))
        for card in members:
            got = model._terms(card, leader_holomem)
            assert got == _reference_terms(model, card, leader_holomem)
            # dict ORDER is the order the first addend arrived in — the same walk
            assert [list(d) for d in got] == \
                [list(d) for d in _reference_terms(model, card, leader_holomem)]
        contribution = resolve(model, leader, members)
        assert model.power_bonus_for(members, leader_holomem) == \
            contribution.power_bonus(members)
        assert model.skill_effect_up_for(members, leader_holomem) == \
            contribution.skill_effect_up
    # the fixture did select the path it names: whole flats always sum per
    # board, and fractional ones push (nearly) every model off that path
    assert exact == {True} if not fractional else False in exact


def test_digests_shared_across_models_give_the_same_model_as_fresh_ones(fractional):
    """What `PlanCache` does: the same board objects and digests under many
    models, a few boards swapped per model. The shared digests carry a per-card
    memo from one model to the next; nothing may leak between them."""
    rng = random.Random(77)
    cards = _cards()
    bench = {name: _random_board(rng, name) for name in _HOLOMEM}
    digests = {name: BoardDigest(board) for name, board in bench.items()}
    for _ in range(120):
        chars, given = dict(bench), dict(digests)
        for name in rng.sample(_HOLOMEM, rng.randrange(0, 6)):
            chars[name] = _random_board(rng, name)
            given[name] = BoardDigest(chars[name])
        shared = BoardModel(characters=chars, _digests=given)
        fresh = BoardModel(characters=chars)
        assert shared == fresh
        for name, value in _reference(fresh).items():
            assert getattr(shared, name) == value, name
        leader_holomem = rng.choice([None, *_HOLOMEM])
        for card in rng.sample(cards, 5):
            assert shared._terms(card, leader_holomem) == \
                _reference_terms(fresh, card, leader_holomem)


def test_a_digest_of_another_board_is_ignored_not_trusted():
    rng = random.Random(5)
    board, other = _random_board(rng, "Chr1"), _random_board(rng, "Chr1")
    while other == board or not other.nodes:
        other = _random_board(rng, "Chr1")
    # an EQUAL copy is still another object: identity, not content, is the check
    twin = CharacterBoard(holomem=board.holomem, nodes=board.nodes)
    for stale in (BoardDigest(other), BoardDigest(twin)):
        model = BoardModel(characters={"Chr1": board}, _digests={"Chr1": stale})
        assert model._digests["Chr1"] is not stale
        assert model._digests["Chr1"].board is board
        for name, value in _reference(model).items():
            assert getattr(model, name) == value, name


def test_an_uptime_effect_off_member_scope_still_refuses_to_build():
    bad = BoardEffect(family="f", kind="cooldown_shorten", stat="", value=0.1,
                      scope="account")
    board = CharacterBoard(holomem="Chr1", nodes=(BoardNode(node_id="B-001", effects=(bad,)),))
    with pytest.raises(BoardError, match="Chr1 node B-001: cooldown_shorten at scope 'account'"):
        BoardModel(characters={"Chr1": board})
    # and a digest built ahead of the model does not swallow it
    with pytest.raises(BoardError, match="cooldown_shorten at scope 'account'"):
        BoardModel(characters={"Chr1": board}, _digests={"Chr1": BoardDigest(board)})


def test_the_plan_cache_reuses_bench_digests_and_developed_boards_across_teams():
    cache = PlanCache.from_team(_team(1, [2, 3, 4, 5, 6]), make_scoring_config())
    teams = [_team(1, [2, 3, 4, 5, 6]), _team(1, [2, 3, 4, 5, 7]),
             _team(1, [2, 3, 4, 5, 6])]
    models = []
    for team in teams:
        (plain, _pp), (fast, _pf) = _both_boards(
            team, cache, budget=453, sings=False, connect=CONNECT, bench=BENCH)
        assert fast == plain
        for card in team.members:
            assert fast._terms(card, team.leader.member) == \
                plain._terms(card, team.leader.member)
            assert fast._terms(card, team.leader.member) == \
                _reference_terms(plain, card, team.leader.member)
        models.append((team, fast))
    (t0, m0), (_t1, m1), (_t2, m2) = models
    in_team = {c.member for c in (t0.leader, *t0.members)}
    bench_only = [name for name in m0.characters if name not in in_team]
    assert bench_only
    for name in bench_only:                 # the bench: one digest for every team
        assert m0._digests[name] is m2._digests[name]
        assert m0._digests[name].board is m0.characters[name]
    for name in in_team:                    # the same team again: its boards are reused
        assert m0.characters[name] is m2.characters[name]
        assert m0._digests[name] is m2._digests[name]
    assert cache._team_boards
    assert math.isfinite(m1.power_bonus_for(teams[1].members, teams[1].leader.member))


def test_flat_exact_is_refused_for_a_fractional_or_an_enormous_flat_value():
    def board(value, kind="stat"):
        effect = BoardEffect(family="f", kind=kind, stat="all", value=value, scope="account")
        return CharacterBoard(holomem="Chr1", nodes=(BoardNode(node_id="G-001", effects=(effect,)),))

    assert BoardDigest(board(50.0)).flat_exact
    assert BoardDigest(board(78)).flat_exact            # `scaled` ceils to an int
    assert not BoardDigest(board(77.5)).flat_exact
    assert not BoardDigest(board(float(2**40))).flat_exact
    assert BoardDigest(board(0.01, kind="rate")).flat_exact   # rates are never summed
    whole, half = board(50.0), board(77.5)
    assert BoardModel(characters={"a": whole})._flat_exact
    assert not BoardModel(characters={"a": whole, "b": half})._flat_exact


def test_terms_over_a_bench_base_equal_the_nested_walk(fractional):
    """`BoardModel._terms_over_base`: the bench's totals corrected by the
    boards a team replaces or appends, against the one nested walk — for every
    leader, including one whose board is the bench's own (her leader-scope
    effects are not in the bench totals) and one with no board at all."""
    rng = random.Random(4242)
    cards = _cards()
    bench_names = _HOLOMEM[:9]
    used = set()
    for _ in range(40):
        bench = {name: _random_board(rng, name) for name in bench_names}
        digests = {name: BoardDigest(board) for name, board in bench.items()}
        base = BoardBase(digests)
        for _ in range(8):
            chars, given = dict(bench), dict(digests)
            # replaced in place (first nine) and appended (the last three)
            for name in rng.sample(_HOLOMEM, rng.randrange(0, 7)):
                chars[name] = _random_board(rng, name)
                given[name] = BoardDigest(chars[name])
            model = BoardModel(characters=chars, _digests=given, _base=base)
            plain = BoardModel(characters=chars)
            used.add(model._base is not None)
            assert model == plain and plain._base is None
            for leader_holomem in (None, "Nobody", *rng.sample(_HOLOMEM, 4)):
                members = tuple(rng.sample(cards, 5))
                for card in members:
                    want = _reference_terms(plain, card, leader_holomem)
                    assert model._terms(card, leader_holomem) == want
                    assert plain._terms(card, leader_holomem) == want
                assert model.power_bonus_for(members, leader_holomem) == \
                    plain.power_bonus_for(members, leader_holomem)
    # whole flats take the base; one fractional flat anywhere switches it off
    assert (False in used) if fractional else used == {True}


def test_a_base_in_another_order_or_longer_than_the_model_is_dropped():
    rng = random.Random(9)
    bench = {name: _random_board(rng, name) for name in _HOLOMEM[:6]}
    digests = {name: BoardDigest(board) for name, board in bench.items()}
    base = BoardBase(digests)
    assert BoardModel(characters=bench, _digests=digests, _base=base)._base is base
    reordered = dict(reversed(list(bench.items())))
    shorter = dict(list(bench.items())[:4])
    cards = _cards()
    for chars in (reordered, shorter):
        model = BoardModel(characters=chars, _digests=digests, _base=base)
        assert model._base is None and not model._base_special
        for card in cards[:5]:
            assert model._terms(card, "Chr2") == _reference_terms(model, card, "Chr2")


def test_the_plan_cache_hands_every_model_the_same_bench_base():
    cache = PlanCache.from_team(_team(1, [2, 3, 4, 5, 6]), make_scoring_config())
    seen = []
    for team in (_team(1, [2, 3, 4, 5, 6]), _team(7, [2, 3, 8, 9, 10])):
        (plain, _pp), (fast, _pf) = _both_boards(
            team, cache, budget=453, sings=False, connect=CONNECT, bench=BENCH)
        assert fast._base is not None and plain._base is None
        seen.append(fast._base)
        for leader_holomem in (team.leader.member, None):
            for card in team.members:
                assert fast._terms(card, leader_holomem) == \
                    _reference_terms(plain, card, leader_holomem)
    assert seen[0] is seen[1]


def test_grouping_greens_a_card_receives_are_capped_at_900():
    """Four Gen-3 boards each giving 300 grouping all-stats: a Gen-3 card receives
    900 per stat, not 1,200 (`GROUPING_ALL_CAP`, the type-20 limit row). An
    account-wide green on top is NOT capped with them."""
    def grp(value):
        return BoardEffect(family="all_parameter_up_for_character_grouping", kind="stat",
                           stat="all", value=value, scope="grouping", grouping="grp-gen_3")
    green = BoardEffect(family="all_parameter_up", kind="stat", stat="all", value=55.0,
                        scope="account")
    names = _HOLOMEM[:4]
    model = BoardModel(characters={
        name: CharacterBoard(holomem=name, nodes=(
            BoardNode(node_id="G-011", effects=(grp(300.0),), connect_multiplier=1.0),
            BoardNode(node_id="G-001", effects=(green,), connect_multiplier=1.0)))
        for name in names})
    gen3 = next(c for c in _cards() if "grp-gen_3" in c.generations)
    other = next(c for c in _cards() if not ({"grp-gen_3", "3期生"} & c.generations))
    flat, _rate = model._terms(gen3, None)
    assert flat == {stat: 900.0 + 4 * 55.0 for stat in STAT_NAMES}
    assert model._terms(other, None)[0] == {stat: 4 * 55.0 for stat in STAT_NAMES}
    contribution = resolve(model, None, (gen3, other))
    assert contribution.flat_by_card[gen3.id] == flat
    # two boards stay under the cap: every point counts
    half = BoardModel(characters={n: model.characters[n] for n in names[:2]})
    assert half._terms(gen3, None)[0] == {stat: 600.0 + 2 * 55.0 for stat in STAT_NAMES}
