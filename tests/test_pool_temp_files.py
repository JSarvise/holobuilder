"""A search pool's temp files (the pickled context, the plan store) never outlive their process:
`close()` removes them, exit removes those of a pool never closed, and the next process's sweep
removes those of a process that died first."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from hbuilder.solver import parallel_refine as pr

SRC = str(Path(pr.__file__).resolve().parents[2])
MAKE = (
    "import os, sys\n"
    "from hbuilder.solver import parallel_refine as pr\n"
    "print(pr._new_context_file({'cards': 1}))\n"
    "print(pr._new_store_dir())\n"
    "sys.stdout.flush()\n"
)


def _run(tmp_path: Path, tail: str) -> list[str]:
    env = {
        **os.environ,
        "PYTHONPATH": SRC,
        "TMP": str(tmp_path),
        "TEMP": str(tmp_path),
        "TMPDIR": str(tmp_path),
    }
    out = subprocess.run(
        [sys.executable, "-c", MAKE + tail], env=env, capture_output=True, text=True, check=True, timeout=60
    )
    made = out.stdout.split()
    assert len(made) == 2 and all(Path(p).parent == tmp_path for p in made)
    return made


def test_a_process_that_exits_removes_its_temp_files(tmp_path):
    made = _run(tmp_path, "")
    assert not any(os.path.exists(p) for p in made)


def test_the_files_of_a_process_that_died_go_with_the_next_sweep(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "_SWEEP_STARTED", True)  # no sweep of the real temp folder here
    made = _run(tmp_path, "os._exit(0)\n")  # no atexit: a killed process
    assert all(os.path.exists(p) for p in made)
    own = Path(pr._new_store_dir())  # this process: alive, kept
    try:
        live = tmp_path / own.name
        live.mkdir()
        legacy_old, legacy_new = tmp_path / "hbctx_ab12cd34.pickle", tmp_path / "hbplans_x1y2z3"
        legacy_old.write_bytes(b"x")
        legacy_new.mkdir()
        old = time.time() - pr.LEGACY_TEMP_AGE_S - 60
        os.utime(legacy_old, (old, old))
        other = tmp_path / "unrelated.pickle"
        other.write_bytes(b"x")
        removed = set(pr.sweep_stale_temp(str(tmp_path)))
        assert removed == {*made, str(legacy_old)}
        assert live.exists() and legacy_new.exists() and other.exists()
    finally:
        pr._drop_temp(str(own))
    assert not own.exists()


def test_the_names_carry_this_process_id(monkeypatch):
    monkeypatch.setattr(pr, "_SWEEP_STARTED", True)
    path = pr._new_context_file({"cards": 1})
    try:
        assert Path(path).name.startswith(f"hbctx-{os.getpid()}-") and path in pr._OWNED_TEMP
    finally:
        pr._drop_temp(path)
    assert not os.path.exists(path) and path not in pr._OWNED_TEMP
    assert pr.pid_alive(os.getpid())
    done = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True
    )
    assert not pr.pid_alive(int(done.stdout))
