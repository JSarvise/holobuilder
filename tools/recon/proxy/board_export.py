"""Per-character Holomem-Board export for the web frontend.

Emits `dumps/board_export.json` -- one object per character with every board node
(branch, effect, position, own cost, unlock path + path cost, state) and the four
sockets (current card placement + the Connect effect it grants). `--gain` adds the
score oracle's marginal per buyable node (a per-node pool re-score, slower).

Uses the game's 4-neighbour graph (`tree_optimise.adjacency`), so costs and unlock
paths are real; a hex-distance graph would add edges the game lacks and under-cost paths.

Runs in proxyenv, FROM THE REPO ROOT (so hbuilder's collection resolves):

    tools/recon/proxy/proxyenv/Scripts/python.exe tools/recon/proxy/board_export.py [--gain]
"""

from __future__ import annotations

import argparse
import base64
import json
import atomic_io
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import tree_optimise as to  # noqa: E402

SCHEMA = "board-export/v0"
SOCKETS = ("S-001", "S-002", "S-003", "S-004")
ICONS_DIR = to.DUMPS / "member_icons"


def _icon_for(chr_id: str, embed: bool) -> str | None:
    """The character's face icon (extracted by member_icons.py). `embed` inlines a
    self-contained `data:` URI (for a single-page frontend); otherwise a relative
    asset ref. `None` if the PNG is absent -- never a placeholder or a wrong face."""
    png = ICONS_DIR / f"{chr_id}.png"
    if not png.exists():
        return None
    if embed:
        return "data:image/png;base64," + base64.b64encode(png.read_bytes()).decode("ascii")
    return f"member_icons/{chr_id}.png"


def _effect_for(meta: dict, cid: str) -> dict:
    """The effect this node grants THIS character: a per-character effect if the
    node carries one for `cid` (R/B nodes and Gen-scoped greens do), else the
    node's generic effect, else `{}`. `None` effect text means the node does
    nothing for this character on this shared layout -- reported, not hidden."""
    per = (meta.get("chr") or {}).get(cid)
    return per or meta.get("e") or {}


def _describe_effect(eff: dict) -> str | None:
    """Readable text for a node's effect. The master `d` when present; for the
    YELLOW per-song score-bonus family (which ships an empty `d`) derive one from
    the singer scope, so the tooltip is not blank on the nodes that carry the
    whole song optimisation. `None` stays `None` (the node does nothing here)."""
    desc = eff.get("d")
    if desc:
        return desc
    etype = str(eff.get("t") or "")
    if "live_score_bonus_add_permil_up" in etype:
        scope = str(eff.get("tr") or "")
        label = {"solo": "solo", "all": "any", "group": "group"}.get(scope, scope or "?")
        return f"Per-song score bonus on {label} songs sung by this member"
    return None


def build(*, gain: bool, pool: int, embed_icons: bool = False,
          scenario: str = "account", song: str | None = None,
          charts_path: str | None = None,
          board_state_path: str | None = None) -> dict:
    # `board_state_path` overrides the live capture — a test pinning a property
    # of a HISTORICAL board state (e.g. the frozen pre-respec fixture) passes
    # the snapshot it means, instead of silently drifting with the account.
    skilltree = to._load(to.DUMPS / "skilltree.json")
    board = to._load(Path(board_state_path) if board_state_path
                     else to.DUMPS / "board_state.json")
    prices = to._prices()
    unspent = to._character_points()
    nodes_meta = skilltree["nodes"]
    models = skilltree["models"]
    card_connect = skilltree.get("card_connect", {})
    graphs = to._graphs(skilltree)

    oracle = base = pooled = None
    if gain:
        oracle = to.Oracle(pool_size=pool, scenario=scenario,
                           song=song, charts_path=charts_path)
        base = oracle.base_score
        pooled = oracle.pool_characters()

    characters = []
    for member in sorted(skilltree["members"], key=lambda m: m["chr"]):
        cid = member["chr"]
        positions = models[member["model"]]
        entry = board.get(cid) or {}
        owned = set(entry.get("nodes") or [])
        slots = entry.get("slots") or {}
        graph = graphs.get(cid) or {node: set() for node in positions}
        # target -> cheapest chain of UNOWNED nodes reaching it, on the real graph.
        paths = to.cheapest_paths(owned, graph, prices, max_nodes=len(positions))

        # Marginal gain of buying a node NOW: only for buyable-now nodes (path of
        # length 1, so `with_nodes` stays a legal board) and only on a pooled
        # character -- off-pool B/R read a fixed-pool 0 (see Oracle.pool_characters).
        gains: dict[str, float] = {}
        if gain and cid in pooled:
            for nid, path in paths.items():
                if len(path) != 1 or nid.startswith("S-"):
                    continue
                try:
                    gains[nid] = round(oracle.score(oracle.with_nodes({cid: [nid]})) - base, 1)
                except to.BoardError:
                    # life_up and other unrouted families carry no score model by
                    # design -- they are utility, not score -- so a gain is skipped
                    # (left null), never scored as zero.
                    continue

        node_objs = []
        for nid, pos in positions.items():
            meta = nodes_meta.get(nid, {})
            eff = _effect_for(meta, cid)
            if nid in owned or nid == to.ROOT:
                # ROOT (S-001) is the free centre socket: 0 points, always
                # available, and often absent from a capture because nobody
                # "buys" it (test_skilltree_selection.py). It is never unreachable.
                state, path = "owned", []
            else:
                path = list(paths.get(nid, ()))
                state = "buyable" if len(path) == 1 else ("locked" if path else "unreachable")
            node_objs.append({
                "id": nid,
                "branch": meta.get("br"),
                "star": meta.get("lv"),
                "pos": pos,
                "effect": _describe_effect(eff),
                "effect_icon": eff.get("icon"),
                "cost": prices.get(nid, {}),
                "unlock_path": path,
                "unlock_points": sum(prices.get(n, {}).get("points", 0) for n in path),
                "gain": gains.get(nid),
                "state": state,
            })

        socket_objs = []
        for sid in SOCKETS:
            if sid not in positions:
                continue
            placed = slots.get(sid)
            socket_objs.append({
                "socket": sid,
                "pos": positions[sid],
                "point_cost": prices.get(sid, {}).get("points", 0),
                "placed_card": placed,
                "connect_effect": card_connect.get(placed) if placed else None,
            })

        spent = sum(prices.get(n, {}).get("points", 0) for n in owned)
        held = unspent.get(cid)
        characters.append({
            "chr": cid,
            "name": member.get("name", "?"),
            "model": member["model"],
            "icon": _icon_for(cid, embed_icons),
            "points": {
                "unspent": held,
                "spent_on_owned": spent,
                "granted": (held + spent) if held is not None else None,
            },
            "owned_count": len(owned),
            "nodes": node_objs,
            "sockets": socket_objs,
        })

    return {
        "schema": SCHEMA,
        "scenario": scenario,
        "song": song,
        # The branch palette (code -> name/hue/when), the GAME'S own colours, so
        # the frontend paints the tree without a second /api/board fetch or a
        # hardcoded copy that could drift from build_skilltree.py.
        "branches": skilltree.get("branches", {}),
        "gain_included": gain,
        "notes": (
            "Numbers use the fixed 4-neighbour graph (adjacency corrected "
            "2026-08-09). `gain` is the marginal pool re-score of buying that node "
            "now, on pooled characters only (off-pool B/R read 0 under the fixed "
            "pool); null where not computed."
        ),
        "characters": characters,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--gain", action="store_true",
                    help="add per-node marginal gain (slower: one pool re-score per buyable node)")
    ap.add_argument("--pool", type=int, default=60, help="team pool size for the gain oracle")
    ap.add_argument("--embed-icons", action="store_true",
                    help="inline each icon as a data: URI (self-contained JSON) instead of a ref")
    ap.add_argument("--scenario", default="account", choices=to.SCENARIOS,
                    help="collection state to score --gain over (BOARD-EXPORT-CONTRACT.md): "
                         "account, my-cards-maxed, full-collection-maxed. Default: account.")
    ap.add_argument("--song", default=None, metavar="MUSIC_ID",
                    help="score --gain per-song for this chart instead of the general ranking")
    ap.add_argument("--charts", default=None, metavar="PATH",
                    help="chart library for --song (default tools/recon/proxy/dumps/charts.json)")
    ap.add_argument("--out", default=str(to.DUMPS / "board_export.json"))
    args = ap.parse_args()

    doc = build(gain=args.gain, pool=args.pool, embed_icons=args.embed_icons,
                scenario=args.scenario, song=args.song, charts_path=args.charts)
    atomic_io.write_text(
        Path(args.out),
        json.dumps(doc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    chars = len(doc["characters"])
    nodes = sum(len(c["nodes"]) for c in doc["characters"])
    buyable = sum(1 for c in doc["characters"] for n in c["nodes"] if n["state"] == "buyable")
    icons = sum(1 for c in doc["characters"] if c["icon"])
    print(f"{chars} characters, {nodes} nodes ({buyable} buyable), {icons} icons -> {args.out} "
          f"(gain={'yes' if args.gain else 'no'}, icons={'embed' if args.embed_icons else 'ref'})",
          file=sys.stderr)


if __name__ == "__main__":
    main()
