"""The deck screen's **Unit Score Details** panel, from card parameters alone.

This module is a SECOND, opt-in model. It does not touch `total_score`, the
`_score_up_pool`, `DEFAULT_ACTIVE_STACKING` or `deck_screen_unit_score` — those
are the shipped ranking objective and stay exactly as they are. What this adds
is the ability to compute the five panel lines the server shows, for an
ARBITRARY hypothetical deck (unowned cards, maxed levels, a board the account
does not have), because every input is a plain number rather than a captured
server response.

Exact integer/`Fraction` arithmetic everywhere EXCEPT the two window sums the
server itself accumulates in float64 (see "the float64 window sums" below): the
model reproduces that accumulation in the server's own order, because the
displayed integers are its ceilings and on integral-valued decks the float lands
on the far side of the integer. `a_raw` and `tot_raw` are still reported exact.

## Provenance

The laws were extracted from captured server panels
(`tools/recon/proxy/dumps/bonus_extract_db.jsonl`, `SaveDeck` -> `GetDeck`
oracles) and re-checked in exact rational arithmetic. A hit rate quoted below is
the share of captured decks on which the integer the law produces equals the
server's own integer.

## The laws

Per member card `i`, all permil integers unless noted:

    cool'_i = cool_i * 1000 / (1000 + c_i)          EXACT Fraction, never rounded
    P'_i    = ceil( P_i * (1000 + u_i) / 1000 )     WHOLE PERMIL, rounded UP

`c_i` is the board's cooldown-shorten and `u_i` the board's
activation-probability-up.

⚠ **`P'` is a WHOLE PERMIL, rounded UP — the server does not carry it exactly.**
Equivalently `P'_i = P_i + ceil(P_i * u_i / 1000)`: the board's probability-up
increment is itself a whole permil. It only matters on boarded cards — with
`u_i = 0` the rounding is a no-op. Keeping `P'` exact makes `tot` come out low
by about 0.06, decisive only where `tot` sits just under an integer, and the
total then loses an integer to the ceiling.

The rounded `P'` must be used in the numerator AND in the saturation denominator
— one rounded value everywhere, which is what "the server stores an integer
permil" means; rounding only one side breaks decks. The Active line is untouched
by construction (it is the base lattice, `u_i = 0`).

⚠ **What is NOT pinned:** the captured boards offer only a few distinct `(P, u)`
pairs, whose fractional parts are all >= 0.36. So `ceil` is indistinguishable
from any round-up-above-threshold rule with a cut <= 0.36. It IS distinguished
from round-half-up and from `floor`. The client's own arithmetic helper
`CalcUtility` uses only truncation and round-half-away-from-zero — but the panel
is computed SERVER-side, so that is evidence about the client, not the server,
whose observable convention is to round up everywhere.

Card `i` is **live at integer second t** iff

    k * cool'_i <= t < k * cool'_i + dur_i     for some integer k >= 1

(so t = 0 is never live: the first activation ends the first cooldown). The
window is the first `PANEL_WINDOW_S` = 200 whole seconds.

    v(t) = SUM_{i live at t} mag_i * P'_i / max(1000, SUM_{i live at t} P'_i)
    A    = SUM_{t=1..200} v(t) / 200

    [Active Skill] = ceil( float64(A) )            bit-exact on every captured deck

### The float64 window sums

The server accumulates both window sums in float64, one quotient per live second
in timeline order, and divides by 200 at the end:

    s = 0.0;  for t = 1..200:  s += n_t / d_t;   A = s / 200      (same for tot)

with `n_t = SUM mag*P'*1000` and `d_t = max(10**6, SUM P'*1000)` the exact
integer numerator and denominator of `v(t)`. The exact ceiling and the float
ceiling differ ONLY where the exact sum is an integer: there the float lands a
few ulp above and the ceiling comes out one higher. The exact ceiling misses a
handful of Active lines, all with integral `A`; this float order repairs exactly
those and bumps none of the other integral-`A` decks. Other orders (per-term
/200 first, ascending, denominator-last, grouped equal terms) fit the boosted
total clearly worse.

⚠ The **[Active Skill] line is computed on the BASE lattice** — `u_i` and `c_i`
are set aside for it. The server books the board's lattice gain under
[Holomem Board], not under [Active Skill] (using the buffed lattice for the line
breaks it on about a third of decks). The buffed lattice is used for the
boosted total below, which is where the board's gain appears.

The three effect-up lines share ONE pool. With `E_i` the total active-skill
effect-up landing on card i (leader costume outfit + passives targeting it +
board effect-up):

    mag'_i = ceil(mag_i * (1000 + E_i) / 1000)
    tot    = the same window sum, on the BUFFED lattice, using mag'_i

    CLAMP: if tot <= A the server shows Outfit, Passive AND Board as ZERO,
    together — never a negative line, never a partial one.
    The comparison is against the BASE-lattice A, the same one the Active line
    ceils, and is taken EXACTLY (the two floats cannot straddle it: they are
    ceilings of the same numbers). Comparing against a buffed-lattice A instead
    makes every board-buffed, effect-up-free deck clamp (its boosted total
    equals its own buffed A by construction) and throws the board line away.

    base = ceil( float64(tot) ) - Active         the integer the three lines split

### The per-line SPLIT

Every displayed line is an INDEPENDENT ceiling of its share of `base`; there is
no common divisor and no fitted pedestal:

    w_i    = mag_i * P_i * dur_i / cool_i        the card's expected uptime RATE,
                                                 NOMINAL cooldown, base P
    q_o    = SUM_i outfit_i  * w_i
    q_p    = SUM_i passive_i * w_i
    q_b    = SUM_i w_i * 1000 * [ (1 + E_i/1000)(1 + u_i/1000)(1 + c_i/1000) - 1 ]
    line_s = ceil( base * q_s / (q_o + q_p + q_b) )     for each present source

Consequences:

* the lines need not sum to `ceil(tot)`: `SUM_s line_s = base + #{s : share_s
  not an integer} - SUM_s frac(share_s)`, an identity rather than a term, so no
  fitted `+ max(N-1, 0)` pedestal is needed. Independent ceilings ARE Adams'
  method at the standard divisor `(SUM q)/base`; Adams forced onto a fitted
  target is not.
* the weight is a continuous RATE, not a count of live seconds (on the exactly
  pinned ratio pairs, `mag*P*live` fails the one pair that separates them);
  it is on the NOMINAL cooldown — the shorten multiplies the card's own rate
  inside ITS board gain and must not enter the outfit/passive weights.
* the board quota is a PER-CARD product, not a product of deck-level means:
  additive within a buff kind, multiplicative across kinds — the same convention
  as the `eff3` law. It has no fitted parameter.

Strict lines, every displayed value equal, hold on about 96.5 % of unclamped
captured decks with a drawn line, and on every cooldown-shorten cohort.

⚠ **What remains is a single-source `+1`**: on decks with exactly ONE non-zero
quota the wire reads `line + 1` on about 5 % of them, always exactly +1,
deterministic in the sorted per-card `(mag, P, cd, dur, E, u, c)` multiset,
non-additive over cards, absent from both window sums and from every
multi-quota deck, and it persists under a song read.
Its formula is not known; the model does not add it. A caller that needs the
displayed total on a single-line deck should treat `nonspecial_total` as exact
or one low.

The Special line is a deck-wide multiplier on the Active INTEGER, additive over
the cards, with the special's activation-rate rider counting HALF:

    [Special Skill] = ceil( Active * SUM_i smag_i * (1 + R_i/2000) * sdur_i
                            / 120000 )

    It matches about 99.9 % of board-free decks. The misses are all decks whose
    product `Active * pool / 120000` is an exact integer; where it is
    fractional the line is never wrong. Why the server reads differently on
    some integral decks is not known: no float64/float32 evaluation order or
    reciprocal-multiplication route reproduces it. Trigger conditions on
    specials are ignored (cards with one sit on the same line).

    Score Bonus = Active + Outfit + Passive + Board + Special
    Unit Score  = ceil( OverallPower * (1000 + ScoreBonus) * 203734 / 10**8 )

The Unit Score identity is bit-exact on every captured deck — including
per-song rows — when fed the server's own Score Bonus. K = 2.03734 exactly; the
admissible interval over all rows is [2.037339999, 2.037340003] and floor/round
are infeasible for any K.

## Per-song board bonus

`song_bonus_permil` (`eff3`) adds the song's per-song (yellow) board nodes: the
per-song board line is `board + eff3 + ceil((Active + Outfit + Passive +
special_raw) * eff3 / 1000)`, with `eff3` the song's singers' own per-song bonus
(`BoardModel.song_score_bonus_for`, capped at 100 permil). It matches the game's
deck screen exactly.

## What is NOT modelled

* The single-source `+1` above, and a few multi-line decks whose exactly
  integral shares read one above on the wire.

## Resolving the inputs

`magnitude_permil` is **override-resolved by the caller**: a run-state trigger
(life/combo) uses the override magnitude; a deck-composition trigger
(type/generation) uses the override iff the count of matching members
*including the card itself* meets the trigger count; otherwise the base
magnitude. Skill level 2 iff bloom >= 1; special skill level 2 iff bloom >= 3
(bloom >= 1 for the special does not fit the captured panels). `probability_permil` is
the master's `LiveActiveSkillLevel` field 8 (Low 370 / Medium 460 / High 550),
NOT the tier label in `data/cards.json`.

`tools/recon/proxy/deck_panel_params.py` does that resolution against the
game's own master tables; this module deliberately takes plain numbers and does
no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from math import ceil
from typing import Iterable, Sequence

__all__ = [
    "PANEL_WINDOW_S",
    "UNIT_SCORE_NUMERATOR",
    "PanelCard",
    "DeckPanel",
    "deck_panel",
    "live_seconds",
    "unit_score_from_panel",
]

#: The panel's evaluation window: the first 200 whole seconds, weighted
#: uniformly (a fitted note-density weighting is strictly worse).
PANEL_WINDOW_S = 200

#: `Unit Score = ceil(OverallPower * (1000 + bonus) * 203734 / 10**8)`.
UNIT_SCORE_NUMERATOR = 203734
_UNIT_SCORE_DENOMINATOR = 100_000_000

#: The denominator floor in `v(t)`, in permil.
_PROBABILITY_FLOOR_PERMIL = 1000

#: `Special = ceil(Active * pool / 120000)` with the rider counting half.
_SPECIAL_DENOMINATOR = 120_000
_SPECIAL_RIDER_DENOMINATOR = 2000

#: THE COMBO-GATE ONSET: a combo-gated override counts at its BASE
#: magnitude for t < 20 s of the 200 s timeline (combo 40 assumed reached at
#: t = 20 — 2 combo/s). The only onset in 0..44 s that fits the captured panels.
COMBO_GATE_ONSET_S = 20

#: Whether the onset law also gates the BOOSTED pass (`tot_raw`). The base pass
#: (Active line) is confirmed; whether the boosted side is gated is not known —
#: flip to compare hypotheses against captured totals.
GATE_BOOSTED_PASS = True


@dataclass(frozen=True, slots=True)
class PanelCard:
    """One member's panel inputs. Every field is an integer permil or second.

    The three `*_effect_up_permil` fields are kept apart ONLY so the model can
    count how many panel lines are non-zero (the `N - 1` term) and attribute
    the pool; the magnitude boost uses their sum.
    """

    magnitude_permil: int
    """Override-resolved active score-up magnitude (see the module docstring)."""

    probability_permil: int
    """`LiveActiveSkillLevel` field 8 — 370 / 460 / 550."""

    cooldown_s: int
    duration_s: int

    outfit_effect_up_permil: int = 0
    """Leader costume active-skill effect-up landing on this card."""

    passive_effect_up_permil: int = 0
    """Members' passive active-skill effect-up targeting this card."""

    board_effect_up_permil: int = 0
    """Board (leader-scope red node) active-skill effect-up on this card."""

    board_probability_up_permil: int = 0
    """`u_i` — board activation-probability-up."""

    board_cooldown_shorten_permil: int = 0
    """`c_i` — board cooldown-shorten."""

    special_magnitude_permil: int = 0
    special_duration_s: int = 0
    special_rate_rider_permil: int = 0
    """`R_i` — the special's activation-rate rider; it counts HALF."""

    combo_base_magnitude_permil: int | None = None
    """THE COMBO-GATE ONSET LAW:
    when `magnitude_permil` is a COMBO-gated override, the panel's 200 s
    timeline uses the BASE magnitude for `t < 20 s` and the override from
    `t >= 20 s` (combo 40 assumed reached at t = 20). Set this to the BASE
    magnitude for such a card; `None` (the default) keeps the magnitude
    constant over the whole timeline — LIFE-gated overrides and plain
    magnitudes stay `None`. No captured card has a combo-gated override with
    cd < 20, so for those decks the default and the onset law agree."""

    @property
    def effect_up_permil(self) -> int:
        """`E_i`: every active-skill effect-up landing on this card."""
        return (
            self.outfit_effect_up_permil
            + self.passive_effect_up_permil
            + self.board_effect_up_permil
        )

    @property
    def has_board_source(self) -> bool:
        """Board effect-up and lattice buffs are ONE panel line, not two."""
        return bool(
            self.board_effect_up_permil
            or self.board_probability_up_permil
            or self.board_cooldown_shorten_permil
        )


@dataclass(frozen=True, slots=True)
class DeckPanel:
    """The five panel lines, the Score Bonus and the Unit Score, as integers.

    `a_raw` and `tot_raw` are the UNROUNDED exact quantities the lines are
    derived from — kept so a caller can see how close a line was to tipping.
    The displayed integers are ceilings of the server's FLOAT64 accumulation
    of the same sums (module docstring), which differs from the exact ceiling
    only on integral-valued decks.

    ⚠ `outfit`, `passive` and `board` are independent ceilings of their shares
    of `base` (about 96.5 % strict on captured decks); on a deck with exactly one
    non-zero quota the wire may read one above (`+1`, about 5 %).
    """

    active: int
    outfit: int
    passive: int
    board: int
    special: int
    score_bonus_permil: int
    unit_score: int
    a_raw: Fraction
    """`A` — the base-lattice window average, unrounded (exact)."""
    tot_raw: Fraction
    """`tot` — the boosted, buffed-lattice window average, unrounded (exact)."""
    clamped: bool
    """True when `tot <= A` zeroed the three effect-up lines together."""

    @property
    def nonspecial_total(self) -> int:
        """`Active + Outfit + Passive + Board` — the sum of the displayed lines."""
        return self.active + self.outfit + self.passive + self.board


def live_seconds(
    *, cooldown_s: int, duration_s: int, shorten_permil: int = 0
) -> tuple[int, ...]:
    """The whole seconds in `1..PANEL_WINDOW_S` at which a card is live.

    `cool' = cooldown_s * 1000 / (1000 + shorten_permil)` is kept as an exact
    `Fraction`; the window `[k*cool', k*cool' + dur)` is then intersected with
    the integers. Rounding `cool'` to a whole second first is wrong and shows
    up immediately in the Active line.
    """
    if cooldown_s <= 0:
        raise ValueError(f"cooldown_s must be positive, got {cooldown_s!r}")
    if duration_s < 0:
        raise ValueError(f"duration_s must not be negative, got {duration_s!r}")
    cool = Fraction(cooldown_s * 1000, 1000 + shorten_permil)
    out: list[int] = []
    k = 1
    while k * cool <= PANEL_WINDOW_S:
        start = k * cool
        low = max(1, ceil(start))
        # t < start + dur, so the last integer is ceil(start + dur) - 1 whether
        # or not the endpoint is itself an integer.
        high = min(PANEL_WINDOW_S, ceil(start + duration_s) - 1)
        out.extend(range(low, high + 1))
        k += 1
    return tuple(out)


def _second_terms(
    cards: Sequence[PanelCard], magnitudes: Sequence[int], *, buffed: bool,
    pre20_magnitudes: Sequence[int] | None = None,
) -> list[tuple[int, int]]:
    """`(numerator, denominator)` of `v(t)` for every LIVE second, in timeline
    order — the exact integers behind both the exact and the float64 sums.

    `P'` is carried as the integer `Q = P * (1000 + u)` (i.e. `P' * 1000`), so
    `v(t) = SUM mag*Q / max(10**6, SUM Q)` is a ratio of integers and the
    saturation floor stays exact. Seconds where no card is live contribute
    nothing to either sum and are omitted.

    `pre20_magnitudes` is the combo-gate onset law: when given, seconds
    `t < COMBO_GATE_ONSET_S` read magnitudes from it instead (the combo-gated
    overrides at their BASE value). `None` is bit-identical to passing
    `magnitudes` itself.
    """
    q = [
        1000
        * -(
            -c.probability_permil
            * (1000 + (c.board_probability_up_permil if buffed else 0))
            // 1000
        )
        for c in cards
    ]
    live: list[list[int]] = [[] for _ in range(PANEL_WINDOW_S + 1)]
    for i, card in enumerate(cards):
        for t in live_seconds(
            cooldown_s=card.cooldown_s,
            duration_s=card.duration_s,
            shorten_permil=card.board_cooldown_shorten_permil if buffed else 0,
        ):
            live[t].append(i)
    floor = _PROBABILITY_FLOOR_PERMIL * 1000
    seen: dict[tuple, tuple[int, int]] = {}
    terms: list[tuple[int, int]] = []
    for t in range(1, PANEL_WINDOW_S + 1):
        key = tuple(live[t])
        if not key:
            continue
        early = pre20_magnitudes is not None and t < COMBO_GATE_ONSET_S
        mags = pre20_magnitudes if early else magnitudes
        memo_key = (key, early)
        term = seen.get(memo_key)
        if term is None:
            term = (sum(mags[i] * q[i] for i in key), max(floor, sum(q[i] for i in key)))
            seen[memo_key] = term
        terms.append(term)
    return terms


def _exact_window_average(terms: Sequence[tuple[int, int]]) -> Fraction:
    """`SUM_{t=1..200} v(t) / 200`, EXACTLY (reported as `a_raw` / `tot_raw` and
    used for the clamp). Distinct terms are memoized: a deck's live set repeats
    with the cooldown period, so most seconds share a value.
    """
    total = Fraction(0)
    seen: dict[tuple[int, int], Fraction] = {}
    for term in terms:
        value = seen.get(term)
        if value is None:
            value = Fraction(term[0], term[1])
            seen[term] = value
        total += value
    return total / PANEL_WINDOW_S


def _window_average(
    cards: Sequence[PanelCard], magnitudes: Sequence[int], *, buffed: bool,
    pre20_magnitudes: Sequence[int] | None = None,
) -> Fraction:
    """`_exact_window_average` straight from the cards. `deck_panel` builds the
    terms once and takes both the exact and the float64 sum of them, so this is
    the convenience form for anything outside that path."""
    return _exact_window_average(
        _second_terms(cards, magnitudes, buffed=buffed, pre20_magnitudes=pre20_magnitudes)
    )


def _forward_float_sum(terms: Sequence[tuple[int, int]]) -> float:
    """The server's accumulation: `s = 0.0; for each live second s += n/d; s / 200`.

    One correctly-rounded float64 quotient per live second, added in timeline
    order, divided by 200 last: the order that fits the captured panels (module
    docstring).
    """
    s = 0.0
    for n, d in terms:
        s += n / d
    return s / PANEL_WINDOW_S


def _float_window_average(
    cards: Sequence[PanelCard], magnitudes: Sequence[int], *, buffed: bool,
    pre20_magnitudes: Sequence[int] | None = None,
) -> float:
    """`_window_average` as the server computes it, in float64."""
    return _forward_float_sum(
        _second_terms(cards, magnitudes, buffed=buffed, pre20_magnitudes=pre20_magnitudes)
    )


def _attribution_weights(cards: Sequence[PanelCard]) -> list[Fraction]:
    """`w_i = mag_i * P_i * dur_i / cool_i` — the card's expected uptime RATE on
    the NOMINAL cooldown, as an exact Fraction.

    Base magnitude, base `P` (the board's probability-up is not in the weight),
    nominal cooldown (the board's shorten is not in the weight either: it
    multiplies the card's own rate inside its board gain, see `_split_quotas`).
    There is no saturation clamp and no truncation to whole windows or whole
    seconds: on the exactly pinned ratio pairs only the continuous rate fits
    all of them — the one pair that separates them needs `w_p/w_c` in
    [0.8462, 1.0128) and the rate gives 1.0119 where `mag*P*live` gives
    1.0492.
    """
    return [
        Fraction(c.magnitude_permil * c.probability_permil * c.duration_s, c.cooldown_s)
        for c in cards
    ]


def _split_quotas(cards: Sequence[PanelCard]) -> list[Fraction]:
    """The three sources' quotas `(q_outfit, q_passive, q_board)` as exact
    Fractions — only their RATIOS matter to the split.

        q_outfit  = SUM_i outfit_i  * w_i
        q_passive = SUM_i passive_i * w_i
        q_board   = SUM_i w_i * 1000 * [ (1+E_i/1000)(1+u_i/1000)(1+c_i/1000) - 1 ]

    ## The board quota is a PER-CARD product

    Outfit and passive each have one buff kind, so their quota is the plain
    `w`-weighted sum. The board has three — effect-up, probability-up and
    cooldown-shorten — and they do **not** add: each card's board gain is the
    multiplicative excess of its own three buffs, weighted by its own rate.
    Additive within a source, multiplicative across sources — the convention the
    `eff3` law follows.

    Why this is a mechanism and not a fit:

    * **Zero fitted parameters.** Weighting by the SHORTENED rate, or by a
      product of deck-level mean permils, makes the shorten look concave; on the
      unshortened per-card rate the concavity is gone.
    * **It predicts.** A 251-permil effect-up pedestal (Calliope leading)
      predicts the probability-up's coefficient becomes `1 + E/1000 = 1.251`;
      prob-up-only decks spanning an 8x range of E/P give 1.250, flat across
      every band. Without the pedestal the same form predicts 1.000 and a
      matched control gives 1.015.
    * **Every cooldown-shorten cohort fits** once the weight is the unshortened
      rate and the board gain is per card. A deck-level mean form loses
      three-line decks because the passive quota must shrink when the shorten
      carrier is a passive target and grow when it is not — exactly what the
      per-card weight does and no deck-level mean can.

    Per-card `(1+u_i)(1+c_i)` versus a product of deck means is indistinguishable
    while the board effect-up is leader-scope (uniform on every deck); the
    per-card form is the statement.
    """
    w = _attribution_weights(cards)
    q_outfit = sum((c.outfit_effect_up_permil * wi for c, wi in zip(cards, w)), Fraction(0))
    q_passive = sum((c.passive_effect_up_permil * wi for c, wi in zip(cards, w)), Fraction(0))
    q_board = Fraction(0)
    for c, wi in zip(cards, w):
        gain = (
            (1000 + c.board_effect_up_permil)
            * (1000 + c.board_probability_up_permil)
            * (1000 + c.board_cooldown_shorten_permil)
            - 1000 ** 3
        )
        # gain / 1000**2 is `1000 * [(1+E)(1+u)(1+c) - 1]` in permil
        q_board += wi * Fraction(gain, 1000 ** 2)
    return [q_outfit, q_passive, q_board]


def _independent_ceilings(quotas: Sequence[Fraction], base: int) -> list[int]:
    """`line_s = ceil(base * q_s / SUM q)` for every source, each on its own.

    The lines do NOT re-sum to `base` in general: `SUM ceil(x_s) = base +
    #{non-integral shares} - SUM frac(x_s)` (an integer). That identity is the
    observed pedestal, which is why a fixed `+max(N-1, 0)` target is wrong on a
    minority of decks and Adams forced onto it can never be exact.
    """
    total = sum(quotas, Fraction(0))
    if total <= 0 or base <= 0:
        return [0] * len(quotas)
    return [ceil(base * q / total) if q > 0 else 0 for q in quotas]


def _adams(quotas: Sequence[int], target: int) -> list[int] | None:
    """Adams' method (smallest divisors): `line_s = ceil(kappa * q_s)` with
    `kappa` DETERMINED by `SUM line_s == target`.

    ⚠ Not called by `deck_panel`: the server ceils each share of `base` on its
    own (`_independent_ceilings`) and does not force a target. Kept as a
    reference implementation.

    Implemented as the equivalent sequential divisor allocation — every unit with
    a positive quota takes a seat first, then each further seat goes to the
    largest `q_s / a_s`. Exact integer arithmetic; ties break on source order.

    Returns `None` when the method cannot apply (no positive quota, or fewer
    seats than units).
    """
    live = [i for i, q in enumerate(quotas) if q > 0]
    if not live or target < len(live):
        return None
    alloc = [0] * len(quotas)
    for i in live:
        alloc[i] = 1
    for _ in range(target - len(live)):
        # argmax of q_i / a_i, compared by cross-multiplication to stay integral
        best = live[0]
        for i in live[1:]:
            if quotas[i] * alloc[best] > quotas[best] * alloc[i]:
                best = i
        alloc[best] += 1
    return alloc


def _largest_remainder(total: int, weights: Sequence[int]) -> list[int]:
    """Split `total` across `weights` so the parts sum to `total` exactly.

    Retained as the FALLBACK for the degenerate case where every displayed
    source is weightless (every carrier dead on the lattice, `SUM q = 0`), so
    the parts still sum exactly and a present source still shows.
    """
    pool = sum(weights)
    if total <= 0 or pool <= 0:
        return [0] * len(weights)
    exact = [Fraction(total * w, pool) for w in weights]
    parts = [int(x) for x in exact]
    short = total - sum(parts)
    order = sorted(
        range(len(weights)), key=lambda i: (exact[i] - parts[i], weights[i]), reverse=True
    )
    for i in order[:short]:
        parts[i] += 1
    return parts


def unit_score_from_panel(overall_power: int, score_bonus_permil: int) -> int:
    """`ceil(OverallPower * (1000 + bonus) * 203734 / 10**8)` — bit-exact
    against the server's own Unit Score."""
    numerator = (
        int(overall_power) * (1000 + int(score_bonus_permil)) * UNIT_SCORE_NUMERATOR
    )
    return -(-numerator // _UNIT_SCORE_DENOMINATOR)


def deck_panel(
    cards: Iterable[PanelCard], *, overall_power: int = 0, song_bonus_permil: int = 0
) -> DeckPanel:
    """Compute the panel for one deck. See the module docstring for every law.

    `overall_power` is the server's Overall Power for the deck; it only feeds
    `unit_score`, and defaults to 0 so the bonus lines can be computed without
    it. `song_bonus_permil` (`eff3`) is the song's per-song board bonus -- the
    singers' own yellow nodes, on any team -- folded into the Board line by the
    per-song law (module docstring); 0 = the song-free panel.
    """
    members = tuple(cards)
    if not members:
        return DeckPanel(
            active=0,
            outfit=0,
            passive=0,
            board=0,
            special=0,
            score_bonus_permil=0,
            unit_score=unit_score_from_panel(overall_power, 0),
            a_raw=Fraction(0),
            tot_raw=Fraction(0),
            clamped=True,
        )

    base_magnitudes = [c.magnitude_permil for c in members]
    gated = any(c.combo_base_magnitude_permil is not None for c in members)
    pre20 = (
        [c.combo_base_magnitude_permil
         if c.combo_base_magnitude_permil is not None else c.magnitude_permil
         for c in members]
        if gated else None
    )
    base_terms = _second_terms(members, base_magnitudes, buffed=False, pre20_magnitudes=pre20)
    a_raw = _exact_window_average(base_terms)
    active = ceil(_forward_float_sum(base_terms))

    boosted = [
        ceil(Fraction(c.magnitude_permil * (1000 + c.effect_up_permil), 1000))
        for c in members
    ]
    boosted_pre20 = (
        [ceil(Fraction(m * (1000 + c.effect_up_permil), 1000))
         for m, c in zip(pre20, members)]
        if gated and GATE_BOOSTED_PASS else None
    )
    boosted_terms = _second_terms(members, boosted, buffed=True, pre20_magnitudes=boosted_pre20)
    tot_raw = _exact_window_average(boosted_terms)

    outfit_w = sum(c.outfit_effect_up_permil for c in members)
    passive_w = sum(c.passive_effect_up_permil for c in members)
    board_w = sum(
        c.board_effect_up_permil
        + c.board_probability_up_permil
        + c.board_cooldown_shorten_permil
        for c in members
    )
    present = (
        bool(outfit_w),
        bool(passive_w),
        any(c.has_board_source for c in members),
    )
    clamped = tot_raw <= a_raw
    if clamped:
        outfit = passive = board = 0
    else:
        base = ceil(_forward_float_sum(boosted_terms)) - active

        quotas = [q if present[i] else Fraction(0) for i, q in enumerate(_split_quotas(members))]

        if sum(quotas, Fraction(0)) > 0:
            alloc = _independent_ceilings(quotas, base)
        else:
            alloc = _largest_remainder(
                base,
                [
                    outfit_w if present[0] else 0,
                    passive_w if present[1] else 0,
                    board_w if present[2] else 0,
                ],
            )
        outfit, passive, board = alloc

    pool = sum(
        Fraction(
            c.special_magnitude_permil
            * c.special_duration_s
            * (_SPECIAL_RIDER_DENOMINATOR + c.special_rate_rider_permil),
            _SPECIAL_RIDER_DENOMINATOR,
        )
        for c in members
    )
    special_raw = active * pool / _SPECIAL_DENOMINATOR if pool else Fraction(0)
    special = ceil(special_raw) if pool else 0
    if song_bonus_permil:
        e = int(song_bonus_permil)
        board += e + ceil((active + outfit + passive + special_raw) * e / 1000)

    score_bonus = active + outfit + passive + board + special
    return DeckPanel(
        active=active,
        outfit=outfit,
        passive=passive,
        board=board,
        special=special,
        score_bonus_permil=score_bonus,
        unit_score=unit_score_from_panel(overall_power, score_bonus),
        a_raw=a_raw,
        tot_raw=tot_raw,
        clamped=clamped,
    )
