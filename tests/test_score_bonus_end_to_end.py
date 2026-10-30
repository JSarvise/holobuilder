"""End-to-end check of the Score Bonus: does the model PRODUCE the game's
`score_bonus_permil` from the cards?

`tests/test_passive_power_line.py` covers the `stat_up` half (the model
reproduces `up_by_live_passive_skill`). This file is the `score_support` half:
our `combined_score_rate`, computed from the cards at their captured levels,
against the deck screen's own `score_bonus_permil`.
`test_deck_screen_unit_score.py` takes `power` and `permil` FROM THE WIRE to
verify `K` and the ceiling rule; it does not ask whether the model produces
that `permil`, which is what this file checks.

**Rows used.** Only captures whose Score Bonus is skills only — no Holomem
Board line, no Outfit line — so neither the account's board nor the leader
outfit enters.

**ACTIVE uptime basis.** The deck screen counts ACTIVE uptime at steady state
(`min(1, duration/interval)`, no start-of-song dead zone). With
`config.json` shipping `model.active_uptime = "steady"`, the homogeneous
skills-only rows agree with the wire within a few percent, and
`test_our_score_bonus_agrees_with_the_games_own` asserts that.

**The panel carries occupancy, not nominal magnitude.** Reconstructing the
bonus at full magnitude (occ=1) overshoots the wire by +90..+100 %, and a
duty-cycle-only reading (P=1) by +43..+47 %; only the expected-occupancy model
lands near it. A wire SPECIAL line of 150 permil against raw SPECIAL magnitudes
of 1000-1400 is a ~9x occupancy discount a nominal panel could not produce.

**The residual per-deck spread is cooldown-lattice synchronisation.** On decks
built to have sharply different ACTIVE intervals, the residual (ours - wire)
changes sign, so no single scale or reference window can fix it. The spread
lives in the ACTIVE `E[max]` channel, not in `B`, occupancy magnitude, or
overrides. `ExpectedMaximumActiveSkill` assumes the ACTIVE windows are
INDEPENDENT, so its discount `E[max]/SUM(occ*mag)` is a near-constant ~0.75;
the deck screen computes `E[max]` on ONE fixed cooldown lattice (checks at
k*interval from measure 0), where actives with COINCIDENT intervals share live
windows and only the largest is paid — e.g. three actives at interval 29 s read
overstated, five distinct intervals read understated. Rebuilding `E[max]` on
the real lattice removes the sign-changing outliers.

**Why the scorer does not adopt the lattice.** `combined_score_rate` is the
CORPUS-AVERAGE `E[max]` (independence), which is unbiased across the chart
corpus because synchronisation averages out over songs of differing length.
The deck screen is a SINGLE-LATTICE quantity where it does not. Matching the
deck screen's non-uniformity would mean adopting a lattice `E[max]` that
degrades the multi-song ranking the model is for — an instrument-vs-objective
mismatch, not a bug. A lattice `E[max]` belongs in a separate per-song entry
point, not the ranking.

**Effect on rankings.** Score support enters `total_score` as one factor
`(1 + score_rate)`; a near-uniform error in that factor cannot reorder teams,
and the residual differential can only flip teams already within a couple of
percent of each other.

If a model change moves the gap OUT of the band, in either direction, that is
the signal to look, not to re-fit. `deck_screen_unit_score`'s docstring claims
to reproduce the deck panel; it is right in structure, and this file is what
holds its bonus term to the wire.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hbuilder.cli import apply_collection, build_parser
from hbuilder.domain.loader import load_cards, load_owned
from hbuilder.domain.scoring import score_with_breakdown
from hbuilder.domain.scoring_config import load_scoring_config
from hbuilder.domain.team import Team, TeamError

_REPO = Path(__file__).resolve().parents[1]
_ORACLE = _REPO / "tools" / "recon" / "proxy" / "dumps" / "deck_oracle.json"

_BONUS_KEYS = {
    "A": "by_live_active_skill [Active Skill]",
    "S": "by_live_special_skill [Special Skill]",
    "L": "by_live_leader_skill [Outfit Skill]",
    "P": "by_live_passive_skill [Passive Skill]",
    "B": "by_skill_tree [Holomem Board]",
}

# The agreement band, derived by a fixed rule so a model change cannot quietly
# re-fit it.
#
#   RULE. Take the measured gap over the non-contrast skills-only rows. The
#   bounds are the extremes rounded OUTWARD to the next whole percent, plus one
#   further percentage point on the LOW side (the side that carries the known
#   one-signed reference-window / occupancy understatement, so level drift in a
#   capture cannot flip the test).
#
#   Current rows:
#     rows 0/8/11/14/27/33/36  wire 563  ours 566.2   +0.56 %
#     row  9                   wire 723  ours 735.1   +1.67 %
#     row 10                   wire 742  ours 688.5   -7.20 %
#   -> -7.20 -> -8 -> -9 % and +1.67 -> +2 %.
#
# Rows 9 and 10 are the only two skills-only decks carrying a count-targeted
# `{Happy, 2}` member PASSIVE support (Choco 0011 / Polka 0027). With per-recipient
# targeting, row 10's PASSIVE contribution lands on the wire's own per-line figure
# (10 permil wire, 12.9 ours); what is left on row 10 is the ACTIVE/SPECIAL
# residual (675.7 vs 732 non-passive, -7.7 %), the same one-signed understatement
# the LOW side of the band allows for.
_GAP_LOW, _GAP_HIGH = -0.09, 0.02


# Interval-contrast captures. Their decks are built ON PURPOSE to have sharply
# different ACTIVE intervals, and they are excluded from the uniform band below
# because they straddle the wire in OPPOSITE directions, which a single-scale
# band cannot describe. They get their own, stronger assertion instead.
# Any `deck_screens_capture_*` file is such a hand-built contrast deck. They are
# excluded from the band by PREFIX (not one hardcoded name) and fed to the
# non-uniformity test, so each new contrast capture adds evidence without
# breaking a band it was never part of.
_CONTRAST_PREFIX = "deck_screens_capture_"


def _is_contrast(row) -> bool:
    return str(row.get("capture") or "").startswith(_CONTRAST_PREFIX)


def _skills_only_rows(include_contrast: bool = False) -> list[tuple[int, dict]]:
    rows = json.loads(_ORACLE.read_text(encoding="utf-8"))
    out = []
    for index, row in enumerate(rows):
        bonus = row.get("bonus") or {}
        shape = {k for k, v in _BONUS_KEYS.items() if v in bonus}
        if shape & {"B", "L"}:
            continue
        if not include_contrast and _is_contrast(row):
            continue
        if row.get("score_bonus_permil") and row.get("levels"):
            out.append((index, row))
    return out


def _our_bonus_permil(row, catalogue, owned, cfg, tmp_path):
    """Our `combined_score_rate` in permil for one oracle row, at its capture-time
    levels, or None if the row cannot be scored."""
    ids = [row["cards"][slot] for slot in sorted(row["cards"])]
    ids = [c if isinstance(c, str) else (c.get("card") or "") for c in ids]
    capture = tmp_path / f"capture_{row.get('score_bonus_permil')}_{len(ids)}.json"
    capture.write_text(
        json.dumps({"card_default": "unknown", "cards": row["levels"]}),
        encoding="utf-8",
    )
    cards, _basis = apply_collection(
        catalogue, str(capture), owned_ids=[c.id for c in owned]
    )
    by_id = {card.id: card for card in cards}
    if any(cid not in by_id for cid in ids):
        return None
    members = tuple(by_id[cid] for cid in ids)
    try:
        team = Team(leader=members[0], members=members)
    except TeamError:
        return None
    return score_with_breakdown(team, cfg).combined_score_rate * 1000.0


@pytest.mark.skipif(not _ORACLE.exists(), reason="dumps/ is gitignored account data")
def test_our_score_bonus_agrees_with_the_games_own(tmp_path):
    """The Score Bonus AGREES with the wire on the HOMOGENEOUS skills-only
    captures under the deck screen's steady-state ACTIVE uptime
    (`model.active_uptime = "steady"`). Read the module docstring before
    touching a bound. The interval-contrast decks are excluded on purpose --
    they carry the residual synchronisation SPREAD and get their own test below."""
    defaults = build_parser().parse_args(["solve"])
    catalogue = load_cards(_REPO / defaults.cards)
    owned = load_owned(_REPO / defaults.owned, catalogue)
    cfg = load_scoring_config(_REPO / "config.json")
    assert cfg.active_uptime_model == "steady", (
        "this agreement band was measured under the deck-screen steady uptime; "
        "config.json must ship model.active_uptime = 'steady' for it to hold"
    )

    measured = []
    for index, row in _skills_only_rows():
        wire = row["score_bonus_permil"]
        ours = _our_bonus_permil(row, catalogue, owned, cfg, tmp_path)
        if ours is None:
            continue
        measured.append((index, wire, ours, (ours - wire) / wire))

    assert len(measured) >= 5, (
        f"expected the skills-only captures, got {len(measured)}"
    )

    outside = [
        (index, wire, round(ours, 1), round(100 * gap, 2))
        for index, wire, ours, gap in measured
        if not (_GAP_LOW <= gap <= _GAP_HIGH)
    ]
    assert not outside, (
        "the Score Bonus gap left its agreement band (index, wire, ours, gap %): "
        f"{outside}. Under steady uptime the skills-only rows sit in "
        f"[{100 * _GAP_LOW:.0f}%, {100 * _GAP_HIGH:.0f}%]; a row outside it means a "
        "model change regressed the deck-screen match. If the shift is real and "
        "measured, re-derive the band from the new numbers (see IN-RUN-SCORER.md), "
        "do not widen it to hide a regression."
    )

    # NEGATIVE CONTROL. Every row here is the same handful of compositions, so a
    # band this wide could be satisfied by a model that ignored the cards
    # entirely and returned a constant. Require the rows to actually differ.
    distinct = {round(wire) for _i, wire, _o, _g in measured}
    assert len(distinct) >= 2, (
        f"only {len(distinct)} distinct wire figure(s) among {len(measured)} "
        "rows — this check cannot tell a card-sensitive model from a constant"
    )


@pytest.mark.skipif(not _ORACLE.exists(), reason="dumps/ is gitignored account data")
def test_the_score_bonus_gap_is_not_a_uniform_scale(tmp_path):
    """The Score Bonus residual is NOT a uniform scale.

    Across deliberately contrasting decks the residual (ours - wire, as a
    fraction of wire) runs from well below the wire to well above it. No single
    multiplicative correction, and no single reference-window value, can move a
    set that straddles zero onto zero.

    The cause is cooldown-lattice interval SYNCHRONISATION, not median interval
    and not strongest-active magnitude: our `ExpectedMaximumActiveSkill` assumes
    INDEPENDENT windows, while the deck screen computes `E[max]` on ONE fixed
    lattice where actives at COINCIDENT intervals share windows (see the module
    docstring).

    The model deliberately stays the corpus-average `E[max]`, which is unbiased
    across the chart corpus because synchronisation averages out across songs;
    a lattice `E[max]` would degrade the multi-song objective. This test
    therefore asserts only the robust fact (non-uniformity), which the
    corpus-average model correctly does NOT reproduce; a green here is the model
    behaving as designed.
    """
    defaults = build_parser().parse_args(["solve"])
    catalogue = load_cards(_REPO / defaults.cards)
    owned = load_owned(_REPO / defaults.owned, catalogue)
    cfg = load_scoring_config(_REPO / "config.json")

    rows = json.loads(_ORACLE.read_text(encoding="utf-8"))
    contrast = [r for r in rows if _is_contrast(r)
                and r.get("score_bonus_permil") and r.get("levels")]
    gaps = []
    for row in contrast:
        ours = _our_bonus_permil(row, catalogue, owned, cfg, tmp_path)
        if ours is not None:
            gaps.append((ours - row["score_bonus_permil"]) / row["score_bonus_permil"])

    assert len(gaps) >= 4, (
        f"need the contrast decks to judge non-uniformity, have {len(gaps)}; "
        "regenerate the oracle from all captures"
    )
    # The durable, data-supported claim: the residual is not one scale. It spans
    # a wide range AND changes sign -- both are needed, because a uniform -10 %
    # would span nearly nothing and never cross zero.
    assert max(gaps) - min(gaps) > 0.15, (
        f"residual span {max(gaps) - min(gaps):.2f} is too small to call it "
        "non-uniform; the uniform-scale reading may be back -- investigate"
    )
    assert min(gaps) < 0 < max(gaps), (
        f"residuals no longer straddle zero (min {min(gaps):.2f}, max "
        f"{max(gaps):.2f}); if a model change made them one-signed the fix may "
        "be a scale after all -- rewrite this test, do not force it"
    )
