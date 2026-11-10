"""ctypes binding for `cdvar.c` — the cooldown screen, compiled.

Web/recon side only: nothing under `src/hbuilder/` imports this. The domain exposes a seam
(`song.COOLDOWN_SCREEN_KERNEL`) and `enable()` injects `screen` into it, for this process and,
through the environment, for every worker it spawns. `screen(spec)` takes the dict
`song._cooldown_screen_spec` builds and returns one ESTIMATED total per variant (an order for
activation-frequency choices, never a served number), or None to make the caller score them
exactly.

    python tools/web/kernel/cdvar.py --build      # compile the shared library
"""

from __future__ import annotations

import ctypes
import math
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "cdvar.c"
INCLUDED = HERE / "notewalk.c"  # cdvar.c includes it: a change there rebuilds this
if str(HERE) not in sys.path:  # loaded by file path in a worker too
    sys.path.insert(0, str(HERE))
import cbuild  # noqa: E402

LIBRARY = cbuild.library(HERE, "cdvar")
# the per-note `ceil`'s mean, added per note of a run
CEIL_BIAS = 0.5

_I32 = ctypes.POINTER(ctypes.c_int32)
_F64 = ctypes.POINTER(ctypes.c_double)


def build(force: bool = False) -> Path:
    """Compile the kernel if the library is missing or older than its sources."""
    stale = LIBRARY.exists() and LIBRARY.stat().st_mtime < INCLUDED.stat().st_mtime
    return cbuild.build(SOURCE, LIBRARY, "cooldown-screen", force or stale)


_lib: ctypes.CDLL | None = None
_lock = threading.Lock()


def load() -> ctypes.CDLL:
    """The loaded library, built on first use. Raises if it cannot be built."""
    global _lib
    with _lock:
        if _lib is None:
            lib = ctypes.CDLL(str(build()))
            lib.hd_cdvar.restype = ctypes.c_double
            lib.hd_cdvar.argtypes = [
                ctypes.c_int,
                _F64,
                _F64,
                ctypes.c_double,
                ctypes.c_double,
                ctypes.c_double,
                _F64,
                ctypes.c_int,
                ctypes.c_double,
                ctypes.c_int,
                _I32,
                _F64,
                ctypes.c_int,
                _I32,
                _F64,
                _I32,
                _F64,
                ctypes.c_int,
                _I32,
                _F64,
                ctypes.c_int,
                _I32,
                ctypes.c_int,
                _I32,
                ctypes.c_int,
                _F64,
                _F64,
            ]
            _lib = lib
    return _lib


# a chart's note times and the prefix sums of its note weights as C arrays, keyed by the
# identity of the tuples `song` hands (kept alive in the entry, so an id is never reused)
_chart_cache: dict[tuple[int, int], tuple] = {}


def _chart_arrays(times, note_weights):
    key = (id(times), id(note_weights))
    hit = _chart_cache.get(key)
    if hit is None or hit[0] is not times or hit[1] is not note_weights:
        prefix = [0.0]
        for w in note_weights:
            prefix.append(prefix[-1] + w)
        hit = (
            times,
            note_weights,
            (ctypes.c_double * len(times))(*times),
            (ctypes.c_double * len(prefix))(*prefix),
        )
        if len(_chart_cache) > 512:
            _chart_cache.clear()
        _chart_cache[key] = hit
    return hit[2], hit[3]


def _ints(values):
    return (ctypes.c_int32 * max(1, len(values)))(*values)


def _doubles(values):
    return (ctypes.c_double * max(1, len(values)))(*values)


def screen(spec: dict) -> list[float] | None:
    """One estimated total per variant of `spec` (`song._cooldown_screen_spec`), or None when
    the kernel declines one of them (more terms than it holds)."""
    lib = load()
    times, prefix = _chart_arrays(spec["times"], spec["note_weights"])
    count = spec["variants"]
    out = (ctypes.c_double * max(1, count))()
    rates = spec["score_rates"]
    lib.hd_cdvar(
        len(spec["times"]),
        times,
        prefix,
        spec["song_seconds"],
        spec["constant"],
        CEIL_BIAS,
        _doubles(rates),
        len(rates),
        spec["skill_effect_up"],
        len(spec["fixed_d"]) // 2,
        _ints(spec["fixed_i"]),
        _doubles(spec["fixed_d"]),
        len(spec["active_d"]) // 7,
        _ints(spec["active_i"]),
        _doubles(spec["active_d"]),
        _ints(spec["rider_host"]),
        _doubles(spec["rider_q"]),
        len(spec["host_d"]) // 10,
        _ints(spec["host_i"]),
        _doubles(spec["host_d"]),
        len(spec["order"]) // 2,
        _ints(spec["order"]),
        len(spec["costume"]),
        _ints(spec["costume"]),
        count,
        _doubles(spec["cd"]),
        out,
    )
    values = list(out[:count])
    if any(math.isnan(v) for v in values):
        return None
    return values


def enable() -> bool:
    """Switch the compiled screen on for this process AND every worker it spawns (the
    module attribute here, `HBUILDER_COOLDOWN_SCREEN_KERNEL` for the children). False,
    having changed nothing, when it cannot be built: the variants are then scored exactly."""
    import os

    try:
        load()
    except Exception as exc:  # noqa: BLE001 - no compiler, a failed build
        print(f"cooldown-screen kernel unavailable ({exc!r}); variants are scored exactly", file=sys.stderr)
        return False
    from hbuilder.domain import song

    os.environ[song.COOLDOWN_SCREEN_KERNEL_ENV] = f"{Path(__file__).resolve()}:screen"
    song.COOLDOWN_SCREEN_KERNEL = screen
    return True


if __name__ == "__main__":
    print(build(force="--build" in sys.argv))
