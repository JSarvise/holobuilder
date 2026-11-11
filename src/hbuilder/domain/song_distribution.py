"""The exact LAW of a team's run score on one chart — not just its expectation.

`song.score_song_detailed` returns the expectation of the run score under the
ACTIVE activation roll. This returns the whole distribution of the same model:
min, max, mean, sd, percentiles and a binned probability mass function.

What is random and what is not (the same model, nothing added):

    ACTIVE activation   one independent Bernoulli(p) per cooldown window — the
                        ONLY randomness in the model;
    SPECIAL cue times   fixed by the chart, at the slot the member sits in;
    PASSIVE / COSTUME   live for the whole run;
    judgement           outside the model: the full-perfect premise (or the
                        AUTO column when `cfg.song_auto`), as `score_song`.

The method is exact, not sampled. A note is covered by at most one window per skill,
so its value depends on the fired flags of the windows covering it. Enumerating each
group of windows that share notes (2^k) is too slow (real teams reach 22+ windows), so
the module sweeps the notes in time order, carrying the law per set of open windows (at
most one per ACTIVE skill): exact, and linear in the notes.

The per-note terms are built by the same steps `score_song_detailed` takes for one
leader at one slot order, and `expected_total` recomputes the scorer's own number from
them; the caller compares the two (`verified`) and reports a disagreement.

`deck_power` is REQUIRED: the caller passes the power its own figure used
(`SongScore.deck_power`), so the law is on the row's basis — Overall Power on
the default `song_absolute_deck_power`.
"""

from __future__ import annotations

import math
from bisect import bisect_left
from collections.abc import Sequence

from hbuilder.domain.card import Card
from hbuilder.domain.gates import resolve_card_set_gates, resolve_leader_costume_skills
from hbuilder.domain.scoring import (
    _board_bonus,
    _fold_activation_rate_up,
    _is_score_up_effect,
    _pool_for,
    _resolve_skill,
    active_magnitude_by_card,
)
from hbuilder.domain.scoring_config import ScoringConfig
from hbuilder.domain.song import (
    SCORE_COEFFICIENT,
    SongChart,
    _chart_weights,
    _combo_split,
    _dominated_riders,
    _ratio_at,
    _rider_terms,
    _support_state,
    _term_for,
)

__all__ = ["best_of_n", "pace_cuts", "run_score_distribution", "score_pace"]

PERCENTILES = (0.01, 0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.99)


def _terms(members: tuple[Card, ...], leader: Card, chart: SongChart, cfg: ScoringConfig):
    """`(terms, score_rates, board_score_rate, board_skill_effect_up)` exactly as
    `score_song_detailed` builds them for `leader`."""
    times = tuple(note.time for note in chart.notes)
    song_seconds = times[-1]
    gates = resolve_card_set_gates(members, cfg.run_state)
    normal = []
    for card in members:
        normal.extend(gates.normal_by_card_id[card.id])
    team_rate_up = _fold_activation_rate_up(tuple(normal), cfg)
    active_rank = active_magnitude_by_card(normal)
    index_of = {card.id: i for i, card in enumerate(members)}
    terms = []
    score_rates = []
    for resolved in normal:
        pool = _pool_for(resolved)
        if pool in ("none", "stat"):
            continue
        rate, state, _missing = _resolve_skill(resolved, cfg, team_rate_up)
        weight = cfg.family_weights[resolved.family]
        index = index_of[resolved.card_id]
        if pool == "support":
            s_state, s_targets = _support_state(resolved, members, cfg, state, active_rank)
            terms.extend(
                _combo_split(
                    resolved,
                    cfg,
                    _term_for(resolved, cfg, team_rate_up, s_state, song_seconds, index, s_targets),
                    times,
                )
            )
        elif _is_score_up_effect(resolved):
            terms.extend(
                _combo_split(
                    resolved, cfg, _term_for(resolved, cfg, team_rate_up, state, song_seconds, index), times
                )
            )
        else:
            score_rates.append(rate * weight)
    # the SPECIAL activation-rate riders, in the scorer's place (after the members)
    terms.extend(_rider_terms(normal, index_of, terms, cfg, chart, times))
    _power, board_score_rate, board_skill_effect_up = _board_bonus(leader, members, cfg)
    if cfg.board is not None and chart.singers:
        board_score_rate = cfg.board.song_score_bonus_for(chart.singers, scope=chart.singer_type)
        board_skill_effect_up += cfg.board.conditional_skill_effect_up_for(
            members, leader.member, chart.singers
        )
    for costume in resolve_leader_costume_skills(leader, gates.aggregates, cfg.run_state):
        pool = _pool_for(costume)
        if pool in ("none", "stat"):
            continue
        rate, state, _missing = _resolve_skill(costume, cfg, team_rate_up)
        weight = cfg.family_weights[costume.family]
        if pool == "support":
            s_state, s_targets = _support_state(costume, members, cfg, state, active_rank)
            terms.append(_term_for(costume, cfg, team_rate_up, s_state, song_seconds, None, s_targets))
        elif pool == "score":
            if _is_score_up_effect(costume):
                terms.append(_term_for(costume, cfg, team_rate_up, state, song_seconds, None))
            else:
                score_rates.append(rate * weight)
    return terms, score_rates, board_score_rate, board_skill_effect_up


def _summary(
    dist: dict[int, float],
    base: int,
    bins: int,
    bin_width: int,
    lowest: int | None = None,
    highest: int | None = None,
    keep_law: bool = False,
) -> dict:
    items = sorted((v + base, p) for v, p in dist.items())
    if bin_width > 1 and lowest is not None and highest is not None:
        # a binned body can put a grid point up to one bin past an exact extreme:
        # that mass sits AT the extreme, the only reachable value there
        clamped: dict[int, float] = {}
        for v, p in items:
            v = min(max(v, lowest), highest)
            clamped[v] = clamped.get(v, 0.0) + p
        items = sorted(clamped.items())
    mean = sum(v * p for v, p in items)
    var = max(0.0, sum((v - mean) ** 2 * p for v, p in items))
    out: dict = {
        "min": items[0][0],
        "max": items[-1][0],
        "mean": mean,
        "sd": var**0.5,
        "p_min": items[0][1],
        "p_max": items[-1][1],
        "support": len(items),
        "bin_width": bin_width,
        "exact": bin_width == 1,
    }
    acc = 0.0
    got: dict[float, int] = {}
    for v, p in items:
        acc += p
        for q in PERCENTILES:
            if q not in got and acc >= q - 1e-12:
                got[q] = v
    for q in PERCENTILES:
        out[f"p{round(q * 100)}"] = got.get(q, items[-1][0])
    # the histogram spans p0.1 .. max (a long, ~1e-13 lower tail would
    # otherwise squeeze the body into a few bins); the mass below is `below`
    acc, lo = 0.0, items[0][0]
    for v, p in items:
        acc += p
        if acc >= 0.001:
            lo = v
            break
    hi = items[-1][0]
    width = max(1, math.ceil((hi - lo + 1) / max(1, bins)))
    pmf: dict[int, float] = {}
    below = 0.0
    for v, p in items:
        if v < lo:
            below += p
            continue
        key = lo + ((v - lo) // width) * width
        pmf[key] = pmf.get(key, 0.0) + p
    out["pmf"] = {
        "start": lo,
        "width": width,
        "below": below,
        "mass": [pmf.get(lo + k * width, 0.0) for k in range((hi - lo) // width + 1)],
    }
    if keep_law:
        out["law"] = items
    return out


def best_of_n(law: Sequence[tuple[int, float]], n: int, score: float | None = None) -> dict:
    """The best of `n` independent runs, from one run's law (`[(score, probability)]`,
    ascending: `run_score_distribution(keep_law=True)["law"]`).

    The best of n is at most x when every run is: `P(best <= x) = F(x) ** n`, `F` the
    one-run CDF. So `expected = SUM over x of x * (F(x)**n - F(x-)**n)`, and its
    percentiles are where `F**n` crosses them. With `score`: `run_percentile` = the share
    of single runs below it (`F(score-)`), `best_percentile` = the share of best-of-n
    sessions below it (`F(score-)**n`), and `chance_to_reach` = `1 - F(score-)**n`, the
    chance n runs reach it at least once."""
    if n < 1:
        raise ValueError(f"n must be >= 1, got {n}")
    if not law:
        raise ValueError("an empty law")
    total = sum(p for _v, p in law)
    acc, prev_n, expected = 0.0, 0.0, 0.0
    cdf_n: list[tuple[int, float]] = []
    for v, p in law:
        acc += p / total
        now = min(1.0, acc) ** n
        expected += v * (now - prev_n)
        prev_n = now
        cdf_n.append((v, now))
    out: dict = {"n": n, "expected": expected, "max": law[-1][0]}
    for q in PERCENTILES:
        out[f"p{round(q * 100)}"] = next((v for v, c in cdf_n if c >= q - 1e-12), law[-1][0])
    if score is not None:
        below = sum(p for v, p in law if v < score) / total
        out.update(
            {
                "score": score,
                "run_percentile": below,
                "best_percentile": below**n,
                "chance_to_reach": 1.0 - below**n,
            }
        )
    return out


class _Sweep:
    """The per-note terms of one row at one slot order, and the sweep over its notes.

    Built once per row; `run` sweeps the notes in time order (the run law) or in reverse
    (the law of what is left after a point), and can keep the states at chosen points."""

    def __init__(
        self,
        members: tuple[Card, ...],
        leader: Card,
        chart: SongChart,
        cfg: ScoringConfig,
        member_order: Sequence[str],
        deck_power: float,
        cap: int,
    ) -> None:
        by_id = {card.id: card for card in members}
        if sorted(member_order) != sorted(by_id):
            raise ValueError(f"member_order {list(member_order)} is not a permutation of the members")
        times = tuple(note.time for note in chart.notes)
        terms, score_rates, board_score_rate, skill_effect_up = _terms(members, leader, chart, cfg)
        note_weights, perfect_sum = _chart_weights(chart, cfg.platform, cfg.song_auto)
        constant = (
            float(deck_power)
            * SCORE_COEFFICIENT
            * chart.music_coefficient()
            / perfect_sum
            * (1.0 + board_score_rate)
        )
        slot_of = {cid: slot for slot, cid in enumerate(member_order)}
        index_slot = {i: slot_of[card.id] for i, card in enumerate(members)}
        n = len(times)
        fixed = [0] * n
        windows_at: list[dict[int, int]] = [dict() for _ in range(n)]
        # one rider per check, as the game rolls it: a rider another one of the same ACTIVE
        # outranks on the same window counts nothing (`song._dominated_riders`)
        dominated = _dominated_riders(terms, index_slot)
        for bit, term in enumerate(terms):
            if term.slot_cover is not None:
                if bit in dominated:
                    continue
                # a rider's extra roll on one ACTIVE window: random (occupancy q), live
                # where its host's slot in this order puts it. Fired together with the
                # base roll it counts once (`E[max]` of two certain = one)
                slot = index_slot[term.special_member]
                span = term.slot_cover[slot] if slot < len(term.slot_cover) else None
                if span is not None and term.occupancy > 0.0:
                    for k in range(bisect_left(times, span[0]), bisect_left(times, span[1])):
                        if term.occupancy >= 1.0:
                            fixed[k] |= 1 << bit
                        else:
                            windows_at[k][bit] = 0
                continue
            if term.special_member is not None:
                slot = index_slot[term.special_member]
                if slot < len(chart.special_cues):
                    cue = chart.special_cues[slot]
                    for k in range(bisect_left(times, cue), bisect_left(times, cue + term.special_duration)):
                        fixed[k] |= 1 << bit
                continue
            if term.windows is None:
                for k in range(n):
                    fixed[k] |= 1 << bit
                continue
            certain = term.occupancy >= 1.0
            for w, (start, end) in enumerate(term.windows):
                for k in range(bisect_left(times, start), bisect_left(times, end)):
                    if certain:
                        fixed[k] |= 1 << bit
                    elif term.occupancy > 0.0:
                        windows_at[k][bit] = w
        self.times = times
        self.n = n
        self.cap = cap
        self.fixed = fixed
        self.windows_at = windows_at
        self.terms = terms
        self.certain_terms = [t if not t.occupancy else _with_occupancy(t, 1.0) for t in terms]
        self.constant = constant
        self.note_weights = note_weights
        self._score_rates = score_rates
        self._skill_effect_up = skill_effect_up
        self._cfg = cfg
        self._expect_cache: dict[int, float] = {}
        self._fired_cache: dict[int, float] = {}

        # the scorer's own expectation, from the same terms (occupancy = p), note by note
        self.expected_notes = []
        for k in range(n):
            mask = fixed[k]
            for bit in windows_at[k]:
                mask |= 1 << bit
            self.expected_notes.append(
                math.ceil(constant * note_weights[k] * self._ratio(mask, terms, self._expect_cache))
            )
        self.expected_total = 0.0
        for value in self.expected_notes:
            self.expected_total += value

        # each random window (a `(bit, window)` key) spans a contiguous run of notes
        first: dict[tuple[int, int], int] = {}
        last: dict[tuple[int, int], int] = {}
        for k, cover in enumerate(windows_at):
            for key in cover.items():
                first.setdefault(key, k)
                last[key] = k
        self.first = first
        self.last = last
        self.occupancy = {key: terms[key[0]].occupancy for key in first}

    def _ratio(self, mask: int, source, cache) -> float:
        hit = cache.get(mask)
        if hit is None:
            live_ratio = [t for b, t in enumerate(source) if mask >> b & 1 and not t.is_boost]
            live_boost = [t for b, t in enumerate(source) if mask >> b & 1 and t.is_boost]
            hit = 1.0 + self._cfg.stacking_model.combine(
                [*self._score_rates, _ratio_at(live_ratio, live_boost, self._cfg, self._skill_effect_up)]
            )
            cache[mask] = hit
        return hit

    def note_value(self, k: int, fired) -> int:
        """Note `k`'s score when exactly the windows in `fired` (of those covering it) fired."""
        mask = self.fixed[k]
        for bit, _w in fired:
            mask |= 1 << bit
        return math.ceil(
            self.constant * self.note_weights[k] * self._ratio(mask, self.certain_terms, self._fired_cache)
        )

    def run(self, *, reverse: bool = False, keep_at: frozenset[int] = frozenset()) -> tuple:
        """`(final, kept, bin_width)`: the law of the score of every note, and at each
        boundary `c` in `keep_at` the states there, `{fired: (weight, offset, law)}`, of the
        notes before `c` (forward) or from `c` on (`reverse`).

        Notes in time order (or reverse); a window covers a contiguous run of notes, so at
        most a few windows are OPEN at once (one per ACTIVE skill). The state is the set of
        open windows that fired; each state carries a probability weight, an integer offset
        (the notes scored since it was last merged) and a normalised law of what came before.
        Opening a window splits every state in two (sharing the law, no copy); closing one
        merges the two states that differ only in it. Exact unless a merged law's support
        exceeds `cap`, when it is binned (`bin_width`): each value's mass is split between its
        two neighbours on one grid of the absolute score, in proportion to its distance from
        each, so the mean stays exact and the error is unbiased (flooring at each merge would
        pull every percentile down). Each state also carries its exact lowest and highest
        value (relative, as the law), so the served extremes stay exact when the body is
        binned. A law is never changed once made, so a kept state shares it."""
        first, last = (self.last, self.first) if reverse else (self.first, self.last)
        opening: dict[int, list] = {}
        closing: dict[int, list] = {}
        for key in first:
            opening.setdefault(first[key], []).append(key)
            closing.setdefault(last[key], []).append(key)
        occupancy = self.occupancy
        cap = self.cap
        states: dict[frozenset, list] = {frozenset(): [1.0, 0, {0: 1.0}, 0, 0]}
        bin_width = 1
        kept: dict[int, dict] = {}

        def keep(boundary: int) -> None:
            if boundary in keep_at:
                kept[boundary] = {fired: (s[0], s[1], s[2]) for fired, s in states.items()}

        def put(out: dict[int, float], key: int, mass: float, base: int) -> None:
            """`mass` at relative `key` (absolute `base + key`), split between the two
            grid points around it when the law is binned."""
            if bin_width == 1:
                out[key] = out.get(key, 0.0) + mass
                return
            absolute = base + key
            grid = (absolute // bin_width) * bin_width
            upper = (absolute - grid) / bin_width
            low = grid - base
            out[low] = out.get(low, 0.0) + mass * (1.0 - upper)
            if upper:
                out[low + bin_width] = out.get(low + bin_width, 0.0) + mass * upper

        def merged(a: list, b: list) -> list:
            nonlocal bin_width
            wa, oa, da, la, ha = a
            wb, ob, db, lb, hb = b
            w = wa + wb
            if da is db and oa == ob:
                return [w, oa, da, la, ha]
            base_off = min(oa, ob)
            out: dict[int, float] = {}
            for weight, off, law in ((wa, oa, da), (wb, ob, db)):
                share = weight / w if w else 0.0
                shift = off - base_off
                for v, p in law.items():
                    put(out, v + shift, p * share, base_off)
            if len(out) > cap:
                lo, hi = min(out), max(out)
                bin_width = max(bin_width * 2, (hi - lo) // (cap // 2) + 1)
                binned: dict[int, float] = {}
                for v, p in out.items():
                    put(binned, v, p, base_off)
                out = binned
            return [w, base_off, out, min(la + oa, lb + ob) - base_off, max(ha + oa, hb + ob) - base_off]

        n = self.n
        keep(n if reverse else 0)
        for k in range(n - 1, -1, -1) if reverse else range(n):
            for key in opening.get(k, ()):
                q = occupancy[key]
                nxt: dict[frozenset, list] = {}
                for fired, (w, off, law, lo, hi) in states.items():
                    if 1.0 - q > 0.0:
                        nxt[fired] = [w * (1.0 - q), off, law, lo, hi]
                    if q > 0.0:
                        nxt[fired | {key}] = [w * q, off, law, lo, hi]
                states = nxt
            for fired, state in states.items():
                state[1] += self.note_value(k, fired)
            for key in closing.get(k, ()):
                nxt = {}
                for fired, state in states.items():
                    rest = fired - {key}
                    held = nxt.get(rest)
                    nxt[rest] = state if held is None else merged(held, state)
                states = nxt
            keep(k if reverse else k + 1)
        final = None
        for state in states.values():
            final = state if final is None else merged(final, state)
        return final, kept, bin_width


def run_score_distribution(
    members: Sequence[Card],
    leader: Card,
    chart: SongChart,
    cfg: ScoringConfig,
    member_order: Sequence[str],
    deck_power: float,
    *,
    bins: int = 60,
    cap: int = 400_000,
    keep_law: bool = False,
    pace: bool = False,
) -> dict:
    """The law of the run score of (`leader`, `members` in `member_order`) on
    `chart`, at `deck_power`. Returns `mean`, `sd`, `min`, `max`, `p1..p99`,
    `pmf` (`start`, `width`, `mass[]`), `exact` (no value binning was needed),
    `expected_total` (the scorer's own expectation recomputed from the same
    terms) and the component sizes; with `keep_law`, the whole law too (`law`: `[(score,
    probability)]` ascending, for `best_of_n`); with `pace`, the score pace of the runs
    in each histogram bar (`pace`, see `score_pace`)."""
    sweep = _Sweep(tuple(members), leader, chart, cfg, member_order, deck_power, cap)
    cuts = pace_cuts(chart, sweep.times) if pace else []
    final, forward, bin_width = sweep.run(keep_at=frozenset(c for _label, _t, c in cuts))
    _w, base, dist, lo, hi = final
    out = _summary(dist, base, bins, bin_width, lo + base, hi + base, keep_law=keep_law)
    out.update(
        {
            "expected_total": sweep.expected_total,
            "deck_power": float(deck_power),
            "member_order": list(member_order),
            "random_windows": len(sweep.first),
            "notes": sweep.n,
        }
    )
    if pace:
        # the reverse law spreads from its first notes on, so a full cap would cost three
        # forward sweeps; a smaller one bins it to a few tens of points (a bar is thousands)
        sweep.cap = min(cap, PACE_REVERSE_CAP)
        _final, backward, reverse_width = sweep.run(
            reverse=True, keep_at=frozenset(c for _label, _t, c in cuts)
        )
        out["pace"] = score_pace(sweep, cuts, forward, backward, out["pmf"])
        out["pace"]["reverse_bin_width"] = reverse_width
    return out


# ---------------------------------------------------------------------------
# the score pace: how far into its score a run that ends in a given bar is, cue by cue
# ---------------------------------------------------------------------------
PACE_SUBCELLS = 8
PACE_REVERSE_CAP = 100_000
PACE_BAND = (0.10, 0.90)


def pace_cuts(chart: SongChart, times: Sequence[float]) -> list[tuple[str, float, int]]:
    """`(label, time, boundary)` of each pace cut: the chart's Special cues and the song's
    end. `boundary` = the notes scored before the cut (a note on a cue is the cue's)."""
    cuts = [
        (f"Special {i + 1}", float(cue), bisect_left(times, cue)) for i, cue in enumerate(chart.special_cues)
    ]
    cuts.append(("End", float(times[-1]) if times else 0.0, len(times)))
    return cuts


def _quantile(cells: list[tuple[float, float]], total: float, q: float) -> float | None:
    """The value at which `cells` (`[(value, mass)]`, ascending) reach the share `q`."""
    acc = 0.0
    for value, mass in cells:
        acc += mass
        if acc >= q * total - 1e-15:
            return value
    return cells[-1][0] if cells else None


def _sorted_law(law: dict[int, float], off: int) -> tuple[list[int], list[float], list[float]]:
    """`(values, mass_below, score_mass_below)`: the law's absolute values ascending, and
    before each index the mass and the mass-weighted score of the values below it."""
    values = sorted(law)
    below, score_below = [0.0], [0.0]
    mass = score = 0.0
    for v in values:
        p = law[v]
        mass += p
        score += p * (off + v)
        below.append(mass)
        score_below.append(score)
    return [off + v for v in values], below, score_below


def score_pace(sweep: _Sweep, cuts: list, forward: dict, backward: dict, pmf: dict) -> dict:
    """The score pace of the runs that end in each histogram bar: at each cut (the five
    Special cues and the end), the mean and the 10-90 % band of the score made so far,
    among the runs whose final score falls in the bar.

    At a cut, `S` = the score of the notes before it, `R` = the rest, `F = S + R`. Every
    roll belongs to one window; a window that does not cross the cut lies wholly on one
    side, so given which of the windows open at the cut fired (`sigma`), `S` and `R` are
    independent. The forward sweep kept `P(sigma)` and the law of `S | sigma` there, the
    reverse sweep the law of `R | sigma`; so
    `P(S = s, F in [a, b)) = SUM_sigma P(sigma) P(S = s | sigma) P(a - s <= R < b - s | sigma)`,
    summed value by value over the smaller of the two laws (exact: the mass and the mean of
    each bar). The band is read on cells of one eighth of a bar, aligned on the bar edges.
    `check.mass` = the largest gap, at any cut, between a bar's mass so rebuilt and the
    histogram's own."""
    lo, width, mass_of = pmf["start"], pmf["width"], pmf["mass"]
    nb = len(mass_of)
    sub = PACE_SUBCELLS
    sorted_cache: dict[tuple[int, int], tuple[list[int], list[float], list[float]]] = {}

    def sorted_of(law: dict[int, float], off: int):
        key = (id(law), off)
        hit = sorted_cache.get(key)
        if hit is None:
            hit = sorted_cache[key] = _sorted_law(law, off)
        return hit

    def cell_of(s: float) -> int:
        return math.floor((s - lo) * sub / width)

    def cell_start(c: int) -> int:
        # the lowest whole score in cell `c`
        return math.ceil(lo + c * width / sub)

    def edge(k: int) -> float:
        # the upper edge of bin k: 0 = below the first bar, k = bar k - 1, nb = open above
        return lo + k * width if k < nb else math.inf

    expected_prefix = [0]
    for value in sweep.expected_notes:
        expected_prefix.append(expected_prefix[-1] + value)
    out_cuts = []
    # bin k (0..nb): 0 = the mass below the histogram's first bar, k = bar k - 1
    bar_mean: list[list[float | None]] = [[None] * len(cuts) for _ in range(nb + 1)]
    bar_low: list[list[float | None]] = [[None] * len(cuts) for _ in range(nb + 1)]
    bar_high: list[list[float | None]] = [[None] * len(cuts) for _ in range(nb + 1)]
    bar_mass = [[0.0] * len(cuts) for _ in range(nb + 1)]
    worst_mass = 0.0
    for j, (label, t, boundary) in enumerate(cuts):
        here, rest = forward[boundary], backward[boundary]
        # per bin: {cell: [mass, mass x score]}; `every`: the same over all runs
        acc: list[dict[int, list[float]]] = [{} for _ in range(nb + 1)]
        every: dict[int, list[float]] = {}

        def add(into: dict[int, list[float]], cell: int, mass: float, smass: float) -> None:
            got = into.get(cell)
            if got is None:
                into[cell] = [mass, smass]
            else:
                got[0] += mass
                got[1] += smass

        for fired, (w, s_off, s_law) in here.items():
            _wr, r_off, r_law = rest[fired]
            for v, p in s_law.items():
                s = s_off + v
                add(every, cell_of(s), w * p, w * p * s)
            if len(s_law) <= len(r_law):
                # value by value over S: P(F < e) = P(R < e - s)
                r_values, r_below, _r_score = sorted_of(r_law, r_off)
                r_min, r_max = r_values[0], r_values[-1]
                for v, p in s_law.items():
                    s = s_off + v
                    cell, mass = cell_of(s), w * p
                    k = min(nb, max(0, math.floor((s + r_min - lo) / width)))
                    prev = 0.0
                    while True:
                        x = edge(k) - s
                        g = 0.0 if x <= r_min else 1.0 if x > r_max else r_below[bisect_left(r_values, x)]
                        d = g - prev
                        if d > 0.0:
                            add(acc[k], cell, mass * d, mass * d * s)
                        prev = g
                        if g >= 1.0:
                            break
                        k += 1
            else:
                # value by value over R: the S of bin k lies in [e_(k-1) - r, e_k - r),
                # split on the cells for the band
                s_values, s_below, s_score = sorted_of(s_law, s_off)
                s_min, s_max = s_values[0], s_values[-1]
                for v, pr in r_law.items():
                    r = r_off + v
                    k = min(nb, max(0, math.floor((s_min + r - lo) / width)))
                    start = -math.inf if k == 0 else edge(k - 1) - r
                    while start <= s_max:
                        stop = edge(k) - r
                        c = cell_of(max(start, s_min))
                        a = start
                        while a < stop and a <= s_max:
                            b = min(stop, cell_start(c + 1))
                            i, i2 = bisect_left(s_values, a), bisect_left(s_values, b)
                            if i2 > i:
                                m = s_below[i2] - s_below[i]
                                add(acc[k], c, w * pr * m, w * pr * (s_score[i2] - s_score[i]))
                            a, c = b, c + 1
                        if k == nb:
                            break
                        start, k = stop, k + 1
        for k in range(nb + 1):
            items = sorted(acc[k].items())
            total = sum(m for _c, (m, _ms) in items)
            bar_mass[k][j] = total
            if k:
                worst_mass = max(worst_mass, abs(total - mass_of[k - 1]))
            if total <= 0.0:
                continue
            bar_mean[k][j] = sum(ms for _c, (_m, ms) in items) / total
            ladder = [(ms / m, m) for _c, (m, ms) in items if m > 0.0]
            bar_low[k][j] = _quantile(ladder, total, PACE_BAND[0])
            bar_high[k][j] = _quantile(ladder, total, PACE_BAND[1])
        items = sorted(every.items())
        total = sum(m for _c, (m, _ms) in items)
        ladder = [(ms / m, m) for _c, (m, ms) in items if m > 0.0]
        out_cuts.append(
            {
                "label": label,
                "t": t,
                "notes": boundary,
                "expected": expected_prefix[boundary],
                "mean": sum(ms for _c, (_m, ms) in items) / total if total else 0.0,
                "p10": _quantile(ladder, total, PACE_BAND[0]),
                "p90": _quantile(ladder, total, PACE_BAND[1]),
            }
        )
    end = sweep.times[-1] if sweep.times else 0.0
    # the Expected score of the notes before each whole second (to the song's end), for
    # placing a grade between two cuts
    by_second = []
    k = 0
    for second in range(math.floor(end) + 1):
        while k < sweep.n and sweep.times[k] < second:
            k += 1
        by_second.append(expected_prefix[k])
    return {
        "cuts": out_cuts,
        "bars": {"mean": bar_mean[1:], "p10": bar_low[1:], "p90": bar_high[1:], "mass": bar_mass[1:]},
        "below": {"mean": bar_mean[0], "mass": bar_mass[0]},
        "expected_by_second": by_second,
        "expected_total": expected_prefix[-1],
        "song_seconds": end,
        "band": list(PACE_BAND),
        "grid": width / sub,
        "check": {"mass": worst_mass},
        "method": "exact forward and reverse sweeps of the same law, joined at each cut; "
        "the band on cells of one eighth of a bar",
    }


def _with_occupancy(term, occupancy: float):
    import dataclasses  # noqa: PLC0415

    return dataclasses.replace(term, occupancy=occupancy)
