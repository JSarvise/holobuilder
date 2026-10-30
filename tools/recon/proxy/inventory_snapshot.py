"""The account's ITEM balances, live off the wire.

The other refresh artefacts do not hold how many Red Cubes the account has,
and the board optimiser needs it: cubes and hexas are half of what a node
costs. The balance is in `User/Get`, but only when read through
`grpc_decrypt.decrypt_exact`: the plain `decrypt` does not INFLATE the body and
yields 57 KB of garbage, while `decrypt_exact` inflates the same response to
~630 KB carrying every item the account owns.

    proxyenv/Scripts/python inventory_snapshot.py            # live, writes dumps/inventory.json
    proxyenv/Scripts/python inventory_snapshot.py --capture dumps/foo.mitm   # offline, no credential
    proxyenv/Scripts/python inventory_snapshot.py --dry-run  # what it would send

⚠ **The live path spends `state/account_credential.json`, which IS the account**
— 255 opaque bytes, full access, no second factor. This module opens exactly one
authenticated endpoint, `/rpc.api.User/Get`, which is on `account_snapshot.py`'s
READ-ONLY whitelist. It imports that module rather than reimplementing the call
so the whitelist stays enforced in ONE place; a second copy of that judgement is
exactly how a read-only tool stops being read-only.

What it writes: `dumps/inventory.json`, and nothing else. That file is derived
account state, not hand-edited truth like `data/owned.json` and
`state/collection.json`.
"""

from __future__ import annotations

import argparse
import json
import atomic_io
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DUMPS = HERE / "dumps"
sys.path.insert(0, str(HERE))

import account_snapshot as snap  # noqa: E402

OUT = DUMPS / "inventory.json"

# `UserData.user_item_list`, from
# `symphytum-server/types/protobufs/rpc/api/common/user_data.gen.proto`.
ITEM_FIELD = 208896283
ITEM_SUB = {2: "item_id", 3: "expired_time", 4: "quantity", 5: "last_acquired_time"}

# The eight skill-tree currencies, named so the report is readable without a
# lookup. Everything else the account holds is still written out verbatim --
# naming a subset must never mean dropping the rest.
SKILL_TREE = {
    "item-skill_tree_leader-1": "Red Cube",
    "item-skill_tree_leader-2": "Red Hexa",
    "item-skill_tree_card-1": "Blue Cube",
    "item-skill_tree_card-2": "Blue Hexa",
    "item-skill_tree_allmember-1": "Green Cube",
    "item-skill_tree_allmember-2": "Green Hexa",
    "item-skill_tree_content-1": "Yellow Cube",
    "item-skill_tree_content-2": "Yellow Hexa",
}


def items_from_userdata(body: bytes) -> list[dict]:
    """Every `UserItem` in an inflated `User/Get` body, or `[]`.

    `[]` and "the account owns nothing" are different answers and this cannot
    tell them apart, so the caller checks and refuses to write an empty file."""
    schema = snap.load_userdata_schema()
    if not schema:
        raise SystemExit(
            "UserData schema not found -- inventory_snapshot needs the .proto tree "
            f"at {snap.PROTO_DIR}"
        )
    level = snap.locate_userdata(body, schema)
    if not level:
        return []
    return [
        snap.sub(payload, ITEM_SUB)
        for field, wire, payload in level
        if wire == 2 and field == ITEM_FIELD
    ]


def from_capture(path: Path) -> list[dict]:
    """Offline: the newest `User/Get` in a `.mitm`. No credential, no socket.

    ⛔ Through `decrypt_exact`. The plain `decrypt` does not inflate and every
    body it returns is garbage."""
    import grpc_decrypt as gd  # noqa: PLC0415

    best: list[dict] = []
    for name, _token, body in gd.iter_grpc_exact(str(path)):
        if "User/Get" not in name:
            continue
        items = items_from_userdata(body)
        if len(items) > len(best):
            best = items
    return best


def live() -> list[dict]:
    """One `Master/Get`, one `Auth/Login`, one `User/Get`. Nothing else."""
    host, credential = snap.load_credential("main")
    ident = snap.client_headers("main")
    master_version = snap.fetch_master_version(host, ident)
    token = snap.login(host, credential, ident, master_version)
    return items_from_userdata(snap.fetch_userdata(host, token, ident, master_version))


def write(items: list[dict], source: str) -> None:
    document = {
        "_source": source,
        "_captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "_note": (
            "Every item the account holds, from UserData.user_item_list. "
            "skill_tree carries the eight board currencies by their English "
            "names; items carries all of them verbatim."
        ),
        "skill_tree": {
            SKILL_TREE[row["item_id"]]: int(row.get("quantity") or 0)
            for row in items
            if row.get("item_id") in SKILL_TREE
        },
        "items": {
            str(row.get("item_id")): int(row.get("quantity") or 0) for row in items
        },
    }
    DUMPS.mkdir(parents=True, exist_ok=True)
    atomic_io.write_bytes(OUT, json.dumps(document, indent=1, ensure_ascii=False).encode("utf-8"))
    print(f"wrote {OUT}  ({len(items)} items, {len(document['skill_tree'])} board currencies)")
    for name in SKILL_TREE.values():
        if name in document["skill_tree"]:
            print(f"    {name:12s} {document['skill_tree'][name]:>10,}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", default=None, help="read a .mitm instead of the network")
    parser.add_argument("--dry-run", action="store_true", help="say what would be sent")
    args = parser.parse_args()

    if args.dry_run:
        host, credential = snap.load_credential("main")
        print(f"host       : {host}")
        print(f"credential : {len(credential)} B, not printed")
        print("would POST : /rpc.api.Master/Get, /rpc.api.Auth/Login, /rpc.api.User/Get")
        print(f"would write: {OUT}")
        return 0

    if args.capture:
        items = from_capture(Path(args.capture))
        source = f"capture {Path(args.capture).name}"
    else:
        items = live()
        source = "live User/Get"

    if not items:
        # An empty write would look exactly like an account that owns nothing,
        # and the board optimiser would then plan against a zero budget without
        # a word. Refusing is the only honest outcome.
        print("no user_item_list decoded -- refusing to write an empty inventory", file=sys.stderr)
        return 1
    write(items, source)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
