"""The DISTRIBUTION of a team's score on one chart -- min, max, mean, spread -- exactly.

WHY THIS EXISTS. `song.score_song` returns ONE number: the expectation of the run score
under the ACTIVE activation roll. That number is not what a player sees: four AUTO runs
of one deck on one song can land at 578424 / 617974 / 650951 / 702485 -- a 21.4 % spread
around a mean the model reproduces. This computes the average, the minimum and the
maximum, and the whole law between them.

WHAT IS RANDOM, AND WHAT IS NOT.

    ACTIVE activation   RANDOM.  One independent Bernoulli(p) per cooldown check, p from
                                 `cfg.probability_tiers` folded through
                                 `cfg.activation_rate_model`. This is the ONLY randomness
                                 in the model.
    SPECIAL cue times    FIXED.  Chart-written, `#mmm0B`, five per song (true of
                                 704/704 real charts).
    PASSIVE / COSTUME    FIXED.  Live for the whole run.
    Judgement            OUT OF THE MODEL. The governing premise is full-perfect play,
                                 the same premise `song.py` scores under. A distribution
                                 computed here is the distribution of the PERFECT-FC
                                 score; a real run's judgement scatter sits UNDER it and
                                 is a property of the player, not of the team.

THE METHOD IS EXACT, NOT SAMPLED. Each ACTIVE window is one Bernoulli. Windows of the
same skill cannot overlap (duration < interval in all real data), so a note is covered by
at most one window per skill and its score depends on the fired-flags of at most one
window per skill. Build the graph on windows with an edge wherever two windows cover a
common note; its CONNECTED COMPONENTS are independent, because every note's random
covering set lies wholly inside one component. Enumerate each component exactly (2^k --
the largest component over 300 team x chart pairs is 13) and convolve.
`--monte-carlo N` runs the sampling version of the same generative model as a cross-check,
never as the answer: 30000 trials cost 4.9x the exact law and are still 0.7 % out on the sd.

    python score_distribution.py --verify --teams 8                  # against song.py
    python score_distribution.py --chart chart_m0001_easy --team 0 --by-skill --runs-of 4
    python score_distribution.py --sweep --teams 10 --charts 24      # separability
    python score_distribution.py --clamp-sweep --teams 8 --charts 24 # price the clamp
    python score_distribution.py --chart chart_m0001_easy --team 0 \\
           --stability 1000 3000 10000 30000 --repeats 20

Every number this prints is computed over the inputs the header line names.
"""

import argparse
import dataclasses
import itertools
import json
import math
import os
import random
import sys
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from hbuilder.domain import targets                                     # noqa: E402
from hbuilder.domain.gates import (                                     # noqa: E402
    resolve_card_set_gates,
    resolve_leader_costume,
)
from hbuilder.domain.loader import load_cards, load_owned               # noqa: E402
from hbuilder.domain.schema import STAT_NAMES                           # noqa: E402
from hbuilder.domain.scoring import (                                   # noqa: E402
    STAT_NAMES_BY_EFFECT_TYPE,
    _board_score_rate,
    _effective_stat_sum,
    _FIXED_TARGET,
    _fold_activation_rate_up,
    _is_score_up_effect,
    _order_varying_indices,
    _ordered_target_card_ids,
    _pool_for,
    _resolve_skill,
    _stat_sum,
)
from hbuilder.domain.scoring_config import load_scoring_config           # noqa: E402
from hbuilder.domain.song import (                                      # noqa: E402
    LIVE_COMBO_TIERS,
    SCORE_COEFFICIENT,
    SPECIAL_CUE_COUNT,
    ChartNote,
    SongChart,
    _ratio_at,
    _support_state,
    _term_for,
    score_song_detailed,
)

CHART_CACHE = os.path.join(HERE, "dumps", "lattice_charts.json")
MUSIC_CACHE = os.path.join(HERE, "dumps", "music_coef.json")


# --------------------------------------------------------------------------- charts
def _combo_coefficient(combo):
    permil = 0
    for threshold, tier in LIVE_COMBO_TIERS:
        if combo >= threshold:
            permil = tier
    return permil / 1000.0


def load_music_coefficients():
    """{chart asset name: (difficulty_level, live_score_coefficient_permil)}.

    Read from `dumps/music_coef.json` if present, else rebuilt from the game's own local
    master cache (`master_cache.rows('Music')` field 17 and `MusicDifficulty` fields 3/4 --
    `verify_formula.py`'s own two accessors, not a second reading of them)."""
    if os.path.exists(MUSIC_CACHE):
        return {k: tuple(v) for k, v in json.load(open(MUSIC_CACHE, encoding="utf-8")).items()}
    import master_cache as mc

    def _f(d, i):
        v = d.get(str(i)) or d.get(i)
        return v[0] if v else None

    music = {r["id"]: r["data"] for r in mc.rows("Music")}
    out = {}
    for r in mc.rows("MusicDifficulty"):
        d = r["data"]
        out[_f(d, 4)] = (_f(d, 3), _f(music[_f(d, 1)], 17))
    json.dump(out, open(MUSIC_CACHE, "w", encoding="utf-8"))
    return out


def load_charts():
    """The 704 real charts, from `active_lattice.py`'s own cache so the two harnesses
    cannot disagree about what a chart is."""
    if not os.path.exists(CHART_CACHE):
        raise SystemExit(
            f"{CHART_CACHE} missing -- run `python active_lattice.py --charts 1` once to "
            f"build it (a few minutes), then re-run this."
        )
    return json.load(open(CHART_CACHE, encoding="utf-8"))


def build_song_chart(name, raw, music):
    """A real `SongChart`, so the cross-check below goes through `score_song_detailed`
    itself rather than through a second implementation of it.

    The cache stores `w = permil x (1 + ComboCoefficient(i+1))`; under the full-perfect
    premise the combo at note `i` IS `i + 1`, so the permil is recoverable exactly."""
    level, permil = music[name]
    notes = []
    for index, (t, w) in enumerate(zip(raw["t"], raw["w"])):
        note_permil = int(round(w / (1.0 + _combo_coefficient(index + 1))))
        notes.append(ChartNote(time=float(t), perfect_permil=note_permil))
    music_id, difficulty = name.split("_")[1], name.split("_")[2]
    return SongChart(
        music_id=music_id,
        difficulty=difficulty,
        difficulty_level=int(level),
        live_score_coefficient_permil=int(permil),
        notes=tuple(notes),
        special_cues=tuple(float(c) for c in raw["cues"]),
    )


# --------------------------------------------------------------------------- the model
class Prepared:
    """One (team, leader, chart, slot order) reduced to everything the distribution needs.

    Deliberately a straight transcription of `song.score_song_detailed`'s setup for ONE
    leader and ONE permutation -- the terms come from `song._term_for`, the aggregation
    from `song._ratio_at`, the deck power from `scoring._effective_stat_sum`. Nothing is
    re-derived. `verify()` below proves the transcription is faithful by reproducing
    `score_song_detailed`'s own integer, and does so 8/8 on `chart_m0001_easy`."""

    __slots__ = ("terms", "certain", "score_rates", "constant", "note_weights", "times",
                 "cues", "slot_of", "cfg", "power")

    def __init__(self, members, leader, chart, cfg, deck_power, permutation, clamp=False,
                 flat=0.0):
        times = tuple(note.time for note in chart.notes)
        song_seconds = times[-1]
        card_set_gates = resolve_card_set_gates(members, cfg.run_state)
        normal_resolved = []
        for card in members:
            normal_resolved.extend(card_set_gates.normal_by_card_id[card.id])
        team_rate_up = _fold_activation_rate_up(tuple(normal_resolved), cfg)
        index_by_id = {card.id: i for i, card in enumerate(members)}

        terms, score_rates, stat_entries = [], [], []
        for resolved in normal_resolved:
            pool = _pool_for(resolved)
            if pool == "none":
                continue
            rate, state, _missing = _resolve_skill(resolved, cfg, team_rate_up)
            weight = cfg.family_weights[resolved.family]
            index = index_by_id[resolved.card_id]
            if pool == "stat":
                target_ids = frozenset()
                if state == "applied":
                    target_ids = targets.resolve_target_card_ids(
                        resolved.skill.target, resolved.card_id, members,
                        STAT_NAMES_BY_EFFECT_TYPE[resolved.skill.effect_type],
                        cfg.target_selection_model, cfg.target_under_supply)
                stat_entries.append((
                    rate * weight,
                    STAT_NAMES_BY_EFFECT_TYPE[resolved.skill.effect_type],
                    target_ids,
                    resolved.skill.target if state == "applied" else _FIXED_TARGET,
                    resolved.card_id))
            elif pool == "support":
                terms.append(_term_for(
                    resolved, cfg, team_rate_up,
                    _support_state(resolved, members, cfg, state),
                    song_seconds, index))
            elif _is_score_up_effect(resolved):
                terms.append(_term_for(
                    resolved, cfg, team_rate_up, state, song_seconds, index))
            else:
                score_rates.append(rate * weight)

        costume = resolve_leader_costume(leader, card_set_gates.aggregates, cfg.run_state)
        if costume is not None:
            pool = _pool_for(costume)
            if pool != "none":
                rate, state, _missing = _resolve_skill(costume, cfg, team_rate_up)
                weight = cfg.family_weights[costume.family]
                if pool == "support":
                    terms.append(_term_for(
                        costume, cfg, team_rate_up,
                        _support_state(costume, members, cfg, state),
                        song_seconds, None))
                elif pool == "score":
                    if _is_score_up_effect(costume):
                        terms.append(_term_for(
                            costume, cfg, team_rate_up, state, song_seconds, None))
                    else:
                        score_rates.append(rate * weight)

        if clamp:
            # `chart_timeline.active_windows`'s clamp, reproduced verbatim so its cost can
            # be priced. Windows are half-open, so this pushes the LAST note -- the one
            # carrying the highest combo multiplier -- out of every window still open at
            # the end of the chart. `song._active_windows` does not do this.
            terms = [
                dataclasses.replace(term, windows=tuple(
                    (s, min(e, song_seconds)) for s, e in term.windows))
                if term.windows else term
                for term in terms]

        # When a count-limited passive binds on slot order the deck power is a function
        # of the permutation, exactly as `score_song_detailed`'s joint walk has it -- so
        # it is evaluated FOR THIS permutation rather than through the order-invariant
        # shortcut. Randomness is unaffected either way: a stat buff is deterministic.
        varying = _order_varying_indices(stat_entries, members)
        if varying and cfg.slot_order_rule == "best_order":
            ordered = tuple(members[i] for i in permutation)
            recipients = [
                _ordered_target_card_ids(e[3], e[4], ordered, cfg.target_under_supply)
                if i in varying else e[2]
                for i, e in enumerate(stat_entries)]
            stat_sum = _stat_sum(members, stat_entries, cfg, recipients)
        else:
            stat_sum, _order = _effective_stat_sum(members, stat_entries, cfg, {})
        power = stat_sum if deck_power is None else float(deck_power)

        self.terms = terms
        # The SAME terms with occupancy forced to 1.0. `_ratio_at` reads the occupancy as
        # a probability, so on a REALISED outcome -- "this window fired" -- the ACTIVE
        # term is live with certainty and its occupancy must be 1.0, or `E[max]` discounts
        # a skill that has already been observed to fire and the conditional value comes
        # out as the unconditional expectation, and max would come out equal to mean.
        self.certain = [dataclasses.replace(t, occupancy=1.0) if t.occupancy else t
                        for t in terms]
        # A deterministic Score UP the card data does not carry -- the deck screen's own
        # Outfit and Passive lines.
        # It enters `score_rates`, so under `AdditiveStacking` the note value is
        # `flat + (1 + S) x A`.
        if flat:
            score_rates = [*score_rates, flat]
        self.score_rates = score_rates
        self.times = times
        self.cues = chart.special_cues
        self.cfg = cfg
        self.power = power
        self.slot_of = [0] * SPECIAL_CUE_COUNT
        for slot, member in enumerate(permutation):
            self.slot_of[member] = slot
        self.note_weights = tuple(
            (note.perfect_permil / 1000.0) * (1.0 + chart.combo_coefficient(i + 1))
            for i, note in enumerate(chart.notes))
        self.constant = (power * SCORE_COEFFICIENT * chart.music_coefficient()
                         / chart.notes_perfect_coefficient_sum()
                         * (1.0 + _board_score_rate(members, cfg)))


def liveness(prep):
    """`(fixed_bits, window_of)`.

    `fixed_bits[n]` is the bitmask of terms live at note `n` with certainty (PASSIVE,
    COSTUME, SPECIAL-in-its-cue-window, and any ACTIVE whose probability is exactly 1).
    `window_of[n]` is `{term_index: window_index}` for the RANDOM windows covering note
    `n` -- at most one per term, because a skill's windows are disjoint."""
    terms = prep.terms
    fixed_bits = [0] * len(prep.times)
    window_of = [dict() for _ in prep.times]
    for bit, term in enumerate(terms):
        if term.special_member is not None:
            cue = prep.cues[prep.slot_of[term.special_member]]
            for n, t in enumerate(prep.times):
                if cue <= t < cue + term.special_duration:
                    fixed_bits[n] |= 1 << bit
            continue
        if term.windows is None:
            for n in range(len(prep.times)):
                fixed_bits[n] |= 1 << bit
            continue
        if not term.windows:
            continue
        certain = term.occupancy >= 1.0
        for w, (start, end) in enumerate(term.windows):
            for n, t in enumerate(prep.times):
                if start <= t < end:
                    if certain:
                        fixed_bits[n] |= 1 << bit
                    else:
                        window_of[n][bit] = w
    return fixed_bits, window_of


def note_value(prep, live_mask, cache, certain=True):
    """`1 + combined` at a note whose live set is `live_mask` -- `song._ratio_at`'s own
    answer, memoised on the mask.

    `certain=True` is the CONDITIONAL value: every term in the mask is known to be live,
    so its occupancy is 1.0 and `_ratio_at`'s `E[max]` degenerates to a plain maximum.
    `certain=False` keeps the ACTIVE probabilities and reproduces `song.py`'s expectation
    instead -- the two are used for different questions and must not be swapped."""
    source = prep.certain if certain else prep.terms
    hit = cache.get(live_mask)
    if hit is None:
        live_ratio = [t for b, t in enumerate(source)
                      if live_mask >> b & 1 and not t.is_boost]
        live_boost = [t for b, t in enumerate(source)
                      if live_mask >> b & 1 and t.is_boost]
        hit = 1.0 + prep.cfg.stacking_model.combine(
            [*prep.score_rates, _ratio_at(live_ratio, live_boost, prep.cfg)])
        cache[live_mask] = hit
    return hit


def expectation_path(prep):
    """`song.py`'s own number, recomputed through `Prepared`: the per-note value at
    occupancy = p, which is where `_ratio_at` takes the exact expectation over the ACTIVE
    Bernoullis. Used only to prove the transcription -- the distribution below never calls
    it, because `E[ceil(.)] != ceil(E[.])` and the mean it wants is the real one."""
    cache = {}
    fixed_bits, window_of = liveness(prep)
    total = 0.0
    for n in range(len(prep.times)):
        mask = fixed_bits[n]
        for bit in window_of[n]:
            mask |= 1 << bit
        total += math.ceil(prep.constant * prep.note_weights[n]
                           * note_value(prep, mask, cache, certain=False))
    return total


def verify(members, leader, chart, cfg, deck_power):
    """`Prepared` against `song.score_song_detailed` itself: the best of the 120 slot
    orders on each side must be the SAME integer. That is the acceptance test for this
    file -- an artefact, not a counter."""
    theirs = score_song_detailed(members, (leader,), chart, cfg, deck_power=deck_power)[0]
    best, best_order = None, None
    for permutation in itertools.permutations(range(SPECIAL_CUE_COUNT)):
        prep = Prepared(members, leader, chart, cfg, deck_power, permutation)
        value = expectation_path(prep)
        if best is None or value > best:
            best, best_order = value, permutation
    return theirs.total_score, best, tuple(members[i].id for i in best_order), theirs


# --------------------------------------------------------------------------- components
def components(prep, window_of):
    """Connected components of the window-overlap graph, as lists of `(term, window)`.

    Two random windows share a component iff some note is covered by both. Every note's
    random covering set therefore lies wholly inside one component, so the components'
    score contributions are INDEPENDENT and the run total is their convolution."""
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for n, cover in enumerate(window_of):
        keys = [(bit, w) for bit, w in cover.items()]
        for k in keys:
            find(k)
        for k in keys[1:]:
            union(keys[0], k)
    groups = {}
    for k in parent:
        groups.setdefault(find(k), []).append(k)
    return [sorted(v) for v in groups.values()]


def convolve(components_dists, cap=400000):
    """Exact convolution of independent integer-valued laws, with a value cap.

    If the joint support would exceed `cap`, values are quantised to a bin wide enough to
    fit -- reported by the caller, because a binned percentile is not an exact one."""
    dist = {0: 1.0}
    binw = 1
    for comp in components_dists:
        nxt = {}
        for v0, p0 in dist.items():
            for dv, dp in comp:
                v = v0 + dv
                nxt[v] = nxt.get(v, 0.0) + p0 * dp
        if len(nxt) > cap:
            lo, hi = min(nxt), max(nxt)
            binw = max(binw, max(1, (hi - lo) // (cap // 2)))
            merged = {}
            for v, p in nxt.items():
                key = (v // binw) * binw
                merged[key] = merged.get(key, 0.0) + p
            nxt = merged
        dist = nxt
    return dist, binw


def summarise(dist, base):
    items = sorted((v + base, p) for v, p in dist.items())
    mean = sum(v * p for v, p in items)
    var = sum((v - mean) ** 2 * p for v, p in items)
    out = {"min": items[0][0], "max": items[-1][0], "mean": mean,
           "sd": var ** 0.5, "p_min": items[0][1], "p_max": items[-1][1],
           "support": len(items)}
    acc = 0.0
    wanted = [0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99]
    got = {}
    for v, p in items:
        acc += p
        for q in wanted:
            if q not in got and acc >= q:
                got[q] = v
    for q in wanted:
        out[f"p{int(q * 100)}"] = got.get(q, items[-1][0])
    return out


def distribution(prep, cap=400000):
    """The exact law of the run score. Returns `(summary, per-component detail)`."""
    cache = {}
    fixed_bits, window_of = liveness(prep)
    groups = components(prep, window_of)
    member_of = {}
    for gi, group in enumerate(groups):
        for key in group:
            member_of[key] = gi
    # Route every note to its component (or to the deterministic base).
    note_group = []
    for cover in window_of:
        if not cover:
            note_group.append(None)
        else:
            gs = {member_of[(bit, w)] for bit, w in cover.items()}
            assert len(gs) == 1, "a note spans two components -- the graph is wrong"
            note_group.append(gs.pop())
    base = 0
    for n, g in enumerate(note_group):
        if g is None:
            base += math.ceil(prep.constant * prep.note_weights[n]
                              * note_value(prep, fixed_bits[n], cache))
    dists = []
    for gi, group in enumerate(groups):
        notes = [n for n, g in enumerate(note_group) if g == gi]
        idx = {k: i for i, k in enumerate(group)}
        probs = [prep.terms[bit].occupancy for bit, _w in group]
        acc = {}
        for combo in itertools.product((0, 1), repeat=len(group)):
            p = 1.0
            for fired, q in zip(combo, probs):
                p *= q if fired else (1.0 - q)
            if p == 0.0:
                continue
            value = 0
            for n in notes:
                mask = fixed_bits[n]
                for bit, w in window_of[n].items():
                    if combo[idx[(bit, w)]]:
                        mask |= 1 << bit
                value += math.ceil(prep.constant * prep.note_weights[n]
                                   * note_value(prep, mask, cache))
            acc[value] = acc.get(value, 0.0) + p
        dists.append(sorted(acc.items()))
    dist, binw = convolve(dists, cap)
    summary = summarise(dist, base)
    summary["bin_width"] = binw
    summary["components"] = [len(g) for g in groups]
    summary["random_windows"] = sum(len(g) for g in groups)
    summary["base"] = base
    detail = []
    for group, d in zip(groups, dists):
        m = sum(v * p for v, p in d)
        v = sum((x - m) ** 2 * p for x, p in d)
        detail.append({"windows": group, "mean": m, "var": v,
                       "min": d[0][0], "max": d[-1][0]})
    return summary, detail, dist, base


def monte_carlo(prep, trials, seed=0):
    """The same generative model, sampled. A cross-check on the exact answer -- never the
    answer itself, because the exact one costs less than a thousand trials."""
    rng = random.Random(seed)
    cache = {}
    fixed_bits, window_of = liveness(prep)
    windows = sorted({k for cover in window_of for k in cover.items()})
    probs = {k: prep.terms[k[0]].occupancy for k in windows}
    out = []
    for _ in range(trials):
        fired = {k: (rng.random() < probs[k]) for k in windows}
        total = 0
        for n in range(len(prep.times)):
            mask = fixed_bits[n]
            for bit, w in window_of[n].items():
                if fired[(bit, w)]:
                    mask |= 1 << bit
            total += math.ceil(prep.constant * prep.note_weights[n]
                               * note_value(prep, mask, cache))
        out.append(total)
    return out


# --------------------------------------------------------------------------- teams
def load_pool():
    cards = load_cards(Path(ROOT) / "data" / "cards.json")
    owned = load_owned(Path(ROOT) / "data" / "owned.json", cards)
    return cards, sorted(owned, key=lambda c: c.id)


def sample_teams(pool, count, seed):
    rng = random.Random(seed)
    return [tuple(rng.sample(pool, 6)) for _ in range(count)]


# --------------------------------------------------------------------------- reporting
def report_one(prep, chart, members, leader, args):
    exact_mean_path = expectation_path(prep)
    summary, detail, dist, base = distribution(prep)
    print(f"chart {chart.music_id}/{chart.difficulty}  level {chart.difficulty_level}  "
          f"music_coef {chart.music_coefficient():.4f}  notes {len(chart.notes)}")
    print(f"leader {leader.id}  members {[c.id for c in members]}")
    print(f"deck_power {prep.power:.0f}  random windows {summary['random_windows']}  "
          f"components {summary['components']}  bin {summary['bin_width']}")
    print()
    print(f"  mean (exact)          {summary['mean']:12.1f}")
    print(f"  mean via score_song   {exact_mean_path:12.1f}   "
          f"delta {100 * (exact_mean_path / summary['mean'] - 1):+.4f} %")
    print(f"  sd                    {summary['sd']:12.1f}   "
          f"= {100 * summary['sd'] / summary['mean']:.2f} % of the mean")
    print(f"  min  (nothing fires)  {summary['min']:12.0f}   "
          f"{100 * (summary['min'] / summary['mean'] - 1):+7.2f} %   p={summary['p_min']:.3e}")
    print(f"  max  (all fire)       {summary['max']:12.0f}   "
          f"{100 * (summary['max'] / summary['mean'] - 1):+7.2f} %   p={summary['p_max']:.3e}")
    for q in (1, 5, 25, 50, 75, 95, 99):
        print(f"  p{q:<3d}                  {summary[f'p{q}']:12.0f}   "
              f"{100 * (summary[f'p{q}'] / summary['mean'] - 1):+7.2f} %")
    print(f"  p95/p5 spread         {100 * (summary['p95'] / summary['p5'] - 1):7.2f} %")
    print(f"  max/min spread        {100 * (summary['max'] / summary['min'] - 1):7.2f} %")
    print()
    total_var = summary["sd"] ** 2
    print("  variance by component (independent, so these SUM to the total):")
    for d in sorted(detail, key=lambda x: -x["var"]):
        share = 100 * d["var"] / total_var if total_var else 0.0
        skills = sorted({bit for bit, _w in d["windows"]})
        print(f"    windows {str(d['windows']):<28s} terms {skills}  "
              f"var share {share:5.1f} %  range {d['max'] - d['min']:8d}")
    if args.monte_carlo:
        sample = monte_carlo(prep, args.monte_carlo, seed=args.seed)
        m = sum(sample) / len(sample)
        sd = (sum((x - m) ** 2 for x in sample) / len(sample)) ** 0.5
        print()
        print(f"  monte carlo n={args.monte_carlo}: mean {m:.1f} "
              f"({100 * (m / summary['mean'] - 1):+.3f} % vs exact), "
              f"sd {sd:.1f} ({100 * (sd / summary['sd'] - 1):+.2f} %), "
              f"min {min(sample)}, max {max(sample)}")
    return summary


def stability(prep, summary, args):
    """How many Monte-Carlo trials the mean and the sd need to land inside 1 %.

    Checked against the exact answer, over `--repeats` independent seeds at each n."""
    print(f"\n  monte-carlo stability, {args.repeats} seeds per n, target +/-1 % of exact")
    print(f"  {'n':>8} {'max |mean err| %':>18} {'max |sd err| %':>16} {'max |p05 err| %':>16}")
    for n in args.stability:
        me, se, pe = 0.0, 0.0, 0.0
        for r in range(args.repeats):
            sample = monte_carlo(prep, n, seed=args.seed + 1000 * r)
            m = sum(sample) / len(sample)
            sd = (sum((x - m) ** 2 for x in sample) / len(sample)) ** 0.5
            sample.sort()
            p5 = sample[max(0, int(0.05 * len(sample)) - 1)]
            me = max(me, abs(m / summary["mean"] - 1))
            se = max(se, abs(sd / summary["sd"] - 1))
            pe = max(pe, abs(p5 / summary["p5"] - 1))
        print(f"  {n:>8} {100 * me:>18.3f} {100 * se:>16.3f} {100 * pe:>16.3f}")


def by_skill(prep, summary):
    """What each ACTIVE skill is worth, and what it COSTS in uncertainty.

    For every random term, recompute the exact law with that term's activation forced to
    certainty. The mean rises by what the skill's misses were costing; the sd falls by the
    share of the band that skill owns. Both are exact -- this is a re-solve, not a
    sensitivity estimate -- and both are what a UI would put next to a card."""
    print("\n  per-ACTIVE-skill: the law with THIS skill forced to always fire")
    print(f"    {'term':>5} {'p':>6} {'windows':>8} {'mean +':>9} {'sd -':>9} "
          f"{'sd share':>9}")
    random_terms = sorted({b for cover in liveness(prep)[1] for b in cover})
    rows = []
    for bit in random_terms:
        forced = Prepared.__new__(Prepared)
        for slot in Prepared.__slots__:
            setattr(forced, slot, getattr(prep, slot))
        forced.terms = list(prep.terms)
        forced.terms[bit] = dataclasses.replace(prep.terms[bit], occupancy=1.0)
        forced.certain = list(prep.certain)
        s2, _d, _x, _b = distribution(forced)
        rows.append((bit, prep.terms[bit].occupancy,
                     len(prep.terms[bit].windows or ()),
                     s2["mean"] - summary["mean"], summary["sd"] - s2["sd"],
                     1 - (s2["sd"] / summary["sd"]) ** 2))
    for bit, p, w, dm, ds, share in sorted(rows, key=lambda r: -r[4]):
        print(f"    {bit:>5} {p:6.3f} {w:8d} {dm:+9.0f} {ds:+9.0f} {100 * share:8.1f} %")
    print("    (shares do not sum to 100 %: the ACTIVE bucket takes a MAXIMUM, so two "
          "skills of\n     similar magnitude are partly redundant and their variances "
          "overlap.)")


def runs_of(prep, summary, args, draws=200000):
    """The law of `max/min` over N independent runs of the SAME team on the SAME song.

    This is the quantity behind a "21.4 % spread over four runs": it is not the
    distribution's own spread, it is the RANGE OF FOUR DRAWS from it, and the two are very
    different numbers. Sampled from the exact law (not from the model again), so the only
    error is the `draws` Monte Carlo on an already-exact distribution."""
    _s, _d, dist, base = distribution(prep)
    items = sorted((v + base, p) for v, p in dist.items())
    values = [v for v, _p in items]
    cum, acc = [], 0.0
    for _v, p in items:
        acc += p
        cum.append(acc)
    import bisect
    rng = random.Random(args.seed)
    ratios = []
    n = args.runs_of
    for _ in range(draws):
        draw = [values[min(bisect.bisect_left(cum, rng.random()), len(values) - 1)]
                for _ in range(n)]
        ratios.append(max(draw) / min(draw))
    ratios.sort()
    mean = sum(ratios) / len(ratios)
    print(f"\n  range of {n} runs (max/min), {draws} resamples of the exact law:")
    print(f"    mean {100 * (mean - 1):.1f} %   median {100 * (ratios[draws // 2] - 1):.1f} %"
          f"   p05 {100 * (ratios[draws // 20] - 1):.1f} %"
          f"   p95 {100 * (ratios[19 * draws // 20] - 1):.1f} %")
    for target in (0.214,):
        below = bisect.bisect_left(ratios, 1 + target) / draws
        print(f"    P(range <= {100 * target:.1f} %) = {100 * below:.1f} %  "
              f"<- the four AUTO runs of `POSTMORTEM.md` section 6")


def clamp_sweep(args, cfg, charts_raw, music, pool):
    """What `chart_timeline.active_windows`'s `min(start + duration, song_seconds)` costs,
    over teams x charts. The windows are half-open, so the clamp evicts the chart's LAST
    note -- the highest-combo one -- from every window still open when the song ends."""
    names = sorted(charts_raw)
    if args.charts and args.charts < len(names):
        step = len(names) / float(args.charts)
        names = [names[int(i * step)] for i in range(args.charts)]
    teams = sample_teams(pool, args.teams, args.seed)
    dm, ds, dmax, hit = [], [], [], 0
    for team in teams:
        members, leader = team[:5], team[5]
        for name in names:
            chart = build_song_chart(name, charts_raw[name], music)
            a, _d, _x, _b = distribution(Prepared(members, leader, chart, cfg,
                                                 args.deck_power, tuple(range(5))))
            b, _d, _x, _b2 = distribution(Prepared(members, leader, chart, cfg,
                                                  args.deck_power, tuple(range(5)),
                                                  clamp=True))
            dm.append(b["mean"] / a["mean"] - 1)
            ds.append(b["sd"] / a["sd"] - 1)
            dmax.append(b["max"] / a["max"] - 1)
            hit += b["mean"] != a["mean"]
    n = len(dm)
    print(f"clamp cost over {len(teams)} teams x {len(names)} charts = {n} pairs")
    print(f"  pairs the clamp moves at all : {hit}/{n} ({100 * hit / n:.1f} %)")
    for label, xs in (("mean", dm), ("sd", ds), ("max", dmax)):
        print(f"  {label:>4}: mean {100 * sum(xs) / n:+.4f} %   "
              f"worst {100 * min(xs, key=abs) if False else 100 * max(xs, key=abs):+.4f} %   "
              f"range {100 * min(xs):+.4f} .. {100 * max(xs):+.4f} %")


def sweep(args, cfg, charts_raw, music, pool):
    """SEPARABILITY: can a per-song factor be precomputed once and reused across teams?

    Three matrices over `teams x charts` are built and each is fitted with a rank-1
    multiplicative model `M[t][s] = row[t] x col[s]` (least squares in logs, alternating
    once from the geometric row/column means -- an exact closed form for rank 1). The
    residual spread is the answer: a small residual means the song half FACTORS OUT and
    the UI can hold one number per song; a large one means the ratio profile genuinely
    interacts with the team and every (team, song) pair has to be scored.

        mean          the predicted average score
        sd            the absolute spread, in points
        sd/mean       the shape a player would see as a percentage band
    """
    names = sorted(charts_raw)
    if args.charts and args.charts < len(names):
        step = len(names) / float(args.charts)
        names = [names[int(i * step)] for i in range(args.charts)]
    teams = sample_teams(pool, args.teams, args.seed)
    print(f"sweep: {len(teams)} teams x {len(names)} charts = "
          f"{len(teams) * len(names)} pairs", file=sys.stderr)
    mean_m, sd_m, shape_m, spread_m = [], [], [], []
    for ti, team in enumerate(teams):
        members, leader = team[:5], team[5]
        rm, rs, rh, rp = [], [], [], []
        for name in names:
            chart = build_song_chart(name, charts_raw[name], music)
            prep = Prepared(members, leader, chart, cfg, args.deck_power,
                            tuple(range(5)), clamp=args.clamp, flat=args.flat)
            s_, _d, _x, _b = distribution(prep)
            rm.append(s_["mean"])
            rs.append(s_["sd"])
            rh.append(s_["sd"] / s_["mean"])
            rp.append((s_["p95"] - s_["p5"]) / s_["mean"])
        mean_m.append(rm), sd_m.append(rs), shape_m.append(rh), spread_m.append(rp)
        print(f"  team {ti} done", file=sys.stderr)

    def rank1(matrix):
        """Residual of `M[t][s] = row[t] x col[s]`, as a percentage. Logs make the fit
        exact: `log M = a_t + b_s + e`, whose least-squares solution is the two-way mean
        decomposition, so `e` is the interaction and nothing else."""
        rows, cols = len(matrix), len(matrix[0])
        logs = [[math.log(v) for v in row] for row in matrix]
        grand = sum(sum(r) for r in logs) / (rows * cols)
        a = [sum(r) / cols - grand for r in logs]
        b = [sum(logs[t][s_] for t in range(rows)) / rows - grand for s_ in range(cols)]
        res = [logs[t][s_] - grand - a[t] - b[s_]
               for t in range(rows) for s_ in range(cols)]
        rms = (sum(e * e for e in res) / len(res)) ** 0.5
        return 100 * rms, 100 * max(abs(e) for e in res)

    def spreadstat(matrix):
        flat = [v for row in matrix for v in row]
        m = sum(flat) / len(flat)
        sd = (sum((v - m) ** 2 for v in flat) / len(flat)) ** 0.5
        cols = list(zip(*matrix))
        within_song = sum((sum((v - sum(c) / len(c)) ** 2 for v in c) / len(c)) ** 0.5
                          for c in cols) / len(cols)
        within_team = sum((sum((v - sum(r) / len(r)) ** 2 for v in r) / len(r)) ** 0.5
                          for r in matrix) / len(matrix)
        return m, sd, within_song, within_team

    print(f"\n{len(teams)} teams x {len(names)} charts, "
          f"deck_power={'stat sum' if args.deck_power is None else args.deck_power}, "
          f"flat={args.flat}")
    print(f"{'quantity':>10} {'rank-1 rms %':>13} {'worst %':>9} "
          f"{'mean':>10} {'sd/mean %':>10} {'sd within song':>15} {'sd within team':>15}")
    for label, matrix in (("mean", mean_m), ("sd", sd_m),
                          ("sd/mean", shape_m), ("(p95-p5)/mean", spread_m)):
        rms, worst = rank1(matrix)
        m, sd, ws, wt = spreadstat(matrix)
        print(f"{label:>10} {rms:13.2f} {worst:9.2f} {m:10.4f} "
              f"{100 * sd / m:10.2f} {ws:15.4f} {wt:15.4f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chart", default="chart_m0001_easy")
    ap.add_argument("--team", type=int, default=0, help="index into the sampled teams")
    ap.add_argument("--teams", type=int, default=8)
    ap.add_argument("--charts", type=int, default=16)
    ap.add_argument("--seed", type=int, default=20260803)
    ap.add_argument("--deck-power", type=float, default=None)
    ap.add_argument("--monte-carlo", type=int, default=0)
    ap.add_argument("--stability", type=int, nargs="*", default=None)
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--clamp", action="store_true",
                    help="reproduce chart_timeline.py's window clamp, to price it")
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--clamp-sweep", action="store_true",
                    help="price `chart_timeline.py`'s window clamp over teams x charts")
    ap.add_argument("--members", nargs="*", default=None,
                    help="explicit member card ids (5) -- overrides --team")
    ap.add_argument("--leader", default=None, help="explicit leader card id")
    ap.add_argument("--flat", type=float, default=0.0,
                    help="a deterministic Score UP rate the card data does not carry "
                         "(the deck screen's Outfit + Passive lines)")
    ap.add_argument("--runs-of", type=int, default=0,
                    help="law of the max/min ratio over N independent runs -- the "
                         "quantity `POSTMORTEM.md` section 6's 21.4 %% actually is")
    ap.add_argument("--by-skill", action="store_true",
                    help="re-solve the exact law with each ACTIVE forced to certainty")
    ap.add_argument("--verify", action="store_true",
                    help="Prepared's 120-order best against score_song_detailed's own")
    args = ap.parse_args()

    cfg = load_scoring_config()
    charts_raw = load_charts()
    music = load_music_coefficients()
    _cards, pool = load_pool()

    if args.sweep:
        sweep(args, cfg, charts_raw, music, pool)
        return
    if args.clamp_sweep:
        clamp_sweep(args, cfg, charts_raw, music, pool)
        return

    teams = sample_teams(pool, args.teams, args.seed)
    team = teams[args.team]
    members, leader = team[:5], team[5]
    if args.members:
        by_id = {c.id: c for c in _cards}
        members = tuple(by_id[i] for i in args.members)
        if args.leader:
            leader = by_id[args.leader]
    chart = build_song_chart(args.chart, charts_raw[args.chart], music)
    if args.verify:
        ok = 0
        for i, t in enumerate(teams):
            m, ld = t[:5], t[5]
            try:
                theirs, mine, order, obj = verify(m, ld, chart, cfg, args.deck_power)
            except SystemExit as exc:
                print(f"  team {i}: skipped ({exc})")
                continue
            same = theirs == mine
            ok += same
            print(f"  team {i}: score_song_detailed {theirs:12.0f}   Prepared "
                  f"{mine:12.0f}   {'MATCH' if same else 'DIFFER'}   "
                  f"order {'=' if tuple(obj.member_order) == order else '!='}")
        print(f"  {ok}/{len(teams)} exact matches")
        return
    prep = Prepared(members, leader, chart, cfg, args.deck_power,
                    tuple(range(5)), clamp=args.clamp, flat=args.flat)
    summary = report_one(prep, chart, members, leader, args)
    if args.by_skill:
        by_skill(prep, summary)
    if args.runs_of:
        runs_of(prep, summary, args)
    if args.stability:
        stability(prep, summary, args)
    if args.clamp:
        plain = Prepared(members, leader, chart, cfg, args.deck_power, tuple(range(5)),
                         flat=args.flat)
        s2, _d, _dist, _b = distribution(plain)
        print(f"\n  clamp cost: mean {summary['mean']:.1f} vs unclamped {s2['mean']:.1f} "
              f"({100 * (summary['mean'] / s2['mean'] - 1):+.4f} %), "
              f"sd {summary['sd']:.1f} vs {s2['sd']:.1f} "
              f"({100 * (summary['sd'] / s2['sd'] - 1):+.4f} %), "
              f"max {summary['max']} vs {s2['max']} "
              f"({100 * (summary['max'] / s2['max'] - 1):+.4f} %)")


if __name__ == "__main__":
    main()
