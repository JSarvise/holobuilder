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
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hbuilder.domain.board_plan import ConnectTriple
    from hbuilder.domain.card import Card

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "connect.c"
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
    """Compile the kernel if the library is missing or older than its source."""
    return cbuild.build(SOURCE, LIBRARY, "connect", force)


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
        ]
        lib.hd_py_sum.restype = ctypes.c_double
        lib.hd_py_sum.argtypes = [f64p, ctypes.c_int]
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
        from hbuilder.domain import board_plan as bp

        generations = sorted(set(bp._GENERATION_BY_GROUPING.values()))
        if len(generations) > 64:
            raise RuntimeError("more than 64 generations; the mask does not fit")
        _domain = (
            tuple(bp.STAT_NAMES),
            {g: i for i, g in enumerate(generations)},
            bp._GROUPING_RE,
            bp._GENERATION_BY_GROUPING,
        )
    return _domain


# Card groups (`entries[..][3]`, id-sorted tuples) -> their rank among every
# group seen, in tuple order — the column order `_connect_assignment_exact`
# sorts into. A new group shifts the ranks, so it empties the plan packs.
_col_rank: dict[tuple, int] = {}
# id(asm) -> (asm, recipes, Plan, keep-alive arrays, entries), or `None` in
# place of the Plan for a plan this kernel cannot express.
_packs: dict[int, tuple] = {}
# id(card) -> (card, three stats as floats, generation mask)
_cards: dict[int, tuple] = {}


def _pack(asm, recipes):
    stat_names, gen_bit, grouping_re, generation_by_grouping = _names()
    unseen = {e[3] for e in asm.entries} - _col_rank.keys()
    if unseen:
        ranked = sorted(set(_col_rank) | unseen)
        _col_rank.clear()
        _col_rank.update((ids, rank) for rank, ids in enumerate(ranked))
        _packs.clear()
    if len(_col_rank) > COL_IDS or len(recipes) > MAX_NODES or len(asm.entries) > MAX_ENTRIES:
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
                _col_rank[card_ids],
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
        # packing a plan with an unseen card group re-ranks the columns and
        # empties the packs, so the plans are (re)collected until none does
        seen_groups = len(_col_rank)
        hits = [_plan_for(i[1], i[2]) for i in inputs]
        if len(_col_rank) == seen_groups:
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


def _tie_for(chr_id, asm, incumbent):
    """Per socket entry, whether its group holds the card the account already has on that
    (board, socket) -- `_connect_assignment_exact`'s `incumbent.get((chr, socket)) in
    card_ids` -- as a uint8 array, or None when no entry does."""
    key = (id(asm), id(incumbent))
    hit = _ties.get(key)
    if hit is None or hit[0] is not asm or hit[1] is not incumbent:
        flags = [
            1 if incumbent.get((chr_id, socket)) in card_ids else 0
            for socket, _permil, _covered, card_ids in asm.entries
        ]
        array = (ctypes.c_uint8 * max(1, len(flags)))(*flags) if any(flags) else None
        if len(_ties) > 50_000:
            _ties.clear()
        hit = _ties[key] = (asm, incumbent, array)
    return hit[2]


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
) -> list[ConnectTriple] | None:
    """The chosen Connect placements of one team under the song bonus's cap, as the
    `ConnectTriple`s `board_plan.board_model_from_plan` would have matched -- `(gain, chr_id,
    socket, permil, covered, card_ids)` -- or `None` to make the caller run its own Python.

    `inputs` is `board_plan`'s per-character list (`ConnectInput`, the held cards already
    out of each plan's groups); `yellows` per input, its board's paying yellows `(rows, their
    sum)`; `bench` the off-team boards' paying yellow rows `(chr id, permil x multiplier)`;
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
        # packing a plan with an unseen card group re-ranks the columns and
        # empties the packs, so the plans are (re)collected until none does
        seen_groups = len(_col_rank)
        hits = [_plan_for(i[1], i[2]) for i in inputs]
        if len(_col_rank) == seen_groups:
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
        flags = _tie_for(i[0], i[1], incumbent) if incumbent else None
        buf.tie[c] = ctypes.cast(flags, _U8P) if flags is not None else _U8P()
        any_tie = any_tie or flags is not None
    for position, card in enumerate(members):
        _c, card_stats, mask = _card(card)
        buf.stats[3 * position], buf.stats[3 * position + 1], buf.stats[3 * position + 2] = card_stats
        buf.gen[position] = mask
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
    )
    if chosen < 0:
        return None
    LAST_INFO[:] = buf.out_info[0], buf.out_info[1], buf.out_info[2]
    triples = []
    for k in range(chosen):
        i = inputs[buf.out_char[k]]
        socket, permil, covered, card_ids = i[1].entries[buf.out_entry[k]]
        triples.append((buf.out_gain[k], i[0], socket, permil, covered, card_ids))
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
    board_plan.CONNECT_KERNEL = assign
    board_plan.BOARD_KERNEL = assemble
    return True
