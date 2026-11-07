"""Planned boards drawn as the board export: `POST /api/board-doc` (a team's six boards
with a given developed board overlaid) and `POST /api/board-plan-account` (the whole
account's planned boards for a team, as holodori.best shows them).

Both build the board-export/v0 document (`board_export.build`, through
`precompute_optimisations`), overlay the plan as to_buy / to_sell / optimised_nodes,
place the sockets the plan chose, and mark what changes. Read-only.
"""

from __future__ import annotations

import time
from typing import Mapping

import board_values

from hbweb import basis, config


def _maxed_complete_state(precompute, team: dict, ids: list) -> dict:
    """A maxed account: EVERY holomem's target, against the account's real boards, so the
    player sees what to unlock and what it costs. Only what scores for THIS team: per
    holomem the nodes `leaf_inputs.maxed_bench_targets(team)` names (greens that reach a
    team card, per-song yellows, a team singer's conditional reds, socketed Connect cards);
    a team holomem also its row's planned board -- the row's plan is ONE song's, the union
    covers the songs she sings (solo AND group yellows); each plus the cheapest path from
    the account's boards and what the account already holds (a maxed account resets nothing)."""
    import leaf_inputs  # noqa: PLC0415

    targets = leaf_inputs.maxed_bench_targets(ids)
    now = precompute.board_export.build(gain=False, pool=1,
                                        scenario=basis.SCENARIO_BY_BASIS["maxed"])
    state = {}
    for ch in now.get("characters", []):
        c = ch["chr"]
        nodes = {n["id"]: n for n in ch.get("nodes", [])}
        extra = set((targets.get(c) or {}).get("nodes") or [])
        if c in team:                       # its sockets are its row's
            extra = {i for i in extra if not i.startswith("S-")}
        want = set((team.get(c) or {}).get("nodes") or []) | extra
        owned = {i for i, n in nodes.items() if n.get("state") == "owned"}
        reach = set()
        for i in want:
            reach |= set((nodes.get(i) or {}).get("unlock_path") or []) | {i}
        slots = (team.get(c) or targets.get(c) or {}).get("slots") or {}
        state[c] = {"nodes": sorted(owned | reach), "slots": dict(slots)}
    return state


def over_points(doc: Mapping, budget: int) -> list[dict]:
    """The boards of `doc` whose planned nodes cost more than `budget` points, as
    `{chr, name, points}`. A maxed account's complete board (`_maxed_complete_state`) keeps
    every node the account already holds, since it resets nothing, so an account that
    bought nodes off the scoring paths can need more points than a max-level holomem holds."""
    return [{"chr": c["chr"], "name": c.get("name") or c["chr"], "points": c["plan"]["point_cost"]}
            for c in doc.get("characters", [])
            if (c.get("plan") or {}).get("point_cost") is not None
            and c["plan"]["point_cost"] > budget]


def serve_board_doc(h) -> None:
    """`POST /api/board-doc {board_state, cards, basis, budget}` — the
    board-export/v0 doc for the six characters of a team with a GIVEN
    developed board overlaid (`_apply_plan`: to_buy / to_sell /
    optimised_nodes / slots) — build + overlay + sockets from the given
    slots, NO optimisation call. Fetched lazily when a row's board section
    is opened. `budget`: "real" (points.granted) | "max" (what a max-level holomem
    holds, `basis.maxed_account_budget`) | int flat cap (at most the whole tree) —
    `point_budget` is labelled by that kind, never "453 / 97"."""
    body = h.body_or_empty()
    board_state = body.get("board_state")
    if not isinstance(board_state, dict) or not board_state:
        h.reply_json(400, {"error": "need a non-empty 'board_state' {chr: {nodes, slots}}"})
        return
    ids = [str(c) for c in (body.get("cards") or []) if c]
    basis_name = basis.parse_basis(body.get("basis"))
    try:
        flat_budget = basis.parse_budget(body.get("budget"))
        if flat_budget is None and basis_name == basis.MAXED_ACCOUNT_BASIS:
            flat_budget = basis.maxed_account_budget()
    except basis.MaxedAccountUnavailable as exc:
        h.reply_json(503, {"error": str(exc)})
        return
    precompute = basis.load_precompute()
    if precompute is None or not hasattr(precompute, "board_export"):
        h.reply_json(503, {"error": (
            "could not import board_export via precompute_optimisations "
            "(needs the packages of requirements.txt; setup.cmd or setup.sh installs them).")})
        return
    try:
        # the team's sockets are the plan's; one it left empty keeps the account's card
        # when the plan uses that card nowhere (`settle_slots`)
        state = {c: {"nodes": list((st or {}).get("nodes") or []), "slots": dict((st or {}).get("slots") or {})}
                 for c, st in board_state.items() if isinstance(st, dict)}
        complete = bool(body.get("complete")) and basis_name == "maxed"
        if complete:
            state = _maxed_complete_state(precompute, state, ids)
            flat_budget = basis.maxed_account_budget()
        else:
            settle_slots(state, config.read_json(config.BOARD_STATE_PATH) or {}, set(state),
                         connect_groups(precompute))
        doc = board_doc_for(precompute, state, ids, basis_name, flat_budget)
        if complete:
            doc["over_points"] = over_points(doc, flat_budget)
    except Exception as exc:  # noqa: BLE001 — report as JSON, never HTML 500
        h.reply_error(exc)
        return
    h.reply_json(200, doc)


def _plan_account_state(body: Mapping, whole: Mapping, captured: Mapping) -> tuple[dict, set]:
    """`(state, chosen)`: the plan's boards with the account's sockets, the row's own
    six boards (`board_state`) over them, and the bench sockets the detail re-chose
    (`bench_slots`, `row_detail._bench_slots`); `chosen` = the boards whose sockets
    the plan re-chose."""
    state = {chr_id: {"nodes": list((plan or {}).get("nodes") or []),
                      "slots": dict((captured.get(chr_id) or {}).get("slots") or {})}
             for chr_id, plan in whole.items()}
    for chr_id, st in (body.get("board_state") or {}).items():
        if isinstance(st, dict):
            state[chr_id] = {"nodes": list(st.get("nodes") or []), "slots": dict(st.get("slots") or {})}
    chosen = {c for c, st in (body.get("board_state") or {}).items() if isinstance(st, dict)}
    for chr_id, slots in (body.get("bench_slots") or {}).items():
        if isinstance(slots, dict) and chr_id in state:
            state[chr_id]["slots"] = {str(k): str(v) for k, v in slots.items() if v}
            chosen.add(chr_id)
    return state, chosen


def serve_board_plan_account(h) -> None:
    """`POST /api/board-plan-account {cards: [leader, m1..m5], song?, song_pool?, assumptions,
    board_state?}` — the WHOLE account's planned Holomem Boards for a team, as
    holodori.best shows them: every holomem's target board, not only the six.

    The plan is the exact one (`materials_plan.plan_for` -> `board_plan_solver.
    solve_team_board(develop_bench=True)`) under the request's board levers
    (`ondemand.Assumptions.plan_opts`); the team's six boards are the row's own
    (`board_state`, the boards its score was computed on) when given. Answers the
    board-export doc of every character with the plan overlaid (to_buy / to_sell /
    optimised_nodes), `changed` = the holomems whose board moves. Read-only."""
    import materials_plan  # noqa: PLC0415
    import ondemand  # noqa: PLC0415

    body = h.body_or_empty()
    ids = [str(c) for c in (body.get("cards") or []) if c]
    if len(ids) != 6:
        h.reply_json(400, {"error": "need 'cards': [leader, five members]"})
        return
    try:
        a = ondemand.parse_assumptions(body.get("assumptions"))
    except (TypeError, ValueError) as exc:
        h.reply_json(400, {"error": f"bad assumptions: {exc}"})
        return
    if a.boards != "plan":
        h.reply_json(400, {"error": "boards as they are: there is no plan to show"})
        return
    precompute = basis.load_precompute()
    if precompute is None or not hasattr(precompute, "board_export"):
        h.reply_json(503, {"error": "could not import board_export via precompute_optimisations"})
        return
    try:
        opts = a.plan_opts()
        if isinstance(body.get("song_pool"), list) and body["song_pool"]:
            # a Highlight row on the whole pool: its plan values the yellows on every
            # pool song (`row_detail.pool_detail`'s `plan_pool`, the same plan)
            opts["song_pool"] = body["song_pool"]
        planned = materials_plan.plan_for(ids[0], ids[1:], body.get("song") or None, opts,
                                          parallel=True)
        if planned is None:
            h.reply_json(422, {"error": "the wallet cannot fund a plan for this team"})
            return
        captured = config.read_json(config.BOARD_STATE_PATH) or {}
        state, chosen = _plan_account_state(body, planned[2], captured)
        settle_slots(state, captured, chosen, connect_groups(precompute))
        flat = basis.maxed_account_budget() if a.max_points else None
        doc = board_doc_for(precompute, state, ids, "account", flat)
        team = {"chr-" + c.split("-")[1] for c in ids}
        doc["team_chrs"] = sorted(team)
        doc["changed"] = sorted(c["chr"] for c in doc.get("characters", [])
                                if (c.get("plan") or {}).get("to_buy") or (c.get("plan") or {}).get("to_sell")
                                or (c.get("plan") or {}).get("socket_changes"))
        doc["assumptions"] = a.describe()
        # False: a MILP of the plan stopped on the solver's time limit
        doc["plan_optimal"] = materials_plan.plan_optimal(ids[0], ids[1:],
                                                          body.get("song") or None, opts)
    except Exception as exc:  # noqa: BLE001
        h.reply_error(exc)
        return
    h.reply_json(200, doc)


def settle_slots(state: dict, captured: Mapping, chosen: set, group_of: Mapping | None = None) -> None:
    """Make `state`'s socket map one the account can hold, in place. `chosen`: the boards
    whose sockets the plan re-chose; every other board keeps the account's cards.
    - `group_of` (card -> its Connect shape and permil): the matching names a placement
      by the lowest unused id of an interchangeable group, so a socket already holding
      an EQUIVALENT card would read as a swap that changes nothing. The account's card is
      kept there instead (the plan's id goes where the account's card was planned, if
      anywhere) -- the same multipliers, no move shown;
    - a card the plan placed is taken off any board that kept it (one card, one socket);
    - a socket the plan left empty keeps the account's card when the plan uses that card
      nowhere and still owns the socket: the matching only places a card where it adds
      score, so an empty socket there means "nothing to gain", not "take the card out"
      (no move shown for it). A socket the plan resets loses its card."""
    if group_of:
        where = {card: (c, sock) for c in chosen
                 for sock, card in ((state.get(c) or {}).get("slots") or {}).items()}
        for chr_id in sorted(chosen):
            slots = (state.get(chr_id) or {}).get("slots") or {}
            for sock, card in list(slots.items()):
                now = ((captured.get(chr_id) or {}).get("slots") or {}).get(sock)
                if not now or now == card or group_of.get(now) is None \
                        or group_of.get(now) != group_of.get(card):
                    continue
                if now in where:                       # the plan put the account's card elsewhere
                    oc, osock = where[now]
                    state[oc]["slots"][osock] = card
                    where[card] = (oc, osock)
                else:
                    where.pop(card, None)
                slots[sock] = now
                where[now] = (chr_id, sock)
    used = {card for c in chosen for card in ((state.get(c) or {}).get("slots") or {}).values()}
    for chr_id, entry in state.items():
        if chr_id not in chosen:
            entry["slots"] = {k: v for k, v in (entry.get("slots") or {}).items() if v not in used}
    for chr_id in chosen:
        entry = state.get(chr_id)
        if entry is None:
            continue
        slots = entry.setdefault("slots", {})
        owned = {"S-001", *(entry.get("nodes") or ())}     # the root is free on every board
        for socket, card in ((captured.get(chr_id) or {}).get("slots") or {}).items():
            # only a socket the plan still owns can keep a card (a reset S-004 cannot)
            if card and socket not in slots and card not in used and socket in owned:
                slots[socket] = card
                used.add(card)


_CONNECT_GROUPS: list = [None, 0.0]


def connect_groups(precompute) -> dict:
    """card id -> (shape, permil) of the account's Connect cards (`connect_cards_for`), the
    key two cards are interchangeable by; re-read at most once a minute."""
    if _CONNECT_GROUPS[0] is None or time.monotonic() - _CONNECT_GROUPS[1] > 60:
        try:
            cards = precompute.connect_cards_for("account") if precompute is not None else {}
        except Exception:  # noqa: BLE001 - without it a socket simply shows the plan's id
            cards = {}
        _CONNECT_GROUPS[0] = {k: (tuple(map(tuple, v[0])), v[1]) for k, v in cards.items()}
        _CONNECT_GROUPS[1] = time.monotonic()
    return _CONNECT_GROUPS[0]


def socket_changes(doc: dict) -> None:
    """Mark every socket of the doc with what the plan does to it (`change`: "add" an
    empty socket gets a card, "remove" its card comes off, "swap" another card goes in;
    absent = unchanged) and count them per holomem (`plan.socket_changes`), comparing the
    account's card (`placed_card`) with the plan's (`recommended_card`)."""
    for c in doc.get("characters", []):
        n = 0
        for sock in c.get("sockets", []) or []:
            now, then = sock.get("placed_card") or None, sock.get("recommended_card") or None
            change = None if now == then else "add" if not now else "remove" if not then else "swap"
            if change:
                sock["change"] = change
                n += 1
            else:
                sock.pop("change", None)
        if c.get("plan") is not None:
            c["plan"]["socket_changes"] = n


def _place_sockets(doc: dict, overlay: Mapping, names: Mapping) -> None:
    """Fold the GIVEN placements onto each character's socket list (no optimiser call),
    naming the account's card and the recommended one."""
    for c in doc["characters"]:
        slots = (overlay.get(c["chr"]) or {}).get("slots") or {}
        for sock in c.get("sockets", []) or []:
            if sock.get("placed_card"):
                now = names.get(sock["placed_card"]) or {}
                sock["placed_card_name"] = now.get("name") or sock["placed_card"]
                sock["placed_card_member"] = now.get("member") or ""
            cid = slots.get(sock.get("socket"))
            if cid:
                info = names.get(cid) or {}
                sock["recommended_card"] = cid
                sock["recommended_card_name"] = info.get("name") or cid
                sock["recommended_card_member"] = info.get("member") or ""


def board_doc_for(precompute, board_state: dict, ids: list, basis_name: str, flat_budget) -> dict:
    """The board-export/v0 doc of the characters in `board_state` (+ `ids`' own) with that
    state overlaid as the plan (`_apply_plan`: to_buy / to_sell / optimised_nodes), its
    sockets the GIVEN placements, node values annotated. No optimisation call."""
    scenario = basis.SCENARIO_BY_BASIS.get(basis_name, "account")
    doc = precompute.board_export.build(gain=False, pool=1, scenario=scenario)
    wanted = set(board_state)
    if ids:
        wanted |= {"chr-" + c.split("-")[1] for c in ids if "-" in c}
    doc["characters"] = [c for c in doc.get("characters", []) if c["chr"] in wanted]
    overlay = {chr_id: {"nodes": list((st or {}).get("nodes") or []),
                        "slots": dict((st or {}).get("slots") or {})}
               for chr_id, st in board_state.items()}
    precompute._apply_plan(doc, overlay)
    precompute._annotate_plan_budget(doc, char_level="current")
    if flat_budget is not None:
        for c in doc["characters"]:
            if c.get("plan"):
                c["plan"]["point_budget"] = flat_budget
    names = precompute._card_names() if hasattr(precompute, "_card_names") else {}
    _place_sockets(doc, overlay, names)
    socket_changes(doc)
    doc["for_team"] = ids
    doc["plan_included"] = True
    # node values + Connect ranges/boosts (the page prints the game's numbers)
    board_values.annotate(doc, config.read_json(config.SKILLTREE_PATH) or {},
                          config.read_json(config.COLLECTION_PATH) or {},
                          maxed=basis_name == basis.MAXED_ACCOUNT_BASIS)
    doc["engine"] = "given"
    doc["budget"] = "real" if flat_budget is None else flat_budget
    doc["budget_kind"] = "real" if flat_budget is None else "flat"
    return doc
