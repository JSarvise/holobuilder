"""Board and socket helpers the web calls on demand (`tools/web/hbweb/basis.py` imports this
module by path): a plan's per-character buy/sell diff (`_apply_plan`) and point budget
(`_annotate_plan_budget`), the Connect socket picks (`connect_cards_for`), and the maxed-account scenario's bonuses and bench boards. Nothing
here is precomputed and nothing is written.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))  # this checkout's hbuilder first
from hbuilder import paths  # noqa: E402

sys.path.insert(0, str(paths.REFRESH_DIR))

import board_export  # noqa: E402,F401  (callers read precompute.board_export)
import socket_optimise  # noqa: E402
import tree_optimise as to  # noqa: E402


def _apply_plan(doc: dict, optimised: dict) -> None:
    """Attach the planner's picks to each character as `plan`: `to_buy` (nodes in
    the optimised board the current one lacks), `to_sell` (currently-owned nodes
    the respec plan drops), and `optimised_nodes`. This is what the tree paints
    as the real "buy this" — the planner's board, not the gain>0 heuristic."""
    for character in doc.get("characters", []):
        owned = {n["id"] for n in character.get("nodes", []) if n.get("state") == "owned"}
        opt = set((optimised.get(character["chr"]) or {}).get("nodes") or [])
        character["plan"] = {
            "to_buy": sorted(opt - owned),
            "to_sell": sorted(owned - opt),
            "optimised_nodes": sorted(opt),
            "slots": (optimised.get(character["chr"]) or {}).get("slots") or {},
        }


def _annotate_plan_budget(doc: dict, *, char_level: str = "current") -> None:
    """Set each planned character's point COST and the point BUDGET it is costed
    against, so the board header reads '<board cost> / <points at this level>',
    e.g. '248/361'.

    At max the budget is the level-50 grant (361), at current the real earned points;
    `point_cost` is the developed board's own point total, not the current board's spend
    (which against a max-level plan would read as an impossible figure)."""
    prices = to._prices()
    max_points = None
    if char_level == "max":
        import tree_budget  # noqa: PLC0415

        max_points = tree_budget.budget_curve()[to.MAX_CHARACTER_LEVEL]
    for character in doc.get("characters", []):
        plan = character.get("plan")
        if not plan:
            continue
        plan["point_cost"] = sum(
            prices.get(n, {}).get("points", 0) for n in plan.get("optimised_nodes") or []
        )
        plan["point_budget"] = (
            max_points if char_level == "max" else (character.get("points") or {}).get("granted")
        )


_CHR_NAMES: dict | None = None


def _chr_names() -> dict:
    """chr id -> holomem NAME, from the skilltree's member list. Lets a socket
    placement on a BENCH character (whose board the fast plan does not render, so
    it is absent from `doc.characters`) still read "Otonose Kanade · S-001" in the
    socket LIST rather than the raw `chr-06002`. Cached; `{}` if unreadable."""
    global _CHR_NAMES
    if _CHR_NAMES is None:
        _CHR_NAMES = {}
        try:
            skilltree = to._load(paths.DUMPS / "skilltree.json")
        except (OSError, ValueError):
            skilltree = {}
        for member in skilltree.get("members", []):
            cid = member.get("chr")
            if cid:
                _CHR_NAMES[cid] = member.get("name") or cid
    return _CHR_NAMES


_CARD_NAMES: dict | None = None


def _card_names() -> dict:
    """card id -> {name, member, costume} from `data/cards.json`, so the UI can show
    a card by its NAME ("Crimson Captain") rather than the game's long id
    (`card-00031-5-uniq-0025-00`). Cached; `{}` if the catalogue is unreadable."""
    global _CARD_NAMES
    if _CARD_NAMES is None:
        _CARD_NAMES = {}
        try:
            raw = json.loads((paths.DATA / "cards.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = []
        cards = raw if isinstance(raw, list) else raw.get("cards", [])
        for card in cards:
            cid = card.get("id")
            if cid:
                _CARD_NAMES[cid] = {
                    "name": card.get("title") or card.get("costume_name") or cid,
                    "member": card.get("member") or "",
                    "costume": card.get("costume_name") or "",
                }
    return _CARD_NAMES


def connect_cards_for(
    scenario: str = "account", *, assume_bloom: object = None, keep_higher_bloom: bool = False
) -> dict[str, tuple[tuple, int]]:
    """`card id -> (shape offsets, permil at the card's resolved Connect level)`
    — the `connect_cards` input the domain's REAL socket assignment takes
    (`board_plan.board_model_from_plan`). On the `account` scenario the pool is
    the account's Connect cards at their collection-resolved level (AUTO bloom);
    on a maxed scenario it is every card owned (`my-cards-maxed`) or every card
    in the game (`full-collection-maxed`), all at Connect level 2.

    `assume_bloom` / `keep_higher_bloom`: the what-if bloom the cards are restated
    at (`cli._override_collection_levels`, the same document the stats use), so
    "bloom 5" also lifts the account's Connect cards to level 2, as bloom 5 does in
    game (restating only the stats and leaving every Connect card at level 1 costs
    -4.4..-5.3 % on 5 teams)."""
    skilltree = to._load(paths.DUMPS / "skilltree.json")
    connect = skilltree.get("connect", {})
    card_connect = skilltree.get("card_connect", {})
    extents = skilltree.get("extents", {})
    # "maxed-account" (see the bottom of this module) is the whole
    # catalogue at level 2 too — a fully maxed account owns every card.
    if scenario in ("full-collection-maxed", "maxed-account"):
        pool = set(card_connect)
    else:
        pool = socket_optimise._owned_connect_cards()
    forced = None if scenario == "account" else 2
    collection = to._load(paths.STATE / "collection.json")
    if assume_bloom is not None and collection is not None:
        from hbuilder.inputs import _override_collection_levels  # noqa: PLC0415

        collection = _override_collection_levels(
            collection, None, assume_bloom, keep_higher_bloom=keep_higher_bloom
        )
    levels = socket_optimise.build_board.ConnectLevels(
        forced, collection, to._load(paths.DUMPS / "card_caps.json")
    )
    out: dict[str, tuple[tuple, int]] = {}
    for card in sorted(pool):
        eff = connect.get(card_connect.get(card))
        if not eff:
            continue
        area = extents.get(eff.get("area"))
        if not area:
            continue
        lvl, _basis = levels.for_card(card)
        els = eff.get("levels") or []
        if not 1 <= lvl <= len(els):
            continue
        out[card] = (tuple(tuple(p) for p in area), int(els[lvl - 1]["v"]))
    return out


# ---------------------------------------------------------------------------
# "maxed-account" scenario, matching the community tool holodori.best's.
#
# A "fully maxed account": every card Lv80 / bloom max, EVERY holomem at max level with its
# board developed (the points its levels grant, `hbuilder.inputs.max_level_board_points`, fewer
# than the whole tree costs), memory posters = every poster the game has shipped, member
# power-up at the roster ceiling. The `maxed` basis (`apply_collection(maxed=True, ...)` and
# `maxed_account_bonuses`) gives the cards and the power-up ceiling; the helpers below add
# (a) a bench of developed boards and (b) the master's poster count rather than the 50-row cap
# of `PosterCollectEffect`.
# ---------------------------------------------------------------------------
MAXED_ACCOUNT_SCENARIO = "maxed-account"
_POSTER_MASTER = paths.HOLODORI / "main_Poster.json"


def master_poster_count() -> int | None:
    """How many posters the game has (`hbuilder.inputs.master_poster_count` on the
    master's `main_Poster.json` export): what an account owning every poster
    holds. `None` if the export is absent."""
    from hbuilder.inputs import master_poster_count as count  # noqa: PLC0415

    return count(_POSTER_MASTER)


def maxed_account_bonuses(basis, *, posters: int | None = None):
    """`AccountBonuses` for the maxed-account scenario. `basis` is the
    `CollectionBasis` `apply_collection(maxed=True)` returned: its `bonuses`
    already carry the roster's power-up ceiling, level 50 and every poster the
    game has; `posters` overrides the poster count."""
    import dataclasses  # noqa: PLC0415

    bonuses = basis.bonuses
    if posters is None or bonuses is None:
        return bonuses
    return dataclasses.replace(bonuses, posters=int(posters))


def bench_reach_nodes(skilltree: dict, node_master: list, chr_id: str) -> list[str]:
    """The nodes of `chr_id`'s board that reach a team while she is OFF it: its greens
    (account-wide stats), its yellows (the song bonus of a song she sings, on the team
    or not) and its Connect sockets (the cards lifting those) — each green or yellow only
    when the row that applies to her (`build_board._select_row`) carries an effect the
    scorer counts (not `ignored`: mini-game / work / live rewards, EXP, Hologold, LIFE).
    Her reds need her to lead and her blues need her card on the team, so a bench board
    gets nothing from them. An effect id the parser does not know is kept: the scorer
    then reports it instead of it silently dropping out here."""
    import build_board  # noqa: PLC0415

    from hbuilder.domain.board import BoardError, parse_effect_id  # noqa: PLC0415

    model_id = next((m.get("model") for m in skilltree["members"] if m["chr"] == chr_id), None)
    model = skilltree["models"].get(model_id) or {}
    rows_by_group = _rows_by_group(node_master)
    out = []
    for node in sorted(model):
        if node.startswith("S-"):
            out.append(node)
            continue
        branch = node[0]
        if branch not in ("G", "Y"):
            continue
        row, _why = build_board._select_row(rows_by_group.get(node) or [], chr_id)
        if row is None:
            continue
        for effect_id in row[build_board.ROW_DATA_KEY].get("6") or []:
            try:
                scores = parse_effect_id(effect_id, branch=branch).kind != "ignored"
            except BoardError:
                scores = True
            if scores:
                out.append(node)
                break
    return out


_ROWS_BY_GROUP: dict = {}


def _rows_by_group(node_master: list) -> dict:
    """`build_board._effect_rows_by_group(node_master)`, kept for the last master seen."""
    import build_board  # noqa: PLC0415

    held = _ROWS_BY_GROUP.get(id(node_master))
    if held is None or held[0] is not node_master:
        _ROWS_BY_GROUP.clear()
        held = _ROWS_BY_GROUP[id(node_master)] = (node_master, build_board._effect_rows_by_group(node_master))
    return held[1]


def maxed_account_board_state(
    skilltree: dict | None = None, points: int | None = None, *, node_master: list | None = None
) -> dict:
    """`{chr: {"nodes": [...], "slots": {}}}` for every holomem in `skilltree.json`: her
    board on a maxed account's bench, no sockets filled yet (`maxed_account_bench`
    places them).

    Off the team only her greens, yellows and sockets reach a team (`bench_reach_nodes`).
    Each of them is bought by its cheapest path from the centre (`tree_optimise.
    cheapest_paths` on the game's adjacency), the nearest first, while the spend stays
    within `points` (default: what a max-level holomem holds,
    `hbuilder.inputs.max_level_board_points`). The points left stay unspent: no other
    node of a bench board reaches a team, so spending them changes no score."""
    from hbuilder.domain.board_plan import node_costs_from_master  # noqa: PLC0415
    from hbuilder.inputs import max_level_board_points  # noqa: PLC0415

    skilltree = skilltree or to._load(paths.DUMPS / "skilltree.json")
    node_master = node_master or to._load(paths.DUMPS / "master_SkillTreeNode.json")
    points = max_level_board_points() if points is None else int(points)
    prices = {node: {"points": cost} for node, cost in node_costs_from_master(node_master).items()}
    models = skilltree["models"]
    graphs: dict[str, dict] = {}
    out = {}
    for member in skilltree["members"]:
        model_id = member.get("model")
        if model_id not in models:
            continue
        chr_id = member["chr"]
        graph = graphs.get(model_id)
        if graph is None:
            graph = graphs[model_id] = to.adjacency(models[model_id])
        owned = {to.ROOT}
        spent = 0
        wanted = [n for n in bench_reach_nodes(skilltree, node_master, chr_id) if n != to.ROOT]
        while True:
            left = [n for n in wanted if n not in owned]
            if not left:
                break
            reach = to.cheapest_paths(owned, graph, prices, max_nodes=len(models[model_id]))
            options = [(sum(prices[n]["points"] for n in reach[t]), t) for t in left if t in reach]
            if not options:
                break
            cost, target = min(options)
            if spent + cost > points:
                break
            owned.update(reach[target])
            spent += cost
        out[chr_id] = {"nodes": sorted(owned), "slots": {}}
    return out


def maxed_account_bench(
    cards,
    cfg,
    nominal_team,
    *,
    skilltree=None,
    node_master=None,
    connect_cards=None,
    points: int | None = None,
    report: list | None = None,
    state_out: dict | None = None,
) -> dict:
    """The maxed-account BENCH: `board.json`-shaped, name-keyed `characters`
    mapping (`bench_characters` for `optimize_and_rerank` / `RefineContext`)
    in which EVERY holomem is at max level, her `points` (default: the max-level
    grant) buying what of her board reaches a team from the bench
    (`maxed_account_board_state`), and every socket holds a Connect card at level 2.

    Sockets: the domain's own exact team-wide assignment
    (`board_plan._team_connect_assignment`, one card -> one socket over the
    whole game's Connect pool at level 2 = `connect_cards_for("maxed-account")`)
    run over ALL 54 boards at once, valuing each board's GREEN nodes only
    (`bench_node_values` with no song cast: the bench is song-agnostic) at
    the linearisation point of `nominal_team` (leader id, member ids) scored
    board-off. Deterministic: the placement depends on the nominal
    roster only through the greens' Unit-Score deltas, and a team that later
    develops one of these characters REPLACES its bench entry (its own sockets
    are then re-assigned from the full pool exactly as the account scenario
    does, i.e. a card may be socketed on the bench and on the team — the same
    convention `board.json` + `connect_cards_for("account")` already have).
    The multipliers are then resolved by `build_board.build(level=2)`, the same
    converter that writes `board.json`, so the mapping is byte-compatible."""
    import dataclasses  # noqa: PLC0415

    import build_board  # noqa: PLC0415

    from hbuilder.domain.board_plan import (  # noqa: PLC0415
        _team_connect_assignment,
        bench_node_values,
        model_for_character,
        node_effect_ids_from_master,
    )
    from hbuilder.domain.scoring import UNIT_SCORE_K, deck_screen_unit_score, score_with_breakdown  # noqa: PLC0415
    from hbuilder.domain.team import Team  # noqa: PLC0415

    skilltree = skilltree or to._load(paths.DUMPS / "skilltree.json")
    node_master = node_master or to._load(paths.DUMPS / "master_SkillTreeNode.json")
    connect_cards = connect_cards or connect_cards_for(MAXED_ACCOUNT_SCENARIO)
    effects = node_effect_ids_from_master(node_master)
    by_id = {c.id: c for c in cards}
    leader_id, member_ids = nominal_team
    team = Team(leader=by_id[leader_id], members=tuple(by_id[m] for m in member_ids))
    bd = score_with_breakdown(team, dataclasses.replace(cfg, board=None))
    ds = deck_screen_unit_score(bd)
    state = maxed_account_board_state(skilltree, points, node_master=node_master)
    per_char = {}
    for chr_id, entry in state.items():
        model_id = model_for_character(skilltree, chr_id) or ""
        if not model_id:
            continue
        values = bench_node_values(
            effects,
            member_cards=team.members,
            bonus_permil=ds.score_bonus_permil,
            overall_power=ds.overall_power,
            k_constant=UNIT_SCORE_K,
            skilltree=skilltree,
            model_id="",
            own_chr=chr_id,
        )
        per_char[chr_id] = (skilltree["models"][model_id], frozenset(entry["nodes"]), values)
    _mults, placements = _team_connect_assignment(per_char, connect_cards)
    for p in placements:
        state[p.chr]["slots"][p.socket] = p.card
    if state_out is not None:
        # `{chr: {"nodes": [her bench board], "slots": {socket: card}}}`: the same bench as
        # a board state, for showing what an account needs to reach it (the web's board
        # panel on a maxed account)
        state_out.update(
            {c: {"nodes": list(v["nodes"]), "slots": dict(v["slots"])} for c, v in state.items()}
        )
    doc, rep, problems = build_board.build(state, skilltree, node_master, level=2)
    if report is not None:
        report.extend(rep)
        report.extend(problems)
    return doc["characters"]


_DEVELOPED_BENCH: dict = {}


def developed_bench_characters(seed_ids: list[str], *, max_level: bool, resources: str) -> dict:
    """The bench boards (every character NOT in `seed_ids`) with their idle points
    spent on account-wide green, as a `board.json`-shaped `characters` mapping.

    Green stacks onto every team from any board, so the idle points of the 48
    characters off the team are score left on the table: +12..26 % with
    unlimited cubes/hexas and -2..+11 % within the account's own wallet. One
    joint MILP
    (`board_plan_solver.solve_team_board`, `develop_bench=True`) around a nominal
    team, song-agnostic; the bench part of its answer replaces those characters'
    captured boards, the rest stay as captured. `resources`: "inventory" (the
    account's balance) or "infinite"; `max_level` plans every board with the
    level-50 grant instead of the earned points."""
    import json  # noqa: PLC0415

    import board_plan_solver  # noqa: PLC0415
    import build_board  # noqa: PLC0415

    from hbuilder.domain.collection import caps_by_rarity_from_mapping  # noqa: PLC0415

    dumps = paths.DUMPS
    read = lambda path: json.loads(path.read_text(encoding="utf-8"))  # noqa: E731
    solved = (
        board_plan_solver.solve_team_board(
            scenario="account",
            song=None,
            pool=1,
            for_team=list(seed_ids),
            char_level="max" if max_level else "current",
            resources=resources,
            develop_bench=True,
            include_sockets=False,
        )
        or {}
    )
    team_chr = {"chr-" + card_id.split("-")[1] for card_id in seed_ids}
    state = {k: dict(v) for k, v in read(dumps / "board_state.json").items()}
    for chr_id, plan in solved.items():
        if chr_id not in team_chr:
            state[chr_id] = {**state.get(chr_id, {}), "nodes": plan["nodes"]}
    document, _report, _problems = build_board.build(
        state,
        read(dumps / "skilltree.json"),
        read(dumps / "master_SkillTreeNode.json"),
        level=None,
        collection=read(paths.STATE / "collection.json"),
        caps_by_rarity=caps_by_rarity_from_mapping(read(dumps / "card_caps.json")),
    )
    return document.get("characters") or {}
