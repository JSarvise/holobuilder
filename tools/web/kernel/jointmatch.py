"""ctypes binding for `jointmatch.c` -- the dynamic Hungarian matching of
`hbuilder.domain.joint_matching.JointMatching`, compiled. `connect.c` includes it (one library:
`connect.py`'s joint deal solves a song's bench matching built here), so this loads that library.

Web/recon side only: nothing under `src/hbuilder/` imports this. The domain exposes the seam
`board_plan.JOINT_MATCHING` (the matching class a joint deal uses) and `enable()` injects this
one, as `connect.py` does for its kernels.

    python tools/web/kernel/jointmatch.py --build      # compile the shared library (connect's)

Compiled with `cbuild.py`'s flags, so the result is bit-identical to the Python it replaces.
"""

from __future__ import annotations

import ctypes
import sys
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:  # loaded by file path in a worker too
    sys.path.insert(0, str(HERE))
import connect  # noqa: E402

MAX_COLS = 256

_lib: ctypes.CDLL | None = None
_I32P = ctypes.POINTER(ctypes.c_int32)
_F64P = ctypes.POINTER(ctypes.c_double)


def build(force: bool = False) -> Path:
    """Compile the library this kernel lives in (`connect`'s) if missing or stale."""
    return connect.build(force)


def load() -> ctypes.CDLL:
    """The loaded library, built on first use. Raises if it cannot be built."""
    global _lib
    if _lib is None:
        lib = connect.load()
        lib.hd_jm_new.restype = ctypes.c_void_p
        lib.hd_jm_new.argtypes = [ctypes.c_int, ctypes.c_int, _F64P, _I32P]
        lib.hd_jm_solve.restype = ctypes.c_void_p
        lib.hd_jm_solve.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, _I32P, _F64P]
        lib.hd_jm_assignment.restype = ctypes.c_int
        lib.hd_jm_assignment.argtypes = [ctypes.c_void_p, _I32P, _I32P]
        lib.hd_jm_state.restype = ctypes.c_int
        lib.hd_jm_state.argtypes = [ctypes.c_void_p, _F64P, _F64P, _I32P]
        lib.hd_jm_free.restype = None
        lib.hd_jm_free.argtypes = [ctypes.c_void_p]
        _lib = lib
    return _lib


class _Scratch(threading.local):
    """One re-solve buffer per (thread, sizes): a team's matching is read and dropped before
    the next one is solved."""

    def __init__(self) -> None:
        self.by_size: dict[tuple[int, int, int], int] = {}


_SCRATCH = _Scratch()


class CJointMatching:
    """`JointMatching` on the compiled kernel: the same constructor, `solve`, `assignment`,
    `value` and `assignment_after`, the same floats."""

    __slots__ = ("_handle", "n", "m", "rows", "_lib", "_owned")

    def __init__(self, weights: Sequence[Sequence[float]], capacities: Sequence[int]) -> None:
        lib = load()
        n, m = len(weights), len(capacities)
        if m + 1 > MAX_COLS:
            raise ValueError(f"{m} card groups: the kernel takes at most {MAX_COLS - 1}")
        flat = (ctypes.c_double * max(1, n * m))(*[w for row in weights for w in row])
        caps = (ctypes.c_int32 * max(1, m))(*capacities)
        handle = lib.hd_jm_new(n, m, flat, caps)
        if not handle:
            raise ValueError("the matching could not be built")
        self._handle, self._lib, self._owned = handle, lib, True
        self.n, self.m, self.rows = n, m, n + sum(capacities)

    @classmethod
    def _wrap(cls, handle: int, like: CJointMatching, owned: bool) -> CJointMatching:
        out = object.__new__(cls)
        out._handle, out._lib, out._owned = handle, like._lib, owned
        out.n, out.m, out.rows = like.n, like.m, like.rows
        return out

    def __del__(self) -> None:
        if getattr(self, "_owned", False) and self._handle:
            self._lib.hd_jm_free(self._handle)
            self._handle = None

    def _solve_into(self, changes: Mapping[int, Sequence[float]], into) -> int:
        order = sorted(changes)
        rows = (ctypes.c_int32 * max(1, len(order)))(*order)
        flat = (ctypes.c_double * max(1, len(order) * self.m))(*[w for r in order for w in changes[r]])
        for r in order:
            if len(changes[r]) != self.m:
                raise ValueError(f"row {r} has {len(changes[r])} weights, not {self.m}")
        handle = self._lib.hd_jm_solve(self._handle, into, len(order), rows, flat)
        if not handle:
            raise ValueError("a changed row is not a socket row")
        return handle

    def solve(self, changes: Mapping[int, Sequence[float]]) -> CJointMatching:
        """A new matching with each socket row of `changes` given new weights, re-solved."""
        return self._wrap(self._solve_into(changes, None), self, True)

    def assignment_after(self, changes: Mapping[int, Sequence[float]]) -> dict[int, int]:
        """`solve(changes).assignment()`, in a buffer kept per thread instead of a new matching."""
        key = (self.n, self.m, self.rows)
        scratch = _SCRATCH.by_size.get(key)
        if scratch is None:
            scratch = self._solve_into(changes, None)
            _SCRATCH.by_size[key] = scratch
        else:
            self._solve_into(changes, scratch)
        return self._wrap(scratch, self, False).assignment()

    def assignment(self) -> dict[int, int]:
        """`{socket row: group}` of the rows holding a card at a positive weight."""
        rows = (ctypes.c_int32 * max(1, self.n))()
        cols = (ctypes.c_int32 * max(1, self.n))()
        count = self._lib.hd_jm_assignment(self._handle, rows, cols)
        return {rows[t]: cols[t] for t in range(count)}

    def state(self) -> tuple[list[float], list[float], list[int]]:
        """`(u, v, col_of_row)`, for the bit-identity test."""
        u = (ctypes.c_double * max(1, self.rows))()
        v = (ctypes.c_double * (self.m + 1))()
        col = (ctypes.c_int32 * max(1, self.rows))()
        self._lib.hd_jm_state(self._handle, u, v, col)
        return list(u[: self.rows]), list(v), list(col[: self.rows])


def enable() -> bool:
    """Switch the compiled matching on for this process AND every worker it spawns (the
    module attribute, `HBUILDER_JOINT_MATCHING` for spawned children). Returns False, having
    changed nothing, when the kernel cannot be built; the joint deal then runs on Python."""
    import os

    try:
        load()
    except Exception as exc:  # noqa: BLE001 - no compiler, a failed build
        print(f"joint matching kernel unavailable ({exc!r}); using the Python path", file=sys.stderr)
        return False
    from hbuilder.domain import board_plan

    os.environ[board_plan.JOINT_MATCHING_ENV] = f"{Path(__file__).resolve()}:CJointMatching"
    board_plan.JOINT_MATCHING = CJointMatching
    return True


if __name__ == "__main__":
    print(build(force="--build" in sys.argv))
