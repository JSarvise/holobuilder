"""`normalized_lattice` — the opt-in ACTIVE combiner that reproduces the deck
screen's Score-Bonus ACTIVE line under the measured per-instant law
(`scoring_models.NormalizedLatticeActiveSkill`):

    windows_i = [k*cool_i, k*cool_i + dur_i), k >= 1   (all lattices share origin 0)
    value(t)  = sum_{i live at t} m_i P_i / max(1, sum_{i live at t} P_i)
    rate      = sum_b rho_b * value(b)                  (rho: 1-s-bin density, sum 1)

Every hand test below uses a SYNTHETIC uniform density injected through the
constructor, so the numbers are derivable by hand in the docstrings; the
shipped `data/deck_screen_density.json` is exercised only by the loader test
and the oracle test at the bottom (which skips when the gitignored DB is
absent).
"""

from __future__ import annotations

import dataclasses
import json
from fractions import Fraction
from pathlib import Path

import pytest

from hbuilder.domain.card import EffectTarget, GateCondition, SlotOverride
from hbuilder.domain.scoring import score_with_breakdown
from hbuilder.domain.scoring_models import (
    ACTIVE_STACKING_MODELS,
    DECK_SCREEN_DENSITY_PATH,
    DEFAULT_ACTIVE_STACKING,
    NormalizedLatticeActiveSkill,
    active_line_permil,
    load_deck_screen_density,
)

from conftest import make_card, make_scoring_config, make_skill, make_team

REPO = Path(__file__).resolve().parents[1]
DB_PATH = REPO / "tools" / "recon" / "proxy" / "dumps" / "bonus_extract_db.jsonl"

B = 100
UNIFORM = [1.0 / B] * B


def _term(mag, prob, cool, dur, bucket="ACTIVE"):
    """A 7-tuple ActiveTerm. `occupancy` (index 2) is what `max`/`sum` read; the
    normalized model reads (interval, duration, probability) instead, so the
    occupancy here is an arbitrary non-zero placeholder."""
    return (bucket, mag, 0.5, cool, False, dur, prob)


def _model(density=UNIFORM):
    return NormalizedLatticeActiveSkill(density=density)


# --- registry / plumbing -----------------------------------------------------


def test_registered_opt_in_and_default_untouched():
    assert "normalized_lattice" in ACTIVE_STACKING_MODELS
    assert ACTIVE_STACKING_MODELS["normalized_lattice"].name == "normalized_lattice"
    assert DEFAULT_ACTIVE_STACKING.name == "max"


def test_density_loader_reads_shipped_file_and_sums_to_one():
    rho = load_deck_screen_density()
    assert len(rho) >= 200
    assert sum(rho) == pytest.approx(1.0, abs=1e-9)
    assert all(r >= 0.0 for r in rho)
    doc = json.loads(DECK_SCREEN_DENSITY_PATH.read_text("utf8"))
    assert "MEASURED" in doc["provenance"]
    assert doc["bin_seconds"] == 1


def test_shipped_density_is_flat_over_t_1_to_200_and_ceiled():
    """6017/6017 bit-exact with a FLAT 1/200 density on integer
    seconds 1..200 (bin 0 empty) and a CEIL, not a round, on the permil line."""
    rho = load_deck_screen_density()
    assert len(rho) == 201
    assert rho[0] == 0.0
    assert all(r == 0.005 for r in rho[1:])
    doc = json.loads(DECK_SCREEN_DENSITY_PATH.read_text("utf8"))
    assert doc["rounding"] == "ceil"
    assert "6017/6017" in doc["provenance"]


def test_active_line_permil_is_a_ceiling_guarded_against_float_noise():
    """The integer permil line is `ceil(1000 * rate)`; a rate that is an exact
    integer permil up to float noise must NOT be pushed to the next integer (16
    of 6017 oracle decks are exact integers and read exactly that integer)."""
    assert active_line_permil(0.0684) == 69
    assert active_line_permil(0.068) == 68
    assert active_line_permil(0.068 + 1e-13) == 68
    assert active_line_permil(0.0) == 0


def test_window_contains_integer_t_iff_start_le_t_lt_end():
    """Live at integer t iff k*cool <= t < k*cool + dur: cool 20 / dur 3 on a
    100-bin density is live at t = 20, 21, 22 and not at 19 or 23."""
    t = _term(0.4, 1.0, 20, 3)
    for b, live in ((19, False), (20, True), (22, True), (23, False), (40, True)):
        rho = [0.0] * B
        rho[b] = 1.0
        assert (_model(rho).combine([t]) > 0.0) is live, b


def test_density_loader_accepts_a_path(tmp_path):
    p = tmp_path / "rho.json"
    p.write_text(json.dumps({"rho": [0.25, 0.75]}), "utf8")
    assert load_deck_screen_density(p) == (0.25, 0.75)


# --- the law on hand-built terms ---------------------------------------------


def test_single_card_is_magnitude_times_probability_times_uptime():
    """cool 10, dur 5 over 100 uniform bins: windows [10,15), [20,25) ... [90,95)
    = 9 x 5 = 45 live bins. value = 0.4*0.5/max(1,0.5) = 0.2 in each.
    rate = 45/100 * 0.2 = 0.09."""
    assert _model().combine([_term(0.4, 0.5, 10, 5)]) == pytest.approx(0.09)


def test_two_same_lattice_cards_sum_while_probability_mass_is_below_one():
    """Both on cool 10 / dur 5 at P 0.5: S = 1.0 in every live bin, so the
    denominator is max(1, 1.0) = 1 and the two simply add:
    0.45 * (0.4*0.5 + 0.2*0.5) = 0.135 = 0.09 + 0.045."""
    m = _model()
    a, b = _term(0.4, 0.5, 10, 5), _term(0.2, 0.5, 10, 5)
    assert m.combine([a, b]) == pytest.approx(m.combine([a]) + m.combine([b]))
    assert m.combine([a, b]) == pytest.approx(0.135)


def test_three_same_lattice_cards_saturate_at_probability_mass_one():
    """Three at P 0.5: S = 1.5 > 1, so value = 0.5*(0.4+0.2+0.3)/1.5 = 0.3 —
    the probability-weighted MEAN magnitude, not the sum. rate = 0.45*0.3 =
    0.135, strictly below the 0.2025 the plain sum of solos would give."""
    m = _model()
    terms = [_term(0.4, 0.5, 10, 5), _term(0.2, 0.5, 10, 5), _term(0.3, 0.5, 10, 5)]
    assert m.combine(terms) == pytest.approx(0.135)
    assert m.combine(terms) < sum(m.combine([t]) for t in terms)


def test_different_lattices_normalise_only_on_their_overlap():
    """A: cool 10 / dur 5 / P 0.5 / m 0.4 -> 45 bins. B: cool 20 / dur 10 / P 0.8
    / m 0.2 -> [20,30) [40,50) [60,70) [80,90) = 40 bins. They overlap on
    [20,25) [40,45) [60,65) [80,85) = 20 bins where S = 1.3:
        A-only  25 bins x 0.20
        B-only  20 bins x 0.16
        both    20 bins x (0.20 + 0.16) / 1.3
    rate = (5 + 3.2 + 7.2/1.3) / 100."""
    expected = (Fraction(5) + Fraction(16, 5) + Fraction(72, 10) / Fraction(13, 10)) / 100
    got = _model().combine([_term(0.4, 0.5, 10, 5), _term(0.2, 0.8, 20, 10)])
    assert got == pytest.approx(float(expected))


def test_density_weights_bins_not_uniformly():
    """All the mass on bin 12 (inside A's first window) -> rate is A's full
    value 0.2; all the mass on bin 0 (nothing live: no check at t=0) -> 0."""
    t = _term(0.4, 0.5, 10, 5)
    rho = [0.0] * B
    rho[12] = 1.0
    assert _model(rho).combine([t]) == pytest.approx(0.2)
    rho = [0.0] * B
    rho[0] = 1.0
    assert _model(rho).combine([t]) == 0.0


def test_unapplied_term_neither_contributes_nor_normalises():
    """A suppressed skill arrives with probability 0 (and magnitude 0); it must
    not enter S, or it would dilute a live sibling."""
    live = _term(0.4, 0.5, 10, 5)
    dead = ("ACTIVE", 0.0, 0.0, 10, False, 0.0, 0.0)
    m = _model()
    assert m.combine([live, dead]) == m.combine([live])


def test_terms_without_a_lattice_fall_back_to_the_shipped_max_walk():
    """A SPECIAL / always-on term (interval 0) has no lattice to place; it is
    combined exactly as `max` would, per bucket, and ADDED to the lattice part.
    Legacy 4/5-tuples are such terms too."""
    m = _model()
    special5 = ("SPECIAL", 0.5, 0.1, 0, True)
    special4 = ("SPECIAL", 0.5, 0.1, True)
    assert m.combine([special5]) == ACTIVE_STACKING_MODELS["max"].combine([special5])
    assert m.combine([special4]) == m.combine([special5])
    lattice = _term(0.4, 0.5, 10, 5)
    assert m.combine([lattice, special5]) == pytest.approx(0.09 + 0.05)


def test_two_buckets_of_lattice_terms_add():
    a = _term(0.4, 0.5, 10, 5, bucket="ACTIVE")
    s = _term(0.4, 0.5, 10, 5, bucket="SPECIAL")
    m = _model()
    assert m.combine([a, s]) == pytest.approx(m.combine([a]) + m.combine([s]))


# --- legacy parity: the 7-tuple changes nothing for the other combiners -----


@pytest.mark.parametrize("name", ["max", "sum", "max_lattice"])
def test_other_combiners_ignore_the_trailing_lattice_elements(name):
    model = ACTIVE_STACKING_MODELS[name]
    five = [("ACTIVE", 0.4, 0.3, 17, False), ("ACTIVE", 0.2, 0.5, 30, False),
            ("SPECIAL", 0.5, 0.1, 0, True)]
    seven = [t + (7.0, 0.46) for t in five]
    assert model.combine(seven) == model.combine(five)


# --- through the real domain path --------------------------------------------


def _cfg(name="normalized_lattice", **kw):
    return dataclasses.replace(
        make_scoring_config(**kw), active_stacking_model=ACTIVE_STACKING_MODELS[name]
    )


def _active(card_id, magnitude, *, interval=10.0, duration=5.0, override=None, type_="Cute"):
    return make_card(
        card_id,
        type_=type_,
        active=make_skill(
            "ACTIVE", duration=duration, interval=interval,
            probability_tier="Medium", magnitude=magnitude, override=override,
        ),
    )


def _team(*scorers):
    filler = [make_card(f"m{i}", type_="Cool") for i in range(len(scorers), 5)]
    return make_team([make_card("outfit"), *scorers, *filler])


def test_domain_path_single_active_under_shipped_density_matches_hand_formula():
    """Through `score_with_breakdown`: one ACTIVE 40 % / Medium (0.46) on
    cool 10 / dur 5. Under the shipped density the pool must equal
    0.40 * 0.46 * (mass of rho on the union of [10k, 10k+5))."""
    rho = load_deck_screen_density()
    uptime = sum(r for b, r in enumerate(rho) if b >= 10 and (b % 10) < 5)  # bins [10k, 10k+5)
    bd = score_with_breakdown(_team(_active("a", 40.0)), _cfg())
    assert bd.active_stacking_model_name == "normalized_lattice"
    assert bd.active_pool == pytest.approx(0.40 * 0.46 * uptime)


def test_domain_path_override_resolves_against_the_deck_count():
    """Override 70 % with {type: Cute, count: 2}. Alone (one Cute) the base
    35 % is used; with a second Cute in the deck the override fires, and the
    normalized model reads the resolved magnitude."""
    ov = SlotOverride(magnitude=70.0, condition=GateCondition("type", "Cute", 2))
    solo = score_with_breakdown(_team(_active("a", 35.0, override=ov)), _cfg())
    pair = score_with_breakdown(
        _team(_active("a", 35.0, override=ov), make_card("c2", type_="Cute")), _cfg()
    )
    assert pair.active_pool == pytest.approx(2.0 * solo.active_pool)


def test_domain_path_default_model_is_unchanged():
    team = _team(_active("a", 40.0), _active("b", 20.0))
    assert score_with_breakdown(team, _cfg("max")).active_pool == pytest.approx(
        score_with_breakdown(team, dataclasses.replace(make_scoring_config())).active_pool
    )


# --- oracle: bonus_extract_db.jsonl through the real domain path -------------


class _NoRateUp:
    """`ActivationRateModel` that returns the bare tier: the server's [Active
    Skill] line is the BARE law — master probability, no team rate-up."""

    name = "none"

    def apply(self, base_prob: float, team_rate_up: float) -> float:
        return base_prob


@pytest.mark.skipif(not DB_PATH.exists(), reason="gitignored oracle DB absent")
def test_oracle_db_active_line_bit_exact_on_at_least_20_five_card_decks(monkeypatch):
    """Reproduce the server's `bonus.active` permil on real five-card decks,
    built from data/cards.json + state/collection.json (the collection
    restates stats and bloom -> skill level) the way deck_screen_reconcile.py
    builds its teams, with no board: the DB decks are board-free oracles.

    The server's [Active Skill] line is the BARE law — `m_i` is the
    override-resolved magnitude with NO Score-Support multiplier and `P_i` the
    master tier with NO activation-rate-up (measured through this very path: 3582/3790 = 94.5 % bit-exact bare, 48.4 % with the supports and
    rate-up folded in as `_score_up_pool` does for the ranking). So the config
    zeroes the `score_support` family weight and pins the rate model to the
    tier, and reads `bd.active_pool` directly. The ranking path is untouched."""
    from hbuilder.cli import apply_collection, build_parser
    from hbuilder.domain.loader import load_cards
    from hbuilder.domain.team import Team

    # The catalogue and collection at the CLI's own defaults, as
    # board_reconcile.build_cards does — the real game data, not the fixture.
    monkeypatch.chdir(REPO)
    defaults = build_parser().parse_args(["solve"])
    cards = load_cards(REPO / defaults.cards)
    # the account as it was when the DB was captured (bonus_extract.py writes it beside the
    # DB): the rows carry no card state, so today's levels would mix two instants
    at_capture = DB_PATH.with_name("bonus_extract_db.collection.json")
    collection = str(at_capture) if at_capture.exists() else defaults.collection
    cards, _basis = apply_collection(cards, collection, maxed=False)
    by_id = {c.id: c for c in cards}
    base = _cfg()
    cfg = dataclasses.replace(
        base,
        family_weights={**base.family_weights, "score_support": 0.0},
        activation_rate_model=_NoRateUp(),
    )
    assert cfg.board is None

    def costume_of(cid):
        p = cid.split("-")
        return f"cos-{p[1]}-{p[3]}-{p[4]}-{p[5]}"

    by_costume = {}
    for c in by_id.values():
        by_costume.setdefault(costume_of(c.id), c)

    exact = total = 0
    with DB_PATH.open(encoding="utf-8") as fh:
        for line in fh:
            r = json.loads(line)
            if r["music_id"] or len(r["members"]) != 5:
                continue
            if any(m not in by_id for m in r["members"]):
                continue
            leader = by_costume.get(r["costume"]) or next(
                (by_id[m] for m in r["members"] if m.split("-")[1] == r["leader"].split("-")[1]),
                None,
            )
            if leader is None:
                continue
            bd = score_with_breakdown(
                Team(leader=leader, members=tuple(by_id[m] for m in r["members"])), cfg
            )
            assert bd.combined_support_rate == 0.0
            total += 1
            exact += int(active_line_permil(bd.active_pool) == r["bonus"]["active"])
            if total >= 300:
                break
    print(f"normalized_lattice oracle: exact {exact}/{total}")
    assert total >= 20
    assert exact == total
