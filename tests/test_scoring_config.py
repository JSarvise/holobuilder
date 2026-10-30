"""Coverage for the scoring config: `DEFAULT_CONFIG` fallback on an absent
file, per-top-level-key provenance on a partial file, and
`validate_scoring_config` rejecting every malformed shape by name — out-of
range tier probabilities, missing/non-numeric family weights, a negative
`expected_fires_per_run`, an unknown model name, a non-boolean or unmapped
`assumed_run_state` flag, and a reintroduced `reference_run_seconds` key.

Test names state the exact rejection or fallback each one pins, following
`tests/test_gates.py`'s convention. Config documents are built as dicts and
written to `tmp_path` as JSON, following the temp-file pattern
`tests/test_tracer_gates.py` already uses.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hbuilder.domain.schema import SchemaError
from hbuilder.domain.scoring_config import (
    DEFAULT_CONFIG,
    load_scoring_config,
    validate_scoring_config,
)
from hbuilder.domain.scoring_models import ACTIVATION_RATE_MODELS, STACKING_MODELS


def _write_config(tmp_path: Path, doc: dict) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


# --- Absent-file fallback and provenance -------------------------------------


def test_load_scoring_config_on_nonexistent_path_falls_back_to_default_without_raising(
    tmp_path,
):
    absent_path = tmp_path / "does_not_exist.json"
    cfg = load_scoring_config(absent_path)
    assert set(cfg.provenance.values()) == {"default"}


def test_load_scoring_config_on_repo_root_config_json_records_file_provenance_for_every_key():
    cfg = load_scoring_config()
    assert set(cfg.provenance.values()) == {"file"}


def test_config_file_supplying_only_probability_tiers_records_file_for_that_key_and_default_for_others(
    tmp_path,
):
    path = _write_config(tmp_path, {"probability_tiers": {"Low": 0.4, "Medium": 0.6, "High": 0.8}})
    cfg = load_scoring_config(path)
    assert cfg.provenance["probability_tiers"] == "file"
    other_keys = set(cfg.provenance) - {"probability_tiers"}
    assert all(cfg.provenance[key] == "default" for key in other_keys)


# --- probability_tiers rejections --------------------------------------------


def test_probability_tiers_missing_a_tier_name_is_rejected_naming_probability_tiers():
    errors = validate_scoring_config({"probability_tiers": {"Low": 0.35, "Medium": 0.55}})
    assert any(e.startswith("probability_tiers") and "High" in e for e in errors)


def test_probability_tier_value_below_zero_is_rejected_naming_probability_tiers_and_tier():
    errors = validate_scoring_config(
        {"probability_tiers": {"Low": -0.1, "Medium": 0.55, "High": 0.75}}
    )
    assert any(e.startswith("probability_tiers") and "Low" in e for e in errors)


def test_probability_tier_value_above_one_is_rejected_naming_probability_tiers_and_tier():
    errors = validate_scoring_config(
        {"probability_tiers": {"Low": 0.35, "Medium": 0.55, "High": 1.5}}
    )
    assert any(e.startswith("probability_tiers") and "High" in e for e in errors)


def test_misspelled_probability_tier_key_is_rejected_naming_the_unknown_key():
    errors = validate_scoring_config(
        {"probability_tiers": {"Loww": 0.35, "Medium": 0.55, "High": 0.75}}
    )
    assert any("Loww" in e for e in errors)
    assert any("'Low'" in e for e in errors)


# --- family_weights rejections ------------------------------------------------


def test_family_weights_missing_a_family_is_rejected_naming_family_weights_and_the_family():
    doc = {
        "family_weights": {
            "score_support": 1.0,
            "conditional_score_up": 1.0,
            "stat_up": 1.0,
            "activation_rate_up": 0.0,
            "life_recovery": 0.0,
            # judgment_enhancement deliberately omitted
        }
    }
    errors = validate_scoring_config(doc)
    assert any(
        e.startswith("family_weights") and "judgment_enhancement" in e for e in errors
    )


def test_non_numeric_family_weight_is_rejected_naming_family_weights_and_the_family():
    doc = {
        "family_weights": {
            "score_support": "a lot",
            "conditional_score_up": 1.0,
            "stat_up": 1.0,
            "activation_rate_up": 0.0,
            "life_recovery": 0.0,
            "judgment_enhancement": 0.0,
        }
    }
    errors = validate_scoring_config(doc)
    assert any(
        e.startswith("family_weights") and "score_support" in e for e in errors
    )


def test_nonzero_activation_rate_up_family_weight_is_rejected_as_an_inert_knob():
    weights = dict(DEFAULT_CONFIG["family_weights"])
    weights["activation_rate_up"] = 0.5
    errors = validate_scoring_config({"family_weights": weights})
    assert any(
        e.startswith("family_weights") and "activation_rate_up" in e for e in errors
    )


def test_activation_rate_up_family_weight_of_exactly_zero_is_accepted():
    weights = dict(DEFAULT_CONFIG["family_weights"])
    weights["activation_rate_up"] = 0.0
    errors = validate_scoring_config({"family_weights": weights})
    assert errors == []


def test_misspelled_family_weight_key_is_rejected_naming_the_unknown_key():
    weights = dict(DEFAULT_CONFIG["family_weights"])
    weights["score_suport"] = 1.0
    errors = validate_scoring_config({"family_weights": weights})
    assert any(
        e.startswith("family_weights") and "score_suport" in e for e in errors
    )


# --- special.expected_fires_per_run rejection ---------------------------------


def test_negative_expected_fires_per_run_is_rejected_naming_special_expected_fires_per_run():
    errors = validate_scoring_config({"special": {"expected_fires_per_run": -1.0}})
    assert any(e.startswith("special.expected_fires_per_run") for e in errors)


# --- model name rejections -----------------------------------------------------


def test_unknown_model_stacking_name_is_rejected_naming_model_stacking_and_accepted_names():
    errors = validate_scoring_config({"model": {"stacking": "geometric", "activation_rate": "pp"}})
    assert any(
        e.startswith("model.stacking") and "additive" in e and "multiplicative" in e
        for e in errors
    )


def test_unknown_model_activation_rate_name_is_rejected_naming_model_activation_rate_and_accepted_names():
    errors = validate_scoring_config(
        {"model": {"stacking": "additive", "activation_rate": "logarithmic"}}
    )
    assert any(
        e.startswith("model.activation_rate") and "pp" in e and "multiplicative" in e
        for e in errors
    )


# --- model presence rejections -------------------------------------------------


def test_model_object_missing_stacking_is_rejected_naming_model_stacking():
    errors = validate_scoring_config({"model": {"activation_rate": "pp"}})
    assert any(e.startswith("model.stacking") for e in errors)


def test_model_object_missing_activation_rate_is_rejected_naming_model_activation_rate():
    errors = validate_scoring_config({"model": {"stacking": "additive"}})
    assert any(e.startswith("model.activation_rate") for e in errors)


def test_empty_model_object_is_rejected_naming_both_required_keys():
    errors = validate_scoring_config({"model": {}})
    assert any(e.startswith("model.stacking") for e in errors)
    assert any(e.startswith("model.activation_rate") for e in errors)


# --- model.active_uptime (deck-screen vs in-run uptime reference) ---------------


def test_unknown_model_active_uptime_name_is_rejected_naming_it_and_accepted_names():
    errors = validate_scoring_config(
        {"model": {"stacking": "additive", "activation_rate": "pp", "active_uptime": "instant"}}
    )
    assert any(
        e.startswith("model.active_uptime") and "finite" in e and "steady" in e
        for e in errors
    )


def test_model_active_uptime_is_OPTIONAL_a_block_omitting_it_is_accepted_and_reads_finite():
    # Back-compat: the shipped config.json's "model" block predates this key, so
    # omitting it must NOT be a "missing required key" error, and must default to
    # the in-run "finite" reference.
    errors = validate_scoring_config({"model": {"stacking": "additive", "activation_rate": "pp"}})
    assert not any(e.startswith("model.active_uptime") for e in errors)


def test_model_active_uptime_steady_is_accepted_and_reaches_the_config(tmp_path: Path):
    path = tmp_path / "config.json"
    doc = {
        "model": {"stacking": "additive", "activation_rate": "multiplicative", "active_uptime": "steady"}
    }
    path.write_text(json.dumps(doc), encoding="utf-8")
    assert load_scoring_config(path).active_uptime_model == "steady"


def test_fallback_is_finite_but_shipped_config_ranks_by_the_deck_screen_panel():
    # The DEFAULT_CONFIG FALLBACK (used only when config.json is absent) stays the
    # in-run "finite" reference, so every test helper — which builds configs off the
    # fallback via make_scoring_config — keeps its in-run numbers untouched.
    assert DEFAULT_CONFIG["model"]["active_uptime"] == "finite"
    # The SHIPPED repo config.json ships "steady", so a real CLI run reports the
    # deck-screen panel Unit Score the player reads in-game — without it the Score
    # Bonus is understated by ~10% (test_score_bonus_end_to_end.py). The residual
    # sign-changing SPREAD is cooldown-lattice SYNCHRONISATION, a second-order effect
    # steady does not address (test_the_score_bonus_gap_is_not_a_uniform_scale).
    assert load_scoring_config().active_uptime_model == "steady"


def test_config_file_with_partial_model_object_raises_schema_error_not_key_error(
    tmp_path,
):
    path = _write_config(tmp_path, {"model": {"activation_rate": "pp"}})
    with pytest.raises(SchemaError) as excinfo:
        load_scoring_config(path)
    assert excinfo.value.field == "model.stacking"


# --- assumed_run_state rejections ---------------------------------------------


def test_non_numeric_assumed_run_state_dimension_is_rejected_naming_it():
    errors = validate_scoring_config(
        {"assumed_run_state": {"life": "yes", "combo": 100}}
    )
    assert any(e.startswith("assumed_run_state") and "life" in e for e in errors)


def test_boolean_assumed_run_state_dimension_is_rejected_rather_than_coerced():
    """The pre-revision shape must fail loudly, not load as 1/0.

    `bool` is a subclass of `int`, so a stale `"life": true` would otherwise
    validate and then load as `life=1` — silently stopping every LIFE gate
    above threshold 1 from firing while every output still looked
    well-formed. That is the migration failure this rejection exists to
    prevent.
    """
    errors = validate_scoring_config(
        {"assumed_run_state": {"life": True, "combo": 100}}
    )
    assert any(e.startswith("assumed_run_state") and "life" in e for e in errors)


def test_unmapped_assumed_run_state_key_is_rejected_naming_assumed_run_state_and_the_key():
    errors = validate_scoring_config(
        {
            "assumed_run_state": {
                "life": 1000,
                "combo": 100,
                "extra_life_up": 1,
            }
        }
    )
    assert any(
        e.startswith("assumed_run_state") and "extra_life_up" in e for e in errors
    )


# --- reference_run_seconds rejection ------------------------------------------


def test_reference_run_seconds_at_top_level_is_rejected_as_a_fixed_constant():
    errors = validate_scoring_config({"reference_run_seconds": 60.0})
    assert any(e.startswith("reference_run_seconds") for e in errors)


def test_reference_run_seconds_nested_anywhere_in_the_document_is_rejected():
    errors = validate_scoring_config({"special": {"reference_run_seconds": 60.0}})
    assert any(e.startswith("reference_run_seconds") for e in errors)


# --- Malformed-but-present file raises, never falls back ----------------------


def test_malformed_config_file_raises_schema_error_naming_the_field_not_falling_back(
    tmp_path,
):
    path = _write_config(tmp_path, {"model": {"stacking": "geometric", "activation_rate": "pp"}})
    with pytest.raises(SchemaError) as excinfo:
        load_scoring_config(path)
    assert excinfo.value.field == "model.stacking"


# --- Shipped defaults never drift out of their own contract -------------------


def test_repo_root_config_json_passes_validate_scoring_config_with_empty_errors():
    with open("config.json", "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    assert validate_scoring_config(doc) == []


def test_default_config_passes_validate_scoring_config_with_empty_errors():
    assert validate_scoring_config(DEFAULT_CONFIG) == []


# --- Model resolution goes through the registries, in exactly one place -------


def test_load_scoring_config_resolves_stacking_and_activation_rate_models_through_the_registries():
    cfg = load_scoring_config()
    assert cfg.stacking_model in STACKING_MODELS.values()
    assert cfg.activation_rate_model in ACTIVATION_RATE_MODELS.values()


def test_load_scoring_config_on_file_choosing_multiplicative_resolves_the_multiplicative_instances(
    tmp_path,
):
    doc = dict(DEFAULT_CONFIG)
    doc["model"] = {"stacking": "multiplicative", "activation_rate": "multiplicative"}
    path = _write_config(tmp_path, doc)
    cfg = load_scoring_config(path)
    assert cfg.stacking_model is STACKING_MODELS["multiplicative"]
    assert cfg.activation_rate_model is ACTIVATION_RATE_MODELS["multiplicative"]
