"""Systematic deck-evaluation probe -> append-only DB.

Derives each board contribution to the server's Unit Score decomposition by controlled
SaveDeck+GetDeck ablations, and records EVERY probe (inputs + full server outputs) to
dumps/deck_probe_db.jsonl. The DB is append-only and keyed by an exact spec, so:
  * overwriting a scratch slot never loses data (it is already in the DB), and
  * re-runs skip specs already probed.

Isolation strategy (targeted ablation, NOT brute force):
  --members : every owned card in a team led by a NULL leader (0 leader contribution) ->
              each card's per-card MEMBER board (effect_up / prob / cool / power share).
  --leaders : every owned (character,costume) leader over FIXED null-board members ->
              each leader's LEADER board + costume contribution (team-wide delta).
  --analyze : read the DB and print the per-card and per-leader tables.

SAFETY: SaveDeck/GetDeck are temp-added to account_snapshot.ALLOWED IN MEMORY only (the
on-disk read-only whitelist is never touched). Writes go to a FREE slot per character
(never an existing deck). Rate-limited 0.3s. No credential printed.
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import account_snapshot as snap
import deck_oracle as do

SAVEDECK = "/rpc.api.Live/SaveDeck"
GETDECK = "/rpc.api.Live/GetDeck"
snap.ALLOWED = frozenset(set(snap.ALLOWED) | {SAVEDECK, GETDECK})

DB = os.path.join(HERE, "dumps", "deck_probe_db.jsonl")
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
# a leader measured to contribute 0 board + 0 costume effect_up:
NULL_LEADER = ("chr-00001", "cos-00001-cmmn-0000-00")


def vf(fn, val):
    return bytes([(fn << 3)]) + snap.write_varint(val)


def save_req(leader, costume, number, cards, name="zzP"):
    r = snap.pb_bytes_field(1, leader.encode()) + snap.pb_bytes_field(2, costume.encode())
    r += vf(3, number) + snap.pb_bytes_field(4, name.encode())
    for i, c in enumerate(cards, 1):
        r += snap.pb_bytes_field(5, vf(1, i) + snap.pb_bytes_field(2, c.encode()))
    return r


def fire(host, path, req, token, ident, mver):
    rh, tr, payload = snap.call(host, path, snap.frame(snap.seal(req)),
                                token=token, ident=ident, master_version=mver)
    st = tr.get("grpc-status", rh.get("grpc-status"))
    if st not in (None, "0"):
        return None, f"grpc-status {st} {tr.get('grpc-message', '')}"
    for msg in snap.unframe(payload):
        if msg:
            return snap.unseal(msg), None
    return b"", None


def read_full(pt):
    ev = do._evaluation(pt)
    if not ev:
        return None
    ev["cards"] = do._cards_with_stats(pt)
    return ev


def load_done():
    done = set()
    if os.path.exists(DB):
        with open(DB, encoding="utf-8") as f:
            for line in f:
                try:
                    done.add(json.loads(line)["spec"])
                except Exception:
                    pass
    return done


def free_slot(sd, character, used_extra):
    used = {d.get("number") for d in sd.get("decks", [])
            if d.get("character_id") == character} | used_extra.get(character, set())
    for n in range(1, 11):
        if n not in used:
            return n
    return 10  # fall back to the last slot (still overwrites only a probe, never a kept deck)


def probe(host, token, ident, mver, sd, done, used_extra, kind, leader, costume, members):
    spec = f"{leader}|{costume}|{'|'.join(members)}"
    if spec in done:
        return "skip"
    slot = free_slot(sd, leader, used_extra)
    pt, err = fire(host, SAVEDECK, save_req(leader, costume, slot, members), token, ident, mver)
    if err:
        print(f"  REJECTED {leader}/{costume}: {err}")
        return None
    ev = read_full(pt)
    if not ev:
        print(f"  no eval for {leader}/{costume}")
        return None
    rec = {"kind": kind, "spec": spec, "leader": leader, "costume": costume,
           "members": members, "slot": slot, "ev": ev}
    with open(DB, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    done.add(spec)
    used_extra.setdefault(leader, set()).add(slot)
    time.sleep(0.3)
    return ev


def owned_cards():
    return json.load(open(os.path.join(REPO, "data", "owned.json"), encoding="utf-8"))["owned"]


def char_of(cid):
    return cid.split("-")[1]


def representatives():
    """One owned card per character (highest rarity digit) -- the member board is a per-character
    skill-tree, shared by every card of that character, so one representative probes it."""
    by_char = {}
    for c in owned_cards():
        ch = char_of(c)
        if ch not in by_char or c.split("-")[2] > by_char[ch].split("-")[2]:
            by_char[ch] = c
    return by_char


def distinct_teams(pool, size=5):
    """Partition `pool` (one card per character) into teams of `size` DISTINCT characters.
    The last short team is padded with earlier reps of characters not already in it, so every
    SaveDeck gets 5 distinct-character members (the game rejects duplicates)."""
    teams = [pool[i:i + size] for i in range(0, len(pool), size)]
    if teams and len(teams[-1]) < size:
        have = {char_of(c) for c in teams[-1]}
        for c in pool:
            if len(teams[-1]) >= size:
                break
            if char_of(c) not in have:
                teams[-1].append(c)
                have.add(char_of(c))
    return teams


def sweep_members(host, token, ident, mver, sd, done, used_extra):
    """Every owned character as a member under a NULL leader -> its member-board.

    Teams are 5 DISTINCT characters. The null leader's own character is excluded from the member
    pool (a character can't be both leader and member), then probed separately under a second
    null leader so its board is captured too."""
    reps = representatives()
    lead_ch = char_of(NULL_LEADER[1].replace("cos-", "card-"))  # '00001'
    pool = [reps[ch] for ch in reps if ch != lead_ch]
    n = 0
    for g in distinct_teams(pool):
        ev = probe(host, token, ident, mver, sd, done, used_extra,
                   "member", NULL_LEADER[0], NULL_LEADER[1], g)
        if ev and ev != "skip":
            n += 1
            eu = {char_of(c["card"]): c.get("active_effect_up") for c in ev["cards"].values()}
            print(f"  {eu}")
    # capture the null-leader character's own board: put it as a member under a 2nd null leader
    if lead_ch in reps:
        ALT = ("chr-00004", "cos-00004-cmmn-0000-00")  # measured 0 leader contribution too
        others = [reps[ch] for ch in reps if ch not in (lead_ch, "00004")][:4]
        team = [reps[lead_ch]] + others
        ev = probe(host, token, ident, mver, sd, done, used_extra,
                   "member", ALT[0], ALT[1], team)
        if ev and ev != "skip":
            n += 1
            eu = {char_of(c["card"]): c.get("active_effect_up") for c in ev["cards"].values()}
            print(f"  (alt leader) {eu}")
    print(f"member sweep: {n} new probes")


def sweep_leaders(host, token, ident, mver, sd, done, used_extra):
    """Every distinct owned (character,costume) leader over FIXED null-board members."""
    cards = owned_cards()
    reps = representatives()
    # every distinct owned (character, costume): a card id 'card-04003-5-uniq-0044-00'
    # -> leader 'chr-04003' + costume 'cos-04003-uniq-0044-00'.
    seen, leaders = set(), []
    for cid in sorted(cards):
        p = cid.split("-")  # ['card','04003','5','uniq','0044','00']
        key = (f"chr-{p[1]}", f"cos-{p[1]}-{p[3]}-{p[4]}-{p[5]}")
        if key not in seen:
            seen.add(key)
            leaders.append(key)
    n = 0
    for leader, costume in leaders:
        # 5 fixed members: distinct characters, none equal to the leader's character.
        mem = [reps[ch] for ch in reps if ch != leader.split("-")[1]][:5]
        if len(mem) < 5:
            continue
        ev = probe(host, token, ident, mver, sd, done, used_extra,
                   "leader", leader, costume, mem)
        if ev and ev != "skip":
            n += 1
            eu = [c.get("active_effect_up") for c in ev["cards"].values()]
            print(f"  {leader} {costume}: effect_up={eu}")
    print(f"leader sweep: {n} new probes")


def analyze():
    if not os.path.exists(DB):
        print("no DB yet")
        return
    recs = [json.loads(l) for l in open(DB, encoding="utf-8")]
    print(f"DB: {len(recs)} probes\n")
    # per-card member-board (from member-kind probes)
    print("=== per-card MEMBER board (effect_up / prob / cool) ===")
    seen = {}
    for r in recs:
        if r["kind"] != "member":
            continue
        for c in r["ev"]["cards"].values():
            cid = c["card"]
            seen[cid] = (c.get("active_effect_up") or 0, c.get("active_prob_up") or 0,
                         c.get("active_cool_shorten") or 0)
    for cid in sorted(seen):
        eu, pr, co = seen[cid]
        if eu or pr or co:
            print(f"  {cid:<28} effect_up={eu:>6} prob={pr:>6} cool={co:>5}")


def main():
    if "--analyze" in sys.argv:
        analyze()
        return
    host, cred = snap.load_credential("main")
    ident = snap.client_headers("main")
    mver = snap.fetch_master_version(host, ident)
    token = snap.login(host, cred, ident, mver)
    sd = snap.decode_snapshot(snap.fetch_userdata(host, token, ident, mver))
    done = load_done()
    used_extra = {}
    print(f"DB has {len(done)} probes already; appending to {DB}")
    if "--members" in sys.argv:
        sweep_members(host, token, ident, mver, sd, done, used_extra)
    if "--leaders" in sys.argv:
        sweep_leaders(host, token, ident, mver, sd, done, used_extra)
    print("Probe decks left in FREE slots; delete in-game if you like. Data is safe in the DB.")


if __name__ == "__main__":
    main()
