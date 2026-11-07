"""A disposable copy of a checkout whose data paths are the frozen snapshot.

Every module finds its data relative to its own file (``REPO_ROOT = Path(__file__).parents[2]``
and friends), so the golden runs execute a copy of the code under test with the frozen inputs
laid over it. Nothing a run writes can reach the live files or the snapshot itself.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DUMPS_REL = "tools/recon/proxy/dumps/"
KERNEL_REL = Path("tools/web/kernel")
# A checkout links dumps/ to the live data: its tracked files there would be the LIVE ones.
CODE_SKIP_PREFIXES = (DUMPS_REL,)

# Fixed instant for date-dependent views (unix seconds). It must fall inside the event chapter
# that was open when the snapshot was frozen; a snapshot's own `clock.txt` overrides it.
DEFAULT_CLOCK = 1793707200


def clock_of(frozen: Path) -> float:
    path = frozen / "clock.txt"
    return float(path.read_text().strip()) if path.is_file() else float(DEFAULT_CLOCK)


def _git_files(code: Path) -> list[str]:
    out = subprocess.run(["git", "-C", str(code), "ls-files", "-z", "--cached", "--others",
                          "--exclude-standard"], check=True, capture_output=True).stdout
    return sorted({p for p in out.decode("utf-8").split("\0") if p})


def build(code: Path, frozen: Path, dest: Path | None = None) -> Path:
    """Copy the code of `code` and overlay `frozen/inputs` into a new directory."""
    code, frozen = code.resolve(), frozen.resolve()
    dest = Path(tempfile.mkdtemp(prefix="hbgold_")) if dest is None else dest
    dest.mkdir(parents=True, exist_ok=True)
    for rel in _git_files(code):
        if rel.startswith(CODE_SKIP_PREFIXES):
            continue
        src = code / rel
        if not src.is_file():
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
    # the compiled kernels are build products, not tracked
    for lib in (code / KERNEL_REL).glob("*"):
        if lib.suffix in (".dll", ".so") and lib.is_file():
            shutil.copy2(lib, dest / KERNEL_REL / lib.name)
    inputs = frozen / "inputs"
    for dirpath, _dirs, files in os.walk(inputs):
        for name in files:
            src = Path(dirpath) / name
            target = dest / src.relative_to(inputs)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
    return dest


def remove(sandbox: Path) -> None:
    """Delete a sandbox. It holds plain copies only (no junctions), so this is safe."""
    for dirpath, dirnames, _files in os.walk(sandbox):
        for d in dirnames:
            p = Path(dirpath) / d
            if p.is_symlink() or (hasattr(os.path, "isjunction") and os.path.isjunction(p)):
                raise RuntimeError(f"refusing to delete a sandbox holding a link: {p}")
    shutil.rmtree(sandbox, ignore_errors=True)


def env(sandbox: Path, frozen: Path, *, audit_log: Path, workers: int | None,
        clock: float | None = None) -> dict[str, str]:
    """The environment of every process of a golden run."""
    e = dict(os.environ)
    for key in list(e):
        # HB_PLAN_TIMING only logs the planner's stage times (board_plan_solver)
        if key.startswith(("HB_", "HBUILDER_")) and key != "HB_PLAN_TIMING":
            del e[key]
    local = frozen / "localappdata" / "AppData" / "Local"
    local.mkdir(parents=True, exist_ok=True)
    e.update({
        "PYTHONPATH": str(sandbox / "tests" / "golden" / "site"),
        "PYTHONHASHSEED": "0",
        "PYTHONUTF8": "1",
        "HB_GOLDEN_CLOCK": str(clock_of(frozen) if clock is None else clock),
        "HB_GOLDEN_AUDIT": str(audit_log),
        "HB_GOLDEN_OFFLINE": "1",
        "LOCALAPPDATA": str(local),
        "HB_MASTER_REPO": str(frozen / "master_repo"),
        "HB_MASTER_NO_FETCH": "1",
        "HB_CREDENTIAL_FILE": str(sandbox / "state" / "no_credential.json"),
        "HBUILDER_CUTOFFS_POLL_MIN": "0",
    })
    if workers is not None:
        e["HBUILDER_WEB_WORKERS"] = str(workers)
        e["HBUILDER_WORKERS"] = str(workers)
    return e


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    """`tools/web/server.py` running inside a sandbox."""

    def __init__(self, sandbox: Path, environ: dict[str, str], python: str, log: Path):
        self.port = free_port()
        self.log = log.open("w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [python, "-u", str(sandbox / "tools" / "web" / "server.py"), "--port", str(self.port)],
            cwd=sandbox, env=environ, stdout=self.log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"server exited early, see {log}")
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=1):
                    return
            except OSError:
                time.sleep(0.2)
        raise RuntimeError("server did not start")

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.log.close()


def python_exe() -> str:
    return os.environ.get("HB_GOLDEN_PYTHON") or sys.executable
