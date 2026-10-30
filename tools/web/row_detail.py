"""ONE number set per row: the figures a detail view shows for a ranked team,
computed on the row's own basis.

A ranked row is (team, board, slot order) scored by one leaf. Its detail views
— the Unit Score panel, the Score Bonus split, the timeline, the run
distribution — must not be recomputed by routes that each build their OWN board
and their OWN deck power: the same team on the same song then reads 2,298,116 in
the row, 2,269,749 in the panel and 2,119,866 in the timeline. This module is
the one place those figures are produced, from ONE board:

* `figures(team, chart, cfg_board)` — the song figures (estimate, ceiling,
  slot order, deck power) through `song_rank.score_team_on_chart`, the ranker's
  own entry point (Overall Power by default — never the bare modelled pool);
* `unit_score_block(team, cfg_board, cfg_off, order)` — the Overall Power lines
  (`compare_view.panel_lines`) and the five Score Bonus lines
  (`deck_panel_team`), in the row's slot order;
* `distribution(...)` — the exact run-score law (`song_distribution`) at the
  row's deck power and slot order, with its expectation checked against the
  row's own figure (`verified`);
* `board_from_state(...)` — a `{chr: {nodes, slots}}` board state rebuilt into
  the SAME `BoardModel` the develop assembled (`board_model_from_plan` with the
  basis' Connect cards, bench and cast names), not a `build_board` rebuild at
  Connect level 1 without the bench;
* `two_stage_detail(...)` — a two-stage / event row re-developed on ITS leaf
  (`parallel_refine.develop_team`), so the cached rows get a board, a slot
  order and every figure above.

READ-ONLY: nothing here writes a file.
"""
from __future__ import annotations

import dataclasses
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO = Path(__file__).resolve().parents[2]
for _p in (REPO / "src", REPO / "tools" / "recon" / "proxy", REPO / "tools" / "web" / "kernel"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from hbuilder.domain.board_optimizer import BoardPlan  # noqa: E402
from hbuilder.domain.board_plan import (  # noqa: E402
    UNIT_SCORE_K,
    SocketPlacement,
    TeamBoardPlan,
    board_model_from_plan,
)
from hbuilder.domain.deck_panel_team import team_deck_panel  # noqa: E402
from hbuilder.domain.scoring import deck_screen_unit_score, score_with_breakdown  # noqa: E402
from hbuilder.domain.song import timeline as song_timeline  # noqa: E402
from hbuilder.domain.song_distribution import run_score_distribution  # noqa: E402
from hbuilder.domain.song_rank import score_team_ceiling, score_team_on_chart  # noqa: E402
from hbuilder.domain.team import Team  # noqa: E402

SCORE_BONUS_LINES = (
    ("active", "Active Skill"),
    ("outfit", "Outfit Skill"),
    ("passive", "Passive Skill"),
    ("board", "Holomem Board Bonus"),
    ("special", "Special Skill"),
)


def _chr(card) -> str:
    return "chr-" + card.id.split("-")[1]


# ---------------------------------------------------------------------------
# the song figures
# ---------------------------------------------------------------------------
def figures(team: Team, chart, cfg_board, *, cfg_off=None) -> dict:
    """`song_est`, `song_max` (every ACTIVE fires), `song_off` (board off, when
    `cfg_off` is given), `member_order`, `deck_power` — all through the
    ranker's own `score_team_on_chart`."""
    est = score_team_on_chart(team, chart, cfg_board)
    # the ceiling AT the row's own slot order: the top of the row's run law
    mx = score_team_ceiling(team, chart, cfg_board, est.member_order)
    out = {"song_est": est.total_score, "song_max": mx,
           "member_order": list(est.member_order), "deck_power": est.deck_power,
           "deck_power_basis": "server" if cfg_board.song_absolute_deck_power else "modelled"}
    if cfg_off is not None:
        out["song_off"] = score_team_on_chart(team, chart, cfg_off).total_score
    return out


def modal_order(per_chart: Sequence) -> tuple[list[str] | None, float | None]:
    """The slot order that is best on the most charts, and the share of charts
    it is best on. A team has one best order PER SONG; an across row carries
    this as its default and says how often it holds."""
    orders = Counter(tuple(s.member_order) for s in per_chart if getattr(s, "member_order", None))
    if not orders:
        return None, None
    order, count = orders.most_common(1)[0]
    return list(order), round(count / len(per_chart), 3)


# ---------------------------------------------------------------------------
# the Unit Score panel + Score Bonus split
# ---------------------------------------------------------------------------
def score_bonus_lines(team: Team, cfg_board, member_order: Sequence[str] | None = None,
                      *, overall_power: float | None = None, chart=None) -> dict:
    """The deck screen's five Score Bonus lines for `team` on `cfg_board`, in
    `member_order`. Integers in permil, as the game shows them (x 0.1 %). With
    `chart`, the Board line includes that song's per-song (singers') bonus, as the
    deck screen shows it once a song is chosen."""
    got = team_deck_panel(team, cfg_board, member_order, overall_power=overall_power,
                          chart=chart)
    p = got.panel
    return {
        "song": song_board_bonus(team, cfg_board, chart),
        "lines": [{"key": key, "label": label, "permil": getattr(p, key)}
                  for key, label in SCORE_BONUS_LINES],
        "score_bonus_permil": p.score_bonus_permil,
        "unit_score": p.unit_score if overall_power is not None else None,
        "member_order": list(got.card_ids),
        "unmodelled": list(got.unmodelled),
        "model": "hbuilder.domain.deck_panel (the server's panel rules: Active line exact on all 847 "
                 "captured decks, total exact on 92.4 % of them)",
    }


def song_board_bonus(team: Team, cfg_board, chart) -> dict | None:
    """What the boards add on THIS song only (how much extra yellow or red bonus the song
    gets): the YELLOW song score bonus -- each singer's board,
    Connect multipliers included, then the 100-permil cap -- and the RED singer-conditional
    terms (R-002 Score Support, R-044/062/063 Overall Power), which pay only when their
    holomem sings and their scope reaches the team. None without a chart, a cast or a board."""
    board = getattr(cfg_board, "board", None)
    singers = tuple(getattr(chart, "singers", None) or ()) if chart is not None else ()
    if board is None or not singers:
        return None
    from hbuilder.domain.board import SONG_ALL_CAP_PERMIL  # noqa: PLC0415

    scope = getattr(chart, "singer_type", "") or "all"
    raw = board.song_score_bonus_by_singer(singers, scope=scope)
    uncapped = sum(raw.values())
    leader = team.leader.member
    return {
        "scope": scope,
        "singers": sorted(raw),
        "yellow_permil": round(1000 * min(uncapped, SONG_ALL_CAP_PERMIL / 1000.0)),
        "yellow_uncapped_permil": round(1000 * uncapped),
        "yellow_cap_permil": SONG_ALL_CAP_PERMIL,
        "yellow_by_singer": [{"holomem": h, "permil": round(1000 * v)} for h, v in raw.items()],
        "red_support_permil": round(1000 * board.conditional_skill_effect_up_for(
            tuple(team.members), leader, singers)),
        "red_power": round(board.conditional_stat_power_for(tuple(team.members), leader, singers)),
        "leader_sings": leader in raw,
    }


def unit_score_block(team: Team, cfg_board, cfg_off, member_order: Sequence[str] | None = None,
                     chart=None) -> dict:
    """`{"panel": Overall Power lines (compare_view.panel_lines), "score_bonus":
    the five-line split}` on the row's board and slot order."""
    import compare_view  # noqa: PLC0415 — compare_view imports server lazily

    panel = compare_view.panel_lines(team, cfg_board, cfg_off)
    panel.pop("_bd", None)
    split = score_bonus_lines(team, cfg_board, member_order,
                              overall_power=panel.get("overall_power"), chart=chart)
    return {"panel": panel, "score_bonus": split}


# ---------------------------------------------------------------------------
# the run distribution
# ---------------------------------------------------------------------------
def distribution(team: Team, chart, cfg_board, member_order: Sequence[str], deck_power: float,
                 row_score: float | None = None) -> dict:
    """The exact law of the run score on the row's basis. `verified` says the
    law's own expectation reproduced the row's figure exactly."""
    try:
        law = run_score_distribution(team.members, team.leader, chart, cfg_board,
                                     member_order, deck_power)
    except ValueError as exc:
        return {"available": False, "reason": str(exc)}
    law["available"] = True
    if row_score is not None:
        law["row_score"] = row_score
        law["verified"] = law["expected_total"] == row_score
    law["premise"] = ("AUTO" if cfg_board.song_auto else "full-perfect") + \
        "; the only randomness is the ACTIVE activation roll"
    return law


def quantile_fields(got: dict) -> dict:
    """The row fields taken from a `leaf_detail(..., want_distribution=True)`
    result: `p10`/`p50`/`p90` of the exact run-score law, `song_max` (every
    ACTIVE fires), `dist_mean` (the law's expectation) and `dist_verified`
    (that expectation == the re-developed row's `song_est`). Empty when the
    law is unavailable."""
    law = got.get("distribution") or {}
    if not law.get("available"):
        return {}
    return {"p10": law.get("p10"), "p50": law.get("p50"), "p90": law.get("p90"),
            "song_max": got.get("song_max"), "dist_mean": law.get("expected_total"),
            "dist_verified": law.get("verified")}


def detail(team: Team, chart, cfg_board, cfg_off, *, want_distribution: bool = False,
           want_timeline: bool = False, member_order: Sequence[str] | None = None) -> dict:
    """Every figure of one row from ONE board (`cfg_board`). `chart=None` is
    the song-agnostic Unit Score row."""
    out: dict[str, Any] = {}
    order = list(member_order) if member_order else None
    if chart is not None:
        fig = figures(team, chart, cfg_board, cfg_off=cfg_off)
        out.update(fig)
        if order and sorted(order) == sorted(fig["member_order"]):
            out["member_order_requested"] = order
            out["member_order_requested_matches"] = order == fig["member_order"]
        order = fig["member_order"]
    out.update(unit_score_block(team, cfg_board, cfg_off, order, chart=chart))
    if chart is not None and want_timeline:
        out["timeline"] = song_timeline(tuple(team.members), chart, cfg_board, tuple(order))
    if chart is not None and want_distribution:
        out["distribution"] = distribution(team, chart, cfg_board, order, out["deck_power"],
                                           row_score=out["song_est"])
    return out


# ---------------------------------------------------------------------------
# a board state rebuilt into the develop's own BoardModel
# ---------------------------------------------------------------------------
# lives in the solver (the leaf scores boards as they are); re-exported here
from hbuilder.solver.parallel_refine import board_from_state  # noqa: E402,F401


def _bench_slots(ctx, team, sockets) -> dict | None:
    """`{chr: {socket: card}}` the plan gives every off-team board whose sockets it
    re-chose (`RefineContext.socket_bench` with `bench_sockets`), or None when the
    bench keeps the account's cards."""
    if not getattr(ctx, "socket_bench", False) or not getattr(ctx, "bench_sockets", None):
        return None
    team_chrs = {_chr(c) for c in (team.leader, *team.members)}
    out: dict = {c: {} for c in ctx.bench_sockets if c not in team_chrs}
    for p in sockets:
        if p.chr in out:
            out[p.chr][p.socket] = p.card
    return out


def board_state_of(bt) -> dict:
    """`{chr: {nodes, slots}}` of a developed team — the server's own
    `_board_state_of` shape (the plan's nodes, the REAL Connect placements)."""
    slots_by_chr: dict[str, dict[str, str]] = {}
    for p in bt.sockets:
        slots_by_chr.setdefault(p.chr, {})[p.socket] = p.card
    state: dict = {}
    for card in (bt.team.leader, *bt.team.members):
        cplan = bt.plan.per_character.get(card.id)
        if cplan is None:
            continue
        state[_chr(card)] = {"nodes": sorted(cplan.selected), "slots": slots_by_chr.get(_chr(card), {})}
    return state


# ---------------------------------------------------------------------------
# a two-stage / event row, on its own leaf
# ---------------------------------------------------------------------------
_LEAF_CACHE: dict[tuple, tuple[float, dict]] = {}
_LEAF_TTL = 120.0


def _leaf_inputs(basis: str, song: str, difficulty: str, assumptions=None) -> dict:
    """The two-stage builder's own leaf inputs for `basis` on `song` (cached a
    couple of minutes per (basis, song, difficulty): ~0.3 s to build)."""
    import time  # noqa: PLC0415

    import two_stage_build as tsb  # noqa: PLC0415

    key = (basis, song, difficulty, assumptions.key() if assumptions is not None else None)
    held = _LEAF_CACHE.get(key)
    if held is not None and time.monotonic() - held[0] < _LEAF_TTL:
        return held[1]
    if assumptions is not None:
        import ondemand  # noqa: PLC0415
        built = ondemand.build_inputs(assumptions, difficulty, song)
    else:
        built = tsb.leaf_inputs(basis, difficulty, song)
    _LEAF_CACHE.clear()
    _LEAF_CACHE[key] = (time.monotonic(), built)
    return built


def cached_row(basis: str, song: str, difficulty: str, leader: str,
               members: Sequence[str]) -> dict | None:
    """The two-stage cache's row for this exact (leader, member set), if any."""
    import two_stage_build as tsb  # noqa: PLC0415

    cached = tsb._read(tsb.CACHE / tsb.song_file(song, basis, difficulty)) or {}
    want = sorted(members)
    for row in cached.get("rows") or []:
        if row.get("leader") == leader and sorted(row.get("members") or []) == want:
            return {**row, "key": cached.get("key")}
    return None


def leaf_detail(built: Mapping, team: Team, *, refine: bool = False,
                want_distribution: bool = False, want_timeline: bool = False,
                member_order: Sequence[str] | None = None, panel: bool = True,
                chart=None, as_is: bool = False) -> dict:
    """`team` re-developed on a two-stage leaf (`built`: a `leaf_inputs` /
    `two_stage_account.build` result) on `chart` (default `built["chart"]`) and
    every figure from that one board (`panel=False`: the song figures only, plus
    the run law when `want_distribution`). `as_is` adds `song_est_as_is` (the
    team on the account's boards as captured) and `plan_cost` (`_as_is`)."""
    from hbuilder.solver.parallel_refine import RefinePool, develop_team, new_plan_cache  # noqa: PLC0415

    chart = chart if chart is not None else built["chart"]
    ctx = dataclasses.replace(built["ctx"], charts=(chart,))
    with RefinePool(ctx, workers=1) as helper:
        cache = new_plan_cache(ctx, helper._by_id) if ctx.use_plan_cache else None
        bt = develop_team(ctx, team, helper._tree_for, helper._budget_for, chart,
                          plan_cache=cache, refine=refine)
        budget = {_chr(c): helper._budget_for(c) for c in (team.leader, *team.members)}
        helper_tree_for = helper._tree_for
    cfg_board = dataclasses.replace(ctx.cfg, board=bt.board)
    cfg_off = dataclasses.replace(ctx.cfg, board=None)
    if panel:
        out = detail(team, chart, cfg_board, cfg_off, want_distribution=want_distribution,
                     want_timeline=want_timeline, member_order=member_order)
    else:
        out = figures(team, chart, cfg_board)
        if want_distribution:
            out["distribution"] = distribution(team, chart, cfg_board, out["member_order"],
                                               out["deck_power"], row_score=out["song_est"])
    if as_is:
        out.update(_as_is(ctx, team, chart, helper_tree_for, bt))
    by_id = {c.id: c for c in (team.leader, *team.members)}
    out.update({
        "leaf": "two-stage leaf: plan cache seeded from the basis' nominal team"
                + (", activation nodes refined on this chart" if refine else ""),
        "refined": bool(refine),
        "board_state": board_state_of(bt),
        # the bench's root sockets the plan chose with the team's (`RefineContext.socket_bench`)
        "bench_sockets": [{"chr": p.chr, "holomem": (ctx.name_by_chr or {}).get(p.chr),
                           "socket": p.socket, "card": p.card,
                           "multiplier": round(p.multiplier, 3), "value": round(p.value, 1)}
                          for p in bt.sockets
                          if p.chr not in {_chr(c) for c in (team.leader, *team.members)}],
        # with `socket_bench` every unlocked bench socket was re-chosen: its WHOLE map
        # (a socket the plan leaves empty is absent), so the board view can show what moves
        "bench_slots": _bench_slots(ctx, team, bt.sockets),
        "points": {c: sum(p.spent for cid, p in bt.plan.per_character.items()
                          if _chr(by_id[cid]) == c) for c in budget},
        "points_max": budget,
    })
    return out


# ---------------------------------------------------------------------------
# "as it is" against "after the plan"
# ---------------------------------------------------------------------------
CAPTURED_BOARD = REPO / "tools" / "recon" / "proxy" / "dumps" / "board_state.json"
INVENTORY = REPO / "tools" / "recon" / "proxy" / "dumps" / "inventory.json"
# Hologold per board node taken off (`main_Setting.skillTreeResetConsumptionGoldQuantity`;
# the reset refunds the node's materials). Per NODE is the reading the MILP's
# `max_resets` uses (`board_plan_solver.py`); whether the game charges per node or per
# reset action has not been checked in game.
RESET_HOLOGOLD = 1000
START_NODE = "S-001"


def plan_cost(planned: Mapping[str, Any], current: Mapping[str, Sequence[str]],
              prices: Mapping[str, Mapping[str, int]], *, wallet: Mapping[str, int] | None = None,
              hologold: int | None = None, unspent_points: Mapping[str, int] | None = None,
              names: Mapping[str, str] | None = None) -> dict:
    """What moving the team's boards from `current` (`{chr: [nodes]}`, the
    account as captured) to `planned` (`{chr: {"nodes": [...]}}`, the row's
    board) costs, against what the account holds.

    Per character: `buy` (planned, not owned), `reset` (owned, not planned --
    each one `RESET_HOLOGOLD`, its materials and points refunded), and the
    board points the buys need against the points the resets give back plus
    the character's unspent points. Totals: `materials` needed, `refund`,
    `wallet`, `shortfall` (positive entries only: needed - refund - wallet),
    `resets`, `hologold` (+ `hologold_wallet`, `hologold_shortfall`),
    `affordable`. A wallet / point figure nobody measured stays `None`, never
    0. Nodes without a price are counted in `unpriced`."""
    names = names or {}
    per_character = []
    need: dict[str, int] = {}
    refund: dict[str, int] = {}
    resets = 0
    unpriced = 0

    def add(nodes, into) -> int:
        nonlocal unpriced
        points = 0
        for nid in nodes:
            price = prices.get(nid)
            if price is None:
                unpriced += 1
                continue
            for item, qty in price.items():
                if item == "points":
                    points += int(qty)
                else:
                    into[item] = into.get(item, 0) + int(qty)
        return points

    for chr_id, entry in planned.items():
        nodes = entry.get("nodes") if isinstance(entry, Mapping) else entry
        want = set(nodes or ()) - {START_NODE}
        owned = set(current.get(chr_id) or ()) - {START_NODE}
        buy, reset = sorted(want - owned), sorted(owned - want)
        pts_need = add(buy, need)
        pts_back = add(reset, refund)
        resets += len(reset)
        unspent = None if unspent_points is None else unspent_points.get(chr_id)
        short = None if unspent is None else max(0, pts_need - pts_back - int(unspent))
        per_character.append({"chr": chr_id, "name": names.get(chr_id), "buy": buy, "reset": reset,
                              "points_needed": pts_need, "points_refunded": pts_back,
                              "points_unspent": unspent, "points_shortfall": short})
    shortfall = None
    if wallet is not None:
        shortfall = {item: need[item] - refund.get(item, 0) - int(wallet.get(item, 0))
                     for item in need}
        shortfall = {k: v for k, v in shortfall.items() if v > 0}
    gold = resets * RESET_HOLOGOLD
    points_known = all(c["points_shortfall"] is not None for c in per_character)
    return {
        "per_character": per_character,
        "buys": sum(len(c["buy"]) for c in per_character),
        "points": sum(c["points_needed"] for c in per_character),
        "points_refunded": sum(c["points_refunded"] for c in per_character),
        "materials": need, "refund": refund,
        "wallet": dict(wallet) if wallet is not None else None,
        "shortfall": shortfall,
        "resets": resets, "hologold": gold, "hologold_wallet": hologold,
        "hologold_shortfall": None if hologold is None else max(0, gold - int(hologold)),
        "affordable": (None if shortfall is None or hologold is None or not points_known
                       else not shortfall and gold <= int(hologold)
                       and not any(c["points_shortfall"] for c in per_character)),
        "unpriced": unpriced,
        "reset_rule": f"{RESET_HOLOGOLD} Hologold per node taken off, its materials and points "
                      "refunded (the game's own master value, per node)",
    }


_PRICES: dict | None = None


def _account_holdings() -> tuple:
    """`(node prices, cube/hexa wallet, Hologold, unspent points per chr,
    reason)` from the captured on-disk state; each `None` when unreadable."""
    global _PRICES
    import tree_optimise as to  # noqa: PLC0415

    reasons = []
    if _PRICES is None:
        try:
            _PRICES = to._prices()
        except Exception as exc:  # noqa: BLE001
            reasons.append(f"node prices: {exc}")
    wallet = gold = unspent = None
    try:
        wallet, _source = to.balance_from_disk()
    except Exception as exc:  # noqa: BLE001
        reasons.append(f"wallet: {exc}")
    try:
        items = (json.loads(INVENTORY.read_text("utf-8")).get("items") or {})
        gold = int(items["item-gold"]) if "item-gold" in items else None
    except (OSError, ValueError) as exc:
        reasons.append(f"hologold: {exc}")
    try:
        unspent = to._character_points()
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - its loader exits on a missing file
        reasons.append(f"board points: {exc}")
    return _PRICES, wallet, gold, unspent, "; ".join(reasons) or None


def _as_is(ctx, team: Team, chart, tree_for, bt) -> dict:
    """`song_est_as_is` -- the team scored on the account's boards AS CAPTURED
    (`board_state.json`, assembled by `board_from_state` like the develop's own
    board) -- and `plan_cost` of the row's planned boards against that capture
    and the wallet. Each half fails on its own into `{available: False}`."""
    out: dict[str, Any] = {}
    try:
        current = json.loads(CAPTURED_BOARD.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        reason = f"{CAPTURED_BOARD.name} unreadable ({exc}); run refresh_all.py"
        return {"song_est_as_is": None, "as_is": {"available": False, "reason": reason},
                "plan_cost": {"available": False, "reason": reason}}
    try:
        board, _placements = board_from_state(
            team, current, cfg=ctx.cfg, skilltree=ctx.skilltree,
            node_effect_ids=ctx.node_effect_ids, tree_for=tree_for,
            connect_cards=ctx.connect_cards, bench_characters=ctx.bench_characters,
            name_by_chr=ctx.name_by_chr,
            sings=chart is not None and _chr(team.leader) in tuple(chart.singers or ()),
            use_slots=True)
        now = score_team_on_chart(team, chart, dataclasses.replace(ctx.cfg, board=board))
        out["song_est_as_is"] = now.total_score
        out["as_is"] = {"available": True, "song_est": now.total_score,
                        "member_order": list(now.member_order),
                        "board": "the team's characters on their boards as captured "
                                 f"({CAPTURED_BOARD.name}); the other characters as the basis "
                                 "models them"}
    except Exception as exc:  # noqa: BLE001
        out["song_est_as_is"] = None
        out["as_is"] = {"available": False, "reason": f"{type(exc).__name__}: {exc}"}
    try:
        prices, wallet, gold, unspent, reason = _account_holdings()
        if prices is None:
            raise ValueError(reason or "node prices unavailable")
        names = {_chr(c): c.member for c in (team.leader, *team.members)}
        cost = plan_cost(board_state_of(bt),
                         {k: (v or {}).get("nodes") or [] for k, v in current.items()},
                         prices, wallet=wallet, hologold=gold, unspent_points=unspent, names=names)
        cost["available"] = True
        if reason:
            cost["partial"] = reason
        out["plan_cost"] = cost
    except Exception as exc:  # noqa: BLE001
        out["plan_cost"] = {"available": False, "reason": f"{type(exc).__name__}: {exc}"}
    return out


def _whole_plan_cost(whole_plan: Mapping, ctx) -> dict:
    """`plan_cost` of a plan over EVERY board (`materials_plan.plan_for`), only
    the characters whose board changes listed."""
    try:
        current = json.loads(CAPTURED_BOARD.read_text("utf-8"))
        prices, wallet, gold, unspent, reason = _account_holdings()
        if prices is None:
            raise ValueError(reason or "node prices unavailable")
        cost = plan_cost({c: {"nodes": v.get("nodes") or []} for c, v in whole_plan.items()},
                         {k: (v or {}).get("nodes") or [] for k, v in current.items()},
                         prices, wallet=wallet, hologold=gold, unspent_points=unspent,
                         names=dict(ctx.name_by_chr or {}))
        cost["per_character"] = [c for c in cost["per_character"] if c["buy"] or c["reset"]]
        cost["available"] = True
        cost["whole_account"] = True
        if reason:
            cost["partial"] = reason
        return cost
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}


def two_stage_detail(basis: str, song: str, difficulty: str, leader_id: str,
                     member_ids: Sequence[str], *, refine: bool | None = None,
                     want_distribution: bool = False, want_timeline: bool = False,
                     member_order: Sequence[str] | None = None, as_is: bool = True,
                     assumptions=None, want_reset_price: bool = False) -> dict:
    """A two-stage (or event, `refine=False`) row re-developed on the leaf that
    ranked it, with every figure from that one board. `refine=None` follows the
    cached row (`refined`) when the row is in the cache, else the plain leaf.
    `assumptions` (`ondemand.Assumptions`): the on-demand search's account levers
    instead of a fixed `basis` -- the same leaf that ranked the row."""
    built = _leaf_inputs(basis, song, difficulty, assumptions)
    if assumptions is not None and assumptions.replan:
        # the row was ranked on its plan WITHIN THE WALLET (`materials_plan`): the
        # same plan, the same off-team boards, scored by the same leaf
        import materials_plan  # noqa: PLC0415
        planned = materials_plan.plan_for(leader_id, list(member_ids), song,
                                          assumptions.plan_opts())
        if planned is None:
            raise ValueError("the wallet cannot fund a plan for this team")
        built = dict(built, ctx=dataclasses.replace(built["ctx"], board_state=planned[0],
                                                     bench_characters=planned[1], socket_bench=True))
        whole_plan = planned[2]
    else:
        whole_plan = None
    by_id = built["by_id"]
    missing = [c for c in (leader_id, *member_ids) if c not in by_id]
    if missing:
        raise KeyError(f"not in this basis' pool: {missing}")
    team = Team(leader=by_id[leader_id], members=tuple(by_id[m] for m in member_ids))
    row = cached_row(basis, song, difficulty, leader_id, member_ids)
    if refine is None:
        refine = bool(row and row.get("refined"))
    out = leaf_detail(built, team, refine=bool(refine), want_distribution=want_distribution,
                      want_timeline=want_timeline, member_order=member_order, as_is=as_is)
    if as_is and whole_plan is not None:
        # the cost of the WHOLE plan: its off-team resets are refunds the team spends
        out["plan_cost"] = _whole_plan_cost(whole_plan, built["ctx"])
        if want_reset_price and (out["plan_cost"].get("resets") or 0) > 0:
            out["reset_price"] = reset_price(
                _leaf_inputs(basis, song, difficulty, assumptions), team, song, assumptions,
                refine=bool(refine), current=(out["plan_cost"]["resets"], out["song_est"]))
    if row is not None:
        out["row_best"] = row.get("best")
        out["row_matches"] = round(row.get("best") or 0) == round(out["song_est"])
    return out


# ---------------------------------------------------------------------------
# a Highlight row on the WHOLE pool: one board, one slot order
# ---------------------------------------------------------------------------
def _common_order(team: Team, charts: Sequence, cfg_board) -> tuple[tuple[str, ...], float, list]:
    """The ONE slot order with the best total summed over `charts` on `cfg_board`
    (`song_rank.score_team_orders`, the scorer's reference walk: exact), its mean, and
    `[(music_id, total at that order, the chart's own best)]`. Ties: the first order
    the walk reaches, as the scorer's own tie-break."""
    from hbuilder.domain.song_rank import score_team_orders  # noqa: PLC0415

    per = [(c.music_id, score_team_orders(team, c, cfg_board)) for c in charts]
    sums: dict = {}
    for _m, totals in per:
        for order, total in totals.items():
            sums[order] = sums.get(order, 0.0) + total
    best = None
    for order, total in sums.items():
        if best is None or total > sums[best]:
            best = order
    return best, sums[best] / len(charts), [(m, tot[best], max(tot.values())) for m, tot in per]


def pool_yellow(team: Team, charts: Sequence, cfg_board) -> list[dict]:
    """The pool's songs as the plan solver values their yellows (`board_plan_solver.
    _pool_yellow_terms`): `[{"singers", "singer_type", "slope"}]`, one per distinct
    (cast, singer type), `slope` the team's score per permil of that song's bonus on
    `cfg_board` -- `total / (1000 x (1 + bonus))`, the bonus multiplying every note
    (`score_song_detailed`'s `1 + board_score_rate`) -- over the pool's size, so the
    capped terms add up to the pool MEAN the view ranks by."""
    out: dict = {}
    for c in charts:
        if not c.singers or not c.singer_type:
            continue
        total = score_team_on_chart(team, c, cfg_board).total_score
        rate = (cfg_board.board.song_score_bonus_for(c.singers, scope=c.singer_type)
                if cfg_board.board is not None else 0.0)
        key = (tuple(sorted(c.singers)), c.singer_type)
        out[key] = out.get(key, 0.0) + total / (1000.0 * (1.0 + rate)) / len(charts)
    return [{"singers": list(k[0]), "singer_type": k[1], "slope": v} for k, v in sorted(out.items())]


def pool_detail(basis: str, songs: Sequence[str], difficulty: str, leader_id: str,
                member_ids: Sequence[str], *, assumptions=None, refine: bool = True) -> dict:
    """The Highlight pool's "whole pool" view of a row: the best team order in general
    and boards optimised for the whole pool, rather than per song. The song is drawn
    after the team is set, so ONE board and ONE slot order play every song. Returns the
    mean over `songs` at the slot order best summed over them, on the board that
    maximises that mean among:

    * boards planned within the wallet (`assumptions.replan`): the plan for no song
      (`materials_plan.plan_for(song=None)`, song-agnostic), the plan for the pool song
      whose singers on the team are the commonest, and that plan with the yellows valued
      on EVERY pool song, one capped term per song (`pool_yellow`: e.g. a bench
      Raden's yellows count on her pool songs, up to each song's cap);
    * boards as they are: the account's, the only one;
    * developed boards: each singer context's develop (the boards the search scores the
      pool on) and, with `refine`, the best of them with its activation nodes re-chosen
      on the score summed over the WHOLE pool (`develop_refined_team`).

    The best of these candidates, not a proof of the best board; every score is exact.
    Measured on one 23-song pool (account, developed boards, top 3 rows): the refined
    board at the one order is +0.3..+1.9 % over the search's per-song mean,
    the unrefined candidates -4.1..-6.1 %."""
    import two_stage_build as tsb  # noqa: PLC0415
    from hbuilder.solver.parallel_refine import (  # noqa: PLC0415
        RefinePool, develop_refined_team, develop_team, song_cast)

    songs = [str(s) for s in songs if s]
    if not songs:
        raise ValueError("no pool songs")
    built = _leaf_inputs(basis, songs[0], difficulty, assumptions)
    library = built["ex"].load_charts(str(tsb.charts_path(difficulty))).at(difficulty)
    by_music = {c.music_id: c for c in library}
    charts = [by_music[m] for m in songs if m in by_music]
    if not charts:
        raise ValueError(f"no chart of the pool loads at {difficulty}")
    by_id = built["by_id"]
    missing = [c for c in (leader_id, *member_ids) if c not in by_id]
    if missing:
        raise KeyError(f"not in this basis' pool: {missing}")
    team = Team(leader=by_id[leader_id], members=tuple(by_id[m] for m in member_ids))
    team_chrs = {_chr(c) for c in (team.leader, *team.members)}
    # the pool's singer contexts on this team (the search's board key), commonest first
    key_of = lambda c: (frozenset(team_chrs & set(c.singers or ())), c.singer_type)  # noqa: E731
    rep_of: dict = {}
    for c in charts:
        rep_of.setdefault(key_of(c), c)
    reps = [rep_of[k] for k, _n in Counter(key_of(c) for c in charts).most_common()]
    ctx0 = built["ctx"]
    cands: list[tuple[str, Any, Any, Any]] = []      # (label, plan song, ctx, developed team)
    pool_opts: dict = {}                              # label -> the plan's `song_pool`
    with RefinePool(ctx0, workers=1) as helper:
        tree_for, budget_for = helper._tree_for, helper._budget_for
        if assumptions is not None and assumptions.replan:
            import materials_plan  # noqa: PLC0415
            rep = reps[0].music_id
            for song, extra in ((None, None), (rep, None), (rep, "pool")):
                opts = assumptions.plan_opts()
                if extra == "pool":
                    # the pool's yellows, priced on the one-song plan's board
                    last = next((cd for cd in cands if cd[1] == rep), None)
                    if last is None:
                        continue
                    opts["song_pool"] = pool_yellow(
                        team, charts, dataclasses.replace(last[2].cfg, board=last[3].board))
                planned = materials_plan.plan_for(leader_id, list(member_ids), song, opts)
                if planned is None:
                    continue
                ctx = dataclasses.replace(ctx0, board_state=planned[0],
                                          bench_characters=planned[1], socket_bench=True)
                chart = by_music.get(song) if song else None
                if extra == "pool":
                    pool_opts["the plan for the pool, its yellows valued on every song"] = opts["song_pool"]
                cands.append(("the plan for no song in particular" if song is None
                              else "the plan for one song of the pool" if extra is None
                              else "the plan for the pool, its yellows valued on every song",
                              song, ctx, develop_team(ctx, team, tree_for, budget_for, chart)))
            if not cands:
                raise ValueError("the wallet cannot fund a plan for this team")
        elif ctx0.board_state is not None:
            cands.append(("your boards as they are", None, ctx0,
                          develop_team(ctx0, team, tree_for, budget_for, None)))
        else:
            seen: set = set()
            for rep in reps:
                bt = develop_team(dataclasses.replace(ctx0, charts=(rep,)), team, tree_for,
                                  budget_for, rep, refine=False)
                key = json.dumps(board_state_of(bt), sort_keys=True)
                if key not in seen:
                    seen.add(key)
                    cands.append(("the board developed for one song of the pool", rep.music_id, ctx0, bt))
            if refine:
                def pool_sum(board):
                    cfg_b = dataclasses.replace(ctx0.cfg, board=board)
                    return sum(score_team_on_chart(team, c, cfg_b).total_score for c in charts)
                top = max(cands, key=lambda cd: pool_sum(cd[3].board))
                bt = develop_refined_team(
                    team, ctx0.cfg, pool_sum, sings=song_cast(by_music[top[1]]),
                    tree_for=tree_for, budget_for=budget_for, node_effect_ids=ctx0.node_effect_ids,
                    skilltree=ctx0.skilltree, connect_cards=ctx0.connect_cards,
                    bench_characters=ctx0.bench_characters, name_by_chr=ctx0.name_by_chr,
                    plan_cache=None, bench_sockets=ctx0.bench_sockets)[0]
                cands = [top, ("a board developed for the pool, its activation nodes chosen on "
                               "every song", top[1], ctx0, bt)]
        budget = {_chr(c): budget_for(c) for c in (team.leader, *team.members)}
    best = None
    for label, song, ctx, bt in cands:
        cfg_board = dataclasses.replace(ctx.cfg, board=bt.board)
        order, mean, per = _common_order(team, charts, cfg_board)
        if best is None or mean > best[1]:
            best = (label, mean, order, per, song, ctx, bt, cfg_board)
    label, mean, order, per, song, ctx, bt, cfg_board = best
    cfg_off = dataclasses.replace(ctx.cfg, board=None)
    out: dict[str, Any] = {
        "pool": True, "song_est": mean, "member_order": list(order),
        "per_song": [{"music_id": m, "score": s, "own_order_best": b} for m, s, b in per],
        "own_order_mean": sum(b for _m, _s, b in per) / len(per),
        "board_from": label, "plan_song": song if ctx0.board_state is None else None,
        # the plan's pool yellows: /api/board-plan-account re-plans with them (same plan)
        "plan_pool": pool_opts.get(label),
        "candidates": len(cands), "charts": len(charts),
        "lost": [m for m in songs if m not in by_music],
    }
    out.update(unit_score_block(team, cfg_board, cfg_off, list(order), chart=None))
    by_card = {c.id: c for c in (team.leader, *team.members)}
    out.update({
        "board_state": board_state_of(bt),
        "bench_slots": _bench_slots(ctx, team, bt.sockets),
        "points": {c: sum(p.spent for cid, p in bt.plan.per_character.items()
                          if _chr(by_card[cid]) == c) for c in budget},
        "points_max": budget,
    })
    return out


# the plans `reset_price` compares the row's own with: (label, plan option overrides)
RESET_PRICE_POINTS = (
    ("No resets", {"max_resets": 0, "reset_min_gain": 0.0}),
    ("Each reset earns 0.2 %", {"max_resets": None, "reset_min_gain": 0.002}),
    ("Each reset earns 0.05 %", {"max_resets": None, "reset_min_gain": 0.0005}),
)


def reset_price(built: Mapping, team: Team, song: str, assumptions, *, refine: bool,
                current: tuple[int, float]) -> dict:
    """What the row's resets BUY: the same team re-planned within the wallet with
    fewer resets (none; only those earning 0.2 % / 0.05 % of the score each), every
    plan scored by the same leaf as the row. `current` = the row's `(resets, score)`.
    Rather than a reset COUNT cap, this shows the price of each step, so the
    user can see that the last resets often buy almost nothing (e.g. one m0342
    team: 64 resets 1,909,985; 45 at 0.05 % -0.43 %; 0 resets -6.8 %)."""
    import materials_plan  # noqa: PLC0415

    leader_id, member_ids = team.leader.id, [m.id for m in team.members]
    points = [{"label": "This plan", "resets": int(current[0]), "score": float(current[1]),
               "hologold": int(current[0]) * RESET_HOLOGOLD}]
    base = assumptions.plan_opts()
    for label, override in RESET_PRICE_POINTS:
        opts = {**base, **override}
        if opts == base:
            continue
        planned = materials_plan.plan_for(leader_id, member_ids, song, opts)
        if planned is None:
            points.append({"label": label, "resets": None, "score": None, "hologold": None,
                           "reason": "the wallet cannot fund it"})
            continue
        b = dict(built, ctx=dataclasses.replace(built["ctx"], board_state=planned[0],
                                                 bench_characters=planned[1], socket_bench=True))
        est = leaf_detail(b, team, refine=refine, as_is=False)["song_est"]
        current_state = json.loads(CAPTURED_BOARD.read_text("utf-8"))
        n = sum(len(set((current_state.get(c) or {}).get("nodes") or ()) - set(v.get("nodes") or ())
                    - {START_NODE}) for c, v in planned[2].items())
        points.append({"label": label, "resets": n, "score": float(est),
                       "hologold": n * RESET_HOLOGOLD})
    points.sort(key=lambda r: (r["resets"] is None, -(r["resets"] or 0)))
    return {"points": points, "basis": "each plan re-solved within your cubes and hexas and "
            "scored by the row's own leaf"}


def load_ranks(holo: Path) -> dict:
    """The grade thresholds from the master (`main_LiveDeckEvaluationRank.json`
    for the Unit Score, `main_LiveScoreEvaluationRank.json` for a song score,
    by group; songs name their group in `main_Music.json`), all under `holo`
    (`dumps/holodori`)."""

    def read(name):
        try:
            return json.loads((holo / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    deck = read("main_LiveDeckEvaluationRank.json")
    score = read("main_LiveScoreEvaluationRank.json")
    music = read("main_Music.json")
    if not deck or not score:
        return {"available": False,
                "reason": "dumps/holodori/main_Live*EvaluationRank.json not on disk "
                          "(run refresh_all.py)"}

    def grade(letter: str, plus: int) -> str:
        return letter if not plus else (f"{letter}+" if plus == 1 else f"{letter}+{plus}")

    unit = []
    for r in deck:
        d = r.get("data") or {}
        letter = str(d.get("type") or "").rsplit("_", 1)[-1]
        plus = int(r.get("plus") or 0)
        unit.append({"grade": grade(letter, plus), "letter": letter, "plus": plus,
                     "threshold": int(d.get("threshold") or 0), "order": int(d.get("order") or 0)})
    unit.sort(key=lambda g: g["order"])
    groups: dict[str, list] = {}
    for r in score:
        d = r.get("data") or {}
        letter = str(d.get("evaluationRankType") or "").rsplit("_", 1)[-1]
        plus = int(r.get("plus") or 0)
        groups.setdefault(str(r.get("group_id")), []).append(
            {"grade": grade(letter, plus), "letter": letter, "plus": plus,
             "threshold": int(d.get("score") or 0)})
    for rows in groups.values():
        rows.sort(key=lambda g: g["threshold"])
    single = Counter()
    exceptions: dict[str, str] = {}
    for m in music or []:
        g = (m.get("data") or {}).get("singleLiveScoreEvaluationRankGroupId") or m.get(
            "singleLiveScoreEvaluationRankGroupId")
        if g:
            single[g] += 1
    default_single = single.most_common(1)[0][0] if single else "live_score_rank-s001"
    for m in music or []:
        d = m.get("data") or m
        g = d.get("singleLiveScoreEvaluationRankGroupId")
        mid = d.get("id") or m.get("id")
        if g and g != default_single and mid:
            exceptions[str(mid)] = g
    return {
        "available": True,
        "unit_score": unit,
        "song": {"default_group": default_single, "groups": groups, "exceptions": exceptions},
        "source": "master main_LiveDeckEvaluationRank.json / main_LiveScoreEvaluationRank.json "
                  "; a song's group is Music.singleLiveScoreEvaluationRankGroupId",
        "rule": "a grade applies at score >= threshold; the lowest grade has threshold 0",
    }
