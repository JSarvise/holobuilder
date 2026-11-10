"""The worker count follows the machine's memory, and every process asks for one BLAS thread."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from hbuilder.solver import parallel

REPO = Path(__file__).resolve().parents[1]


def test_workers_fit_the_memory(monkeypatch):
    monkeypatch.delenv("HBUILDER_WORKERS", raising=False)
    monkeypatch.setattr(os, "cpu_count", lambda: 16)
    for ram, expect in ((16.0, 14), (8.0, 6), (4.0, 2), (2.0, 1), (64.0, 16)):
        monkeypatch.setattr(parallel, "physical_ram_gb", lambda ram=ram: ram)
        assert parallel.default_workers() == expect, ram
    # 16 GB: the workers' budget and the server's fit in half the memory
    assert parallel.ram_workers(16.0) * parallel.WORKER_RAM_GB + parallel.SERVER_RAM_GB <= 8.0
    monkeypatch.setattr(parallel, "physical_ram_gb", lambda: None)
    assert parallel.default_workers() == 16  # memory unknown: every core
    monkeypatch.setenv("HBUILDER_WORKERS", "3")
    assert parallel.default_workers() == 3  # the user's setting wins


def test_physical_memory_is_read():
    ram = parallel.physical_ram_gb()
    assert ram is None or ram > 0.5


def test_one_blas_thread_reaches_the_server_and_the_pool():
    """`server.py` and `parallel_refine` set it before numpy loads (a pool's workers inherit
    it); a value the user set is kept."""
    code = (
        "import os, sys; sys.path.insert(0, 'src'); import hbuilder.solver.parallel_refine; "
        "print(os.environ.get('OPENBLAS_NUM_THREADS'))"
    )
    env = {k: v for k, v in os.environ.items() if k != "OPENBLAS_NUM_THREADS"}
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env, capture_output=True, text=True)
    assert out.stdout.strip() == "1", out.stderr
    out = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO,
        env={**env, "OPENBLAS_NUM_THREADS": "4"},
        capture_output=True,
        text=True,
    )
    assert out.stdout.strip() == "4", out.stderr
    server = (REPO / "tools" / "web" / "server.py").read_text(encoding="utf-8")
    assert server.index("one_blas_thread()") < server.index("from hbweb")
