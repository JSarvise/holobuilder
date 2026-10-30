"""The BIT-EXACT, per-member per-stat Memory Bonus.

Memory Bonus (a summand of Overall Power) rounds up ONCE PER
member AND PER stat — fifteen ceilings for a five-member deck, not one:

    memory = SUM over the 5 members, over {performance, technique, sense}
                 of ceil( memberParameter_stat * R / 1000 )

where `R` is the poster-collect rate (`memory_bonus_permil`) and each
`memberParameter_stat` is that member's leveled+bloomed base parameter for one
of the three stats — `StatBasis.stats_at`, which the ranking path carries as
`Card.stats`.

Bit-exact against the server: 2989/2989 captured deck rows in
`tools/recon/proxy/dumps/bonus_extract_db.jsonl`. The whole-deck
approximation `ceil(total * R / 1000)` hit only 24 of those.

These unit cases are HAND-COMPUTED — the arithmetic is written out beside the
assertion so the check does not compare the code to itself.
"""

import json
import math
from pathlib import Path

import pytest

from hbuilder.domain.collection import (
    memory_bonus,
    memory_bonus_per_stat,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = REPO_ROOT / "tools" / "recon" / "proxy" / "dumps" / "bonus_extract_db.jsonl"
CURVE_PATH = REPO_ROOT / "tools" / "recon" / "proxy" / "dumps" / "card_level_curve.json"
# ⚠ FROZEN, not live. The DB rows are the server's own decomposition of decks
# saved at ONE MOMENT, and the Memory Bonus is a pure function of
# `player.posters` — so the moment the account collects a poster, EVERY row
# stops matching (one extra poster takes this test from 6098/6098 bit-exact
# to 0/6098 with nothing in the model changing). Re-freeze the fixture only when the DB is re-captured.
COLLECTION_PATH = REPO_ROOT / "tests" / "fixtures" / "collection_2026-10-13_oracle_basis.json"


def test_unset_permil_stays_none_and_zero_is_a_real_zero():
    """`None` (player has not told us) and `0` (told us they have none) are
    different answers and both survive — the module's standing contract."""
    assert memory_bonus_per_stat([(100, 100, 100)] * 5, None) is None
    assert memory_bonus_per_stat([(100, 100, 100)] * 5, 0) == 0
    # An empty deck is a modelled zero, not an absence.
    assert memory_bonus_per_stat([], 52) == 0


def test_one_member_rounds_up_each_of_its_three_stats():
    """One member, three stats, at R=52. HAND-COMPUTED:
        ceil(100 * 52 / 1000) = ceil(5.2)  = 6
        ceil(200 * 52 / 1000) = ceil(10.4) = 11
        ceil(300 * 52 / 1000) = ceil(15.6) = 16
        total = 33
    """
    assert memory_bonus_per_stat([(100, 200, 300)], 52) == 6 + 11 + 16
    assert memory_bonus_per_stat([(100, 200, 300)], 52) == 33


def test_exact_division_does_not_round_up_a_whole_result():
    """Ceil, not "always add one": a stat that lands exactly on a 1/1000
    boundary contributes its exact quotient. 250 * 4 / 1000 = 1.000 -> 1."""
    assert memory_bonus_per_stat([(250, 0, 0)], 4) == 1
    # And the next unit up does round: 251 * 4 / 1000 = 1.004 -> 2.
    assert memory_bonus_per_stat([(251, 0, 0)], 4) == 2


def test_per_stat_differs_from_the_whole_deck_approximation():
    """The reason this function exists. Three stats each just over a /1000
    boundary pay three ceilings the whole-deck form collapses into (nearly) one.

    Deck of five members, every stat = 101, R = 10.
      per-stat: 5 * 3 * ceil(101 * 10 / 1000) = 15 * ceil(1.01) = 15 * 2 = 30
      whole-deck total = 5 * 3 * 101 = 1515;  ceil(1515 * 10 / 1000)
                       = ceil(15.15) = 16
    The 14-unit gap is exactly the fourteen fractional parts the single ceiling
    throws away — and it is always LOW, the sign of the whole-deck form.
    """
    per_member = [(101, 101, 101)] * 5
    exact = memory_bonus_per_stat(per_member, 10)
    approx = memory_bonus(sum(sum(m) for m in per_member), 10)

    assert exact == 30
    assert approx == 16
    assert exact > approx  # the approximation is always LOW


def test_at_the_owner_rate_the_gap_is_the_sum_of_dropped_fractions():
    """Synthetic member at a real account's rate R=52, chosen so every stat
    clears its integer boundary by a hair. HAND-COMPUTED per stat:
        ceil(3387 * 52 / 1000) = ceil(176.124) = 177
        ceil(3385 * 52 / 1000) = ceil(176.020) = 177
        ceil(3386 * 52 / 1000) = ceil(176.072) = 177
        total = 531
    The whole-deck form rounds the summed 10158 once:
        ceil(10158 * 52 / 1000) = ceil(528.216) = 529
    so it is two low — the two fractions the per-stat ceilings kept."""
    assert memory_bonus_per_stat([(3387, 3385, 3386)], 52) == 177 * 3 == 531
    assert memory_bonus(3387 + 3385 + 3386, 52) == 529


# --------------------------------------------------------------------------
# Data-backed: the model function against the server's own `power.memory`.
# --------------------------------------------------------------------------

_DUMPS_PRESENT = DB_PATH.exists() and CURVE_PATH.exists() and COLLECTION_PATH.exists()


@pytest.mark.skipif(
    not _DUMPS_PRESENT,
    reason="recon dumps (bonus_extract_db / card_level_curve / collection) absent",
)
def test_model_function_is_bit_exact_against_captured_server_rows():
    """The measurement, run through the shipped `memory_bonus_per_stat`.

    For each captured deck whose members are all in the account's collection, feed
    the model's own per-stat base parameters (`StatBasis.stats_at` at collection
    level/bloom — exactly what the ranking path carries as `Card.stats`) to the
    function and require its result to equal the server's `power.memory` on
    every row. Non-circular: the target is the wire figure, not the model's own code."""
    from hbuilder.domain.collection import stat_basis_from_mapping

    basis = stat_basis_from_mapping(
        json.loads(CURVE_PATH.read_text(encoding="utf-8"))
    )
    col = json.loads(COLLECTION_PATH.read_text(encoding="utf-8"))
    cards_col = col["cards"]
    t = min(col["player"]["posters"], 50)
    R = min(2 * t, t + 30)

    def per_stat(card_id):
        c = cards_col.get(card_id)
        if not c:
            return None
        return basis.stats_at(card_id, c["level"], c["bloom"])

    exact = approx = applicable = 0
    misses = []
    with DB_PATH.open(encoding="utf-8") as fh:
        for line in fh:
            row = json.loads(line)
            parts = [per_stat(m) for m in row["members"]]
            if any(p is None for p in parts):
                continue
            applicable += 1
            server = row["power"]["memory"]
            model = memory_bonus_per_stat(parts, R)
            whole = memory_bonus(sum(sum(p) for p in parts), R)
            if model == server:
                exact += 1
            else:
                misses.append((row.get("spec_hash"), server, model))
            if whole == server:
                approx += 1

    assert applicable >= 100, f"too few applicable rows ({applicable})"
    # The whole-deck approximation is the baseline: it is nearly always wrong,
    # and the pin proves the per-stat form is a real improvement.
    assert approx < applicable // 2
    assert exact == applicable, (
        f"per-stat Memory not bit-exact: {exact}/{applicable} "
        f"(approx {approx}/{applicable}); first misses: {misses[:5]}"
    )
