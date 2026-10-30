# Decode the Holomem Board (skill tree): the node/effect model from the masters, plus the
# account's own unlocked-node state from a captured rpc.api.User/Get.
#
# The board is account state, and it matters because the game's own Unit Score decomposition (LangTipsDetail_Eng ::
# la-tips-hint-live_live_deck_evaluation-description-1) puts board effects in BOTH buckets:
#
#     Unit Score = Total Power x Score Bonus
#       Total Power  <- ... + Holomem Board (Parameter UP) + ...
#       Score Bonus  <- ... + Holomem Board (Score Bonus UP) + ...
#
# so no arithmetic check against an observed Unit Score is possible without the board.
#
# MODEL (from SkillTreeNode.f4 + the effect id's own `-<condition>` suffix; node colour is the
# group_id prefix):
#   R (type 2, 63 nodes)  condition `set_live_leader`, target `all`
#                         -> applies to the WHOLE team, but only when that holomem is Leader.
#   B (type 3, 31 nodes)  condition `always`, target `skill_tree_character`
#                         -> applies to that holomem's own card, always.
#   G (type 4, 182 nodes) condition `always`, target `all`
#                         -> small account-wide flat bonus on every card, e.g. a flat
#                            "+15 per stat" (green gives performance/technique/sense +5 and
#                            all_parameter +10).
#   S (type 1, 4 nodes)   the Connect nodes.
#   Y (type 5, 43 nodes)  not decoded here.
#
# Effect ids are self-describing:
#   skill_tree_effect-<name>-<value>-skill_tree_effect_target-<target>-<condition>
# `<name>` ending in `_permil_up` means the value is per-mille (a multiplier); otherwise the
# value is FLAT stat points. Both shapes occur on the same node colour.
#
# Effect names include performance/technique/sense/all_parameter_up,
# live_active_skill_effect_up, live_active_skill_activation_probability_up and
# live_active_skill_cool_time_shorten.
#
# Usage: proxyenv/Scripts/python board_state.py <flows.mitm>   (writes dumps/board_state.json)
#        python board_state.py --report                        (resolve the saved state)
import collections, json, os, re, sys, types, zlib

HERE = os.path.dirname(os.path.abspath(__file__))
DUMPS = os.path.join(HERE, "dumps")
sys.path.insert(0, HERE)
from master_cache import rows  # noqa: E402
import atomic_io  # noqa: E402

STATE_PATH = os.path.join(DUMPS, "board_state.json")
_EFFECT_RE = re.compile(
    r"skill_tree_effect-(?P<name>.+?)-(?P<value>\d+)"
    r"-skill_tree_effect_target-(?P<target>.+?)-(?P<condition>always|set_live_leader|.+)$")
COLOUR_BY_TYPE = {1: "S(connect)", 2: "R(leader)", 3: "B(member)", 4: "G(account)", 5: "Y"}


def parse_effect(effect_id):
    m = _EFFECT_RE.match(effect_id or "")
    if not m:
        return None
    d = m.groupdict()
    d["value"] = int(d["value"])
    d["permil"] = d["name"].endswith("_permil_up")
    d["name"] = d["name"][:-len("_permil_up")] if d["permil"] else d["name"]
    return d


def node_table():
    """node group_id -> [{number, type, effect, unlock}] (a node can have several levels)."""
    out = collections.defaultdict(list)
    for x in rows("SkillTreeNode"):
        d = x["data"]
        out[x["group_id"]].append({"number": int(x["number"]), "type": d.get(4, [None])[0],
                                   "effect": d.get(6, [None])[0], "unlock": d.get(7, [None])[0]})
    return dict(out)


def extract_state(mitm_path):
    """Unlocked nodes per holomem, from a captured User/Get.

    Segment-scanned rather than field-parsed: each `chr-NNNNN` marker owns the node ids up to
    the next one, which is unambiguous for this payload.

    ⛔ **Both the decrypt and the body matter.** It must be `gd.decrypt_exact` over
    `raw_content` (the bytes off the wire), not `gd.decrypt` (the CBC path) over
    `content` (which mitmproxy may have already transformed). The wrong pair turns a
    `User/Get` into ~58 KB of noise with **zero** `chr-NNNNN` markers, so the segment scan
    finds nothing and returns an empty dict — no exception, no warning, and empty reads as
    *"this capture carries no board state"*. The right pair gives ~657 KB carrying thousands
    of `chr-NNNNN` markers, the node ids and the `skill_tree` section. `deck_oracle.py` uses
    the exact decrypt for the same reason.

    Degrades per response rather than dropping it, so a decrypt regression shows up as a
    short payload instead of as a silent `{}`."""
    import grpc_decrypt as gd
    from mitmproxy import io as mio
    best, lossy = b"", False
    with open(mitm_path, "rb") as fh:
        for fl in mio.FlowReader(fh).stream():
            if fl.type != "http" or "User/Get" not in fl.request.path:
                continue
            resp = getattr(fl, "response", None)
            if not resp or not resp.raw_content:
                continue
            got = gd.decrypt_exact(resp.raw_content)
            if got:
                pt, how = got[1], False
            else:
                env = gd.parse_envelope(resp.raw_content)
                if not env:
                    continue
                pt, how = gd.decrypt(env[1], bytes.fromhex(gd.KEY_HEX)), True
            for wbits in (15, -15, 47):
                try:
                    pt = zlib.decompress(pt, wbits)
                    break
                except Exception:
                    pass
            if len(pt) > len(best):
                best, lossy = pt, how
    if not best:
        raise SystemExit("no User/Get response in that capture")
    if lossy:
        print("  WARNING: fell back to the CBC decrypt; this payload is probably unreadable "
              "and the result may be silently incomplete")
    text = re.sub(rb"[^\x20-\x7e]", b"\x00", best).decode("latin1")
    marks = [(m.group(0), m.start()) for m in re.finditer(r"chr-\d{5}", text)]
    board = {}
    for i, (chr_id, pos) in enumerate(marks):
        end = marks[i + 1][1] if i + 1 < len(marks) else len(text)
        found = re.findall(r"[BGRSY]-\d{3}", text[pos:end])
        if found:
            board.setdefault(chr_id, set()).update(found)
    return {k: sorted(v) for k, v in sorted(board.items())}


def _walk(buf, off, stop_fields=(3, 4, 5)):
    """Walk length-delimited protobuf fields from `off`, yielding (field, bytes).

    Only usable from a resync point: the first 16 plaintext bytes of every message are lost
    (the per-message nonce is not in the envelope), so parsing can never start at byte 0."""
    p = off
    while p < len(buf):
        tag = buf[p]
        field, wire = tag >> 3, tag & 7
        if field not in stop_fields or wire != 2:
            return
        p += 1
        n = shift = 0
        while p < len(buf):
            b = buf[p]; p += 1
            n |= (b & 0x7F) << shift; shift += 7
            if not b & 0x80:
                break
        yield field, buf[p:p + n]
        p += n


def extract_connect(mitm_path):
    """Boards from SkillTree/ConnectNode responses: {chr: {"nodes": [...], "slots": {S: card}}}.

    Placing a card on a Connect node returns that holomem's WHOLE board -- unlocked tiles plus
    every occupied slot -- which is the only readable source for the SLOTS.

    `User/Get` is readable too: its envelopes are NOT compressed (byte 0 is `0`) and through
    `decrypt_exact` they carry every board (see `extract_state`). This function is useful for
    the one thing `User/Get`'s segment scan does not give: the **slot -> card** pairing.

    Wire shape, from the resync point at the `chr-NNNNN` string:
        field 3  repeated  unlocked node group id      ("G-001")
        field 4  repeated  connect node group id       ("S-001")
        field 5  repeated  card id placed on it        ("card-00004-4-cmmn-0000-00")
    Slots and cards are parallel repeated fields, paired in emission order."""
    src = open(os.path.join(HERE, "grpc_decrypt.py"), encoding="utf-8").read().split("def main(")[0]
    gd = types.ModuleType("gd")
    exec(compile(src, "grpc_decrypt.py", "exec"), gd.__dict__)
    from mitmproxy import io as mio
    key = bytes.fromhex(gd.KEY_HEX)
    out, home = {}, {}
    with open(mitm_path, "rb") as fh:
        for fl in mio.FlowReader(fh).stream():
            if fl.type != "http" or "SkillTree/ConnectNode" not in fl.request.path:
                continue
            if not getattr(fl, "response", None) or not fl.response.raw_content:
                continue
            env = gd.parse_envelope(fl.response.raw_content)
            if not env:
                continue
            pt = gd.decrypt(env[1], key)
            m = re.search(rb"chr-\d{5}", pt)
            if not m:
                continue
            chr_id = m.group(0).decode()
            nodes, slots, cards = [], [], []
            for field, val in _walk(pt, m.end()):
                (nodes if field == 3 else slots if field == 4 else cards).append(
                    val.decode("utf-8", "replace"))
            rec = out.setdefault(chr_id, {"nodes": set(), "slots": {}})
            rec["nodes"].update(nodes)
            rec["slots"].update(dict(zip(slots, cards)))
            for slot, card in zip(slots, cards):
                home[card] = (chr_id, slot)
    # A card lives in exactly one place, but reading a board requires PLACING a card on it, so a
    # session that walks every holomem necessarily drags the same cards around and leaves earlier
    # snapshots showing a card that has since moved. Flows arrive in order, so the last placement
    # of each card is the true one; drop every earlier sighting.
    for chr_id, rec in out.items():
        rec["slots"] = {s: c for s, c in rec["slots"].items()
                        if home.get(c) == (chr_id, s)}
    return {k: {"nodes": sorted(v["nodes"]), "slots": v["slots"]} for k, v in sorted(out.items())}


def _nodes_of(entry):
    """State entries are either the old bare list of node ids or {"nodes": [...], "slots": {}}."""
    return entry if isinstance(entry, list) else entry.get("nodes", [])


def resolve(board, nodes):
    """(holomem, effect name, condition, target, permil?) -> summed value."""
    agg = collections.Counter()
    for chr_id, entry in board.items():
        for gid in _nodes_of(entry):
            for node in nodes.get(gid, []):
                eff = parse_effect(node["effect"])
                if eff:
                    agg[(chr_id, eff["name"], eff["condition"], eff["target"],
                         eff["permil"])] += eff["value"]
    return agg


def merge_connect(state, found):
    """Fold ConnectNode results into the saved state, keeping node ids already known.

    Only the slots are authoritative here: a ConnectNode response lists the board of the holomem
    acted on, so a holomem absent from this capture must keep whatever User/Get gave."""
    for chr_id, rec in found.items():
        old = state.get(chr_id)
        nodes = sorted(set(_nodes_of(old) if old else []) | set(rec["nodes"]))
        slots = dict((old or {}).get("slots", {}) if isinstance(old, dict) else {})
        slots.update(rec["slots"])
        state[chr_id] = {"nodes": nodes, "slots": slots}
    # The same invariant across captures, not just within one: a card this capture placed has moved
    # away from wherever an older capture last saw it, so evict it there.
    moved = {c: chr_id for chr_id, rec in found.items() for c in rec["slots"].values()}
    for chr_id, entry in state.items():
        if not isinstance(entry, dict):
            continue
        entry["slots"] = {s: c for s, c in (entry.get("slots") or {}).items()
                          if moved.get(c, chr_id) == chr_id}
    return state


def main():
    nodes = node_table()
    if "--connect" in sys.argv:
        found = extract_connect(sys.argv[sys.argv.index("--connect") + 1])
        if not found:
            raise SystemExit(f"no SkillTree/ConnectNode in that capture — {STATE_PATH} untouched")
        state = json.load(open(STATE_PATH)) if os.path.exists(STATE_PATH) else {}
        state = merge_connect(state, found)
        atomic_io.write_text(STATE_PATH, json.dumps(state, indent=1, sort_keys=True))
        for chr_id, rec in sorted(found.items()):
            print(f"  {chr_id}  {len(rec['nodes']):3d} tiles  slots: "
                  + (", ".join(f"{s}={c}" for s, c in sorted(rec["slots"].items())) or "(none)"))
        have = sum(1 for v in state.values() if isinstance(v, dict) and v.get("slots"))
        print(f"{len(found)} holomem from this capture; {have}/{len(state)} now have slot data "
              f"-> {STATE_PATH}")
        return
    if "--report" not in sys.argv:
        board = extract_state(sys.argv[1])
        if not board:
            # Not every User/Get carries the skill-tree section (the one in gacha_capture.mitm
            # does not). Refuse to overwrite a good state file with an empty extraction.
            raise SystemExit(
                f"no skill-tree state in that capture's User/Get — {STATE_PATH} left untouched")
        atomic_io.write_text(STATE_PATH, json.dumps(board, indent=1))
        print(f"{len(board)} holomem with unlocked nodes -> {STATE_PATH}")
    else:
        board = json.load(open(STATE_PATH))
    kinds = collections.Counter(
        (gid[0], n["type"]) for gid, ns in nodes.items() for n in ns)
    print("node colour -> type code:",
          {f"{c}={COLOUR_BY_TYPE.get(t, t)}": v for (c, t), v in sorted(kinds.items())})
    for (chr_id, name, cond, target, permil), value in sorted(resolve(board, nodes).items()):
        unit = f"{value / 10:g}%" if permil else f"+{value} flat"
        print(f"  {chr_id}  {name:44} {unit:>12}  when={cond:16} to={target}")


if __name__ == "__main__":
    main()
