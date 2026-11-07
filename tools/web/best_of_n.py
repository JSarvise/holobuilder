"""Where a score falls for one team on one song: the best of N runs, and a given score's percentile.

    python tools/web/best_of_n.py --song m0342 --cards LEADER M1 M2 M3 M4 M5 --runs 26 --score 2993069

The team is developed and scored exactly as the Optimizer's row detail does (the basis'
boards planned for the song, the best slot order), and its run law is the exact one
(`song_distribution.run_score_distribution`). The best of N independent runs follows from
it (`song_distribution.best_of_n`): `P(best <= x) = F(x) ** N`. `--basis maxed` (the
default) scores every card at max level, bloom and SP training with full boards, the
right basis for someone else's team; `account` uses this install's account.

The premise is the scorer's: every note PERFECT (or AUTO with `--auto`), the only luck the
ACTIVE skills' activation rolls. A real best of N also varies with misses, so a score
above every run of the law means the stats, boards or premise differ, not luck.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import leaf_inputs  # noqa: E402  (sets up the solver's import path)
import ondemand  # noqa: E402
import row_detail  # noqa: E402
from hbuilder.domain.song_distribution import best_of_n, run_score_distribution  # noqa: E402
from hbuilder.domain.team import Team  # noqa: E402
from hbuilder.solver.parallel_refine import RefinePool, develop_team, new_plan_cache  # noqa: E402


def where(song: str, cards: list[str], runs: int, score: float | None, *, basis: str = "maxed",
          difficulty: str = "EXPERT", platform: str = "mobile", auto: bool = False,
          refine: bool = False) -> dict:
    """The team's figures, its one-run law summary and `best_of_n` for `runs` runs."""
    built = row_detail._leaf_inputs(basis, song, leaf_inputs._difficulty(difficulty))
    by_id = built["by_id"]
    missing = [c for c in cards if c not in by_id]
    if missing:
        raise KeyError(f"not in the {basis} basis' pool: {missing}")
    team = Team(leader=by_id[cards[0]], members=tuple(by_id[c] for c in cards[1:]))
    chart = built["chart"]
    ctx = dataclasses.replace(ondemand.with_play(built["ctx"], (platform, auto)), charts=(chart,))
    with RefinePool(ctx, workers=1) as helper:
        cache = new_plan_cache(ctx, helper._by_id) if ctx.use_plan_cache else None
        bt = develop_team(ctx, team, helper._tree_for, helper._budget_for, chart,
                          plan_cache=cache, refine=refine)
    cfg = dataclasses.replace(ctx.cfg, board=bt.board)
    fig = row_detail.figures(team, chart, cfg)
    law = run_score_distribution(team.members, team.leader, chart, cfg, fig["member_order"],
                                 fig["deck_power"], keep_law=True)
    one = {k: law[k] for k in ("mean", "sd", "min", "max", "p_max", "p50", "p90", "p99", "exact")}
    return {"song": song, "difficulty": chart.difficulty, "basis": basis, "auto": auto,
            "expected": fig["song_est"], "ceiling": fig["song_max"],
            "member_order": fig["member_order"], "one_run": one,
            "best_of": best_of_n(law["law"], runs, score)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--song", required=True, help="music id, e.g. m0342")
    ap.add_argument("--cards", nargs=6, required=True, metavar="CARD",
                    help="the leader's card id, then the five members'")
    ap.add_argument("--runs", type=int, required=True, help="how many runs the best is taken of")
    ap.add_argument("--score", type=float, default=None, help="a score to place (e.g. a record)")
    ap.add_argument("--basis", default="maxed", choices=sorted(leaf_inputs.BASES))
    ap.add_argument("--difficulty", default="EXPERT")
    ap.add_argument("--platform", default="mobile", choices=ondemand.PLATFORMS)
    ap.add_argument("--auto", action="store_true", help="an AUTO run")
    ap.add_argument("--refine", action="store_true",
                    help="choose the activation nodes on this song too (as a refined row)")
    args = ap.parse_args(argv)
    out = where(args.song, args.cards, args.runs, args.score, basis=args.basis,
                difficulty=args.difficulty, platform=args.platform, auto=args.auto,
                refine=args.refine)
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
