"""A test server for checks in the browser: the web server running on COPIES of the code and of
every file it reads, never on the checkout's own `state/`, `data/` and dumps.

A page under test can write (the collection editor, a refresh, an admin route): here it writes
to the copies, which are deleted when the server stops.

    python tests/golden/serve.py --fresh               # today's data/, state/ and dumps, copied now
    python tests/golden/serve.py --frozen <snapshot>   # a golden snapshot (`freeze.py`), faster
    ... [--art] [--port N] [--workers N]

`--fresh` copies the inputs the server reads (`freeze.py`; ~1.4 GB on disk with the sandbox, a
minute or two); `--art` also copies the pictures and models the pages show (card art, skill
icons, Live2D, chibi, banners: ~1.9 GB), which a snapshot leaves out. The server runs offline (loopback only, no
account credential) at the snapshot's clock (`--fresh`: now). It prints its URL and runs until
Ctrl-C or until it is killed; the copies of a server that was killed are deleted by the next
start.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "src"))

import freeze  # noqa: E402
import sandbox  # noqa: E402

PREFIX = "hbserve_"
PID_FILE = "serve.pid"
# What `--art` copies (relative to the checkout): the pictures and models the pages show,
# which a snapshot leaves out (`freeze.DUMPS_SKIP_DIRS`, `freeze.DATA_SKIP_DIRS`)
ART = (
    "tools/recon/proxy/dumps/card_images",
    "tools/recon/proxy/dumps/skill_icons",
    "tools/recon/proxy/dumps/live2d",
    "tools/recon/proxy/dumps/chibi",
    "data/gacha/art",
    "tools/explorer/cards_explorer_art",
)


def sweep_stale(tmp: Path) -> list[Path]:
    """Delete the copies of test servers no longer running (their `serve.pid` names a dead
    process, or there is none). `sandbox.remove` refuses a tree that holds a link."""
    from hbuilder.solver.parallel_refine import pid_alive  # noqa: PLC0415

    gone = []
    for root in sorted(tmp.glob(PREFIX + "*")):
        if not root.is_dir():
            continue
        try:
            pid = int((root / PID_FILE).read_text().strip())
        except (OSError, ValueError):
            pid = 0
        if pid and pid_alive(pid):
            continue
        sandbox.remove(root)
        gone.append(root)
    return gone


def die_with_this_process() -> None:
    """On Windows, put this process in a job that kills every process in it when the job's
    last handle closes: the server and its workers, started after this, are in it too, so
    killing this process stops them (otherwise they would keep the port and the copies)."""
    if sys.platform != "win32":
        return
    import ctypes  # noqa: PLC0415
    from ctypes import wintypes  # noqa: PLC0415

    class Basic(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class Extended(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", Basic),
            ("IoInfo", ctypes.c_ulonglong * 6),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    job = kernel32.CreateJobObjectW(None, None)
    info = Extended()
    info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = job and kernel32.SetInformationJobObject(
        wintypes.HANDLE(job), 9, ctypes.byref(info), ctypes.sizeof(info)
    )  # 9: JobObjectExtendedLimitInformation
    if not ok or not kernel32.AssignProcessToJobObject(
        wintypes.HANDLE(job), wintypes.HANDLE(kernel32.GetCurrentProcess())
    ):
        print("note: the server will outlive this process if it is killed", flush=True)
    # the handle stays open for this process's life, and closes with it


def copy_art(box: Path) -> int:
    """Copy `ART` from the checkout into the sandbox; returns the bytes copied."""
    copied = 0
    for rel in ART:
        src = REPO / rel
        if not src.is_dir():
            continue
        for dirpath, _dirs, files in os.walk(src):
            for name in files:
                s = Path(dirpath) / name
                t = box / rel / s.relative_to(src)
                t.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(s, t)
                copied += t.stat().st_size
    return copied


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tests/golden/serve.py", description=__doc__.split("\n\n")[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--fresh", action="store_true", help="copy this checkout's inputs now")
    src.add_argument("--frozen", type=Path, help="a golden snapshot directory (freeze.py)")
    ap.add_argument("--art", action="store_true", help="also copy the pictures and models the pages show")
    ap.add_argument("--port", type=int, default=None, help="default: a free port")
    ap.add_argument("--workers", type=int, default=None, help="the search's worker processes")
    args = ap.parse_args(argv)

    die_with_this_process()
    tmp = Path(tempfile.gettempdir())
    for root in sweep_stale(tmp):
        print(f"deleted the copies of a stopped test server: {root}", flush=True)
    root = Path(tempfile.mkdtemp(prefix=PREFIX))
    (root / PID_FILE).write_text(str(os.getpid()))
    server = None
    try:
        snap = root / "snapshot"
        if args.fresh:
            master = Path(os.environ.get("HB_MASTER_REPO") or REPO.parent / "hololive-dreams-master")
            print("copying this checkout's inputs ...", flush=True)
            freeze.freeze(REPO, snap, master)
            clock = time.time()
            inputs_of = snap
        else:
            frozen = args.frozen.resolve()
            # the process environment points into the snapshot (LOCALAPPDATA, the master
            # repo): those are copied too, so nothing the server writes reaches it
            for part in ("localappdata", "master_repo"):
                if (frozen / part).is_dir():
                    shutil.copytree(frozen / part, snap / part)
            clock = sandbox.clock_of(frozen)
            inputs_of = frozen
        box = sandbox.build(REPO, inputs_of, root / "box")
        if args.art:
            print("copying the art ...", flush=True)
            print(f"  {copy_art(box) / 1e9:.1f} GB", flush=True)
        environ = sandbox.env(box, snap, audit_log=root / "audit.tsv", workers=args.workers, clock=clock)
        environ.pop("HB_GOLDEN_AUDIT", None)  # no file-access log for an interactive server
        server = sandbox.Server(box, environ, sandbox.python_exe(), root / "server.log", port=args.port)
        print(f"test server: {server.base}/optimizer", flush=True)
        print(f"  copies in {root} (deleted when it stops); log {root / 'server.log'}", flush=True)
        server.proc.wait()
        print(f"the server exited ({server.proc.returncode}), see {root / 'server.log'}", flush=True)
        return 1
    except KeyboardInterrupt:
        return 0
    finally:
        if server is not None:
            server.stop()
        sandbox.remove(root)


if __name__ == "__main__":
    sys.exit(main())
