"""Scoring configuration: load, validate and construct `ScoringConfig` from `config.json`.

`config.json` (repo root) holds the user-tunable scoring constants: probability tiers,
family weights, the SPECIAL constant, model choices and the assumed run state. Loading
follows `loader.py`'s idiom (`json.load`, `validate_scoring_config`, a frozen dataclass,
`SchemaError` naming the first bad field). `DEFAULT_CONFIG` applies only when the file is
absent, and every top-level key records its provenance (`"file"` or `"default"`) so a run
report can name an assumption as one. A present but malformed file always raises."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from hbuilder import paths
from hbuilder.domain.gates import RunState
from hbuilder.domain.schema import (
    PROBABILITY_TIERS,
    RUN_STATE_DIMENSIONS,
    SKILL_FAMILIES,
    SchemaError,
)
from hbuilder.domain.scoring_models import (
    ACTIVATION_RATE_MODELS,
    DEFAULT_ACTIVE_STACKING,
    STACKING_MODELS,
    TARGET_SELECTION_MODELS,
    ActivationRateModel,
    ActiveStackingModel,
    StackingModel,
    TargetSelectionModel,
)

if TYPE_CHECKING:
    from hbuilder.domain.board import BoardModel
    from hbuilder.domain.collection import AccountBonuses

# The values `target_selection.under_supply` may take, both implemented so a user can
# compare rankings under either reading.
UNDER_SUPPLY_RULES: tuple[str, ...] = ("apply_to_available", "do_not_apply")

# How a member set's stat pool is scored (`scoring.SLOT_ORDER_RESOLVERS` implements it).
#
#   "free": a count-limited effect reaches the `count` matching members with the
#           greatest base value of the stat it raises (total parameter for an all-stats
#           effect), via `targets.resolve_target_card_ids`; 690/690 against captured
#           server rows. Slot order only breaks an exact tie, where the payout is the
#           same, so `member_order` is `None`.
#
# Not a config.json key: the rule is measured from the game.
SLOT_ORDER_RULES: tuple[str, ...] = ("free",)

# Model names a config may not select, with the reason its error message gives.
REMOVED_MODELS: dict[tuple[str, str], str] = {
    ("stacking", "multiplicative"): "the game's rule is 'additive'",
    ("activation_rate", "pp"): "the game's rule is 'multiplicative'",
}


def _removed_note(key: str, value: object) -> str:
    reason = REMOVED_MODELS.get((key, value)) if isinstance(value, str) else None
    return f" ({value!r} was removed: {reason})" if reason else ""


# A fixed normalisation denominator, not a config key: it makes SPECIAL's
# `fires * duration / REFERENCE_RUN_SECONDS` commensurable with ACTIVE's uptime (both
# "fraction of the run the effect is live"); tuning it would rescale the whole SPECIAL
# family against ACTIVE.
#
# Solved from the game's numbers: captured deck evaluations report a Special line of
# `Active line * SUM(magnitude * duration) / T`, and solving `T` per deck from
# `LiveSpecialSkillLevel` gives, over five distinct decks,
#
#     130.5  133.7  126.8  127.1  125.8   ->  mean 128.8 s
#
# within 6.2 %, against a 90 % spread for a direct-score reading. It is biased high
# (every SPECIAL is taken at skill level 2; a level-1 card has a smaller magnitude), so
# the true value is likely nearer 126; fixing that needs each card's bloom stage, which
# the deck captures lack.
REFERENCE_RUN_SECONDS = 128.8

DEFAULT_CONFIG_PATH = paths.CONFIG

# The ACTIVE uptime references `ScoringConfig.active_uptime_model` selects between.
ACTIVE_UPTIME_MODELS: tuple[str, ...] = ("finite", "steady")

# The master's `LiveNote` table has a PERFECT column per client:
# `scoreCoefficientPermilMultiply` (mobile) and `pcScoreCoefficientPermilMultiply`
# (Steam). They differ in one cell: FLICK x PERFECT/PERFECT_PLUS is 1050 permil on mobile
# and 1000 on PC. `ScoringConfig.platform` selects the column; charts stay neutral.
PLATFORMS: tuple[str, ...] = ("mobile", "pc")
DEFAULT_PLATFORM = "mobile"

# Mirrors config.json; used only when the file is absent.
DEFAULT_CONFIG: dict = {
    "schema_version": "1",
    # `LiveActiveSkillLevel.f8` is the activation chance in permil and takes exactly
    # three values across the catalogue: 370 / 460 / 550. 460 and 550 match a 50-card
    # transcription of "Medium" and "High"; 370 is "Low" by elimination (the only other
    # value, present only on 3★ cards).
    "probability_tiers": {
        "Low": 0.37,
        "Medium": 0.46,
        "High": 0.55,
    },
    "family_weights": {
        "score_support": 1.0,
        "conditional_score_up": 1.0,
        "stat_up": 1.0,
        # Pinned to 0.0 (validated): the family folds into team_rate_up, not a pool.
        "activation_rate_up": 0.0,
        "life_recovery": 0.0,
        "judgment_enhancement": 0.0,
    },
    "special": {
        "expected_fires_per_run": 1.0,
    },
    # `multiplicative` is the game's rule: `ActiveSkillEffectDataCollection::
    # CalcActivationProbabilityPermil` (rva `0x5efcf00`) ends
    #
    #     addss xmm7,1000.0 ; addss xmm7,bonuses ; mulss xmm7,xmm0 ; divss xmm7,1000.0
    #
    # so the bonuses SCALE the tier rather than adding percentage points. Adding points
    # would overstate the bonus by 1/tier (2.70x at Low, 1.82x at High), a tier-dependent
    # error that reorders teams. Pinned by `tests/test_activation_rate_model_default.py`.
    "model": {
        "stacking": "additive",
        "activation_rate": "multiplicative",
        # "finite" (in-run, the default) or "steady" (deck screen); see
        # ScoringConfig.active_uptime_model. Optional: absent reads "finite".
        "active_uptime": "finite",
    },
    # Which N members receive a targeted buff when more than N qualify is client logic in
    # no master table (`LiveSkillEffectTarget`, `LiveSkillTrigger`,
    # `LiveSkillEffectFilterCategory`, `Setting` and `Rule` have no ordering field): the
    # top N by the base value of the stat the buff raises, as the server's deck figures
    # show. With fewer qualifiers than the count, the ones there are receive it (the count
    # is not a gate), as the server's figures show too.
    "target_selection": {
        "strategy": "top_n_by_stat",
        "under_supply": "apply_to_available",
    },
    # Numeric run-state snapshot (numeric because gates use several thresholds per
    # dimension). `life` 1000: LIFE starts at its 1000 maximum and only drops on misses,
    # so under perfect play every LIFE gate (600 and 1000 in the data) fires.
    #
    # `combo` is all-or-nothing, not simulated: a combo gate holds only from the note
    # that reaches its threshold, which needs the song's note count, and the run-average
    # scorer takes no song. The data's combo thresholds are 40 (ACTIVE) and 100 (SPECIAL);
    # a gate above this value would not fire, and the value is printed in the
    # assumptions header.
    "assumed_run_state": {
        "life": 1000,
        "combo": 100,
    },
}

_TOP_LEVEL_KEYS: tuple[str, ...] = tuple(DEFAULT_CONFIG.keys())


def _split_error(error: str) -> tuple[str, str]:
    """Split a `{field}: {detail}` error string into `(field, message)`."""
    field, _, _ = error.partition(": ")
    return field, error


def _contains_reference_run_seconds(value: object) -> bool:
    """True if `"reference_run_seconds"` is a key anywhere in `value`, at any depth. It stays
    off the config surface: it looks like a calibration knob but would rescale the whole
    SPECIAL family against ACTIVE."""
    if isinstance(value, dict):
        if "reference_run_seconds" in value:
            return True
        return any(_contains_reference_run_seconds(v) for v in value.values())
    if isinstance(value, list):
        return any(_contains_reference_run_seconds(v) for v in value)
    return False


def validate_scoring_config(doc: object) -> list[str]:
    """Validate a `config.json`-shaped document or fragment. Returns `{field}: {detail}` error
    strings (empty means valid); never raises and never repairs.

    Only keys present in `doc` are checked (absent keys are filled from `DEFAULT_CONFIG` by
    `load_scoring_config`), so the same function validates a partial fragment, the merged
    document, `config.json` and `DEFAULT_CONFIG`. Reuses `schema.py`'s and
    `scoring_models.py`'s tables rather than redeclaring them."""
    if not isinstance(doc, dict):
        return ["root: scoring config document must be a JSON object"]

    errors: list[str] = []

    if _contains_reference_run_seconds(doc):
        errors.append(
            "reference_run_seconds: this is a fixed constant (D-21) that "
            "normalizes SPECIAL against ACTIVE, not a tunable — it must "
            "not appear anywhere in config.json"
        )

    if "probability_tiers" in doc:
        tiers = doc["probability_tiers"]
        if not isinstance(tiers, dict):
            errors.append("probability_tiers: must be a JSON object")
        else:
            for tier_name in PROBABILITY_TIERS:
                if tier_name not in tiers:
                    errors.append(f"probability_tiers: missing required tier '{tier_name}'")
                    continue
                value = tiers[tier_name]
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    errors.append(f"probability_tiers: tier '{tier_name}' must be a number, got {value!r}")
                elif not (0.0 <= value <= 1.0):
                    errors.append(
                        f"probability_tiers: tier '{tier_name}' must be between 0.0 and 1.0, got {value!r}"
                    )
            for tier_name in tiers:
                if tier_name not in PROBABILITY_TIERS:
                    errors.append(f"probability_tiers: {tier_name!r} is not one of {list(PROBABILITY_TIERS)}")

    if "family_weights" in doc:
        weights = doc["family_weights"]
        if not isinstance(weights, dict):
            errors.append("family_weights: must be a JSON object")
        else:
            for family_name in SKILL_FAMILIES:
                if family_name not in weights:
                    errors.append(f"family_weights: missing required family '{family_name}'")
                    continue
                value = weights[family_name]
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    errors.append(
                        f"family_weights: family '{family_name}' weight must be a number, got {value!r}"
                    )
                elif family_name == "activation_rate_up" and value != 0.0:
                    # The family is folded into team_rate_up, so a nonzero weight would be
                    # an inert knob that looks live.
                    errors.append(
                        f"family_weights: family 'activation_rate_up' "
                        f"weight must be exactly 0.0 (D-17 folds this "
                        f"family into the team rate modifier instead of a "
                        f"score pool, so no weight here can scale "
                        f"anything), got {value!r}"
                    )
                elif value < 0:
                    # No game mechanic lowers score through a skill family, and the `>=`
                    # lower-bound guarantee rests on non-negative weights.
                    errors.append(
                        f"family_weights: family '{family_name}' weight must be non-negative, got {value!r}"
                    )
            for family_name in weights:
                if family_name not in SKILL_FAMILIES:
                    errors.append(f"family_weights: {family_name!r} is not one of {list(SKILL_FAMILIES)}")

    if "special" in doc:
        special = doc["special"]
        if not isinstance(special, dict):
            errors.append("special.expected_fires_per_run: 'special' must be a JSON object")
        elif "expected_fires_per_run" not in special:
            errors.append("special.expected_fires_per_run: missing required key")
        else:
            value = special["expected_fires_per_run"]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                errors.append(f"special.expected_fires_per_run: must be a number, got {value!r}")
            elif value < 0:
                errors.append(f"special.expected_fires_per_run: must be non-negative, got {value!r}")

    if "model" in doc:
        model = doc["model"]
        if not isinstance(model, dict):
            errors.append("model: must be a JSON object")
        else:
            if "stacking" not in model:
                errors.append("model.stacking: missing required key")
            elif model["stacking"] not in STACKING_MODELS:
                errors.append(
                    f"model.stacking: {model['stacking']!r} is not one of "
                    f"{sorted(STACKING_MODELS)}{_removed_note('stacking', model['stacking'])}"
                )
            if "activation_rate" not in model:
                errors.append("model.activation_rate: missing required key")
            elif model["activation_rate"] not in ACTIVATION_RATE_MODELS:
                errors.append(
                    f"model.activation_rate: {model['activation_rate']!r} "
                    f"is not one of {sorted(ACTIVATION_RATE_MODELS)}"
                    f"{_removed_note('activation_rate', model['activation_rate'])}"
                )
            # Optional (absent reads "finite"), but a present value must be valid.
            if "active_uptime" in model and model["active_uptime"] not in ACTIVE_UPTIME_MODELS:
                errors.append(
                    f"model.active_uptime: {model['active_uptime']!r} "
                    f"is not one of {sorted(ACTIVE_UPTIME_MODELS)}"
                )

    if "target_selection" in doc:
        target_selection = doc["target_selection"]
        if not isinstance(target_selection, dict):
            errors.append("target_selection: must be a JSON object")
        else:
            if "strategy" not in target_selection:
                errors.append("target_selection.strategy: missing required key")
            elif target_selection["strategy"] not in TARGET_SELECTION_MODELS:
                errors.append(
                    f"target_selection.strategy: "
                    f"{target_selection['strategy']!r} is not one of "
                    f"{sorted(TARGET_SELECTION_MODELS)}"
                )
            if "under_supply" not in target_selection:
                errors.append("target_selection.under_supply: missing required key")
            elif target_selection["under_supply"] not in UNDER_SUPPLY_RULES:
                errors.append(
                    f"target_selection.under_supply: "
                    f"{target_selection['under_supply']!r} is not one of "
                    f"{list(UNDER_SUPPLY_RULES)}"
                )

    if "assumed_run_state" in doc:
        run_state_doc = doc["assumed_run_state"]
        if not isinstance(run_state_doc, dict):
            errors.append("assumed_run_state: must be a JSON object")
        else:
            for dimension in RUN_STATE_DIMENSIONS:
                if dimension not in run_state_doc:
                    errors.append(f"assumed_run_state: missing required dimension '{dimension}'")
            for dimension, value in run_state_doc.items():
                if dimension not in RUN_STATE_DIMENSIONS:
                    errors.append(
                        f"assumed_run_state: {dimension!r} is not one of {list(RUN_STATE_DIMENSIONS)}"
                    )
                # `bool` is an `int`: reject it, or `true`/`false` would load as 1/0 and
                # silently disable every gate above threshold 1.
                elif isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    errors.append(
                        f"assumed_run_state: dimension '{dimension}' must be "
                        f"a non-negative integer, got {value!r}"
                    )

    return errors


@dataclass(frozen=True, slots=True)
class ScoringConfig:
    probability_tiers: dict
    family_weights: dict
    expected_fires_per_run: float
    stacking_model: StackingModel
    activation_rate_model: ActivationRateModel
    target_selection_model: TargetSelectionModel
    target_under_supply: str
    run_state: RunState
    provenance: dict
    # Holomem -> ACTIVE cooldown reduction as a fraction. Only the Holomem Board supplies
    # it (branch B: three 4 % nodes, so 0.12 at most), and it DIVIDES:
    # `cooldown / (1 + reduction)`. Account state, here because `_score_active` receives
    # only the config. Empty means no reduction.
    active_cooldown_reduction: dict = field(default_factory=dict)
    # Holomem -> that holomem's board Score Bonus as a fraction (0.115 = +11.5 %): the
    # game's `ScoreUpBonusRatioBySkillTree`, a separate trailing factor in `CalcScore`
    # (`... x (1 + SkillScoreUpRatio) x (1 + ScoreUpBonusRatioBySkillTree)`), read only by
    # `scoring._board_score_rate`. Keyed by holomem because the board reaches every skin
    # of a character. Summing the five members' entries is an assumption: the game gives
    # one deck-level float. Account state; empty means a factor of exactly 1.0.
    board_score_bonus: dict = field(default_factory=dict)
    # The structural Holomem Board model (`domain/board.py`), or `None`. When set it takes
    # precedence over `board_score_bonus`, which cannot express branch scopes (a red node
    # applies to the whole team only while its holomem leads). Captured account state,
    # not a config.json key.
    board: BoardModel | None = None
    # How the team's ACTIVE score-up skills combine into one pool; not a config.json key
    # (see `scoring_models.ACTIVE_STACKING_MODELS`). On the config because `scoring.py`
    # receives nothing else.
    active_stacking_model: ActiveStackingModel = DEFAULT_ACTIVE_STACKING
    # Which deck power the per-song modes score on. `True`: the deck screen's own Overall
    # Power (`song.overall_power_for`), the quantity the game multiplies, bit-exact against
    # the server. `False`: the modelled stat pool, fine for comparisons but about 4 % low
    # in absolute terms (over 150 teams on m0303 the server power is 3.4-5.2 % higher,
    # with Kendall tau 0.993 and the same top 10). Costs about 8 % of a per-song run.
    song_absolute_deck_power: bool = True
    # Score the per-song modes for an AUTO run (`song_rank._score_one` ->
    # `score_song_detailed(auto=True)`). On AUTO the combo never accumulates, so a
    # `combo >= N` override never fires (life-gated ones do): an AUTO ranking must also
    # set `run_state.combo = 0` (the web app's AUTO option sets both).
    song_auto: bool = False
    # Which ACTIVE uptime a run averages (not a free knob):
    #   "finite": the finite-window count on the real cooldown lattice, with the
    #     start-of-song dead zone; the in-run truth (`_finite_window_uptime`, checked
    #     against the note-weighted integral over the shipped charts).
    #   "steady": `min(1, duration/interval)`, no dead zone; what the song-independent
    #     deck screen shows. It moves the deck-screen Score Bonus residual over 71 saved
    #     decks from median -8.1 % to +2.3 % (decks within +/-5 %: 15/71 -> 45/71).
    # The deck-screen driver passes "steady".
    active_uptime_model: str = "finite"
    # Runtime-only ceiling: every ACTIVE fires with probability 1, the "all actives fired"
    # upper bound. Not a config.json key: `replace(cfg, active_all_fire=True)`.
    active_all_fire: bool = False
    # The account scalars (posters collected, Member Power-Up permyriad): captured account
    # state, not a config.json key. `None` leaves both terms out.
    account: AccountBonuses | None = None
    # The slot-order rule that scores the stat pool, one of `SLOT_ORDER_RULES`.
    slot_order_rule: str = SLOT_ORDER_RULES[0]
    # Which client's PERFECT column the per-song scorer weighs notes with (`PLATFORMS`).
    # Charts carry a per-note flick flag, so the choice is made at scoring time. A runtime
    # what-if, not a config.json key: `replace(cfg, platform="pc")`.
    platform: str = DEFAULT_PLATFORM


def run_state_from_record(record: dict) -> RunState:
    return RunState(
        life=int(record["life"]),
        combo=int(record["combo"]),
    )


def _config_from_merged(merged: dict, provenance: dict) -> ScoringConfig:
    return ScoringConfig(
        probability_tiers=merged["probability_tiers"],
        family_weights=merged["family_weights"],
        expected_fires_per_run=merged["special"]["expected_fires_per_run"],
        stacking_model=STACKING_MODELS[merged["model"]["stacking"]],
        activation_rate_model=ACTIVATION_RATE_MODELS[merged["model"]["activation_rate"]],
        # Optional key: a "model" block without it reads "finite".
        active_uptime_model=merged["model"].get("active_uptime", "finite"),
        target_selection_model=TARGET_SELECTION_MODELS[merged["target_selection"]["strategy"]],
        target_under_supply=merged["target_selection"]["under_supply"],
        run_state=run_state_from_record(merged["assumed_run_state"]),
        provenance=provenance,
    )


def load_scoring_config(path: Path = DEFAULT_CONFIG_PATH) -> ScoringConfig:
    """Read `path` (default: repo-root `config.json`) into a `ScoringConfig`.

    A missing file falls back to `DEFAULT_CONFIG` in full (every provenance `"default"`).
    Otherwise the file is merged over `DEFAULT_CONFIG` per top-level key (a present key wins
    whole and is `"file"`), then validated: a malformed file raises `SchemaError` naming the
    field rather than falling back."""
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except FileNotFoundError:
        provenance = {key: "default" for key in _TOP_LEVEL_KEYS}
        return _config_from_merged(DEFAULT_CONFIG, provenance)

    merged: dict = {}
    provenance = {}
    for key in _TOP_LEVEL_KEYS:
        if key in doc:
            merged[key] = doc[key]
            provenance[key] = "file"
        else:
            merged[key] = DEFAULT_CONFIG[key]
            provenance[key] = "default"

    config_errors = validate_scoring_config(merged)
    if config_errors:
        field, message = _split_error(config_errors[0])
        raise SchemaError(message, field=field)

    return _config_from_merged(merged, provenance)


# The shipped default must pass its own validator.
assert validate_scoring_config(DEFAULT_CONFIG) == [], (
    "DEFAULT_CONFIG must itself pass validate_scoring_config — a shipped "
    "default cannot be rejected by the same validator that gates a "
    "hand-edited file"
)
