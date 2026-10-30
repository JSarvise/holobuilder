"""Pick the best card for each Connect socket to maximise the
GLOBAL account score, over the boards as they stand.

A card on a socket multiplies (`1 + connect_permil/1000`) the effect of every
unlocked node inside that card's fixed shape, anchored at the socket
(`build_board` resolves the geometry). Shapes never collide
(0 of 6,936 slot/shape pairs overlap), so a placement only ever helps
its own character's board -- but the objective is the best TEAM's score, so only
placements on pooled characters move the number, and a card can occupy just ONE
socket account-wide.

Method: score each (character, socket, card) placement in isolation against an
all-sockets-empty baseline (the true optimisation baseline), greedily assign the
highest-value card to each socket respecting the one-card-per-socket rule, then
VERIFY the joint score by re-scoring the whole assignment together. The
recommendation is never worse than the current placement.

    tools/recon/proxy/proxyenv/Scripts/python.exe tools/recon/proxy/socket_optimise.py [--pool 60] [--json PATH]

⚠ v1 scope: the boards are taken AS THEY STAND (sockets come after board
optimisation -- feed an optimised board state later). Connect level is the tool's
base level (bloom is not known from the capture). Independent
marginals + greedy matching is a heuristic; the joint re-score is the honest check.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import tree_optimise as to  # noqa: E402
import build_board  # noqa: E402

SOCKETS = ("S-001", "S-002", "S-003", "S-004")

_HIGHLIGHT_RE = __import__("re").compile(r"\[/?highlight\]")


def _placement_detail(chr_id, socket, card_id, skilltree, levels):
    """The game-facing detail for one Connect placement: the exact MULTIPLIER
    (`1 + permil/1000`, at the card's resolved bloom level), the human EFFECT text,
    and WHICH board nodes the card's shape covers — the "what it buffs and to how
    much" view. `None` if any piece is missing (reported upstream, so a
    silent gap here is fine). Mirrors `build_board._connect_multipliers`' geometry."""
    card_connect = skilltree.get("card_connect", {})
    connect = skilltree.get("connect", {})
    extents = skilltree.get("extents", {})
    models = skilltree.get("models", {})
    members = {m["chr"]: m for m in skilltree.get("members", [])}
    effect_id = card_connect.get(card_id)
    effect = connect.get(effect_id) if effect_id else None
    if effect is None:
        return None
    area = extents.get(effect["area"])
    positions = models.get((members.get(chr_id) or {}).get("model")) or {}
    socket_pos = positions.get(socket)
    if area is None or socket_pos is None:
        return None
    level, _basis = levels.for_card(card_id)
    effect_levels = effect.get("levels") or []
    if len(effect_levels) < level:
        return None
    permil = int(effect_levels[level - 1]["v"])
    desc = _HIGHLIGHT_RE.sub("", str(effect_levels[level - 1].get("d") or "")).strip()
    sx, sy = socket_pos
    covered = {(sx + dx, sy + dy) for dx, dy in area}
    buffed = sorted(
        node for node, pos in positions.items()
        if tuple(pos) in covered and not node.startswith("S-")
    )
    return {
        "multiplier": round(1 + permil / 1000.0, 3),
        "permil": permil,
        "level": level,
        "effect": desc,
        "buffed_nodes": buffed,
    }


def _owned_connect_cards() -> set[str]:
    """The account's cards that carry a Connect effect (rarity 4/5 only)."""
    skilltree = to._load(to.DUMPS / "skilltree.json")
    connect_cards = set(skilltree.get("card_connect", {}))
    collection = to._load(to.REPO / "state" / "collection.json")
    cards = collection.get("cards")
    ids = set(cards) if isinstance(cards, dict) else {
        (c.get("id") if isinstance(c, dict) else c) for c in (cards or [])
    }
    return {cid for cid in ids if cid in connect_cards}


def _empty(board: dict) -> dict:
    state = copy.deepcopy(board)
    for entry in state.values():
        entry["slots"] = {}
    return state


def _with_slots(base: dict, assignment: dict[tuple[str, str], str]) -> dict:
    """`base` plus `{(chr, socket): card}` placements."""
    state = copy.deepcopy(base)
    for (cid, socket), card in assignment.items():
        state.setdefault(cid, {"nodes": [], "slots": {}})["slots"][socket] = card
    return state


def _greedy_assign(triples: list[tuple[float, str, str, str]]) -> dict[tuple[str, str], str]:
    """Highest value first, one card and one socket each -- the account-wide
    one-card-per-socket rule. `triples` are `(value, chr, socket, card)`."""
    assignment: dict[tuple[str, str], str] = {}
    used_cards: set[str] = set()
    for _value, cid, socket, card in sorted(triples, reverse=True):
        if card in used_cards or (cid, socket) in assignment:
            continue
        assignment[(cid, socket)] = card
        used_cards.add(card)
    return assignment


def optimise(
    *,
    pool: int,
    board_state_path: Path,
    scenario: str = "account",
    song: str | None = None,
    charts_path: str | None = None,
    leader_id: str | None = None,
    member_ids: tuple[str, ...] | None = None,
) -> dict:
    board = to._load(board_state_path)
    # `--song`: the Oracle scores every placement through the PER-SONG scorer, so
    # a card's socket shape is valued by the score it adds ON THIS SONG. On a solo
    # song the singer-conditional yellow-node bonus is what the shape multiplies,
    # which is exactly the interaction this mode exists to optimise.
    # `--scenario` sets the collection state (account / my-cards-maxed /
    # full-collection-maxed) the placements are valued under.
    oracle = to.Oracle(pool_size=pool, quiet=True, scenario=scenario,
                       song=song, charts_path=charts_path)

    # TEAM mode: when a fixed team is given, value every placement by the score of
    # THAT team (leader + five), not the pool max -- so the sockets are optimised
    # for the same team the Solve won and the board plan was built for. `score_team`
    # reads the state's slots through `board_for`, so a raw board dict scores the
    # Connect multipliers directly. Candidate sockets are the team's own boards,
    # where a card's shape amplifies the red/blue the team actually scores on.
    team_mode = leader_id is not None
    if team_mode:
        def score_state(st: dict) -> float:
            return oracle.score_team(leader_id, member_ids, st)
        # EVERY board is a candidate, not just the team's. A socket on the leader or
        # a member amplifies their RED/BLUE; a socket on a BENCH board amplifies that
        # board's GREEN/YELLOW, which are ACCOUNT-GLOBAL and so raise the team score
        # too — a socket on another member can buff the green tree.
        # A placement that reaches nothing scores 0 and is dropped, so widening the
        # set only adds real placements, never noise (a card is still one socket
        # account-wide, so the greedy assignment cannot double-spend it).
        slot_chars = set(board)
    else:
        score_state = oracle.score
        slot_chars = oracle.pool_characters()

    # `base_current` is the placement carried by THIS state -- the current cards on
    # the current boards for the default, or the current cards on the OPTIMISED
    # boards when `board_state_path` is a `--plan --dump-state` output. Not
    # `oracle.base_score`, which is hard-wired to the on-disk current board.
    base_current = score_state(board)

    empty = _empty(board)
    base_empty = score_state(empty)

    cards = sorted(_owned_connect_cards())
    team_set = set()
    if team_mode:
        team_set = {"chr-" + leader_id.split("-")[1]}
        team_set |= {"chr-" + m.split("-")[1] for m in (member_ids or ())}
    # Candidate sockets: S-001 is the free root on EVERY board; S-002/3/4 must be
    # owned and, in team mode, are only searched on the TEAM's boards — a bench board
    # contributes its free S-001 (account-global green/yellow amplification) but is
    # not worth its own point-costed extra sockets. This is what keeps the widened
    # search tractable (bench boards add one socket each, not four).
    slots: list[tuple[str, str]] = []
    for cid in sorted(slot_chars):
        owned = set((board.get(cid) or {}).get("nodes") or [])
        on_team = (not team_mode) or cid in team_set
        for socket in SOCKETS:
            if socket == "S-001" or (socket in owned and on_team):
                slots.append((cid, socket))

    # Bucket cards by their connect (area, effective-permil): two cards with the SAME
    # shape and the SAME buff at their resolved bloom apply the IDENTICAL connect
    # multiplier, so they score IDENTICALLY on any socket. Score one representative per
    # bucket (~48 cards -> ~13 buckets), then hand its value to every card in the bucket
    # -- EXACT, ~4x fewer scorings than a per-card loop. Bloom is resolved the same
    # way the board is scored (build_board.ConnectLevels, also reused for the detail below).
    skilltree = to._load(to.DUMPS / "skilltree.json")
    levels = build_board.ConnectLevels(None, oracle.collection, oracle.caps)
    _connect = skilltree.get("connect", {})
    _card_connect = skilltree.get("card_connect", {})
    card_bucket: dict[str, tuple] = {}
    for card in cards:
        eff = _connect.get(_card_connect.get(card))
        if not eff:
            continue
        lvl, _basis = levels.for_card(card)
        els = eff.get("levels") or []
        if len(els) >= lvl:
            card_bucket[card] = (eff.get("area"), int(els[lvl - 1]["v"]))
    rep_by_bucket: dict[tuple, str] = {}
    for card in cards:
        b = card_bucket.get(card)
        if b is not None and b not in rep_by_bucket:
            rep_by_bucket[b] = card

    print(f"scoring {len(slots)} sockets x {len(rep_by_bucket)} card-shapes "
          f"({len(slots) * len(rep_by_bucket)} evals, {len(cards)} cards bucketed) "
          f"at ~8 ms ...", file=sys.stderr)
    started = time.monotonic()
    triples: list[tuple[float, str, str, str]] = []
    for cid, socket in slots:
        value_by_bucket: dict[tuple, float] = {}
        for bucket, rep in rep_by_bucket.items():
            v = score_state(_with_slots(empty, {(cid, socket): rep})) - base_empty
            if v > 0:
                value_by_bucket[bucket] = v
        for card in cards:
            b = card_bucket.get(card)
            if b is None:
                # no connect bucket (shouldn't happen for an owned connect card) --
                # scored individually so nothing is silently dropped.
                v = score_state(_with_slots(empty, {(cid, socket): card})) - base_empty
                if v > 0:
                    triples.append((v, cid, socket, card))
            elif b in value_by_bucket:
                triples.append((value_by_bucket[b], cid, socket, card))
    triples.sort(reverse=True)
    print(f"  {len(triples)} placements with positive value in "
          f"{time.monotonic() - started:.0f}s", file=sys.stderr)

    # Greedy assignment: highest value first, one card and one socket each.
    assignment = _greedy_assign(triples)

    # Honest joint check, and never recommend worse than the current placement.
    joint = score_state(_with_slots(empty, assignment)) if assignment else base_empty
    marginal_sum = sum(v for v, c, s, k in triples if assignment.get((c, s)) == k)

    # Enrich each pick with the game-facing detail: exact multiplier, effect text,
    # and which board nodes the card's shape covers ("what it buffs, and to
    # how much") -- `skilltree`/`levels` already built for the bucketing above.
    placements = sorted(
        ({"chr": cid, "socket": socket, "card": card,
          "isolated_value": round(next(v for v, c, s, k in triples
                                       if (c, s, k) == (cid, socket, card)), 1),
          **({"detail": detail}
             if (detail := _placement_detail(cid, socket, card, skilltree, levels))
             else {})}
         for (cid, socket), card in assignment.items()),
        key=lambda p: -p["isolated_value"],
    )
    return {
        "objective": (f"per-song score ({song})" if song else "global account score"),
        "scenario": scenario,
        "song": song,
        "board_state": str(board_state_path),
        "base_empty": round(base_empty, 1),
        "base_current": round(base_current, 1),
        "current_worth_over_empty": round(base_current - base_empty, 1),
        "optimised_joint": round(joint, 1),
        "gain_vs_current": round(joint - base_current, 1),
        "additivity_note": (f"sum of isolated values {marginal_sum:,.1f} vs joint over empty "
                            f"{joint - base_empty:,.1f}"),
        "recommend": "optimised" if joint > base_current else "keep_current",
        "placements": placements,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pool", type=int, default=60)
    ap.add_argument("--board-state", default=str(to.DUMPS / "board_state.json"),
                    help="board state to optimise sockets over. Default: the current "
                         "boards. Pass a `tree_optimise.py --plan --dump-state` output to "
                         "optimise sockets over the OPTIMISED boards (sockets after boards).")
    ap.add_argument("--json", default=None, help="also write the result to PATH")
    ap.add_argument(
        "--song", default=None, metavar="MUSIC_ID",
        help="score placements FOR THIS SONG (per-song scorer) instead of the "
        "general account score. A solo song's singer-conditional yellow bonus is "
        "what the socket shape multiplies. Needs the chart library (see --charts).",
    )
    ap.add_argument(
        "--charts", default=None, metavar="PATH",
        help="chart library for --song (default tools/recon/proxy/dumps/charts.json)",
    )
    ap.add_argument(
        "--scenario", default="account", choices=to.SCENARIOS,
        help="collection state to value placements under (BOARD-EXPORT-CONTRACT.md): "
        "account, my-cards-maxed, full-collection-maxed. Default: account.",
    )
    args = ap.parse_args()

    result = optimise(
        pool=args.pool,
        board_state_path=Path(args.board_state),
        scenario=args.scenario,
        song=args.song,
        charts_path=args.charts,
    )
    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")

    print(f"\nbest card per socket (global score) -- boards from {Path(result['board_state']).name}")
    print(f"  empty sockets      {result['base_empty']:>12,.1f}")
    print(f"  your placement     {result['base_current']:>12,.1f}   "
          f"(+{result['current_worth_over_empty']:,.1f} over empty)")
    print(f"  optimised          {result['optimised_joint']:>12,.1f}   "
          f"({result['gain_vs_current']:+,.1f} vs yours)")
    print(f"  -> recommend: {result['recommend'].upper()}")
    print(f"\n  {'value':>10}  {'character':12s} {'socket':6s} card")
    for p in result["placements"]:
        print(f"  {p['isolated_value']:>10,.1f}  {p['chr']:12s} {p['socket']:6s} {p['card']}")
    if not result["placements"]:
        print("  (no positive placement found)")


if __name__ == "__main__":
    main()
