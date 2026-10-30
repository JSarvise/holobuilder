"""How far the RANKING path's `SkillScoreUpRatio` sits from the exact per-song integral.

WHAT THIS MEASURES AND WHY IT EXISTS. `scoring._score_up_pool` answers with ONE number
per team: a run-average `(1 + B) x A`, where `A` is `E[max]` over the ACTIVE bucket and
`B` the boost maximum. `song.py` answers the same question on a real chart, on a real
time axis, note-weighted the way `CalcScore` weights -- and it is the reference, because
it makes no stationarity assumption at all. The gap between them is what the finite
reference window (`scoring._finite_window_uptime`) addresses.

    A_rank    = _score_up_pool(ratio_entries, [], cfg)              # boost half suppressed
    ratio_rank= _score_up_pool(ratio_entries, boost_entries, cfg)

    A_exact   = SUM_n w_n _ratio_at(live_ratio(t_n), [],       cfg) / SUM_n w_n
    ratio_exct= SUM_n w_n _ratio_at(live_ratio(t_n), live_boost(t_n), cfg) / SUM_n w_n

    w_n = perfect_permil_n x (1 + ComboCoefficient(n + 1))          # CalcScore's own weight

BOTH SIDES ARE THE SHIPPED OBJECTS, not a reimplementation of them: the entries come from
`scoring._bucket_entry`, the terms from `song._term_for`, the aggregation from
`scoring._score_up_pool` via `song._ratio_at`. Swapping `cfg.active_stacking_model` moves
both sides, which is exactly what makes this an acceptance harness rather than a fit.

    python active_lattice.py [--charts N] [--teams M] [--seed S] [--model NAME] [--per-team]

`--charts` samples evenly from the real charts that carry five `#mmm0B` cues; `--charts 0`
uses all of them. Parsed charts are cached under `dumps/lattice_charts.json` (gitignored) --
the first run takes a few minutes, later ones a few seconds.
"""
import argparse
import json
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

import sus_parse as sp                                                  # noqa: E402
import chart_timeline as ct                                            # noqa: E402

from hbuilder.domain.gates import resolve_card_set_gates               # noqa: E402
from hbuilder.domain.loader import load_cards, load_owned              # noqa: E402
from hbuilder.domain.scoring import (                                  # noqa: E402
    _bucket_entry,
    _fold_activation_rate_up,
    _is_score_up_effect,
    _pool_for,
    _resolve_skill,
    _score_up_implements_boost,
    _score_up_pool,
)
from hbuilder.domain.scoring_config import load_scoring_config          # noqa: E402
from hbuilder.domain.scoring_models import ACTIVE_STACKING_MODELS       # noqa: E402
from hbuilder.domain.song import (                                      # noqa: E402
    LIVE_COMBO_TIERS,
    _ratio_at,
    _support_state,
    _term_for,
)

import dataclasses                                                      # noqa: E402
from pathlib import Path                                                # noqa: E402

CHARTS = os.path.join(HERE, "dumps", "charts_cdn")
CACHE = os.path.join(HERE, "dumps", "lattice_charts.json")

# `LiveNote` PERFECT row, PC column -- the column the client actually reads. Inlined
# rather than pulled through `master_cache` so this harness runs without the master DB
# present. From the master `LiveNote` table, PERFECT row, PC column:
#
#     NORMAL 1000 · FLICK 1000 · LONG_START 1000 · LONG_END 1000 · LONG_FLICK_END 1000
#     LONG_RELAY 100 · LONG_CONTINUATION 100
#
# LONG_RELAY and LONG_CONTINUATION carry the SAME weight; they are the two tick types.
# A wrong weight does NOT cancel against `NotesPerfectCoefficientSum`: the denominator is
# a chart-wide constant, so an over-weighted note steals score share from every other note
# in its own chart. That distorts WHERE on the time axis the weight sits, which is
# precisely what decides whether an ACTIVE's window is worth anything -- so it can reorder
# teams, not merely rescale them.
#
# PLATFORM. The table below is the PC column, kept as the platform-NEUTRAL base. The mobile
# column (`scoreCoefficientPermilMultiply`, same master table) differs in exactly one cell:
# FLICK at PERFECT is **1050**. Rather than bake a platform into this cache, each chart
# record also carries `"f"`: the indices (into `t`/`w`) of its FLICK notes, so `build_charts`
# can emit a per-note flag and the scorer can pick the column at run time
# (`ScoringConfig.platform`, default mobile). `w` stays PC-based.
FLICK_MOBILE_PERMIL = 1050
PERFECT_PERMIL = {
    "NORMAL": 1000,
    "FLICK": 1000,
    "LONG_START": 1000,
    "LONG_RELAY": 100,
    "LONG_CONTINUE": 100,
    "LONG_END": 1000,
    "LONG_FLICK_END": 1000,
}


def combo_coefficient(combo):
    permil = 0
    for threshold, tier in LIVE_COMBO_TIERS:
        if combo >= threshold:
            permil = tier
    return permil / 1000.0


# --------------------------------------------------------------------------- charts
def build_chart_cache():
    """{name: {"t": [note times], "w": [note weights], "cues": [5 cue times]}}.

    Only the charts carrying five ascending `#mmm0B` cues; the four tutorial `m9999`
    difficulties carry two and are skipped, exactly as `SongChart.__post_init__` demands."""
    out = {}
    names = sorted(n for n in os.listdir(CHARTS) if n.endswith(".sus"))
    for i, name in enumerate(names):
        path = os.path.join(CHARTS, name)
        try:
            text = open(path, encoding="utf-8", errors="replace").read()
            chart = sp.parse(text)
            kinds = sp.classify(chart)
            cues = sorted(
                e["time"] for e in ct.events(text, chart) if e["channel"] == "0B"
            )
        except Exception:
            continue
        if len(cues) != 5 or sorted(cues) != cues:
            continue
        times, weights, flicks, combo = [], [], [], 0
        for kind, note in kinds:
            permil = PERFECT_PERMIL.get(kind)
            if permil is None:
                continue
            if kind == "FLICK":
                flicks.append(combo)          # index of this note in t/w (0-based)
            combo += 1
            times.append(round(note.time, 6))
            weights.append(permil * (1.0 + combo_coefficient(combo)))
        if not times or sum(weights) <= 0:
            continue
        out[name[:-4]] = {"t": times, "w": weights, "cues": cues, "f": flicks}
        if (i + 1) % 50 == 0:
            print(f"  parsed {i + 1}/{len(names)} ...", file=sys.stderr)
    return out


def charts(limit):
    if os.path.exists(CACHE):
        data = json.load(open(CACHE, encoding="utf-8"))
    else:
        print("building chart cache (first run only) ...", file=sys.stderr)
        data = build_chart_cache()
        json.dump(data, open(CACHE, "w", encoding="utf-8"))
    names = sorted(data)
    if limit and limit < len(names):
        step = len(names) / float(limit)
        names = [names[int(i * step)] for i in range(limit)]
    return [(n, data[n]) for n in names]


# --------------------------------------------------------------------------- teams
def teams(count, seed, size=5):
    cards = load_cards(Path(ROOT) / "data" / "cards.json")
    owned = load_owned(Path(ROOT) / "data" / "owned.json", cards)
    rng = random.Random(seed)
    pool = sorted(owned, key=lambda c: c.id)
    return [tuple(rng.sample(pool, size)) for _ in range(count)]


# --------------------------------------------------------------------------- the two sides
def sides(members, cfg, song_seconds):
    """`(ratio_entries, boost_entries, exact_terms)` for one team.

    Mirrors `song.score_song_detailed`'s member half exactly -- same gate resolution, same
    `team_rate_up` fold, same routing -- and builds BOTH representations from the same
    `ResolvedSkill` list so the only difference between them is the occupancy."""
    card_set_gates = resolve_card_set_gates(members, cfg.run_state)
    resolved_all = []
    for card in members:
        resolved_all.extend(card_set_gates.normal_by_card_id[card.id])
    team_rate_up = _fold_activation_rate_up(tuple(resolved_all), cfg)
    index_by_id = {card.id: i for i, card in enumerate(members)}

    ratio_entries, boost_entries, exact_terms = [], [], []
    for resolved in resolved_all:
        pool = _pool_for(resolved)
        if pool not in ("score", "support"):
            continue
        _rate, state, _missing = _resolve_skill(resolved, cfg, team_rate_up)
        if pool == "score" and not _is_score_up_effect(resolved):
            continue
        if pool == "support":
            state = _support_state(resolved, members, cfg, state)
        weight = cfg.family_weights[resolved.family]
        entry = _bucket_entry(resolved, cfg, team_rate_up, weight, state)
        is_boost = pool == "support" or _score_up_implements_boost(resolved)
        (boost_entries if is_boost else ratio_entries).append(entry)
        exact_terms.append(
            _term_for(
                resolved,
                cfg,
                team_rate_up,
                state,
                song_seconds,
                index_by_id[resolved.card_id],
            )
        )
    return ratio_entries, boost_entries, exact_terms


def _live(term, t, cues):
    if term.windows is None:
        if term.special_member is None:
            return True
        cue = cues[term.special_member]
        return cue <= t < cue + term.special_duration
    return any(start <= t < end for start, end in term.windows)


def exact(members, cfg, chart):
    """`(A_exact, ratio_exact)` -- note-weighted, through `song._ratio_at`.

    Memoised on the liveness signature: the integrand is piecewise constant, so a chart of
    a thousand notes still costs about sixty `_score_up_pool` calls."""
    times, weights, cues = chart["t"], chart["w"], chart["cues"]
    _r, _b, terms = sides(members, cfg, times[-1])
    memo, num_a, num_r, num_b, den = {}, 0.0, 0.0, 0.0, 0.0
    for t, w in zip(times, weights):
        key = tuple(_live(term, t, cues) for term in terms)
        hit = memo.get(key)
        if hit is None:
            live_ratio = [term for term, on in zip(terms, key) if on and not term.is_boost]
            live_boost = [term for term, on in zip(terms, key) if on and term.is_boost]
            # `B` alone: `_score_up_pool` with a single unit-magnitude ratio term returns
            # `1 x (1 + B) - ...`; simpler and exact — `combine` over the boost population
            # IS `B`, which is what the multiplier is built from.
            boost_only = cfg.active_stacking_model.combine(
                [(term.bucket, term.magnitude, term.occupancy, False) for term in live_boost]
            )
            hit = (
                _ratio_at(live_ratio, [], cfg),
                _ratio_at(live_ratio, live_boost, cfg),
                boost_only,
            )
            memo[key] = hit
        num_a += w * hit[0]
        num_r += w * hit[1]
        num_b += w * hit[2]
        den += w
    return num_a / den, num_r / den, num_b / den


def drop_exclusive(entries):
    """The plain independence walk, reproduced exactly rather than approximated.

    `ExpectedMaximumActiveSkill` is a strict generalisation: with `exclusive=False` on every
    term the `used` accumulator stays `0.0`, the `(1 - used)` factor is a multiply by exactly
    `1.0`, and the walk is `SUM_i mag_i occ_i PROD_{j before i} (1 - occ_j)` bit for
    bit. So this switch IS that code path, not a model of it."""
    return [
        (bucket, mag, occ, interval, False)
        for bucket, mag, occ, interval, _ in entries
    ]


def run(team_list, chart_list, cfg, pre_overlap=False):
    """Per-team `(A_rank, A_exact, ratio_rank, ratio_exact)` averaged over the charts, plus
    the flat lists of every (team, chart) residual."""
    per_team, res_a, res_r = [], [], []
    for members in team_list:
        ratio_entries, boost_entries, _ = sides(members, cfg, 128.8)
        if pre_overlap:
            ratio_entries = drop_exclusive(ratio_entries)
            boost_entries = drop_exclusive(boost_entries)
        a_rank = _score_up_pool(ratio_entries, [], cfg)
        r_rank = _score_up_pool(ratio_entries, boost_entries, cfg)
        a_ex, r_ex = [], []
        for _name, chart in chart_list:
            a, r, _b = exact(members, cfg, chart)
            a_ex.append(a)
            r_ex.append(r)
            res_a.append(a_rank - a)
            res_r.append(r_rank - r)
        per_team.append(
            (a_rank, sum(a_ex) / len(a_ex), r_rank, sum(r_ex) / len(r_ex))
        )
    return per_team, res_a, res_r


def rms(values):
    return (sum(v * v for v in values) / len(values)) ** 0.5


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--charts", type=int, default=40)
    ap.add_argument("--teams", type=int, default=9)
    ap.add_argument("--seed", type=int, default=20260803)
    ap.add_argument("--model", default=None, help="active_stacking_model override")
    ap.add_argument("--per-team", action="store_true")
    ap.add_argument(
        "--pre-overlap",
        action="store_true",
        help="score `B` as `master` did, before the SPECIAL disjointness half",
    )
    ap.add_argument(
        "--steady-uptime",
        action="store_true",
        help="score `A` as `master` did, on the steady-state duty cycle `min(1, dur/int)`",
    )
    args = ap.parse_args()
    if args.steady_uptime:
        # `_finite_window_uptime`'s `T -> infinity` limit, which IS the steady-state
        # expression -- so this switch is that code path rather than a model of it.
        import hbuilder.domain.scoring as _scoring

        _scoring._finite_window_uptime = lambda duration, interval: min(
            1.0, duration / interval
        )

    cfg = load_scoring_config()
    if args.model:
        cfg = dataclasses.replace(
            cfg, active_stacking_model=ACTIVE_STACKING_MODELS[args.model]
        )
    chart_list = charts(args.charts)
    team_list = teams(args.teams, args.seed)
    print(
        f"model={cfg.active_stacking_model.name}  teams={len(team_list)}  "
        f"charts={len(chart_list)}  pairs={len(team_list) * len(chart_list)}"
        + ("  [B: independent]" if args.pre_overlap else "  [B: disjoint]")
        + ("  [A: steady]" if args.steady_uptime else "  [A: finite window]")
    )
    per_team, res_a, res_r = run(team_list, chart_list, cfg, args.pre_overlap)

    if args.per_team:
        print(f"{'team':>5} {'A_rank':>8} {'A_exact':>8} {'over %':>8} "
              f"{'r_rank':>8} {'r_exact':>8} {'over %':>8}")
        for i, (ar, ae, rr, re_) in enumerate(per_team):
            print(f"{'ABCDEFGHIJKLMNOPQRSTUVWXYZ'[i % 26]:>5} {ar:8.4f} {ae:8.4f} "
                  f"{100 * (ar / ae - 1):+8.1f} {rr:8.4f} {re_:8.4f} "
                  f"{100 * (rr / re_ - 1):+8.1f}")
    print()
    print(f"E[A]   rms {rms(res_a):.4f}   mean {sum(res_a) / len(res_a):+.4f}")
    print(f"TOTAL  rms {rms(res_r):.4f}   mean {sum(res_r) / len(res_r):+.4f}")


if __name__ == "__main__":
    main()
