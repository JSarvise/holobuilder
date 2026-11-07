"""ctypes binding for `surrogate.c` — the SCREEN of the two-stage search.

Web/recon side only. A `Screen` is built over one card pool; `fit` teaches it
the exact scores taken so far and `best` ranks every legal member 5-set by the
fitted model and returns the top ones not yet scored.

It orders candidates and nothing else: no number it computes is ever reported,
and the exact leaf scores whatever it proposes (`tools/web/two_stage.py`).

    python tools/web/kernel/surrogate.py --build      # compile the shared library
"""
from __future__ import annotations

import ctypes
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "surrogate.c"
if str(HERE) not in sys.path:          # loaded by file path in a worker too
    sys.path.insert(0, str(HERE))
import cbuild  # noqa: E402

LIBRARY = cbuild.library(HERE, "surrogate")
MAX_CARDS = 250
MAX_KINDS = 64
MAX_GENS_PER_CARD = 6
THRESHOLDS = 4


class Pool(ctypes.Structure):
    _fields_ = [("n_cards", ctypes.c_int32), ("n_types", ctypes.c_int32),
                ("n_kinds", ctypes.c_int32),
                ("holomem", ctypes.POINTER(ctypes.c_int32)),
                ("type_of", ctypes.POINTER(ctypes.c_int32)),
                ("gens", ctypes.POINTER(ctypes.c_int32))]


class Model(ctypes.Structure):
    _fields_ = [("bias", ctypes.c_double),
                ("card", ctypes.POINTER(ctypes.c_double)),
                ("gate", ctypes.POINTER(ctypes.c_double)),
                ("pair", ctypes.POINTER(ctypes.c_double))]


def build(force: bool = False) -> Path:
    """Compile the kernel if the library is missing or older than its source."""
    return cbuild.build(SOURCE, LIBRARY, "screen", force)


_lib = None


def load():
    """The loaded library, built on first use. Raises if it cannot be built."""
    global _lib
    if _lib is None:
        lib = ctypes.CDLL(str(build()))
        lib.hd_sur_fit.restype = ctypes.c_double
        lib.hd_sur_fit.argtypes = [
            ctypes.POINTER(Pool), ctypes.POINTER(Model), ctypes.c_int,
            ctypes.POINTER(ctypes.c_uint8), ctypes.POINTER(ctypes.c_double), ctypes.c_int,
            ctypes.c_double, ctypes.c_double, ctypes.c_double,
        ]
        lib.hd_sur_scan.restype = ctypes.c_int
        lib.hd_sur_scan.argtypes = [
            ctypes.POINTER(Pool), ctypes.POINTER(Model), ctypes.c_int,
            ctypes.POINTER(ctypes.c_uint64), ctypes.c_int,
            ctypes.POINTER(ctypes.c_uint8), ctypes.POINTER(ctypes.c_double),
            ctypes.POINTER(ctypes.c_int64),
        ]
        lib.hd_sur_scan_part.restype = ctypes.c_int
        lib.hd_sur_scan_part.argtypes = [
            ctypes.POINTER(Pool), ctypes.POINTER(Model), ctypes.c_int,
            ctypes.POINTER(ctypes.c_uint64), ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.POINTER(ctypes.c_uint8), ctypes.POINTER(ctypes.c_double),
            ctypes.POINTER(ctypes.c_int64),
        ]
        _lib = lib
    return _lib


# The scan runs in this many threads, when the pool is large enough for it to pay: the
# search's process scans while its workers wait, so the cores are free.
SCAN_THREADS = min(16, os.cpu_count() or 1)
SCAN_THREADS_MIN_CARDS = 40
_EXECUTOR: list = []


def scan_threads(count: int) -> int:
    """How many threads scan a pool of `count` cards (1: the scan runs in the caller)."""
    return SCAN_THREADS if count >= SCAN_THREADS_MIN_CARDS and SCAN_THREADS > 1 else 1


def _executor(threads: int) -> ThreadPoolExecutor:
    """One pool of scan threads for the process, made on first use."""
    if not _EXECUTOR or _EXECUTOR[0][0] != threads:
        _EXECUTOR[:] = [(threads, ThreadPoolExecutor(max_workers=threads,
                                                     thread_name_prefix="screen-scan"))]
    return _EXECUTOR[0][1]


def set_key(indices) -> int:
    """The scan's key for a set of ASCENDING pool indices; keys order as the
    sets do lexicographically."""
    a, b, c, d, e = indices
    return a << 32 | b << 24 | c << 16 | d << 8 | e


class Screen:
    """The model over one pool. `cards` is the pool in the order the caller's
    index tuples refer to; only `member`, `type` and `generations` are read."""

    def __init__(self, cards) -> None:
        cards = list(cards)
        count = len(cards)
        if count > MAX_CARDS:
            raise ValueError(f"the screen holds at most {MAX_CARDS} cards, got {count}")
        holomems = {name: i for i, name in enumerate(sorted({c.member for c in cards}))}
        types = sorted({c.type for c in cards})
        generations = sorted({g for c in cards for g in c.generations})
        if len(types) + len(generations) > MAX_KINDS:
            raise ValueError("too many types + generations for the screen")
        if any(len(c.generations) > MAX_GENS_PER_CARD for c in cards):
            raise ValueError("a card carries more generations than the screen holds")
        kind = {t: i for i, t in enumerate(types)}
        kind.update({g: len(types) + i for i, g in enumerate(generations)})
        gens: list[int] = []
        for card in cards:
            own = sorted({kind[g] for g in card.generations})   # a repeat would count twice
            gens.extend(own + [-1] * (MAX_GENS_PER_CARD - len(own)))
        self.count = count
        self.kinds = len(kind)
        self._holomem = (ctypes.c_int32 * count)(*(holomems[c.member] for c in cards))
        self._type_of = (ctypes.c_int32 * count)(*(kind[c.type] for c in cards))
        self._gens = (ctypes.c_int32 * len(gens))(*gens)
        self._pool = Pool(count, len(types), self.kinds, self._holomem, self._type_of, self._gens)
        self._card = (ctypes.c_double * count)()
        self._gate = (ctypes.c_double * (THRESHOLDS * self.kinds))()
        self._pair = (ctypes.c_double * (count * count))()
        self._model = Model(0.0, self._card, self._gate, self._pair)
        self.fitted = False

    def fit(self, sets, log_scores, *, sweeps: int = 12, lam_card: float = 1.0,
            lam_gate: float = 3.0, lam_pair: float = 4.0) -> float:
        """Fit to `sets` (ascending index 5-tuples) and their log exact scores
        — ridge regression by coordinate descent, `sweeps` passes. Returns the
        mean absolute residual in log units."""
        n = len(sets)
        flat = (ctypes.c_uint8 * (5 * n))(*(i for s in sets for i in s))
        target = (ctypes.c_double * n)(*log_scores)
        mae = load().hd_sur_fit(ctypes.byref(self._pool), ctypes.byref(self._model), n, flat,
                                target, sweeps, lam_card, lam_gate, lam_pair)
        if mae < 0:
            raise RuntimeError("the screen could not be fitted")
        self.fitted = True
        return mae

    def predict(self, indices) -> float:
        """The fitted model's value of the cards `indices` (a set or part of one, any order):
        the bias, their card weights, the gate features their kinds switch on and their pair
        weights, as the scan values a full set."""
        cards = sorted(indices)
        value = self._model.bias + sum(self._card[i] for i in cards)
        count: dict[int, int] = {}
        for i in cards:
            kinds = [self._type_of[i]]
            for g in range(MAX_GENS_PER_CARD):
                kind = self._gens[i * MAX_GENS_PER_CARD + g]
                if kind < 0:
                    break
                kinds.append(kind)
            for kind in kinds:
                count[kind] = count.get(kind, 0) + 1
        for kind, c in count.items():
            for threshold in range(2, min(c, 5) + 1):
                value += self._gate[(threshold - 2) * self.kinds + kind]
        for a in range(len(cards)):
            for b in range(a + 1, len(cards)):
                value += self._pair[cards[a] * self.count + cards[b]]
        return value

    def best(self, want: int, seen=()) -> tuple[list[tuple[int, ...]], list[float], int]:
        """`(sets, predictions, how many legal sets exist)`: the `want` legal
        sets the model ranks highest, best first, leaving out `seen` (ascending
        index tuples). Before any `fit` the model is flat and the order is the
        lexicographic one."""
        keys = [set_key(s) for s in seen]
        seen_array = (ctypes.c_uint64 * max(1, len(keys)))(*keys)
        parts = scan_threads(self.count)
        if parts <= 1:
            out_sets = (ctypes.c_uint8 * (5 * want))()
            out_pred = (ctypes.c_double * want)()
            legal = ctypes.c_int64(0)
            got = load().hd_sur_scan(ctypes.byref(self._pool), ctypes.byref(self._model),
                                     len(keys), seen_array, want, out_sets, out_pred,
                                     ctypes.byref(legal))
            if got < 0:
                raise RuntimeError("the screen scan failed")
            raw = bytes(out_sets)[: 5 * got]
            sets = [tuple(raw[5 * i: 5 * i + 5]) for i in range(got)]
            return sets, list(out_pred[:got]), legal.value
        # The scan split by first card over `parts` threads (the C call releases the GIL),
        # each keeping its own best `want`; the best `want` of their union under the scan's
        # total order (prediction, then the earlier set) are the whole scan's, in its order.
        def part(a0: int):
            out_sets = (ctypes.c_uint8 * (5 * want))()
            out_pred = (ctypes.c_double * want)()
            legal = ctypes.c_int64(0)
            got = load().hd_sur_scan_part(ctypes.byref(self._pool), ctypes.byref(self._model),
                                          len(keys), seen_array, want, a0, parts, out_sets,
                                          out_pred, ctypes.byref(legal))
            if got < 0:
                raise RuntimeError("the screen scan failed")
            raw = bytes(out_sets)[: 5 * got]
            return ([(out_pred[i], tuple(raw[5 * i: 5 * i + 5])) for i in range(got)],
                    legal.value)

        load()
        results = list(_executor(parts).map(part, range(parts)))
        items = [item for found, _legal in results for item in found]
        items.sort(key=lambda item: (-item[0], set_key(item[1])))
        items = items[:want]
        return [s for _p, s in items], [p for p, _s in items], sum(n for _f, n in results)


if __name__ == "__main__":
    print(build(force="--build" in sys.argv))
