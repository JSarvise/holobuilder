"""ctypes binding for `notewalk.c` — the per-song note walk, compiled, and the whole
per-chart step of a leader around it (`score_chart`).

Web/recon side only: nothing under `src/hbuilder/` imports this. The domain
exposes a seam and the caller injects the kernel, the way the exact board solver
is injected. `ctypes` is stdlib; the only build requirement is a C compiler
(`gcc`: WinLibs on Windows, the distribution's on Linux).

    python tools/web/kernel/notewalk.py --build      # compile the shared library

Compiled with `cbuild.py`'s flags, so the result is bit-identical to the Python it
replaces, on Windows and Linux alike.
"""

from __future__ import annotations

import ctypes
import sys
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hbuilder.domain.scoring_config import ScoringConfig

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "notewalk.c"
if str(HERE) not in sys.path:  # loaded by file path in a worker too
    sys.path.insert(0, str(HERE))
import cbuild  # noqa: E402

LIBRARY = cbuild.library(HERE, "notewalk")
MAX_TERMS = 64  # 64-bit live masks (riders add ~10 terms a team)
SLOTS = 5


class Term(ctypes.Structure):
    _fields_ = [
        ("bucket", ctypes.c_int32),
        ("is_boost", ctypes.c_int32),
        ("owner", ctypes.c_int32),
        ("has_target", ctypes.c_int32),
        ("target_mask", ctypes.c_uint32),
        ("pad_", ctypes.c_int32),
        ("magnitude", ctypes.c_double),
        ("occupancy", ctypes.c_double),
    ]


def build(force: bool = False) -> Path:
    """Compile the kernel if the library is missing or older than its source."""
    return cbuild.build(SOURCE, LIBRARY, "note-walk", force)


_lib: ctypes.CDLL | None = None


def load() -> ctypes.CDLL:
    """The loaded library, built on first use. Raises if it cannot be built."""
    global _lib
    if _lib is None:
        lib = ctypes.CDLL(str(build()))
        lib.hd_pool.restype = ctypes.c_double
        lib.hd_pool.argtypes = [ctypes.POINTER(Term), ctypes.c_int, ctypes.c_uint64, ctypes.c_double]
        for walk_fn in (lib.hd_walk, lib.hd_walk_general):
            walk_fn.restype = ctypes.c_double
            walk_fn.argtypes = [
                ctypes.c_int,
                ctypes.POINTER(ctypes.c_double),
                ctypes.POINTER(ctypes.c_uint64),
                ctypes.c_double,
                ctypes.POINTER(Term),
                ctypes.c_int,
                ctypes.POINTER(ctypes.c_double),
                ctypes.c_int,
                ctypes.c_double,
                ctypes.c_int,
                ctypes.POINTER(ctypes.c_int32),
                ctypes.POINTER(ctypes.c_int32),
                ctypes.POINTER(ctypes.c_int32),
                ctypes.POINTER(ctypes.c_int32),
                ctypes.POINTER(ctypes.c_double),
                ctypes.POINTER(ctypes.c_uint64),
                ctypes.POINTER(ctypes.c_uint8),
            ]
        lib.hd_bits_fill.restype = None
        lib.hd_bits_fill.argtypes = [ctypes.POINTER(ctypes.c_uint64), ctypes.c_int, ctypes.c_uint64]
        lib.hd_bits_or.restype = None
        lib.hd_bits_or.argtypes = [
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_int32),
        ]
        lib.hd_score_chart.restype = ctypes.c_int
        lib.hd_score_chart.argtypes = [
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_int32),
            ctypes.POINTER(ctypes.c_double),
            ctypes.POINTER(ctypes.c_double),
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_int32),
            ctypes.POINTER(ctypes.c_double),
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_double),
            ctypes.POINTER(ctypes.c_double),
            ctypes.POINTER(ctypes.c_double),
            ctypes.c_double,
            ctypes.c_double,
            ctypes.POINTER(ctypes.c_double),
            ctypes.c_int,
            ctypes.c_double,
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.POINTER(ctypes.c_double),
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(ctypes.c_double),
            ctypes.POINTER(ctypes.c_int32),
        ]
        _lib = lib
    return _lib


def pack_terms(terms: Sequence[Any], member_ids: Sequence[str]) -> ctypes.Array[Term] | None:
    """`song._Term` objects -> a C array. `member_ids` are the five member card
    ids in slot order; a term's owner and a boost's recipients become indices
    into it. Returns `None` when a team cannot be expressed (too many terms)."""
    if len(terms) > MAX_TERMS:
        return None
    index = {card_id: i for i, card_id in enumerate(member_ids)}
    buckets: dict = {}
    array = (Term * max(1, len(terms)))()
    for i, term in enumerate(terms):
        out = array[i]
        out.bucket = buckets.setdefault(term.bucket, len(buckets))
        out.is_boost = 1 if term.is_boost else 0
        out.owner = index.get(term.owner, -1)
        if term.targets is None:
            out.has_target = 0
            out.target_mask = 0
        else:
            out.has_target = 1
            out.target_mask = sum(1 << index[c] for c in term.targets if c in index)
        out.magnitude = float(term.magnitude)
        out.occupancy = float(term.occupancy)
    return array


if __name__ == "__main__":
    print(build(force="--build" in sys.argv))


# --- the adapter `song.NOTE_WALK_KERNEL` is set to ---------------------------

_weights_cache: dict[int, tuple] = {}
# The walk's SCRATCH arrays (`base`, `extra`, `bits`), one set per thread and note
# count — never shared through `_weights_cache` with the read-only weights: the walk
# runs with the GIL released (CDLL), so two server threads on the same song would
# write into each other's arrays (four concurrent /api/row-detail calls then return
# 1,869,502 .. 3,503,889 for a 3,425,201 team).
_scratch = threading.local()


def _weights(note_weights):
    """`(note_weights, weights, base, extra, bits)`: the chart's note weights as a C
    array, converted once per tuple object and shared (read-only); `song.score_song_
    detailed` hands the same cached tuple for a chart, so this is keyed by identity
    and keeps the tuple alive alongside its array. `base` / `extra` / `bits` are
    this THREAD's scratch arrays for that note count."""
    hit = _weights_cache.get(id(note_weights))
    if hit is None or hit[0] is not note_weights:
        hit = (note_weights, (ctypes.c_double * len(note_weights))(*note_weights))
        if len(_weights_cache) > 512:
            _weights_cache.clear()
        _weights_cache[id(note_weights)] = hit
    count = len(note_weights)
    mine = getattr(_scratch, "arrays", None)
    if mine is None:
        mine = _scratch.arrays = {}
    work = mine.get(count)
    if work is None:
        if len(mine) > 512:
            mine.clear()
        work = mine[count] = (
            (ctypes.c_double * count)(),
            (ctypes.c_uint64 * count)(),
            (ctypes.c_uint64 * count)(),
        )
    return (hit[0], hit[1], *work)


_ranges_cache: dict[int, tuple] = {}


def _ranges(ranges):
    """One term's note-index ranges as a flat C array, converted once per tuple
    object — `song._window_note_ranges` hands the same tuple for a (lattice,
    chart), so this is keyed by identity like the weights."""
    hit = _ranges_cache.get(id(ranges))
    if hit is None or hit[0] is not ranges:
        flat = [bound for pair in ranges for bound in pair]
        if len(_ranges_cache) > 65536:
            _ranges_cache.clear()
        hit = (ranges, (ctypes.c_int32 * max(1, len(flat)))(*flat), len(ranges))
        _ranges_cache[id(ranges)] = hit
    return hit


def walk(
    note_weights: Sequence[float],
    liveness: tuple[int, Sequence[tuple[int, Sequence[tuple[int, int]]]]],
    constant: float,
    terms: Sequence[Any],
    score_rates: Sequence[float],
    skill_effect_up: float,
    special_terms: Sequence[int],
    cover_range: Mapping[int, Sequence[tuple[int, int]]],
    member_ids: Sequence[str],
    cfg: ScoringConfig,
    general: bool = False,
    allowed: Sequence[bool] | None = None,
) -> tuple[float, tuple[int, ...]] | None:
    """The separable fast path of `score_song_detailed`: `(total, permutation)`,
    or `None` to make the caller run its own Python (a configuration or a team
    this kernel does not express). Never an approximation. `general`: the
    COUPLED case (slots' terms meet on a note), walked order by order
    (`hd_walk_general`) instead of by per-slot deltas. `allowed`: 120 flags in
    `itertools.permutations(range(5))` order, the only orders the maximum is taken over
    (a recipients tie's outcome, `song._support_tie_outcomes`); `None` is every order."""
    if (
        cfg.stacking_model.name != "additive"
        or cfg.active_stacking_model.name != "max"
        or len(terms) > MAX_TERMS
        or len(score_rates) > MAX_TERMS
    ):
        return None
    lib = load()
    _, weights, base, extra, bits = _weights(note_weights)
    count = len(note_weights)
    packed = pack_terms(terms, member_ids)
    always_mask, window_ranges = liveness
    lib.hd_bits_fill(bits, count, always_mask)
    for bit, ranges in window_ranges:
        _, array, pairs = _ranges(ranges)
        lib.hd_bits_or(bits, count, 1 << bit, pairs, array)
    rates = (ctypes.c_double * max(1, len(score_rates)))(*score_rates)
    special_count = len(special_terms)
    special_bit = (ctypes.c_int32 * max(1, special_count))(*special_terms)
    special_member = (ctypes.c_int32 * max(1, special_count))(
        *(terms[bit].special_member for bit in special_terms)
    )
    flat: list[int] = []
    for bit in special_terms:
        for low, high in cover_range[bit]:
            flat.append(low)
            flat.append(high)
    cover = (ctypes.c_int32 * max(1, len(flat)))(*flat)
    permutation = (ctypes.c_int32 * SLOTS)()
    mask = None if allowed is None else (ctypes.c_uint8 * len(allowed))(*(1 if a else 0 for a in allowed))
    total = (lib.hd_walk_general if general else lib.hd_walk)(
        count,
        weights,
        bits,
        constant,
        packed,
        len(terms),
        rates,
        len(score_rates),
        skill_effect_up,
        special_count,
        special_bit,
        special_member,
        cover,
        permutation,
        base,
        extra,
        mask,
    )
    if total != total:  # NaN: no order allowed
        return None
    return total, tuple(permutation)


# --- the adapter `song.CHART_KERNEL` is set to ------------------------------

TEMPLATE_INTS = 11  # `CK_TI` in notewalk.c
TEMPLATE_DOUBLES = 6  # `CK_TD`
HOST_INTS = 3  # `CK_HI`

_spec_cache: dict[int, tuple] = {}
_chart_cache: dict[int, tuple] = {}
_allowed_cache: dict[int, tuple] = {}


def _packed_spec(spec: tuple) -> tuple:
    """`song._chart_kernel_spec`'s tuple as C arrays, converted once per spec OBJECT (a team's
    spec is kept with its member half and asked for once per chart), keyed by identity with the
    spec kept alive in the entry."""
    hit = _spec_cache.get(id(spec))
    if hit is None or hit[0] is not spec:
        rows, rider_q, hosts = spec
        ints = [v for r in rows for v in (*r[:10], 0)]
        doubles = [float(v) for r in rows for v in r[10:16]]
        host_ints = [v for h in hosts for v in h[:3]]
        packed = (
            len(rows),
            (ctypes.c_int32 * max(1, len(ints)))(*ints),
            (ctypes.c_double * max(1, len(doubles)))(*doubles),
            (ctypes.c_double * max(1, len(rider_q)))(*rider_q),
            len(hosts),
            (ctypes.c_int32 * max(1, len(host_ints)))(*host_ints),
            (ctypes.c_double * max(1, len(hosts)))(*(float(h[3]) for h in hosts)),
        )
        if len(_spec_cache) > 4096:
            _spec_cache.clear()
        hit = _spec_cache[id(spec)] = (spec, packed)
    return hit[1]


def _chart_arrays(times: Sequence[float], cues: Sequence[float]) -> tuple:
    """A chart's note times and SPECIAL cues as C arrays, once per (times, cues) objects."""
    hit = _chart_cache.get(id(times))
    if hit is None or hit[0] is not times or hit[1] is not cues:
        hit = (
            times,
            cues,
            (ctypes.c_double * max(1, len(times)))(*times),
            (ctypes.c_double * SLOTS)(*cues),
        )
        if len(_chart_cache) > 512:
            _chart_cache.clear()
        _chart_cache[id(times)] = hit
    return hit[2], hit[3]


def _allowed_mask(allowed: Sequence[bool]) -> ctypes.Array:
    hit = _allowed_cache.get(id(allowed))
    if hit is None or hit[0] is not allowed:
        hit = (allowed, (ctypes.c_uint8 * len(allowed))(*(1 if a else 0 for a in allowed)))
        if len(_allowed_cache) > 4096:
            _allowed_cache.clear()
        _allowed_cache[id(allowed)] = hit
    return hit[1]


def score_chart(
    spec: tuple,
    times: Sequence[float],
    note_weights: Sequence[float],
    cues: Sequence[float],
    song_seconds: float,
    constant: float,
    score_rates: Sequence[float],
    skill_effect_up: float,
    allowed: Sequence[bool] | None,
) -> tuple[float, tuple[int, ...]] | None:
    """One leader's per-chart step of `song._score_song` (`song.CHART_KERNEL`): `(total, the
    winning order as member indices per slot)`, or None to make the caller walk in Python (a
    team the kernel does not express). Never an approximation."""
    if len(score_rates) > MAX_TERMS or len(cues) != SLOTS:
        return None
    lib = load()
    nt, ti, td, rq, nh, hi, hd = _packed_spec(spec)
    _, weights, base, extra, bits = _weights(note_weights)
    times_c, cues_c = _chart_arrays(times, cues)
    rates = (ctypes.c_double * max(1, len(score_rates)))(*score_rates)
    total = ctypes.c_double()
    permutation = (ctypes.c_int32 * SLOTS)()
    status = lib.hd_score_chart(
        nt,
        ti,
        td,
        rq,
        nh,
        hi,
        hd,
        len(note_weights),
        times_c,
        weights,
        cues_c,
        song_seconds,
        constant,
        rates,
        len(score_rates),
        skill_effect_up,
        None if allowed is None else _allowed_mask(allowed),
        base,
        extra,
        bits,
        ctypes.byref(total),
        permutation,
    )
    if status != 0:
        return None
    return total.value, tuple(permutation)


def enable() -> bool:
    """Switch the compiled walk and chart step on for this process AND every worker it
    spawns.

    Builds the library if needed, then sets each kernel's two hooks: the module attribute for
    this interpreter and `HBUILDER_NOTE_WALK_KERNEL` / `HBUILDER_CHART_KERNEL` for spawned children (a pool
    on Windows starts fresh interpreters, which inherit the environment and
    nothing else). Returns False, having changed nothing, when the kernel cannot
    be built — the callers then simply run the Python path."""
    import os

    try:
        load()
    except Exception as exc:  # noqa: BLE001 - no compiler, a failed build
        print(f"note-walk kernel unavailable ({exc!r}); using the Python path", file=sys.stderr)
        return False
    from hbuilder.domain import song

    os.environ[song.NOTE_WALK_KERNEL_ENV] = f"{Path(__file__).resolve()}:walk"
    song.NOTE_WALK_KERNEL = walk
    os.environ[song.CHART_KERNEL_ENV] = f"{Path(__file__).resolve()}:score_chart"
    song.CHART_KERNEL = score_chart
    return True
