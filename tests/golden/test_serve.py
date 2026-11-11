"""`serve.py`: the copies of a test server that is no longer running are swept at the next start."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import serve  # noqa: E402


def _copies(root: Path, name: str, pid: int | None) -> Path:
    box = root / (serve.PREFIX + name)
    (box / "box" / "state").mkdir(parents=True)
    (box / "box" / "state" / "collection.json").write_text("{}")
    if pid is not None:
        (box / serve.PID_FILE).write_text(str(pid))
    return box


def test_sweep_deletes_only_the_copies_of_stopped_servers(tmp_path):
    dead_pid = subprocess.Popen([sys.executable, "-c", "pass"])
    dead_pid.wait()
    stopped = _copies(tmp_path, "stopped", dead_pid.pid)
    no_pid = _copies(tmp_path, "nopid", None)
    running = _copies(tmp_path, "running", os.getpid())
    other = tmp_path / "hbgold_other"
    other.mkdir()

    gone = serve.sweep_stale(tmp_path)

    assert sorted(gone) == sorted([stopped, no_pid])
    assert not stopped.exists() and not no_pid.exists()
    assert running.is_dir() and other.is_dir()
