"""The two-stage member search: a cheap SCREEN chooses where to look, the EXACT
leaf decides what is true.

    1. exact-score a random sample of legal member 5-sets;
    2. fit the screen (`kernel/surrogate.py`: per card + gate counts + card pairs,
       on log score) to every exact score taken so far;
    3. rank EVERY legal set by the screen and exact-score the best `step` not yet
       scored;
    4. repeat 2-3 until the top of the exact ranking stops changing, or the
       round / time budget ends.

**Every score this returns is the exact leaf's.** The screen never contributes a
number, only an order of visit, so a wrong screen costs recall and never
correctness. What it CANNOT give is a certificate: a set the screen never
proposed is not scored, so "best found" is not "proven best". `stats` says how
many of the legal sets were scored so a caller can say so.

Web/recon side: this module needs the compiled screen and takes the exact
scorer as a function, so it runs over a `RefinePool`, a worker pool, or (in the
tests) an oracle table.
"""
from __future__ import annotations

import math
import random
import sys
import time
from pathlib import Path
from typing import Callable, Sequence

_KERNEL = Path(__file__).resolve().parent / "kernel"
if str(_KERNEL) not in sys.path:
    sys.path.insert(0, str(_KERNEL))

import surrogate  # noqa: E402

ScoreSets = Callable[[list[tuple[int, ...]]], Sequence[float]]


def random_legal_sets(holomems: Sequence[str], count: int, rng: random.Random,
                      exclude=()) -> list[tuple[int, ...]]:
    """`count` distinct legal sets (ascending pool indices, five different
    holomems), uniform over the legal sets, none of them in `exclude`."""
    n = len(holomems)
    seen = set(exclude)
    out: list[tuple[int, ...]] = []
    attempts = 0
    while len(out) < count:
        attempts += 1
        if attempts > 200 * count + 1000:
            break                      # the pool has fewer legal sets than asked for
        pick = tuple(sorted(rng.sample(range(n), 5)))
        if pick in seen or len({holomems[i] for i in pick}) != 5:
            continue
        seen.add(pick)
        out.append(pick)
    return out


def search(
    cards: Sequence,
    score_sets: ScoreSets,
    *,
    initial: int = 5_000,
    step: int = 2_000,
    max_rounds: int = 15,
    stable_top: int = 10,
    stable_rounds: int = 2,
    seed: int = 20261013,
    seeds: Sequence[tuple[int, ...]] = (),
    neighbours: int = 0,
    net: int = 0,
    net_window: int = 100_000,
    deadline: float | None = None,
    on_round: Callable[[dict], None] | None = None,
) -> dict:
    """Run the search over `cards` (the pool; index tuples refer to its order).

    `score_sets(list of ascending index 5-tuples) -> exact scores`, same order.
    `seeds` are sets to score in the first batch whatever the sample draws (an
    incumbent, yesterday's answer). Stops after `max_rounds` screen rounds, at
    `deadline` (a `time.perf_counter()` value, checked between rounds), or when
    the best `stable_top` exact sets have not changed for `stable_rounds`
    consecutive rounds.

    `neighbours` adds a proposal source that does NOT go through the screen:
    after every exact batch, each of the best `neighbours` exact sets that has
    not been expanded yet has all its legal one-card swaps exact-scored. The
    screen only sees structure up to card pairs; a set whose value it cannot
    explain is reached this way if a set one swap away is already near the top.

    `net` widens what the SCREEN proposes: besides its best `step` sets, every
    card is guaranteed `net` proposals that contain it, taken in the screen's
    order from its best `net_window` unseen sets. The screen's top is one
    region of the space; a set whose value it under-predicts by more than that
    window's spread sits in another, and a per-card quota is what reaches it.
    At most `net` x pool size extra sets a round.

    A seed that is not a legal set of this pool, or an exact score that is not
    a finite number, raises `ValueError` — neither may reach the fit. The
    defaults are a measured setting (true best found in 40/40 oracle runs, ~14k
    exact evaluations).

    Returns `{"ranking": [(score, set), ...] best first (every scored set),
    "rounds": [...], "stats": {...}}`."""
    cards = list(cards)
    holomems = [c.member for c in cards]
    screen = surrogate.Screen(cards)
    rng = random.Random(seed)
    scored: dict[tuple[int, ...], float] = {}
    rounds: list[dict] = []
    started = time.perf_counter()

    def take(sets: list[tuple[int, ...]]) -> float:
        t0 = time.perf_counter()
        if sets:
            values = list(score_sets(sets))
            if len(values) != len(sets):
                raise ValueError(f"the scorer returned {len(values)} scores for {len(sets)} sets")
            for s, value in zip(sets, values):
                value = float(value)
                if not math.isfinite(value):
                    raise ValueError(f"the scorer returned {value!r} for set {s}")
                scored[s] = value
        return time.perf_counter() - t0

    expanded: set[tuple[int, ...]] = set()
    count = len(cards)

    def take_neighbours() -> tuple[float, int]:
        """Exact-score every legal one-card swap of the best `neighbours` sets
        not expanded before. `(seconds, how many sets were scored)`."""
        if neighbours <= 0:
            return 0.0, 0
        fresh: dict[tuple[int, ...], None] = {}
        for _value, members in top(neighbours):
            if members in expanded:
                continue
            expanded.add(members)
            for position in range(5):
                rest = members[:position] + members[position + 1:]
                names = {holomems[i] for i in rest}
                for card in range(count):
                    if card in members or holomems[card] in names:
                        continue
                    swapped = tuple(sorted(rest + (card,)))
                    if swapped not in scored:
                        fresh.setdefault(swapped)
        return take(list(fresh)), len(fresh)

    def top(n: int) -> list[tuple[float, tuple[int, ...]]]:
        return sorted(((v, s) for s, v in scored.items()), key=lambda r: (-r[0], r[1]))[:n]

    first = [tuple(s) for s in dict.fromkeys(tuple(sorted(s)) for s in seeds)]
    for s in first:
        if (len(s) != 5 or len(set(s)) != 5 or not all(isinstance(i, int) and 0 <= i < count for i in s)
                or len({holomems[i] for i in s}) != 5):
            raise ValueError(f"seed {s} is not a legal member set of this {count}-card pool")
    # The random sample is drawn in full whatever the seeds: a first fit on
    # seeds alone would be a fit on one corner of the space.
    first += random_legal_sets(holomems, initial, rng, exclude=first)
    exact_seconds = take(first)
    neighbour_seconds, neighbour_sets = take_neighbours()
    exact_seconds += neighbour_seconds
    legal = None
    leaders = top(stable_top)
    unchanged = 0
    record = {"round": 0, "exact_evals": len(scored), "best": leaders[0][0] if leaders else None,
              "exact_seconds": exact_seconds, "fit_seconds": 0.0, "scan_seconds": 0.0,
              "fit_mae_log": None, "top_changed": True, "neighbour_sets": neighbour_sets}
    rounds.append(record)
    if on_round:
        on_round(record)
    stop = "max_rounds"
    for number in range(1, max_rounds + 1):
        if deadline is not None and time.perf_counter() >= deadline:
            stop = "deadline"
            break
        # Sets the exact scorer valued at <= 0 carry no log; they stay scored
        # (so they are not proposed again) and are left out of the fit.
        fit_sets = [s for s, v in scored.items() if v > 0]
        if len(fit_sets) < 2:
            stop = "nothing_to_fit"
            break
        t0 = time.perf_counter()
        mae = screen.fit(fit_sets, [math.log(scored[s]) for s in fit_sets])
        fit_seconds = time.perf_counter() - t0
        t0 = time.perf_counter()
        wide, _predictions, legal = screen.best(max(step, net_window) if net > 0 else step,
                                                scored.keys())
        proposed = wide[:step]
        net_sets = 0
        if net > 0:
            held = [0] * count
            for members in proposed:
                for card in members:
                    held[card] += 1
            for members in wide[step:]:
                if any(held[card] < net for card in members):
                    proposed.append(members)
                    net_sets += 1
                    for card in members:
                        held[card] += 1
        scan_seconds = time.perf_counter() - t0
        if not proposed:
            stop = "exhausted"
            break
        exact_seconds = take(proposed)
        neighbour_seconds, neighbour_sets = take_neighbours()
        exact_seconds += neighbour_seconds
        now = top(stable_top)
        changed = [s for _v, s in now] != [s for _v, s in leaders]
        leaders = now
        unchanged = 0 if changed else unchanged + 1
        record = {"round": number, "exact_evals": len(scored), "best": leaders[0][0],
                  "exact_seconds": exact_seconds, "fit_seconds": fit_seconds,
                  "scan_seconds": scan_seconds, "fit_mae_log": mae, "top_changed": changed,
                  "neighbour_sets": neighbour_sets, "net_sets": net_sets}
        rounds.append(record)
        if on_round:
            on_round(record)
        if unchanged >= stable_rounds:
            stop = "stable"
            break
    return {
        "ranking": top(len(scored)),
        "rounds": rounds,
        "stats": {
            "stop": stop, "exact_evals": len(scored), "legal_sets": legal,
            "scored_fraction": (len(scored) / legal) if legal else None,
            "seconds": time.perf_counter() - started,
            "exact_seconds": sum(r["exact_seconds"] for r in rounds),
            "screen_seconds": sum(r["fit_seconds"] + r["scan_seconds"] for r in rounds),
            "initial": initial, "step": step, "seed": seed, "neighbours": neighbours, "net": net,
            "net_evals": sum(r.get("net_sets", 0) for r in rounds),
            "neighbour_evals": sum(r["neighbour_sets"] for r in rounds),
        },
    }


def objective_scorer(score: Callable[[object, list[tuple[int, ...]]], Sequence[float]],
                     bonus: Sequence[tuple[int, int]], objective: str,
                     ) -> Callable[[object, list[tuple[int, ...]]], list[float]]:
    """`score(leader, sets)` turned into the event objective, so the whole search —
    screen fit, proposals, neighbours, leader triage — ranks by it instead of
    re-sorting a list found by score. `bonus[i]` = pool card i's
    `(live permil, song permil)` from `marathon.card_bonus_table`; E sums the five
    members, P takes the largest, the leader slot adds nothing."""
    from hbuilder.domain.marathon import event_objective  # noqa: PLC0415

    if objective == "score":
        return lambda leader, sets: [float(v) for v in score(leader, sets)]

    def wrapped(leader, sets):
        sets = list(sets)
        return [event_objective(value, objective=objective,
                                live_permil=sum(bonus[i][0] for i in members),
                                song_permil=max(bonus[i][1] for i in members))
                for value, members in zip(score(leader, sets), sets)]
    return wrapped


def _fires(gate, cards, members) -> bool:
    """Whether a member set satisfies an outfit gate `(kind, target, count)` —
    `kind` "type" or "generation", as `members_first.gate_of` reads it. An
    unknown kind is treated as not fired (the set simply joins no gate tier)."""
    kind, target, count = gate
    if kind == "type":
        return sum(cards[i].type == target for i in members) >= count
    if kind == "generation":
        return sum(target in cards[i].generations for i in members) >= count
    return False


def search_song(
    cards: Sequence,
    leaders: Sequence,
    score: Callable[[object, list[tuple[int, ...]]], Sequence[float]],
    *,
    base_leader: object,
    always: Sequence = (),
    gate_of: Callable[[object], tuple | None] | None = None,
    margin: float = 0.04,
    max_searched: int = 12,
    triage_top: int = 300,
    tier: int = 100,
    seed: int = 20261013,
    deadline: float | None = None,
    on_leader: Callable[[dict], None] | None = None,
    **search_options,
) -> dict:
    """The best (leader, five members) of ONE song without a search per leader.

    A leader moves the score through its outfit (often gated on the members'
    type / generation counts), its own character's board role and whether it
    sings — so most leaders are close to one another and a few (the song's
    singers) are far ahead. On m0334, for example: 2 singers +18-21 %, the other 82
    within 6.8 % of each other. Hence:

    1. one `search` under `base_leader` (any leader; it only has to produce
       good member sets);
    2. TRIAGE — every leader exact-scored on a short list: the base ranking's
       top `triage_top`, plus its top `tier` sets that fire that leader's
       outfit gate, plus its top `tier` sets containing that leader's own
       holomem (the two ways a leader's best set can sit low in a list made
       without it);
    3. a full `search` for every leader in `always` (pass the song's singers)
       and every leader whose triage best is within `margin` of the best
       triage score, seeded with its own triage list.

    `max_searched` caps step 3 at the `always` leaders plus the best-triaged
    ones up to that many in all. It only binds on a FLAT song — e.g. m0001 (no
    cast): 67 of 84 leaders within 4 %, the top three within 0.01 %
    — where searching them all is the 80 s this function exists to avoid and
    the leaders left on their triage row differ from the best by little.

    `score(leader, sets) -> exact scores`; `leaders` are opaque to this
    function apart from `.member`. Each row says how its number was obtained:
    `"searched"` (step 3) or `"triage"` (the best of its short list — a LOWER
    bound of what a search would find, exact as a score). Every row carries
    `ranking`, its best 100 `(score, set)` under that leader (the search's
    ranking, or the triage short list's), so a caller can show the next teams. `deadline` is checked
    before each full search; leaders not reached keep their triage row.

    Returns `{"rows": [...] best first, "base": the base search's stats,
    "stats": {...}}`."""
    cards = list(cards)
    started = time.perf_counter()
    evals = 0

    def take(leader, sets):
        nonlocal evals
        sets = list(sets)
        evals += len(sets)
        return [float(v) for v in score(leader, sets)] if sets else []

    base = search(cards, lambda sets: take(base_leader, sets), seed=seed, **search_options)
    base_ranking = base["ranking"]
    shortlist = [s for _v, s in base_ranking[:triage_top]]
    rows = []
    triage_lists = {}
    for position, leader in enumerate(leaders):
        chosen = dict.fromkeys(shortlist)
        gate = gate_of(leader) if gate_of is not None else None
        if gate is not None:
            fired = 0
            for _v, s in base_ranking:
                if _fires(gate, cards, s):
                    chosen.setdefault(s)
                    fired += 1
                    if fired >= tier:
                        break
        own = 0
        for _v, s in base_ranking:
            if any(cards[i].member == leader.member for i in s):
                chosen.setdefault(s)
                own += 1
                if own >= tier:
                    break
        sets = list(chosen)
        scored = sorted(zip(take(leader, sets), sets), key=lambda r: (-r[0], r[1]))
        triage_lists[position] = scored
        rows.append({"position": position, "leader": leader, "status": "triage",
                     "best": scored[0][0], "members": scored[0][1],
                     "triage_best": scored[0][0], "triage_sets": len(sets), "exact_evals": len(sets),
                     # the short list's own order: the next sets under this leader
                     # (a searched row replaces it with its search ranking)
                     "ranking": scored[:100]})
    best_triage = max(r["best"] for r in rows)
    forced = {id(leader) for leader in always}
    wanted = [r for r in rows
              if id(r["leader"]) in forced or r["best"] >= (1.0 - margin) * best_triage]
    wanted.sort(key=lambda r: (id(r["leader"]) not in forced, -r["best"]))
    within_margin = len(wanted)
    keep = max(max_searched, sum(id(r["leader"]) in forced for r in wanted))
    wanted = wanted[:keep]
    skipped = 0
    for row in wanted:
        if deadline is not None and time.perf_counter() >= deadline:
            skipped += 1
            continue
        leader = row["leader"]
        result = search(cards, lambda sets, leader=leader: take(leader, sets), seed=seed,
                        seeds=[s for _v, s in triage_lists[row["position"]][:triage_top]],
                        **search_options)
        best, members = result["ranking"][0]
        row.update(status="searched", best=best, members=members,
                   exact_evals=row["exact_evals"] + result["stats"]["exact_evals"],
                   stop=result["stats"]["stop"], ranking=result["ranking"][:100])
        if on_leader:
            on_leader(row)
    rows.sort(key=lambda r: (-r["best"], r["position"]))
    return {
        "rows": rows,
        "base": base["stats"],
        "stats": {
            "leaders": len(rows), "searched": sum(r["status"] == "searched" for r in rows),
            "not_reached": skipped, "exact_evals": evals, "margin": margin,
            "within_margin": within_margin, "max_searched": max_searched,
            "triage_top": triage_top, "tier": tier, "legal_sets": base["stats"]["legal_sets"],
            "seconds": time.perf_counter() - started,
        },
    }


def chart_sample(charts: Sequence, count: int = 16) -> list:
    """`count` charts standing in for all of them while a search is running:
    the cast kinds (solo / group / none) in proportion, at least one of each
    present, and within a kind spread evenly over the charts ordered by note
    count. Deterministic — the same library gives the same sample.

    The sample only GUIDES the search; whatever it finds is re-scored on every
    chart before anything is reported (`search_general`)."""
    charts = list(charts)
    if len(charts) <= count:
        return charts
    kinds: dict[object, list] = {}
    for chart in charts:
        kinds.setdefault(getattr(chart, "singer_type", None) or None, []).append(chart)
    quota = {kind: max(1, round(count * len(group) / len(charts))) for kind, group in kinds.items()}
    while sum(quota.values()) > count:                       # trim the largest
        quota[max(quota, key=lambda k: (quota[k], len(kinds[k])))] -= 1
    while sum(quota.values()) < count:                       # top up the most under-sampled
        quota[max(quota, key=lambda k: len(kinds[k]) / quota[k])] += 1
    picked = []
    for kind in sorted(kinds, key=str):
        group = sorted(kinds[kind], key=lambda c: (len(c.notes), getattr(c, "music_id", "")))
        take = min(quota[kind], len(group))
        for k in range(take):
            picked.append(group[(2 * k + 1) * len(group) // (2 * take)])
    return picked


def fitted_chart_sample(scores: Sequence[Sequence[float]], count: int) -> list[int]:
    """The `count` chart indices whose plain mean best tracks each team's mean
    over EVERY chart: forward selection, each step adding the chart that most
    lowers `sum over teams of (sample mean / full mean - 1)^2`. `scores[t][k]` is
    team t's score on chart k (`RefinePool.batch_score_per_chart`), for teams
    drawn at random -- no search needed before the search.

    Why not `chart_sample`: spreading charts by note count says nothing about how
    a TEAM's score varies from chart to chart, and on a maxed account (every
    singer node bought: +4 % on a song one of the team's holomems sings) its 6
    charts ranked the strong teams worse than chance (Spearman -0.24; 6 random
    charts 0.64; these 6 fitted on 300 random teams 0.96, the best team 2nd, 0.01 %
    under the 1st). Deterministic: ties go to the lower index."""
    teams = [list(map(float, row)) for row in scores if row and sum(row) > 0]
    if not teams:
        raise ValueError("no team with a positive score to fit the sample on")
    charts = len(teams[0])
    if any(len(row) != charts for row in teams):
        raise ValueError("every team needs a score on every chart")
    if count >= charts:
        return list(range(charts))
    means = [sum(row) / charts for row in teams]
    chosen: list[int] = []
    sums = [0.0] * len(teams)
    for step in range(1, count + 1):
        best = None
        for k in range(charts):
            if k in chosen:
                continue
            err = 0.0
            for t, row in enumerate(teams):
                e = (sums[t] + row[k]) / step / means[t] - 1.0
                err += e * e
            if best is None or err < best[0]:
                best = (err, k)
        chosen.append(best[1])
        sums = [s + row[best[1]] for s, row in zip(sums, teams)]
    return chosen


def search_general(
    cards: Sequence,
    leaders: Sequence,
    score_sample: Callable[[object, list[tuple[int, ...]]], Sequence[float]],
    score_full: Callable[[object, list[tuple[int, ...]]], Sequence[float]],
    *,
    base_leader: object,
    gate_of: Callable[[object], tuple | None] | None = None,
    keep: int = 100,
    extra: Sequence[tuple[object, tuple[int, ...]]] = (),
    **song_options,
) -> dict:
    """The best (leader, five members) IN GENERAL — over many songs at once.

    Scoring a team on every chart costs as many leaf evaluations as there are
    charts, so the search runs on a SAMPLE (`score_sample`, e.g. the mean over
    `chart_sample`) through `search_song`, and then every candidate it produced
    is re-scored on the whole library (`score_full`): the best `keep` sets of
    each fully searched leader, the best set of each triaged leader, and
    `extra` — `(leader, set)` pairs a caller already believes in, such as each
    song's own winner.

    **Every reported score is `score_full`'s.** The sample decides only which
    candidates exist; `stats.sample_agreement` says how well it ranked them, so
    a sample that misleads is visible rather than silent.

    Returns `{"rows": one per leader, best first (full score), "ranking":
    [(full score, leader, set), ...] over all candidates, "stats": {...}}`."""
    started = time.perf_counter()
    cards = list(cards)
    found = search_song(cards, leaders, score_sample, base_leader=base_leader,
                        gate_of=gate_of, **song_options)
    position = {id(leader): k for k, leader in enumerate(leaders)}
    candidates: dict[int, dict[tuple[int, ...], float | None]] = {}
    for row in found["rows"]:
        sets = candidates.setdefault(position[id(row["leader"])], {})
        if row["status"] == "searched":
            for value, members in row["ranking"][:keep]:
                sets[members] = value
        else:
            sets[row["members"]] = row["best"]
    for leader, members in extra:
        candidates.setdefault(position[id(leader)], {}).setdefault(tuple(sorted(members)), None)
    ranking = []
    pairs = []                                  # (sample score, full score) where both exist
    full_evals = 0
    for k, sets in candidates.items():
        order = list(sets)
        full_evals += len(order)
        for members, value in zip(order, score_full(leaders[k], order)):
            value = float(value)
            ranking.append((value, k, members))
            if sets[members] is not None:
                pairs.append((sets[members], value))
    ranking.sort(key=lambda r: (-r[0], r[1], r[2]))
    status = {position[id(r["leader"])]: r["status"] for r in found["rows"]}
    rows, seen = [], set()
    for value, k, members in ranking:
        if k in seen:
            continue
        seen.add(k)
        rows.append({"position": k, "leader": leaders[k], "best": value, "members": members,
                     "status": status[k], "candidates": len(candidates[k])})
    by_sample = sorted(pairs, key=lambda r: -r[0])
    best_full = max(v for _s, v in pairs) if pairs else None
    agreement = {
        "candidates": len(pairs),
        # where the full objective's best candidate sat in the SAMPLE's order
        "rank_of_full_best_in_sample_order": (
            next(i for i, (_s, v) in enumerate(by_sample) if v == best_full) + 1 if pairs else None),
        "sample_best_over_full_best": (by_sample[0][1] / best_full) if pairs else None,
    }
    return {
        "rows": rows,
        "ranking": [(value, leaders[k], members) for value, k, members in ranking],
        "stats": {
            "leaders": len(rows), "searched": found["stats"]["searched"],
            "sample_exact_evals": found["stats"]["exact_evals"], "full_evals": full_evals,
            "sample_seconds": found["stats"]["seconds"],
            "seconds": time.perf_counter() - started, "keep": keep, "extra": len(extra),
            "sample_agreement": agreement, "legal_sets": found["stats"]["legal_sets"],
        },
    }
