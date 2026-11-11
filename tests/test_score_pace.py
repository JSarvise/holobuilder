"""The score pace (`song_distribution.score_pace`): for the runs that end in each bar of the
run-score histogram, the score made by each Special cue and the end.

It joins a forward and a reverse sweep of the same law at each cut. On a chart short enough
to enumerate every roll, it must equal the brute force: each bar's mass, and the mean score
by each cut of the runs that end in it. And on any chart, the bars' means weighted by their
masses give back the mean score by the cut (`E[S] = SUM_I P(I) E[S | I]`), which is the
Expected score of the notes before the cut up to rounding."""

from __future__ import annotations

import itertools
import math

import pytest
from support.builders import FIXTURE_CARDS_PATH, make_scoring_config

from hbuilder.domain.loader import load_cards
from hbuilder.domain.song import ChartNote, SongChart, overall_power_for, score_song_detailed
from hbuilder.domain.song_distribution import _Sweep, run_score_distribution


@pytest.fixture(scope="module")
def cards():
    return load_cards(FIXTURE_CARDS_PATH)


@pytest.fixture(scope="module")
def cfg():
    return make_scoring_config()


def _chart(seconds: float = 70.0, cues=(8.0, 21.0, 33.0, 46.0, 58.0)) -> SongChart:
    return SongChart(
        music_id="m0001",
        difficulty="EXPERT",
        difficulty_level=26,
        live_score_coefficient_permil=4,
        notes=tuple(ChartNote(time=0.5 * t, perfect_permil=1000) for t in range(1, int(seconds * 2))),
        special_cues=cues,
    )


def _row(cards, cfg, chart, leader_index=0):
    leader = cards[leader_index]
    members = tuple(c for i, c in enumerate(cards) if i != leader_index)[:5]
    power = overall_power_for(members, leader, cfg)
    best = score_song_detailed(members, (leader,), chart, cfg, deck_power=power)[0]
    return members, leader, best


def _brute_force(sweep: _Sweep, boundaries):
    """Every roll enumerated: `[(probability, [S at each boundary], F)]`."""
    keys = list(sweep.first)
    assert len(keys) <= 16, "too many windows to enumerate"
    runs = []
    for flags in itertools.product((False, True), repeat=len(keys)):
        prob = 1.0
        for key, fired in zip(keys, flags):
            q = sweep.occupancy[key]
            prob *= q if fired else 1.0 - q
        fired_keys = {key for key, fired in zip(keys, flags) if fired}
        values = []
        for k in range(sweep.n):
            covering = {(bit, w) for bit, w in sweep.windows_at[k].items()} & fired_keys
            values.append(sweep.note_value(k, covering))
        prefix = [0]
        for v in values:
            prefix.append(prefix[-1] + v)
        runs.append((prob, [prefix[c] for c in boundaries], prefix[-1]))
    return runs


@pytest.mark.parametrize("leader_index", [0, 1])
def test_the_pace_is_the_brute_force(cards, cfg, leader_index):
    chart = _chart()
    members, leader, best = _row(cards, cfg, chart, leader_index)
    law = run_score_distribution(members, leader, chart, cfg, best.member_order, best.deck_power, pace=True)
    pace = law["pace"]
    sweep = _Sweep(members, leader, chart, cfg, best.member_order, best.deck_power, 400_000)
    boundaries = [cut["notes"] for cut in pace["cuts"]]
    # windows to enumerate, and some open across a cue (the case the join is for)
    assert len(sweep.first) <= 16
    assert any(sweep.first[key] < c <= sweep.last[key] for key in sweep.first for c in boundaries)
    runs = _brute_force(sweep, boundaries)
    pmf = law["pmf"]
    nb = len(pmf["mass"])

    def bar_of(score: int) -> int:  # -1 = below the first bar
        if score < pmf["start"]:
            return -1
        return min(nb - 1, (score - pmf["start"]) // pmf["width"])

    mass = [0.0] * nb
    by_cut = [[0.0] * len(boundaries) for _ in range(nb)]
    seen = [[[] for _ in boundaries] for _ in range(nb)]
    for prob, prefix, total in runs:
        b = bar_of(total)
        if b < 0:
            continue
        mass[b] += prob
        for j, s in enumerate(prefix):
            by_cut[b][j] += prob * s
            if prob > 0.0:
                seen[b][j].append(s)
    assert pace["check"]["mass"] < 1e-12
    for b in range(nb):
        assert pace["bars"]["mass"][b][-1] == pytest.approx(mass[b], abs=1e-12)
        if mass[b] < 1e-12:
            continue
        for j in range(len(boundaries)):
            assert pace["bars"]["mean"][b][j] == pytest.approx(by_cut[b][j] / mass[b], rel=1e-9), (b, j)
            # the band lies among the scores these runs have there, and the score by a
            # later cut is never lower
            low, high = pace["bars"]["p10"][b][j], pace["bars"]["p90"][b][j]
            assert min(seen[b][j]) - 1e-6 <= low <= high <= max(seen[b][j]) + 1e-6
            if j:
                assert pace["bars"]["mean"][b][j] >= pace["bars"]["mean"][b][j - 1]
    # the average run by each cut
    for j, cut in enumerate(pace["cuts"]):
        assert cut["mean"] == pytest.approx(sum(p * pre[j] for p, pre, _f in runs), rel=1e-12)


def _mean_by_note(sweep: _Sweep) -> list[float]:
    """Each note's mean score, from its own covering windows (independent rolls)."""
    out = []
    for k in range(sweep.n):
        keys = list(sweep.windows_at[k].items())
        mean = 0.0
        for flags in itertools.product((False, True), repeat=len(keys)):
            prob = 1.0
            for key, fired in zip(keys, flags):
                q = sweep.occupancy[key]
                prob *= q if fired else 1.0 - q
            mean += prob * sweep.note_value(k, {key for key, fired in zip(keys, flags) if fired})
        out.append(mean)
    return out


def test_the_bars_add_up_to_the_mean_score_by_each_cut(cards, cfg):
    """`SUM_I P(I) E[S_j | I] = E[S_j]` on the full fixture chart (too many rolls to
    enumerate): the right side note by note, from each note's own windows. `E[S_j]` is
    the Expected score of the notes before the cut but for the rounding (the scorer rounds
    each note's expectation up, the law each note's score), under one point per note."""
    chart = _chart(120.0, (10.0, 30.0, 50.0, 70.0, 90.0))
    members, leader, best = _row(cards, cfg, chart)
    law = run_score_distribution(members, leader, chart, cfg, best.member_order, best.deck_power, pace=True)
    pace = law["pace"]
    sweep = _Sweep(members, leader, chart, cfg, best.member_order, best.deck_power, 400_000)
    by_note = _mean_by_note(sweep)
    assert pace["check"]["mass"] < 1e-9
    for j, cut in enumerate(pace["cuts"]):
        weighted = pace["below"]["mass"][j] * (pace["below"]["mean"][j] or 0.0) + sum(
            m[j] * mu[j] for m, mu in zip(pace["bars"]["mass"], pace["bars"]["mean"]) if mu[j] is not None
        )
        exact = math.fsum(by_note[: cut["notes"]])
        assert weighted == pytest.approx(exact, rel=1e-9)
        assert cut["mean"] == pytest.approx(exact, rel=1e-9)
        assert abs(exact - cut["expected"]) <= cut["notes"]
    assert pace["cuts"][-1]["expected"] == law["expected_total"] == pace["expected_total"]


def test_the_last_cut_is_the_bar_itself(cards, cfg):
    """At the song's end the score so far IS the final score: each bar's mean is the mean of
    the law inside the bar."""
    chart = _chart(120.0, (10.0, 30.0, 50.0, 70.0, 90.0))
    members, leader, best = _row(cards, cfg, chart)
    law = run_score_distribution(
        members, leader, chart, cfg, best.member_order, best.deck_power, pace=True, keep_law=True
    )
    pmf, pace = law["pmf"], law["pace"]
    nb = len(pmf["mass"])
    for b in range(nb):
        a, z = pmf["start"] + b * pmf["width"], pmf["start"] + (b + 1) * pmf["width"]
        inside = [(v, p) for v, p in law["law"] if a <= v < z or (b == nb - 1 and v >= a)]
        mass = sum(p for _v, p in inside)
        if mass < 1e-12:
            continue
        assert pace["bars"]["mean"][b][-1] == pytest.approx(sum(v * p for v, p in inside) / mass, rel=1e-12)


def test_the_pace_does_not_change_the_distribution(cards, cfg):
    chart = _chart(120.0, (10.0, 30.0, 50.0, 70.0, 90.0))
    members, leader, best = _row(cards, cfg, chart)
    plain = run_score_distribution(members, leader, chart, cfg, best.member_order, best.deck_power)
    paced = run_score_distribution(members, leader, chart, cfg, best.member_order, best.deck_power, pace=True)
    assert "pace" not in plain
    paced.pop("pace")
    assert paced == plain
    # the cuts are the five Special cues and the end; a note on a cue is the cue's
    sweep = _Sweep(members, leader, chart, cfg, best.member_order, best.deck_power, 400_000)
    again = run_score_distribution(members, leader, chart, cfg, best.member_order, best.deck_power, pace=True)
    cuts = again["pace"]["cuts"]
    assert [c["label"] for c in cuts] == [
        "Special 1",
        "Special 2",
        "Special 3",
        "Special 4",
        "Special 5",
        "End",
    ]
    assert [c["notes"] for c in cuts] == [19, 59, 99, 139, 179, sweep.n]
