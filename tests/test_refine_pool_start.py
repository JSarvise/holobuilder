"""`RefinePool` hands its workers the context through ONE pickled file: pickled per worker and
read by each worker's boot in turn, a 6 MB context kept a 16-worker pool from existing for ~6.7 s.
The workers get the same objects; the file goes with the pool."""

from __future__ import annotations

import multiprocessing
import os
from types import SimpleNamespace

from hbuilder.solver import parallel_refine as pr


class _Card(SimpleNamespace):
    pass


def test_the_workers_read_the_context_from_one_file_that_goes_with_the_pool(monkeypatch):
    started: list = []

    class FakePool:
        def __init__(self, processes, initializer, initargs):
            started.append(initargs)
            for _ in range(processes):  # each worker's initializer, here
                initializer(*initargs)

        def terminate(self):
            pass

        def join(self):
            pass

    real = multiprocessing.get_context

    def context(method):
        ctx = real(method)
        return SimpleNamespace(RawValue=ctx.RawValue, Pool=FakePool)

    monkeypatch.setattr(multiprocessing, "get_context", context)
    leaf = SimpleNamespace(cards=(_Card(id="card-00001-5-a-0000-00"),), charts=("chart",) * 3)
    pool = pr.RefinePool(leaf, workers=4).__enter__()
    path = started[0][0]
    assert isinstance(path, str) and os.path.isfile(path)  # a path, not the context
    assert pr._CTX == leaf and pr._BY_ID == {"card-00001-5-a-0000-00": leaf.cards[0]}
    pool.close()
    assert not os.path.exists(path)
