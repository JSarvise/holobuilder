"""An engine retired while a search runs on it (a refresh moved its inputs, or a newer engine
pushed it out of MAX_ENGINES) keeps its pool until that search ends; a job no page ever reads
is cancelled and leaves the registry."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

KERNELS = Path(__file__).resolve().parent / "kernel"  # ondemand imports the C kernels' loaders
if str(KERNELS) not in sys.path:
    sys.path.insert(0, str(KERNELS))

import ondemand  # noqa: E402
from hbweb import jobs  # noqa: E402


class _Pool:
    def __init__(self) -> None:
        self.closed = 0

    def __exit__(self, *exc) -> None:
        self.closed += 1


class _Engine(ondemand.Engine):
    """An engine with nothing loaded: only the lifecycle `engine()` drives."""

    def __init__(self, basis, difficulty, **_kw) -> None:  # noqa: D107 -- no data, no pool
        self.basis, self.key, self.closed, self.pool = basis, basis, False, _Pool()
        self._users, self._users_lock = 0, threading.Lock()


def test_a_closed_engine_keeps_its_pool_until_its_search_ends():
    eng = _Engine("account", "EXPERT").acquire()
    eng.close()
    assert eng.closed and eng.pool.closed == 0  # the search on it still runs
    eng.release()
    assert eng.pool.closed == 1
    eng.close()
    eng.release()  # a stray release changes nothing
    assert eng.pool.closed == 1
    idle = _Engine("maxed", "EXPERT")
    idle.close()
    assert idle.pool.closed == 1


def test_an_engine_pushed_out_while_searching_closes_when_its_search_ends(monkeypatch):
    monkeypatch.setattr(ondemand, "Engine", _Engine)
    monkeypatch.setattr(ondemand, "_ENGINES", {})
    monkeypatch.setattr(ondemand, "MAX_ENGINES", 2)
    first = ondemand.engine("a", "EXPERT")  # a search runs on it
    for basis in ("b", "c"):
        ondemand.engine(basis, "EXPERT").release()
    assert first.closed and first.pool.closed == 0
    first.release()
    assert first.pool.closed == 1


def test_a_job_whose_stream_nobody_opens_is_cancelled_and_dropped(monkeypatch):
    monkeypatch.setattr(jobs, "STREAM_GRACE_S", 0.2)

    def runner(job, _opts):
        job.cancel.wait(30)
        job.done = True
        job.events.put({"type": "end"})

    unread = jobs.launch(runner, {}, "test")
    read = jobs.launch(runner, {}, "test")
    read.streamed = True  # a page opened its stream
    deadline = time.monotonic() + 10
    while unread.job_id in jobs.JOBS and time.monotonic() < deadline:
        time.sleep(0.05)
    assert unread.cancel.is_set() and unread.job_id not in jobs.JOBS
    assert not read.cancel.is_set() and read.job_id in jobs.JOBS
    read.cancel.set()
    with jobs.JOBS_LOCK:
        jobs.JOBS.pop(read.job_id, None)
