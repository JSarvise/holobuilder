"""The EXACT team-wide Connect assignment (`board_plan._connect_assignment_exact`
on `_max_weight_matching`): a maximum-weight bipartite matching between
(character, socket) rows and card columns, rather than a highest-lift-first
greedy. Pinned here: the Hungarian core on hand and
random matrices, matching >= greedy on 200 random instances, equality (ids
included) wherever the greedy is optimal, one hand board where the greedy is
provably suboptimal, determinism under input shuffling, and — when the shipped
skilltree dump is on disk — the additivity premise (no two sockets of a board
reach a common node)."""
from __future__ import annotations

import dataclasses
import itertools
import json
import random
from pathlib import Path

import pytest

from hbuilder.domain.board_plan import (
    _connect_assignment_exact, _max_weight_matching, _team_connect_assignment,
)

REPO = Path(__file__).resolve().parents[1]
SKILLTREE = REPO / "tools" / "recon" / "proxy" / "dumps" / "skilltree.json"


# ---------------------------------------------------------------------------
# reference: a highest-lift-first greedy (one card per socket,
# each card once; lowest unused id of the group), over the SAME triples
# ---------------------------------------------------------------------------
def _greedy(triples):
    order = sorted(triples, key=lambda t: (-t[0], t[1], t[2], t[5][0]))
    used_cards: set = set()
    used_sockets: set = set()
    total = 0.0
    placements = []
    for gain, chr_id, socket, permil, covered, card_ids in order:
        if (chr_id, socket) in used_sockets:
            continue
        card = next((c for c in card_ids if c not in used_cards), None)
        if card is None:
            continue
        used_cards.add(card)
        used_sockets.add((chr_id, socket))
        total += gain
        placements.append((chr_id, socket, card, gain))
    return total, placements


def _random_instance(rng: random.Random, *, integer=True, max_cap=3, small=False):
    """Random triples: 1-4 characters, 1-4 sockets each, 1-6 card groups of
    capacity 1..max_cap, small-integer gains (ties happen on purpose).
    `small`: 1-2 characters, <= 3 sockets, <= 4 groups (brute-forceable)."""
    n_chr = rng.randint(1, 2 if small else 4)
    n_groups = rng.randint(1, 4 if small else 6)
    groups = []
    k = 0
    for g in range(n_groups):
        ids = tuple(f"card-{k + i:03d}" for i in range(rng.randint(1, max_cap)))
        k += len(ids)
        groups.append((rng.choice([500, 1000, 2000]), ids))
    triples = []
    for c in range(n_chr):
        chr_id = f"chr-{c:05d}"
        for s in range(1, rng.randint(1, 3 if small else 4) + 1):
            socket = f"S-00{s}"
            for permil, ids in groups:
                gain = rng.randint(0, 6) if integer else rng.random() * 6
                if gain > 0:
                    covered = tuple(f"N-{s}{i}" for i in range(rng.randint(1, 3)))
                    triples.append((float(gain), chr_id, socket, permil, covered, ids))
    return triples


def _brute_force_optimum(triples) -> float:
    """Exhaustive over all valid placement sets (small instances only)."""
    rows = sorted({(t[1], t[2]) for t in triples})
    by_row = {r: [t for t in triples if (t[1], t[2]) == r] for r in rows}
    best = 0.0
    caps = {t[5]: len(t[5]) for t in triples}

    def rec(i, used, total):
        nonlocal best
        if i == len(rows):
            best = max(best, total)
            return
        rec(i + 1, used, total)  # row unmatched
        for t in by_row[rows[i]]:
            if used.get(t[5], 0) < caps[t[5]]:
                used[t[5]] = used.get(t[5], 0) + 1
                rec(i + 1, used, total + t[0])
                used[t[5]] -= 1

    rec(0, {}, 0.0)
    return best


def _check_valid(triples, placements):
    used_cards = [p.card for p in placements]
    assert len(used_cards) == len(set(used_cards)), "a card placed twice"
    sockets = [(p.chr, p.socket) for p in placements]
    assert len(sockets) == len(set(sockets)), "a socket used twice"
    known = {(t[1], t[2], t[5]): t for t in triples}
    for p in placements:
        t = next(t for t in triples if t[1] == p.chr and t[2] == p.socket
                 and p.card in t[5] and t[0] == p.value)
        assert p.multiplier == 1.0 + t[3] / 1000.0
        assert known[(t[1], t[2], t[5])] is t


# ---------------------------------------------------------------------------
# the Hungarian core
# ---------------------------------------------------------------------------
def test_hungarian_hand_case_where_greedy_is_suboptimal():
    # greedy: (0,0)=10 then row 1 gets nothing -> 10; optimum 9 + 9 = 18.
    w = [[10.0, 9.0], [9.0, 0.0]]
    assert _max_weight_matching(w) == {0: 1, 1: 0}


def test_hungarian_leaves_rows_unmatched_and_respects_capacity():
    # a column of capacity 2 serves two rows; the third row has nothing.
    w = [[5.0], [4.0], [3.0]]
    assert _max_weight_matching(w, [2]) == {0: 0, 1: 0}
    assert _max_weight_matching(w, [1]) == {0: 0}
    assert _max_weight_matching([[0.0, 0.0]], [1, 1]) == {}
    assert _max_weight_matching([]) == {}


def test_hungarian_equals_brute_force_on_random_matrices():
    """Capacities up to 3 and rows up to 5, so saturated columns hold several
    rows and the augmenting path must walk back through them."""
    rng = random.Random(1)
    for _ in range(300):
        n, m = rng.randint(1, 5), rng.randint(1, 4)
        w = [[float(rng.randint(0, 5)) for _ in range(m)] for _ in range(n)]
        caps = [rng.randint(1, 3) for _ in range(m)]
        got = _max_weight_matching(w, caps)
        # valid
        for j in range(m):
            assert sum(1 for c in got.values() if c == j) <= caps[j]
        total = sum(w[i][j] for i, j in got.items())
        # brute force over row->column-or-none
        best = 0.0
        for choice in itertools.product([-1, *range(m)], repeat=n):
            if any(choice.count(j) > caps[j] for j in range(m)):
                continue
            best = max(best, sum(w[i][j] for i, j in enumerate(choice) if j >= 0))
        assert total == best


# ---------------------------------------------------------------------------
# the assignment on triples
# ---------------------------------------------------------------------------
def test_matching_beats_or_equals_the_greedy_on_200_random_instances():
    rng = random.Random(20260818)
    strictly_better = 0
    for _ in range(200):
        triples = _random_instance(rng)
        g_total, _g = _greedy(triples)
        _m, placements = _connect_assignment_exact(triples)
        _check_valid(triples, placements)
        total = sum(p.value for p in placements)
        assert total >= g_total
        strictly_better += total > g_total
    # the greedy is NOT always optimal on these instances (else the test is vacuous)
    assert strictly_better > 0


def test_matching_equals_the_brute_force_optimum_on_small_instances():
    rng = random.Random(20260819)
    for _ in range(200):
        triples = _random_instance(rng, small=True)
        _m, placements = _connect_assignment_exact(triples)
        _check_valid(triples, placements)
        assert sum(p.value for p in placements) == _brute_force_optimum(triples)


def test_matching_equals_the_greedy_ids_included_when_the_greedy_is_optimal():
    """No contention (every column's capacity >= the number of rows): the
    greedy is optimal, and the exact result must be the greedy's placements
    exactly — same cards, same order, same values."""
    rng = random.Random(7)
    seen_nonempty = 0
    for _ in range(200):
        triples = _random_instance(rng, integer=False, max_cap=1)
        rows = {(t[1], t[2]) for t in triples}
        # widen every group to capacity >= rows so no card is ever contended
        widened = []
        for t in triples:
            ids = tuple(f"{t[5][0]}-{i}" for i in range(len(rows)))
            widened.append((t[0], t[1], t[2], t[3], t[4], ids))
        g_total, g_pl = _greedy(widened)
        _m, placements = _connect_assignment_exact(widened)
        assert [(p.chr, p.socket, p.card, p.value) for p in placements] == g_pl
        seen_nonempty += bool(placements)
    assert seen_nonempty > 100


def test_matching_is_deterministic_under_input_shuffling():
    rng = random.Random(3)
    for _ in range(50):
        triples = _random_instance(rng, integer=False)
        ref = _connect_assignment_exact(triples)
        for _k in range(3):
            shuffled = list(triples)
            rng.shuffle(shuffled)
            got = _connect_assignment_exact(shuffled)
            assert got[0] == ref[0]
            assert [dataclasses.astuple(p) for p in got[1]] == \
                [dataclasses.astuple(p) for p in ref[1]]


def test_hand_board_where_the_greedy_is_suboptimal():
    """Two boards. Card X (shape (1,0)) is worth 10 on A and 9 on B; card Y
    (shape (0,1)) is worth 9 on A and nothing on B. Greedy: X->A (10), Y has
    no socket left -> 10. Exact: X->B + Y->A = 18."""
    positions_a = {"S-001": [0, 0], "N-1": [1, 0], "N-2": [0, 1]}
    positions_b = {"S-001": [0, 0], "N-1": [1, 0]}
    per_char = {
        "chr-A": (positions_a, frozenset({"N-1", "N-2"}), {"N-1": 10.0, "N-2": 9.0}),
        "chr-B": (positions_b, frozenset({"N-1"}), {"N-1": 9.0}),
    }
    connect = {"card-x": (((1, 0),), 1000), "card-y": (((0, 1),), 1000)}
    mults, placements = _team_connect_assignment(per_char, connect)
    assert sorted((p.chr, p.card, p.value) for p in placements) == [
        ("chr-A", "card-y", 9.0), ("chr-B", "card-x", 9.0)]
    assert mults == {"chr-A": {"N-2": 2.0}, "chr-B": {"N-1": 2.0}}
    # placements are emitted value-desc, then (chr, socket)
    assert [p.chr for p in placements] == ["chr-A", "chr-B"]


@pytest.mark.skipif(not SKILLTREE.is_file(), reason="skilltree dump not on disk")
def test_shipped_shapes_never_let_two_sockets_reach_one_node():
    """The additivity premise of the matching, on the real data: for every
    model, every pair of sockets and every pair of card shapes, the covered
    node sets are disjoint (so `max` per node never fires and the per-(socket,
    card) values add exactly)."""
    st = json.loads(SKILLTREE.read_text("utf-8"))
    extents, connect, card_connect = st["extents"], st["connect"], st["card_connect"]
    shapes = sorted({tuple(map(tuple, extents[connect[e]["area"]]))
                     for e in card_connect.values()
                     if e in connect and connect[e].get("area") in extents})
    assert shapes
    checked = 0
    for positions in st["models"].values():
        pos_to_node = {tuple(p): n for n, p in positions.items()}
        sockets = sorted(n for n in positions if n.startswith("S"))
        cover = {}
        for s in sockets:
            sx, sy = positions[s]
            for sh in shapes:
                cover[(s, sh)] = frozenset(
                    pos_to_node[(sx + dx, sy + dy)] for dx, dy in sh
                    if (sx + dx, sy + dy) in pos_to_node)
        for (s1, a), (s2, b) in itertools.combinations(cover, 2):
            if s1 != s2:
                checked += 1
                assert not (cover[(s1, a)] & cover[(s2, b)]), (s1, s2)
    assert checked > 0


# ---------------------------------------------------------------------------
# incumbents: on a tie the account's own placement stays
# ---------------------------------------------------------------------------
def _tie_boards():
    positions = {"S-001": [0, 0], "N-1": [1, 0]}
    per_char = {c: (positions, frozenset({"N-1"}), {"N-1": 5.0}) for c in ("chr-A", "chr-B")}
    return per_char, {"card-x": (((1, 0),), 1000)}


@pytest.mark.parametrize("home", ["chr-A", "chr-B"])
def test_a_card_stays_where_it_is_when_moving_it_gains_nothing(home):
    """A card that lifts greens worth exactly the same on two boards must not
    be moved between them -- a move worth nothing. One card, two boards of
    equal value: it stays on the board that holds it, whichever that is."""
    per_char, connect = _tie_boards()
    _mults, placements = _team_connect_assignment(
        per_char, connect, incumbent={(home, "S-001"): "card-x"})
    assert [(p.chr, p.card) for p in placements] == [(home, "card-x")]


def test_an_incumbent_never_beats_a_real_gain():
    per_char, connect = _tie_boards()
    per_char["chr-A"] = (per_char["chr-A"][0], per_char["chr-A"][1], {"N-1": 5.001})
    _mults, placements = _team_connect_assignment(
        per_char, connect, incumbent={("chr-B", "S-001"): "card-x"})
    assert [(p.chr, p.card) for p in placements] == [("chr-A", "card-x")]


def test_the_incumbent_card_of_an_equivalent_group_is_the_one_named():
    """Two interchangeable cards (same shape and permil): the board that holds card-2
    keeps card-2, not the group's lowest id."""
    per_char, _ = _tie_boards()
    connect = {"card-1": (((1, 0),), 1000), "card-2": (((1, 0),), 1000)}
    _mults, placements = _team_connect_assignment(
        per_char, connect, incumbent={("chr-B", "S-001"): "card-2"})
    assert sorted((p.chr, p.card) for p in placements) == [("chr-A", "card-1"),
                                                          ("chr-B", "card-2")]


def test_incumbents_never_change_the_optimum_on_random_instances():
    rng = random.Random(20261022)
    for _ in range(300):
        triples = _random_instance(rng)
        rows = sorted({(t[1], t[2]) for t in triples})
        cards = sorted({c for t in triples for c in t[5]})
        incumbent = {r: rng.choice(cards) for r in rows if rng.random() < 0.6}
        base = sum(p.value for p in _connect_assignment_exact(triples)[1])
        _m, placed = _connect_assignment_exact(triples, incumbent=incumbent)
        _check_valid(triples, placed)
        assert sum(p.value for p in placed) == pytest.approx(base, rel=1e-9, abs=1e-9)


def test_the_cached_path_keeps_the_incumbent_too():
    """The plan-cache path (`_team_connect_assignment_grouped`, the default 'boards as
    they are' detail) breaks ties the same way: three boards with equal greens, the
    cards rotated among them only when nothing says where they are."""
    from hbuilder.domain.board_plan import _team_connect_assignment_grouped

    cards = ("card-1", "card-2", "card-3")
    per_char = [(c, [("S-001", 1000, ("G-001",), (card,)) for card in cards], {"G-001": 7.0})
                for c in ("chr-A", "chr-B", "chr-C")]
    home = {("chr-A", "S-001"): "card-3", ("chr-B", "S-001"): "card-1",
            ("chr-C", "S-001"): "card-2"}
    _m, placed = _team_connect_assignment_grouped(per_char, home)
    assert {(p.chr, p.socket): p.card for p in placed} == home
