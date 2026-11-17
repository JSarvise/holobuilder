"""ctypes binding for `lawmerge.c` -- the merge of two run-score laws
(`hbuilder.domain.song_distribution.merge_laws`), compiled.

Web side only: nothing under `src/hbuilder/` imports this. The domain exposes the seam
`song_distribution.MERGE_KERNEL` and `enable()` injects `merge`. A merged law stays in C (`CLaw`)
until something reads it (the summary, the score pace), so a sweep's laws are never copied
into Python dictionaries between merges.

    python tools/web/kernel/lawmerge.py --build      # compile the shared library

Compiled with `cbuild.py`'s flags, so the result is bit-identical to the Python it replaces.
"""

from __future__ import annotations

import ctypes
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "lawmerge.c"
if str(HERE) not in sys.path:  # loaded by file path in a worker too
    sys.path.insert(0, str(HERE))
import cbuild  # noqa: E402

LIBRARY = cbuild.library(HERE, "lawmerge")

_I64P = ctypes.POINTER(ctypes.c_int64)
_F64P = ctypes.POINTER(ctypes.c_double)
_lib: ctypes.CDLL | None = None
_lock = threading.Lock()


def build(force: bool = False) -> Path:
    """Compile the kernel if the library is missing or older than its source."""
    return cbuild.build(SOURCE, LIBRARY, "law merge", force)


def load() -> ctypes.CDLL:
    """The loaded library, built on first use. Raises if it cannot be built."""
    global _lib
    with _lock:
        if _lib is None:
            lib = ctypes.CDLL(str(build()))
            lib.hd_law_new.restype = ctypes.c_void_p
            lib.hd_law_new.argtypes = [_I64P, _F64P, ctypes.c_int64]
            lib.hd_law_size.restype = ctypes.c_int64
            lib.hd_law_size.argtypes = [ctypes.c_void_p]
            lib.hd_law_copy.restype = None
            lib.hd_law_copy.argtypes = [ctypes.c_void_p, _I64P, _F64P]
            lib.hd_law_free.restype = None
            lib.hd_law_free.argtypes = [ctypes.c_void_p]
            lib.hd_law_merge.restype = ctypes.c_void_p
            lib.hd_law_merge.argtypes = [
                ctypes.c_void_p,
                ctypes.c_double,
                ctypes.c_int64,
                ctypes.c_void_p,
                ctypes.c_double,
                ctypes.c_int64,
                ctypes.c_int64,
                _I64P,
                ctypes.c_int64,
            ]
            _lib = lib
    return _lib


class CLaw:
    """A law held in C: `items()` (in insertion order), iteration over its values, `len()` and
    `[value]`, as the dictionary `merge_laws` returns; read into one on first use."""

    __slots__ = ("_dict", "_handle", "_lib")

    def __init__(self, handle: int, lib: ctypes.CDLL) -> None:
        self._handle: int | None = handle
        self._lib = lib
        self._dict: dict[int, float] | None = None

    @classmethod
    def of(cls, law) -> CLaw:
        """`law` itself, or a dictionary copied into C in its own order."""
        if isinstance(law, CLaw):
            return law
        lib = load()
        items = list(law.items())
        n = len(items)
        keys = (ctypes.c_int64 * max(1, n))(*(k for k, _p in items))
        probs = (ctypes.c_double * max(1, n))(*(p for _k, p in items))
        handle = lib.hd_law_new(keys, probs, n)
        if not handle:
            raise MemoryError("law merge kernel: out of memory")
        return cls(handle, lib)

    def _as_dict(self) -> dict[int, float]:
        if self._dict is None:
            n = self._lib.hd_law_size(self._handle)
            keys = (ctypes.c_int64 * max(1, n))()
            probs = (ctypes.c_double * max(1, n))()
            self._lib.hd_law_copy(self._handle, keys, probs)
            self._dict = dict(zip(keys[:n], probs[:n]))
        return self._dict

    def items(self):
        return self._as_dict().items()

    def __iter__(self):
        return iter(self._as_dict())

    def __len__(self) -> int:
        return int(self._lib.hd_law_size(self._handle))

    def __getitem__(self, value: int) -> float:
        return self._as_dict()[value]

    def __del__(self) -> None:
        handle, self._handle = self._handle, None
        if handle:
            self._lib.hd_law_free(handle)


def merge(
    law_a,
    share_a: float,
    shift_a: int,
    law_b,
    share_b: float,
    shift_b: int,
    base: int,
    bin_width: int,
    cap: int,
):
    """`song_distribution.merge_laws`, in C: `(CLaw, bin_width)`."""
    lib = load()
    a, b = CLaw.of(law_a), CLaw.of(law_b)
    width = ctypes.c_int64(bin_width)
    handle = lib.hd_law_merge(
        a._handle, share_a, shift_a, b._handle, share_b, shift_b, base, ctypes.byref(width), cap
    )
    if not handle:
        raise MemoryError("law merge kernel: out of memory")
    return CLaw(handle, lib), int(width.value)


def enable() -> bool:
    """Switch the compiled merge on for this process (`song_distribution.MERGE_KERNEL`). Returns
    False, having changed nothing, when the kernel cannot be built; the Python merge then runs."""
    try:
        load()
    except Exception as exc:  # noqa: BLE001 - no compiler, a failed build
        print(f"law merge kernel unavailable ({exc!r}); using the Python path", file=sys.stderr)
        return False
    from hbuilder.domain import song_distribution

    song_distribution.MERGE_KERNEL = merge
    return True


if __name__ == "__main__":
    print(build(force="--build" in sys.argv))
