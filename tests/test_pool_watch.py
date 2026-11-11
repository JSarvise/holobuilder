"""A worker process that dies, or a task that raises, never hangs a search nor leaves it in one
process for good: the wait on the workers notices the death, the batch's missing teams are scored
in this process with a warning, and the pool (which replaced the dead worker) serves the next batch."""

from __future__ import annotations

import multiprocessing
import os
import time

import pytest

from hbuilder.solver import parallel_refine as pr


def _square_or_die(item):
    index, value = item
    if value == "die":
        os._exit(3)  # the worker is gone: no answer, no exception
    if value == "raise":
        raise ValueError("a task's own error")
    return index, value * value


@pytest.fixture
def pool():
    workers = multiprocessing.get_context("spawn").Pool(2)
    try:
        yield workers
    finally:
        workers.terminate()
        workers.join()


def test_a_dead_worker_ends_the_wait_instead_of_hanging_it(pool):
    out: list = [None] * 4
    t0 = time.monotonic()
    with pytest.raises(pr.WorkerDied):
        pr._gather(pool, _square_or_die, list(enumerate([1, "die", 3, 4])), 2, out)
    assert time.monotonic() - t0 < 30
    # the pool replaced the worker: the next batch runs on it
    again: list = [None] * 3
    pr._gather(pool, _square_or_die, list(enumerate([5, 6, 7])), 1, again)
    assert again == [25, 36, 49]


@pytest.mark.parametrize("chunksize", [1, 2, 3, 64])
def test_every_answer_arrives_whatever_the_chunk_size(pool, chunksize):
    # the batches go out in chunks (`_chunksize`): each answer lands at its own index
    out: list = [None] * 10
    pr._gather(pool, _square_or_die, list(enumerate(range(10))), chunksize, out)
    assert out == [v * v for v in range(10)]


def test_a_task_that_raises_raises_its_own_error(pool):
    with pytest.raises(ValueError, match="own error"):
        pr._gather(pool, _square_or_die, list(enumerate([1, "raise", 3])), 2, [None] * 3)


def test_a_failed_batch_is_reported_and_the_pool_kept(pool):
    refine = pr.RefinePool.__new__(pr.RefinePool)
    refine._pool, refine.workers, refine.warnings, refine.on_fallback = pool, 2, [], None
    out: list = [None] * 4
    refine._gather_or_warn(_square_or_die, list(enumerate([1, "die", 3, 4])), out)
    assert out.count(None) >= 1 and refine._pool is pool
    (line,) = refine.take_warnings()
    assert "WorkerDied" in line and "single process" in line
    assert refine.take_warnings() == []
    again: list = [None] * 2
    refine._gather_or_warn(_square_or_die, list(enumerate([2, 3])), again)
    assert again == [4, 9] and refine.warnings == []
