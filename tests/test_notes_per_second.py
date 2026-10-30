"""`song.notes_per_second`: the per-second note density the skill-uptime
timeline draws (docs/redesign/DESIGN-V3.md §5c). It is believed on sight, so the
two properties pinned are conservation (every note counted exactly once) and
the bin edges (half-open `[k, k+1)`)."""

from hbuilder.domain.song import notes_per_second


def test_empty_chart_has_no_bins():
    assert notes_per_second(()) == []


def test_bins_are_half_open_seconds():
    assert notes_per_second((0.0, 0.99, 1.0, 1.5, 3.0)) == [2, 2, 0, 1]


def test_every_note_counted_once_and_last_second_kept():
    times = tuple(i * 0.37 for i in range(500))
    bins = notes_per_second(times)
    assert sum(bins) == 500
    assert len(bins) == int(max(times)) + 1
