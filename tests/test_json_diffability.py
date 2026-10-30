"""`--json`'s diffability contract. Every run over identical inputs must
serialize byte-identically, every key must sort at every nesting level, every
float must encode round-trip-exact, and the lower-bound claim must ride on the
payload's data, not on a rendering flourish — a machine consumer diffing two
runs must never mistake a lower bound for a settled score.

The payload is a flat envelope: six sorted top-level keys (`schema_version`,
`config`, `completeness`, `pool`, `ordering`, `teams`), floats as native JSON
numbers rather than `repr()` strings, because CPython's `json` encoder already
formats a float with `float.__repr__` — verified below over an adversarial
set, so a future CPython encoder change trips this test rather than silently
drifting float precision across a diff.
"""

from __future__ import annotations

import dataclasses
import json
import math

from hbuilder.domain.loader import load_cards, load_owned
from hbuilder.domain.schema import SCHEMA_VERSION
from hbuilder.domain.scoring import ScoreBreakdown, SkillContribution, score_with_breakdown
from hbuilder.domain.scoring_config import load_scoring_config
from hbuilder.output import json_output
from hbuilder.output.table import exact_row_count
from hbuilder.solver.exact import RankedTeam, solve_exact

from conftest import FIXTURE_CARDS_PATH, FIXTURE_OWNED_PATH, make_card, make_scoring_config, make_skill, make_team

# An adversarial float set (0.1, 1/3, 1e308, 5e-324, -0.0, 2.675, and a
# realistic score magnitude) — named for the defect it guards: if this ever
# fails, native-number encoding no longer holds and floats must move to a
# string form.
_ADVERSARIAL_FLOATS: tuple[float, ...] = (
    0.1,
    1 / 3,
    1e308,
    5e-324,
    -0.0,
    8786.6,
    2**53 + 1.0,
    0.30000000000000004,
    2.675,
    12_345.6,  # a realistic score magnitude
)

_EMPTY_SELECTORS: dict = {
    "require": [],
    "leader": None,
    "exclude": [],
    "include_unowned": False,
}


def _shipped_payload_inputs():
    """The shipped 6-card fixture, re-scored with breakdowns — every
    candidate team is tainted because `mio_01`'s `ACTIVE.magnitude` is
    unknown."""
    cards = load_cards(FIXTURE_CARDS_PATH)
    owned = load_owned(FIXTURE_OWNED_PATH, cards)
    cfg = load_scoring_config()
    ranked = solve_exact(owned, cfg, top_n=6)
    breakdowns = tuple(score_with_breakdown(candidate.team, cfg) for candidate in ranked)
    return ranked, breakdowns, cfg, owned


def _ranked(score: float, leader_id: str, member_ids: list[str]) -> RankedTeam:
    """A bare `RankedTeam` for synthetic payload tests, mirroring
    `test_lower_bound_rendering.py`'s own local helper — `team` is never
    read by anything under test here."""
    return RankedTeam(score=score, leader_id=leader_id, member_ids=tuple(member_ids), team=None)


def _breakdown(
    total_score: float,
    *,
    incomplete_skills: tuple[str, ...] = (),
    contributions: tuple[SkillContribution, ...] = (),
) -> ScoreBreakdown:
    return ScoreBreakdown(
        total_score=total_score,
        base_stat_sum=total_score,
        effective_stat_sum=total_score,
        combined_stat_rate=0.0,
        combined_score_rate=0.0,
        combined_support_rate=0.0,
        board_score_rate=0.0,
        team_rate_up=0.0,
        contributions=contributions,
        incomplete_skills=incomplete_skills,
        stacking_model_name="additive",
        activation_rate_model_name="pp",
        active_stacking_model_name="max",
        run_state=make_scoring_config().run_state,
    )


def _contribution(
    card_id: str = "c1",
    slot_kind: str = "ACTIVE",
    *,
    gate_fired: bool | None = None,
    gate_dimension: str | None = None,
    gate_value: str | None = None,
    gate_threshold: int | None = None,
    gate_actual_count: int | None = None,
    override_fired: bool | None = None,
    rider_family: str | None = None,
    rider_effect_type: str | None = None,
    rider_magnitude: float | None = None,
) -> SkillContribution:
    return SkillContribution(
        card_id=card_id,
        slot_kind=slot_kind,
        source="normal",
        family="score_support",
        effect_type="score",
        state="applied",
        pool="score",
        rate=0.1,
        weight=1.0,
        weighted_rate=0.1,
        missing_field=None,
        gate_dimension=gate_dimension,
        gate_value=gate_value,
        gate_threshold=gate_threshold,
        gate_actual_count=gate_actual_count,
        gate_fired=gate_fired,
        override_fired=override_fired,
        rider_family=rider_family,
        rider_effect_type=rider_effect_type,
        rider_magnitude=rider_magnitude,
    )


def _assert_keys_sorted_recursively(text: str) -> None:
    """Walk every JSON object in the parsed text and assert its own key
    order, as it appeared in the serialized string, is ascending — at
    every nesting depth, including inside `teams[]` and `contributions[]`
    entries. `object_pairs_hook` sees each object's pairs in on-the-wire
    order, before Python's dict construction could reorder anything."""

    def hook(pairs: list[tuple[str, object]]) -> dict:
        keys = [key for key, _ in pairs]
        assert keys == sorted(keys), f"keys not sorted at some nesting level: {keys}"
        return dict(pairs)

    json.loads(text, object_pairs_hook=hook)


def test_byte_identical_rerun_over_identical_inputs():
    """Two payload builds from identical inputs serialize to
    identical strings."""
    ranked, breakdowns, cfg, owned = _shipped_payload_inputs()
    first = json_output.dumps(
        json_output.build_payload(ranked, breakdowns, cfg, owned, _EMPTY_SELECTORS)
    )
    second = json_output.dumps(
        json_output.build_payload(ranked, breakdowns, cfg, owned, _EMPTY_SELECTORS)
    )
    assert first == second


def test_sorted_keys_at_every_nesting_level_including_teams_and_contributions():
    """Sorted keys everywhere, verified on the real shipped fixture
    with `--explain`-shaped `contributions` attached to one team, so the
    deepest nesting level (`teams[].contributions[]`) is exercised too."""
    ranked, breakdowns, cfg, owned = _shipped_payload_inputs()
    payload = json_output.build_payload(
        ranked, breakdowns, cfg, owned, _EMPTY_SELECTORS, explain_rank=1
    )
    text = json_output.dumps(payload)
    assert '"contributions"' in text  # sanity: the deep nesting is present
    _assert_keys_sorted_recursively(text)


def test_contribution_object_key_set_includes_target_kind_and_target_card_ids():
    """`SkillContribution`'s own field list,
    `_build_contribution_object`'s explicit key dict, and this pinned key
    set are three separate enumerations of the same fields -- pinning the
    JSON side here closes the drift path a fourth field added to only one
    of the three would otherwise leave open."""
    ranked, breakdowns, cfg, owned = _shipped_payload_inputs()
    payload = json_output.build_payload(
        ranked, breakdowns, cfg, owned, _EMPTY_SELECTORS, explain_rank=1
    )
    contributions = payload["teams"][0]["contributions"]
    assert contributions
    assert set(contributions[0].keys()) == {
        "card_id",
        "slot_kind",
        "source",
        "family",
        "effect_type",
        "state",
        "pool",
        "rate",
        "weight",
        "weighted_rate",
        "missing_field",
        "gate_dimension",
        "gate_value",
        "gate_threshold",
        "gate_actual_count",
        "gate_fired",
        "target_kind",
        "target_card_ids",
        "override_fired",
        "rider_family",
        "rider_effect_type",
        "rider_magnitude",
    }


def test_targeted_stat_up_contribution_carries_target_kind_and_target_card_ids_in_json():
    """A resolved targeted stat_up contribution's JSON object
    carries the same `target_kind`/`target_card_ids` values `--explain`
    renders, byte-for-byte -- `--json --explain <rank>` is not a second,
    independently-derived attribution surface."""
    ranked, breakdowns, cfg, owned = _shipped_payload_inputs()
    payload = json_output.build_payload(
        ranked, breakdowns, cfg, owned, _EMPTY_SELECTORS, explain_rank=1
    )
    contributions = payload["teams"][0]["contributions"]
    stat_up_targeted = [
        c
        for c in contributions
        if c["family"] == "stat_up" and c["target_kind"] is not None
    ]
    assert stat_up_targeted, "expected at least one resolved targeted stat_up contribution"
    for contribution in stat_up_targeted:
        assert isinstance(contribution["target_card_ids"], list)
        assert contribution["target_card_ids"] == sorted(contribution["target_card_ids"])


def test_override_fired_appears_in_json_contribution_object_for_all_three_states():
    """`override_fired` round-trips
    through the JSON payload exactly as `SkillContribution` carries it,
    for each of the three states -- fired, unfired, and no override at
    all."""
    ranked_team = _ranked(100.0, "leader_x", ["m1", "m2", "m3", "m4", "m5"])
    fired = _contribution("c1", override_fired=True)
    unfired = _contribution("c2", override_fired=False)
    no_override = _contribution("c3", override_fired=None)
    breakdown = _breakdown(100.0, contributions=(fired, unfired, no_override))

    team_object = json_output.build_team_object(
        1, ranked_team, breakdown, with_contributions=True
    )

    assert [c["override_fired"] for c in team_object["contributions"]] == [
        True,
        False,
        None,
    ]


def test_rider_fields_round_trip_through_the_json_contribution_object():
    """`rider_family`/`rider_effect_type`/
    `rider_magnitude` round-trip through the JSON payload exactly as
    `SkillContribution` carries them, for both the carries-a-rider and
    carries-none cases."""
    ranked_team = _ranked(100.0, "leader_x", ["m1", "m2", "m3", "m4", "m5"])
    with_rider = _contribution(
        "c1",
        rider_family="activation_rate_up",
        rider_effect_type="activation_rate",
        rider_magnitude=55.0,
    )
    without_rider = _contribution("c2")
    breakdown = _breakdown(100.0, contributions=(with_rider, without_rider))

    team_object = json_output.build_team_object(
        1, ranked_team, breakdown, with_contributions=True
    )

    first, second = team_object["contributions"]
    assert first["rider_family"] == "activation_rate_up"
    assert first["rider_effect_type"] == "activation_rate"
    assert first["rider_magnitude"] == 55.0
    assert second["rider_family"] is None
    assert second["rider_effect_type"] is None
    assert second["rider_magnitude"] is None


def test_top_level_key_set_matches_the_flat_envelope_checkpoint_decision():
    """Exactly these six top-level keys —
    pinned here so a future edit that adds/removes/renames one trips a
    named test rather than silently reshaping the contract."""
    ranked, breakdowns, cfg, owned = _shipped_payload_inputs()
    payload = json_output.build_payload(ranked, breakdowns, cfg, owned, _EMPTY_SELECTORS)
    round_tripped = json.loads(json_output.dumps(payload))
    assert set(round_tripped.keys()) == {
        "schema_version",
        "config",
        "completeness",
        "pool",
        "ordering",
        "teams",
    }


def test_native_float_encoding_is_repr_exact_guard_against_encoder_drift():
    """The round-trip-exact requirement, pinned over an adversarial float
    set. A failure here means CPython's `json` encoder no longer formats
    floats via `float.__repr__`, and the native-number encoding must be
    revisited."""
    for value in _ADVERSARIAL_FLOATS:
        encoded = json.dumps(value)
        assert encoded == repr(value), (
            f"json.dumps({value!r}) == {encoded!r} but repr({value!r}) == "
            f"{repr(value)!r} — D-15's native-float-is-repr-exact "
            f"assumption no longer holds; floats must move to the "
            f"string-floats form"
        )
        assert json.loads(encoded) == value, (
            f"round-trip failed for {value!r}: json.loads(json.dumps(v)) != v"
        )


def test_non_finite_float_is_rejected_with_the_payload_path_named():
    """A `nan` score raises `ValueError` naming the offending
    payload path, rather than serializing to invalid JSON."""
    ranked = [_ranked(100.0, "leader_x", ["m1", "m2", "m3", "m4", "m5"])]
    breakdowns = [_breakdown(float("nan"))]
    try:
        json_output.build_team_object(1, ranked[0], breakdowns[0], with_contributions=False)
        raise AssertionError("expected ValueError for a non-finite score")
    except ValueError as exc:
        assert "teams[1].score" in str(exc)


def test_config_provenance_marks_exactly_the_file_supplied_key(tmp_path):
    """A temporary `config.json` supplying exactly one top-level key
    (`special`) produces a payload whose `config.provenance` marks that
    key `"file"` and every other top-level key `"default"`."""
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"special": {"expected_fires_per_run": 2.0}}), encoding="utf-8"
    )
    cfg = load_scoring_config(config_path)

    ranked = [_ranked(100.0, "leader_x", ["m1", "m2", "m3", "m4", "m5"])]
    breakdowns = [_breakdown(100.0)]
    payload = json_output.build_payload(ranked, breakdowns, cfg, (), _EMPTY_SELECTORS)

    provenance = payload["config"]["provenance"]
    assert provenance["special"] == "file"
    for key, origin in provenance.items():
        if key != "special":
            assert origin == "default", f"{key} should be default, got {origin!r}"


def test_both_schema_versions_are_stamped_into_the_payload():
    """Both `OUTPUT_SCHEMA_VERSION` (this contract's own
    version) and `cards.json`'s `SCHEMA_VERSION` appear in the payload,
    so a consumer can trace a stored result to both contracts that
    produced it."""
    ranked = [_ranked(100.0, "leader_x", ["m1", "m2", "m3", "m4", "m5"])]
    breakdowns = [_breakdown(100.0)]
    payload = json_output.build_payload(ranked, breakdowns, make_scoring_config(), (), _EMPTY_SELECTORS)

    assert payload["schema_version"] == json_output.OUTPUT_SCHEMA_VERSION
    assert payload["completeness"]["cards_schema_version"] == SCHEMA_VERSION


def test_lower_bound_survives_for_the_shipped_tainted_collection():
    """Every team object built from the shipped fixture
    (every candidate tainted by `mio_01`'s unknown `ACTIVE.magnitude`)
    carries `is_lower_bound: true` and a non-empty `incomplete_fields`
    naming `mio_01`'s missing field."""
    ranked, breakdowns, cfg, owned = _shipped_payload_inputs()
    payload = json_output.build_payload(ranked, breakdowns, cfg, owned, _EMPTY_SELECTORS)

    assert payload["teams"], "expected at least one ranked team"
    for team in payload["teams"]:
        assert team["is_lower_bound"] is True
        assert any(
            field.startswith("mio_01:") for field in team["incomplete_fields"]
        ), team["incomplete_fields"]


def test_lower_bound_absent_for_a_fully_populated_synthetic_pool():
    """A fully populated synthetic pool's team objects
    carry `is_lower_bound: false` and empty `incomplete_fields` — the
    lower-bound claim is never asserted when nothing is actually
    missing."""
    ranked = [
        _ranked(300.0, "leader_a", ["m1", "m2", "m3", "m4", "m5"]),
        _ranked(200.0, "leader_b", ["m1", "m2", "m3", "m4", "m6"]),
    ]
    breakdowns = [_breakdown(300.0), _breakdown(200.0)]
    payload = json_output.build_payload(
        ranked, breakdowns, make_scoring_config(), (), _EMPTY_SELECTORS
    )

    for team in payload["teams"]:
        assert team["is_lower_bound"] is False
        assert team["incomplete_fields"] == []


def test_ordering_boundary_agrees_with_table_exact_row_count():
    """`ordering.exact_through_rank` is sourced from the same
    `table.exact_row_count` the text footer uses, so the two can never
    disagree — checked for both a partially tainted and a fully
    populated breakdown list."""
    tainted_breakdowns = [
        _breakdown(600.0),
        _breakdown(500.0),
        _breakdown(400.0),
        _breakdown(300.0, incomplete_skills=("card_d:skills.ACTIVE.magnitude",)),
        _breakdown(200.0, incomplete_skills=("card_e:skills.ACTIVE.magnitude",)),
    ]
    tainted_ranked = [
        _ranked(600.0, "a", ["b", "c", "d", "e", "f"]),
        _ranked(500.0, "g", ["h", "i", "j", "k", "l"]),
        _ranked(400.0, "m", ["n", "o", "p", "q", "r"]),
        _ranked(300.0, "s", ["t", "u", "v", "w", "x"]),
        _ranked(200.0, "y", ["z", "aa", "bb", "cc", "dd"]),
    ]
    payload = json_output.build_payload(
        tainted_ranked, tainted_breakdowns, make_scoring_config(), (), _EMPTY_SELECTORS
    )
    assert payload["ordering"]["exact_through_rank"] == exact_row_count(tainted_breakdowns) == 3

    full_breakdowns = [_breakdown(300.0), _breakdown(200.0)]
    full_ranked = [
        _ranked(300.0, "leader_a", ["m1", "m2", "m3", "m4", "m5"]),
        _ranked(200.0, "leader_b", ["m1", "m2", "m3", "m4", "m6"]),
    ]
    full_payload = json_output.build_payload(
        full_ranked, full_breakdowns, make_scoring_config(), (), _EMPTY_SELECTORS
    )
    assert (
        full_payload["ordering"]["exact_through_rank"]
        == exact_row_count(full_breakdowns)
        == len(full_breakdowns)
    )


def test_explain_rank_touches_only_the_named_rank_orthogonality():
    """`build_payload(..., explain_rank=2)` produces a payload
    where exactly the rank-2 team carries `contributions`, and every
    other team object — and every other section — is byte-identical to
    the payload built with `explain_rank=None`. Depth (`--explain`) does
    not constrain format (`--json`)."""
    ranked = [
        _ranked(300.0, "leader_a", ["m1", "m2", "m3", "m4", "m5"]),
        _ranked(250.0, "leader_b", ["m1", "m2", "m3", "m4", "m6"]),
        _ranked(200.0, "leader_c", ["m1", "m2", "m3", "m4", "m7"]),
    ]
    contribution = _contribution(gate_fired=True, gate_dimension="type", gate_value="Cute", gate_threshold=2, gate_actual_count=2)
    breakdowns = [
        _breakdown(300.0, contributions=(contribution,)),
        _breakdown(250.0, contributions=(contribution,)),
        _breakdown(200.0, contributions=(contribution,)),
    ]

    cfg = make_scoring_config()
    without_explain = json_output.build_payload(ranked, breakdowns, cfg, (), _EMPTY_SELECTORS)
    with_explain = json_output.build_payload(
        ranked, breakdowns, cfg, (), _EMPTY_SELECTORS, explain_rank=2
    )

    assert "contributions" not in without_explain["teams"][0]
    assert "contributions" not in without_explain["teams"][1]
    assert "contributions" not in without_explain["teams"][2]

    assert "contributions" not in with_explain["teams"][0]
    assert "contributions" in with_explain["teams"][1]
    assert "contributions" not in with_explain["teams"][2]

    # Every other section, and every other team object, is unaffected.
    assert with_explain["config"] == without_explain["config"]
    assert with_explain["completeness"] == without_explain["completeness"]
    assert with_explain["pool"] == without_explain["pool"]
    assert with_explain["ordering"] == without_explain["ordering"]
    assert with_explain["teams"][0] == without_explain["teams"][0]
    assert with_explain["teams"][2] == without_explain["teams"][2]
    rank2_without_contributions = {
        key: value for key, value in with_explain["teams"][1].items() if key != "contributions"
    }
    assert rank2_without_contributions == without_explain["teams"][1]


def test_diffability_in_practice_changing_one_stat_touches_only_score_lines():
    """A unified line diff of two serialized payloads, differing only by
    one card's stat, touches only the lines that genuinely changed —
    the `score` line of the affected team's entry — never a key
    reordering artifact."""
    skill = make_skill("PASSIVE", magnitude=5.0)
    base_cards = [make_card(f"card_{i}", passive=skill) for i in range(6)]
    team_1 = make_team(base_cards)

    changed_cards = list(base_cards)
    # Index 1, not 0: make_team puts cards[0] in the OUTFIT slot, which
    # contributes no stats at all, so changing its
    # performance could never move the score and this test would assert
    # nothing.
    changed_cards[1] = dataclasses.replace(
        changed_cards[1], stats=dataclasses.replace(changed_cards[1].stats, performance=999)
    )
    team_2 = make_team(changed_cards)

    cfg = make_scoring_config()
    breakdown_1 = score_with_breakdown(team_1, cfg)
    breakdown_2 = score_with_breakdown(team_2, cfg)
    assert breakdown_1.total_score != breakdown_2.total_score, "the stat change must move the score"

    ranked = [_ranked(breakdown_1.total_score, base_cards[0].id, [c.id for c in base_cards[1:]])]
    ranked_2 = [_ranked(breakdown_2.total_score, base_cards[0].id, [c.id for c in base_cards[1:]])]

    payload_1 = json_output.dumps(
        json_output.build_payload(ranked, [breakdown_1], cfg, tuple(base_cards), _EMPTY_SELECTORS)
    )
    payload_2 = json_output.dumps(
        json_output.build_payload(ranked_2, [breakdown_2], cfg, tuple(changed_cards), _EMPTY_SELECTORS)
    )

    lines_1 = payload_1.splitlines()
    lines_2 = payload_2.splitlines()
    assert len(lines_1) == len(lines_2), "structure must be identical; only values may change"

    changed_lines = [
        (before, after)
        for before, after in zip(lines_1, lines_2)
        if before != after
    ]
    assert changed_lines, "expected at least one changed line"
    for before, after in changed_lines:
        assert '"score":' in before and '"score":' in after, (
            f"unexpected non-score line changed: {before!r} -> {after!r}"
        )
