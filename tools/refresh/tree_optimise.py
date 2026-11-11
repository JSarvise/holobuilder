"""The Holomem Board cost model and the value oracle the exact board planner
(`board_plan_solver.solve_team_board`) prices nodes with.

* the state and prices: each node's cost in points and the nine currencies
  (`node_price.py`), each character's point budget (`tree_budget.py`), the
  account's own nodes, and its cube/hexa balance (`balance_from_disk`,
  `dumps/inventory.json`);
* the board graph (`adjacency`, `cheapest_paths`, `frontier`) and the branch and socket
  rules the planner respects;
* `Oracle`: a fixed team scored through the real scorer on a candidate board
  (`score_team`, `board_for`), and `_branch_values`, the per-node values the MILP is
  built from;
* `fill_boards`: spends the points and currencies a plan leaves, in context.

Node values are not independent (a lone cooldown node can be worth 0 and two of them
+25 %), which is why the planner solves the boards jointly and re-scores in context.
Reads only gitignored account data already on disk. Writes nothing.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

# Windows consoles and pipes default to cp1252, which cannot encode the ⚠ → × this tool
# prints: a UnicodeEncodeError would abort the run before `--dump-state` writes the board.
# Force UTF-8 on this process's streams (it also covers the modules that import this one).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # not a reconfigurable stream
        pass

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))  # this checkout's hbuilder first
from hbuilder import paths  # noqa: E402
from hbuilder.errors import MissingInput  # noqa: E402

DUMPS = paths.DUMPS
REPO = paths.REPO
# The three collection states the board export uses, in order.
SCENARIOS = ("account", "my-cards-maxed", "full-collection-maxed")
sys.path.insert(0, str(paths.REFRESH_DIR))

import dataclasses  # noqa: E402

import build_board  # noqa: E402
import node_price  # noqa: E402

from hbuilder.domain.board import BoardError, BoardModel, board_from_mapping  # noqa: E402
from hbuilder.domain.loader import load_cards, load_owned  # noqa: E402
from hbuilder.domain.scoring_config import load_scoring_config  # noqa: E402
from hbuilder.domain.skilltree import neighbours  # noqa: E402
from hbuilder.domain.team import MEMBER_COUNT  # noqa: E402,F401  (the planner reads to.MEMBER_COUNT)
from hbuilder.inputs import apply_collection, default_paths  # noqa: E402

# The eight account-wide skill-tree currencies, in the node-price table's order.
CURRENCIES = (
    "item-skill_tree_leader-1",
    "item-skill_tree_leader-2",
    "item-skill_tree_card-1",
    "item-skill_tree_card-2",
    "item-skill_tree_allmember-1",
    "item-skill_tree_allmember-2",
    "item-skill_tree_content-1",
    "item-skill_tree_content-2",
)
PRETTY = {
    "item-skill_tree_leader-1": "Red Cube",
    "item-skill_tree_leader-2": "Red Hexa",
    "item-skill_tree_card-1": "Blue Cube",
    "item-skill_tree_card-2": "Blue Hexa",
    "item-skill_tree_allmember-1": "Green Cube",
    "item-skill_tree_allmember-2": "Green Hexa",
    "item-skill_tree_content-1": "Yellow Cube",
    "item-skill_tree_content-2": "Yellow Hexa",
}


def _load(path: Path):
    if not path.exists():
        raise MissingInput(
            f"{path} is missing: the refresh builds it from your account. Run refresh.cmd "
            "(./refresh.sh on Linux) in the holobuilder folder, or restart the app."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _fresh(value):
    """A deep copy of parsed JSON (dicts, lists, scalars): what a second `_load` returns."""
    if isinstance(value, dict):
        return {k: _fresh(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_fresh(v) for v in value]
    return value


# --------------------------------------------------------------------------
# state: what is owned, what it cost, what is buyable
# --------------------------------------------------------------------------


def _master_readable() -> bool:
    """True when this PC has the SQLCipher library the master reader needs."""
    import master_cache  # noqa: PLC0415

    return master_cache.can_decrypt()


def _prices_from_export(path: Path | None = None) -> dict[str, dict]:
    """`_prices()` read from the master export `holodori/main_SkillTreeNode.json`:
    `consumptionSkillTreePointQuantity` is field 9 (the points), and `consumptions`
    is the repeated field 1000 (every entry kept, quantities summed per item as in
    `node_price.price`)."""
    path = path or paths.HOLODORI / "main_SkillTreeNode.json"
    try:
        rows = json.loads(path.read_text("utf8"))
    except FileNotFoundError:
        raise MissingInput(
            f"missing {path}: the board prices need the game's sqlcipher.dll or this synced export"
        ) from None
    out: dict[str, dict] = {}
    for row in rows:
        data = row.get("data") or {}
        group = data.get("groupId") or row.get("group_id")
        if not group or group in out:
            continue
        price = {"points": int(data.get("consumptionSkillTreePointQuantity") or 0)}
        for c in data.get("consumptions") or []:
            item = c.get("resourceId")
            if item:
                price[item] = price.get(item, 0) + int(c.get("quantity") or 0)
        out[group] = price
    return out


def _prices():
    """group id -> {"points": int, currency-id: qty}.

    ⛔ Read through `node_price.load`, which reads the SQLCipher master directly and keeps
    field 1000 as BYTES: `dumps/master_SkillTreeNode.json`'s field-1000 blob went through a
    UTF-8 decode, so every price above 127 would read wrong. `node_price.price` reads EVERY
    entry of the repeated field 1000 (the second one carries the Hexa currencies).

    Without the game's `sqlcipher.dll` (the desktop app away from a game install) the
    prices come from the synced `holodori/main_SkillTreeNode.json` (`_prices_from_export`),
    the same table exported with typed fields."""
    if not _master_readable():
        return _prices_from_export()
    rows = node_price.load("SkillTreeNode")
    out: dict[str, dict] = {}
    for row in rows:
        group = node_price.f(row, 1)
        if not group or group in out:
            # Rows within a group differ by `characterIds` but share the price; which
            # row's EFFECT applies is `build_board._select_row`'s business.
            continue
        vector = node_price.price(row)
        price = {"points": vector.get(node_price.POINT_ITEM, 0)}
        for item, qty in vector.items():
            if item != node_price.POINT_ITEM:
                price[item] = qty
        out[group] = price
    return out


def _character_points():
    """chr id -> UNSPENT Holomem Board Pt, from the account's own snapshot.

    The rows are `{"point_id": "skill_tree_point-chr-00004", "quantity": 32}`.
    These are the unspent balance, not the grant: `spent(field 9) + unspent ==
    budget(character level)` holds on every character (`tree_budget.py`), which
    is also what makes the character level recoverable."""
    snapshot = _load(DUMPS / "account_snapshot_main.json")
    out = {}
    for entry in snapshot.get("board_points") or []:
        if not isinstance(entry, dict):
            continue
        point_id = str(entry.get("point_id") or "")
        prefix = "skill_tree_point-"
        if not point_id.startswith(prefix):
            continue
        # proto3 elides a zero field: no `quantity` means 0 UNSPENT, not unknown.
        # Skipping it would leave `granted=None` -> the develop would budget the
        # character 0 points and the plan would propose selling her whole board.
        quantity = entry.get("quantity")
        out[point_id[len(prefix) :]] = int(quantity) if quantity is not None else 0
    return out


def _owned_nodes():
    state = _load(DUMPS / "board_state.json")
    return {cid: list(entry.get("nodes") or []) for cid, entry in state.items()}


def _spent(owned, prices):
    total = {"points": 0}
    per_character_points = {}
    for cid, groups in owned.items():
        for group in groups:
            price = prices.get(group)
            if price is None:
                continue
            per_character_points[cid] = per_character_points.get(cid, 0) + price["points"]
            for key, qty in price.items():
                total[key] = total.get(key, 0) + qty
    return total, per_character_points


def balance_from_disk(directory: Path = DUMPS) -> tuple[dict, str] | tuple[None, None]:
    """The account's own cube/hexa balance, from `dumps/inventory.json`
    (`inventory_snapshot.py`, a live `User/Get`, a refresh step).

    Returns the figures and the source they came from (with its capture time), or
    `(None, None)` when the file is absent, unreadable or lacks one of the eight
    currencies. Never a default: a balance nobody measured must not be
    indistinguishable from a balance that is zero."""
    stock = directory / "inventory.json"
    try:
        document = json.loads(stock.read_text("utf8"))
    except (OSError, ValueError):
        return None, None
    # `items` carries the currency ids verbatim; `skill_tree` is the same eight
    # under their English names, which is the display side.
    try:
        held = {k: int(v) for k, v in (document.get("items") or {}).items() if k in CURRENCIES}
    except (AttributeError, TypeError, ValueError):
        return None, None
    if len(held) != len(CURRENCIES):
        return None, None
    return held, f"{stock.name} ({document.get('_captured_at', 'undated')})"


# --------------------------------------------------------------------------
# the value oracle
# --------------------------------------------------------------------------


# `modelled_song_power()`: inside it, this thread's Oracle scores a song on the modelled stat
# pool instead of the deck screen's Overall Power. The planner solves one candidate plan that
# way (`board_plan_solver`, the "modelled" valuation), so its choice never scores below that
# plan. Thread-local: the server plans in threads.
_VALUATION = threading.local()


class modelled_song_power:
    def __enter__(self):
        self._before = getattr(_VALUATION, "modelled", False)
        _VALUATION.modelled = True
        return self

    def __exit__(self, *exc):
        _VALUATION.modelled = self._before
        return False


class Oracle:
    """Scores a candidate board through the REAL scorer.

    Everything except the board is loaded once, exactly as the CLI loads it (same
    `load_cards`, `apply_collection`, `load_scoring_config`), so there is no second
    definition of what a card is worth.
    """

    def __init__(
        self,
        *,
        pool_size: int,
        quiet: bool = False,
        maxed: bool = False,
        scenario: str | None = None,
        song: str | None = None,
        charts_path: str | None = None,
        uptime_model: str | None = None,
    ):
        self.quiet = quiet
        # `scenario` is one of the three collection states of the board export, on two
        # axes, the scoring BASIS and the POOL:
        #
        #   account               real levels   owned  (the default)
        #   my-cards-maxed         maxed         owned
        #   full-collection-maxed  maxed         all cards
        #
        # `data/cards.json` IS the fully-maxed state, so a maxed basis is the collection
        # not applied (see `apply_collection`).
        #
        # `scenario=None`: the explicit `maxed` flag drives the basis and the pool is
        # owned. A named scenario sets both axes.
        if scenario is not None:
            if scenario not in SCENARIOS:
                raise ValueError(f"--scenario: unknown {scenario!r}; one of {', '.join(SCENARIOS)}")
            maxed = scenario != "account"
        self._full_pool = scenario == "full-collection-maxed"
        self.scenario = scenario or "account"
        # `maxed=True` scores every card at max level and bloom: the basis a team being
        # planned toward needs (`--team`).
        self.maxed = maxed
        # The CLI's own defaults, read off its parser, so they cannot disagree with it.
        defaults = default_paths()
        cards = load_cards(REPO / defaults.cards)
        owned = load_owned(REPO / defaults.owned, cards)
        cfg = load_scoring_config(paths.CONFIG)
        # ⛔ `config.json` says `active_uptime: steady` — the DECK-SCREEN model. The
        # search and the page score runs with `finite` (what a live pays out) and
        # pass it explicitly; an Oracle left on the file's value plans boards for a
        # different objective (its plans can score up to ~13 % below on the in-run
        # score). `None` keeps the file.
        if uptime_model is not None:
            cfg = dataclasses.replace(cfg, active_uptime_model=uptime_model)
        cards, basis = apply_collection(
            cards,
            None if maxed else str(REPO / defaults.collection),
            owned_ids=[c.id for c in cards] if self._full_pool else [c.id for c in owned],
            maxed=maxed,
        )
        # ⛔ THE SILENT BASIS SWITCH. Without `card_level_curve.json` in `dumps/`, every card
        # keeps `data/cards.json`'s fully-maxed value and the run quietly answers "every
        # owned card maxed" instead of "the account as it is" (tens of percent apart, and
        # different nodes ranked). So it refuses to run.
        if basis.path and basis.applied == 0 and not basis.maxed:
            raise MissingInput(
                "the collection could not be applied, so every card would be "
                "scored FULLY MAXED and this plan would silently answer a "
                "different question.\n"
                f"  collection : {basis.path}\n"
                f"  cards restated: {basis.applied} of {len(cards)}\n"
                "  most likely cause: no `card_level_curve.json` in "
                f"{paths.DUMPS} (the refresh builds it)"
            )
        if basis.bonuses is not None:
            cfg = dataclasses.replace(cfg, account=basis.bonuses)
        by_id = {c.id: c for c in cards}
        # The whole catalogue, not just the owned pool (`--team` names unowned cards), at
        # the run's basis.
        self._by_id = by_id
        # full-collection-maxed searches the WHOLE catalogue; the other two
        # scenarios search only what the account owns.
        if self._full_pool:
            self.pool = tuple(cards)
        else:
            self.pool = tuple(by_id[c.id] for c in owned if c.id in by_id)
        self.base_cfg = cfg
        self.skilltree = _load(DUMPS / "skilltree.json")
        self.node_master = _load(DUMPS / "master_SkillTreeNode.json")
        self.collection = _load(REPO / "state" / "collection.json")
        self.caps = _load(DUMPS / "card_caps.json")
        # `board_for`'s per-character memo: (chr, its nodes IN ORDER, its slots) ->
        # (holomem, CharacterBoard, BoardDigest). One Oracle per plan, so it lives as
        # long as that plan's ~750-1,100 scorings and is freed with it.
        self._board_memo: dict = {}
        self._name_by_chr: dict | None = None
        # how many teams `score_team` has scored (the planner's stage log reads it)
        self.scorings = 0
        self.pool_size = pool_size
        # `--song`: score every board through the per-song scorer (`score_song`) instead
        # of `score_card_set`; both return `.total_score` per leader, so `_score_set`
        # dispatches. As in the CLI's `--song`, `prime()` enumerates the team pool
        # song-agnostically and `score()` re-scores those candidates on the chart.
        self.song = song
        self._chart = None
        if song:
            from hbuilder.domain.song import load_charts  # noqa: PLC0415

            charts_file = Path(charts_path) if charts_path else (DUMPS / "charts.json")
            library = load_charts(charts_file)
            if library is None:
                raise MissingInput(
                    f"--song: chart library not on disk at {charts_file}. Build it "
                    "with tools/refresh/build_charts.py"
                )
            chart = library.get(song)
            if chart is None:
                known = ", ".join(library.music_ids[:8])
                raise MissingInput(
                    f"--song: no EXPERT chart for {song!r} in {charts_file} "
                    f"({len(library.music_ids)} songs available; e.g. {known})"
                )
            self._chart = chart

    def _score_set(self, members, leaders, cfg):
        """Dispatch to the per-song scorer when `--song` is set, else the general
        ranking scorer. Both take `(members, leaders, ..., cfg)` and return an
        iterable of results carrying `.total_score`, so every caller maximises
        over either identically without knowing which is live.

        Per song, the deck power is the one `song_rank._score_one` uses for every
        reported row: with `cfg.song_absolute_deck_power` (the default) the deck
        screen's Overall Power per leader (`overall_power_for`: the board's stat
        rates under one ceiling with a singer's conditional ones, and the Member
        Power-Up paid on that base), else the modelled stat pool. The board MILP's
        node values and Act tables are differences of this score, so scoring it on
        another deck power than the rows makes the MILP optimise another function:
        a singer-led team's plan could lose on the reported score while every MILP
        solved to optimality."""
        if self._chart is not None:
            from hbuilder.domain.song import overall_power_for, score_song  # noqa: PLC0415

            if not cfg.song_absolute_deck_power or getattr(_VALUATION, "modelled", False):
                return score_song(members, leaders, self._chart, cfg, auto=cfg.song_auto)
            return tuple(
                score
                for leader in leaders
                for score in score_song(
                    members,
                    (leader,),
                    self._chart,
                    cfg,
                    deck_power=overall_power_for(members, leader, cfg),
                    auto=cfg.song_auto,
                )
            )
        from hbuilder.domain.scoring import score_card_set  # noqa: PLC0415

        return score_card_set(members, leaders, cfg)

    def board_for(self, state: dict):
        """The `BoardModel` of a whole board state.

        Built per CHARACTER and remembered: consecutive states differ in one to six team
        boards while the ~50 off-team ones repeat (rebuilding them is about two thirds of
        a scoring). Exact: each character parses alone and `BoardModel` replays the
        per-board `BoardDigest`s in `characters` order, so the model is the same float
        for float. A state whose new characters report a problem takes the full build
        below, which names them."""
        memo = self._board_memo
        keys = {}
        missing = {}
        for chr_id in sorted(state):
            entry = state[chr_id]
            key = (chr_id, tuple(entry.get("nodes") or ()), tuple(sorted((entry.get("slots") or {}).items())))
            keys[chr_id] = key
            if key not in memo:
                missing[chr_id] = entry
        if missing:
            document, _report, problems = build_board.build(
                missing,
                self.skilltree,
                self.node_master,
                level=None,
                collection=self.collection,
                caps_by_rarity=self.caps,
            )
            if problems:
                return self._board_for_full(state)
            sub = board_from_mapping(document, source="<tree_optimise>")
            if self._name_by_chr is None:
                self._name_by_chr = dict(sub.name_by_chr)
            names = sub.name_by_chr
            for chr_id in missing:
                holomem = names[chr_id]
                memo[keys[chr_id]] = (holomem, sub.characters[holomem], sub._digests[holomem])
        characters = {}
        digests = {}
        for chr_id in sorted(state):
            holomem, board, digest = memo[keys[chr_id]]
            characters[holomem] = board
            digests[holomem] = digest
        return BoardModel(characters=characters, name_by_chr=dict(self._name_by_chr), _digests=digests)

    def _board_for_full(self, state: dict):
        """`board_for` without the memo: the whole state built and parsed at once."""
        document, _report, problems = build_board.build(
            state,
            self.skilltree,
            self.node_master,
            level=None,
            collection=self.collection,
            caps_by_rarity=self.caps,
        )
        if problems:
            raise BoardError(f"build_board reported problems: {problems[:3]}")
        return board_from_mapping(document, source="<tree_optimise>")

    def _cfg(self, state: dict):
        return dataclasses.replace(self.base_cfg, board=self.board_for(state))

    def score_team(self, leader_id: str, member_ids, state: dict) -> float:
        """ONE team's score under ONE board.

        The team is fixed: a max over the pool would optimise some other team's board.
        Per-song when `--song` is set (via `_score_set`), else general."""
        # the full catalogue: a named team may hold a card outside the owned pool
        by_id = self._by_id
        self.scorings += 1
        cfg = dataclasses.replace(self.base_cfg, board=self.board_for(state))
        members = tuple(by_id[m] for m in member_ids)
        results = self._score_set(members, (by_id[leader_id],), cfg)
        return max((r.total_score for r in results), default=0.0)

    def _board_state(self) -> dict:
        """`board_state.json` as `_load` returns it, a fresh copy per call: parsed once
        per file version (an exact plan scores ~1,000 states, and re-parsing the file
        for each would cost about half a second of a plan)."""
        path = DUMPS / "board_state.json"
        if not path.exists():
            return _load(path)  # its MissingInput
        stamp = path.stat().st_mtime_ns
        held = getattr(self, "_state_file", None)
        if held is None or held[0] != stamp:
            held = self._state_file = (stamp, _load(path))
        return _fresh(held[1])

    def state_from(self, nodes_by_character: dict[str, list[str]]) -> dict:
        """A whole board state, keeping every character's SLOTS (and so its Connect
        multipliers)."""
        state = self._board_state()
        for cid, groups in nodes_by_character.items():
            entry = state.setdefault(cid, {"nodes": [], "slots": {}})
            entry["nodes"] = list(groups)
        return state


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def adjacency(model: dict) -> dict[str, set[str]]:
    """`node -> the set of nodes the game links it to`, for one tree model.

    Delegates to `hbuilder.domain.skilltree.neighbours`, the game's rule
    (`SkillTreeMapController.SetAdjacentNode`): FOUR axis steps
    `{(1,0),(-1,0),(0,1),(0,-1)}`, four of the six hex directions.

    ⛔ A hex-distance-1 test would also link the two diagonals `(+1,-1)/(-1,+1)` the
    game never uses, and `cheapest_paths` would under-cost plans through them."""
    return {node: set(links) for node, links in neighbours(model).items()}


ROOT = "S-001"

# The longest chain a single move may buy, and the depth `_branch_values` /
# `fill_boards` look for buyable nodes. The deepest corridor is the RED tree at 17 nodes
# (blue 12, green 10, yellow 12), so 20 covers every branch; a shorter cap would hide the
# deep red/blue nodes (score-support + all-stats, R-052..R-063), the most valuable ones.
DEFAULT_PATH_LENGTH = 20


def cheapest_paths(
    owned: set[str],
    graph: dict[str, set[str]],
    prices: dict,
    *,
    max_nodes: int,
    allowed=None,
) -> dict[str, tuple[str, ...]]:
    """`target -> the cheapest chain of UNOWNED nodes that reaches it`.

    A corridor of worthless nodes can gate a large payoff, and every single-node move
    across it is worth 0; priced as ONE move, the corridor is a purchase that pays off.

    Dijkstra with NODE weights (the point cost) from everything owned at distance 0;
    `max_nodes` bounds the chain length. Paths come in purchase order, so a caller can
    charge them one at a time and keep the board legal at every step."""
    import heapq  # noqa: PLC0415

    # ⛔ ROOT is always free, even when `owned` is empty (a `--respec` bare board, a fresh
    # `--team` character), as in `frontier()`. It carries no price, so it is never pushed.
    free = owned | {ROOT}
    best: dict[str, tuple[int, tuple[str, ...]]] = {}
    heap: list[tuple[int, int, str, tuple[str, ...]]] = []
    counter = 0
    # SORTED: the heap breaks equal costs by push order, so set-iteration order (the
    # per-process string hash seed) would pick different equal-cost paths and the
    # planner would give different boards run to run.
    # `allowed(node)`: a node the caller may buy (the branch filter and
    # keep-only targets); a path never runs through a forbidden node
    for node in sorted(free):
        for other in sorted(graph.get(node, ())):
            if other in free or other not in prices or (allowed is not None and not allowed(other)):
                continue
            counter += 1
            heapq.heappush(heap, (prices[other]["points"], counter, other, (other,)))
    while heap:
        cost, _tie, node, path = heapq.heappop(heap)
        if node in best and best[node][0] <= cost:
            continue
        best[node] = (cost, path)
        if len(path) >= max_nodes:
            continue
        for other in sorted(graph.get(node, ())):
            if other in free or other in path or other not in prices:
                continue
            if allowed is not None and not allowed(other):
                continue
            counter += 1
            heapq.heappush(
                heap,
                (cost + prices[other]["points"], counter, other, path + (other,)),
            )
    # the Connect socket at [0, 0]; every model has it there
    return {node: path for node, (_cost, path) in best.items()}


def frontier(owned: set[str], graph: dict[str, set[str]]) -> set[str]:
    """The nodes buyable NEXT: adjacent to something already owned, rooted at
    the central socket.

    On every character an account has touched, the owned set is fully reachable from
    `S-001`, matching the UI's padlocks. A plan that respects adjacency is safe whether or
    not the game enforces it."""
    reachable = {ROOT}
    stack = [ROOT]
    while stack:
        node = stack.pop()
        for other in graph.get(node, ()):
            if other in owned and other not in reachable:
                reachable.add(other)
                stack.append(other)
    out: set[str] = set()
    for node in reachable:
        for other in graph.get(node, ()):
            if other not in owned and other != ROOT:
                out.add(other)
    return out


def _graphs(skilltree):
    models = {name: adjacency(model) for name, model in skilltree["models"].items()}
    by_character = {m["chr"]: models[m["model"]] for m in skilltree["members"]}
    return by_character


def _affordable(price, wallet, points, cid):
    if price["points"] > points.get(cid, 0):
        return False
    for item, qty in price.items():
        if item == "points":
            continue
        if wallet.get(item, 0) < qty:
            return False
    return True


def _spend(price, wallet, points, cid):
    wallet = dict(wallet)
    points = dict(points)
    points[cid] = points.get(cid, 0) - price["points"]
    for item, qty in price.items():
        if item != "points":
            wallet[item] = wallet.get(item, 0) - qty
    return wallet, points


MAX_CHARACTER_LEVEL = 50


# The three `all_parameter_up_for_character_grouping` rows. They target the
# HOLDER's own grouping, so unlike the rest of G they are
# per-character and must never be deduplicated across boards.
PER_CHARACTER_GREEN = frozenset({"G-008", "G-011", "G-021"})

# The ACTIVE-uptime effect types (activation probability + activation frequency /
# cooldown). Their score is INTERACTIVE: the active Score UP is a per-tick MAX over the
# live skills (`scoring._score_up_pool`), so a second activation saturates against the
# first, against the blues that raise the same skill, and against whether an active lands
# under a SPECIAL cue. A node can read +9,000 alone but 0 on the built board, so they are
# valued IN CONTEXT of the board being built.
_ACTIVE_UPTIME_EFFECT_TYPES = frozenset({"Act.Prob", "Act.CD"})


def _interactive_node_ids(skilltree) -> frozenset:
    """Node ids whose effect is an ACTIVE-uptime family (`_ACTIVE_UPTIME_EFFECT_TYPES`)
    — the ones `fill_boards` values in context rather than in isolation."""
    nodes = skilltree.get("nodes", {})
    return frozenset(
        nid for nid, meta in nodes.items() if (meta.get("e") or {}).get("t") in _ACTIVE_UPTIME_EFFECT_TYPES
    )


def _move_in_scope(target: str, on_team: bool, *, song_mode: bool = False, is_singer: bool = False) -> bool:
    """Whether buying `target` is worth searching on this character.

    RED is leader-scoped and BLUE reaches only its own character's cards, so both are
    worth 0 off the team. GREEN is uncapped flat stats (about +1,000 per board, linear),
    so it is in scope everywhere.

    YELLOW pays differently in the two modes:
    - GENERAL (song-agnostic): the account-global Y caps (~17 boards to saturate), and
      the bench boards cover that from points red/blue can never use: bench only.
    - SONG: only the chart's SINGERS contribute (`song_score_bonus_for` sums the cast,
      up to +40k on a solo song): singers only, team or bench.

    Sockets (`S-`) are filtered by the caller. The single definition of the
    branch-scope rule."""
    branch = target[0]
    if branch == "Y":
        return is_singer if song_mode else not on_team
    if on_team:
        return branch in ("R", "B", "G")
    return branch == "G"


def _socket_branch_pays(branches, *, is_leader, song_mode, is_singer, is_member=True):
    """Does a Connect socket whose shape covers `branches` add ANY score on a team
    character with this role? A socket only multiplies the nodes inside its fixed
    shape, so it is worth a point only where those nodes SCORE for the holder:
      R (Leader branch): only the LEADER (a member's red is dead — it fires only
        while that member leads, which it never does on this fixed team).
      B (own-card branch): a character that fields a MEMBER card (its own board's
        blue). A pure-outfit leader with no member card of hers has no recipient:
        her blue is dead.
      G (account-global): always.
      Y (per-song): the chart's singers in song mode; nobody on a team board in the
        song-agnostic general mode (yellow lives on the bench there).
    So S-002 (red-only shape) pays only on the leader, S-004 (yellow-only) never on a
    general-mode team board, and the fill must not spend a corridor reaching them
    elsewhere."""
    for br in branches:
        if br == "R" and is_leader:
            return True
        if br == "B" and is_member:
            return True
        if br == "G":
            return True
        if br == "Y" and (is_singer if song_mode else False):
            return True
    return False


def _payable_sockets(skilltree, leader_chr, member_chrs, *, song_mode, singer_chars):
    """Per team character, the set of sockets worth BUYING — those whose Connect
    shape can cover a node that PAYS on that character (`_socket_branch_pays`). The
    free root S-001 always qualifies (its shape reaches every branch, so its green
    alone pays). Used to stop `fill_boards` spending corridor points to reach a
    socket no card could ever use on that holder."""
    models = skilltree.get("models", {})
    extents = skilltree.get("extents", {})
    nodes = skilltree.get("nodes", {})
    members = {m["chr"]: m for m in skilltree.get("members", [])}
    # The union of every Connect area's offsets — the MAXIMAL shape any owned card
    # could stamp from a socket. If even that reaches no paying branch, no card can.
    offsets = {tuple(o) for offs in extents.values() for o in offs}
    member_set = set(member_chrs)
    out = {}
    for cid in {leader_chr} | member_set:
        positions = models.get((members.get(cid) or {}).get("model")) or {}
        is_leader = cid == leader_chr
        payable = {"S-001"}
        for socket in ("S-002", "S-003", "S-004"):
            spos = positions.get(socket)
            if spos is None:
                continue
            sx, sy = spos
            covered = {(sx + dx, sy + dy) for dx, dy in offsets}
            branches = {
                nodes.get(nid, {}).get("br")
                for nid, pos in positions.items()
                if tuple(pos) in covered and not nid.startswith("S-")
            }
            if _socket_branch_pays(
                branches,
                is_leader=is_leader,
                song_mode=song_mode,
                is_singer=cid in singer_chars,
                is_member=cid in member_set,
            ):
                payable.add(socket)
        out[cid] = payable
    return out


def _branch_values(
    oracle,
    graphs,
    prices,
    leader_id,
    member_ids,
    *,
    path_length=DEFAULT_PATH_LENGTH,
    singer_chars=frozenset(),
    context: dict | None = None,
):
    """OWN value of each node FOR THE FIXED TEAM, computed ONCE per node:
    RED on the leader, BLUE on a member, GREEN global, YELLOW on a SINGER (song
    mode) else global: the `score_team` marginal over a bare board (stat/global nodes
    stack roughly linearly, so one marginal answers it).

    ⛔ The node is scored ALONE (`{ROOT, target}`), not with its path, which would credit
    every node on the way. An unrouted family (reward/minigame/exp/life) scores 0 and is
    dropped.

    ⚠ In song mode only a chart singer's yellow pays (`song_score_bonus_for`), so a Y node
    is valued on a singer from `singer_chars`, or it would read 0.

    `context` (`{chr: nodes}`, e.g. a solved plan): each node is valued INSIDE that board
    (removed from its host if the board has it, added if not). Inside a built board a node
    is worth more than on a bare one, by a factor that differs per node (a flat-stat red
    x1.2, an Act.Power red x1.8), so the planner re-solves on these values."""

    def tscore(nodes_by_chr):
        return oracle.score_team(
            leader_id,
            member_ids,
            oracle.state_from({c: sorted(v) for c, v in nodes_by_chr.items()}),
        )

    bare = {c: [ROOT] for c in graphs}
    base = tscore(bare)
    leader_chr = "chr-" + leader_id.split("-")[1]
    member_chrs = ["chr-" + m.split("-")[1] for m in member_ids]
    rep_member = next(
        (c for c in member_chrs if c != leader_chr), member_chrs[0] if member_chrs else leader_chr
    )

    # node -> the host character it pays on (RED=leader, BLUE=a member, GREEN=any,
    # YELLOW=a singer in song mode else any).
    host_of = {}
    for cid, graph in graphs.items():
        for target in cheapest_paths({ROOT}, graph, prices, max_nodes=path_length):
            if target in host_of or target in PER_CHARACTER_GREEN or target[0] == "S":
                continue
            branch = target[0]
            if branch == "R":
                host = leader_chr
            elif branch == "B":
                host = rep_member
            elif branch == "Y" and singer_chars:
                # value it on a singer who has the node, else it reads 0
                host = next((s for s in singer_chars if s in graphs and target in graphs[s]), cid)
            else:
                host = cid  # green (account-global), or general-mode yellow
            if host in graphs:
                host_of[target] = host

    values: dict[str, float] = {}
    if context is not None:
        around = {c: set(context.get(c) or ()) | {ROOT} for c in graphs}
        full = tscore(around)
    for target, host in host_of.items():
        try:
            if context is None:
                trial = dict(bare)
                trial[host] = sorted({ROOT, target})  # the node's OWN effect
                val = tscore(trial) - base
            elif target in around[host]:
                val = full - tscore({**around, host: around[host] - {target}})
            else:
                val = tscore({**around, host: around[host] | {target}}) - full
        except BoardError:
            val = 0.0
        if val > 0:
            values[target] = val
    return values


def fill_boards(
    graphs,
    prices,
    state,
    wallet,
    points,
    values,
    leader_chr,
    member_chrs,
    *,
    path_length=DEFAULT_PATH_LENGTH,
    song_mode=False,
    singer_chars=frozenset(),
    interactive_nodes=frozenset(),
    score_fn=None,
    socket_scope=None,
    node_ok=None,
):
    """Spend every character's LEFTOVER points greedily, per its ROLE: the LEADER with
    RED, each MEMBER with BLUE, GREEN everywhere, and YELLOW where it pays (as in
    `_move_in_scope`: the bench in general mode, the chart's singers in song mode).

    Phase 1 fills every non-interactive node on its precomputed isolated value
    (`values`; stat/global nodes stack roughly linearly), so no oracle call in the loop.
    Phase 2 fills the ACTIVE-uptime nodes (`interactive_nodes`) and the generation greens,
    whose value is not additive: each is re-scored IN CONTEXT via `score_fn` and bought
    only on a real positive marginal. Phase 2 is skipped without a `score_fn`."""
    state = {c: set(n) for c, n in state.items()}
    wallet = dict(wallet)
    points = dict(points)
    member_set = set(member_chrs)
    bought = 0

    # SOCKETS FIRST, on the team's boards. An empty socket scores 0, so the value-driven
    # fill never buys one, yet the socket optimiser can only place a card where a socket
    # exists; a socket costs ~1 point plus a short path and holds a card worth thousands.
    # Only the sockets whose shape can PAY on this holder (`socket_scope`; None = all):
    # a red-region socket (S-002) is dead on a member.
    for cid in sorted(({leader_chr} | member_set) & set(graphs)):
        graph = graphs[cid]
        allowed = None if socket_scope is None else socket_scope.get(cid, {ROOT})
        while True:
            have = state.get(cid, {ROOT})
            best = None  # cheapest reachable unbought socket: (path, price)
            for target, path in cheapest_paths(
                have,
                graph,
                prices,
                max_nodes=path_length,
                allowed=None if node_ok is None else (lambda n, c=cid: node_ok(c, n)),
            ).items():
                if not target.startswith("S-"):
                    continue
                if allowed is not None and target not in allowed:
                    continue
                price = {"points": 0}
                for node in path:
                    for key, qty in prices.get(node, {}).items():
                        price[key] = price.get(key, 0) + qty
                if not _affordable(price, wallet, points, cid):
                    continue
                if best is None or price["points"] < best[1]["points"]:
                    best = (path, price)
            if best is None:
                break
            path, price = best
            state[cid] = set(state.get(cid, {ROOT})) | set(path)
            wallet, points = _spend(price, wallet, points, cid)
            bought += 1

    for cid, graph in graphs.items():
        on_team = cid == leader_chr or cid in member_set
        is_singer = cid in singer_chars
        # GREEN everywhere (uncapped); RED on the leader, BLUE on members.
        scope = {"G"}
        if cid == leader_chr:
            scope.add("R")
        if cid in member_set:
            scope.add("B")
        # YELLOW: singers only in song mode, else the bench only.
        if is_singer if song_mode else not on_team:
            scope.add("Y")
        while True:
            have = state.get(cid, {ROOT})
            best = None  # (value_per_point, path, price)
            for target, path in cheapest_paths(
                have,
                graph,
                prices,
                max_nodes=path_length,
                allowed=None if node_ok is None else (lambda n, c=cid: node_ok(c, n)),
            ).items():
                # Interactive nodes wait for phase 2 (their isolated value lies).
                if target[0] not in scope or target in PER_CHARACTER_GREEN or target in interactive_nodes:
                    continue
                price = {"points": 0}
                for node in path:
                    for key, qty in prices.get(node, {}).items():
                        price[key] = price.get(key, 0) + qty
                if not _affordable(price, wallet, points, cid):
                    continue
                # A path may still PASS THROUGH an interactive prereq; it just does
                # not count toward the value (its isolated number would inflate it).
                value = sum(values.get(n, 0.0) for n in path if n[0] in scope and n not in interactive_nodes)
                if value <= 0:
                    continue
                per_pt = value / max(price["points"], 1)
                if best is None or per_pt > best[0]:
                    best = (per_pt, path, price)
            if best is None:
                break
            _, path, price = best
            state[cid] = set(have) | set(path)
            wallet, points = _spend(price, wallet, points, cid)
            bought += 1

    # Phase 2: nodes whose value is CONTEXTUAL, re-scored on the actual board via `score_fn`:
    #   - ACTIVE-uptime (`interactive_nodes`): member-scoped, so team boards only.
    #   - GEN-GREENS (`PER_CHARACTER_GREEN`, G-008/011/021): "+stats to the holder's whole
    #     GENERATION", so they pay on ANY character sharing a team gen (bench included)
    #     and stack across same-gen boards.
    # ~2 score_fn calls per candidate: each scored once on the filled board, then bought in
    # value order, re-validated on the updated board just before (a node a prior buy made
    # redundant re-scores to <=0 and is skipped).
    if score_fn is not None and (interactive_nodes or PER_CHARACTER_GREEN):
        team = ({leader_chr} | member_set) & set(graphs)
        base_score = score_fn(state)
        candidates = []  # (per_pt, cid, path, price)
        for cid, graph in graphs.items():
            on_team = cid in team
            have = state.get(cid, {ROOT})
            for target, path in cheapest_paths(
                have,
                graph,
                prices,
                max_nodes=path_length,
                allowed=None if node_ok is None else (lambda n, c=cid: node_ok(c, n)),
            ).items():
                # interactive: team only (member-scoped); gen-greens: any character.
                if target in interactive_nodes:
                    if not on_team:
                        continue
                elif target not in PER_CHARACTER_GREEN:
                    continue
                price = {"points": 0}
                for node in path:
                    for key, qty in prices.get(node, {}).items():
                        price[key] = price.get(key, 0) + qty
                if not _affordable(price, wallet, points, cid):
                    continue
                trial = {c: set(n) for c, n in state.items()}
                trial[cid] = set(have) | set(path)
                gain = score_fn(trial) - base_score
                if gain > 0:
                    candidates.append((gain / max(price["points"], 1), cid, path, price))
        candidates.sort(reverse=True, key=lambda entry: entry[0])
        for _per_pt, cid, path, price in candidates:
            have = state.get(cid, {ROOT})
            if not (set(path) - have):
                continue  # already owned via an earlier buy's path
            if not _affordable(price, wallet, points, cid):
                continue
            trial = {c: set(n) for c, n in state.items()}
            trial[cid] = have | set(path)
            gain = score_fn(trial) - base_score  # re-validate on the updated board
            if gain <= 0:
                continue
            state[cid] = trial[cid]
            wallet, points = _spend(price, wallet, points, cid)
            base_score += gain
            bought += 1
    return state, wallet, points, bought


def character_levels(owned, prices) -> dict[str, int]:
    """Each character's LEVEL, inverted from the point identity.

    `spent + unspent == budget(level)` holds on every character (`tree_budget.py`),
    which makes the level recoverable rather than hand-entered."""
    import tree_budget  # noqa: PLC0415

    curve = tree_budget.budget_curve()
    unspent = _character_points()
    by_total = {total: level for level, total in curve.items()}
    out = {}
    for cid, groups in owned.items():
        spent = sum(prices[g]["points"] for g in groups if g in prices)
        total = spent + unspent.get(cid, 0)
        out[cid] = by_total.get(total, 0)
    return out


def respec_budget(owned, prices, balance):
    """Everything back in the pot: a board RESET REFUNDS points and materials
    (it costs gold, which the player absorbs).

    So the budget is the inventory PLUS everything already spent, the points are each
    character's FULL grant, and the starting board is bare: "the best board my total
    resources can buy".

    ⛔ ONLY S-001 (the free root at (0,0)) starts owned. S-002/003/004 cost 1 point each
    and need a path to S-001 like any node; as free roots they would let a plan ground an
    R-cluster on S-002 without its corridor, a disconnected board. The card placements
    ride along via `state_from`'s slots; a card on an unbought socket grants nothing."""
    spent, _per_character = _spent(owned, prices)
    wallet = {item: balance.get(item, 0) + spent.get(item, 0) for item in CURRENCIES}
    levels = character_levels(owned, prices)
    import tree_budget  # noqa: PLC0415

    curve = tree_budget.budget_curve()
    points = {cid: curve.get(level, 0) for cid, level in levels.items()}
    bare = {cid: [ROOT] for cid in owned}
    return wallet, points, bare, levels


def resolve_at_max_level(scenario: str, at_max_flag: bool, real_flag: bool) -> bool:
    """Whether to score at CHARACTER level 50 (361 board points), from the
    collection scenario and the two override toggles:

      account               real levels   (--at-max-level lifts it to 361)
      my-cards-maxed         361           (--real-char-level drops it to real)
      full-collection-maxed  361           (forced -- a complete account is level 50)

    `ValueError` on a contradiction rather than a silent winner, because a board
    plan costed on the wrong point budget ranks different nodes."""
    if at_max_flag and real_flag:
        raise ValueError("--at-max-level and --real-char-level are opposites; pick one")
    if scenario == "full-collection-maxed":
        if real_flag:
            raise ValueError(
                "--real-char-level is incompatible with --scenario "
                "full-collection-maxed: a fully maxed account is at level 50"
            )
        return True
    return at_max_flag or (scenario == "my-cards-maxed" and not real_flag)
