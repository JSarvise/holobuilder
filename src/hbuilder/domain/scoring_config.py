"""The second and last I/O boundary in `domain/` — narrower than
`loader.py` because `config.json` is scoring configuration, not canonical
card data. Mirrors `loader.py`'s load/validate/construct idiom exactly:
`json.load`, then `validate_scoring_config`, then construct a frozen
dataclass, raising `SchemaError` naming the offending field on the first
validation failure — the same discipline `loader.py` applies to
`cards.json`.

`config.json` at the repo root holds the user-tunable scoring constants
(probability tiers, family weights, the SPECIAL constant, model defaults,
assumed run-state) — separate from `data/`, which holds card data. `DEFAULT_CONFIG` is used only when the file is
absent (a plain missing-file fall back, the one sanctioned try/catch
boundary in this module), and every load records per-top-level-key
provenance (`"file"` vs. `"default"`) so a later run report can name an
assumption as an assumption, never as a measurement. A present-but-malformed
file always raises — never silently falls back to defaults, which would let a
defaulted value be reported with `"file"` provenance. `validate_scoring_config`
also enforces that `family_weights["activation_rate_up"]` is pinned to
exactly `0.0`, so a hand-edited nonzero value — which would silently do
nothing — is rejected loudly instead of read as a working knob.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

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

# The two values `target_selection.under_supply` may take. Both are
# implemented, never a stub — a user who believes the effect simply does not fire on under-supply can switch to
# "do_not_apply" and compare rankings, the same escape hatch
# STACKING_MODELS gives the other unverified semantics.
UNDER_SUPPLY_RULES: tuple[str, ...] = ("apply_to_available", "do_not_apply")

# How a member SET's stat pool is scored once deck slot order is known to
# matter (`scoring.SLOT_ORDER_RESOLVERS` holds the two implementations and
# asserts it covers exactly these names).
#
#   "free"       — the default rule, which matches 690/690 captured server
#                  rows. A count-limited effect reaches the `count` matching
#                  members with the greatest base value of the STAT IT RAISES
#                  (total parameter for an all-stats effect), resolved by
#                  `targets.resolve_target_card_ids`. Slot order only breaks an
#                  exact tie, where the payout is the same either way, so a set
#                  has a single stat sum and `member_order` is `None`.
#   "best_order" — a refuted alternative. Assumes the player picks the recipients
#                  via slot order (first `count` IN SLOT ORDER), so a set would
#                  score at the MAXIMUM over its 120 orderings. Kept runnable so the
#                  refuted rule stays testable and `ScoreBreakdown` can name it.
#
# Captured decks are bit-exact on ranking recipients by the buffed stat and match
# only 510/690 rows on ranking by total parameter; first-by-slot ("best_order")
# matches neither. An older capture (tests/test_passive_power_line.py, row 9)
# shows the total rule paying, so the server's basis has changed over time.
#
# Deliberately NOT a `config.json` key: the rule is measured from the game, not
# a user guess. It lives on `ScoringConfig` so the refuted alternative stays
# runnable under test with one `dataclasses.replace`.
SLOT_ORDER_RULES: tuple[str, ...] = ("free", "best_order")

# A fixed normalization denominator, not a config key. It makes
# SPECIAL's `fires * duration / reference_run_seconds` commensurable with
# ACTIVE's `duration / interval` — both formulas produce "fraction of the
# run the effect is live". Tuning it would silently rescale the entire
# SPECIAL family against ACTIVE, so it is reported as a fixed constant, never
# as an assumed value alongside the config'd tiers.
#
# It is solved from the game's own numbers. Ten captured deck evaluations report a Score Bonus decomposition whose Special line is
# `Active line * SUM(magnitude * duration) / T`; solving `T` per deck from
# `LiveSpecialSkillLevel` gives, across the five distinct decks,
#
#     130.5  133.7  126.8  127.1  125.8   ->  mean 128.8 s
#
# holding to 6.2 %, against a 90 % spread for a direct-score reading.
#
# The estimate is biased HIGH: it assumes every SPECIAL is at skill level 2,
# and a card sitting at level 1 has a smaller magnitude, which inflates the
# solved `T`. So 128.8 is an upper bound on a value somewhere near 126.
# Recomputing it needs each card's real bloom stage, which the client's
# `Transaction.UserCard` carries and the deck captures do not.
REFERENCE_RUN_SECONDS = 128.8

DEFAULT_CONFIG_PATH = Path("config.json")

# The two ACTIVE-skill uptime references `active_uptime_model` selects between:
# "finite" (in-run, with the start-of-song dead zone) and "steady" (the
# deck-screen panel's song-independent min(1, duration/interval)). See
# `ScoringConfig.active_uptime_model`.
ACTIVE_UPTIME_MODELS: tuple[str, ...] = ("finite", "steady")

# The two clients the master's `LiveNote` table carries a PERFECT column for:
# `scoreCoefficientPermilMultiply` (mobile) and `pcScoreCoefficientPermilMultiply`
# (the Steam build). The 38 rows differ in exactly ONE cell (note_type 2 FLICK
# x PERFECT/PERFECT_PLUS): FLICK is 1050 permil on mobile and 1000 on PC.
# `ScoringConfig.platform` selects the column at scoring time; the chart files
# stay platform-neutral. Default "mobile".
PLATFORMS: tuple[str, ...] = ("mobile", "pc")
DEFAULT_PLATFORM = "mobile"

# Mirrors config.json exactly; used only when the file is absent.
DEFAULT_CONFIG: dict = {
    "schema_version": "1",
    # Read from the game's own data. `LiveActiveSkillLevel.f8` is the activation chance in
    # per-mille and takes exactly three values across all 167 cards: 370 / 460 / 550.
    # 460 == "Medium" and 550 == "High" match an independent 50-card transcription card for
    # card; 370 == "Low" is by elimination (the only remaining value, present only on 3★
    # cards, and no "Low" appears anywhere in the 4★/5★ set).
    "probability_tiers": {
        "Low": 0.37,
        "Medium": 0.46,
        "High": 0.55,
    },
    "family_weights": {
        "score_support": 1.0,
        "conditional_score_up": 1.0,
        "stat_up": 1.0,
        # Pinned to 0.0: activation_rate_up folds entirely into
        # team_rate_up, never a score pool, so this weight cannot scale
        # anything. validate_scoring_config enforces the pin.
        "activation_rate_up": 0.0,
        "life_recovery": 0.0,
        "judgment_enhancement": 0.0,
    },
    "special": {
        "expected_fires_per_run": 1.0,
    },
    # `multiplicative` is what the game does and `pp` is refuted. `Vision.LiveCore!ActiveSkillEffectDataCollection::
    # CalcActivationProbabilityPermil` @ rva `0x5efcf00` ends
    #
    #     addss xmm7,1000.0 ; addss xmm7,bonuses ; mulss xmm7,xmm0 ; divss xmm7,1000.0
    #
    # — the accumulated bonuses are added to 1000 per-mille and the whole thing
    # then MULTIPLIES the tier (`xmm0`) before the divide by 1000. The bonus
    # SCALES the tier; it is not added in percentage points.
    #
    # The two models disagree by exactly `1 / tier` on the size of the bonus,
    # so the error is TIER-DEPENDENT: `pp` overstates the bonus 1/0.37 = 2.70x
    # at Low and 1/0.55 = 1.82x at High. A tier-dependent error REORDERS teams;
    # it does not merely offset them.
    # Pinned by `tests/test_activation_rate_model_default.py`.
    "model": {
        "stacking": "additive",
        "activation_rate": "multiplicative",
        # Which ACTIVE-skill uptime reference to score by: "finite" (in-run,
        # with the start-of-song dead zone — the default) or "steady" (the
        # deck-screen panel's song-independent min(1, duration/interval)). See
        # ScoringConfig.active_uptime_model.
        # Optional for back-compat: a "model" block that omits it reads "finite".
        "active_uptime": "finite",
    },
    # Which N members receive a targeted buff when more than N qualify is
    # client logic in no master table — `LiveSkillEffectTarget` has no
    # priority/ordering field and neither do `LiveSkillTrigger`,
    # `LiveSkillEffectFilterCategory`, `Setting` or `Rule`. The default is
    # "top-N by the buffed stat".
    # `under_supply` (fewer qualifying cards than the target's own count)
    # has no rule in the master data either — the default assumes the count is not itself a gate, a deliberately weak
    # argument, which is why it is reported on every run rather than
    # buried (see `output/assumptions.py`).
    "target_selection": {
        "strategy": "top_n_by_stat",
        "under_supply": "apply_to_available",
    },
    # Numeric run-state snapshot. `life` is 1000
    # because LIFE STARTS at its 1000 maximum and only decreases on missed
    # notes — under the project's perfect-play premise it never leaves the
    # maximum, so every attested LIFE gate (600 and 1000 are both attested,
    # on different skills) fires.
    #
    # `combo` is an all-or-nothing assumption, not a simulated value. Two
    # independent reasons: (1) a combo gate is satisfied only from the note
    # that reaches its threshold onward, so modelling it proportionally needs
    # the song's note count, and the run-average scorer takes no song-length
    # parameter because song length cancels out of every team-vs-team comparison;
    # (2) expectation is linear — `(duration/interval) * P(tier)` already IS
    # the expected active fraction, so simulating discrete procs would add
    # variance, not a better expectation, and the objective is maximum
    # EXPECTED score.
    #
    # Attested combo thresholds in the 50-card SSR dataset: 40 (7 ACTIVE
    # skills) and 100 (4 SPECIAL skills). Both fire at this value. A combo
    # gate ABOVE this number would NOT fire; raise it if one is ever found
    # (the value is printed in the assumptions header, so the assumption is
    # visible rather than silent).
    #
    # Attested LIFE thresholds: 600 (6 ACTIVE) and 1000 (5 SPECIAL). Both
    # fire at life=1000. Four distinct run-state thresholds across two
    # dimensions is why run state is numeric rather than one boolean per
    # dimension.
    "assumed_run_state": {
        "life": 1000,
        "combo": 100,
    },
}

_TOP_LEVEL_KEYS: tuple[str, ...] = tuple(DEFAULT_CONFIG.keys())


def _split_error(error: str) -> tuple[str, str]:
    """Split a `validate_scoring_config` error string into (field,
    message). Every error is formatted as `{field}: {detail}`, mirroring
    `loader.py`'s own `_split_error` exactly, so this split is exact, not a
    heuristic."""
    field, _, _ = error.partition(": ")
    return field, error


def _contains_reference_run_seconds(value: object) -> bool:
    """True if the string `"reference_run_seconds"` appears as a key
    anywhere in `value`, at any nesting depth — not just at the top level.
    It is kept off the config surface on purpose: it looks like a
    harmless calibration knob but would silently rescale the entire
    SPECIAL family against ACTIVE."""
    if isinstance(value, dict):
        if "reference_run_seconds" in value:
            return True
        return any(_contains_reference_run_seconds(v) for v in value.values())
    if isinstance(value, list):
        return any(_contains_reference_run_seconds(v) for v in value)
    return False


def validate_scoring_config(doc: object) -> list[str]:
    """Validate a `config.json`-shaped document (or a partial fragment of
    one). Returns human-readable error strings, each formatted as
    `{field}: {detail}` (empty list means valid) — mirrors `schema.py`'s
    `validate_*` convention exactly: it never raises and never repairs a
    value.

    Only validates top-level keys actually present in `doc` — an absent
    key is `load_scoring_config`'s responsibility (filled from
    `DEFAULT_CONFIG`), not a validation failure here. This lets the same
    function validate a hand-authored partial fragment (e.g. a file
    supplying only `probability_tiers`), the fully-merged document
    `load_scoring_config` builds, and the repo-root `config.json` and
    `DEFAULT_CONFIG` themselves.

    Reuses `schema.py`'s constant tables (`PROBABILITY_TIERS`,
    `SKILL_FAMILIES`, `RUN_STATE_DIMENSIONS`) and `scoring_models.py`'s
    `STACKING_MODELS`/`ACTIVATION_RATE_MODELS` key sets — never
    re-declares them."""
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
                    errors.append(
                        f"probability_tiers: missing required tier "
                        f"'{tier_name}'"
                    )
                    continue
                value = tiers[tier_name]
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    errors.append(
                        f"probability_tiers: tier '{tier_name}' must be a "
                        f"number, got {value!r}"
                    )
                elif not (0.0 <= value <= 1.0):
                    errors.append(
                        f"probability_tiers: tier '{tier_name}' must be "
                        f"between 0.0 and 1.0, got {value!r}"
                    )
            for tier_name in tiers:
                if tier_name not in PROBABILITY_TIERS:
                    errors.append(
                        f"probability_tiers: {tier_name!r} is not one of "
                        f"{list(PROBABILITY_TIERS)}"
                    )

    if "family_weights" in doc:
        weights = doc["family_weights"]
        if not isinstance(weights, dict):
            errors.append("family_weights: must be a JSON object")
        else:
            for family_name in SKILL_FAMILIES:
                if family_name not in weights:
                    errors.append(
                        f"family_weights: missing required family "
                        f"'{family_name}'"
                    )
                    continue
                value = weights[family_name]
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    errors.append(
                        f"family_weights: family '{family_name}' weight "
                        f"must be a number, got {value!r}"
                    )
                elif family_name == "activation_rate_up" and value != 0.0:
                    # This family's entire contribution is
                    # folded into team_rate_up, never a score pool, so no
                    # weight here can scale anything — a nonzero value
                    # would be an inert knob masquerading as a live one.
                    errors.append(
                        f"family_weights: family 'activation_rate_up' "
                        f"weight must be exactly 0.0 (D-17 folds this "
                        f"family into the team rate modifier instead of a "
                        f"score pool, so no weight here can scale "
                        f"anything), got {value!r}"
                    )
                elif value < 0:
                    # There is no known hololive Dreams mechanic
                    # where a skill family reduces score. A negative weight
                    # would silently collapse the non-negativity
                    # invariant the >= lower-bound guarantee rests on — a
                    # hand-edited `judgment_enhancement: -0.5` validates
                    # clean without this check.
                    errors.append(
                        f"family_weights: family '{family_name}' weight "
                        f"must be non-negative, got {value!r}"
                    )
            for family_name in weights:
                if family_name not in SKILL_FAMILIES:
                    errors.append(
                        f"family_weights: {family_name!r} is not one of "
                        f"{list(SKILL_FAMILIES)}"
                    )

    if "special" in doc:
        special = doc["special"]
        if not isinstance(special, dict):
            errors.append("special.expected_fires_per_run: 'special' must be a JSON object")
        elif "expected_fires_per_run" not in special:
            errors.append("special.expected_fires_per_run: missing required key")
        else:
            value = special["expected_fires_per_run"]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                errors.append(
                    f"special.expected_fires_per_run: must be a number, "
                    f"got {value!r}"
                )
            elif value < 0:
                errors.append(
                    f"special.expected_fires_per_run: must be non-negative, "
                    f"got {value!r}"
                )

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
                    f"{sorted(STACKING_MODELS)}"
                )
            if "activation_rate" not in model:
                errors.append("model.activation_rate: missing required key")
            elif model["activation_rate"] not in ACTIVATION_RATE_MODELS:
                errors.append(
                    f"model.activation_rate: {model['activation_rate']!r} "
                    f"is not one of {sorted(ACTIVATION_RATE_MODELS)}"
                )
            # Optional (a pre-existing "model" block omits it and reads "finite"),
            # but a PRESENT value must be one of the two references.
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
                    errors.append(
                        f"assumed_run_state: missing required dimension "
                        f"'{dimension}'"
                    )
            for dimension, value in run_state_doc.items():
                if dimension not in RUN_STATE_DIMENSIONS:
                    errors.append(
                        f"assumed_run_state: {dimension!r} is not one of "
                        f"{list(RUN_STATE_DIMENSIONS)}"
                    )
                # `bool` is a subclass of `int`, so it must be rejected
                # explicitly — otherwise legacy boolean `true`/`false`
                # values would silently load as 1/0 and every run-state gate
                # above threshold 1 would stop firing without an error.
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
    # Holomem -> ACTIVE cooldown reduction, as a fraction. ONLY the Holomem
    # Board supplies this (no card can change an Active's cooldown or
    # probability), branch B carries exactly three 4 % nodes so 0.12 is the
    # ceiling, and it DIVIDES: `cooldown / (1 + reduction)`.
    #
    # This is account state, not a model choice, so it does not belong on a
    # config object on principle — it lives here because `_score_active`
    # receives the config and nothing else that knows whose board applies.
    #
    # Empty by default, which is the correct baseline: an absent entry means
    # "no board data for this holomem", and the reduction is then zero.
    active_cooldown_reduction: dict = field(default_factory=dict)
    # Holomem -> that holomem's Holomem Board Score Bonus
    # contribution, as a fraction (0.115 means +11.5%). This is the game's own
    # `CalcScoreResult.ScoreUpBonusRatioBySkillTree`, read out of
    # `Vision.LiveCore!LiveScoreCalculator::CalcScore` @ rva 0x5f01f80, whose
    # tail is
    #     ... x (1 + SkillScoreUpRatio) x (1 + ScoreUpBonusRatioBySkillTree)
    # — two SEPARATE multiplicative factors, corroborated by an independent
    # IDA Pro derivation. Everything the cards contribute is the first factor
    # (`combined_score_rate`); the board is the second and must never be summed
    # into it. `scoring.py::_board_score_rate` is the one place it is read.
    #
    # Keyed by HOLOMEM for the same reason `active_cooldown_reduction` is: the
    # board is per-character, so it reaches every skin of that character. Like
    # `active_cooldown_reduction`, this is account state, not a model choice.
    #
    # ⚠ The per-team AGGREGATION — summing the five members' entries — is an
    # assumption. The game gives one deck-level float; it is not known how the
    # deck's members combine into it, nor whether the leader slot contributes a
    # term of its own (`Live/GetDeck` carries a leader-scoped board field, for a
    # different quantity).
    #
    # Empty by default, which is the correct baseline: an absent entry means
    # "no board data for this holomem" and contributes a factor of exactly 1.0.
    board_score_bonus: dict = field(default_factory=dict)
    # The structural Holomem Board model (`domain/board.py`), or `None` for
    # "no board data". When set it takes precedence over `board_score_bonus` —
    # the dict is one float per holomem and cannot
    # express the branch scopes the board actually has (a red node applies to
    # the whole team only while that holomem LEADS, which no per-member dict
    # can say). Both are kept because the dict is the simple hook and this is
    # the model that fills it in.
    #
    # `None` by default, and deliberately NOT a `config.json` key: a board is
    # captured account state, like `data/owned.json`, not a modelling choice.
    board: object | None = None
    # How the team's ACTIVE score-up skills combine into one pool. NOT a
    # `config.json` key — see `scoring_models.ACTIVE_STACKING_MODELS`'s own
    # comment for why (the same reasoning as for `REFERENCE_RUN_SECONDS`). It lives on the config object because
    # `scoring.py` receives the config and nothing else, and because a test
    # comparing a refuted alternative needs one `dataclasses.replace` rather
    # than a monkeypatch.
    active_stacking_model: ActiveStackingModel = DEFAULT_ACTIVE_STACKING
    # How co-live ACTIVEs combine AT ONE INSTANT of a real run — read ONLY by
    # `song._ratio_at`, never by the run-average ranking scorer. `None` means
    # "use `active_stacking_model`", i.e. the same object through the same call,
    # so the default is identical to the run-average combine by construction.
    # Set it to `ACTIVE_INSTANT_MODELS["exclusive"]` to score the exclusive
    # co-firing hypothesis (`scoring_models.ExclusiveNormalizedActiveSkill`);
    # traces of real runs support the independent-roll default, so this is
    # opt-in and NOT a `config.json` key — the same reasoning
    # `active_stacking_model` gives.
    active_instant_model: ActiveStackingModel | None = None
    # Which deck power the PER-SONG modes score on. `True` (the default) is the
    # deck screen's own Overall Power (`song.overall_power_for`) — the quantity
    # the game actually multiplies, bit-exact against the server. `False` is the
    # modelled stat pool, which is a fine quantity for a COMPARISON (the absolute
    # scale cancels).
    #
    # Why `True` is the default: the report calls its figure an "expected run
    # score", and on the modelled pool that figure is about 4 % low. Over 150
    # legal teams on m0303 the server power is +3.4 % .. +5.2 % higher, and the
    # ORDER barely moves (Kendall tau 0.993, top-10 identical, same top-1). So
    # it buys a truthful absolute for no meaningful change of objective — unlike
    # `active_stacking_model`, whose alternatives change the ranking and
    # therefore stay opt-in.
    #
    # Cost: +8 % on a per-song run (`overall_power_for` 0.21 ms against
    # `score_song_detailed`'s 2.5 ms on m0303), documented at `_score_one`.
    song_absolute_deck_power: bool = True
    # Score the per-song modes for an AUTO run instead of manual play. Reaches
    # `score_song_detailed(auto=True)` through `song_rank._score_one`.
    #
    # ⚠ IT IS ONLY HALF THE PREMISE. A trace of a live run shows that on AUTO
    # the combo never accumulates, so a `SlotOverride` gated on `combo >= N`
    # NEVER fires — Luna ran a whole song
    # at her base 50 % where this config, at `run_state.combo = 100`, resolves
    # her to 100 %. Life-gated overrides DO fire (life is full from the first
    # second). So an AUTO ranking must ALSO set `run_state.combo = 0`, or it
    # keeps granting magnitudes the run cannot produce and over-values exactly
    # the cards that carry them. The CLI's `--auto` sets both and says so.
    song_auto: bool = False
    # Which ACTIVE-skill uptime a run averages. Two DIFFERENT references, both
    # measured, and the choice is NOT a free knob:
    #   "finite" — the finite-window count on the real cooldown lattice, with the
    #     start-of-song DEAD ZONE (the first activation check sits one whole
    #     cooldown in). This is the IN-RUN truth: `_finite_window_uptime` is
    #     validated to the note-weighted integral over the 704 shipped charts.
    #   "steady" — the steady-state ratio `min(1, duration/interval)`, no dead
    #     zone. This is what the DECK-SCREEN panel shows: the deck screen is
    #     song-independent, so it has no first-cooldown dead zone to discount.
    #     Switching the deck-screen Score-Bonus from "finite" to "steady" moves
    #     its residual over 71 saved decks from median -8.1% to +2.3%, and decks
    #     within +/-5% from 15/71 to 45/71 — the finite dead zone is the whole
    #     systematic bias there. `min(1, .)` is wrong in-run but is the
    #     deck-screen's own reference.
    # Default "finite" preserves every in-run/`total_score` number; the
    # deck-screen driver passes "steady".
    active_uptime_model: str = "finite"
    # Runtime-only CEILING toggle: when True, every ACTIVE skill is treated as
    # firing with probability 1 (all actives always trigger) — the "max possible
    # score if all actives fired" ceiling, above the probabilistic estimate. NOT a
    # config.json key; set at runtime via `replace(cfg, active_all_fire=True)`.
    active_all_fire: bool = False
    # The two account-level scalars the player supplies (posters collected and
    # the Member Power-Up permyriad). Deliberately NOT a config.json key, for
    # the same reason `board` is not: these are captured ACCOUNT state, not a
    # modelling choice. `None` is the default and leaves both terms out.
    account: object | None = None
    # Which slot-order rule scores the stat pool — one of `SLOT_ORDER_RULES`,
    # whose comment carries the evidence. Default "free".
    slot_order_rule: str = SLOT_ORDER_RULES[0]
    # Which client's PERFECT column the per-song scorer weighs notes with — one
    # of `PLATFORMS`. Only FLICK differs (1050 mobile / 1000 PC in the master's
    # `LiveNote` table); everything else, including the AUTO column and
    # the hold ticks, is identical. Charts carry a per-note flick flag and the
    # PC-column base permil, so the choice is made HERE at scoring time and never
    # baked into `charts.json`. NOT a config.json key: it is a runtime what-if
    # like `active_all_fire`, set via `replace(cfg, platform="pc")`.
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
        # .get for back-compat: a "model" block predating this key reads "finite".
        active_uptime_model=merged["model"].get("active_uptime", "finite"),
        target_selection_model=TARGET_SELECTION_MODELS[
            merged["target_selection"]["strategy"]
        ],
        target_under_supply=merged["target_selection"]["under_supply"],
        run_state=run_state_from_record(merged["assumed_run_state"]),
        provenance=provenance,
    )


def load_scoring_config(path: Path = DEFAULT_CONFIG_PATH) -> ScoringConfig:
    """Read `path` (default: repo-root `config.json`) and construct a
    `ScoringConfig`. `FileNotFoundError` is the one sanctioned try/catch in
    this codebase's `domain/` layer — an absent file falls back to
    `DEFAULT_CONFIG` in full, with every provenance entry recorded as
    `"default"`. When the file loads, it is merged over `DEFAULT_CONFIG` at
    top-level-key granularity: a key present in the file wins in full (no
    field-level merge inside a key) and is recorded `"file"`; a key absent
    from the file falls back to `DEFAULT_CONFIG[key]` and is recorded
    `"default"`.

    The merged document is then run through `validate_scoring_config`
    before construction: a malformed-but-present file raises `SchemaError`
    naming the offending field, rather than silently falling back to
    `DEFAULT_CONFIG` (which would let a defaulted value be reported with
    `"file"` provenance)."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
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


# Import-time guarantee: the shipped default cannot drift out of
# its own validation contract.
assert validate_scoring_config(DEFAULT_CONFIG) == [], (
    "DEFAULT_CONFIG must itself pass validate_scoring_config — a shipped "
    "default cannot be rejected by the same validator that gates a "
    "hand-edited file"
)
