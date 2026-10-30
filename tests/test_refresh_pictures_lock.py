"""refresh_all: one picture run at a time. A background run (--pictures-later) takes about an
hour the first time; a refresh started meanwhile leaves the pictures to it."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "recon" / "proxy"))

import refresh_all  # noqa: E402


def _lock(tmp_path, pid, age_s=0.0):
    (tmp_path / "pictures_refresh.lock").write_text(
        json.dumps({"pid": pid, "started": time.time() - age_s}), "utf8")


def test_only_a_live_recent_other_process_holds_the_pictures(tmp_path, monkeypatch):
    monkeypatch.setattr(refresh_all, "DUMPS", tmp_path)
    assert refresh_all.pictures_busy() is None                     # no lock
    _lock(tmp_path, os.getppid())
    assert refresh_all.pictures_busy() == os.getppid()            # another live process
    _lock(tmp_path, os.getppid(), age_s=refresh_all.PICTURES_LOCK_MAX_S + 60)
    assert refresh_all.pictures_busy() is None                     # a stopped run's leftover
    _lock(tmp_path, os.getpid())
    assert refresh_all.pictures_busy() is None                     # this process's own
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    _lock(tmp_path, gone.pid)
    assert refresh_all.pictures_busy() is None                     # a process that ended
    (tmp_path / "pictures_refresh.lock").write_text("not json", "utf8")
    assert refresh_all.pictures_busy() is None


def test_a_refresh_during_a_picture_run_skips_the_pictures(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(refresh_all, "DUMPS", tmp_path)
    _lock(tmp_path, os.getppid())
    assert refresh_all.main(["--dry-run", "--only", "card_art,chibi,songs"]) == 0
    plan = capsys.readouterr().out
    for step in ("card_art", "chibi"):
        line = next(ln for ln in plan.splitlines() if f"] {step} " in ln)
        assert line.lstrip().startswith("[skip]") and "fetching the pictures" in line
    assert next(ln for ln in plan.splitlines() if "] songs " in ln).lstrip().startswith("[run ]")


def test_the_lock_is_this_process_until_it_ends(tmp_path, monkeypatch):
    monkeypatch.setattr(refresh_all, "DUMPS", tmp_path)
    refresh_all.hold_pictures_lock()
    doc = json.loads((tmp_path / "pictures_refresh.lock").read_text("utf8"))
    assert doc["pid"] == os.getpid() and abs(doc["started"] - time.time()) < 60
    assert refresh_all.pictures_busy() is None                     # never busy for itself
