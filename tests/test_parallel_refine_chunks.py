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
            assert count / size >= workers * 8      # at least 8 chunks per worker
