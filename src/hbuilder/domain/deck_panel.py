"""The deck screen's **Unit Score Details** panel, from card parameters alone.

An opt-in second model, separate from the ranking objective (`total_score`,
`deck_screen_unit_score` are untouched): it computes the five panel lines the server shows
for an arbitrary hypothetical deck (unowned cards, maxed levels, a board the account does
not have), because every input is a plain number rather than a captured server response.
The laws below were extracted from captured `SaveDeck` -> `GetDeck` panels; a quoted hit
rate is the share of captured decks on which the law's integer equals the server's.

Arithmetic is exact (`Fraction`) except the two window sums, which the server accumulates
in float64: the displayed integers are their ceilings, and on integral-valued decks the
float lands on the far side of the integer, so the model repeats the server's order.
`a_raw` and `tot_raw` are reported exact.

## The laws

Per member card `i`, all permil integers unless noted, `c_i` the board's cooldown-shorten
and `u_i` its activation-probability-up:

    cool'_i = cool_i * 1000 / (1000 + c_i)          exact Fraction, never rounded
    P'_i    = ceil( P_i * (1000 + u_i) / 1000 )     whole permil, rounded UP

The server stores `P'` as a whole permil (a no-op when `u_i = 0`); the rounded value is
used in the numerator and in the saturation denominator alike. Keeping it exact reads
`tot` about 0.06 low and loses an integer where `tot` sits just under one. The captured
`(P, u)` pairs only pin the cut at <= 0.36 (it is not round-half-up, not floor).

Card `i` is live at integer second t iff `k * cool'_i <= t < k * cool'_i + dur_i` for
some integer k >= 1 (t = 0 is never live), over the first `PANEL_WINDOW_S` = 200 seconds:

    v(t) = SUM_{i live at t} mag_i * P'_i / max(1000, SUM_{i live at t} P'_i)
    A    = SUM_{t=1..200} v(t) / 200
    [Active Skill] = ceil( float64(A) )            bit-exact on every captured deck

The float64 sums: one quotient `n_t / d_t` per live second (the exact integers of `v(t)`,
`d_t = max(10**6, SUM P'*1000)`), added in timeline order, divided by 200 last. It differs
from the exact ceiling only where the exact sum is an integer (the float lands a few ulp
above); other orders fit the boosted total worse.

The [Active Skill] line is on the BASE lattice (`u_i = c_i = 0`): the server books the
board's lattice gain under [Holomem Board]. The three effect-up lines share one pool, with
`E_i` the active-skill effect-up landing on card i (outfit + passives + board):

    mag'_i = ceil(mag_i * (1000 + E_i) / 1000)
    tot    = the same window sum, on the BUFFED lattice, using mag'_i
    if tot <= A (exact, base-lattice A): Outfit, Passive and Board all show 0
    base   = ceil( float64(tot) ) - Active       the integer the three lines split

(Comparing against a buffed-lattice A would clamp every board-only deck, whose boosted
total equals its own buffed A.)

### The per-line split

Each displayed line is an independent ceiling of its share of `base`:

    w_i    = mag_i * P_i * dur_i / cool_i        expected uptime rate, nominal cooldown, base P
    q_o    = SUM_i outfit_i  * w_i
    q_p    = SUM_i passive_i * w_i
    q_b    = SUM_i w_i * 1000 * [ (1 + b_i/1000)(1 + u_i/1000)(1 + c_i/1000) - 1 ]
    line_s = ceil( base * q_s / (q_o + q_p + q_b) )     for each present source

with `outfit_i` and `passive_i` the effect-up the outfit and the passives give card i, and
`b_i`, `u_i`, `c_i` the board's own effect-up, probability-up and cooldown shorten on it
(the board part of `E_i` only).

The lines need not sum to `base` (independent ceilings are Adams' method at the standard
divisor). The weight is a continuous rate on the nominal cooldown (the shorten enters only
the card's own board gain); the board quota is a per-card product, additive within a buff
kind and multiplicative across kinds, as in the `eff3` law. Every line holds on about
96.5 % of unclamped captured decks. Not modelled: on decks with exactly one non-zero quota
the wire reads `line + 1` on about 5 % (deterministic, formula unknown), so a single-line
deck's displayed total is exact or one low.

The Special line multiplies the Active integer, additive over cards, the special's
activation-rate rider counting half (a special whose composition gate is not met loses its
rider and keeps its magnitude, `deck_panel_team._rider_permil`):

    [Special Skill] = ceil( Active * SUM_i smag_i * (1 + R_i/2000) * sdur_i / 120000 )

It matches about 99.9 % of board-free decks; the misses are all decks where the product is
an exact integer, and no float evaluation order reproduces them.

    Score Bonus = Active + Outfit + Passive + Board + Special
    Unit Score  = ceil( OverallPower * (1000 + ScoreBonus) * 203734 / 10**8 )

The Unit Score identity is bit-exact on every captured deck given the server's Score Bonus
(K = 2.03734; floor and round are infeasible for any K).

## Per-song board bonus

`song_bonus_permil` (`eff3`, the song's singers' own yellow nodes,
`BoardModel.song_score_bonus_for`, capped at 100 permil) adds
`ceil(eff3 + (Active + Outfit + Passive + special_raw) * eff3 / 1000)` to the Board line,
`eff3` kept exact (it can be 55.5) under the one ceiling. A singer's conditional skill
effect-up (red R-002, "when X is included as a singer...") is not part of `eff3`: the
caller adds it to `board_effect_up_permil` first, so it moves the Board line through the
boosted window sum.

## Resolving the inputs

`magnitude_permil` is override-resolved by the caller: a run-state trigger (life/combo)
uses the override; a deck-composition trigger (type/generation) uses it iff the matching
members, the card itself included, meet the trigger count. Skill level 2 iff bloom >= 1;
special skill level 2 iff bloom >= 3. `probability_permil` is the master's
`LiveActiveSkillLevel` field 8 (Low 370 / Medium 460 / High 550), not the tier label in
`data/cards.json`. `tests/support/deck_panel_params.py` does that resolution against the
master tables; this module takes plain numbers and does no I/O.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from fractions import Fraction
from math import ceil

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

#: The combo-gate onset: a combo-gated override counts at its BASE magnitude for
#: t < 20 s of the 200 s timeline (combo 40 reached at t = 20, 2 combo/s). The only
#: onset in 0..44 s that fits the captured panels.
COMBO_GATE_ONSET_S = 20

#: Whether the onset also gates the boosted pass (`tot_raw`). The Active line's gating
#: fits the captured panels; the boosted side's is not pinned by them.
GATE_BOOSTED_PASS = True


@dataclass(frozen=True, slots=True)
class PanelCard:
    """One member's panel inputs. Every field is an integer permil or second.

    The three `*_effect_up_permil` fields are kept apart only so the model can tell
    which panel lines are present and split the pool; the magnitude boost uses their sum.
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
        return self.outfit_effect_up_permil + self.passive_effect_up_permil + self.board_effect_up_permil

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

    `a_raw` and `tot_raw` are the unrounded exact quantities the lines come from, so a
    caller can see how close a line was to tipping (the displayed integers are ceilings
    of the float64 sums, module docstring). On a deck with exactly one non-zero quota the
    wire may read one above `outfit`, `passive` or `board`.
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


def live_seconds(*, cooldown_s: int, duration_s: int, shorten_permil: int = 0) -> tuple[int, ...]:
    """The whole seconds in `1..PANEL_WINDOW_S` at which a card is live.

    `cool' = cooldown_s * 1000 / (1000 + shorten_permil)` is an exact `Fraction`; the
    window `[k*cool', k*cool' + dur)` is intersected with the integers. Rounding `cool'`
    to a whole second first breaks the Active line.
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
    cards: Sequence[PanelCard],
    magnitudes: Sequence[int],
    *,
    buffed: bool,
    pre20_magnitudes: Sequence[int] | None = None,
) -> list[tuple[int, int]]:
    """`(numerator, denominator)` of `v(t)` for every live second, in timeline order:
    the exact integers behind both the exact and the float64 sums.

    `P'` is carried as the integer `Q = P' * 1000`, so `v(t) = SUM mag*Q / max(10**6,
    SUM Q)` is a ratio of integers. Seconds with no live card are omitted.
    `pre20_magnitudes`, when given, replaces `magnitudes` for `t < COMBO_GATE_ONSET_S`.
    """
    q = [
        1000 * -(-c.probability_permil * (1000 + (c.board_probability_up_permil if buffed else 0)) // 1000)
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
            term = (sum(mags[i] * q[i] for i in key), max(floor, sum(q[i] for i in key)))  # type: ignore[index, misc]
            seen[memo_key] = term
        terms.append(term)
    return terms


def _exact_window_average(terms: Sequence[tuple[int, int]]) -> Fraction:
    """`SUM_{t=1..200} v(t) / 200`, exactly (`a_raw` / `tot_raw`, and the clamp).
    Distinct terms are memoized: the live set repeats with the cooldown period.
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


def _forward_float_sum(terms: Sequence[tuple[int, int]]) -> float:
    """The server's accumulation: `s = 0.0; for each live second s += n/d; s / 200`.

    The order that fits the captured panels (module docstring).
    """
    s = 0.0
    for n, d in terms:
        s += n / d
    return s / PANEL_WINDOW_S


def _attribution_weights(cards: Sequence[PanelCard]) -> list[Fraction]:
    """`w_i = mag_i * P_i * dur_i / cool_i`: the card's expected uptime rate, exact.

    Base magnitude, base `P` and nominal cooldown: the board's probability-up and
    shorten enter only the card's own board gain (`_split_quotas`). No saturation clamp
    and no truncation to whole windows or seconds: on the exactly pinned ratio pairs only
    the continuous rate fits all of them (a live-second count `mag*P*live` does not).
    """
    return [Fraction(c.magnitude_permil * c.probability_permil * c.duration_s, c.cooldown_s) for c in cards]


def _split_quotas(cards: Sequence[PanelCard]) -> list[Fraction]:
    """The three sources' quotas `(q_outfit, q_passive, q_board)` as exact Fractions;
    only their ratios matter to the split.

        q_outfit  = SUM_i outfit_i  * w_i
        q_passive = SUM_i passive_i * w_i
        q_board   = SUM_i w_i * 1000 * [ (1+E_i/1000)(1+u_i/1000)(1+c_i/1000) - 1 ]

    The board has three buff kinds (effect-up, probability-up, cooldown-shorten) and they
    do not add: each card's board gain is the multiplicative excess of its own three
    buffs, weighted by its own unshortened rate. A deck-level mean form fails three-line
    decks: the passive quota must shrink when the shorten carrier is a passive target and
    grow when it is not, which only the per-card weight does. The form has no fitted
    parameter.
    """
    w = _attribution_weights(cards)
    q_outfit = sum((c.outfit_effect_up_permil * wi for c, wi in zip(cards, w)), Fraction(0))
    q_passive = sum((c.passive_effect_up_permil * wi for c, wi in zip(cards, w)), Fraction(0))
    q_board = Fraction(0)
    for c, wi in zip(cards, w):
        gain = (1000 + c.board_effect_up_permil) * (1000 + c.board_probability_up_permil) * (
            1000 + c.board_cooldown_shorten_permil
        ) - 1000**3
        # gain / 1000**2 is `1000 * [(1+E)(1+u)(1+c) - 1]` in permil
        q_board += wi * Fraction(gain, 1000**2)
    return [q_outfit, q_passive, q_board]


def _independent_ceilings(quotas: Sequence[Fraction], base: int) -> list[int]:
    """`line_s = ceil(base * q_s / SUM q)` for every source, each on its own.

    The lines do not re-sum to `base` in general: `SUM ceil(x_s) = base +
    #{non-integral shares} - SUM frac(x_s)`, which is why no fixed target total fits.
    """
    total = sum(quotas, Fraction(0))
    if total <= 0 or base <= 0:
        return [0] * len(quotas)
    return [ceil(base * q / total) if q > 0 else 0 for q in quotas]


def _largest_remainder(total: int, weights: Sequence[int]) -> list[int]:
    """Split `total` across `weights` so the parts sum to `total` exactly.

    The fallback when every displayed source is weightless (`SUM q = 0`), so a present
    source still shows.
    """
    pool = sum(weights)
    if total <= 0 or pool <= 0:
        return [0] * len(weights)
    exact = [Fraction(total * w, pool) for w in weights]
    parts = [int(x) for x in exact]
    short = total - sum(parts)
    order = sorted(range(len(weights)), key=lambda i: (exact[i] - parts[i], weights[i]), reverse=True)
    for i in order[:short]:
        parts[i] += 1
    return parts


def unit_score_from_panel(overall_power: int, score_bonus_permil: int) -> int:
    """`ceil(OverallPower * (1000 + bonus) * 203734 / 10**8)` — bit-exact
    against the server's own Unit Score."""
    numerator = int(overall_power) * (1000 + int(score_bonus_permil)) * UNIT_SCORE_NUMERATOR
    return -(-numerator // _UNIT_SCORE_DENOMINATOR)


def deck_panel(
    cards: Iterable[PanelCard],
    *,
    overall_power: int = 0,
    song_bonus_permil: int | float | Fraction = 0,
) -> DeckPanel:
    """Compute the panel for one deck (the laws are in the module docstring).

    `overall_power` only feeds `unit_score` (0 computes the bonus lines without it).
    `song_bonus_permil` (`eff3`) is the song's per-song board bonus, folded into the
    Board line; 0 = the song-free panel.
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
        [
            c.combo_base_magnitude_permil if c.combo_base_magnitude_permil is not None else c.magnitude_permil
            for c in members
        ]
        if gated
        else None
    )
    base_terms = _second_terms(members, base_magnitudes, buffed=False, pre20_magnitudes=pre20)
    a_raw = _exact_window_average(base_terms)
    active = ceil(_forward_float_sum(base_terms))

    boosted = [ceil(Fraction(c.magnitude_permil * (1000 + c.effect_up_permil), 1000)) for c in members]
    boosted_pre20 = (
        [ceil(Fraction(m * (1000 + c.effect_up_permil), 1000)) for m, c in zip(pre20, members)]  # type: ignore[arg-type]
        if gated and GATE_BOOSTED_PASS
        else None
    )
    boosted_terms = _second_terms(members, boosted, buffed=True, pre20_magnitudes=boosted_pre20)
    tot_raw = _exact_window_average(boosted_terms)

    outfit_w = sum(c.outfit_effect_up_permil for c in members)
    passive_w = sum(c.passive_effect_up_permil for c in members)
    board_w = sum(
        c.board_effect_up_permil + c.board_probability_up_permil + c.board_cooldown_shorten_permil
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
        # eff3 is fractional on the wire (a float32: 55.5 on a solo whose nodes
        # sum to it), so it is kept exact and the whole per-song term pays ONE
        # ceiling; rounding eff3 first reads one permil low on such a song
        e = Fraction(song_bonus_permil).limit_denominator(1000)
        board += ceil(e + (active + outfit + passive + special_raw) * e / 1000)

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
