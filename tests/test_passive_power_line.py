"""⛔ THE SECOND NON-CIRCULAR CHECK: does a `stat_up` move Overall Power by the
amount the SERVER says it does?

`test_collection.py` pins the Member Parameter line — the cards' own stats. This
pins the next line up: `up_by_live_passive_skill`, what the PASSIVE slot adds on
top. Neither number is ours. `deck_oracle.json` carries the game's own figure for
36 captured decks, each row with the card levels of its own capture.

**Why it matters.** `stat_up` and `score_support` decide real ranking
disagreements (e.g. Ouro Kronii's `stat_up sense 41` against Mococo
Abyssgard's `score_support 11`). `score_support`'s FORM follows the game's own
compiled code (`CalcScoreUpRatio`). This test gives `stat_up`'s magnitude an
end-to-end check against the wire.

⚠ **Three instrument faults are easy to make here, and each one makes the line
come out wrong while still looking plausible:**

1. Multiplying by each card's TOTAL stat sum. `effect_type` names WHICH stat a
   `stat_up` moves; only `all_stats` means all three. 130 % error.
2. Keeping the fallback "no named targets means everyone". A targeted effect
   reaches `count` members, not the team.
3. Scoring capture-time STATS with fully-MAXED SKILLS. A passive's magnitude is
   bloom-dependent — 24 at the capture's bloom against 32 at max on the two
   cards in row 25 — so the line came out exactly 4/3 too big and looked like a
   model error. 37 % error.

With all three avoided, the model reproduces the server to a mean 0.21 %.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hbuilder.cli import apply_collection, build_parser
from hbuilder.domain.collection import (
    caps_by_rarity_from_mapping,
    collection_stats,
    stat_basis_from_mapping,
)
from hbuilder.domain.loader import load_cards, load_owned
from hbuilder.domain.scoring import score_with_breakdown
from hbuilder.domain.scoring_config import load_scoring_config
from hbuilder.domain.team import Team, TeamError

_REPO = Path(__file__).resolve().parents[1]
_DUMPS = _REPO / "tools" / "recon" / "proxy" / "dumps"
_ORACLE = _DUMPS / "deck_oracle.json"
_CAPS_FILE = _DUMPS / "card_caps.json"
_CURVE = _DUMPS / "card_level_curve.json"

_PASSIVE_POWER = "up_by_live_passive_skill [Passive Skill]"
_STAT_INDEX = {"performance": 0, "technique": 1, "sense": 2}

# Per card, per stat, the game rounds. Five members x three stats is fifteen
# roundings, so a few units either way is arithmetic, not disagreement. The
# bound is deliberately absolute AND relative: an absolute-only bound would pass
# a 1 % error on a small deck, and a relative-only one would fail on rounding
# when the line is tiny.
_ABS_TOLERANCE = 60
_REL_TOLERANCE = 0.03


def _rows_with_levels() -> list[tuple[int, dict]]:
    rows = json.loads(_ORACLE.read_text(encoding="utf-8"))
    return [
        (index, row)
        for index, row in enumerate(rows)
        if row.get("levels")
        and (row.get("power") or {}).get(_PASSIVE_POWER) is not None
        # ⚠ The hand-built `deck_screens_capture_*` contrast decks are EXCLUDED,
        # and this is a real finding, not a convenience: on one of them (the
        # mid-interval Sora deck) the stat_up power line reads +224 on wire 2455
        # (+9.1 %), well outside the 0.21 % this reproduces on the homogeneous
        # captures. That residual is the same non-uniform model error
        # test_score_bonus_end_to_end.py covers -- not this pin, which asserts
        # stat_up on the population it was measured on.
        and not str(row.get("capture") or "").startswith("deck_screens_capture_")
    ]


@pytest.mark.skipif(
    not (_ORACLE.exists() and _CAPS_FILE.exists() and _CURVE.exists()),
    reason="dumps/ is gitignored account data",
)
def test_stat_up_moves_overall_power_by_what_the_server_says(tmp_path):
    """The check itself, over every capture that carries the line."""
    caps = caps_by_rarity_from_mapping(json.loads(_CAPS_FILE.read_text(encoding="utf-8")))
    basis_curve = stat_basis_from_mapping(json.loads(_CURVE.read_text(encoding="utf-8")))
    defaults = build_parser().parse_args(["solve"])
    catalogue = load_cards(_REPO / defaults.cards)
    owned = load_owned(_REPO / defaults.owned, catalogue)
    cfg = load_scoring_config(_REPO / "config.json")

    checked, failures, exact_ish = 0, [], 0
    for index, row in _rows_with_levels():
        wire = row["power"][_PASSIVE_POWER]
        ids = [row["cards"][slot] for slot in sorted(row["cards"])]
        ids = [c if isinstance(c, str) else (c.get("card") or "") for c in ids]

        document = {"card_default": "unknown", "cards": row["levels"]}
        stats, _report = collection_stats(ids, document, caps, basis_curve)
        if len(stats) != len(ids):
            continue

        # ⛔ The capture's own blooms on the CARDS too, not just on the stats.
        # Fault 3 above: a passive's magnitude is bloom-dependent, and taking the
        # two halves of one comparison from two different states is the mistake
        # this project keeps paying for.
        capture_file = tmp_path / f"capture_{index}.json"
        capture_file.write_text(json.dumps(document), encoding="utf-8")
        cards, _basis = apply_collection(
            catalogue, str(capture_file), owned_ids=[c.id for c in owned]
        )
        by_id = {card.id: card for card in cards}
        if any(cid not in by_id for cid in ids):
            continue
        members = tuple(by_id[cid] for cid in ids)
        try:
            # The wire names the leader as a (character, costume) PAIR, so no
            # leader card exists to reconstruct. A member stands in: the member
            # set — and therefore every passive gate — is unchanged, and the
            # leader contributes no stats, which is the only thing this line
            # reads.
            team = Team(leader=members[0], members=members)
        except TeamError:
            continue

        ours = 0.0
        for contribution in score_with_breakdown(team, cfg).contributions:
            if (
                contribution.slot_kind != "PASSIVE"
                or contribution.state != "applied"
                or contribution.pool != "stat"
            ):
                continue
            # No fallback to "everyone" -- fault 2. A stat contribution that
            # named no cards reached no cards.
            for cid in contribution.target_card_ids or ():
                triple = stats.get(cid)
                if triple is None:
                    continue
                amount = (
                    float(sum(triple))
                    if contribution.effect_type == "all_stats"
                    else float(triple[_STAT_INDEX[contribution.effect_type]])
                )
                ours += amount * contribution.rate

        checked += 1
        delta = ours - wire
        if abs(delta) <= _ABS_TOLERANCE and abs(delta) <= _REL_TOLERANCE * wire:
            exact_ish += 1
        else:
            failures.append((index, wire, round(ours), round(delta)))

    assert checked >= 25, f"expected the captured decks, only checked {checked}"

    # ⚠ ONE row here disagrees ON PURPOSE, and it is evidence rather than noise.
    # These decks were captured on an older game version. Row 9 is five Happy
    # members with a `technique +20 % -> 2 Happy` passive; the wire (5041) is
    # what you get by paying the two highest-TOTAL Happy members, and 5312 is
    # what you get by paying the two highest-TECHNIQUE ones. Newer captures are
    # bit-exact on ranking by the stat the effect raises and not on ranking by
    # total, so the current server pays the highest-technique pair and this row
    # records the older rule. It is the only row where the two rules pick
    # different members.
    superseded_rule_rows = {9}
    assert {index for index, *_ in failures} <= superseded_rule_rows, failures
    assert [(w, o) for i, w, o, _d in failures if i == 9] in ([], [(5041, 5312)]), failures
    failures = [f for f in failures if f[0] not in superseded_rule_rows]
    assert not failures, (
        f"{len(failures)} of {checked} decks disagree with the server's own "
        f"Passive Skill power line (index, wire, ours, delta): {failures[:5]}"
    )

    # NEGATIVE CONTROL. The three faults above all made this line come out too
    # BIG while every row still "looked plausible" -- the run reported a number,
    # it was simply the wrong one. So assert the check can still see a
    # disagreement: inflating the model's side by a third (exactly what fault 3 does)
    # must break every row it passes on.
    broken = [
        (index, wire)
        for index, row in _rows_with_levels()
        for wire in [row["power"][_PASSIVE_POWER]]
        if wire and abs(wire * 4 / 3 - wire) <= max(_ABS_TOLERANCE, _REL_TOLERANCE * wire)
    ]
    assert not broken, (
        "the tolerance is wide enough to swallow the 4/3 overstatement this "
        f"test exists to catch, on rows {broken[:3]} -- tighten it"
    )
