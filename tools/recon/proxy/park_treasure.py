# Where are the park's treasures and random symbols RIGHT NOW?
#
# The answer is a READ, not a prediction. Two different mechanics wear the same "hidden
# collectable" costume in-game, and only one of them is random:
#
#   FIXED treasure chests -- master table `ParkTreasure`, 42 rows. Position, reward and
#     unlock condition are all static master data. Nothing about them is random or
#     per-session; the only account state is `UserParkTreasure.park_treasure_ids`, the
#     list of ones already opened (master rows + entity/master/park_treasure.proto).
#
#   RANDOM symbols -- master table `ParkRandomSymbol` is only the CANDIDATE POOL
#     (198 rows: area_id + park_target_mapping_id + unlock condition). Which of those
#     candidates are actually standing in the park is server state, in
#     `UserParkRandomSymbol` per area:
#         picked_park_target_mapping_ids     <- the spawn set the SERVER chose
#         collected_park_target_mapping_ids  <- of those, the ones already grabbed
#         last_refreshed_time                <- when the pick was rolled, PER AREA
#     (entity/transaction/user_park_random_symbol.gen.proto)
#
#     The roll is lazy: `last_refreshed_time` is not the reset boundary, it is the first
#     server contact after it, and it moves per area rather than park-wide -- an area you
#     have not entered can sit a whole day behind.
#
# So the client is TOLD the spawn positions. They arrive inside the UserData blob on
# `/rpc.api.User/Get`, which is part of every login sequence -- no reverse-engineering of
# an RNG is required, and none would help, because the roll happens server-side.
#
# `park_pos-*` ids resolve to a NAMED Unity transform (`reward_random_04`), not to x/y
# numbers -- master data names the spot, it does not place it. Plotting real coordinates
# needs the area scene out of the Octo bundles; naming the spot does not. Not known whether
# the scene carries them in a form worth parsing; not attempted here.
#
# Usage:
#   proxyenv/Scripts/python park_treasure.py <flows.mitm>    # decode from a capture
#   proxyenv/Scripts/python park_treasure.py --live          # pull a fresh User/Get
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from account_snapshot import (  # noqa: E402
    client_headers, fetch_master_version, fetch_userdata, fields, load_credential,
    load_userdata_schema, locate_userdata, login)
from master_cache import decode_record, open_table, rows, sql_table  # noqa: E402

# Field numbers in rpc.api.common.UserData (hash-derived, from the studio's own .proto).
F_RANDOM_SYMBOL_LIST = 364291179
F_TREASURE = 327922352
F_FIXED_SYMBOL = 401110107

RESOURCE_TYPE = {1: "card", 2: "costume", 3: "item", 999: "gold"}


def _text(b):
    return b.decode("utf-8", "replace") if isinstance(b, (bytes, bytearray)) else b


def decode_reward(blob: bytes) -> str:
    """common.Reward -> "12000x item-card_level_up"."""
    rtype = rid = None
    qty = 1
    for fn, wt, v in fields(blob):
        if fn == 1 and wt == 0:
            rtype = v
        elif fn == 2 and wt == 2:
            rid = _text(v)
        elif fn == 3 and wt == 0:
            qty = v
    name = rid or RESOURCE_TYPE.get(rtype, f"type{rtype}")
    return f"{qty}x {name}"


# --- masters ------------------------------------------------------------------------

def master_blobs(table):
    """Rows with the `data` blob left as RAW BYTES.

    master_cache.rows() utf-8-decodes every value, which mangles the packed Reward
    submessages in ParkTreasure.field 1000 -- they are protobuf, not text."""
    db = open_table(table)
    out = []
    for r in db.query(f"SELECT * FROM {sql_table(db, table)}"):
        out.append(decode_record(r["data"]))
    return out


def load_positions():
    """park_target_mapping_id -> readable spot name, via ParkTargetMapping -> ParkPosition."""
    pos_name = {}
    for r in rows("ParkPosition"):
        d = r["data"]
        pos_name[d[1][0]] = (d.get(2, [""])[0], d.get(4, [""])[0])  # (area, transform name)
    spot = {}
    for r in rows("ParkTargetMapping"):
        d = r["data"]
        pid = d.get(8, [None])[0]
        if pid:
            area, name = pos_name.get(pid, ("", pid))
            spot[d[1][0]] = {"position_id": pid, "area_id": area, "spot": name}
    return spot


def load_treasures(spot):
    out = {}
    for d in master_blobs("ParkTreasure"):
        tid = _text(d[1][0])
        mapping = _text(d[2][0])
        out[tid] = {
            "id": tid,
            "park_target_mapping_id": mapping,
            "condition_group_id": _text(d[6][0]) if 6 in d else None,
            "rewards": [decode_reward(b) for b in d.get(1000, [])],
            **spot.get(mapping, {}),
        }
    return out


def load_candidates():
    """area_id -> [park_target_mapping_id, ...] eligible random-symbol spots."""
    pool = {}
    for r in rows("ParkRandomSymbol"):
        d = r["data"]
        pool.setdefault(d[1][0], []).append(d[2][0])
    return pool


# --- account state ------------------------------------------------------------------

def userdata_from_capture(path):
    from grpc_decrypt import iter_grpc_exact
    schema = load_userdata_schema()
    if not schema:
        raise RuntimeError("UserData schema not found -- check PROTO_DIR in account_snapshot.py")
    best = None
    for rpc, _nonce, pt in iter_grpc_exact(path):
        if rpc != "/rpc.api.User/Get":
            continue
        got = locate_userdata(pt, schema)
        if got:
            best = got
    if best is None:
        raise RuntimeError(f"no parseable /rpc.api.User/Get UserData in {path}")
    return best


def userdata_live(account="main"):
    """Log in and pull a fresh User/Get. Read-only: the same whitelisted RPC path
    account_snapshot.py already uses for --refresh-board-state.

    `account` is 'main' or a guest region key (us/jp/as) from state/ranking_accounts.json."""
    schema = load_userdata_schema()
    if not schema:
        raise RuntimeError("UserData schema not found -- check PROTO_DIR in account_snapshot.py")
    host, credential = load_credential(account)
    ident = client_headers(account)
    master_version = fetch_master_version(host, ident)
    token = login(host, credential, ident, master_version)
    blob = fetch_userdata(host, token, ident, master_version)
    got = locate_userdata(blob, schema)
    if got is None:
        raise RuntimeError("live User/Get returned no parseable UserData")
    return got


def park_state(userdata_fields):
    """UserData fields -> {random: {area: {...}}, opened_treasures: [...], fixed: [...]}."""
    state = {"random": {}, "opened_treasures": [], "fixed_collected": []}
    for fn, wt, v in userdata_fields:
        if wt != 2:
            continue
        if fn == F_RANDOM_SYMBOL_LIST:
            area = None
            picked, collected, refreshed = [], [], None
            for f2, w2, v2 in fields(v):
                if f2 == 2:
                    area = _text(v2)
                elif f2 == 3:
                    collected.append(_text(v2))
                elif f2 == 4:
                    picked.append(_text(v2))
                elif f2 == 5:
                    refreshed = v2
            state["random"][area] = {
                "picked": picked, "collected": collected, "last_refreshed_time": refreshed}
        elif fn == F_TREASURE:
            for f2, w2, v2 in fields(v):
                if f2 == 2:
                    state["opened_treasures"].append(_text(v2))
        elif fn == F_FIXED_SYMBOL:
            for f2, w2, v2 in fields(v):
                if f2 == 2:
                    state["fixed_collected"].append(_text(v2))
    return state


# --- report -------------------------------------------------------------------------

def report(state, spot, treasures, pool):
    import datetime

    print("=" * 78)
    print("RANDOM SYMBOLS -- server-picked spawn positions, live from UserData")
    print("=" * 78)
    # The roll is per-area and lazy: an area you have not
    # entered since the 05:00 JST reset still shows YESTERDAY's row. Flag it rather than
    # letting the caller mistake a stale pick for the current one.
    stamps = [s["last_refreshed_time"] for s in state["random"].values()
              if s["last_refreshed_time"]]
    newest = max(stamps) if stamps else None
    for area in sorted(state["random"]):
        s = state["random"][area]
        t = s["last_refreshed_time"]  # epoch MILLIseconds
        when = datetime.datetime.fromtimestamp(t / 1000).isoformat(" ") if t else "never"
        stale = " *** STALE -- enter this area to re-roll ***" if t and t != newest else ""
        cand = pool.get(area, [])
        print(f"\n  {area}   picked {len(s['picked'])} of {len(cand)} eligible spots"
              f"   (rolled {when}){stale}")
        for m in s["picked"]:
            info = spot.get(m, {})
            mark = "COLLECTED" if m in s["collected"] else "   OUT   "
            print(f"    [{mark}] {m:45s} {info.get('spot', '?')}")
        stale = [m for m in s["collected"] if m not in s["picked"]]
        if stale:
            print(f"    ({len(stale)} collected id(s) not in the current pick)")

    print()
    print("=" * 78)
    print("FIXED TREASURE CHESTS -- static positions, from master ParkTreasure")
    print("=" * 78)
    opened = set(state["opened_treasures"])
    by_area = {}
    for t in treasures.values():
        by_area.setdefault(t.get("area_id") or "?", []).append(t)
    for area in sorted(by_area):
        chests = sorted(by_area[area], key=lambda c: c["id"])
        n_open = sum(1 for c in chests if c["id"] in opened)
        print(f"\n  {area}   {n_open}/{len(chests)} opened")
        for c in chests:
            if c["id"] in opened:
                continue
            print(f"    [ UNOPENED ] {c['spot']:28s} {', '.join(c['rewards']) or '(no reward)'}")
            print(f"                 needs: {c['condition_group_id']}")


def main():
    argv = sys.argv[1:]
    account = "main"
    if "--account" in argv:
        i = argv.index("--account")
        account = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]
    args = [a for a in argv if not a.startswith("--")]
    as_json = "--json" in argv
    live = "--live" in argv

    if live:
        source = f"live User/Get [{account}]"
        ud = userdata_live(account)
    else:
        if not args:
            # newest capture wins
            import glob
            caps = sorted(glob.glob(os.path.join(HERE, "dumps", "*.mitm")), key=os.path.getmtime)
            if not caps:
                sys.exit("no capture given and none found in dumps/; or pass --live")
            args = [caps[-1]]
        source = os.path.basename(args[0])
        ud = userdata_from_capture(args[0])
    state = park_state(ud)
    spot = load_positions()
    treasures = load_treasures(spot)
    pool = load_candidates()

    if as_json:
        print(json.dumps({"state": state, "treasures": treasures, "candidate_pool": pool},
                         ensure_ascii=False, indent=1))
    else:
        print(f"source: {source}\n")
        report(state, spot, treasures, pool)


if __name__ == "__main__":
    main()
