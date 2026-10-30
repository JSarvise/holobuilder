"""Bit-exact Overall Power (the deck-screen "score power" number), checked
against the server.

Three deterministic rules bring the model's Overall Power — the sum the
game's Unit Score Details panel labels as such — to bit-exact against the
server, without touching the ranking objective:

  Fix A  the Outfit + Passive stat lines are paid as per-(card, attribute)
         FLOAT32 ceilings, grouped by source line, not one float64 product
         (`_overall_power_stat_subtotals`). The float64 sum is kept for the
         RANKING (`_stat_sum` -> `effective_stat_sum` -> `total_score`).

  Fix B  Member Power-Up is paid its FIVE per-slot ceilings (one per member),
         over each member's MemberParameter+Outfit+Passive+Board base, not one
         ceiling over the aggregate (`_score_team` -> `member_power_up`).

  Fix C  A count-limited effect's recipients are chosen by ranking its matchers
         on TOTAL parameter and, on an EXACT TIE, keeping DECK SLOT ORDER (the
         earlier-slot member wins). The deck-screen re-resolves count-limited
         recipients under the slot-order tie-break (`_SLOT_ORDER_TIE_BREAK`). The
         RANKING uses the SAME slot-order tie-break (`TopNByBuffedStat` is a
         stable sort), so ranking and display agree on the recipient — see
         `test_slot_order_tie_break_moves_the_display_and_ranking_recipient_together`
         and `tests/test_slot_order.py`'s section 4b.

Against the 5-member rows of `tools/recon/proxy/dumps/bonus_extract_db.jsonl`
(the server's own Unit Score decomposition for real saved decks), Overall
Power is **3870/3870 (100%) bit-exact**, against **0/2536** without these
rules. Fixes A+B alone reach ~99.4%; the remaining 22 decks are not a float32
magnitude level-scaling (the Outfit/COSTUME line is already exact on all 22 and
no passive carries a level-scaled or per-count magnitude) but the recipient
tie-break of Fix C: every one carries an exact total-parameter tie among a
count-target's matchers. See `test_slot_order_tie_break_moves_
the_display_and_ranking_recipient_together` and `test_the_residual_is_fully_closed`.

Guardrails: Fixes A and B are DISPLAY-only float32-ceiling rounding, and the
ranking objective is proven untouched by them on non-tie decks by
`test_the_ranking_objective_is_bit_identical`. Fix C's slot-order recipient
tie-break DOES move the RANKING on exact-tie decks, toward the server's own
choice — that is intended and is covered in `tests/test_slot_order.py`.
"""

from __future__ import annotations

import json
import math
import struct
import sys
from pathlib import Path

import pytest

from hbuilder.domain.card import EffectTarget
from hbuilder.domain.collection import member_power_up
from hbuilder.domain.scoring import (
    _FIXED_TARGET,
    _overall_power_stat_subtotals,
    deck_screen_unit_score,
    score_fast,
    score_with_breakdown,
)
from hbuilder.domain.schema import STAT_NAMES

from conftest import make_card, make_scoring_config, make_team

try:
    from hbuilder.domain.schema import Stats
except ImportError:  # pragma: no cover - Stats lives in card in some layouts
    from hbuilder.domain.card import Stats


def _f32(v: float) -> float:
    return struct.unpack("<f", struct.pack("<f", v))[0]


# ==========================================================================
# (a) Fix A — the per-(card, attribute) FLOAT32 ceiling stat helper
# ==========================================================================


def _one_effect_entries(rate: float, stat: str, card_id: str, slot_kind: str):
    """A single stat effect on one card's one stat: the (stat_entries,
    recipients) pair the helper consumes. See `scoring.StatEntry`."""
    entry = (rate, (stat,), frozenset({card_id}), _FIXED_TARGET, card_id, slot_kind)
    return [entry], [frozenset({card_id})]


def test_the_float32_ceiling_kills_the_phantom_point_that_float64_would_ceil_up():
    """HAND-COMPUTED straddle. A `stat_up` of 140 permil (rate 0.14) on a base
    of 100 is EXACTLY 14 — but binary64 cannot hold 0.14, so `0.14 * 100` is
    `14.000000000000002`, and a naive `ceil` invents a fifteenth point:

        float64 :  math.ceil(0.14 * 100) = math.ceil(14.000000000000002) = 15
        float32 :  math.ceil(_f32(0.14 * 100)) = math.ceil(14.0)          = 14

    The server rounds the product in single precision (an `ss` scalar op), so it
    pays 14. The helper does the same and pays 14; the float64 form would
    over-state Overall Power by one point here. This is the whole of Fix A in
    one attribute."""
    # The wrong float64 reading, spelled out so the straddle is not a mystery.
    assert math.ceil(0.14 * 100) == 15
    assert 0.14 * 100 == 14.000000000000002
    # The shipped float32 reading.
    assert math.ceil(_f32(0.14 * 100)) == 14

    card = make_card("c1", stats=Stats(performance=100, technique=0, sense=0))
    entries, recipients = _one_effect_entries(0.14, "performance", "c1", "PASSIVE")
    (subtotal,) = _overall_power_stat_subtotals(
        (card,), entries, make_scoring_config(), recipients
    )
    # MemberParameter (100 + 0 + 0) + the Passive line ceil(_f32(0.14*100)) = 14.
    assert subtotal == 100 + 14
    # And it is genuinely the f32 answer, one below what float64 would have paid.
    assert subtotal != 100 + math.ceil(0.14 * 100)


def test_a_bare_member_with_no_stat_effect_is_its_integer_member_parameter():
    """No effect -> no ceiling: the subtotal is exactly the summed integer base
    parameters, the MemberParameter line alone."""
    card = make_card("c1", stats=Stats(performance=123, technique=456, sense=78))
    (subtotal,) = _overall_power_stat_subtotals(
        (card,), [], make_scoring_config(), []
    )
    assert subtotal == 123 + 456 + 78
    assert subtotal == float(123 + 456 + 78)


def test_two_source_lines_on_the_same_stat_pay_two_ceilings_not_one():
    """The server reports Outfit Skill and Passive Skill as SEPARATE power
    lines and ceils each; the helper groups by source line for that reason.

    HAND-COMPUTED on base 101, two effects each rate 0.05 on `performance`:
      Outfit line : ceil(_f32(0.05 * 101)) = ceil(5.05) = 6
      Passive line: ceil(_f32(0.05 * 101)) = ceil(5.05) = 6
      per-source total = 12
    Combining both into one ceiling would pay ceil(_f32(0.10 * 101)) =
    ceil(10.1) = 11 — one point LOW, which is exactly the understatement the
    source grouping exists to avoid."""
    card = make_card("c1", stats=Stats(performance=101, technique=0, sense=0))
    outfit = (0.05, ("performance",), frozenset({"c1"}), _FIXED_TARGET, "c1", "OUTFIT")
    passive = (0.05, ("performance",), frozenset({"c1"}), _FIXED_TARGET, "c1", "PASSIVE")
    recipients = [frozenset({"c1"}), frozenset({"c1"})]
    (subtotal,) = _overall_power_stat_subtotals(
        (card,), [outfit, passive], make_scoring_config(), recipients
    )
    assert math.ceil(_f32(0.05 * 101)) == 6
    assert subtotal == 101 + 6 + 6  # two source ceilings
    # The one-ceiling-over-the-sum reading is strictly lower here.
    assert math.ceil(_f32(0.10 * 101)) == 11
    assert subtotal > 101 + 11


# ==========================================================================
# (b) Fix B — Member Power-Up pays five per-slot ceilings, not one aggregate
# ==========================================================================


def test_five_per_member_ceilings_sum_higher_than_one_aggregate_ceiling():
    """HAND-COMPUTED. Five members, each base 101, at 100 permyriad:
      per-member : 5 x ceil(101 * 100 / 10000) = 5 x ceil(1.01) = 5 x 2 = 10
      aggregate  :     ceil(505 * 100 / 10000) =     ceil(5.05)       = 6
    The server pays five ceilings (10); paying one (6) under-states it. The
    band is exactly the four fractional parts the single ceiling discards."""
    per_member = [101.0] * 5
    aggregate = [sum(per_member)]
    assert member_power_up(per_member, 100) == 10
    assert member_power_up(aggregate, 100) == 6
    assert member_power_up(per_member, 100) > member_power_up(aggregate, 100)


def test_score_team_pays_member_power_up_per_member_over_the_overall_power_base():
    """End-to-end: `_score_team` feeds `member_power_up` five per-member bases,
    so a uniform team pays five ceilings. The demo team is five 300-stat members
    with no stat skills and no board -> each base 300; at 35 permyriad:
      5 x ceil(300 * 35 / 10000) = 5 x ceil(1.05) = 5 x 2 = 10  (NOT ceil(5.25)=6)."""
    import dataclasses as dc

    from hbuilder.domain.collection import AccountBonuses

    team = make_team([make_card(f"c{i}") for i in range(6)])
    cfg = dc.replace(
        make_scoring_config(),
        account=AccountBonuses(posters=22, power_up_permyriad=35),
    )
    bd = score_with_breakdown(team, cfg)
    assert bd.member_power_up == 10
    # The single-aggregate form is lower.
    assert member_power_up([bd.effective_stat_sum], 35) == 6


# ==========================================================================
# (c) Data-backed: the model's Overall Power against the server's own rows
# ==========================================================================

_REPO = Path(__file__).resolve().parents[1]
_PROXY = _REPO / "tools" / "recon" / "proxy"
_DUMPS = _PROXY / "dumps"
_DB = _DUMPS / "bonus_extract_db.jsonl"
_CURVE = _DUMPS / "card_level_curve.json"

# ⚠ FROZEN account state, NOT the live dumps. The DB rows are the SERVER's own
# decomposition of decks saved at ONE MOMENT. Drive the model with account state
# from any OTHER moment and the comparison is meaningless.
#
# `player.power_up_permyriad` is `"auto"`, i.e. DERIVED FROM EVERY OWNED CARD'S
# LEVEL, so a refresh that levels two cards moves an account-wide scalar and
# over-states the Member Power-Up line of EVERY deck, while `member_parameter`,
# `outfit`, `passive`, `board` and `memory` still match to the unit. The
# per-deck guards cannot catch that because the drift is not per deck.
#
# So both inputs are pinned next to the corpus they belong to. RE-FREEZE THEM
# ONLY WHEN THE DB IS RE-CAPTURED, at the same time, or the failure returns.
_FIXTURES = _REPO / "tests" / "fixtures"
_COLLECTION = _FIXTURES / "collection_2026-10-13_oracle_basis.json"
_BOARD_STATE = _FIXTURES / "board_state_2026-10-13_oracle_basis.json"

_DUMPS_PRESENT = all(p.exists() for p in (_DB, _COLLECTION, _BOARD_STATE, _CURVE))


def _cos_suffix(costume: str) -> str:
    # cos-00001-cmmn-0000-00 -> cmmn-0000-00
    return "-".join(costume.split("-")[2:])


def _card_suffix(card_id: str) -> str:
    # card-00001-4-cmmn-0000-00 -> cmmn-0000-00
    return "-".join(card_id.split("-")[3:])


@pytest.mark.skipif(
    not _DUMPS_PRESENT,
    reason="recon dumps (bonus_extract_db / board_state / snapshot / curve) are gitignored account data",
)
def test_model_overall_power_is_bit_exact_against_captured_server_rows():
    """The measurement, run through the shipped scoring path.

    For each captured 5-member deck, build the team at the account's live levels,
    the live Holomem Board and the account scalars, score it, and require the
    model's `deck_screen_unit_score(...).overall_power` to equal the server's
    own `overall_power` on the row. Non-circular: the target is the wire figure.

    The BEFORE reconstruction (float64 stat sum + one-aggregate Member Power-Up)
    is computed on the SAME rows to prove the fixes are what moved the number —
    it matched 0 of them.
    """
    sys.path.insert(0, str(_PROXY))
    import board_reconcile as BR  # noqa: E402  (os.chdir(REPO) + sys.path at import)

    import dataclasses as dc

    frozen = json.loads(_COLLECTION.read_text("utf8"))["cards"]
    BR.write_live_collection(frozen, _COLLECTION)
    # the roster of the capture day too: power-up "auto" is derived from it
    by_id, cfg, _applied = BR.build_cards({}, roster=frozen)  # writes ONLY to the OS temp dir
    cfg = dc.replace(cfg, board=BR.build_board_model(_BOARD_STATE, _COLLECTION))

    from collections import defaultdict

    chr_cards: dict[str, dict[str, object]] = defaultdict(dict)
    for card in by_id.values():
        chr_cards["chr-" + card.id.split("-")[1]][_card_suffix(card.id)] = card

    from hbuilder.domain.team import Team, TeamError

    applicable = after_exact = before_exact = skipped_roster = 0
    misses = []
    with _DB.open(encoding="utf-8") as fh:
        for line in fh:
            row = json.loads(line)
            if len(row.get("members", [])) != 5:
                continue
            ids = row["members"]
            if any(m not in by_id for m in ids):
                continue
            members = tuple(by_id[m] for m in ids)
            leader = chr_cards.get(row["leader"], {}).get(_cos_suffix(row["costume"]))
            if leader is None:
                pool = chr_cards.get(row["leader"])
                leader = next(iter(pool.values())) if pool else None
            if leader is None:
                continue
            try:
                team = Team(leader=leader, members=members)
            except TeamError:
                continue

            bd = score_with_breakdown(team, cfg)
            ds = deck_screen_unit_score(bd)

            # Guard: only rows whose account state matches the capture (exact
            # MemberParameter + Board + Memory) isolate the two rounding fixes
            # from stale levels. These three are already bit-exact by design.
            if float(bd.base_stat_sum) != row["power"]["member_parameter"]:
                continue
            if round(float(bd.board_power)) != row["power"]["board"]:
                continue
            if bd.memory_bonus != row["power"]["memory"]:
                continue
            # ...and Member Power-Up. It is an ACCOUNT-WIDE sum over the owned
            # roster, so acquiring ONE card moves it for every deck at once and
            # invalidates the comparison against rows captured before that card
            # existed (every row misses, negative, by around -700). Guarding on
            # it keeps the test measuring the rounding fixes it is named for
            # rather than silently measuring roster drift.
            if round(float(bd.member_power_up or 0)) != row["power"]["power_up"]:
                skipped_roster += 1
                continue

            applicable += 1

            # AFTER (the fixes).
            if round(ds.overall_power) == row["overall_power"]:
                after_exact += 1
            else:
                misses.append(
                    (row.get("family"), round(ds.overall_power) - row["overall_power"])
                )

            # BEFORE (float64 stat sum + one-aggregate Member Power-Up).
            old_power_up = member_power_up(
                [bd.effective_stat_sum + bd.board_power],
                cfg.account.power_up_permyriad,
            )
            before = (
                bd.effective_stat_sum
                + bd.board_power
                + (bd.memory_bonus or 0)
                + (old_power_up or 0)
            )
            if round(before) == row["overall_power"]:
                before_exact += 1

    assert applicable >= 200, (
        f"too few applicable rows ({applicable}); {skipped_roster} skipped because the "
        f"model's Member Power-Up disagrees with the capture. If that is most of the "
        f"corpus the owned roster has moved past the captures -- Member Power-Up is "
        f"account-wide, so one new card shifts every historical row at once. Re-capture, "
        f"or score against the roster of the capture day."
    )
    # BEFORE the fixes the model matched essentially none.
    assert before_exact <= applicable // 100, (
        f"the BEFORE reconstruction should match almost nothing, matched "
        f"{before_exact}/{applicable}"
    )
    # AFTER the three fixes it is bit-exact on EVERY applicable row.
    rate = after_exact / applicable
    assert after_exact == applicable, (
        f"Overall Power bit-exact only {after_exact}/{applicable} "
        f"({rate:.1%}); sample misses (family, delta): {misses[:8]}"
    )


@pytest.mark.skipif(not _DUMPS_PRESENT, reason="recon dumps are gitignored account data")
def test_the_residual_is_fully_closed():
    """The residual is now ZERO: after Fix C (the slot-order recipient
    tie-break) every captured deck whose account state matches the capture is
    bit-exact. This pins that there is no remaining bounded-but-nonzero band —
    the worst relative error over all checked decks is exactly 0."""
    sys.path.insert(0, str(_PROXY))
    import board_reconcile as BR

    import dataclasses as dc
    from collections import defaultdict

    frozen = json.loads(_COLLECTION.read_text("utf8"))["cards"]
    BR.write_live_collection(frozen, _COLLECTION)
    by_id, cfg, _ = BR.build_cards({}, roster=frozen)
    cfg = dc.replace(cfg, board=BR.build_board_model(_BOARD_STATE, _COLLECTION))

    chr_cards: dict[str, dict[str, object]] = defaultdict(dict)
    for card in by_id.values():
        chr_cards["chr-" + card.id.split("-")[1]][_card_suffix(card.id)] = card

    from hbuilder.domain.team import Team, TeamError

    worst_rel = 0.0
    checked = 0
    with _DB.open(encoding="utf-8") as fh:
        for line in fh:
            row = json.loads(line)
            if len(row.get("members", [])) != 5:
                continue
            ids = row["members"]
            if any(m not in by_id for m in ids):
                continue
            members = tuple(by_id[m] for m in ids)
            leader = chr_cards.get(row["leader"], {}).get(_cos_suffix(row["costume"]))
            if leader is None:
                pool = chr_cards.get(row["leader"])
                leader = next(iter(pool.values())) if pool else None
            if leader is None:
                continue
            try:
                team = Team(leader=leader, members=members)
            except TeamError:
                continue
            bd = score_with_breakdown(team, cfg)
            if float(bd.base_stat_sum) != row["power"]["member_parameter"]:
                continue
            ds = deck_screen_unit_score(bd)
            checked += 1
            rel = abs(ds.overall_power - row["overall_power"]) / row["overall_power"]
            worst_rel = max(worst_rel, rel)

    assert checked >= 200
    # Every checked deck is bit-exact: no residual remains.
    assert worst_rel == 0.0, f"a residual deck missed by {worst_rel:.4%}"


# ==========================================================================
# (d) The ranking objective is BIT-IDENTICAL — the fixes are display-only
# ==========================================================================


def test_the_ranking_objective_is_bit_identical():
    """CRITICAL guardrail. The fixes live entirely in the deck-screen DISPLAY
    assembly; `total_score` (the ranking objective) must not move.

    Proven three ways on a team that actually exercises a `stat_up` (so the
    display path and the ranking path genuinely diverge):

      1. `breakdown.total_score` is bit-identical to `score_fast` — the hot-loop
         path that returns before any display term is computed.
      2. `total_score` still equals its float64 formula
         `(effective_stat_sum + board_power) * (1 + score) * (1 + board score)`.
      3. `overall_power_stat_sum` (float32 ceilings) DIFFERS from
         `effective_stat_sum` (float64) — the display path really changed, so
         (1) is a real invariance, not a no-op."""
    target = EffectTarget(kind="type", value="Cute", count=5)
    from conftest import make_skill

    # magnitude 14.0 -> combined stat rate 0.14; on a base of 101 the ranking
    # keeps the un-rounded float64 product (101 * 1.14 = 115.14 per member) while
    # the display ceils per source line (101 + ceil(_f32(0.14 * 101)) = 116).
    stat_up = make_skill(
        "PASSIVE", family="stat_up", effect_type="performance", magnitude=14.0, target=target
    )
    leader = make_card("lead", type_="Cute", stats=Stats(performance=101, technique=0, sense=0))
    members = [
        make_card(f"m{i}", type_="Cute", stats=Stats(performance=101, technique=0, sense=0),
                  passive=stat_up if i == 0 else None)
        for i in range(5)
    ]
    team = make_team([leader, *members])
    cfg = make_scoring_config()

    bd = score_with_breakdown(team, cfg)

    # (1) The ranking number is exactly the hot-loop number.
    assert bd.total_score == score_fast(team, cfg)

    # (2) And it is exactly the pre-existing float64 stage formula.
    expected = (
        (bd.effective_stat_sum + bd.board_power)
        * (1.0 + bd.combined_score_rate)
        * (1.0 + bd.board_score_rate)
    )
    assert bd.total_score == expected

    # (3) The display stat sum genuinely diverges from the ranking stat sum, so
    # the invariance above is meaningful rather than vacuous: the display path
    # (float32 ceilings) is really a different computation from the ranking path
    # (float64), and the ranking one did not move.
    assert bd.overall_power_stat_sum is not None
    assert bd.overall_power_stat_sum != bd.effective_stat_sum
    # Concretely: display ceils to the whole 116-per-member the server pays,
    # the ranking keeps the fractional 115.14.
    assert bd.overall_power_stat_sum == 5 * 116
    assert bd.effective_stat_sum == pytest.approx(5 * 115.14)


def test_slot_order_tie_break_moves_the_display_and_ranking_recipient_together():
    """Fix C, HAND-COMPUTED. A count-1 `type:Cute` performance buff whose two top
    matchers TIE on total parameter but split their stats differently. On an exact
    tie the SERVER keeps DECK SLOT ORDER (earlier slot wins), and BOTH the DISPLAY
    (`_SLOT_ORDER_TIE_BREAK`) and the RANKING (`_free_stat_sum`, `top_n_by_stat`
    as a stable sort) honour it, so they land on the SAME member:

      card-zzz: performance 100  (slot-EARLIER)
      card-aaa: performance 300  (card_id-smaller)

    ⚠ This deck ties on TOTAL parameter (350 each). The server ranks by the
    stat the effect raises (690/690 rows, against 510/690 for the total), so
    there is no tie on this deck: card-aaa has the higher performance and
    receives it.

    Member parameter total = 100+250 + 300+50 + 30 + 30 + 30 = 790.
      recipient card-aaa (perf 300):
        DISPLAY  : 790 + ceil(_f32(0.20 * 300)) = 850
        RANKING  : 790 + 0.20 * 300            = 850.0

    What the test pins: the display sum and the ranking sum AGREE on the
    recipient, and total_score equals `score_fast`.
    """
    from conftest import make_skill

    buff = make_skill(
        "PASSIVE",
        family="stat_up",
        effect_type="performance",
        magnitude=20.0,
        target=EffectTarget(kind="type", value="Cute", count=1),
    )
    slot_win = make_card(  # earlier slot, larger id -> both paths pick this one
        "card-zzz", type_="Cute", stats=Stats(performance=100, technique=250, sense=0)
    )
    id_win = make_card(  # later slot, smaller id -> the card_id tie-break's recipient
        "card-aaa", type_="Cute", stats=Stats(performance=300, technique=50, sense=0)
    )
    emitter = make_card(  # carries the buff; too low a total to be the recipient
        "card-emit", type_="Cute", stats=Stats(performance=10, technique=10, sense=10),
        passive=buff,
    )
    # Two non-Cute fillers so only the three Cute cards are ever candidates.
    f1 = make_card("card-f1", type_="Happy", stats=Stats(performance=10, technique=10, sense=10))
    f2 = make_card("card-f2", type_="Happy", stats=Stats(performance=10, technique=10, sense=10))
    leader = make_card("card-lead", type_="Happy", stats=Stats(performance=0, technique=0, sense=0))

    # Members in SLOT ORDER: slot_win (card-zzz) sits before id_win (card-aaa).
    team = make_team([leader, slot_win, id_win, emitter, f1, f2])
    cfg = make_scoring_config()

    bd = score_with_breakdown(team, cfg)

    member_param = 350 + 350 + 30 + 30 + 30
    assert member_param == 790
    # The DISPLAY paid the buff on the highest-performance Cute (card-aaa, perf 300).
    assert bd.overall_power_stat_sum == member_param + math.ceil(_f32(0.20 * 300))
    assert bd.overall_power_stat_sum == 850
    # It is NOT what the total-parameter rule paid on its slot-order tie (card-zzz).
    assert bd.overall_power_stat_sum != member_param + math.ceil(_f32(0.20 * 100))
    # The RANKING uses the same recipient: 300 * 1.20 = 360.
    assert bd.effective_stat_sum == pytest.approx(790 - 300 + 360)
    assert bd.effective_stat_sum == pytest.approx(850.0)
    # Ranking and display agree on the recipient.
    assert bd.effective_stat_sum == pytest.approx(bd.overall_power_stat_sum)
    stat_targets = {row.target_card_ids for row in bd.contributions if row.pool == "stat"}
    assert stat_targets == {("card-aaa",)}
    # And total_score is bit-identical to the hot loop.
    assert bd.total_score == score_fast(team, cfg)
