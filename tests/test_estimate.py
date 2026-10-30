"""The constrained-pool arrangement projection and the throughput-file
provenance contract.

Pins the arrangement formula (including the 54-card unconstrained and
`--require nene_01` figures — a typical real run) and
`load_throughput`/`project_duration`'s staleness-warning behavior.
"""

from __future__ import annotations

import json
import math

import pytest

from hbuilder.domain.schema import SCHEMA_VERSION
from hbuilder.solver.estimate import (
    CONFIRM_THRESHOLD_SECONDS,
    THROUGHPUT_REQUIRED_KEYS,
    Throughput,
    ThroughputSchemaError,
    config_hash,
    derive_threshold_pool_size,
    load_throughput,
    project_arrangements,
    project_duration,
)


def test_unconstrained_54_card_pool_is_member_sets_times_leader_choices():
    """5 members chosen from the pool, times a leader
    chosen from the WHOLE pool -- the outfit slot sits outside the five and may
    reuse one of them, so the factor is N, not 6."""
    assert project_arrangements(54, 0, False) == (
        math.comb(54, 5),
        math.comb(54, 5) * 54,
    )
    assert project_arrangements(54, 0, False) == (3_162_510, 170_775_540)


def test_require_one_card_does_not_shrink_the_set_count_at_all():
    """A formula that counted only the sets where the lock is a MEMBER would
    give a 10.8x reduction, and is wrong.

    A lock is satisfied from either role — `--require x` is met by x being the
    leader just as well as by x being one of the five — so the sets that omit x
    are still candidates, with x forced into the outfit slot. Their counts add
    back to exactly the unconstrained figure:

        C(53,4) + C(53,5) = C(54,5)

    `--require` therefore buys nothing on the SET axis and 9.1x (not 10.8x) on
    the arrangement axis, and since the per-set prefix is untouched the real
    wall-clock saving is smaller than either number suggests."""
    unconstrained = project_arrangements(54, 0, False)
    constrained = project_arrangements(54, 1, False)

    assert constrained[0] == math.comb(54, 5) == unconstrained[0]
    assert constrained[1] == math.comb(53, 4) * 54 + math.comb(53, 5)
    assert constrained == (3_162_510, 18_682_235)
    assert unconstrained[1] / constrained[1] == pytest.approx(9.141, abs=0.001)


def test_leader_fixed_keeps_one_arrangement_per_set():
    """Fixing the leader collapses the leader axis entirely — one arrangement
    per set — and also collapses which lock may be deferred to the outfit slot
    down to the pinned card, so the set count shrinks too."""
    unfixed = project_arrangements(54, 1, False)
    fixed = project_arrangements(54, 1, True)

    assert fixed[1] == fixed[0], "with the leader fixed, one arrangement per set"
    assert fixed == (math.comb(53, 4) + math.comb(53, 5), math.comb(54, 5))
    assert fixed[0] == unfixed[0], "one lock, so there was only one deferral anyway"


def test_pool_of_6_with_no_locks_gives_six_sets_and_thirty_six_arrangements():
    assert project_arrangements(6, 0, False) == (math.comb(6, 5), 36)


def test_pool_of_5_is_exactly_enough_to_field_a_team():
    """Five cards now SOLVE rather than exiting insufficient: they are the five
    members, and the leader is drawn from that same pool (a leader may also be
    a member)."""
    assert project_arrangements(5, 0, False) == (1, 5)


def test_pool_of_4_is_below_the_member_count_and_gives_zero():
    assert project_arrangements(4, 0, False) == (0, 0)


def test_locked_count_of_5_fills_the_member_set_exactly():
    """Five locks fill the member set (one set, N leader choices), and each of
    the five may instead be deferred to the outfit slot, leaving four locked
    members plus one free — C(1,1) = 1 set each, one leader each."""
    assert project_arrangements(6, 5, False) == (6, 11)
    assert project_arrangements(6, 5, True) == (2, 2)


def test_six_locks_are_five_members_plus_the_outfit_slot_not_a_degenerate_zero():
    """Six locks are not "more locks than member slots" (which would give
    (0, 0)): the sixth lock has a slot of its own. Six locks are exactly
    satisfiable: five members and the outfit."""
    assert project_arrangements(54, 6, False) == (6, 6)
    assert project_arrangements(54, 6, True) == (1, 1)


def test_seven_locks_cannot_be_satisfied_by_five_members_and_one_outfit():
    assert project_arrangements(54, 7, False) == (0, 0)
    assert project_arrangements(54, 7, True) == (0, 0)


def test_the_projection_returns_two_plain_ints_and_smuggles_nothing():
    """`project_duration` needs both terms. The two-rate model returns the set
    count as the tuple's first element, and `cli.py` passes it as the
    `set_count=` keyword; the arrangement count must be a plain `int`, not an
    `int` subclass smuggling the set count as an attribute.

    Pinned as `type(...) is int`, not `isinstance`, because such a subclass IS
    an `int` and every `isinstance` check would pass on it. Expected values:
    (3,162,510 sets, 170,775,540 arrangements)."""
    set_count, arrangement_count = project_arrangements(54, 0, False)

    assert (set_count, arrangement_count) == (3_162_510, 170_775_540)
    assert type(set_count) is int
    assert type(arrangement_count) is int
    assert not hasattr(arrangement_count, "set_count"), (
        "the arrangement count is carrying a second value again — the shim is "
        "back, and with it a per-set term that defaults to zero"
    )
    # Still exactly the int every consumer already renders and serialises.
    assert json.loads(json.dumps({"n": arrangement_count})) == {"n": 170_775_540}
    assert f"{arrangement_count:,}" == "170,775,540"


def test_absent_throughput_file_yields_none_and_raises_nothing(tmp_path):
    missing_path = tmp_path / "does_not_exist" / "throughput.json"

    result = load_throughput(missing_path)

    assert result is None


def test_throughput_file_missing_a_required_key_raises_naming_it(tmp_path):
    path = tmp_path / "throughput.json"
    incomplete = {key: "x" for key in THROUGHPUT_REQUIRED_KEYS if key != "measured_at"}
    path.write_text(json.dumps(incomplete), encoding="utf-8")

    with pytest.raises(ThroughputSchemaError, match="measured_at"):
        load_throughput(path)


def test_project_duration_with_no_throughput_returns_none_and_names_the_benchmark_script():
    seconds, warning = project_duration(1000, None, "abc123", set_count=100)

    assert seconds is None
    assert warning is not None
    assert "bench_solver.py" in warning


def _throughput(config_hash_value: str = "abc123", **overrides) -> Throughput:
    fields = {
        "seconds_per_eval": 0.001,
        "seconds_per_set": 0.0,
        "config_hash": config_hash_value,
        "schema_version": SCHEMA_VERSION,
        "measured_at": "2026-01-01T00:00:00Z",
    }
    fields.update(overrides)
    return Throughput(**fields)


def test_project_duration_with_matching_hash_has_no_warning():
    seconds, warning = project_duration(1000, _throughput(), "abc123", set_count=200)

    # `_throughput()`'s seconds_per_set is 0.0, so 200 sets add nothing and
    # this is the same 1.0 s a one-rate model would return.
    assert seconds == pytest.approx(1.0)
    assert warning is None


def test_project_duration_with_stale_hash_still_returns_seconds_but_warns_naming_both_hashes():
    seconds, warning = project_duration(
        1000, _throughput("old_hash"), "new_hash", set_count=200
    )

    assert seconds == pytest.approx(1.0)
    assert warning is not None
    assert "old_hash" in warning
    assert "new_hash" in warning


def test_the_per_set_prefix_is_priced_not_only_the_arrangements():
    """Why the cost model has a per-set term: a `--leader` run has ONE
    arrangement per member set, so `score_card_set`'s set-invariant prefix is
    the entire cost and a per-arrangement rate describes almost none of it.
    The prefix costs about 4.3x one leader's marginal cost."""
    throughput = _throughput(seconds_per_eval=0.001, seconds_per_set=0.004)

    seconds, _ = project_duration(1000, throughput, "abc123", set_count=1000)

    assert seconds == pytest.approx(5.0)


def test_both_of_the_projections_terms_are_threaded_from_the_same_tuple():
    """`cli.py` passes BOTH values `project_arrangements` returns. The answer
    is 6 sets and 36 arrangements at a 6-card pool — this pins the
    behaviour, not the plumbing that carries it."""
    throughput = _throughput(seconds_per_eval=0.001, seconds_per_set=0.004)
    set_count, arrangement_count = project_arrangements(6, 0, False)

    seconds, _ = project_duration(
        arrangement_count, throughput, "abc123", set_count=set_count
    )

    assert (set_count, arrangement_count) == (6, 36)
    assert seconds == pytest.approx(0.060)
    assert seconds == pytest.approx(6 * 0.004 + 36 * 0.001)
    # The one-rate answer, which must NOT come back: pricing only the
    # arrangements is 0.036 s, a 1.67x under-count here and ~4.3x on a
    # leader-pinned run where sets == arrangements.
    assert seconds != pytest.approx(0.036)


def test_omitting_the_set_count_is_a_type_error_not_a_silent_lower_bound():
    """A default of zero would price `score_card_set`'s set-invariant prefix at
    nothing. On a `--leader` run there is one arrangement per set, so that zero
    IS the whole cost — enough to make `--estimate` print 0.04 s for a 178 s
    run. A caller that forgets must fail at the call."""
    throughput = _throughput(seconds_per_eval=0.001, seconds_per_set=0.004)

    with pytest.raises(TypeError, match="set_count"):
        project_duration(1000, throughput, "abc123")

    # And with it supplied, the per-set term is really priced: 1000 sets at
    # 0.004 s plus 1000 arrangements at 0.001 s is 5.0 s, not 1.0 s.
    seconds, _ = project_duration(1000, throughput, "abc123", set_count=1000)
    assert seconds == pytest.approx(5.0)
    assert seconds != pytest.approx(1.0)


def test_config_hash_is_stable_across_key_reordering():
    doc_a = {"b": 1, "a": {"y": 2, "x": 1}}
    doc_b = {"a": {"x": 1, "y": 2}, "b": 1}

    assert config_hash(doc_a) == config_hash(doc_b)


def test_config_hash_differs_when_a_probability_tier_value_changes():
    doc_a = {"probability_tiers": {"Low": 0.35, "Medium": 0.55, "High": 0.75}}
    doc_b = {"probability_tiers": {"Low": 0.36, "Medium": 0.55, "High": 0.75}}

    assert config_hash(doc_a) != config_hash(doc_b)


# ---------------------------------------------------------------------------
# `HEURISTIC_THRESHOLD_POOL_SIZE` is derived, not chosen
# ---------------------------------------------------------------------------


def _rates(seconds_per_set: float, seconds_per_eval: float) -> Throughput:
    return Throughput(
        seconds_per_eval=seconds_per_eval,
        seconds_per_set=seconds_per_set,
        config_hash="x",
        schema_version="2",
        measured_at="",
    )


def test_derive_threshold_pool_size_returns_the_first_pool_that_crosses():
    """The returned N must cross the threshold and N-1 must not — the property
    a hand-picked constant can satisfy loosely while silently sending pools the
    exact solver could still have finished to the beam."""
    throughput = _rates(119.70e-6, 24.10e-6)
    derived = derive_threshold_pool_size(throughput, 300.0)

    def projected(pool_size: int) -> float:
        sets, arrangements = project_arrangements(pool_size, 0, False)
        return (
            sets * throughput.seconds_per_set
            + arrangements * throughput.seconds_per_eval
        )

    assert projected(derived) > 300.0
    assert projected(derived - 1) <= 300.0


def test_derive_threshold_pool_size_moves_when_the_rates_do():
    """The whole point: it tracks the measurement. Scoring an order of
    magnitude dearer must walk the boundary DOWN, and cheaper must walk it up.
    A constant cannot do this."""
    baseline = derive_threshold_pool_size(_rates(119.70e-6, 24.10e-6), 300.0)
    dearer = derive_threshold_pool_size(_rates(1197.0e-6, 241.0e-6), 300.0)
    cheaper = derive_threshold_pool_size(_rates(11.97e-6, 2.41e-6), 300.0)

    assert dearer < baseline < cheaper


def test_derive_threshold_pool_size_raises_on_a_measurement_that_never_crosses():
    """A per-eval cost so small that no realistic pool crosses 300 s is a broken
    measurement, not a fast one — and returning some large N would quietly turn
    the fallback off."""
    with pytest.raises(ValueError, match="broken measurement"):
        derive_threshold_pool_size(_rates(0.0, 1e-30), 300.0)


def test_confirm_threshold_seconds_is_a_module_constant_not_read_from_config():
    # The threshold lives in code, never in config.json — this is a
    # smoke check that the constant exists and is a plain float, not a
    # config-provenance-tracked value.
    assert isinstance(CONFIRM_THRESHOLD_SECONDS, float)
    assert CONFIRM_THRESHOLD_SECONDS > 0
