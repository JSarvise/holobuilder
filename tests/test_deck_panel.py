"""The deck-screen "Unit Score Details" panel, computed from card parameters alone.

Every expected number below is hand-derived from the laws in
`hbuilder.domain.deck_panel`'s docstring, in exact rational arithmetic — never
read back from the implementation. The oracle test at the bottom is the
non-circular one: it compares against ~6,000 captured server panels
(`tools/recon/proxy/dumps/bonus_extract_db.jsonl`, gitignored) and skips when
the dump is absent.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import sys
from fractions import Fraction
from math import ceil
from pathlib import Path

import pytest

from hbuilder.domain.deck_panel import (
    PANEL_WINDOW_S,
    PanelCard,
    _adams,
    _attribution_weights,
    _split_quotas,
    deck_panel,
    live_seconds,
    unit_score_from_panel,
)

_REPO = Path(__file__).resolve().parents[1]
_PROXY = _REPO / "tools" / "recon" / "proxy"
_DUMPS = _PROXY / "dumps"


# --- the lattice --------------------------------------------------------


def test_the_window_is_the_first_two_hundred_whole_seconds():
    assert PANEL_WINDOW_S == 200


def test_a_card_is_live_for_duration_seconds_after_every_whole_cooldown():
    """`k*cool <= t < k*cool + dur`, k >= 1 — so t=0 is NOT live (the first
    activation is at the end of the first cooldown, not at the start)."""
    live = live_seconds(cooldown_s=20, duration_s=10, shorten_permil=0)
    assert live[:10] == (20, 21, 22, 23, 24, 25, 26, 27, 28, 29)
    assert 0 not in live
    assert 19 not in live
    assert live[-1] == 200  # k=10 lands exactly on the window edge
    assert len(live) == 9 * 10 + 1


def test_cooldown_shorten_is_an_exact_fraction_never_a_rounded_second():
    """cool' = cool*1000/(1000+c). 10 s at 200 permil is 25/3 s = 8.333...,
    so the first window is [8.33, 10.33) -> seconds 9 and 10; the second is
    [16.67, 18.67) -> 17 and 18; the third starts exactly at 25."""
    live = live_seconds(cooldown_s=10, duration_s=2, shorten_permil=200)
    assert live[:6] == (9, 10, 17, 18, 25, 26)


# --- the Active line ----------------------------------------------------


def _plain(**kw) -> PanelCard:
    base = dict(
        magnitude_permil=1000, probability_permil=460, cooldown_s=20, duration_s=10
    )
    base.update(kw)
    return PanelCard(**base)


def test_one_card_below_the_saturation_floor_divides_by_one_thousand():
    """91 live seconds; sum P' = 460 < 1000, so the denominator is the floor
    1000 and v(t) = 1000*460/1000 = 460. A = 91*460/200 = 209.3 -> 210."""
    panel = deck_panel([_plain()])
    assert panel.a_raw == Fraction(91 * 460, 200)
    assert panel.a_raw == Fraction(2093, 10)
    assert panel.active == 210


def test_two_cards_on_one_cooldown_saturate_and_do_not_add():
    """Both live on the same 91 seconds with P' = 550 each: sum P' = 1100
    exceeds the floor, so v(t) = (1000*550 + 1000*550)/1100 = 1000 — NOT the
    2*550 = 1100 an unsaturated sum would give. A = 91*1000/200 = 455."""
    pair = [_plain(probability_permil=550), _plain(probability_permil=550)]
    assert deck_panel(pair).a_raw == Fraction(455)
    assert deck_panel(pair).active == 455

    # Each card ALONE is 91*550/200 = 250.25, so an unsaturated model would
    # read 500.5 — the shared lattice costs 45.5 permil of it.
    alone = deck_panel([_plain(probability_permil=550)])
    assert alone.a_raw == Fraction(91 * 550, 200)
    assert deck_panel(pair).a_raw < 2 * alone.a_raw


def test_board_activation_probability_up_scales_p_but_not_the_active_line():
    """P' = P*(1000+u)/1000 feeds the boosted total; the [Active Skill] LINE
    itself stays on the BASE lattice (the server books the board's
    gain under [Holomem Board], not under [Active Skill])."""
    plain = deck_panel([_plain()])
    buffed = deck_panel([_plain(board_probability_up_permil=500)])
    assert buffed.active == plain.active
    assert buffed.a_raw == plain.a_raw
    assert buffed.tot_raw > plain.tot_raw  # the gain lands in the board line
    assert buffed.board > 0


# --- the boosted total, the clamp, and the N-1 term ----------------------


def test_one_effect_up_source_boosts_the_magnitude_and_fills_one_line():
    """mag' = ceil(1000*(1000+500)/1000) = 1500, v = 1500*460/1000 = 690,
    tot = 91*690/200 = 313.95 -> 314. One source, so no N-1 term."""
    panel = deck_panel([_plain(outfit_effect_up_permil=500)])
    assert panel.tot_raw == Fraction(91 * 690, 200)
    assert panel.active == 210
    assert panel.outfit + panel.passive + panel.board == 314 - 210
    assert panel.passive == 0 and panel.board == 0
    assert panel.outfit == 104


def test_each_line_is_its_own_ceiling_and_the_pedestal_is_an_identity():
    """The per-line identity, not a fitted `+ max(N-1, 0)` pedestal.

    Each displayed line is `ceil(base * q_s / SUM q)` computed INDEPENDENTLY, so
    the lines do not re-sum to `ceil(tot)` by construction; what they sum to is
    the identity

        SUM_s ceil(x_s) = base + #{s : x_s not an integer} - SUM_s frac(x_s)

    whose last term is itself an integer. `max(N-1, 0)` is that identity only
    when that term happens to equal 1; on captured decks it is 0, 1 or 2, and
    forcing a pedestal gets the three-line decks wrong.

    One card, 91 live seconds, so `w` is a common factor and every share is the
    plain permil ratio.

    TWO sources: E = 700 -> mag' = 1700, v = 782, tot = 91*782/200 = 355.81 ->
    356; A = 209.3 -> 210, so base = 146. Shares 500:200 = 5:2 of 146 are
    104.2857 and 41.7143 -> 105 and 42; both non-integral and the fracs sum to
    exactly 1, so the total is 356 + 2 - 1 = 357 — the same as the pedestal rule.

    THREE sources: E = 1000 -> mag' = 2000, v = 920, tot = 418.6 -> 419, base =
    209. Shares 5:2:3 of 209 are 104.5, 41.8 and 62.7 -> 105, 42, 63; the fracs
    sum to 2, so the total is 419 + 3 - 2 = 420, where the pedestal rule says 421.
    """
    two = deck_panel(
        [_plain(outfit_effect_up_permil=500, passive_effect_up_permil=200)]
    )
    assert two.tot_raw == Fraction(91 * 782, 200)
    assert two.active == 210
    assert (two.outfit, two.passive, two.board) == (105, 42, 0)
    assert two.nonspecial_total == 357

    three = deck_panel(
        [
            _plain(
                outfit_effect_up_permil=500,
                passive_effect_up_permil=200,
                board_effect_up_permil=300,
            )
        ]
    )
    assert three.tot_raw == Fraction(91 * 920, 200)
    assert three.active == 210
    assert (three.outfit, three.passive, three.board) == (105, 42, 63)
    assert three.nonspecial_total == 420          # the pedestal rule says 421

    # the identity itself, spelled out on the three-line deck
    base = 419 - 210
    shares = [Fraction(base * n, 10) for n in (5, 2, 3)]
    nonint = sum(1 for x in shares if x.denominator != 1)
    frac_sum = sum(x - int(x) for x in shares)
    assert frac_sum.denominator == 1
    assert sum(ceil(x) for x in shares) == base + nonint - frac_sum


def test_the_split_always_sums_to_the_measured_total():
    panel = deck_panel(
        [
            _plain(
                outfit_effect_up_permil=500,
                passive_effect_up_permil=200,
                board_effect_up_permil=300,
            )
        ]
    )
    assert panel.active + panel.outfit + panel.passive + panel.board == (
        panel.nonspecial_total
    )
    assert panel.score_bonus_permil == panel.nonspecial_total + panel.special


# --- P' is a whole permil, rounded up ------------------------------------


def test_the_boosted_probability_is_a_whole_permil_rounded_up():
    """The server carries `P'` as an integer permil and
    rounds it UP; keeping it exact makes `tot` come out low by ~0.06 and the
    ceiling then loses an integer.

    P = 550, u = 396 -> 550*1396/1000 = 767.8 permil -> 768, not 767.8.
    One card, 91 live seconds, sum P' = 768 < the 1000 floor, so
    v(t) = 1000*768 / 1000 = 768 and tot = 768*91/200."""
    card = _plain(probability_permil=550, board_probability_up_permil=396)
    panel = deck_panel([card])
    assert panel.tot_raw == Fraction(768 * 91, 200)
    # the exact-rational value, for contrast
    assert panel.tot_raw != Fraction(7678 * 91, 2000)


def test_rounding_p_prime_is_a_no_op_without_a_board():
    """It is board-only BY CONSTRUCTION: with u = 0 the ceiling has nothing to
    round, which is why the board-free control does not move by a single deck.
    That is the structural guard, not a lucky one."""
    for p in (370, 460, 550):
        plain = deck_panel([_plain(probability_permil=p)])
        assert plain.tot_raw == Fraction(p * 91, 200)
        assert plain.a_raw == plain.tot_raw          # no effect-up, so mag' = mag


def test_the_active_line_ceils_the_servers_float64_sum_not_the_exact_one():
    """The server accumulates the window sum in float64, one
    quotient per live second in timeline order, and divides by 200 last. The two
    ceilings differ ONLY where the exact sum is an integer — there the float
    lands a few ulp above and the line comes out one higher.

    This deck is one of the captured rows where that happens (the server shows
    Active 263). Three board-free cards, all P = 460, so every denominator
    is the 1000-permil floor and the exact sum is the integer 262:

        (500, 20, 7) -> 64 live seconds, (400, 30, 13) -> 78, (700, 23, 10) -> 80

    The float sum is recomputed here from those hand-derived terms rather than
    read back from the module, so the assertion is not circular."""
    specs = ((500, 20, 7), (400, 30, 13), (700, 23, 10))
    cards = [
        PanelCard(
            magnitude_permil=mag, probability_permil=460, cooldown_s=cd, duration_s=dur
        )
        for mag, cd, dur in specs
    ]
    live = {
        mag: live_seconds(cooldown_s=cd, duration_s=dur) for mag, cd, dur in specs
    }
    assert [len(v) for v in live.values()] == [64, 78, 80]

    panel = deck_panel(cards)
    assert panel.a_raw == 262           # the EXACT sum is an integer, on the nose
    assert ceil(panel.a_raw) == 262
    assert panel.active == 263          # ...and the server's float ceils one above

    # the same accumulation, hand-rolled: n_t / d_t per live second, then / 200.
    # 76 seconds have one card live, 55 have two and 12 have all three — and only
    # those 12 saturate (3 * 460 = 1380 permil > the 1000 floor).
    numerator: dict[int, int] = {}
    livecount: dict[int, int] = {}
    for mag, cd, dur in specs:
        for t in live_seconds(cooldown_s=cd, duration_s=dur):
            numerator[t] = numerator.get(t, 0) + mag * 460 * 1000
            livecount[t] = livecount.get(t, 0) + 1
    assert sorted(livecount.values()).count(3) == 12
    s = 0.0
    for t in sorted(numerator):
        s += numerator[t] / max(10 ** 6, livecount[t] * 460 * 1000)
    s /= 200
    assert s == 262.00000000000006      # 262 plus one ulp of accumulated error
    assert ceil(s) == panel.active


def test_the_active_line_is_untouched_by_the_probability_rounding():
    """`A` is computed on the BASE lattice, where `u` is set aside — so the
    Active line stays exact by construction even on a card
    carrying a large probability-up."""
    card = _plain(probability_permil=550, board_probability_up_permil=396)
    assert deck_panel([card]).a_raw == Fraction(550 * 91, 200)
    assert deck_panel([card]).active == ceil(Fraction(550 * 91, 200))


# --- the per-line split: Adams' method on unsaturated contributions ------


def test_adams_gives_every_positive_quota_a_seat_before_any_second_seat():
    """Adams is the method of SMALLEST DIVISORS: `line_s = ceil(kappa*q_s)`, and
    a ceiling of anything positive is at least 1. That is exactly why the server
    never shows a displayed source as zero."""
    assert _adams([1, 1000], 2) == [1, 1]
    assert _adams([1, 10 ** 9], 3) == [1, 2]
    # fewer seats than units, or no positive quota: the method does not apply
    assert _adams([5, 5], 1) is None
    assert _adams([0, 0], 4) is None


def test_adams_matches_the_ceiling_under_a_common_divisor():
    """Hand-checked against the definition rather than the implementation.
    q = (91, 41), target 153. Need ceil(91k) = 105 -> k in (104/91, 105/91]
    and ceil(41k) = 48 -> k in (47/41, 48/41]; the two intervals overlap on
    (47/41, 105/91], so (105, 48) IS reachable by a common divisor and sums to
    153."""
    assert _adams([91, 41], 153) == [105, 48]
    lo, hi = Fraction(47, 41), Fraction(105, 91)
    assert lo < hi
    k = (lo + hi) / 2
    assert (-(-91 * k // 1), -(-41 * k // 1)) == (105, 48)


def test_the_split_weights_a_permil_by_the_card_that_carries_it():
    """A rule that summed permils would ignore WHICH card carried them. Two
    cards with the same 500-permil source but different live time must NOT split
    evenly.

    Card A: cool 20 / dur 10 -> 91 live seconds. Card B: cool 40 / dur 10 ->
    4*10 + 1 = 41. Both P = 460, so sum P = 920 < the 1000 floor: nothing
    saturates and the window sum is linear.

        A   = 460*1000*(91+41)/200 = 303.6            -> active 304
        mag'= ceil(1000*1500/1000) = 1500 on each
        tot = 690*(91+41)/200 = 455.4                 -> 456
        base = 456 - 304 = 152
        w   = mag*P*dur/cool: A gets 1000*460*10/20 = 230000, B half of that,
              so the quotas are 500*230000 : 500*115000 = 2 : 1
        shares = 152*2/3 = 101.33 and 152*1/3 = 50.67 -> 102 and 51

    Two rules are refuted at once: a permil-sum rule would give
    (500, 500) -> 76/77 each, and a COUNT weight (91 : 41) with a fitted
    target 153 would give (105, 48). The weight is a continuous RATE: on
    captured decks the count form fails the one ratio pair that separates
    them."""
    a = PanelCard(
        magnitude_permil=1000,
        probability_permil=460,
        cooldown_s=20,
        duration_s=10,
        outfit_effect_up_permil=500,
    )
    b = PanelCard(
        magnitude_permil=1000,
        probability_permil=460,
        cooldown_s=40,
        duration_s=10,
        passive_effect_up_permil=500,
    )
    panel = deck_panel([a, b])
    assert panel.a_raw == Fraction(460 * 132, 200)
    assert panel.active == 304
    assert panel.tot_raw == Fraction(690 * 132, 200)
    assert _attribution_weights([a, b]) == [Fraction(230000), Fraction(115000)]
    assert (panel.outfit, panel.passive) == (102, 51)
    assert panel.nonspecial_total == 457


def test_the_attribution_weight_is_an_unshortened_rate_with_no_saturation_clamp():
    """The panel splits a SATURATED total using
    each card's UNSATURATED contribution, and that contribution is the expected
    uptime RATE `mag * P * dur / cool` on the NOMINAL cooldown — no
    `max(1, SUM P)` denominator, no board buff of any kind.

    Three cards of P = 550 saturate hard (sum 1650 > the 1000 floor), yet each
    card's weight is unchanged by the presence of the others: 1000*550*10/20."""
    cards = [
        PanelCard(
            magnitude_permil=1000, probability_permil=550, cooldown_s=20, duration_s=10
        )
        for _ in range(3)
    ]
    w = _attribution_weights(cards)
    assert w == [Fraction(1000 * 550 * 10, 20)] * 3
    assert w == [Fraction(275000)] * 3
    alone = _attribution_weights(cards[:1])
    assert alone[0] == w[0]  # saturation of the deck does not touch the weight
    # and no board buff enters the weight — not the probability-up...
    buffed = _attribution_weights(
        [dataclasses.replace(cards[0], board_probability_up_permil=500)]
    )
    assert buffed[0] == alone[0]
    # ...and not the cooldown-shorten either (it multiplies the
    # card's own rate inside ITS board gain, and putting it in the weight is
    # what made the shorten look concave and cost the three-line decks)
    shortened = _attribution_weights(
        [dataclasses.replace(cards[0], board_cooldown_shorten_permil=120)]
    )
    assert shortened[0] == alone[0]
    # the shorten DOES still move the lattice, so this is not a no-op elsewhere
    assert len(live_seconds(cooldown_s=20, duration_s=10, shorten_permil=120)) != len(
        live_seconds(cooldown_s=20, duration_s=10)
    )


def test_one_board_buff_kind_alone_enters_at_face_value():
    """With only ONE board buff kind present the multiplicative quota collapses to
    the plain weighted sum -- every cross term has a zero factor. So each kind
    enters at face value, and no kind carries a coefficient of its own.

    No fitted coefficient on the cooldown-shorten permil is needed. One card,
    so `w = mag*P*dur/cool` is a single common factor and the quota is
    `permil * w`.
    """
    w = Fraction(1000 * 460 * 10, 20)

    effect = PanelCard(
        magnitude_permil=1000,
        probability_permil=460,
        cooldown_s=20,
        duration_s=10,
        board_effect_up_permil=100,
    )
    assert _split_quotas([effect])[2] == 100 * w

    probup = PanelCard(
        magnitude_permil=1000,
        probability_permil=460,
        cooldown_s=20,
        duration_s=10,
        board_probability_up_permil=100,
    )
    assert _split_quotas([probup])[2] == 100 * w

    shorten = PanelCard(
        magnitude_permil=1000,
        probability_permil=460,
        cooldown_s=20,
        duration_s=10,
        board_cooldown_shorten_permil=100,
    )
    # the weight is on the NOMINAL cooldown, so the shorten does not change it
    # — it enters only through this card's own board gain
    assert _split_quotas([shorten])[2] == 100 * w


def test_two_board_buff_kinds_multiply_rather_than_add():
    """With two kinds present the board
    quota exceeds their sum by exactly the cross term.

    One card carrying effect-up E and probability-up P. The mean permils are E and
    P themselves, so the quota is `1000*[(1+E/1000)(1+P/1000) - 1] = E + P + E*P/1000`
    per unit weight. With E = P = 100 that is 210, not 200 -- and 10 is not a
    rounding artefact, it is `E*P/1000`.
    """
    card = PanelCard(
        magnitude_permil=1000,
        probability_permil=460,
        cooldown_s=20,
        duration_s=10,
        board_effect_up_permil=100,
        board_probability_up_permil=100,
    )
    w = Fraction(1000 * 460 * 10, 20)

    got = _split_quotas([card])[2]
    additive = (100 + 100) * w
    cross = Fraction(100 * 100, 1000) * w

    assert got == additive + cross
    assert got > additive
    assert got == 210 * w


def test_all_three_board_kinds_carry_every_cross_term():
    """All three kinds present: the quota carries the three pairwise terms AND the
    triple term. `1000*[(1+E/1000)(1+P/1000)(1+S/1000) - 1]` with E=P=S=100 is
    331 per unit weight, not 300."""
    card = PanelCard(
        magnitude_permil=1000,
        probability_permil=460,
        cooldown_s=20,
        duration_s=10,
        board_effect_up_permil=100,
        board_probability_up_permil=100,
        board_cooldown_shorten_permil=100,
    )
    w = Fraction(1000 * 460 * 10, 20)
    assert _split_quotas([card])[2] == 331 * w


def test_the_board_gain_is_per_card_not_a_product_of_deck_means():
    """Each card's board gain is the product of ITS OWN three buffs,
    weighted by its own rate — not a product of `w`-weighted deck-level means.

    Two cards of equal weight, one carrying effect-up 100 and the other
    probability-up 100. Per card the gains are 100 and 100, so the quota is
    200*w. The deck-mean form would take mean E = 50 and mean u = 50 and give
    `1000*[(1.05)(1.05)-1] = 102.5` per unit weight, i.e. 205*w — bigger by the
    cross term of buffs that never meet on the same card.

    That difference appears only when a buff hits a SUBSET of the members,
    which is exactly when a passive line is also drawn, so it decides the
    three-line decks."""
    e_card = PanelCard(
        magnitude_permil=1000, probability_permil=460, cooldown_s=20, duration_s=10,
        board_effect_up_permil=100,
    )
    u_card = PanelCard(
        magnitude_permil=1000, probability_permil=460, cooldown_s=20, duration_s=10,
        board_probability_up_permil=100,
    )
    w = Fraction(1000 * 460 * 10, 20)
    assert _attribution_weights([e_card, u_card]) == [w, w]
    assert _split_quotas([e_card, u_card])[2] == 200 * w
    # the refuted deck-mean form, spelled out so it cannot creep back
    deck_mean_form = 2 * w * 1000 * ((1 + Fraction(50, 1000)) * (1 + Fraction(50, 1000)) - 1)
    assert deck_mean_form == 205 * w
    assert _split_quotas([e_card, u_card])[2] != deck_mean_form


def test_gamma_is_gone():
    """A fitted gamma coefficient has no mechanism and must not come back by
    accident -- the multiplicative form is what carries the shorten now, and it
    gives each carrier an effective coefficient of `1 + P/1000` instead of one
    shared scalar."""
    import hbuilder.domain.deck_panel as _dp

    assert not hasattr(_dp, "_GAMMA_SHORTEN")


def test_a_displayed_source_with_no_live_carrier_falls_back_and_still_shows():
    """Degenerate case: the source is present but every carrier is weightless
    (magnitude 0), so its quota is 0 and Adams would allocate it nothing. The
    fallback uses a proportional rule so the parts still sum exactly."""
    carrier = PanelCard(
        magnitude_permil=0,
        probability_permil=460,
        cooldown_s=20,
        duration_s=10,
        passive_effect_up_permil=300,
    )
    worker = PanelCard(
        magnitude_permil=1000,
        probability_permil=460,
        cooldown_s=20,
        duration_s=10,
        outfit_effect_up_permil=500,
    )
    panel = deck_panel([carrier, worker])
    assert _split_quotas([carrier, worker])[1] == 0
    assert panel.outfit + panel.passive + panel.board == (
        panel.nonspecial_total - panel.active
    )


def test_a_boost_that_does_not_beat_the_base_zeroes_all_three_lines():
    """When the boosted total fails to exceed the base A the
    server shows Outfit, Passive AND Board as 0 together — it does not show a
    negative line, and it does not show a partial one.

    Card 1 (mag 2000, P 550) and card 2 (mag 100, P 370) share one lattice.
    Base: sum P = 920 < 1000, v = (2000*550 + 100*370)/1000 = 1137.
    Card 2 carries a 1000-permil board prob-up, so P'_2 = 740 and the pair
    saturates: v' = (2000*550 + 100*740)/1290 = 910.08 — the dilution costs
    more than the buff earns."""
    cards = [
        PanelCard(
            magnitude_permil=2000, probability_permil=550, cooldown_s=20, duration_s=10
        ),
        PanelCard(
            magnitude_permil=100,
            probability_permil=370,
            cooldown_s=20,
            duration_s=10,
            board_probability_up_permil=1000,
        ),
    ]
    panel = deck_panel(cards)
    assert panel.a_raw == Fraction(1137 * 91, 200)
    assert panel.tot_raw < panel.a_raw
    assert panel.active == 518
    assert (panel.outfit, panel.passive, panel.board) == (0, 0, 0)
    assert panel.nonspecial_total == 518


def test_a_deck_with_no_source_at_all_shows_only_the_active_line():
    panel = deck_panel([_plain()])
    assert (panel.outfit, panel.passive, panel.board) == (0, 0, 0)
    assert panel.nonspecial_total == panel.active


# --- the Special line ---------------------------------------------------


def test_the_special_line_is_the_active_integer_times_the_special_pool():
    """Special = ceil(Active * sum smag*(1 + R/2000)*sdur / 120000).
    Active = 210, smag = 800, sdur = 7 -> 210*5600/120000 = 9.8 -> 10."""
    panel = deck_panel(
        [_plain(special_magnitude_permil=800, special_duration_s=7)]
    )
    assert panel.active == 210
    assert panel.special == 10


def test_a_special_activation_rate_rider_counts_half(  # noqa: D103
):
    """The rider R enters as (1 + R/2000) — HALF the permil. R = 400 gives
    x1.2: 210*5600*1.2/120000 = 11.76 -> 12."""
    panel = deck_panel(
        [
            _plain(
                special_magnitude_permil=800,
                special_duration_s=7,
                special_rate_rider_permil=400,
            )
        ]
    )
    assert panel.special == 12


def test_no_special_skill_means_no_special_line():
    assert deck_panel([_plain()]).special == 0


# --- Unit Score ---------------------------------------------------------


def test_unit_score_is_the_bit_exact_integer_identity():
    """ceil(OverallPower * (1000 + ScoreBonus) * 203734 / 100_000_000)
    (exact on every captured row). 100000 * 2210 * 203734 / 1e8 = 450252.14 -> 450253."""
    assert unit_score_from_panel(100000, 1210) == 450253


def test_the_panel_carries_its_own_unit_score():
    panel = deck_panel(
        [_plain(special_magnitude_permil=800, special_duration_s=7)],
        overall_power=100000,
    )
    assert panel.score_bonus_permil == 210 + 10
    assert panel.unit_score == unit_score_from_panel(100000, 220)


# --- validation ---------------------------------------------------------


def test_a_zero_cooldown_is_rejected_rather_than_looping_forever():
    with pytest.raises(ValueError):
        deck_panel([_plain(cooldown_s=0)])


# --- the oracle ---------------------------------------------------------


def _params_module():
    path = _PROXY / "deck_panel_params.py"
    if not path.exists():  # pragma: no cover
        pytest.skip("tools/recon/proxy/deck_panel_params.py missing")
    spec = importlib.util.spec_from_file_location("deck_panel_params", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["deck_panel_params"] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # pragma: no cover - offline machines
        pytest.skip(f"master data unavailable: {exc!r}")
    return module


_ORACLE_CACHE: list | None = None


def _oracle_rows():
    """Every song-less capture in the panel DB, paired with its PanelCards.

    ~6,000 rows; the whole sweep costs about 3 s, so it is built once and
    shared. Per-song (yellow board node) rows are out of the model's scope and
    are excluded.
    """
    global _ORACLE_CACHE
    if _ORACLE_CACHE is not None:
        return _ORACLE_CACHE
    db = _DUMPS / "bonus_extract_db.jsonl"
    if not db.exists():
        pytest.skip("dumps/bonus_extract_db.jsonl unavailable (gitignored)")
    params = _params_module()
    out = []
    with db.open(encoding="utf-8") as fh:
        for raw in fh:
            row = json.loads(raw)
            if row.get("music_id"):
                continue
            try:
                cards = params.panel_cards(row)
            except KeyError:  # pragma: no cover - a card outside the collection
                continue
            out.append((row, cards))
    if len(out) < 200:  # pragma: no cover
        pytest.skip(f"too few usable oracle rows ({len(out)})")
    _ORACLE_CACHE = out
    return out


def test_oracle_active_line_is_bit_exact():
    rows = _oracle_rows()
    misses = [
        row["capture_index"]
        for row, cards in rows
        if deck_panel(cards).active != row["bonus"]["active"]
    ]
    assert misses == [], f"{len(misses)}/{len(rows)} Active misses"


def test_oracle_special_line_is_bit_exact_on_board_free_rows():
    rows = _oracle_rows()
    board_free = [
        (row, cards)
        for row, cards in rows
        if not any(
            c.board_effect_up_permil
            or c.board_probability_up_permil
            or c.board_cooldown_shorten_permil
            for c in cards
        )
    ]
    assert len(board_free) >= 100
    exact = sum(
        deck_panel(cards).special == row["bonus"]["special"] for row, cards in board_free
    )
    # Exact on all but two rows. The two misses are decks whose product lands on an
    # exact integer, where the server's own float lands one above; `floor + 1`
    # fixes those two and breaks 26 others, so `ceil` stands.
    assert len(board_free) - exact <= 2, f"{exact}/{len(board_free)}"
    assert exact >= 0.999 * len(board_free), f"{exact}/{len(board_free)}"


def test_oracle_non_special_total_is_within_one_permil():
    """The TOTAL is the asserted quantity (about 97 % bit-exact over the corpus);
    the per-line split of Outfit/Passive/Board is best-effort and NOT asserted
    here. Every residual is +/-1 permil."""
    rows = _oracle_rows()
    resid = []
    for row, cards in rows:
        server = sum(row["bonus"][k] for k in ("active", "outfit", "passive", "board"))
        resid.append(deck_panel(cards).nonspecial_total - server)
    exact = sum(1 for r in resid if r == 0)
    assert exact >= 0.96 * len(resid), f"{exact}/{len(resid)} exact"
    # About 97 % exact; the rest at -1 or +1, and a single row at -2.
    assert sum(1 for r in resid if abs(r) > 1) <= 1, sorted(set(resid))
    assert max(abs(r) for r in resid) <= 2, sorted(set(resid))


def test_oracle_unit_score_follows_the_total():
    rows = _oracle_rows()
    exact = 0
    for row, cards in rows:
        panel = deck_panel(cards, overall_power=row["overall_power"])
        exact += panel.unit_score == row["unit_score"]
    assert exact >= 0.96 * len(rows), f"{exact}/{len(rows)}"


def test_oracle_unit_score_is_bit_exact_from_the_servers_own_lines():
    """Isolates the Unit Score identity from the panel model: fed the server's
    OWN Score Bonus it must reproduce the server's OWN Unit Score, every row."""
    rows = _oracle_rows()
    for row, _cards in rows:
        assert (
            unit_score_from_panel(row["overall_power"], row["score_bonus_permil"])
            == row["unit_score"]
        )


# --- the per-song (singers') board bonus ---------------------------------


def test_the_song_bonus_joins_the_board_line_by_the_per_song_law():
    """board + eff3 + ceil((Active + Outfit + Passive + special_raw) * eff3 / 1000)
    Checked on the game's deck screen, m0342 (We are GAMERS !!!!) with
    a GAMERS-free team: lines 570 / 0 / 23 / 370 / 282 and eff3 = 100 (the cap) gave
    Board 558 = 370 + 100 + ceil(875 * 100 / 1000) -- the game's 55.8 % exactly."""
    plain = deck_panel([_plain()])
    sung = deck_panel([_plain()], song_bonus_permil=100)
    a, o, p = plain.active, plain.outfit, plain.passive
    assert sung.board == plain.board + 100 + -(-(a + o + p) * 100 // 1000)
    assert sung.active == plain.active and sung.special == plain.special
    assert sung.score_bonus_permil == plain.score_bonus_permil + (sung.board - plain.board)
    assert deck_panel([_plain()], song_bonus_permil=0) == plain
