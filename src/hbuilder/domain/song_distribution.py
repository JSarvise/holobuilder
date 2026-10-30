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

THE METHOD IS EXACT, NOT SAMPLED. A note is covered by at most one window per
skill, so its value depends on the fired flags of the windows covering it.
Windows that share a note are joined. Enumerating each connected component
(2^k, as the prototype `tools/recon/proxy/score_distribution.py` does) is too
slow: a real team on a real chart has components of 22+ windows, so this
module SWEEPS the notes in time order instead, carrying the law per set of
open windows (at most one per ACTIVE skill) — exact, and linear in the notes.

FIDELITY. The per-note terms are built by the SAME steps
`score_song_detailed` takes for one leader at one slot order — the member
terms, the leader's outfit terms, the board's song-scope score bonus and its
skill-effect-up (unconditional + singer-conditional), the platform / AUTO note
weights — and `expected_total` recomputes the scorer's own number from them.
The caller compares it with `score_song_detailed`'s total and says whether the
two agree (`verified`); a disagreement is reported, never hidden.

`deck_power` is REQUIRED: the caller passes the power its own figure used
(`SongScore.deck_power`), so the law is on the row's basis — Overall Power on
the default `song_absolute_deck_power`.
"""

from __future__ import annotations

import math
from bisect import bisect_left
from typing import Sequence

from hbuilder.domain.card import Card
from hbuilder.domain.gates import resolve_card_set_gates, resolve_leader_costume_skills
from hbuilder.domain.scoring import (
    _board_bonus,
    _fold_activation_rate_up,
    _is_score_up_effect,
    _pool_for,
    _resolve_skill,
)
from hbuilder.domain.scoring_config import ScoringConfig
from hbuilder.domain.song import (
    SCORE_COEFFICIENT,
    SongChart,
    _chart_weights,
    _combo_split,
    _ratio_at,
    _rider_terms,
    _support_state,
    _term_for,
)

__all__ = ["run_score_distribution"]

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
            s_state, s_targets = _support_state(resolved, members, cfg, state)
            terms.extend(_combo_split(resolved, cfg, _term_for(
                resolved, cfg, team_rate_up, s_state, song_seconds, index, s_targets), times))
        elif _is_score_up_effect(resolved):
            terms.extend(_combo_split(resolved, cfg, _term_for(
                resolved, cfg, team_rate_up, state, song_seconds, index), times))
        else:
            score_rates.append(rate * weight)
    # the SPECIAL activation-rate riders, in the scorer's place (after the members)
    terms.extend(_rider_terms(normal, index_of, terms, cfg, chart, times))
    _power, board_score_rate, board_skill_effect_up = _board_bonus(leader, members, cfg)
    if cfg.board is not None and chart.singers:
        board_score_rate = cfg.board.song_score_bonus_for(chart.singers, scope=chart.singer_type)
        board_skill_effect_up += cfg.board.conditional_skill_effect_up_for(
            members, leader.member, chart.singers)
    for costume in resolve_leader_costume_skills(leader, gates.aggregates, cfg.run_state):
        pool = _pool_for(costume)
        if pool in ("none", "stat"):
            continue
        rate, state, _missing = _resolve_skill(costume, cfg, team_rate_up)
        weight = cfg.family_weights[costume.family]
        if pool == "support":
            s_state, s_targets = _support_state(costume, members, cfg, state)
            terms.append(_term_for(costume, cfg, team_rate_up, s_state, song_seconds,
                                   None, s_targets))
        elif pool == "score":
            if _is_score_up_effect(costume):
                terms.append(_term_for(costume, cfg, team_rate_up, state, song_seconds, None))
            else:
                score_rates.append(rate * weight)
    return terms, score_rates, board_score_rate, board_skill_effect_up


def _summary(dist: dict[int, float], base: int, bins: int, bin_width: int) -> dict:
    items = sorted((v + base, p) for v, p in dist.items())
    mean = sum(v * p for v, p in items)
    var = max(0.0, sum((v - mean) ** 2 * p for v, p in items))
    out: dict = {"min": items[0][0], "max": items[-1][0], "mean": mean, "sd": var ** 0.5,
                 "p_min": items[0][1], "p_max": items[-1][1], "support": len(items),
                 "bin_width": bin_width, "exact": bin_width == 1}
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
    out["pmf"] = {"start": lo, "width": width, "below": below,
                  "mass": [pmf.get(lo + k * width, 0.0) for k in range((hi - lo) // width + 1)]}
    return out


def run_score_distribution(
    members: Sequence[Card], leader: Card, chart: SongChart, cfg: ScoringConfig,
    member_order: Sequence[str], deck_power: float, *, bins: int = 60,
    cap: int = 400_000,
) -> dict:
    """The law of the run score of (`leader`, `members` in `member_order`) on
    `chart`, at `deck_power`. Returns `mean`, `sd`, `min`, `max`, `p1..p99`,
    `pmf` (`start`, `width`, `mass[]`), `exact` (no value binning was needed),
    `expected_total` (the scorer's own expectation recomputed from the same
    terms) and the component sizes."""
    members = tuple(members)
    by_id = {card.id: card for card in members}
    if sorted(member_order) != sorted(by_id):
        raise ValueError(f"member_order {list(member_order)} is not a permutation of the members")
    times = tuple(note.time for note in chart.notes)
    terms, score_rates, board_score_rate, skill_effect_up = _terms(members, leader, chart, cfg)
    note_weights, perfect_sum = _chart_weights(chart, cfg.platform, cfg.song_auto)
    constant = (float(deck_power) * SCORE_COEFFICIENT * chart.music_coefficient()
                / perfect_sum * (1.0 + board_score_rate))
    slot_of = {cid: slot for slot, cid in enumerate(member_order)}
    index_slot = {i: slot_of[card.id] for i, card in enumerate(members)}
    n = len(times)
    fixed = [0] * n
    windows_at: list[dict[int, int]] = [dict() for _ in range(n)]
    for bit, term in enumerate(terms):
        if term.slot_cover is not None:
            # a rider's extra roll on one ACTIVE window: random (occupancy q), live
            # where its host's slot in THIS order puts it. Fired together with the
            # base roll it counts once (one magnitude, `E[max]` of two certain = one)
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
    certain_terms = [t if not t.occupancy else _with_occupancy(t, 1.0) for t in terms]
    expect_cache: dict[int, float] = {}
    fired_cache: dict[int, float] = {}

    def ratio(mask: int, source, cache) -> float:
        hit = cache.get(mask)
        if hit is None:
            live_ratio = [t for b, t in enumerate(source) if mask >> b & 1 and not t.is_boost]
            live_boost = [t for b, t in enumerate(source) if mask >> b & 1 and t.is_boost]
            hit = 1.0 + cfg.stacking_model.combine(
                [*score_rates, _ratio_at(live_ratio, live_boost, cfg, skill_effect_up)])
            cache[mask] = hit
        return hit

    # the scorer's own expectation, from the same terms (occupancy = p)
    expected_total = 0.0
    for k in range(n):
        mask = fixed[k]
        for bit in windows_at[k]:
            mask |= 1 << bit
        expected_total += math.ceil(constant * note_weights[k] * ratio(mask, terms, expect_cache))

    # The sweep. Notes in time order; a window covers a contiguous run of
    # notes, so at most a few windows are OPEN at once (one per ACTIVE skill).
    # The state is the set of open windows that fired; each state carries a
    # probability weight, an integer offset (the notes scored since it was
    # last merged) and a normalised law of what came before. Opening a window
    # splits every state in two (sharing the law, no copy); closing one merges
    # the two states that differ only in it. Exact unless a merged law's
    # support exceeds `cap`, when it is binned (`exact` false, `bin_width`).
    first: dict[tuple[int, int], int] = {}
    last: dict[tuple[int, int], int] = {}
    for k, cover in enumerate(windows_at):
        for key in cover.items():
            first.setdefault(key, k)
            last[key] = k
    opening: dict[int, list] = {}
    closing: dict[int, list] = {}
    for key in first:
        opening.setdefault(first[key], []).append(key)
        closing.setdefault(last[key], []).append(key)
    occupancy = {key: terms[key[0]].occupancy for key in first}
    states: dict[frozenset, list] = {frozenset(): [1.0, 0, {0: 1.0}]}
    bin_width = 1

    def merged(a: list, b: list) -> list:
        nonlocal bin_width
        wa, oa, da = a
        wb, ob, db = b
        w = wa + wb
        if da is db and oa == ob:
            return [w, oa, da]
        base_off = min(oa, ob)
        out: dict[int, float] = {}
        for weight, off, law in ((wa, oa, da), (wb, ob, db)):
            share = weight / w if w else 0.0
            shift = off - base_off
            for v, p in law.items():
                key = v + shift
                if bin_width > 1:
                    key = (key // bin_width) * bin_width
                out[key] = out.get(key, 0.0) + p * share
        if len(out) > cap:
            lo, hi = min(out), max(out)
            bin_width = max(bin_width * 2, (hi - lo) // (cap // 2) + 1)
            binned: dict[int, float] = {}
            for v, p in out.items():
                key = (v // bin_width) * bin_width
                binned[key] = binned.get(key, 0.0) + p
            out = binned
        return [w, base_off, out]

    for k in range(n):
        for key in opening.get(k, ()):
            q = occupancy[key]
            nxt: dict[frozenset, list] = {}
            for fired, (w, off, law) in states.items():
                if 1.0 - q > 0.0:
                    nxt[fired] = [w * (1.0 - q), off, law]
                if q > 0.0:
                    nxt[fired | {key}] = [w * q, off, law]
            states = nxt
        for fired, state in states.items():
            mask = fixed[k]
            for bit, _w in fired:
                mask |= 1 << bit
            state[1] += math.ceil(constant * note_weights[k] * ratio(mask, certain_terms, fired_cache))
        for key in closing.get(k, ()):
            nxt = {}
            for fired, state in states.items():
                rest = fired - {key}
                held = nxt.get(rest)
                nxt[rest] = state if held is None else merged(held, state)
            states = nxt
    final = None
    for state in states.values():
        final = state if final is None else merged(final, state)
    _w, base, dist = final
    out = _summary(dist, base, bins, bin_width)
    out.update({"expected_total": expected_total, "deck_power": float(deck_power),
                "member_order": list(member_order), "random_windows": len(first),
                "notes": n})
    return out


def _with_occupancy(term, occupancy: float):
    import dataclasses  # noqa: PLC0415

    return dataclasses.replace(term, occupancy=occupancy)
