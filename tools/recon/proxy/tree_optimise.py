"""The Holomem Board purchase problem: state, price, and a VALUE ORACLE.

An account's best leader often reflects sunk board investment rather than card
quality, and board optimisation is by a wide margin the largest lever on the
score. The prices (`node_price.py`, every row, 9 currencies) and the point
budgets (`tree_budget.py`, per character) are known; what this adds is a way to
ask **what a candidate purchase is WORTH**, measured through the real scorer
rather than argued from magnitudes.

This is that oracle, plus the state and cost model around it.

    python tree_optimise.py --state
    python tree_optimise.py --marginal [--limit N] [--characters chr-...,...]
    python tree_optimise.py --pair R-034 R-049 --character chr-04003

## How value is measured, and the one approximation, named

A candidate purchase changes the BOARD, and the board changes which team is
best (giving every holomem Ina's red nodes moves the winner and the leader
slot entirely). So the honest objective is "the account's best score under this
board", which means re-solving. A full solve is ~10 s; a marginal sweep over
every buyable node would be hours.

⚠ **THE APPROXIMATION.** A team POOL is enumerated ONCE under the base board and
every candidate board is scored by taking the maximum over that fixed pool.
This is the same stage-1-as-filter shape the CLI already uses for `--song`, and
it has the same failure mode: a purchase whose whole point is to promote a team
from OUTSIDE the pool is scored as if that team did not exist, so every number
here is a **LOWER BOUND** on the purchase's value. `--pool` widens it and
`--verify` re-solves the winner from scratch. How large the gap can be is not
known for board purchases, so treat the cap as unvalidated.

## ⛔ Why this file does not yet contain a search

A lone cooldown node can be worth **exactly 0** while two of them crossing an
activation boundary are worth **+25 %**. Node values are NOT independent, so a
greedy "buy the best marginal node" would never buy the first one and would
report the branch as worthless. The marginal table below is therefore a
DIAGNOSTIC, never a ranking to buy from -- `--pair` exists to demonstrate the
non-additivity on the account's own board.

## The cube/hexa balance

The account's balance is in every `User/Get`, in `UserData.user_item_list`
(field 208896283, `UserItem{item_id, quantity}`) -- but only when the body is
read through `decrypt_exact`. `grpc_decrypt.decrypt` does not INFLATE the
response, so a scan over it finds none of the eight `item-skill_tree_*` ids (and
no `card-` either, in a response that carries every card): an absence that is an
artefact of the reading, not a fact about the account. `board_state.py` records
the same trap for the same RPC.

`--balance` is an OVERRIDE. The default is to read the account's own figure from
the NEWEST source on disk — `dumps/inventory.json` when `inventory_snapshot.py`
has run more recently than the last capture, otherwise the newest `.mitm` that
carries a `User/Get`. An older capture names a real source that is simply stale:
every currency moves between captures.

Reads only gitignored account data already on disk. Writes nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

# Windows consoles and pipes default to cp1252, which cannot encode the ⚠ → ×
# this tool prints -- a UnicodeEncodeError there aborts the run mid-output, and
# it lands right before `--dump-state` writes the optimised board (so the plan
# is computed and then thrown away). Force UTF-8 on this process's streams; errors=
# "replace" degrades a stray glyph to "?" rather than crashing. Harmless on
# platforms that are already UTF-8. This module is imported by board_export.py /
# socket_optimise.py / precompute_optimisations.py, so it covers them too.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # not a reconfigurable stream
        pass

HERE = Path(__file__).resolve().parent
DUMPS = HERE / "dumps"
REPO = HERE.parents[2]
# The three collection states the board export uses, in order.
SCENARIOS = ("account", "my-cards-maxed", "full-collection-maxed")
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "src"))

import build_board  # noqa: E402
import node_price  # noqa: E402

from hbuilder.cli import apply_collection, build_parser  # noqa: E402
from hbuilder.domain.board import BoardError, BoardModel, board_from_mapping  # noqa: E402
from hbuilder.domain.loader import load_cards, load_owned  # noqa: E402
from hbuilder.domain.scoring_config import load_scoring_config  # noqa: E402
from hbuilder.solver.heuristic import solve_heuristic  # noqa: E402
from hbuilder.domain.team import MEMBER_COUNT  # noqa: E402
from hbuilder.domain.skilltree import neighbours  # noqa: E402

import dataclasses  # noqa: E402

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
        raise SystemExit(f"missing {path} -- gitignored account data")
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


def _prices():
    """group id -> {"points": int, currency-id: qty}.

    ⛔ Read through `node_price.load`, which goes to the SQLCipher master
    DIRECTLY and keeps field 1000 as BYTES. `dumps/master_SkillTreeNode.json`
    cannot be used here: its field-1000 blob has been through a UTF-8 decode,
    so a quantity byte like 0x8f is already U+FFFD and every price above 127
    would read wrong. `tree_cost.py` and `node_price.py` both avoid it the same
    way.

    `node_price.price` reads EVERY entry of the repeated field 1000, which is
    what recovers the four Hexa currencies -- reading only `raw[0]` silently
    drops the second entry on many rows."""
    rows = node_price.load("SkillTreeNode")
    out: dict[str, dict] = {}
    for row in rows:
        group = node_price.f(row, 1)
        if not group or group in out:
            # Rows within a group differ by `characterIds`; the PRICE is
            # identical across them, so the first is
            # enough for a cost model. WHICH row's EFFECT applies is a
            # different question and `build_board._select_row` owns it.
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


ITEM_FIELD = 208896283  # UserData.user_item_list
ITEM_SUB = {2: "item_id", 3: "expired_time", 4: "quantity", 5: "last_acquired_time"}


def balance_from_disk(directory: Path = DUMPS) -> tuple[dict, str] | tuple[None, None]:
    """The account's own cube/hexa balance, from the NEWEST source on disk.

    ⛔ Through `decrypt_exact`, never `decrypt`. The plain path does not inflate
    the body and yields garbage for a response that carries every card, which
    makes the balance look absent. `board_state.py` records the same trap on the
    same RPC.

    ⛔ **`inventory.json` FIRST when it is newer.** `inventory_snapshot.py`
    writes the balance straight from a live `User/Get` to `dumps/inventory.json`,
    and every currency moves between captures. A board plan costed against an
    older, smaller wallet is not conservative, it is simply answering a question
    about yesterday -- and nothing would say it is stale, because the source it
    names is real.

    Returns the figures and the source they came from, or `(None, None)`. Never a
    default: a balance nobody measured must not be indistinguishable from a
    balance that is zero."""
    import grpc_decrypt as gd  # noqa: PLC0415
    import account_snapshot as snap  # noqa: PLC0415

    newest_mitm = max(
        (p.stat().st_mtime for p in directory.glob("*.mitm")), default=0.0
    )
    stock = directory / "inventory.json"
    if stock.exists() and stock.stat().st_mtime >= newest_mitm:
        try:
            document = json.loads(stock.read_text("utf8"))
            # `items` carries the currency ids verbatim; `skill_tree` is the same
            # eight under their English names, which is the display side.
            held = {k: int(v) for k, v in (document.get("items") or {}).items()
                    if k in CURRENCIES}
            if len(held) == len(CURRENCIES):
                return held, f"{stock.name} ({document.get('_captured_at', 'undated')})"
        except (OSError, ValueError, TypeError):
            pass  # fall through to the captures rather than fail the run

    schema = snap.load_userdata_schema()
    if not schema:
        return None, None
    candidates = sorted(
        directory.glob("*.mitm"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    for path in candidates:
        try:
            for name, _token, body in gd.iter_grpc_exact(str(path)):
                if "User/Get" not in name:
                    continue
                level = snap.locate_userdata(body, schema)
                if not level:
                    continue
                held = {}
                for fn, wt, payload in level:
                    if wt != 2 or fn != ITEM_FIELD:
                        continue
                    row = snap.sub(payload, ITEM_SUB)
                    item = str(row.get("item_id") or "")
                    if item in CURRENCIES:
                        held[item] = int(row.get("quantity") or 0)
                if held:
                    return held, path.name
        except Exception:  # noqa: BLE001, S112
            continue
    return None, None


def parse_balance(text: str | None) -> dict | None:
    """`--balance "red_cube=3900,red_hexa=224"` -> currency id -> int.

    `None` when not supplied, and that `None` is load-bearing: a missing
    balance must never read as zero, and must never read as unlimited."""
    if not text:
        return None
    short = {
        "red_cube": "item-skill_tree_leader-1",
        "red_hexa": "item-skill_tree_leader-2",
        "blue_cube": "item-skill_tree_card-1",
        "blue_hexa": "item-skill_tree_card-2",
        "green_cube": "item-skill_tree_allmember-1",
        "green_hexa": "item-skill_tree_allmember-2",
        "yellow_cube": "item-skill_tree_content-1",
        "yellow_hexa": "item-skill_tree_content-2",
    }
    out = {}
    for part in text.split(","):
        key, _, value = part.partition("=")
        key = key.strip().lower()
        item = short.get(key, key)
        if item not in CURRENCIES:
            raise SystemExit(f"--balance: unknown currency {key!r}; known: {sorted(short)}")
        out[item] = int(value)
    return out


# --------------------------------------------------------------------------
# the value oracle
# --------------------------------------------------------------------------


class Oracle:
    """Scores a candidate board through the REAL scorer.

    Everything except the board is loaded exactly once and exactly the way the
    CLI loads it -- same `load_cards`, same `apply_collection`, same
    `load_scoring_config`. A second definition of what a card is worth is how
    the tools would drift into wrong numbers, so there is not one here.
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
        # `scenario` is one of the three collection states of the board export.
        # It is two axes — the scoring BASIS (real levels vs maxed) and the POOL
        # (owned cards vs the full 169-card catalogue):
        #
        #   account               real levels   owned  (the default; legacy)
        #   my-cards-maxed         maxed         owned
        #   full-collection-maxed  maxed         all cards
        #
        # `data/cards.json` IS the fully-maxed state, so a maxed basis is the
        # collection deliberately NOT applied (see `apply_collection`). The two
        # maxed scenarios score identical cards and differ ONLY in the pool.
        #
        # `scenario=None` keeps the legacy contract: the explicit `maxed` flag
        # drives the basis and the pool is owned — so `--team`'s `maxed=True`
        # Oracle is unchanged. A named scenario takes over both axes.
        if scenario is not None:
            if scenario not in SCENARIOS:
                raise SystemExit(
                    f"--scenario: unknown {scenario!r}; one of {', '.join(SCENARIOS)}"
                )
            maxed = scenario != "account"
        self._full_pool = scenario == "full-collection-maxed"
        self.scenario = scenario or "account"
        # `maxed=True` scores every card at max level and bloom -- the basis a
        # NAMED team needs, because a team you are planning toward is usually not
        # one you own at those levels yet (`--team`). It also removes
        # the cwd-relative-curve trap below: with `maxed` there is no collection
        # to apply, so there is nothing to silently fail to find.
        self.maxed = maxed
        # The CLI's OWN defaults, read off its parser rather than restated as
        # path literals -- the same rule `tests/test_collection.py` follows,
        # and stricter than a second constant because it cannot disagree with
        # the tool.
        defaults = build_parser().parse_args(["solve"])
        cards = load_cards(REPO / defaults.cards)
        owned = load_owned(REPO / defaults.owned, cards)
        # ⛔ **`REPO /`, not the bare default.** `load_scoring_config`'s
        # default is a RELATIVE `Path("config.json")` -- its docstring says
        # "repo-root", and that is only true when the cwd happens to BE the repo
        # root. An absent file falls back to `DEFAULT_CONFIG` in full and says
        # nothing, which is the right contract for the CLI and a trap for a
        # script under `tools/`. Every other path in this constructor is
        # anchored to `REPO` too.
        cfg = load_scoring_config(REPO / "config.json")
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
        # ⛔ **THE SILENT BASIS SWITCH.** `apply_collection`
        # reads `card_level_curve.json` and `card_caps.json` through
        # `cli.COLLECTION_CURVE_PATH`, a path RELATIVE to the process's cwd. Run
        # from anywhere but the repo root those files are simply not found, and
        # an absent curve is a FIRST-CLASS STATE by design: every card keeps
        # `data/cards.json`'s value, and `data/cards.json` is the FULLY-MAXED
        # state. So the run does not fail and does not degrade -- it quietly
        # answers a different question ("every owned card maxed" instead of
        # "the account as it is"), tens of percent apart on the same board.
        #
        # A board plan costed on the wrong basis is not slightly off, it ranks
        # different nodes -- so this refuses to run rather than print a number
        # whose meaning depends on the directory it was launched from.
        if basis.path and basis.applied == 0 and not basis.maxed:
            raise SystemExit(
                "the collection could not be applied, so every card would be "
                "scored FULLY MAXED and this plan would silently answer a "
                "different question.\n"
                f"  collection : {basis.path}\n"
                f"  cards restated: {basis.applied} of {len(cards)}\n"
                "  most likely cause: `card_level_curve.json` is resolved "
                "RELATIVE to the current directory.\n"
                f"  fix: run from the repo root ({REPO})"
            )
        if basis.bonuses is not None:
            cfg = dataclasses.replace(cfg, account=basis.bonuses)
        by_id = {c.id: c for c in cards}
        # The FULL catalogue lookup (all 169 cards), not just the owned pool.
        # Building `by_id` from `self.pool` alone would KeyError on any card the
        # account does not own -- the exact case `--team` needs.
        # `cards` already carries every catalogue card (owned at real levels or,
        # under `maxed`, all at max), so this resolves any id at the run's basis.
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
        self.owned_nodes = _owned_nodes()
        self.pool_size = pool_size
        self._teams = None
        self._base_score = None
        # `--song`: score every board through the PER-SONG scorer (`score_song`)
        # instead of the general ranking scorer (`score_card_set`). The two share
        # a return shape (`.total_score` per leader), so `_score_set` dispatches
        # and the rest of the Oracle is unchanged. This is the SAME stage-1-as-
        # filter shape the CLI's `--song` uses: `prime()` still enumerates the
        # team pool song-AGNOSTICALLY (stage 1), and `score()` re-scores those
        # candidates on the chart (stage 2). Scoring the pool per-song is a
        # separate, heavier question left to a future `--song`-aware `prime`.
        self.song = song
        self._chart = None
        if song:
            from hbuilder.domain.song import load_charts  # noqa: PLC0415

            charts_file = Path(charts_path) if charts_path else (DUMPS / "charts.json")
            library = load_charts(charts_file)
            if library is None:
                raise SystemExit(
                    f"--song: chart library not on disk at {charts_file}. Build it "
                    "with tools/recon/proxy/build_charts.py"
                )
            chart = library.get(song)
            if chart is None:
                known = ", ".join(library.music_ids[:8])
                raise SystemExit(
                    f"--song: no EXPERT chart for {song!r} in {charts_file} "
                    f"({len(library.music_ids)} songs available; e.g. {known})"
                )
            self._chart = chart

    def _score_set(self, members, leaders, cfg):
        """Dispatch to the per-song scorer when `--song` is set, else the general
        ranking scorer. Both take `(members, leaders, ..., cfg)` and return an
        iterable of results carrying `.total_score`, so every caller maximises
        over either identically without knowing which is live.

        The per-song default `deck_power` (the modelled member stat pool) is the
        one `rank_on_chart`/`_score_one` pass too — kept identical on purpose so
        the Oracle and the CLI's `--song` path cannot diverge. Note the board's
        STAT contribution (`board_power`) is deliberately outside that default in
        `score_song` (its own documented boundary); the board still reaches the
        per-song score through the score-bonus / skill-effect channels, which is
        the yellow-node and socket interaction `--song` exists to optimise."""
        if self._chart is not None:
            from hbuilder.domain.song import score_song  # noqa: PLC0415

            return score_song(members, leaders, self._chart, cfg)
        from hbuilder.domain.scoring import score_card_set  # noqa: PLC0415

        return score_card_set(members, leaders, cfg)

    def board_for(self, state: dict):
        """The `BoardModel` of a whole board state.

        Built per CHARACTER and remembered: a planner's consecutive states differ in
        one to six team boards while the ~50 off-team ones repeat, and rebuilding all
        of them would be about two thirds of every scoring. Exact, not approximate:
        character's rows from that character's nodes (in their order) and slots
        alone, `board_from_mapping` parses each character alone, and `BoardModel`
        replays the per-board `BoardDigest`s in `characters` order -- the same
        objects in the same order give the same model, float for float. A state
        whose new characters report any problem takes the full build below, which
        names them exactly as before."""
        memo = self._board_memo
        keys = {}
        missing = {}
        for chr_id in sorted(state):
            entry = state[chr_id]
            key = (chr_id, tuple(entry.get("nodes") or ()),
                   tuple(sorted((entry.get("slots") or {}).items())))
            keys[chr_id] = key
            if key not in memo:
                missing[chr_id] = entry
        if missing:
            document, _report, problems = build_board.build(
                missing, self.skilltree, self.node_master, level=None,
                collection=self.collection, caps_by_rarity=self.caps,
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
        return BoardModel(characters=characters, name_by_chr=dict(self._name_by_chr),
                          _digests=digests)

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
            raise SystemExit(f"build_board reported problems: {problems[:3]}")
        return board_from_mapping(document, source="<tree_optimise>")

    def _cfg(self, state: dict):
        return dataclasses.replace(self.base_cfg, board=self.board_for(state))

    def _base_state(self):
        return {cid: {"nodes": list(nodes), "slots": {}} for cid, nodes in self.owned_nodes.items()}

    def prime(self):
        """Enumerate the team pool ONCE under the base board."""
        if self._teams is not None:
            return
        state = self._base_state()
        # The slots carry the Connect multipliers and must survive: rebuild
        # from the file rather than from `_base_state`, which drops them.
        state = _load(DUMPS / "board_state.json")
        cfg = self._cfg(state)
        started = time.monotonic()
        ranked = solve_heuristic(self.pool, cfg, top_n=self.pool_size)
        if not self.quiet:
            print(
                f"pool: {len(ranked)} teams enumerated under the base board "
                f"in {time.monotonic() - started:.1f}s",
                file=sys.stderr,
            )
        self._teams = tuple((t.leader_id, t.member_ids) for t in ranked)
        self._base_score = ranked[0].score if ranked else 0.0

    def score(self, state: dict) -> float:
        """Best score over the FIXED pool under `state`'s board. See the
        module docstring: this is a lower bound on the true best.

        Per-song when `--song` is set (via `_score_set`), else general."""
        self.prime()
        cfg = self._cfg(state)
        by_id = {c.id: c for c in self.pool}
        best = 0.0
        for leader_id, member_ids in self._teams:
            members = tuple(by_id[m] for m in member_ids)
            leaders = (by_id[leader_id],)
            for result in self._score_set(members, leaders, cfg):
                if result.total_score > best:
                    best = result.total_score
        return best

    @property
    def base_score(self) -> float:
        self.prime()
        return self.score(_load(DUMPS / "board_state.json"))

    def score_team(self, leader_id: str, member_ids, state: dict) -> float:
        """ONE team's score under ONE board.

        The per-team planner needs this rather than `score`, which maximises
        over the whole pool: here the team is fixed by construction, and taking
        a max would silently optimise some OTHER team's board and report it
        under this team's name.

        Per-song when `--song` is set (via `_score_set`), else general."""
        # `self._by_id` is the full catalogue, so a NAMED team may reference a
        # card outside the owned pool; `self.pool` alone would KeyError on it.
        by_id = self._by_id
        cfg = dataclasses.replace(self.base_cfg, board=self.board_for(state))
        members = tuple(by_id[m] for m in member_ids)
        results = self._score_set(members, (by_id[leader_id],), cfg)
        return max((r.total_score for r in results), default=0.0)

    def pool_characters(self) -> set[str]:
        """The characters whose cards appear on ANY team in the pool.

        A board node only ever reaches its own character's cards, so a node on
        a character absent from every pooled team is worth exactly 0.

        ⚠ **That zero is partly an artefact of the fixed pool and must not be
        read as "this character is worthless".** A purchase whose whole point is
        to promote a card from outside the pool cannot show up, because the pool
        was enumerated before the purchase. Widening `--pool` widens this set;
        it does not remove the bias, it bounds it."""
        out = set()
        for _leader, member_ids in self._teams or ():
            for card_id in member_ids:
                parts = card_id.split("-")
                if len(parts) > 1:
                    out.add(f"chr-{parts[1]}")
        for leader, _members in self._teams or ():
            parts = leader.split("-")
            if len(parts) > 1:
                out.add(f"chr-{parts[1]}")
        return out

    def skilltree_model(self, cid: str) -> str:
        for member in self.skilltree["members"]:
            if member["chr"] == cid:
                return member["model"]
        return "?"

    def _board_state(self) -> dict:
        """`board_state.json` as `_load` returns it, a fresh copy per call: parsed once
        per file version (an exact plan scores ~1,000 states, and re-parsing the file
        for each would cost about half a second of a plan)."""
        path = DUMPS / "board_state.json"
        if not path.exists():
            return _load(path)          # its SystemExit
        stamp = path.stat().st_mtime_ns
        held = getattr(self, "_state_file", None)
        if held is None or held[0] != stamp:
            held = self._state_file = (stamp, _load(path))
        return _fresh(held[1])

    def state_from(self, nodes_by_character: dict[str, list[str]]) -> dict:
        """A whole board state, keeping every character's SLOTS. Dropping the
        slots would silently drop the Connect multipliers, which is a change to
        the thing being measured rather than to the thing being varied."""
        state = self._board_state()
        for cid, groups in nodes_by_character.items():
            entry = state.setdefault(cid, {"nodes": [], "slots": {}})
            entry["nodes"] = list(groups)
        return state

    def with_nodes(self, additions: dict[str, list[str]]) -> dict:
        state = _load(DUMPS / "board_state.json")
        for cid, groups in additions.items():
            entry = state.setdefault(cid, {"nodes": [], "slots": {}})
            nodes = list(entry.get("nodes") or [])
            for group in groups:
                if group not in nodes:
                    nodes.append(group)
            entry["nodes"] = nodes
        return state


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def cmd_state(args) -> None:
    prices = _prices()
    owned = _owned_nodes()
    points = _character_points()
    spent, spent_points = _spent(owned, prices)
    balance = parse_balance(args.balance)
    source = "--balance"
    if balance is None:
        balance, source = balance_from_disk()

    touched = {c: n for c, n in owned.items() if n}
    print(f"characters with any node: {len(touched)} of {len(owned)}")
    print(f"nodes owned: {sum(len(n) for n in owned.values())}")
    print()
    print("SPENT so far (from board_state.json x the master price vector):")
    print(f"  {'Holomem Board Pt':18s} {spent.get('points', 0):>8,}  (per character, does not pool)")
    for item in CURRENCIES:
        if spent.get(item):
            print(f"  {PRETTY[item]:18s} {spent[item]:>8,}")
    print()
    print("BUDGET:")
    if points:
        listed = sorted(points.items(), key=lambda kv: -kv[1])[:8]
        print(f"  Holomem Board Pt, unspent, per character ({len(points)} characters known):")
        for cid, qty in listed:
            print(f"    {cid}  {qty:>4}  (spent {spent_points.get(cid, 0)})")
        if len(points) > 8:
            print(f"    ... {len(points) - 8} more")
    else:
        print("  Holomem Board Pt: UNKNOWN -- account_snapshot_main.json has no board_points")
    print()
    if balance is None:
        print("  Cube / Hexa balance: UNKNOWN -- no capture on disk carried a User/Get")
        print("    with a user_item_list. Nothing downstream may default this to a number.")
        return
    print(f"  Cube / Hexa, account-wide (from {source}):")
    full_board = {
        "item-skill_tree_leader-1": 6065,
        "item-skill_tree_leader-2": 600,
        "item-skill_tree_card-1": 1870,
        "item-skill_tree_card-2": 160,
        "item-skill_tree_allmember-1": 2700,
        "item-skill_tree_allmember-2": 150,
        "item-skill_tree_content-1": 1870,
        "item-skill_tree_content-2": 160,
    }
    print(f"    {'currency':14s} {'held':>8s} {'one board':>10s} {'covers':>8s}")
    tightest = None
    for item in CURRENCIES:
        held = balance.get(item)
        need = full_board[item]
        if held is None:
            print(f"    {PRETTY[item]:14s} {'UNKNOWN':>8s}")
            continue
        share = held / need
        print(f"    {PRETTY[item]:14s} {held:>8,} {need:>10,} {share * 100:>7.1f}%")
        if tightest is None or share < tightest[1]:
            tightest = (PRETTY[item], share)
    if tightest:
        print()
        print(
            f"  ⚑ TIGHTEST: {tightest[0]} at {tightest[1] * 100:.1f} % of ONE complete board."
        )
        print("    'One board' is NODE-PRICE.md §3's 52-character figure; the cube pool is")
        print("    account-wide and funds all 54, so this is the optimistic reading.")


def adjacency(model: dict) -> dict[str, set[str]]:
    """`node -> the set of nodes the game links it to`, for one tree model.

    Delegates to `hbuilder.domain.skilltree.neighbours` -- the game's real rule,
    pinned to the game's degree histogram by
    `tests/test_skilltree_selection.py`. The game builds adjacency in
    `SkillTreeMapController.SetAdjacentNode` by walking a static `Directions`
    array of exactly FOUR axis steps `{(1,0),(-1,0),(0,1),(0,-1)}` -- four of
    the six hex directions -- and linking a node at `(x,y)` to whatever node
    sits at `(x+dx, y+dy)`.

    ⛔ A hex-distance-1 test would ALSO link the two hex diagonals
    `(+1,-1)/(-1,+1)` the game never uses -- hundreds of phantom edges across
    the four layouts -- and `cheapest_paths` would shortcut through them and
    UNDER-COST plans. Reusing the one domain rule keeps the recon tool and the
    solver from disagreeing."""
    return {node: set(links) for node, links in neighbours(model).items()}


ROOT = "S-001"

# The longest chain a single move may buy, and the depth `_branch_values` /
# `fill_boards` look for buyable nodes. The deepest corridor across ALL
# characters is the RED tree at 17 nodes (blue 12, green 10, yellow 12), so 20
# covers every branch with margin. A cap of 10 would blind the planner to the
# deep half of red/blue — the highest-value nodes (score-support + all-stats,
# R-052..R-063) — and it would spill leftover points into flat greens instead.
# Raising it costs one extra oracle call per newly-reachable node (~sub-second),
# so the cap buys correctness cheaply.
DEFAULT_PATH_LENGTH = 20


def cheapest_paths(
    owned: set[str], graph: dict[str, set[str]], prices: dict, *, max_nodes: int,
    allowed=None,
) -> dict[str, tuple[str, ...]]:
    """`target -> the cheapest chain of UNOWNED nodes that reaches it`.

    ⛔ **This is a change of DECISION UNIT, not a smarter ranking.** A corridor
    of eight consecutive worthless red nodes (28 points, 680 cube) can gate a
    payoff of tens of thousands. Every single-node move across that stretch is
    worth exactly 0, so no beam width and no k-step lookahead short of eight can
    cross it. Priced as ONE move, the corridor is simply a purchase that costs
    28 points and pays off, and the search sees it the way a player does.

    Dijkstra with NODE weights (the point cost), sources = everything already
    owned at distance 0, since reaching an owned node is free. `max_nodes` bounds
    the chain length; without it every node in the tree is a move and the step
    cost explodes.

    Returns paths in purchase order, so a caller can charge them one at a time
    and keep the board legal at every intermediate state — which matters,
    because a partially-bought path is exactly what the adjacency rule permits."""
    import heapq  # noqa: PLC0415

    # ⛔ ROOT is always free, even when `owned` is empty. Seeding from `owned`'s
    # neighbours alone would leave a character owning ZERO nodes -- exactly a
    # `--respec` bare board or a fresh `--team` character -- with an empty seed,
    # unable to buy anything. `frontier()` roots reachability at S-001
    # regardless of `owned`; this must agree with it. `ROOT` carries no price,
    # so it is never itself pushed as a purchase, and for a character who
    # already owns the root it is a no-op.
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
    return {node: path for node, (_cost, path) in best.items()}  # the Connect socket at [0, 0]; every model has it there


def frontier(owned: set[str], graph: dict[str, set[str]]) -> set[str]:
    """The nodes buyable NEXT: adjacent to something already owned, rooted at
    the central socket.

    This is a constraint rather than a guess: on every character an account
    has touched, the owned set is fully reachable from `S-001`. Hundreds of
    nodes do not land connected by accident, and it matches the UI's padlocks.

    ⚠ Consistent with the rule, not a proof OF it: a player buying connected
    sets by preference would look identical. What would settle it is one
    `SkillTreeNode` unlock-condition field read off the master, or the game
    refusing a disconnected purchase. A plan that respects adjacency is safe
    under both readings, and one that ignores it is safe under neither."""
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


def _buyable(owned, prices, characters):
    """(character, group) pairs the account does not own. Sockets excluded:
    an `S-*` node has no effect of its own (no paying row)
    and its value is entirely in what card is placed in it -- a different
    decision with a different input."""
    out = []
    for cid in sorted(characters or owned):
        have = set(owned.get(cid) or [])
        for group in sorted(prices):
            if group.startswith("S-") or group in have:
                continue
            out.append((cid, group))
    return out


def cmd_marginal(args) -> None:
    prices = _prices()
    owned = _owned_nodes()
    points = _character_points()
    oracle = Oracle(pool_size=args.pool, song=args.song, charts_path=args.charts,
                    scenario=args.scenario)
    base = oracle.base_score
    print(f"base best score over the fixed pool: {base:,.1f}", file=sys.stderr)

    characters = args.characters.split(",") if args.characters else None
    candidates = _buyable(owned, prices, characters)
    if args.limit:
        candidates = candidates[: args.limit]
    print(f"measuring {len(candidates)} candidate purchases", file=sys.stderr)

    rows = []
    started = time.monotonic()
    for index, (cid, group) in enumerate(candidates, 1):
        score = oracle.score(oracle.with_nodes({cid: [group]}))
        price = prices[group]
        rows.append((score - base, cid, group, price))
        if index % 25 == 0:
            rate = (time.monotonic() - started) / index
            print(
                f"  {index}/{len(candidates)}  {rate * 1000:.0f} ms/candidate",
                file=sys.stderr,
            )

    rows.sort(key=lambda r: -r[0])
    nonzero = [r for r in rows if r[0] > 0]
    print()
    print(f"{len(nonzero)} of {len(rows)} single nodes move the score at all.")
    print()
    print(f"{'delta':>12} {'%':>7}  {'character':12s} {'node':7s} {'pts':>4}  price")
    for delta, cid, group, price in rows[: args.top]:
        items = "  ".join(
            f"{PRETTY[i]} {q}" for i, q in price.items() if i != "points" and q
        )
        pct = delta / base * 100 if base else 0.0
        print(f"{delta:>12,.1f} {pct:>6.3f}%  {cid:12s} {group:7s} {price['points']:>4}  {items}")

    print()
    print("⛔ This table is a DIAGNOSTIC, not a shopping list. SEARCH-PLAN.md §1a:")
    print("   a lone cooldown node is worth 0 and two crossing an activation")
    print("   boundary are worth +25 %. Buying down this ranking would never buy")
    print("   the first one. Use --pair to see the non-additivity directly.")
    if points:
        print()
        over = [
            (cid, group)
            for _d, cid, group, price in rows[: args.top]
            if points.get(cid, 0) < price["points"]
        ]
        if over:
            print(f"⚠ {len(over)} of the top rows are unaffordable in POINTS today: {over[:5]}")


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


def points_at_max(owned, prices) -> dict[str, int]:
    """Every character at CHARACTER level 50, minus what each has already spent.

    ⚠ **Character level, not card level.** They are different quantities with
    different sources and the board only ever cares about the first: points come
    from `CharacterLevel.skill_tree_point_quantity` summed over levels 1..L
    (`tree_budget.py`), and `budget(50) = 361` against a 453-point full board --
    so even a maxed character can buy about four fifths of one tree.

    The identity that makes this safe to compute is `spent + unspent ==
    budget(level)`, which holds on every character (`tree_budget.py`).
    Subtracting the spend from 361 therefore gives exactly what a level-50
    version of this account would have left."""
    import tree_budget  # noqa: PLC0415

    curve = tree_budget.budget_curve()
    grant = curve[MAX_CHARACTER_LEVEL]
    out = {}
    for cid, groups in owned.items():
        spent = sum(prices[g]["points"] for g in groups if g in prices)
        out[cid] = grant - spent
    return out


INFINITE = 10**9

# The three `all_parameter_up_for_character_grouping` rows. They target the
# HOLDER's own grouping, so unlike the rest of G they are
# per-character and must never be deduplicated across boards.
PER_CHARACTER_GREEN = frozenset({"G-008", "G-011", "G-021"})

# The ACTIVE-uptime effect types (activation probability + activation frequency /
# cooldown). Their score is INTERACTIVE, not additive: the game's active Score UP
# is a per-tick MAX over the live skills (as in the client; `scoring._score_up_pool`), so a
# second activation saturates against the first, against the stat blues that raise
# the same skill, and against whether an active lands under a SPECIAL cue (a special
# pays nothing with no active beneath it). So an ISOLATED marginal wildly overstates
# them — a node can read +9,000 alone but 0 on the built board — and they must be valued
# IN CONTEXT of the board actually being built, not on a bare one.
_ACTIVE_UPTIME_EFFECT_TYPES = frozenset({"Act.Prob", "Act.CD"})


def _interactive_node_ids(skilltree) -> frozenset:
    """Node ids whose effect is an ACTIVE-uptime family (`_ACTIVE_UPTIME_EFFECT_TYPES`)
    — the ones `fill_boards` values in context rather than in isolation."""
    nodes = skilltree.get("nodes", {})
    return frozenset(
        nid for nid, meta in nodes.items()
        if (meta.get("e") or {}).get("t") in _ACTIVE_UPTIME_EFFECT_TYPES
    )


def _move_in_scope(
    target: str, on_team: bool, *, song_mode: bool = False, is_singer: bool = False
) -> bool:
    """Whether buying `target` is worth searching on this character.

    RED is leader-scoped and BLUE reaches only its own character's cards, so both
    are worth exactly 0 on a character that is not on the team being planned
    (checked on off-team characters). GREEN is uncapped flat stats (it stacks
    linearly past 30 boards, about +1,000 per board), so every board's
    leftover points -- team included -- are worth a green, and G stays in
    scope everywhere.

    YELLOW pays differently in the two modes:
    - GENERAL (song-agnostic): the account-global song-INDEPENDENT Y CAPS (~17
      boards to saturate), and the ~24 BENCH boards cover that cap from points
      red/blue can never use. So it goes on the BENCH, never a team board.
    - SONG: only the chart's SINGERS contribute yellow (`song_score_bonus_for`
      sums the cast). A singer's yellow is a real per-song bonus (up to +40k on
      a solo song); a non-singer's is 0. So Y is in scope on the SINGERS (team
      or bench) and nobody else.

    Sockets (`S-`) are filtered by the caller before this is asked, so a
    non-socket target's first character is one of R/B/G/Y. This is the single
    definition of the branch-scope rule; `cmd_plan` and `_plan_for_team` both read
    it so the two cannot drift."""
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
            branches = {nodes.get(nid, {}).get("br")
                        for nid, pos in positions.items()
                        if tuple(pos) in covered and not nid.startswith("S-")}
            if _socket_branch_pays(branches, is_leader=is_leader,
                                   song_mode=song_mode, is_singer=cid in singer_chars,
                                   is_member=cid in member_set):
                payable.add(socket)
        out[cid] = payable
    return out


def _branch_values(oracle, graphs, prices, leader_id, member_ids, *,
                   path_length=DEFAULT_PATH_LENGTH, singer_chars=frozenset()):
    """OWN value of each node FOR THE FIXED TEAM, computed ONCE per node:
    RED on the leader, BLUE on a member, GREEN global, YELLOW on a SINGER (song
    mode) else global. `score_team` marginal over a bare board — the effect is
    per-node and roughly state-independent (stat/global nodes stack linearly), so
    one marginal answers it and `fill_boards` needs no oracle call in its loop.

    ⛔ Score the node ALONE (`{ROOT, target}`), NOT its whole path: a path's
    value would credit every node on the way (a reward green can look worth
    thousands that way).
    An unrouted family (reward/minigame/exp/life) raises `BoardError`/scores 0 and
    is dropped; only nodes that move the team score keep a value.

    ⚠ YELLOW is HOST-DEPENDENT in song mode: only a chart singer's yellow pays
    (`song_score_bonus_for`), so a Y node valued on a non-singer reads 0 and is
    never bought even on the singer where it is worth +40k. `singer_chars` (the
    chart's cast) makes the Y host a singer, so the value is the real one."""
    def tscore(nodes_by_chr):
        return oracle.score_team(
            leader_id, member_ids,
            oracle.state_from({c: sorted(v) for c, v in nodes_by_chr.items()}),
        )

    bare = {c: [ROOT] for c in graphs}
    base = tscore(bare)
    leader_chr = "chr-" + leader_id.split("-")[1]
    member_chrs = ["chr-" + m.split("-")[1] for m in member_ids]
    rep_member = next((c for c in member_chrs if c != leader_chr),
                      member_chrs[0] if member_chrs else leader_chr)

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
                host = next((s for s in singer_chars
                             if s in graphs and target in graphs[s]), cid)
            else:
                host = cid  # green (account-global), or general-mode yellow
            if host in graphs:
                host_of[target] = host

    values: dict[str, float] = {}
    for target, host in host_of.items():
        trial = dict(bare)
        trial[host] = sorted({ROOT, target})  # the node's OWN effect
        try:
            val = tscore(trial) - base
        except BoardError:
            val = 0.0
        if val > 0:
            values[target] = val
    return values


def fill_boards(graphs, prices, state, wallet, points, values,
                leader_chr, member_chrs, *, path_length=DEFAULT_PATH_LENGTH,
                song_mode=False, singer_chars=frozenset(),
                interactive_nodes=frozenset(), score_fn=None,
                socket_scope=None, node_ok=None):
    """Top every character's LEFTOVER points after the beam, per its ROLE:
    the LEADER with RED, each MEMBER with BLUE, GREEN everywhere, and YELLOW where
    it pays. The beam (with its lookahead) crosses the red corridor; this greedy
    fill spends whatever budget the depth cap left — without it, blue is starved
    (thousands of Blue Cube left unspent at depth 12). Values are precomputed by
    `_branch_values`, so no oracle call in the loop.

    YELLOW placement, mirroring `_move_in_scope`: GENERAL mode → the BENCH only
    (the capped account-global Y the bench supplies for free, never a team board's
    red/blue-worthy points); SONG mode → the chart's SINGERS only (team or bench),
    since only a singer's yellow contributes to the song score.

    Two phases. **Phase 1** (this pass) fills every non-interactive node on its
    precomputed isolated value — sound because stat/global nodes stack roughly
    linearly. **Phase 2** fills the ACTIVE-uptime nodes (`interactive_nodes`), whose
    value is NOT additive (per-tick max, saturating against each other, the stat
    blues and the SPECIAL cues), so it re-scores each one IN CONTEXT via `score_fn`
    and buys only a real positive marginal, since an isolated value can read
    thousands for a node worth 0 on the built board. Phase 2 is skipped without
    a `score_fn`."""
    state = {c: set(n) for c, n in state.items()}
    wallet = dict(wallet)
    points = dict(points)
    member_set = set(member_chrs)
    bought = 0

    # SOCKETS FIRST, on the team's boards. An empty Connect socket scores 0 for the
    # tree (it boosts nothing until a card is placed), so the value-driven fill never
    # buys one — but the socket optimiser can only place a card where a socket EXISTS,
    # so an unbought S-002/3/4 is a placement it cannot make (an empty
    # socket). They cost ~1 point + a short path on an already-filled board and hold
    # a card worth thousands, so buy them BEFORE green claims the leftover points.
    #
    # But ONLY the sockets whose shape can PAY on this holder (`socket_scope`): a
    # red-region socket (S-002) is dead on a member, and reaching it burns ~10 points
    # of red corridor for a card that never lands. `None` keeps every socket, for
    # callers that do not pass a scope.
    for cid in sorted(({leader_chr} | member_set) & set(graphs)):
        graph = graphs[cid]
        allowed = None if socket_scope is None else socket_scope.get(cid, {ROOT})
        while True:
            have = state.get(cid, {ROOT})
            best = None  # cheapest reachable unbought socket: (path, price)
            for target, path in cheapest_paths(
                    have, graph, prices, max_nodes=path_length,
                    allowed=None if node_ok is None else (lambda n, c=cid: node_ok(c, n))).items():
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
                    have, graph, prices, max_nodes=path_length,
                    allowed=None if node_ok is None else (lambda n, c=cid: node_ok(c, n))).items():
                # Interactive nodes wait for phase 2 (their isolated value lies).
                if (target[0] not in scope or target in PER_CHARACTER_GREEN
                        or target in interactive_nodes):
                    continue
                price = {"points": 0}
                for node in path:
                    for key, qty in prices.get(node, {}).items():
                        price[key] = price.get(key, 0) + qty
                if not _affordable(price, wallet, points, cid):
                    continue
                # A path may still PASS THROUGH an interactive prereq; it just does
                # not count toward the value (its isolated number would inflate it).
                value = sum(values.get(n, 0.0) for n in path
                            if n[0] in scope and n not in interactive_nodes)
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

    # Phase 2: nodes whose value is CONTEXTUAL, not additive, so their isolated
    # marginal lies and they must be re-scored on the ACTUAL board via `score_fn`:
    #   - ACTIVE-uptime (`interactive_nodes`): the active Score UP is a per-tick MAX,
    #     so they saturate against each other / the stat blues / the SPECIAL cues.
    #     Member-scoped, so only the team's own boards can move the score.
    #   - GEN-GREENS (`PER_CHARACTER_GREEN`, G-008/011/021): "+stats to the holder's
    #     whole GENERATION", so they pay on ANY character sharing a team gen (bench
    #     included, on otherwise-idle points) and STACK across same-gen boards (buy
    #     it on Kiara to buff a Calli team). G-008 is bought
    #     incidentally as a green-corridor prereq; G-011/G-021 (the big +225/+300 and
    #     +150/+200 rows) are what this reaches.
    # Cost-bounded to ~2 score_fn calls per candidate: score each once against the
    # filled board, then buy in value order, RE-VALIDATING each against the updated
    # board just before it is bought. The re-validation is what catches saturation —
    # a node that a prior buy made redundant re-scores to <=0 and is skipped — so
    # this stays correct without an N-iterations full re-score.
    if score_fn is not None and (interactive_nodes or PER_CHARACTER_GREEN):
        team = ({leader_chr} | member_set) & set(graphs)
        base_score = score_fn(state)
        candidates = []  # (per_pt, cid, path, price)
        for cid, graph in graphs.items():
            on_team = cid in team
            have = state.get(cid, {ROOT})
            for target, path in cheapest_paths(
                    have, graph, prices, max_nodes=path_length,
                    allowed=None if node_ok is None else (lambda n, c=cid: node_ok(c, n))).items():
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

    So the budget is the inventory PLUS everything already spent, the points
    are each character's FULL grant rather than what is left, and the starting
    board is bare. That turns the question from "what do I add" into "what is
    the best board my total resources can buy", which is a different and
    strictly larger problem.

    ⛔ ONLY S-001 starts owned — the free root at (0,0). **S-002/003/004 do NOT:
    they cost 1 point EACH and need a path to S-001**, exactly like any node.
    (Keeping all four sockets as free connected roots would let the beam ground
    a whole R-cluster on S-002 — which sits at (0,-7), adjacent to the R
    corridor — WITHOUT buying the (0,-1)..(0,-6) corridor that connects S-002 to
    the root, leaving a DISCONNECTED board. The card PLACEMENTS still ride along
    via `state_from`'s slots; a card on an unbought socket simply grants
    nothing, as in the game.)"""
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

    `SystemExit` on a contradiction rather than a silent winner, because a board
    plan costed on the wrong point budget ranks different nodes."""
    if at_max_flag and real_flag:
        raise SystemExit("--at-max-level and --real-char-level are opposites; pick one")
    if scenario == "full-collection-maxed":
        if real_flag:
            raise SystemExit(
                "--real-char-level is incompatible with --scenario "
                "full-collection-maxed: a fully maxed account is at level 50"
            )
        return True
    return at_max_flag or (scenario == "my-cards-maxed" and not real_flag)


def cmd_plan(args) -> None:
    """Beam search over purchase SEQUENCES, respecting adjacency and every budget.

    ⚠ Beam, not optimal, and for a reason: this objective is SUPERMODULAR in
    nearly every sampled trial, so no (1-1/e) greedy guarantee exists, and a
    lone cooldown node can be worth exactly 0 while two crossing a boundary are
    worth +25 %. A width-1 greedy would refuse the first of such a pair forever.
    The beam keeps zero-delta moves alive for exactly that reason -- they are
    not pruned, they are carried."""
    prices = _prices()
    owned_nodes = _owned_nodes()
    points = _character_points()
    balance, source = (parse_balance(args.balance), "--balance")
    if balance is None:
        balance, source = balance_from_disk()
    if balance is None:
        raise SystemExit("no cube/hexa balance available; pass --balance")

    # The CHARACTER-LEVEL (board-point) assumption follows the collection scenario:
    # the maxed collections stand on a level-50 account
    # (361 points), the account scenario on the real levels. account and
    # my-cards-maxed honour an explicit override toggle; full-collection-maxed is a
    # fully maxed account, so level 50 is not optional there.
    #   account               real levels   (--at-max-level lifts it to 361)
    #   my-cards-maxed         361           (--real-char-level drops it to real)
    #   full-collection-maxed  361           (forced) + unlimited materials
    # --at-max-level / --infinite remain the low-level knobs.
    at_max_level = resolve_at_max_level(
        args.scenario, args.at_max_level, args.real_char_level
    )

    start_nodes = {cid: list(groups) for cid, groups in owned_nodes.items()}
    if args.respec:
        balance, points, start_nodes, levels = respec_budget(owned_nodes, prices, balance)
        source = f"{source} + refunded spend"
        budget_desc = "FULL RESPEC -- reset refunds, so every node is re-decidable"
        if at_max_level:
            points = {cid: 361 for cid in points}
            budget_desc += f", every character at level {MAX_CHARACTER_LEVEL}"
        else:
            budget_desc += f" (levels {min(levels.values())}-{max(levels.values())} as they are)"
    elif at_max_level:
        points = points_at_max(owned_nodes, prices)
        budget_desc = f"character level {MAX_CHARACTER_LEVEL} (points regranted, spend deducted)"
    else:
        budget_desc = "this account as it stands"
    # full-collection-maxed is not cube/hexa limited -- a complete account holds the
    # materials, and at 361 points every score node is already affordable, so a real
    # inventory would be the only (accidental) limit. Unlimit materials to make the
    # scenario mean what it says. Points stay 361 (the honest level-50 grant) rather
    # than INFINITE. Explicit --infinite still overrides points too.
    if args.scenario == "full-collection-maxed":
        balance = {item: INFINITE for item in CURRENCIES}
        source = "IGNORED (full collection = unlimited materials)"
        budget_desc += "; materials unlimited"
    if args.infinite_materials:
        # Materials axis only: unlimited cubes/hexas, points UNTOUCHED (real, or 361
        # under --at-max-level). This is 'infinite resources' as the two orthogonal
        # web toggles mean it — distinct from --infinite, which also lifts points.
        balance = {item: INFINITE for item in CURRENCIES}
        source = "IGNORED (materials unlimited; points from character level)"
        budget_desc += "; materials unlimited"
    if args.infinite:
        balance = {item: INFINITE for item in CURRENCIES}
        points = {cid: INFINITE for cid in owned_nodes}
        source = "IGNORED"
        budget_desc = "UNLIMITED resources -- a ceiling, not a plan"
    print(f"scenario: {args.scenario} | budget: {budget_desc}", file=sys.stderr)

    oracle = Oracle(pool_size=args.pool, song=args.song, charts_path=args.charts,
                    scenario=args.scenario)
    graphs = _graphs(oracle.skilltree)
    # `current` is always the board as it stands -- the number any plan has to
    # beat. `base` is where THIS run starts, which for a respec is the bare
    # board. Reporting a respec's progress against `current` would show every
    # early purchase as a catastrophic loss, because it is still digging out of
    # the refund.
    oracle.prime()
    # ⚑ TEAM-FIRST: pick the SINGLE best team, then optimise the boards FOR THAT
    # TEAM. Scoring the POOL maximum (`oracle.score`) would re-pick the leader on
    # every move — so a RED (leader-scoped) node on the intended leader would read
    # as worth 0 the instant the dynamic best team was led by someone else, and no
    # red would ever be bought. So score the FIXED best team with `score_team`, so
    # red pays on its leader and blue on its five.
    # `--for-team "leader,m1..m5"` overrides the pool best with a GIVEN team — this
    # is how the web serves the board plan for the team the SOLVE actually picked
    # (which depends on the board toggle / basis and can differ from the plan's own
    # best). Without it, the plan optimises for _teams[0], which may not be the
    # ranking's winner.
    if getattr(args, "for_team", None):
        ids = [s.strip() for s in args.for_team.split(",") if s.strip()]
        if len(ids) != MEMBER_COUNT + 1:
            raise SystemExit(
                f"--for-team needs {MEMBER_COUNT + 1} card ids "
                f"(leader,m1..m{MEMBER_COUNT}); got {len(ids)}"
            )
        missing = [c for c in ids if c not in oracle._by_id]
        if missing:
            raise SystemExit(f"--for-team: unknown card id(s): {missing}")
        leader_id, member_ids = ids[0], tuple(ids[1:])
        print(f"planning for the GIVEN team (--for-team): leader {leader_id}",
              file=sys.stderr)
    else:
        leader_id, member_ids = oracle._teams[0]
    team_chars = _team_characters(leader_id, member_ids)
    # The chart's cast (chr-ids) — only these characters' YELLOW pays in song mode
    # (`song_score_bonus_for`); empty in the song-agnostic ranking.
    singer_chars = frozenset(oracle._chart.singers) if oracle._chart else frozenset()

    def team_score(node_dict):
        return oracle.score_team(
            leader_id, member_ids,
            oracle.state_from({c: sorted(n) for c, n in node_dict.items()}),
        )

    current = team_score(owned_nodes)
    base = current
    if args.respec:
        base = team_score(start_nodes)
    print(f"team: leader {leader_id}  members {list(member_ids)}", file=sys.stderr)
    print(
        f"current board {current:,.1f}   this run starts at {base:,.1f}   wallet from {source}",
        file=sys.stderr,
    )

    # RED/BLUE pay only on the fixed team's six characters; GREEN/YELLOW are
    # account-global and searched on every holomem (`_move_in_scope` + the global
    # fill). B/R are 0 off-team; G/Y pay the same from ANY board.
    scoped = set(args.characters.split(",")) if args.characters else None
    pool_characters = set(team_chars)
    print(
        f"B/R on the {len(pool_characters)} team character(s); "
        f"G/Y searched on all {len(owned_nodes)}",
        file=sys.stderr,
    )

    # A plan is (score, purchases, wallet, points, owned).
    start = (
        base,
        (),
        dict(balance),
        dict(points),
        {c: set(n) for c, n in start_nodes.items()},
    )
    beam = [start]
    unroutable: dict[str, str] = {}
    step = 0
    while step < args.depth:
        step += 1
        successors = []
        for score, purchases, wallet, pts, owned in beam:
            moves = []
            for cid, have in owned.items():
                if scoped and cid not in scoped:
                    continue
                graph = graphs.get(cid)
                if graph is None:
                    continue
                on_team = cid in pool_characters
                reachable = cheapest_paths(
                    have, graph, prices, max_nodes=args.path_length
                )
                for target, path in reachable.items():
                    if target.startswith("S-"):
                        continue
                    # B and R reach only this character's own cards, so off a
                    # pooled team they are exactly 0 -- not a heuristic, and
                    # skipping them is what keeps the search tractable across
                    # 34 boards. G and Y are account-global and always in.
                    # `_move_in_scope` is that rule, shared with `_plan_for_team`.
                    if not _move_in_scope(target, on_team, song_mode=bool(args.song),
                                          is_singer=cid in singer_chars):
                        continue
                    total = {"points": 0}
                    for node in path:
                        for key, qty in prices[node].items():
                            total[key] = total.get(key, 0) + qty
                    if _affordable(total, wallet, pts, cid):
                        moves.append((cid, path, total))
            # ⛔ DEDUP is NOT the model of how many globals to buy. A G/Y node
            # gives the same delta on every character, but each character's G/Y
            # contribute INDEPENDENTLY to the account score, i.e. they STACK
            # (removing greens from one character and then a second loses more
            # each time). So the right move is to fill EVERY character's points
            # with G/Y, not to collapse them to a single holder. `fill_globals()`
            # (applied after the beam) does that per character; this dedup only
            # stops the beam itself thrashing on 34 identical first-copies.
            # G-008/G-011/G-021 are EXCLUDED -- they target the holder's own
            # grouping and are genuinely per-character.
            best_global = {}
            rest = []
            for cid, path, price in moves:
                target = path[-1]
                if target[0] in ("G", "Y") and target not in PER_CHARACTER_GREEN:
                    key = (target, len(path))
                    seen = best_global.get(key)
                    if seen is None or pts.get(cid, 0) > pts.get(seen[0], 0):
                        best_global[key] = (cid, path, price)
                else:
                    rest.append((cid, path, price))
            moves = rest + list(best_global.values())
            # ⛔ LOOKAHEAD, and it is not an optimisation -- without it this
            # search is WRONG. From a bare board, Ina's frontier after
            # R-001+R-004 is {B-001, G-001, R-002, Y-001}. R-003 and R-005 are
            # worth thousands EACH and both sit behind R-002, which is worth
            # **0.0**. A beam ranking by immediate delta prefers a cheap blue
            # node forever and never opens the red branch.
            #
            # Carrying zero-delta moves into the successor list is not enough:
            # sorting would drop them again. Carrying is not surviving.
            #
            # So a move worth nothing on its own is re-scored by the best
            # single follow-up it UNLOCKS. Only zero-ish moves pay for the
            # extra evaluations, which is what keeps this affordable.
            scored = []
            for cid, path, price in moves:
                if any(node in unroutable for node in path):
                    continue
                state = {c: set(n) for c, n in owned.items()}
                state[cid].update(path)
                as_dict = {c: sorted(n) for c, n in state.items()}
                try:
                    new_score = team_score(as_dict)
                except BoardError as exc:
                    # `board.py` REFUSES an effect family it does not model rather
                    # than scoring it as zero — a reportable exclusion, not a
                    # silent one. Such a node is unplannable until the family is
                    # modelled: NOT worthless, unmeasured, and those must not look
                    # alike.
                    # ⛔ Mark ONLY the TARGET (`path[-1]`), NOT the whole path.
                    # This move buys a CHAIN; marking every node in it would
                    # poison the shared PREFIX — a side-branch unrouted node
                    # whose path runs down the red corridor would mark
                    # R-001/R-002… unroutable and kill every later red buy. An
                    # intermediate that is itself the raiser is caught by its
                    # OWN shorter path.
                    unroutable.setdefault(
                        path[-1], str(exc).split("'")[1] if "'" in str(exc) else "?"
                    )
                    continue
                new_wallet, new_points = _spend(price, wallet, pts, cid)
                scored.append((new_score, cid, path, price, state, new_wallet, new_points))

            # ⛔ THE LOOKAHEAD IS OFF UNDER PATH MOVES; the two mechanisms
            # fight each other. A path already CONTAINS its payoff, so
            # re-scoring a move by what it might unlock double-counts: a 1-point
            # `Y-001` gate whose lookahead promised value would rank top on
            # efficiency, be COMMITTED for 0.0, and then another cheap gate on
            # another character would out-rank the payoff — forever.
            #
            # With paths, a gate-only move ends at a worthless node, scores 0,
            # and the strict-improvement rule drops it. The gate survives only
            # as the PREFIX of a path that reaches something.
            floor = args.lookahead_below
            for new_score, cid, path, price, state, new_wallet, new_points in scored:
                key = new_score
                if args.lookahead and len(path) == 1 and new_score - score <= floor:
                    graph = graphs.get(cid)
                    best_follow = new_score
                    for nxt in frontier(state[cid], graph or {}):
                        follow_price = prices.get(nxt)
                        if follow_price is None or nxt.startswith("S-") or nxt in unroutable:
                            continue
                        if not _affordable(follow_price, new_wallet, new_points, cid):
                            continue
                        probe = {c: set(n) for c, n in state.items()}
                        probe[cid].add(nxt)
                        try:
                            value = team_score(probe)
                        except BoardError:
                            continue
                        best_follow = max(best_follow, value)
                    # Ranked by what it unlocks, COMMITTED as itself. The
                    # follow-up is re-decided next step like any other move.
                    key = best_follow
                # ⛔ STRICTLY IMPROVING ONLY, counting what the move unlocks.
                # Without this, most purchases in a respec run have delta
                # EXACTLY 0.0 — red nodes on characters that do not lead (red is
                # leader-scoped) — and they drain Red Cube and, worse, consume
                # the depth budget.
                #
                # A zero-value move that UNLOCKS value still survives: the
                # lookahead already raised its key above `score`. What is
                # dropped is the move that is worth nothing and leads nowhere
                # within one step.
                #
                # ⚠ Bounded and stated: a node worth 0 whose payoff is TWO
                # steps away is dropped. --lookahead is one-step. The known
                # case (R-002 unlocking R-003/R-005) is one step, so the
                # shipped depth covers the case that is known to exist, not
                # every case that could.
                if key <= score:
                    continue
                # ⛔ RANK BY EFFICIENCY, NOT BY RESULTING SCORE.
                # Under path moves ranking-by-score is actively harmful: a
                # bundle of 9 nodes always scores above a single node, so the
                # beam would buy bundles regardless of what they cost (e.g.
                # `G-001..G-016`, nine nodes and 700 Green Cube, for little
                # more than a lone `G-001` pays for 20).
                #
                # The denominator is the largest FRACTION OF WHAT REMAINS that
                # the move consumes, across points and all eight currencies —
                # the standard multi-dimensional knapsack greedy. A raw
                # per-point ratio would be wrong here because the branches bind
                # on different currencies: red on the leader's points, green on
                # cubes across every board.
                share = 0.0
                for currency, qty in price.items():
                    if not qty:
                        continue
                    left = pts.get(cid, 0) if currency == "points" else wallet.get(currency, 0)
                    share = max(share, qty / left if left else 1.0)
                efficiency = (key - score) / share if share else (key - score)
                successors.append(
                    (
                        new_score,
                        purchases + ((cid, path, new_score - score),),
                        new_wallet,
                        new_points,
                        state,
                        efficiency,
                    )
                )
        if not successors:
            print(
                f"step {step}: CONVERGED -- no affordable move improves the score, "
                "so the plan stops here rather than spending budget on nothing",
                file=sys.stderr,
            )
            break
        # Sort by EFFICIENCY (slot 5), keep the state's own score in slot 0.
        successors.sort(key=lambda s: -s[5])
        beam = [tuple(s[:5]) for s in successors[: args.beam]]
        print(
            f"step {step}: {len(successors)} moves, best {beam[0][0]:,.1f} "
            f"(+{beam[0][0] - base:,.1f} cumulative)",
            file=sys.stderr,
        )

    best = beam[0]

    # ROLE-AWARE FILL. The beam (with its lookahead) crosses the red corridor;
    # then spend every character's LEFTOVER points by ROLE — RED on the leader,
    # BLUE on each member, global G/Y on all — because the depth cap starves the
    # branches the beam did not reach (blue would leave thousands of cube unspent
    # at depth 12; the global G/Y stack and want every idle point).
    leader_chr = "chr-" + leader_id.split("-")[1]
    member_chrs = ["chr-" + m.split("-")[1] for m in member_ids]
    values = _branch_values(oracle, graphs, prices, leader_id, member_ids,
                            path_length=args.path_length, singer_chars=singer_chars)
    interactive_nodes = _interactive_node_ids(oracle.skilltree)
    # Which sockets are worth buying on each team board: a red-region socket pays
    # only on the leader, a yellow-region one only on a singer/in song mode — so the
    # fill never burns a corridor reaching a socket no card can use on that holder.
    socket_scope = _payable_sockets(
        oracle.skilltree, leader_chr, member_chrs,
        song_mode=bool(args.song), singer_chars=singer_chars)
    filled, fwallet, fpoints, nfilled = fill_boards(
        graphs, prices, best[4], best[2], best[3], values,
        leader_chr, member_chrs, path_length=args.path_length,
        song_mode=bool(args.song), singer_chars=singer_chars,
        interactive_nodes=interactive_nodes, score_fn=team_score,
        socket_scope=socket_scope
    )
    fscore = team_score(filled)
    yellow_where = "singers" if args.song else "the bench"
    print(f"role fill: +{nfilled} node(s) (red on leader, blue on members, green "
          f"everywhere, yellow on {yellow_where}) -> {fscore:,.1f} "
          f"(+{fscore - best[0]:,.1f})", file=sys.stderr)
    best = (fscore, best[1], fwallet, fpoints, filled)

    print()
    print(f"PLAN: {len(best[1])} team purchases + {nfilled} global fills, "
          f"{best[0]:,.1f} from {base:,.1f} "
          f"({(best[0] / base - 1) * 100:+.2f} % vs this run's start)")
    if args.respec:
        verdict = "BEATS" if best[0] > current else "does NOT beat"
        print(
            f"  vs the board you have now ({current:,.1f}): "
            f"{(best[0] / current - 1) * 100:+.2f} % -- {verdict} it"
        )
        if best[0] <= current:
            print("  ⚠ so this run has not yet rebuilt what the refund gave up. Raise --depth.")
    print()
    print(f"{'#':>3}  {'character':12s} {'path':28s} {'delta':>10s}  price")
    for index, (cid, path, delta) in enumerate(best[1], 1):
        total = {"points": 0}
        for node in path:
            for key, qty in prices[node].items():
                total[key] = total.get(key, 0) + qty
        items = "  ".join(
            f"{PRETTY[i]} {q}" for i, q in total.items() if i != "points" and q
        )
        shown = path[-1] if len(path) == 1 else f"{path[0]}..{path[-1]} ({len(path)})"
        print(f"{index:>3}  {cid:12s} {shown:28s} {delta:>10,.1f}  {total['points']} pt  {items}")
    if unroutable:
        families = sorted(set(unroutable.values()))
        print()
        print(f"⚠ {len(unroutable)} node(s) EXCLUDED, not evaluated: board.py refuses their")
        print(f"  effect family ({', '.join(families)}). They are unmeasured, not worthless.")
        print("  Model the family in board.py to bring them into the plan.")
    if args.dump_state:
        # The printed table abbreviates long paths (`R-040..R-043 (4)`), which
        # is readable and NOT reconstructable. A plan that cannot be re-scored
        # from its own output cannot be verified, so the exact final board is
        # written out on request.
        #
        # ⛔ **In `board_state.json`'s OWN shape**,
        # `{chr: {"nodes": [...], "slots": {...}}}` -- the shape the only tool
        # that turns a board state into something scoreable (`build_board.py
        # --state`) reads; a bare `{chr: [groups]}` would make it fail.
        # `state_from` is the same function the planner scores through, which
        # also keeps the SLOTS: dropping them would drop the Connect
        # multipliers, and a "verification" scoring a different board than the
        # plan did is worse than none.
        final = oracle.state_from({c: sorted(n) for c, n in best[4].items()})
        Path(args.dump_state).write_text(json.dumps(final, indent=1), encoding="utf-8")
        print(f"final board written to {args.dump_state}")
        print(f"  verify it:  build_board.py --state {args.dump_state} --out board_plan.json")
    print()
    print("wallet left:")
    for item in CURRENCIES:
        print(f"  {PRETTY[item]:14s} {best[2].get(item, 0):>8,}")


def _team_characters(leader_id: str, member_ids) -> set[str]:
    return {"chr-" + card.split("-")[1] for card in (leader_id, *member_ids)}


def _plan_for_team(oracle, graphs, prices, leader_id, member_ids, wallet, points, args,
                   start=None, all_invited=False):
    """Greedy path-move planner for ONE team's board.

    Returns (score before, score after, purchases). Same move generator and
    same efficiency ranking as `cmd_plan` -- the only difference is that the
    objective is THIS team rather than the pool maximum.

    `start` is the board the plan builds ON. Default is the account's current
    board (`owned_nodes`); a `--respec`/`--team` caller passes the BARE board so
    the plan is "what is the best board my refunded total buys", not "what do I
    add". Sockets are kept in a bare start (they carry the Connect placements
    and cost nothing) exactly as `respec_budget` produces them.

    Scope. By default the search is the team's SIX characters only -- their red,
    blue, green and yellow branches. With `all_invited=True` (the ceiling's "all
    invited" toggle) the account-global GREEN/YELLOW branches are searched on
    EVERY holomem with a board, because those pay from any board; RED/BLUE
    stay on the six (`_move_in_scope`). The account-global G/Y are then
    DEDUPED across holders -- one is scored, not 34, keeping the
    cheapest-in-points holder -- exactly as `cmd_plan` does.
    `all_invited=False` searches only the six with no dedup, so `--team` /
    `--respec` plans keep the same move set and order."""
    team_chars = _team_characters(leader_id, member_ids)
    singer_chars = frozenset(oracle._chart.singers) if oracle._chart else frozenset()
    # The universe the account-global branches are searched on. Only widened when
    # all_invited is set; otherwise it is the six and every move is on-team.
    search_chars = set(graphs) if all_invited else set(team_chars)
    base = start if start is not None else oracle.owned_nodes
    state = {c: set(n) for c, n in base.items()}
    # A searched character the account has never boarded (a new holomem, and
    # most of the roster under all_invited) has no start entry;
    # give it a BARE board so the planner can buy onto it from the root.
    # `cheapest_paths` is root-seeded, so an empty set is a legal start.
    for cid in search_chars | team_chars:
        state.setdefault(cid, set())
    wallet, points = dict(wallet), dict(points)

    def score_of(candidate):
        return oracle.score_team(
            leader_id,
            member_ids,
            oracle.state_from({c: sorted(n) for c, n in candidate.items()}),
        )

    before = score_of(state)
    current = before
    purchases = []
    for _step in range(args.depth):
        # Collect every affordable, in-scope candidate move, in the same
        # (sorted character, path) order every time, so the non-invited path is
        # deterministic.
        candidates = []  # (cid, path, cost)
        for cid in sorted(search_chars):
            graph = graphs.get(cid)
            if graph is None:
                continue
            on_team = cid in team_chars
            for target, path in cheapest_paths(
                state[cid], graph, prices, max_nodes=args.path_length
            ).items():
                if target.startswith("S-"):
                    continue
                if not _move_in_scope(target, on_team, song_mode=bool(args.song),
                                      is_singer=cid in singer_chars):
                    continue
                cost = {"points": 0}
                for node in path:
                    for key, qty in prices[node].items():
                        cost[key] = cost.get(key, 0) + qty
                if not _affordable(cost, wallet, points, cid):
                    continue
                candidates.append((cid, path, cost))
        # DEDUP the account-global G/Y (all_invited only): a G or Y node gives the
        # IDENTICAL delta on every holder that lacks it, so evaluating one per
        # character is 34x the work for one answer. Keep the holder with the MOST
        # points left (cheapest relative share), exactly as `cmd_plan`.
        # PER_CHARACTER_GREEN target the holder's own grouping and are NOT deduped.
        if all_invited:
            best_global = {}
            rest = []
            for cid, path, cost in candidates:
                target = path[-1]
                if target[0] in ("G", "Y") and target not in PER_CHARACTER_GREEN:
                    key = (target, len(path))
                    seen = best_global.get(key)
                    if seen is None or points.get(cid, 0) > points.get(seen[0], 0):
                        best_global[key] = (cid, path, cost)
                else:
                    rest.append((cid, path, cost))
            candidates = rest + list(best_global.values())

        best = None
        for cid, path, cost in candidates:
            probe = {c: set(n) for c, n in state.items()}
            probe[cid].update(path)
            try:
                value = score_of(probe)
            except BoardError:
                continue
            if value <= current:
                continue
            # Same multi-dimensional knapsack ratio as `cmd_plan`: the
            # largest share of what REMAINS that this move consumes.
            share = 0.0
            for currency, qty in cost.items():
                if not qty:
                    continue
                left = points.get(cid, 0) if currency == "points" else wallet.get(currency, 0)
                share = max(share, qty / left if left else 1.0)
            efficiency = (value - current) / share if share else (value - current)
            if best is None or efficiency > best[0]:
                best = (efficiency, value, cid, path, cost, probe)
        if best is None:
            break
        _eff, value, cid, path, cost, probe = best
        state = probe
        wallet, points = _spend(cost, wallet, points, cid)
        label = path[0] if len(path) == 1 else f"{path[0]}..{path[-1]}({len(path)})"
        purchases.append((cid, label, value - current))
        current = value
    return before, current, purchases


def cmd_per_team(args) -> None:
    """Optimise a board FOR EACH candidate team, then rank on the result.

    Optimising the board against a team pool that was itself chosen UNDER THE
    CURRENT BOARD means a team that is mediocre today but superb once its
    members' boards are bought can never surface. Planning per result team (as
    the community tool holodori.best does: "Find the best nodes to buy and
    reset **for each result team**") is the tractable shape of the joint
    problem.

    Each team gets the same path-move planner, restricted to its own six
    characters and scored on ITSELF rather than on the pool maximum.

    WARNING, and it is the one way to misread this table: the budget is spent
    independently per team, so the rows are ALTERNATIVES, not a shopping list.
    You can build one of them, not all. That is exactly the question being asked
    -- "which team should I build toward" -- but a reader who sums the column
    would be wrong.
    """
    prices = _prices()
    owned_nodes = _owned_nodes()
    points = _character_points()
    balance, source = (parse_balance(args.balance), "--balance")
    if balance is None:
        balance, source = balance_from_disk()
    if balance is None:
        raise SystemExit("no cube/hexa balance available; pass --balance")
    if args.at_max_level:
        points = points_at_max(owned_nodes, prices)

    oracle = Oracle(pool_size=args.pool, song=args.song, charts_path=args.charts,
                    scenario=args.scenario)
    oracle.prime()
    graphs = _graphs(oracle.skilltree)
    print(f"wallet from {source}", file=sys.stderr)

    results = []
    candidates = oracle._teams[: args.teams]
    for index, (leader_id, member_ids) in enumerate(candidates, 1):
        before, after, purchases = _plan_for_team(
            oracle, graphs, prices, leader_id, member_ids, balance, points, args
        )
        results.append((after, before, leader_id, member_ids, purchases))
        print(
            f"  team {index}/{len(candidates)}: {before:,.1f} -> {after:,.1f} "
            f"({(after / before - 1) * 100:+.2f} %, {len(purchases)} purchases)",
            file=sys.stderr,
        )

    ranked_now = sorted(results, key=lambda r: -r[1])
    results.sort(key=lambda r: -r[0])
    moved = [
        r[2] for r in results if results.index(r) != ranked_now.index(r)
    ]

    print()
    print("RANKED ON (team + the board it earns).")
    print("These are ALTERNATIVES -- the budget is spent independently per team,")
    print("so you can build ONE of these rows, not the column.")
    print()
    print(f"{'#':>3} {'with its board':>15} {'today':>15} {'gain':>8}  team")
    for rank, (after, before, leader_id, member_ids, purchases) in enumerate(results, 1):
        print(
            f"{rank:>3} {after:>15,.1f} {before:>15,.1f} "
            f"{(after / before - 1) * 100:>7.2f}%  leader {leader_id}"
        )
        print(f"      {', '.join(member_ids)}")
        if purchases:
            shown = ", ".join(f"{c} {label}" for c, label, _d in purchases[:5])
            extra = f" (+{len(purchases) - 5} more)" if len(purchases) > 5 else ""
            print(f"      buys: {shown}{extra}")
    print()
    if moved:
        print(f"The board REORDERED the teams -- {len(moved)} row(s) are not in the")
        print("order the plain ranking gives. That reordering is the whole reason")
        print("this mode exists.")
    else:
        print("The board did NOT reorder these teams: the same order comes out")
        print("either way. On this account, at this budget, per-team planning")
        print("changes the SCORES and not the CHOICE.")


def cmd_team(args) -> None:
    """Plan the board for ONE NAMED team -- `--team leader,m1,m2,m3,m4,m5`.

    `--per-team` only plans for teams the pool search surfaced under the CURRENT
    board and with OWNED cards, and it ignores `--respec`. This answers a named
    question instead -- "for this team, assume the trees are empty and spend my
    inventory plus everything refunded".

    The team is scored on the MAXED catalogue basis: a team you are planning
    toward is usually one you do not own at those levels yet, so scoring it at
    your real (mostly absent) levels would answer a different question. The run
    says so.

    Budget follows the same flags as `--plan`:
      --respec        bare boards, refund every point and currency (the usual
                      pairing -- planning an unowned team ON your current board,
                      which holds other characters' nodes, rarely makes sense)
      --at-max-level  every team character's point grant at level 50 (=361),
                      the "if they were all levelled" companion to the respec
                      scenario at real levels
    """
    ids = [c.strip() for c in (args.team or "").split(",") if c.strip()]
    if len(ids) != MEMBER_COUNT + 1:
        raise SystemExit(
            f"--team needs {MEMBER_COUNT + 1} card ids (leader,m1..m{MEMBER_COUNT}), "
            f"got {len(ids)}: {ids}"
        )
    leader_id, member_ids = ids[0], tuple(ids[1:])

    prices = _prices()
    owned_nodes = _owned_nodes()
    balance, source = (parse_balance(args.balance), "--balance")
    if balance is None:
        balance, source = balance_from_disk()
    if balance is None and not args.ceiling:
        raise SystemExit("no cube/hexa balance available; pass --balance")
    if balance is None:
        balance = {}  # --ceiling ignores the wallet; a bare dict is enough

    team_chars = sorted(_team_characters(leader_id, member_ids))
    start_nodes = {cid: list(groups) for cid, groups in owned_nodes.items()}
    points = _character_points()
    if args.respec:
        balance, points, start_nodes, levels = respec_budget(owned_nodes, prices, balance)
        scenario = "FULL RESPEC -- trees bare, all points and currency refunded"
    else:
        scenario = "ADDED to the current board (no refund)"
    if args.at_max_level:
        # Every character on THIS team at the level-50 grant, including ones the
        # account has never boarded (they are absent from `points` otherwise, so
        # would plan with zero points).
        points = dict(points)
        for cid in team_chars:
            points[cid] = 361
        scenario += ", team characters at level 50 (361 pts)"
    if args.infinite:
        balance = {item: INFINITE for item in CURRENCIES}
        points = {cid: INFINITE for cid in set(points) | set(team_chars)}
        source = "IGNORED"
        scenario += " + UNLIMITED resources"

    oracle = Oracle(pool_size=1, quiet=True, maxed=True,  # no prime: the team is fixed
                    song=args.song, charts_path=args.charts)
    graphs = _graphs(oracle.skilltree)
    missing = [c for c in ids if c not in oracle._by_id]
    if missing:
        raise SystemExit(f"--team: unknown card id(s): {missing}")

    if args.ceiling:
        # The community tool holodori.best's Board Optimizer CEILING preset, so the two tools
        # compare on the same footing: bare boards (start from nothing), infinite
        # cubes/hexas (currency ignored -- POINTS are the only limit), every
        # holomem at max board points (361), and "all invited" -- G/Y searched on
        # every board (`_plan_for_team(all_invited=True)`). Overrides --respec
        # /--at-max-level/--infinite, which are its individual pieces.
        universe = set(graphs)
        balance = {item: INFINITE for item in CURRENCIES}
        points = points_at_max({cid: [] for cid in universe}, prices)
        start_nodes = {}
        source = "CEILING (wallet ignored)"
        scenario = (
            f"CEILING (holodori preset) -- bare boards, infinite cubes/hexas, "
            f"max points 361 on all {len(universe)} holomems, all invited"
        )
        # The plan spans all 54 boards and stacks the account-global green/yellow
        # branches on every one of them, so it needs hundreds of moves to
        # CONVERGE -- a low cap silently truncates the tail. The loop still stops
        # the moment no affordable move improves; this is only the backstop.
        args.depth = max(args.depth, 2000)

    print(f"scenario : {scenario}", file=sys.stderr)
    print(f"basis    : MAXED catalogue (every card at max level and bloom)", file=sys.stderr)
    print(f"wallet   : {source}", file=sys.stderr)
    print(f"team     : leader {leader_id}", file=sys.stderr)
    for m in member_ids:
        print(f"           member {m}", file=sys.stderr)

    before, after, purchases = _plan_for_team(
        oracle, graphs, prices, leader_id, member_ids, balance, points, args,
        start=start_nodes, all_invited=args.ceiling,
    )

    print()
    print(f"team score: {before:,.1f} -> {after:,.1f} "
          f"({(after / before - 1) * 100:+.2f} %), {len(purchases)} purchases")
    print()
    # Per move, so the reader can SEE where each branch landed -- the whole
    # point of a named-team respec is that R follows THIS team's leader, not
    # whoever the current board favours. `--dump-state` writes the exact board.
    print(f"{'#':>3}  {'character':<12} {'move':<22} {'gain':>12}")
    for index, (cid, label, delta) in enumerate(purchases, 1):
        print(f"{index:>3}  {cid:<12} {label:<22} {delta:>+12,.1f}")

    final = _replay(start_nodes, graphs, prices, purchases, args)
    red_by_char = {c: sum(1 for n in nodes if n.startswith("R-"))
                   for c, nodes in final.items()}
    red_by_char = {c: n for c, n in red_by_char.items() if n}
    print(f"\nRED nodes by character: {red_by_char or 'none'}")

    if args.dump_state:
        Path(args.dump_state).write_text(
            json.dumps(oracle.state_from(final), indent=1), encoding="utf-8"
        )
        print(f"final board written to {args.dump_state}")
        print(f"  verify it:  build_board.py --state {args.dump_state} --out board_team.json")


def _replay(start_nodes, graphs, prices, purchases, args):
    """Reconstruct the final `{chr: [nodes]}` from a purchase list.

    `_plan_for_team` reports each move as a `first..last(n)` LABEL, which is not
    reconstructable, so the exact path is recomputed here the same way the
    planner chose it -- `cheapest_paths` from the state as it stood when the move
    was made. Deterministic and identical to what was scored, because the price
    graph and the adjacency are fixed."""
    state = {c: set(n) for c, n in start_nodes.items()}
    for cid, label, _delta in purchases:
        graph = graphs.get(cid)
        if graph is None:
            continue
        # A team character absent from the start board (never boarded on this
        # account) has no entry; a bare set is its legal starting state, the same
        # default `_plan_for_team` gives it.
        state.setdefault(cid, set())
        target = label.split("..")[-1].split("(")[0] if ".." in label else label
        paths = cheapest_paths(state[cid], graph, prices, max_nodes=args.path_length)
        path = paths.get(target)
        if path:
            state[cid].update(path)
    return {c: sorted(n) for c, n in state.items()}


def cmd_curve(args) -> None:
    """What each BRANCH pays, against how many POINTS are sunk into it.

    A better shape than a global search: the trees are the same 152 nodes at
    the same prices for every character, so a per-branch investment curve is
    measured ONCE and read off for any character. What the search has to decide
    afterwards is only the ROLE each character plays, and the roles partition
    the value cleanly:

        R  pays on the LEADER only          B  pays on that character's own cards
        G  pays from ANY board              Y  pays from ANY board (per-song part unmodelled)

    ⚠ **The four LAYOUTS are not interchangeable**, though they look alike.
    Same 152 nodes and same costs -- but the four models carry
    different coordinates, so under the game's 4-neighbour adjacency the same
    point budget reaches different nodes (171 edges each -- a different 171).
    Cost transfers between characters; REACH does not. `--model` selects which.

    Each step buys the frontier node inside the branch that maximises the score,
    with the same one-step lookahead the planner uses, so a zero-value gate node
    is not what stops the curve."""
    prices = _prices()
    oracle = Oracle(pool_size=args.pool, song=args.song, charts_path=args.charts,
                    scenario=args.scenario)
    graphs = _graphs(oracle.skilltree)
    owned = oracle.owned_nodes
    # Only S-001 is the free root; S-002/003/004 cost a point + a path (see
    # respec_budget's note on disconnected boards).
    bare = {c: [ROOT] for c in owned}
    base = oracle.score(oracle.state_from(bare))
    print(f"bare board baseline {base:,.1f}", file=sys.stderr)

    # One representative character per ROLE, chosen from the pool so the branch
    # can actually pay: the leader for R, a member for B, anyone for G/Y.
    leader_id, member_ids = oracle._teams[0]
    leader = "chr-" + leader_id.split("-")[1]
    member = next(
        c for c in ("chr-" + m.split("-")[1] for m in member_ids) if c != leader
    )
    offteam = next(c for c in sorted(owned) if c not in oracle.pool_characters())
    roles = [("R (leader)", leader, "R"), ("B (member)", member, "B"),
             ("G (any board)", offteam, "G"), ("Y (any board)", offteam, "Y")]

    for label, cid, branch in roles:
        graph = graphs[cid]
        have = set(bare[cid])
        spent_points = 0
        spent_cube = 0
        previous = base
        print()
        print(f"=== {label}   on {cid}   model {oracle.skilltree_model(cid)}")
        print(f"{'pts':>4} {'cube':>6} {'value':>12} {'per point':>11}  node")
        for _ in range(args.steps):
            candidates = [
                n for n in frontier(have, graph)
                if n.startswith(branch + "-") and n in prices
            ]
            if not candidates:
                break
            best = None
            for node in sorted(candidates):
                probe = dict(bare)
                probe[cid] = sorted(have | {node})
                try:
                    value = oracle.score(oracle.state_from(probe))
                except BoardError:
                    continue
                # One-step lookahead so a zero-value gate does not end the curve.
                key = value
                if value - previous <= 1.0:
                    ahead = set(have) | {node}
                    for nxt in frontier(ahead, graph):
                        if not nxt.startswith(branch + "-") or nxt not in prices:
                            continue
                        probe2 = dict(bare)
                        probe2[cid] = sorted(ahead | {nxt})
                        try:
                            key = max(key, oracle.score(oracle.state_from(probe2)))
                        except BoardError:
                            continue
                if best is None or key > best[0]:
                    best = (key, node, value)
            if best is None:
                break
            _key, node, value = best
            have.add(node)
            price = prices[node]
            spent_points += price["points"]
            cube = next((q for i, q in price.items() if i.endswith("-1")), 0)
            spent_cube += cube
            gained = value - base
            print(
                f"{spent_points:>4} {spent_cube:>6} {gained:>12,.1f} "
                f"{gained / spent_points if spent_points else 0:>11,.1f}  {node}"
            )
            previous = value


def cmd_pair(args) -> None:
    """The non-additivity demonstration on the account's own board: measure A, B and A+B."""
    oracle = Oracle(pool_size=args.pool, song=args.song, charts_path=args.charts,
                    scenario=args.scenario)
    base = oracle.base_score
    cid = args.character
    a, b = args.pair
    only_a = oracle.score(oracle.with_nodes({cid: [a]})) - base
    only_b = oracle.score(oracle.with_nodes({cid: [b]})) - base
    both = oracle.score(oracle.with_nodes({cid: [a, b]})) - base
    print(f"character {cid}   base {base:,.1f}")
    print(f"  {a:8s} alone      {only_a:>12,.1f}")
    print(f"  {b:8s} alone      {only_b:>12,.1f}")
    print(f"  both                {both:>12,.1f}")
    additive = only_a + only_b
    print(f"  sum of singles      {additive:>12,.1f}")
    gap = both - additive
    print(f"  NON-ADDITIVITY      {gap:>12,.1f}   ({gap / base * 100 if base else 0:+.3f} % of base)")
    if abs(gap) < 1e-9:
        print("  -> additive on this pair. That is one pair, not a property of the branch.")
    else:
        print("  -> NOT additive. A greedy search over single-node deltas is refuted here.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--state", action="store_true", help="owned / spent / budget")
    group.add_argument("--marginal", action="store_true", help="value of each single node")
    group.add_argument("--pair", nargs=2, metavar=("A", "B"), help="A, B and A+B together")
    group.add_argument("--plan", action="store_true", help="beam search a purchase plan")
    group.add_argument("--curve", action="store_true", help="value per branch against points sunk")
    group.add_argument("--team", default=None, metavar="L,M1..M5",
                       help="plan the board for ONE named team "
                            "(leader,m1,m2,m3,m4,m5); usually with --respec")
    group.add_argument("--per-team", action="store_true",
                       help="optimise a board FOR EACH candidate team, then rank on the result")
    parser.add_argument("--teams", type=int, default=5, help="how many teams to plan for")
    parser.add_argument("--dump-state", default=None, help="write the final board to PATH")
    parser.add_argument("--steps", type=int, default=25, help="curve length (default 25)")
    parser.add_argument("--beam", type=int, default=4, help="beam width (default 4)")
    parser.add_argument(
        "--at-max-level",
        action="store_true",
        help="assume every CHARACTER (not card) is at level 50 = 361 points. The "
        "default for the maxed scenarios; use it to lift the account scenario to "
        "level 50 too.",
    )
    parser.add_argument(
        "--real-char-level",
        action="store_true",
        help="the opposite toggle: score my-cards-maxed at the account's REAL "
        "character levels instead of its level-50 default. Incompatible with "
        "full-collection-maxed (a complete account is at level 50).",
    )
    parser.add_argument(
        "--path-length", type=int, default=DEFAULT_PATH_LENGTH,
        help=f"longest chain a single move may buy (default {DEFAULT_PATH_LENGTH}). "
        "MEASURED: the red corridor is 17 nodes deep, so a smaller cap leaves the "
        "high-value deep half of red/blue (score-support + all-stats) unbought.",
    )
    parser.add_argument(
        "--lookahead",
        action="store_true",
        default=False,
        help="re-score a zero-value move by what it UNLOCKS (default on; §1a)",
    )
    parser.add_argument(
        "--no-lookahead", dest="lookahead", action="store_false",
        help="rank purely by immediate delta -- the failure mode §1a describes",
    )
    parser.add_argument(
        "--lookahead-below", type=float, default=1.0,
        help="only moves whose own delta is <= this get a follow-up probe",
    )
    parser.add_argument(
        "--respec",
        action="store_true",
        help="board RESET refunds (owner-confirmed): budget = inventory + spent, "
        "points = each character's FULL grant, and the board starts bare",
    )
    parser.add_argument(
        "--infinite",
        action="store_true",
        help="remove every budget -- the ceiling a real plan is measured against",
    )
    parser.add_argument(
        "--infinite-materials",
        action="store_true",
        help="unlimited cubes/hexas ONLY -- points stay bounded by character level "
        "(real, or 361 with --at-max-level). The orthogonal 'infinite resources' the "
        "web board plan means: materials are the material axis, points the level axis, "
        "so lifting materials must NOT also lift the point budget the way --infinite does",
    )
    parser.add_argument(
        "--ceiling",
        action="store_true",
        help="(with --team) reproduce holodori.best's Board Optimizer CEILING "
        "preset: bare boards, infinite cubes/hexas, max board points (361) on "
        "EVERY holomem, all invited (account-global G/Y searched on every "
        "board). Points are the only limit. For validating our plan against "
        "holodori's on the same footing",
    )
    parser.add_argument("--depth", type=int, default=12, help="max purchases (default 12)")
    parser.add_argument("--balance", default=None, help='e.g. "red_cube=3900,red_hexa=224"')
    parser.add_argument("--pool", type=int, default=60, help="team pool size (default 60)")
    parser.add_argument("--top", type=int, default=25, help="rows to print")
    parser.add_argument("--limit", type=int, default=0, help="cap candidates (0 = all)")
    parser.add_argument("--characters", default=None, help="comma-separated chr ids")
    parser.add_argument("--character", default=None, help="character for --pair")
    parser.add_argument(
        "--song", default=None, metavar="MUSIC_ID",
        help="optimise the board/sockets FOR THIS SONG (per-song scorer) instead "
        "of the general ranking. The chart's cast drives the singer-conditional "
        "yellow/socket bonus, so a solo song reaches the score cap with fewer "
        "purchased nodes. Needs the chart library (see --charts).",
    )
    parser.add_argument(
        "--charts", default=None, metavar="PATH",
        help="chart library for --song (default tools/recon/proxy/dumps/charts.json; "
        "build it with tools/recon/proxy/build_charts.py)",
    )
    parser.add_argument(
        "--scenario", default="account", choices=SCENARIOS,
        help="which collection state to optimise over (BOARD-EXPORT-CONTRACT.md): "
        "account (real levels, owned cards), my-cards-maxed (owned cards at max), "
        "full-collection-maxed (every card in the game at max). Default: account.",
    )
    parser.add_argument(
        "--for-team", default=None, metavar="LEADER,M1..M5",
        help="plan the board FOR THIS EXACT team (6 comma-separated card ids: "
        "leader first, then five members) instead of the pool's own best team. "
        "This is how the web serves the plan for the team the SOLVE won — which "
        "depends on the board toggle/basis and can differ from the plan's best. "
        "With --for-team the pool only needs to be big enough to prime the oracle "
        "state (the team is fixed), so a small --pool is fast.",
    )
    args = parser.parse_args()
    if args.state:
        cmd_state(args)
    elif args.marginal:
        cmd_marginal(args)
    elif args.plan:
        cmd_plan(args)
    elif args.curve:
        cmd_curve(args)
    elif args.per_team:
        cmd_per_team(args)
    elif args.team:
        cmd_team(args)
    else:
        if not args.character:
            raise SystemExit("--pair needs --character")
        cmd_pair(args)


if __name__ == "__main__":
    main()
