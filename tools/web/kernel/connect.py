"""ctypes binding for `connect.c` — the team-wide Connect socket assignment and the
team board assembly on it, compiled.

Web/recon side only: nothing under `src/hbuilder/` imports this. The domain
exposes two seams (`board_plan.CONNECT_KERNEL`, `board_plan.BOARD_KERNEL`) and the
caller injects the kernels, exactly as `notewalk.py` does for the note walk.

    python tools/web/kernel/connect.py --build      # compile the shared library

What is compiled is `node_values_from_recipes` + the candidate gains + the exact
matching (`assign`), and for `assemble` that matching under the song bonus's cap:
the checks before and after it and the capped matching with its bisection. What
stays in Python is everything with a name in it (which card goes where, the
multipliers per node, the boards built from them). A plan is packed into C arrays
ONCE and reused for every team that carries it.

Compiled with `cbuild.py`'s flags, so the result is bit-identical to the Python it replaces.
"""

from __future__ import annotations

import ctypes
import re
import sys
import threading
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hbuilder.domain.board_plan import ConnectTriple
    from hbuilder.domain.card import Card

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "connect.c"
INCLUDED = HERE / "jointmatch.c"  # connect.c includes it: a change there rebuilds this
if str(HERE) not in sys.path:  # loaded by file path in a worker too
    sys.path.insert(0, str(HERE))
import cbuild  # noqa: E402

LIBRARY = cbuild.library(HERE, "connect")
MAX_CHARS = 8
MAX_MEMBERS = 8
MAX_NODES = 512
MAX_ENTRIES = 1024
MAX_ROWS = 64
SOCKETS = 64
COL_IDS = 2048

K_STAT, K_RATE, K_SONG, K_SKILL_EFFECT_UP, K_UPTIME, K_OTHER = range(6)
S_LEADER, S_MEMBER, S_GROUPING, S_ACCOUNT = range(4)
F_CONDITIONAL, F_SONG_SCOPED, F_SOLO, F_GROUP = 1, 2, 4, 8
_SONG_MASK = {"": F_SOLO | F_GROUP, "solo": F_SOLO, "group": F_GROUP}
_KIND = {
    "stat": K_STAT,
    "rate": K_RATE,
    "song_score_bonus": K_SONG,
    "skill_effect_up": K_SKILL_EFFECT_UP,
    "activation_rate_up": K_UPTIME,
    "cooldown_shorten": K_UPTIME,
}
_SCOPE = {"leader": S_LEADER, "member": S_MEMBER, "grouping": S_GROUPING}
_SOCKET_RE = re.compile(r"^S-(\d{3})$")


class Eff(ctypes.Structure):
    _fields_ = [
        ("kind", ctypes.c_int32),
        ("scope", ctypes.c_int32),
        ("stat", ctypes.c_int32),
        ("flags", ctypes.c_int32),
        ("value", ctypes.c_double),
    ]


class Node(ctypes.Structure):
    _fields_ = [
        ("is_override", ctypes.c_int32),
        ("gen", ctypes.c_int32),
        ("eff_start", ctypes.c_int32),
        ("eff_count", ctypes.c_int32),
        ("v", ctypes.c_double),
    ]


class Entry(ctypes.Structure):
    _fields_ = [
        ("socket", ctypes.c_int32),
        ("col", ctypes.c_int32),
        ("cap", ctypes.c_int32),
        ("cov_start", ctypes.c_int32),
        ("cov_count", ctypes.c_int32),
        ("pad_", ctypes.c_int32),
        ("permil", ctypes.c_double),
    ]


class Plan(ctypes.Structure):
    _fields_ = [
        ("n_nodes", ctypes.c_int32),
        ("n_entries", ctypes.c_int32),
        ("nodes", ctypes.POINTER(Node)),
        ("effs", ctypes.POINTER(Eff)),
        ("entries", ctypes.POINTER(Entry)),
        ("cov", ctypes.POINTER(ctypes.c_int32)),
    ]


def build(force: bool = False) -> Path:
    """Compile the kernel if the library is missing or older than its sources."""
    stale = LIBRARY.exists() and LIBRARY.stat().st_mtime < INCLUDED.stat().st_mtime
    return cbuild.build(SOURCE, LIBRARY, "connect", force or stale)


_lib: ctypes.CDLL | None = None


def load() -> ctypes.CDLL:
    """The loaded library, built on first use. Raises if it cannot be built.

    `PyDLL`, not `CDLL`: the kernel keeps its work arrays in static storage, so
    the call must not release the GIL (a few microseconds; nothing to gain)."""
    global _lib
    if _lib is None:
        lib = ctypes.PyDLL(str(build()))
        lib.hd_connect.restype = ctypes.c_int
        lib.hd_connect.argtypes = [
            ctypes.c_int,
            ctypes.POINTER(Plan),
            ctypes.POINTER(ctypes.c_int32),
            ctypes.POINTER(ctypes.c_int32),
            ctypes.POINTER(ctypes.c_int32),
            ctypes.POINTER(ctypes.c_double),
            ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_double),
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.c_double,
            ctypes.c_double,
            ctypes.c_double,
            ctypes.c_double,
            ctypes.POINTER(ctypes.c_int32),
            ctypes.POINTER(ctypes.c_int32),
            ctypes.POINTER(ctypes.c_double),
        ]
        i32p, f64p = ctypes.POINTER(ctypes.c_int32), ctypes.POINTER(ctypes.c_double)
        lib.hd_assemble.restype = ctypes.c_int
        lib.hd_assemble.argtypes = [
            ctypes.c_int,
            ctypes.POINTER(Plan),
            ctypes.POINTER(Yel),
            i32p,
            i32p,
            i32p,
            i32p,
            f64p,
            ctypes.c_int,
            ctypes.c_int,
            f64p,
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.c_double,
            ctypes.c_double,
            ctypes.c_double,
            ctypes.c_double,
            ctypes.c_int,
            i32p,
            f64p,
            ctypes.c_double,
            ctypes.c_int,
            ctypes.POINTER(_U8P),
            ctypes.c_double,
            ctypes.c_int,
            ctypes.c_int,
            i32p,
            i32p,
            f64p,
            i32p,
            i32p,
            i32p,
        ]
        lib.hd_set_col_rank.restype = None
        lib.hd_set_col_rank.argtypes = [ctypes.POINTER(ctypes.c_int32), ctypes.c_int]
        lib.hd_py_sum.restype = ctypes.c_double
        lib.hd_py_sum.argtypes = [f64p, ctypes.c_int]
        u8p = ctypes.POINTER(ctypes.c_uint8)
        lib.hd_joint.restype = ctypes.c_int
        lib.hd_joint.argtypes = [
            ctypes.c_int,
            ctypes.POINTER(Plan),
            i32p,
            i32p,
            i32p,
            f64p,
            ctypes.c_int,
            ctypes.c_int,
            f64p,
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.c_double,
            ctypes.c_double,
            ctypes.c_double,
            ctypes.c_double,
            i32p,
            i32p,
            ctypes.POINTER(_U8P),
            ctypes.c_double,
            ctypes.POINTER(Yel),
            i32p,
            ctypes.c_void_p,
            i32p,
            i32p,
            i32p,
            f64p,
            u8p,
            i32p,
            i32p,
            i32p,
            ctypes.c_int,
            f64p,
            f64p,
            i32p,
            ctypes.c_int,
            i32p,
            i32p,
            i32p,
            i32p,
            ctypes.c_double,
            ctypes.c_int,
            i32p,
            i32p,
            f64p,
            f64p,
        ]
        _lib = lib
    return _lib


if __name__ == "__main__":
    print(build(force="--build" in sys.argv))


# --- packing -----------------------------------------------------------------

_domain = None


def _names():
    """`(STAT_NAMES, generation -> bit, grouping regex, grouping -> generation)`
    from the domain, read once. The generation bits are fixed up front from the
    domain's own table, so a card's mask never goes stale."""
    global _domain
    if _domain is None:
        from hbuilder.domain import board

        generations = sorted(set(board._GENERATION_BY_GROUPING.values()))
        if len(generations) > 64:
            raise RuntimeError("more than 64 generations; the mask does not fit")
        _domain = (
            tuple(board.STAT_NAMES),
            {g: i for i, g in enumerate(generations)},
            board._GROUPING_RE,
            board._GENERATION_BY_GROUPING,
        )
    return _domain


# Card groups (`entries[..][3]`, id-sorted tuples) -> a STABLE id, ids only added: a packed
# plan names its groups by id. The column order `_connect_assignment_exact` sorts into is each
# group's rank among every group seen, in tuple order; the library holds it per id
# (`hd_set_col_rank`), so a new group re-ranks there and no packed plan changes. (Plans viewed
# without the bench's held cards bring new groups all through a search.)
_col_id: dict[tuple, int] = {}
# bumped when `_col_id` starts again (a process that saw more groups than the library ranks):
# a call re-collects its plans if it changed meanwhile, so it never mixes the two numberings
_col_generation = [0]
# id(asm) -> (asm, recipes, Plan, keep-alive arrays, entries), or `None` in
# place of the Plan for a plan this kernel cannot express.
_packs: dict[int, tuple] = {}
# id(card) -> (card, three stats as floats, generation mask)
_cards: dict[int, tuple] = {}


def _register_groups(groups) -> None:
    """Give each unseen card group an id and hand the library every group's rank."""
    groups = set(groups)
    unseen = sorted(groups - _col_id.keys())
    if not unseen:
        return
    if len(_col_id) + len(unseen) > COL_IDS:
        # more groups than the library ranks: number them again from this plan's
        _col_id.clear()
        _packs.clear()
        _joint_cols.clear()
        _col_generation[0] += 1
        unseen = sorted(groups)
    for ids in unseen:
        _col_id[ids] = len(_col_id)
    _push_ranks()


def _push_ranks() -> None:
    """Hand the library each group id's rank among `_col_id`'s groups, in tuple order."""
    if len(_col_id) > COL_IDS:
        return
    ranks = [0] * len(_col_id)
    for rank, ids in enumerate(sorted(_col_id)):
        ranks[_col_id[ids]] = rank
    load().hd_set_col_rank((ctypes.c_int32 * max(1, len(ranks)))(*ranks), len(ranks))


def _pack(asm, recipes):
    stat_names, gen_bit, grouping_re, generation_by_grouping = _names()
    _register_groups(e[3] for e in asm.entries)
    if len(_col_id) > COL_IDS or len(recipes) > MAX_NODES or len(asm.entries) > MAX_ENTRIES:
        return None
    node_index: dict[str, int] = {}
    nodes, effs = [], []
    for node_id, override, node_effs in recipes:
        node_index[node_id] = len(nodes)
        if override:
            match = grouping_re.match(override.get("tgt", ""))
            generation = generation_by_grouping.get(match.group("grouping")) if match else None
            nodes.append(
                (1, gen_bit[generation] if generation is not None else -1, 0, 0, float(override.get("v", 0)))
            )
            continue
        start = len(effs)
        for eff in node_effs:
            kind = _KIND.get(eff.kind, K_OTHER)
            stat = -1
            if kind in (K_STAT, K_RATE) and eff.stat != "all":
                if eff.stat not in stat_names:
                    return None
                stat = stat_names.index(eff.stat)
            flags = (
                (F_CONDITIONAL if eff.conditional else 0)
                | (F_SONG_SCOPED if eff.song_scope in ("solo", "group") else 0)
                | (F_SOLO if eff.song_scope == "solo" else F_GROUP if eff.song_scope == "group" else 0)
            )
            effs.append((kind, _SCOPE.get(eff.scope, S_ACCOUNT), stat, flags, float(eff.value)))
        nodes.append((0, -1, start, len(effs) - start, 0.0))
    entries, cov = [], []
    for socket, permil, covered, card_ids in asm.entries:
        match = _SOCKET_RE.match(socket)
        if match is None or int(match.group(1)) >= SOCKETS:
            return None
        start = len(cov)
        cov.extend(node_index.get(node, -1) for node in covered)
        entries.append(
            (
                int(match.group(1)),
                _col_id[card_ids],
                len(card_ids),
                start,
                len(cov) - start,
                0,
                float(permil),
            )
        )
    c_nodes = (Node * max(1, len(nodes)))(*nodes)
    c_effs = (Eff * max(1, len(effs)))(*effs)
    c_entries = (Entry * max(1, len(entries)))(*entries)
    c_cov = (ctypes.c_int32 * max(1, len(cov)))(*cov)
    plan = Plan(len(nodes), len(entries), c_nodes, c_effs, c_entries, c_cov)
    return plan, (c_nodes, c_effs, c_entries, c_cov)


def _plan_for(asm, recipes):
    hit = _packs.get(id(asm))
    if hit is None or hit[0] is not asm or hit[1] is not recipes:
        packed = _pack(asm, recipes)
        if len(_packs) > 50_000:
            _packs.clear()
        hit = (asm, recipes, packed[0] if packed else None, packed[1] if packed else None)
        _packs[id(asm)] = hit
    return hit


def _card(card):
    hit = _cards.get(id(card))
    if hit is None or hit[0] is not card:
        stat_names, gen_bit, _re, _by = _names()
        mask = 0
        for generation in card.generations:
            bit = gen_bit.get(generation)
            if bit is not None:
                mask |= 1 << bit
        if len(_cards) > 50_000:
            _cards.clear()
        hit = (card, tuple(float(card.stat(name)) for name in stat_names), mask)
        _cards[id(card)] = hit
    return hit


# --- the adapter `board_plan.CONNECT_KERNEL` is set to -------------------------


def assign(
    inputs: Sequence[Any],
    members: Sequence[Card],
    *,
    overall_power: float,
    bonus_permil: float,
    k_constant: float,
    active_pool_rate: float,
    song_scope: str = "",
) -> list[ConnectTriple] | None:
    """The chosen Connect placements for one team, as the `ConnectTriple`s
    `board_plan._connect_assignment_exact` would have matched — `(gain, chr_id,
    socket, permil, covered, card_ids)` — or `None` to make the caller run its
    own Python (a team or a plan this kernel does not express).

    `inputs` is `board_plan`'s per-character list: `(chr_id, AssembledPlan,
    recipes, is_leader, own_card, singer_weight)`; `song_scope` the song's scope
    (`node_values_from_recipes`: only that scope's yellows pay; "" both)."""
    count = len(inputs)
    member_count = len(members)
    if count > MAX_CHARS or member_count > MAX_MEMBERS or len(_names()[0]) != 3:
        return None
    if count == 0:
        return []
    lib = load()
    ranks = {chr_id: rank for rank, chr_id in enumerate(sorted({i[0] for i in inputs}))}
    while True:
        generation = _col_generation[0]
        hits = [_plan_for(i[1], i[2]) for i in inputs]
        if _col_generation[0] == generation:
            break
    if any(hit[2] is None for hit in hits):
        return None
    # `hits` stays referenced until the call returns: it owns the arrays the
    # copied `Plan` structs point into.
    plans = (Plan * count)(*(hit[2] for hit in hits))
    stats: list[float] = []
    masks: list[int] = []
    for card in members:
        _c, card_stats, mask = _card(card)
        stats.extend(card_stats)
        masks.append(mask)
    own: list[int] = []
    for i in inputs:
        own_card = i[4]
        index = -1
        if own_card is not None:
            for position, member in enumerate(members):
                if member is own_card or member == own_card:
                    index = position
                    break
        own.append(index)
    int_array = ctypes.c_int32 * count
    out_char = (ctypes.c_int32 * MAX_ROWS)()
    out_entry = (ctypes.c_int32 * MAX_ROWS)()
    out_gain = (ctypes.c_double * MAX_ROWS)()
    chosen = lib.hd_connect(
        count,
        plans,
        int_array(*(ranks[i[0]] for i in inputs)),
        int_array(*(1 if i[3] else 0 for i in inputs)),
        int_array(*own),
        (ctypes.c_double * count)(*(float(i[5]) for i in inputs)),
        _SONG_MASK.get(song_scope, 0),
        member_count,
        (ctypes.c_double * max(1, len(stats)))(*stats),
        (ctypes.c_uint64 * max(1, member_count))(*masks),
        (1.0 + bonus_permil / 1000.0) * k_constant,
        overall_power * k_constant,
        overall_power * ((1.0 + bonus_permil / 1000.0) * k_constant),  # d_us_per_song, same order
        active_pool_rate,
        out_char,
        out_entry,
        out_gain,
    )
    if chosen < 0:
        return None
    triples = []
    for k in range(chosen):
        chr_id, asm = inputs[out_char[k]][0], inputs[out_char[k]][1]
        socket, permil, covered, card_ids = asm.entries[out_entry[k]]
        triples.append((out_gain[k], chr_id, socket, permil, covered, card_ids))
    return triples


# --- the adapter `board_plan.BOARD_KERNEL` is set to ---------------------------


class Yel(ctypes.Structure):
    _fields_ = [
        ("n_pay", ctypes.c_int32),
        ("pad_", ctypes.c_int32),
        ("pay_idx", ctypes.POINTER(ctypes.c_int32)),
        ("pay_p", ctypes.POINTER(ctypes.c_double)),
        ("pay_sum", ctypes.c_double),
    ]


_U8P = ctypes.POINTER(ctypes.c_uint8)
# chr id -> the small code the team's boards and the off-team rows share
_chr_code: dict[str, int] = {}
# (id(recipes), id(rows)) -> (recipes, rows, Yel, keep-alive)
_yels: dict[tuple, tuple] = {}
# id(bench rows) -> (rows, codes, values, count)
_benches: dict[int, tuple] = {}
# (id(asm), id(incumbent)) -> (asm, incumbent, flags or None)
_ties: dict[tuple, tuple] = {}


def _code(chr_id: str) -> int:
    code = _chr_code.get(chr_id)
    if code is None:
        code = _chr_code[chr_id] = len(_chr_code)
    return code


def _yel_for(recipes, paying):
    """One board's paying yellows (`(rows, their sum)`, rows `(node, permil)` in the board's
    own order) as a `Yel`: each node's index among the packed plan's nodes (the recipes'
    order; -1 for a node no socket entry covers)."""
    rows, total = paying
    key = (id(recipes), id(rows))
    hit = _yels.get(key)
    if hit is not None and hit[0] is recipes and hit[1] is rows:
        return hit[2]
    index = {recipe[0]: i for i, recipe in enumerate(recipes)}
    idx = (ctypes.c_int32 * max(1, len(rows)))(*(index.get(node, -1) for node, _p in rows))
    permil = (ctypes.c_double * max(1, len(rows)))(*(float(p) for _node, p in rows))
    yel = Yel(len(rows), 0, idx, permil, float(total))
    if len(_yels) > 50_000:
        _yels.clear()
    _yels[key] = (recipes, rows, yel, (idx, permil))
    return yel


def _bench_for(rows):
    """The off-team rows `(chr id, value)` as C arrays (codes, values, count)."""
    hit = _benches.get(id(rows))
    if hit is None or hit[0] is not rows:
        codes = (ctypes.c_int32 * max(1, len(rows)))(*(_code(chr_id) for chr_id, _v in rows))
        values = (ctypes.c_double * max(1, len(rows)))(*(float(v) for _c, v in rows))
        if len(_benches) > 64:
            _benches.clear()
        hit = _benches[id(rows)] = (rows, codes, values, len(rows))
    return hit[1:]


def _tie_for(chr_id, asm, incumbent, held=frozenset()):
    """Per socket entry, whether its group holds the card the account already has on that
    (board, socket) -- `_connect_assignment_exact`'s `incumbent.get((chr, socket)) in
    card_ids` -- as a uint8 array, or None when no entry does. `held`: those cards out of
    every group (`assemble`'s view). Remembered per plan and incumbents: every team holds a
    different set, and a set that holds none of the plan's tied cards changes nothing."""
    key = (id(asm), id(incumbent))
    hit = _ties.get(key)
    if hit is None or hit[0] is not asm or hit[1] is not incumbent:
        owns = [incumbent.get((chr_id, socket)) for socket, _permil, _covered, _cards in asm.entries]
        flags = [1 if own in entry[3] else 0 for own, entry in zip(owns, asm.entries)]
        array = (ctypes.c_uint8 * max(1, len(flags)))(*flags) if any(flags) else None
        if len(_ties) > 50_000:
            _ties.clear()
        tied = frozenset(own for own, flag in zip(owns, flags) if flag)
        hit = _ties[key] = (asm, incumbent, array, tied)
    if not held or hit[3].isdisjoint(held):
        return hit[2]
    # a tied card held on another board: its entries lose the tie (kept here, alive for the call)
    view = _ties.get(key + (held,))
    if view is None or view[0] is not asm or view[1] is not incumbent:
        flags = [
            1 if (own := incumbent.get((chr_id, socket))) in card_ids and own not in held else 0
            for socket, _permil, _covered, card_ids in asm.entries
        ]
        array = (ctypes.c_uint8 * max(1, len(flags)))(*flags) if any(flags) else None
        view = _ties[key + (held,)] = (asm, incumbent, array, frozenset())
    return view[2]


class _Buffers(threading.local):
    """One call's argument arrays, allocated once per thread (the server answers requests
    on several threads; each fills its own)."""

    def __init__(self):
        self.plans = (Plan * MAX_CHARS)()
        self.yels = (Yel * MAX_CHARS)()
        self.rank = (ctypes.c_int32 * MAX_CHARS)()
        self.team = (ctypes.c_int32 * MAX_CHARS)()
        self.lead = (ctypes.c_int32 * MAX_CHARS)()
        self.own = (ctypes.c_int32 * MAX_CHARS)()
        self.sing = (ctypes.c_double * MAX_CHARS)()
        self.stats = (ctypes.c_double * (3 * MAX_MEMBERS))()
        self.gen = (ctypes.c_uint64 * MAX_MEMBERS)()
        self.tie = (_U8P * MAX_CHARS)()
        self.out_char = (ctypes.c_int32 * MAX_ROWS)()
        self.out_entry = (ctypes.c_int32 * MAX_ROWS)()
        self.out_gain = (ctypes.c_double * MAX_ROWS)()
        self.out_info = (ctypes.c_int32 * 4)()
        # the held view of one call (`assemble(held=)`): per group id, its rank and cards left
        self.call_rank = (ctypes.c_int32 * COL_IDS)()
        self.call_cap = (ctypes.c_int32 * COL_IDS)()


_buffers = _Buffers()
# the last call's out_info: [path, matchings solved, the check that bound] (`hd_assemble`;
# for the tests: one process, one call at a time)
LAST_INFO = [0, 0, 0]


def assemble(
    inputs: Sequence[Any],
    members: Sequence[Card],
    *,
    overall_power: float,
    bonus_permil: float,
    k_constant: float,
    active_pool_rate: float,
    song_scope: str = "",
    yellows: Sequence[Any],
    bench: Sequence[tuple[str, float]],
    cap: float,
    rounds: int,
    incumbent: Mapping[tuple[str, str], str] | None = None,
    tie_factor: float = 1.0,
    tie_first: bool = False,
    checks: bool = True,
    held: AbstractSet[str] = frozenset(),
) -> list[ConnectTriple] | None:
    """The chosen Connect placements of one team under the song bonus's cap, as the
    `ConnectTriple`s `board_plan.board_model_from_plan` would have matched -- `(gain, chr_id,
    socket, permil, covered, card_ids)` -- or `None` to make the caller run its own Python.

    `inputs` is `board_plan`'s per-character list (`ConnectInput`); `held` the cards held on
    off-team sockets, out of every group: matched as `board_plan._held_view` of each plan, each
    plan packed once whole and the view given to the library per call; `yellows` per input, its
    board's paying yellows `(rows, their sum)`; `bench` the off-team boards' paying yellow rows `(chr id, permil x multiplier)`;
    `incumbent` `(chr, socket) -> card` the account's own placements (None: none),
    `tie_factor` the weight an incumbent placement gets, `tie_first` whether the matching
    before the cap binds uses it too (a call that names its cards); `checks` False goes
    straight to the capped matching (a plan the plan cache did not make)."""
    count = len(inputs)
    if count > MAX_CHARS or len(members) > MAX_MEMBERS or len(_names()[0]) != 3:
        return None
    if count == 0:
        return []
    lib = load()
    while True:
        generation = _col_generation[0]
        hits = [_plan_for(i[1], i[2]) for i in inputs]
        if _col_generation[0] == generation:
            break
    if any(hit[2] is None for hit in hits):
        return None
    buf = _buffers
    ranks = {chr_id: rank for rank, chr_id in enumerate(sorted(i[0] for i in inputs))}
    any_tie = False
    for c, (i, hit, paying) in enumerate(zip(inputs, hits, yellows)):
        buf.plans[c] = hit[2]
        buf.yels[c] = _yel_for(i[2], paying)
        buf.rank[c] = ranks[i[0]]
        buf.team[c] = _code(i[0])
        buf.lead[c] = 1 if i[3] else 0
        own_card, own = i[4], -1
        if own_card is not None:
            for position, member in enumerate(members):
                if member is own_card or member == own_card:
                    own = position
                    break
        buf.own[c] = own
        buf.sing[c] = float(i[5])
        flags = _tie_for(i[0], i[1], incumbent, held) if incumbent else None
        buf.tie[c] = ctypes.cast(flags, _U8P) if flags is not None else _U8P()
        any_tie = any_tie or flags is not None
    for position, card in enumerate(members):
        _c, card_stats, mask = _card(card)
        buf.stats[3 * position], buf.stats[3 * position + 1], buf.stats[3 * position + 2] = card_stats
        buf.gen[position] = mask
    kept = _held_view_for(inputs, held, buf) if held else None
    who, values, nb = _bench_for(bench)
    d_us_per_power = (1.0 + bonus_permil / 1000.0) * k_constant
    chosen = lib.hd_assemble(
        count,
        buf.plans,
        buf.yels,
        buf.rank,
        buf.team,
        buf.lead,
        buf.own,
        buf.sing,
        _SONG_MASK.get(song_scope, 0),
        len(members),
        buf.stats,
        buf.gen,
        d_us_per_power,
        overall_power * k_constant,
        overall_power * d_us_per_power,  # d_us_per_song, as `assign` computes it
        active_pool_rate,
        nb,
        who,
        values,
        float(cap),
        int(rounds),
        buf.tie if any_tie else None,
        float(tie_factor),
        1 if tie_first else 0,
        1 if checks else 0,
        buf.out_char,
        buf.out_entry,
        buf.out_gain,
        buf.out_info,
        buf.call_rank if kept is not None else None,
        buf.call_cap if kept is not None else None,
    )
    if chosen < 0:
        return None
    LAST_INFO[:] = buf.out_info[0], buf.out_info[1], buf.out_info[2]
    triples = []
    for k in range(chosen):
        i = inputs[buf.out_char[k]]
        socket, permil, covered, card_ids = i[1].entries[buf.out_entry[k]]
        if kept is not None:
            card_ids = kept[card_ids]
        triples.append((buf.out_gain[k], i[0], socket, permil, covered, card_ids))
    return triples


# id(asm) -> (asm, the card groups of its entries)
_groups: dict[int, tuple] = {}


def _held_view_for(inputs, held, buf) -> dict[tuple, tuple]:
    """`{group: the cards it keeps without `held`}` for the groups of `inputs`' plans, and in
    `buf` each group id's cards left and rank among the groups keeping any, by those cards
    (the column order of `_held_view`'s groups)."""
    groups: set = set()
    for i in inputs:
        asm = i[1]
        hit = _groups.get(id(asm))
        if hit is None or hit[0] is not asm:
            if len(_groups) > 50_000:
                _groups.clear()
            hit = _groups[id(asm)] = (asm, frozenset(e[3] for e in asm.entries))
        groups |= hit[1]
    kept = {g: tuple(c for c in g if c not in held) for g in groups}
    for rank, (cards, g) in enumerate(sorted((cards, g) for g, cards in kept.items() if cards)):
        buf.call_rank[_col_id[g]] = rank
    for g, cards in kept.items():
        buf.call_cap[_col_id[g]] = len(cards)
    return kept


# --- the joint deal (`board_plan.JOINT_KERNEL`): a team's rows re-solved in its song's bench ----


_JOINT_ROWS = 1024
# id(JointBench) -> (the bench, its C arrays, keep-alive)
_joint_packs: dict[int, tuple] = {}
# (id(col_of), groups seen) -> (col_of, group id -> column array)
_joint_cols: dict[tuple, tuple] = {}
# (id(row_of), id(sockets), chr id) -> (row_of, sockets, per socket code: the matching row or -1)
_joint_socks: dict[tuple, tuple] = {}


def _arr(ctype, values):
    values = list(values)
    return (ctype * max(1, len(values)))(*values)


def _joint_pack(jb):
    """A `board_plan.JointBench` as the C arrays `hd_joint` reads (built once per bench): every
    (row, column) whose shape covers a node, its permil, its tie and its covered nodes as ids;
    each node's value and paying permil; each row's board code; the yellow boards in order."""
    hit = _joint_packs.get(id(jb))
    if hit is not None and hit[0] is jb:
        return hit[1]
    ids: dict[tuple[str, str], int] = {}
    bn_value: list[float] = []
    bn_pay: list[float] = []

    def nid(chr_id: str, node: str) -> int:
        i = ids.get((chr_id, node))
        if i is None:
            i = ids[(chr_id, node)] = len(bn_value)
            bn_value.append(float(jb.values[chr_id].get(node, 0.0)))
            bn_pay.append(float((jb.yellows.get(chr_id) or {}).get(node, 0.0)))
        return i

    rows = len(jb.rows)
    be_start, be_count = [0] * rows, [0] * rows
    be_col: list[int] = []
    be_permil: list[float] = []
    be_tie: list[int] = []
    be_cov_start: list[int] = []
    be_cov_count: list[int] = []
    be_cov: list[int] = []
    for r, (chr_id, socket) in enumerate(jb.rows):
        be_start[r] = len(be_col)
        own = jb.incumbent.get((chr_id, socket))
        for j, t in enumerate(jb.triples[r]):
            if t is None:
                continue
            be_col.append(j)
            be_permil.append(float(t[3]))
            be_tie.append(1 if own in t[5] else 0)
            be_cov_start.append(len(be_cov))
            be_cov.extend(nid(chr_id, n) for n in t[4])
            be_cov_count.append(len(t[4]))
        be_count[r] = len(be_col) - be_start[r]
    by_code: list[int] = []
    by_start: list[int] = []
    by_count: list[int] = []
    by_node: list[int] = []
    for chr_id, ys in jb.yellows.items():
        if not ys:
            continue
        by_code.append(_code(chr_id))
        by_start.append(len(by_node))
        by_node.extend(nid(chr_id, n) for n in ys)
        by_count.append(len(ys))
    i32, f64 = ctypes.c_int32, ctypes.c_double
    pack = (
        _arr(i32, be_start),
        _arr(i32, be_count),
        _arr(i32, be_col),
        _arr(f64, be_permil),
        _arr(ctypes.c_uint8, be_tie),
        _arr(i32, be_cov_start),
        _arr(i32, be_cov_count),
        _arr(i32, be_cov),
        len(bn_value),
        _arr(f64, bn_value),
        _arr(f64, bn_pay),
        _arr(i32, (_code(c) for c, _s in jb.rows)),
        len(by_code),
        _arr(i32, by_code),
        _arr(i32, by_start),
        _arr(i32, by_count),
        _arr(i32, by_node),
    )
    if len(_joint_packs) > 256:
        _joint_packs.clear()
    _joint_packs[id(jb)] = (jb, pack)
    return pack


def _joint_cols_for(jb):
    """`connect.py`'s card group ids -> `jb`'s columns (-1: a group the bench lacks). The
    columns are the layout's (`board_plan._joint_layout`), the same object for every song."""
    key = (id(jb.col_of), len(_col_id))
    hit = _joint_cols.get(key)
    if hit is None or hit[0] is not jb.col_of:
        cols = [-1] * COL_IDS
        for card_ids, cid in _col_id.items():
            if cid < COL_IDS:
                cols[cid] = jb.col_of.get(card_ids, -1)
        if len(_joint_cols) > 256:
            _joint_cols.clear()
        hit = _joint_cols[key] = (jb.col_of, (ctypes.c_int32 * COL_IDS)(*cols))
    return hit[1]


# (id(row_of), id(col_of), id(asm)) -> (row_of, col_of, the plan, {(row, column): its entry as a
# triple's tail}): keyed by the matching's layout (`board_plan._joint_layout`, the same objects
# for every song), so a team plan's entries are derived once for all songs
_joint_entries: dict[tuple, tuple] = {}


def _joint_entries_for(jb, chr_id, asm):
    """A team board's socket entries keyed by their (matching row, column) in `jb`."""
    key = (id(jb.row_of), id(jb.col_of), id(asm))
    hit = _joint_entries.get(key)
    if hit is None or hit[0] is not jb.row_of or hit[1] is not jb.col_of or hit[2] is not asm:
        entries = {
            (jb.row_of.get((chr_id, socket)), jb.col_of.get(card_ids)): (
                chr_id,
                socket,
                permil,
                covered,
                card_ids,
            )
            for socket, permil, covered, card_ids in asm.entries
        }
        if len(_joint_entries) > 50_000:
            _joint_entries.clear()
        hit = _joint_entries[key] = (jb.row_of, jb.col_of, asm, entries)
    return hit[3]


def _joint_sockets_for(jb, chr_id):
    """Each socket code of `chr_id`'s board -> its matching row (-1: none), per layout."""
    key = (id(jb.row_of), id(jb.sockets), chr_id)
    hit = _joint_socks.get(key)
    if hit is None or hit[0] is not jb.row_of or hit[1] is not jb.sockets:
        codes = [-1] * SOCKETS
        for socket in jb.sockets.get(chr_id, ()):
            match = _SOCKET_RE.match(socket)
            if match is not None and int(match.group(1)) < SOCKETS:
                codes[int(match.group(1))] = jb.row_of[(chr_id, socket)]
        if len(_joint_socks) > 50_000:
            _joint_socks.clear()
        hit = _joint_socks[key] = (jb.row_of, jb.sockets, (ctypes.c_int32 * SOCKETS)(*codes))
    return hit[2]


class _JointBuffers(threading.local):
    def __init__(self):
        self.sock = (ctypes.c_int32 * (MAX_CHARS * SOCKETS))()
        self.out_row = (ctypes.c_int32 * _JOINT_ROWS)()
        self.out_col = (ctypes.c_int32 * _JOINT_ROWS)()
        self.out_gain = (ctypes.c_double * _JOINT_ROWS)()
        self.out_scale = (ctypes.c_double * 1)()


_joint_buffers = _JointBuffers()


def joint(
    inputs: Sequence[Any],
    members: Sequence[Card],
    *,
    jb: Any,
    overall_power: float,
    bonus_permil: float,
    k_constant: float,
    active_pool_rate: float,
    song_scope: str = "",
    yellows: Sequence[Any],
    cap: float,
    rounds: int,
    incumbent: Mapping[tuple[str, str], str],
    tie_factor: float,
) -> list[ConnectTriple] | None:
    """The matched `ConnectTriple`s of one team dealt with its song's bench (`jb`, a
    `board_plan.JointBench` whose matching is this library's), as `board_plan._joint_assignment`
    matches them, or `None` to make the caller run its own Python. `inputs` and `yellows` as
    `assemble` takes them (the team's boards, all their cards)."""
    count = len(inputs)
    handle = getattr(jb.matching, "_handle", None)
    if count > MAX_CHARS or len(members) > MAX_MEMBERS or len(_names()[0]) != 3 or not handle:
        return None
    if len(jb.rows) > _JOINT_ROWS:
        return None
    lib = load()
    while True:
        generation = _col_generation[0]
        hits = [_plan_for(i[1], i[2]) for i in inputs]
        if _col_generation[0] == generation:
            break
    if any(hit[2] is None for hit in hits):
        return None
    buf, jbuf = _buffers, _joint_buffers
    ranks = {chr_id: rank for rank, chr_id in enumerate(sorted(i[0] for i in inputs))}
    any_tie = False
    for c, (i, hit, paying) in enumerate(zip(inputs, hits, yellows)):
        buf.plans[c] = hit[2]
        buf.yels[c] = _yel_for(i[2], paying)
        buf.rank[c] = ranks[i[0]]
        buf.team[c] = _code(i[0])
        buf.lead[c] = 1 if i[3] else 0
        own_card, own = i[4], -1
        if own_card is not None:
            for position, member in enumerate(members):
                if member is own_card or member == own_card:
                    own = position
                    break
        buf.own[c] = own
        buf.sing[c] = float(i[5])
        flags = _tie_for(i[0], i[1], incumbent) if incumbent else None
        buf.tie[c] = ctypes.cast(flags, _U8P) if flags is not None else _U8P()
        any_tie = any_tie or flags is not None
        ctypes.memmove(
            ctypes.addressof(jbuf.sock) + 4 * c * SOCKETS, _joint_sockets_for(jb, i[0]), 4 * SOCKETS
        )
    for position, card in enumerate(members):
        _c, card_stats, mask = _card(card)
        buf.stats[3 * position], buf.stats[3 * position + 1], buf.stats[3 * position + 2] = card_stats
        buf.gen[position] = mask
    pack = _joint_pack(jb)
    d_us_per_power = (1.0 + bonus_permil / 1000.0) * k_constant
    chosen = lib.hd_joint(
        count,
        buf.plans,
        buf.rank,
        buf.lead,
        buf.own,
        buf.sing,
        _SONG_MASK.get(song_scope, 0),
        len(members),
        buf.stats,
        buf.gen,
        d_us_per_power,
        overall_power * k_constant,
        overall_power * d_us_per_power,
        active_pool_rate,
        jbuf.sock,
        _joint_cols_for(jb),
        buf.tie if any_tie else None,
        float(tie_factor),
        buf.yels,
        buf.team,
        handle,
        *pack,
        float(cap),
        int(rounds),
        jbuf.out_row,
        jbuf.out_col,
        jbuf.out_gain,
        jbuf.out_scale,
    )
    if chosen < 0:
        return None
    by_row: dict = {}
    for i in inputs:
        by_row.update(_joint_entries_for(jb, i[0], i[1]))
    triples = []
    for k in range(chosen):
        r, j, gain = jbuf.out_row[k], jbuf.out_col[k], jbuf.out_gain[k]
        team = by_row.get((r, j))
        if team is not None:
            triples.append((gain, *team))
        else:
            t = jb.triples[r][j]
            triples.append((gain, t[1], t[2], t[3], t[4], t[5]))
    return triples


def py_sum(values: Sequence[float]) -> float:
    """The kernel's `sum()` over floats (`hd_py_sum`), for its own test."""
    array = (ctypes.c_double * max(1, len(values)))(*values)
    return load().hd_py_sum(array, len(values))


def enable() -> bool:
    """Switch the compiled assignment and the compiled board assembly on for this
    process AND every worker it spawns — the module attributes here,
    `HBUILDER_CONNECT_KERNEL` / `HBUILDER_BOARD_KERNEL` for spawned children. Returns
    False, having changed nothing, when the kernel cannot be built; the callers then
    simply run the Python path."""
    import os

    try:
        load()
    except Exception as exc:  # noqa: BLE001 - no compiler, a failed build
        print(f"connect kernel unavailable ({exc!r}); using the Python path", file=sys.stderr)
        return False
    from hbuilder.domain import board_plan

    os.environ[board_plan.CONNECT_KERNEL_ENV] = f"{Path(__file__).resolve()}:assign"
    os.environ[board_plan.BOARD_KERNEL_ENV] = f"{Path(__file__).resolve()}:assemble"
    os.environ[board_plan.JOINT_KERNEL_ENV] = f"{Path(__file__).resolve()}:joint"
    board_plan.CONNECT_KERNEL = assign
    board_plan.BOARD_KERNEL = assemble
    board_plan.JOINT_KERNEL = joint
    return True
