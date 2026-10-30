"""Board (+ socket) optimisation as an ON-DEMAND function library.

The web builds the plan for the EXACT team a Solve returned, when the user asks —
`team_board_plan` runs `tree_optimise --plan --for-team` (subprocess), attaches the
per-character buy/sell diff (`_apply_plan`), the level-aware point budget
(`_annotate_plan_budget`), and the socket picks (`annotate_sockets_for`), and emits
a `board-export/v0` doc the frontend's board renderer
consumes unchanged. `tools/web/server.py` imports this module BY PATH and calls
these functions from `/api/boardplan`.

Runs in proxyenv, FROM THE REPO ROOT (so hbuilder's collection resolves).

There is no offline driver: nothing is precomputed, every function below runs on
demand despite the module's name.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import board_export  # noqa: E402
import socket_optimise  # noqa: E402
import tree_optimise as to  # noqa: E402


def optimised_board(scenario: str, song: str | None, pool: int,
                    for_team: list[str] | None = None,
                    char_level: str = "current",
                    resources: str = "inventory") -> dict | None:
    """Run `tree_optimise --plan` for this scenario/song and read the OPTIMISED
    board it dumps: `{chr: {nodes, slots}}`. The diff against the current board is
    "to buy" / "to sell".

    The plan is always a `--respec` bare rebuild (every node re-decidable). TWO
    ORTHOGONAL toggles set the resource budget it may spend — these are the two
    distinct resource axes of the board:

      char_level: 'current' (`--real-char-level`, each holomem's REAL character
                  level 1-50 and the node POINTS it grants) or 'max'
                  (`--at-max-level`, every holomem at level 50 = 361 points).
      resources : 'inventory' (`--respec`, the cube/hexa wallet you ACTUALLY have)
                  or 'infinite' (`--infinite`, unlimited materials — the ceiling).

    tree_optimise's beam is a multi-dimensional knapsack over BOTH the character
    points AND the 8 cube/hexa currencies, so its plan is affordable in every
    resource — which the fast Engine-A greedy (points only, ignores materials)
    cannot guarantee, and is why the affordable plan lives here.

    `for_team` (leader first, then five members) plans for THAT exact team via
    `--for-team`. With a fixed team the pool only primes the oracle, so a small
    `pool` keeps it responsive. `None` on any failure."""
    char_level = char_level if char_level in ("current", "max") else "current"
    resources = resources if resources in ("inventory", "infinite") else "inventory"
    # A full-collection-maxed scenario IS a level-50 account, so 'current' has no
    # meaning there — tree_optimise's resolve_at_max_level SystemExits on
    # `--real-char-level --scenario full-collection-maxed`. Force max so the
    # default 'current' toggle never crashes the maxed-basis board plan.
    if scenario == "full-collection-maxed":
        char_level = "max"
    team_tag = ("_" + for_team[0] if for_team else "") + f"_{char_level}_{resources}"
    tmp = Path(tempfile.gettempdir()) / f"hbplan_{scenario}_{song or 'general'}{team_tag}.json"
    cmd = [
        sys.executable, str(HERE / "tree_optimise.py"),
        "--plan", "--respec", "--scenario", scenario,
        "--pool", str(pool), "--dump-state", str(tmp),
        # Character level -> node points (the toggle overrides the scenario default).
        "--at-max-level" if char_level == "max" else "--real-char-level",
    ]
    # Materials axis ONLY: --infinite-materials lifts the cube/hexa wallet but leaves
    # the POINT budget bounded by character level (real, or 361 under --at-max-level).
    # NOT --infinite, which would also lift points and let a max-level board exceed
    # the 361 a level-50 holomem actually has. Without it, --respec spends the real
    # inventory read from disk.
    if resources == "infinite":
        cmd.append("--infinite-materials")
    if song:
        cmd += ["--song", song]
    if for_team:
        cmd += ["--for-team", ",".join(for_team)]
    try:
        subprocess.run(cmd, cwd=str(to.REPO), capture_output=True, text=True, timeout=1800)
        if not tmp.exists():
            return None
        return json.loads(tmp.read_text(encoding="utf-8"))
    except (OSError, ValueError, subprocess.SubprocessError):
        return None

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

    Without this the header would show the CURRENT board's spend against the REAL
    level budget (`spent_on_owned / granted`), which CONTRADICTS a max-level plan: a
    level-50 holomem has 361 board points, not the ~80 its real level grants, so a
    plan correctly costed at 361 would read as an impossible '84 to buy / 36 / 83 pt'. At
    max the budget is the level-50 grant (361); at current it is the real earned
    points. `point_cost` is the developed board's own point total, the honest
    numerator (not the current board's spend)."""
    prices = to._prices()
    max_points = None
    if char_level == "max":
        import tree_budget  # noqa: PLC0415
        max_points = tree_budget.budget_curve()[to.MAX_CHARACTER_LEVEL]
    for character in doc.get("characters", []):
        plan = character.get("plan")
        if not plan:
            continue
        plan["point_cost"] = sum(prices.get(n, {}).get("points", 0)
                                 for n in plan.get("optimised_nodes") or [])
        plan["point_budget"] = (max_points if char_level == "max"
                                else (character.get("points") or {}).get("granted"))


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
            skilltree = to._load(to.DUMPS / "skilltree.json")
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
            raw = json.loads((to.REPO / "data" / "cards.json").read_text(encoding="utf-8"))
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


def _annotate_sockets(doc: dict) -> None:
    """Fold the socket optimiser's picks INTO each character's sockets, so the tree
    renderer can show the recommended card + exact multiplier ON the socket and
    highlight the nodes that card's shape buffs. Read straight off `placements`
    (each already carries `detail` = multiplier / effect / buffed_nodes). Each pick
    and socket is also tagged with the card's readable NAME/member, so no view has
    to show the raw card id."""
    names = _card_names()
    chr_names = _chr_names()
    picks = {(p["chr"], p["socket"]): p
             for p in (doc.get("sockets_optimised", {}).get("placements") or [])}
    # name the placements themselves too (the socket LIST reads these directly),
    # including the HOST board's holomem — a bench pick's board is not rendered, so
    # the list is the only place its name appears.
    for pick in (doc.get("sockets_optimised", {}).get("placements") or []):
        info = names.get(pick["card"]) or {}
        pick["card_name"] = info.get("name") or pick["card"]
        pick["card_member"] = info.get("member") or ""
        pick["host_name"] = chr_names.get(pick["chr"]) or pick["chr"]
    for character in doc.get("characters", []):
        buffed_all = set()
        for socket in character.get("sockets", []):
            pick = picks.get((character["chr"], socket["socket"]))
            if not pick:
                continue
            detail = pick.get("detail") or {}
            info = names.get(pick["card"]) or {}
            socket["recommended_card"] = pick["card"]
            socket["recommended_card_name"] = info.get("name") or pick["card"]
            socket["recommended_card_member"] = info.get("member") or ""
            socket["recommended_value"] = pick["isolated_value"]
            socket["recommended_multiplier"] = detail.get("multiplier")
            socket["recommended_effect"] = detail.get("effect")
            socket["recommended_buffs"] = detail.get("buffed_nodes") or []
            buffed_all.update(detail.get("buffed_nodes") or [])
        if buffed_all:
            # the union of every recommended socket's covered nodes, so the tree can
            # mark "this node is amplified by a Connect card" at a glance.
            character["connect_buffed_nodes"] = sorted(buffed_all)


def connect_cards_for(scenario: str = "account", *, assume_bloom: object = None,
                      keep_higher_bloom: bool = False) -> dict[str, tuple[tuple, int]]:
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
    skilltree = to._load(to.DUMPS / "skilltree.json")
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
    collection = to._load(to.REPO / "state" / "collection.json")
    if assume_bloom is not None and collection is not None:
        from hbuilder.cli import _override_collection_levels  # noqa: PLC0415
        collection = _override_collection_levels(collection, None, assume_bloom,
                                                 keep_higher_bloom=keep_higher_bloom)
    levels = socket_optimise.build_board.ConnectLevels(
        forced, collection, to._load(to.DUMPS / "card_caps.json"))
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


def annotate_sockets_for(doc: dict, optimised: dict, *, scenario: str,
                         song: str | None, for_team: list[str],
                         pool: int = 1, include_current_benches: bool = False) -> dict:
    """Run the team-aware socket optimiser against the OPTIMISED board and fold its
    picks into `doc` (`sockets_optimised` + per-socket annotations).

    The socket half of `team_board_plan`, extracted so the web's FAST Engine-A plan
    (which optimises the nodes itself) can reuse it. The board_state temp file is
    written HERE, in the recon layer — never in the web server, whose no-writes
    allowlist forbids it. Non-fatal: sockets are a bonus, never worth failing a plan.

    `include_current_benches`: widen the socket search to EVERY board the account
    has. A Connect card on a BENCH holomem's free S-001 root amplifies that board's
    GREEN nodes, which are account-global and so raise ANY team's score — ~50 benches
    each add a little. The FULL plan's `optimised` dump already carries every
    DEVELOPED board, so it needs nothing here; the FAST Engine-A plan only develops
    the six team boards, so this overlays them onto the CURRENT boards, letting the
    optimiser socket the green you already own on the bench (+8.9k isolated value on
    a real account, ~5s). A bench board is not re-developed — only its
    free S-001 is searched — so this stays a cheap add-on to the fast plan."""
    socket_board = optimised
    if include_current_benches:
        current = to._load(to.DUMPS / "board_state.json")
        socket_board = {
            cid: {"nodes": list((entry or {}).get("nodes") or []),
                  "slots": dict((entry or {}).get("slots") or {})}
            for cid, entry in current.items()
        }
        # The team's DEVELOPED boards (from `optimised`) override their current ones,
        # so team sockets are valued on the plan while benches keep what you own.
        for cid, entry in optimised.items():
            slot = socket_board.setdefault(cid, {"nodes": [], "slots": {}})
            slot["nodes"] = list((entry or {}).get("nodes") or [])
    tmp = (Path(tempfile.gettempdir())
           / f"hbsock_{scenario}_{song or 'general'}_{for_team[0]}.json")
    tmp.write_text(json.dumps(socket_board, ensure_ascii=False), encoding="utf-8")
    try:
        doc["sockets_optimised"] = socket_optimise.optimise(
            pool=pool, board_state_path=tmp, scenario=scenario, song=song,
            leader_id=for_team[0], member_ids=tuple(for_team[1:]))
        _annotate_sockets(doc)
    except Exception as exc:  # noqa: BLE001 — sockets are a bonus, never fatal
        print(f"  ⚠ socket optimise failed: {exc}", file=sys.stderr)
    return doc


def team_board_plan(*, scenario: str, song: str | None, for_team: list[str],
                    pool: int = 6, gain: bool = False, sockets: bool = True,
                    char_level: str = "current",
                    resources: str = "inventory", engine: str = "beam",
                    max_resets: int | None = None) -> dict:
    """Build a board-export/v0 doc whose plan is for THIS EXACT team — the
    on-demand path the web uses after a Solve, so the tree shown is the plan for
    the team the ranking won (not the precompute's own best team). `gain` defaults
    off: the tree paints the plan's real picks, so the per-node pool re-score
    (slow) is not needed. `pool` only primes the oracle (the team is fixed via
    `--for-team`), so it stays small and fast. Raises nothing the caller must
    special-case: a failed plan run leaves `plan_included=False` and the doc still
    carries the current board.

    `sockets` also runs the team-aware socket optimiser AGAINST the optimised board
    (not the current one) and for THIS team, so the socket picks amplify the same
    red/blue the plan just bought — attached as `sockets_optimised`."""
    # A full-collection-maxed account is at level 50, so 'current' is meaningless
    # there and tree_optimise refuses it — mirror optimised_board's coercion so the
    # doc reports the level the plan actually ran at.
    if scenario == "full-collection-maxed":
        char_level = "max"
    doc = board_export.build(gain=gain, pool=pool, scenario=scenario, song=song)
    doc["for_team"] = for_team
    doc["plan_included"] = True
    doc["plan_char_level"] = char_level
    doc["plan_resources"] = resources
    doc["engine"] = engine
    # engine="solver": the HiGHS in-process optimiser (provably optimal, sub-second)
    # instead of the ~60-90s `tree_optimise` beam subprocess. The doc renders the
    # team's own boards, so it's filtered to them (the solver develops those; the
    # beam additionally fills bench green, a separate account-global additive pass).
    if engine == "solver":
        import board_plan_solver  # noqa: PLC0415 — server-side solver
        optimised = board_plan_solver.solve_team_board(
            scenario=scenario, song=song, pool=pool, for_team=for_team,
            char_level=char_level, resources=resources, max_resets=max_resets)
        if optimised:
            team_chr = set(optimised)
            doc["characters"] = [c for c in doc.get("characters", []) if c["chr"] in team_chr]
            # the raw {chr:{nodes,slots}} board, so the page can regenerate the song
            # timeline AGAINST this optimised board (its cooldown nodes tighten the
            # active windows) and read its board-aware song score.
            doc["optimised_board_state"] = optimised
    else:
        optimised = optimised_board(scenario, song, pool, for_team=for_team,
                                    char_level=char_level, resources=resources)
    if optimised:
        # What the plan's resets cost in game: 1,000 Hologold per node taken off a
        # board (read from the client binary, `board_reset`). A lower bound: a partial reset also drops
        # the nodes beyond the chosen one.
        import board_reset  # noqa: PLC0415

        doc["resets"] = board_reset.reset_cost(
            to._owned_nodes(), {c: v.get("nodes") or [] for c, v in optimised.items()},
            gold_per_node=board_reset.gold_per_node())
        doc["max_resets"] = max_resets
        _apply_plan(doc, optimised)
        # Cost each board against the level's budget (361 at max), so the header is
        # honest under the char_level toggle rather than showing the real budget.
        _annotate_plan_budget(doc, char_level=char_level)
        if sockets:
            # Value the socket placements on the OPTIMISED board (what the plan
            # will build), not the current one, and for THIS team.
            annotate_sockets_for(doc, optimised, scenario=scenario, song=song,
                                 for_team=for_team, pool=pool)
    else:
        doc["plan_included"] = False
    return doc


# ---------------------------------------------------------------------------
# "maxed-account" scenario, matching the community tool holodori.best's.
#
# holodori.best's "fully maxed account" is: every card Lv80 / bloom max, EVERY
# holomem's board fully developed (all 453 points), memory posters = every poster
# the game has shipped, member power-up at the roster ceiling. The `maxed` basis
# (`apply_collection(maxed=True, assume_level="max", assume_bloom="max")`) already
# gives the cards and — via `maxed_account_bonuses` — the power-up ceiling; what it
# does NOT give is (a) a bench of fully developed boards (it reuses the account's
# `board.json`) and (b) the poster count of the MASTER
# rather than the 50-row cap of `PosterCollectEffect`. The two helpers below supply
# exactly those two things; nothing else changes, so the scenario is the maxed
# basis + this bench + these bonuses. Kept recon-side (a function library, like
# the rest of this module).
# ---------------------------------------------------------------------------
MAXED_ACCOUNT_SCENARIO = "maxed-account"
# tree_optimise / socket_optimise know three scenario names; for the subprocess
# paths a maxed account IS a full-collection-maxed one (every card, level 50).
SCENARIO_ALIASES = {MAXED_ACCOUNT_SCENARIO: "full-collection-maxed"}
_POSTER_MASTER = to.DUMPS / "holodori" / "main_Poster.json"


def master_poster_count() -> int | None:
    """Rows of the master `Poster` table (the `main_Poster.json` export) — the
    number of posters an account that owns every
    poster holds, i.e. its Memory Bonus input (`memory_bonus_permil(37)`).
    `PosterCollectEffect` runs to 50 thresholds, but only the posters that EXIST
    can be collected. `None` if the export is absent (say so; do not guess)."""
    try:
        rows = to._load(_POSTER_MASTER)
    except (OSError, ValueError):
        return None
    if isinstance(rows, dict):
        rows = rows.get("rows") or rows.get("data") or []
    return len(rows) if isinstance(rows, list) else None


def maxed_account_bonuses(basis, *, posters: int | None = None):
    """`AccountBonuses` for the maxed-account scenario: the roster's power-up
    ceiling (`max_member_power_up_permyriad`, 1158 permyriad on the 192-card
    catalogue = 11.58 %) and memory posters = the master's poster
    count, NOT `MEMORY_BONUS_MAX_POSTERS`.
    `basis` is the `CollectionBasis` `apply_collection(maxed=True)` returned;
    its `bonuses` already carry the power-up ceiling and level 50."""
    import dataclasses  # noqa: PLC0415
    n = master_poster_count() if posters is None else posters
    bonuses = basis.bonuses
    if n is None or bonuses is None:
        return bonuses
    return dataclasses.replace(bonuses, posters=int(n))


def maxed_account_board_state(skilltree: dict | None = None) -> dict:
    """`{chr: {"nodes": [every node of its model, sockets included], "slots": {}}}`
    for every holomem in `skilltree.json` — the FULL 453-point tree, no sockets
    filled yet (`maxed_account_bench` places them)."""
    skilltree = skilltree or to._load(to.DUMPS / "skilltree.json")
    models = skilltree["models"]
    return {
        m["chr"]: {"nodes": sorted(models[m["model"]]), "slots": {}}
        for m in skilltree["members"] if m.get("model") in models
    }


def maxed_account_bench(cards, cfg, nominal_team, *, skilltree=None,
                        node_master=None, connect_cards=None,
                        report: list | None = None, state_out: dict | None = None) -> dict:
    """The maxed-account BENCH: `board.json`-shaped, name-keyed `characters`
    mapping (`bench_characters` for `optimize_and_rerank` / `RefineContext`)
    in which EVERY holomem's board is the full 453 tree and every socket holds
    a Connect card at level 2.

    Sockets: the domain's own exact team-wide assignment
    (`board_plan._team_connect_assignment`, one card -> one socket over the
    whole game's Connect pool at level 2 = `connect_cards_for("maxed-account")`)
    run over ALL 54 boards at once, valuing each board's GREEN nodes only
    (`bench_node_values`: reds/blues/yellows of a bench board reach no team) at
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
        _team_connect_assignment, bench_node_values, model_for_character,
        node_effect_ids_from_master)
    from hbuilder.domain.scoring import (  # noqa: PLC0415
        UNIT_SCORE_K, deck_screen_unit_score, score_with_breakdown)
    from hbuilder.domain.team import Team  # noqa: PLC0415

    skilltree = skilltree or to._load(to.DUMPS / "skilltree.json")
    node_master = node_master or to._load(to.DUMPS / "master_SkillTreeNode.json")
    connect_cards = connect_cards or connect_cards_for(MAXED_ACCOUNT_SCENARIO)
    effects = node_effect_ids_from_master(node_master)
    by_id = {c.id: c for c in cards}
    leader_id, member_ids = nominal_team
    team = Team(leader=by_id[leader_id], members=tuple(by_id[m] for m in member_ids))
    bd = score_with_breakdown(team, dataclasses.replace(cfg, board=None))
    ds = deck_screen_unit_score(bd)
    state = maxed_account_board_state(skilltree)
    per_char = {}
    for chr_id, entry in state.items():
        model_id = model_for_character(skilltree, chr_id) or ""
        if not model_id:
            continue
        values = bench_node_values(
            effects, member_cards=team.members, bonus_permil=ds.score_bonus_permil,
            overall_power=ds.overall_power, k_constant=UNIT_SCORE_K,
            skilltree=skilltree, model_id="", own_chr=chr_id)
        per_char[chr_id] = (skilltree["models"][model_id], frozenset(entry["nodes"]), values)
    _mults, placements = _team_connect_assignment(per_char, connect_cards)
    for p in placements:
        state[p.chr]["slots"][p.socket] = p.card
    if state_out is not None:
        # `{chr: {"nodes": [the whole tree], "slots": {socket: card}}}`: the same bench as
        # a board state, for showing what an account needs to reach it (the web's board
        # panel on a maxed account)
        state_out.update({c: {"nodes": list(v["nodes"]), "slots": dict(v["slots"])} for c, v in state.items()})
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

    dumps = to.DUMPS
    read = lambda path: json.loads(path.read_text(encoding="utf-8"))  # noqa: E731
    solved = board_plan_solver.solve_team_board(
        scenario="account", song=None, pool=1, for_team=list(seed_ids),
        char_level="max" if max_level else "current", resources=resources,
        develop_bench=True, include_sockets=False) or {}
    team_chr = {"chr-" + card_id.split("-")[1] for card_id in seed_ids}
    state = {k: dict(v) for k, v in read(dumps / "board_state.json").items()}
    for chr_id, plan in solved.items():
        if chr_id not in team_chr:
            state[chr_id] = {**state.get(chr_id, {}), "nodes": plan["nodes"]}
    document, _report, _problems = build_board.build(
        state, read(dumps / "skilltree.json"), read(dumps / "master_SkillTreeNode.json"),
        level=None, collection=read(to.REPO / "state" / "collection.json"),
        caps_by_rarity=caps_by_rarity_from_mapping(read(dumps / "card_caps.json")))
    return document.get("characters") or {}
