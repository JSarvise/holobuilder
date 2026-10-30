"""ctypes binding for `connect.c` — the team-wide Connect socket assignment,
compiled.

Web/recon side only: nothing under `src/hbuilder/` imports this. The domain
exposes a seam (`board_plan.CONNECT_KERNEL`) and the caller injects the kernel,
exactly as `notewalk.py` does for the note walk.

    python tools/web/kernel/connect.py --build      # compile the shared library

What is compiled is `node_values_from_recipes` + the candidate gains + the exact
matching; what stays in Python is everything with a name in it (which card goes
where, the multipliers per node). A plan is packed into C arrays ONCE and reused
for every team that carries it.

The flags are part of the contract: `-ffp-contract=off` and no `-ffast-math`, so
the result is bit-identical to the Python it replaces.
"""
from __future__ import annotations

import ctypes
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "connect.c"
LIBRARY = HERE / ("connect.dll" if sys.platform == "win32" else "connect.so")
CFLAGS = ["-O2", "-ffp-contract=off", "-fno-fast-math", "-shared", "-fPIC"]
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
_KIND = {"stat": K_STAT, "rate": K_RATE, "song_score_bonus": K_SONG,
         "skill_effect_up": K_SKILL_EFFECT_UP, "activation_rate_up": K_UPTIME,
         "cooldown_shorten": K_UPTIME}
_SCOPE = {"leader": S_LEADER, "member": S_MEMBER, "grouping": S_GROUPING}
_SOCKET_RE = re.compile(r"^S-(\d{3})$")


class Eff(ctypes.Structure):
    _fields_ = [("kind", ctypes.c_int32), ("scope", ctypes.c_int32),
                ("stat", ctypes.c_int32), ("flags", ctypes.c_int32),
                ("value", ctypes.c_double)]


class Node(ctypes.Structure):
    _fields_ = [("is_override", ctypes.c_int32), ("gen", ctypes.c_int32),
                ("eff_start", ctypes.c_int32), ("eff_count", ctypes.c_int32),
                ("v", ctypes.c_double)]


class Entry(ctypes.Structure):
    _fields_ = [("socket", ctypes.c_int32), ("col", ctypes.c_int32),
                ("cap", ctypes.c_int32), ("cov_start", ctypes.c_int32),
                ("cov_count", ctypes.c_int32), ("pad_", ctypes.c_int32),
                ("permil", ctypes.c_double)]


class Plan(ctypes.Structure):
    _fields_ = [("n_nodes", ctypes.c_int32), ("n_entries", ctypes.c_int32),
                ("nodes", ctypes.POINTER(Node)), ("effs", ctypes.POINTER(Eff)),
                ("entries", ctypes.POINTER(Entry)), ("cov", ctypes.POINTER(ctypes.c_int32))]


def build(force: bool = False) -> Path:
    """Compile the kernel if the library is missing or older than its source."""
    if not force and LIBRARY.exists() and LIBRARY.stat().st_mtime >= SOURCE.stat().st_mtime:
        return LIBRARY
    gcc = shutil.which("gcc")
    if gcc is None:
        raise RuntimeError("no gcc on PATH; the connect kernel cannot be built")
    subprocess.run([gcc, *CFLAGS, "-o", str(LIBRARY), str(SOURCE)], check=True)
    return LIBRARY


_lib = None


def load():
    """The loaded library, built on first use. Raises if it cannot be built.

    `PyDLL`, not `CDLL`: the kernel keeps its work arrays in static storage, so
    the call must not release the GIL (a few microseconds; nothing to gain)."""
    global _lib
    if _lib is None:
        lib = ctypes.PyDLL(str(build()))
        lib.hd_connect.restype = ctypes.c_int
        lib.hd_connect.argtypes = [
            ctypes.c_int, ctypes.POINTER(Plan), ctypes.POINTER(ctypes.c_int32),
            ctypes.POINTER(ctypes.c_int32), ctypes.POINTER(ctypes.c_int32),
            ctypes.POINTER(ctypes.c_double), ctypes.c_int,
            ctypes.c_int, ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_uint64),
            ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double,
            ctypes.POINTER(ctypes.c_int32), ctypes.POINTER(ctypes.c_int32),
            ctypes.POINTER(ctypes.c_double),
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
        from hbuilder.domain import board_plan as bp

        generations = sorted(set(bp._GENERATION_BY_GROUPING.values()))
        if len(generations) > 64:
            raise RuntimeError("more than 64 generations; the mask does not fit")
        _domain = (tuple(bp.STAT_NAMES), {g: i for i, g in enumerate(generations)},
                   bp._GROUPING_RE, bp._GENERATION_BY_GROUPING)
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
            nodes.append((1, gen_bit[generation] if generation is not None else -1,
                          0, 0, float(override.get("v", 0))))
            continue
        start = len(effs)
        for eff in node_effs:
            kind = _KIND.get(eff.kind, K_OTHER)
            stat = -1
            if kind in (K_STAT, K_RATE) and eff.stat != "all":
                if eff.stat not in stat_names:
                    return None
                stat = stat_names.index(eff.stat)
            flags = (F_CONDITIONAL if eff.conditional else 0) | (
                F_SONG_SCOPED if eff.song_scope in ("solo", "group") else 0) | (
                F_SOLO if eff.song_scope == "solo" else F_GROUP if eff.song_scope == "group" else 0)
            effs.append((kind, _SCOPE.get(eff.scope, S_ACCOUNT), stat, flags, float(eff.value)))
        nodes.append((0, -1, start, len(effs) - start, 0.0))
    entries, cov = [], []
    for socket, permil, covered, card_ids in asm.entries:
        match = _SOCKET_RE.match(socket)
        if match is None or int(match.group(1)) >= SOCKETS:
            return None
        start = len(cov)
        cov.extend(node_index.get(node, -1) for node in covered)
        entries.append((int(match.group(1)), _col_rank[card_ids], len(card_ids),
                        start, len(cov) - start, 0, float(permil)))
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

def assign(inputs, members, *, overall_power, bonus_permil, k_constant, active_pool_rate,
           song_scope=""):
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
        count, plans, int_array(*(ranks[i[0]] for i in inputs)),
        int_array(*(1 if i[3] else 0 for i in inputs)), int_array(*own),
        (ctypes.c_double * count)(*(float(i[5]) for i in inputs)),
        _SONG_MASK.get(song_scope, 0), member_count, (ctypes.c_double * max(1, len(stats)))(*stats),
        (ctypes.c_uint64 * max(1, member_count))(*masks),
        (1.0 + bonus_permil / 1000.0) * k_constant, overall_power * k_constant,
        overall_power * ((1.0 + bonus_permil / 1000.0) * k_constant),   # d_us_per_song, same order
        active_pool_rate, out_char, out_entry, out_gain,
    )
    if chosen < 0:
        return None
    triples = []
    for k in range(chosen):
        chr_id, asm = inputs[out_char[k]][0], inputs[out_char[k]][1]
        socket, permil, covered, card_ids = asm.entries[out_entry[k]]
        triples.append((out_gain[k], chr_id, socket, permil, covered, card_ids))
    return triples


def enable() -> bool:
    """Switch the compiled assignment on for this process AND every worker it
    spawns — the module attribute here, `HBUILDER_CONNECT_KERNEL` for spawned
    children. Returns False, having changed nothing, when the kernel cannot be
    built; the callers then simply run the Python path."""
    import os

    try:
        load()
    except Exception as exc:  # noqa: BLE001 - no compiler, a failed build
        print(f"connect kernel unavailable ({exc!r}); using the Python path", file=sys.stderr)
        return False
    from hbuilder.domain import board_plan

    os.environ[board_plan.CONNECT_KERNEL_ENV] = f"{Path(__file__).resolve()}:assign"
    board_plan.CONNECT_KERNEL = assign
    return True
