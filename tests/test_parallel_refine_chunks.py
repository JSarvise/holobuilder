"""`parallel_refine._chunksize`: how a batch is cut for the worker pool. It can
only change throughput — a score is a pure function of its team — so the test
pins the two properties the callers rely on."""

from hbuilder.solver.parallel_refine import _chunksize


def test_a_small_batch_is_still_spread_one_team_at_a_time():
    for count in (1, 12, 64, 127):
        assert _chunksize(count, 16) == 1
    assert _chunksize(5, 0) == 1 and _chunksize(0, 16) == 1


def test_a_large_batch_is_cut_into_bounded_chunks_that_still_use_every_worker():
    for workers in (1, 4, 16):
        for count in (2_000, 20_000, 5_000_000):
            size = _chunksize(count, workers)
            assert 1 <= size <= 64
            assert count / size >= workers * 8  # at least 8 chunks per worker


def test_a_chunk_answers_in_order_and_sets_what_it_kept_aside_from_the_cyclic_collector():
    """`_run_chunk` (`WORKER_GC_FREEZE`): the answers are the tasks', in order, and what is
    alive afterwards is frozen, so the collector's full passes stop walking the caches."""
    import gc

    from hbuilder.solver import parallel_refine as pr

    gc.unfreeze()
    try:
        assert pr._run_chunk((lambda x: x * 2, [3, 1, 2])) == [6, 2, 4]
        assert gc.get_freeze_count() > 0 if pr.WORKER_GC_FREEZE else gc.get_freeze_count() == 0
    finally:
        gc.unfreeze()
