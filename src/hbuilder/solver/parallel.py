"""The worker count every process pool of the search starts with."""

from __future__ import annotations

import os

# numpy's OpenBLAS starts one thread per core in every process that imports numpy and reserves
# a buffer for each: ~490 MB of private memory a process on 16 cores, against 17 MB with one
# thread. Nothing here calls BLAS (numpy only hands arrays to HiGHS), so one thread costs
# nothing; with it the server and each worker hold ~480 MB less.
BLAS_THREAD_ENV = ("OPENBLAS_NUM_THREADS",)


def one_blas_thread() -> None:
    """Ask for one BLAS thread in this process and the ones it starts (they inherit the
    environment), unless the user set it. Effective only before numpy is first imported."""
    for name in BLAS_THREAD_ENV:
        os.environ.setdefault(name, "1")


# What one worker may hold: ~140 MB of private memory after a planned search (one BLAS
# thread), with room for what a long session's caches add.
WORKER_RAM_GB = 0.5
# The workers take at most this share of the machine's memory, after the server's own.
RAM_SHARE = 0.5
SERVER_RAM_GB = 1.0


def physical_ram_gb() -> float | None:
    """The machine's physical memory in GB, or None when it cannot be read."""
    try:
        if os.name == "nt":
            import ctypes  # noqa: PLC0415

            class MemoryStatus(ctypes.Structure):
                _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong),
                            ("total_phys", ctypes.c_ulonglong), ("avail_phys", ctypes.c_ulonglong),
                            ("total_page", ctypes.c_ulonglong), ("avail_page", ctypes.c_ulonglong),
                            ("total_virtual", ctypes.c_ulonglong), ("avail_virtual", ctypes.c_ulonglong),
                            ("avail_extended", ctypes.c_ulonglong)]
            status = MemoryStatus()
            status.length = ctypes.sizeof(status)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return None
            return status.total_phys / 2**30
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30
    except (AttributeError, OSError, ValueError):
        return None


def ram_workers(ram_gb: float | None) -> int | None:
    """How many workers fit in `ram_gb` of memory (`WORKER_RAM_GB` each, in `RAM_SHARE` of it
    after the server's), at least one; None when the memory is unknown. 16 GB: 14; 8 GB: 6."""
    if not ram_gb:
        return None
    return max(1, int((ram_gb * RAM_SHARE - SERVER_RAM_GB) / WORKER_RAM_GB))


def default_workers() -> int:
    """Every logical core (SMT pays on this branch-heavy work: 16 logical cores
    beat 8 physical ones by about a third), as many as the machine's memory holds
    (`ram_workers`). `os.cpu_count()` can return `None`.

    `HBUILDER_WORKERS` overrides it: the desktop app's Settings, for a PC that must stay
    usable while it searches."""
    raw = os.environ.get("HBUILDER_WORKERS", "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    cores = os.cpu_count() or 1
    fit = ram_workers(physical_ram_gb())
    return min(cores, fit) if fit is not None else cores

