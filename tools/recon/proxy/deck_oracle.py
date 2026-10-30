# Pull the server's own team evaluations out of a capture, and check the model against them.
#
# WHY THIS IS THE MOST USEFUL ORACLE. Opening the deck screen makes the server return the
# complete Unit Score breakdown for that team -- the same twelve numbers the in-game "Unit Score
# Details" panel shows -- with no song, no play and no cost. So a captured deck screen is a
# labelled example: inputs (the cards) and the game's own answer (the eleven components).
#
# WHAT IT ESTABLISHES, and what it deliberately does not:
#   * Ground truth is whatever the server said. It is extracted, never computed.
#   * The model's predictions sit next to it with the delta shown. A component not yet computed
#     prints as "--", not as a zero and not as a pass.
#
# THE ONE THING TO BE CAREFUL ABOUT. A GetDeck response carries MORE cards than the selected
# team -- the deck presets and candidate lists travel in the same payload. Reading "the cards in
# the payload" as "the cards in the team" is wrong and silently poisons every comparison built on
# it. This module therefore only reports cards it can tie to a stat block, prints how many it
# found, and refuses to compare team-level sums unless that count is exactly 6 (1 leader +
# 5 members). Getting this wrong is the likeliest way to produce a confident, false validation.
#
# DECRYPTION. The payload is read through `decrypt_exact`, which loses nothing; `gd.decrypt`
# loses the first 16 plaintext bytes to CBC, which hides member slot 1 and the leader. With the
# exact read every slot is present and THE LEADER IS ON THE WIRE (`ev["leader"]`, see `_leader`).
#
#   proxyenv/Scripts/python deck_oracle.py <capture.mitm> [more.mitm ...]
#   proxyenv/Scripts/python deck_oracle.py --check     compare against dumps/cards_import_draft.json
import json, math, os, re, struct, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from master_db import decode_record

OUT = os.path.join(HERE, "dumps", "deck_oracle.json")
DRAFT = os.path.join(HERE, "dumps", "cards_import_draft.json")
CARD_RE = re.compile(rb"card-\d{5}-\d-[a-z]{4}-\d{4}-\d{2}")

# Field numbers confirmed against the game's own Unit Score Details panel; the names are the
# game's own, from LiveDeckEvaluation in the game's proto definitions. Where the proto and the UI
# disagree the UI label is kept in brackets, because that is what the player reads on screen --
# notably field 5, which the UI calls "Outfit Skill" and the proto calls
# live_leader_skill. Same slot: the leader's outfit IS the leader skill.
POWER = {4: "card_potential_parameter [Member Parameter]",
         5: "up_by_live_leader_skill [Outfit Skill]",
         6: "up_by_live_passive_skill [Passive Skill]",
         7: "up_by_skill_tree [Holomem Board]",
         8: "up_by_poster_collect [Memory Bonus]",
         9: "card_training_evaluation [Member Power-Up]"}
BONUS = {4: "by_live_active_skill [Active Skill]",
         5: "by_live_leader_skill [Outfit Skill]",
         6: "by_live_passive_skill [Passive Skill]",
         7: "by_skill_tree [Holomem Board]",
         8: "by_live_special_skill [Special Skill]"}
K = 2.03734          # Unit Score = Overall Power x (1 + Score Bonus) x K
TIER = {"Low": 0.37, "Medium": 0.46, "High": 0.55}


def evaluations(path):
    """Every deck evaluation in a capture, deduplicated, in order of appearance.

    **Reads the payload EXACTLY (`iter_grpc_exact`).** `gd.decrypt` loses the first 16
    plaintext bytes to CBC, which would make slot 1 look always missing and hide the leader:

      * slot 1 sits inside those 16 bytes;
      * the leader is not a CARD id but a (character, costume) PAIR, at `9999.1.282387948.9`.
        See `_leader`.

    `iter_grpc` + `decrypt` stays as the fallback for any body `decrypt_exact` cannot open, so
    such a capture still parses -- degraded, and it says so in `ev["decrypt"]`.

    **Each row carries its own capture-time card levels (`ev["levels"]`).** The
    server's Member Parameter is a frozen number: it is what the account's cards were worth AT
    THE INSTANT OF THE CAPTURE. Scoring it against `state/collection.json` -- a file that is
    re-merged from a live `User/Get` on every account refresh -- compares two different instants,
    and every level-up since the capture reads as a model error. In one sample, 27 of 27 decks
    reproduce the wire figure exactly under their own capture's levels; scored against a
    collection eleven hours newer, 15 of 27 do, with residuals of +253 and +505 that are two
    5-star cards going 69 -> 70. A capture that carries a deck also carries exactly one
    `User/Get`, so the levels are always recoverable."""
    seen, out, userdata = set(), [], None
    for rpc, pt, how in _plaintexts(path):
        if "User/Get" in rpc and userdata is None:
            userdata = pt
            continue
        if "GetDeck" not in rpc and "SaveDeck" not in rpc:
            continue
        ev = _evaluation(pt)
        if not ev or ev["unit_score"] in seen:
            continue
        seen.add(ev["unit_score"])
        ev["cards"] = _cards_with_stats(pt)
        ev["member_power"] = _member_power(pt)
        # The reply's own checksum: the five per-member shares sum to Overall Power. None when the list wasn't fully recovered, so a lossy plaintext reads as
        # "unchecked", never as a false pass.
        mp = ev["member_power"]
        ev["member_power_sum_ok"] = (
            sum(mp.values()) == ev["overall_power"] if len(mp) == 5 else None)
        ev["leader"] = _leader(pt)
        ev["decrypt"] = how
        ev["rpc"] = rpc
        ev["capture"] = os.path.basename(path)
        out.append(ev)

    levels = _levels(userdata) if userdata is not None else None
    for ev in out:
        # Only the cards this deck names. The whole collection would be ~100 near-identical
        # entries per row for no gain -- what the comparison needs is these six.
        ev["levels"] = None if levels is None else {
            cid: levels[cid] for cid in _card_ids(ev["cards"]) if cid in levels
        }
    return out


def _card_ids(cards):
    """The card ids in an `ev["cards"]` block, whichever shape it is in."""
    out = []
    for _slot, entry in sorted((cards or {}).items()):
        cid = entry if isinstance(entry, str) else (entry or {}).get("card") or ""
        if cid:
            out.append(cid)
    return out


def _levels(userdata):
    """`{card id: {level, bloom, limit_break}}` from a capture's own `User/Get`.

    The same decode `account_snapshot.py` uses for the live call, so the levels a row is judged
    against are produced by exactly the code that produces `state/collection.json` -- a second
    implementation here could drift from it and the comparison would blame the model."""
    import account_snapshot as snap
    snapshot = snap.decode_snapshot(userdata)
    snap.resolve_levels(snapshot)
    entries = {}
    for c in snapshot.get("cards", []):
        cid = c.get("card_id")
        if not cid:
            continue
        e = {}
        if "level" in c:
            e["level"] = c["level"]
        e["bloom"] = int(c.get("bloom") or 0)
        e["limit_break"] = int(c.get("limit_break") or 0)
        entries[cid] = e
    return entries


def _plaintexts(path):
    """`(rpc, plaintext, how)` per gRPC response in a capture, exact where possible.

    `iter_grpc_exact` DROPS a body `decrypt_exact` cannot open, which would turn a decrypt
    regression into a silently shorter deck list. This walks the flows once and degrades per body
    instead, so a failure shows up as `how == "cbc-lossy"` in the output rather than as an absence."""
    import grpc_decrypt as gd
    from mitmproxy import io as mio
    with open(path, "rb") as fh:
        for fl in mio.FlowReader(fh).stream():
            if fl.type != "http":
                continue
            resp = getattr(fl, "response", None)
            if not resp or "grpc" not in resp.headers.get("content-type", ""):
                continue
            body = resp.raw_content or b""
            got = gd.decrypt_exact(body)
            if got:
                yield fl.request.path, got[1], "exact"
                continue
            env = gd.parse_envelope(body)
            if env:
                yield (fl.request.path,
                       gd.decrypt(env[1], bytes.fromhex(gd.KEY_HEX)), "cbc-lossy")


# The leader, on the wire. `9999.1.282387948.9` is the leader's character and it VARIES per deck.
# `9999.1.282387948.11` is constant across captures of one account -- the home character,
# NOT the leader. Reading .11 as the leader is the one way to get this wrong quietly.
LEADER_CHR = (9999, 1, 282387948, 9)
LEADER_COSTUME = (9999, 1, 370704040)      # {2: chr, 3: cos-..., 4: sdcos-...}, non-default only


def _leader(pt):
    """`{"character": "chr-…", "costume": "cos-…"|None}`, or None if the payload has neither.

    The leader is a (character, outfit) PAIR, which is why
    `LiveInGameDeckData` carries both and why no leader CARD id exists to search for. The costume
    record is absent when the leader wears its default outfit, so `costume` is None far more often
    than `character` is."""
    def dig(rec, fields):
        cur = rec
        for f in fields:
            vals = cur.get(f)
            if not vals:
                return None
            v = vals[0]
            if f is fields[-1]:
                return v
            if not isinstance(v, bytes):
                return None
            try:
                cur = decode_record(v)
            except Exception:
                return None
        return None

    try:
        rec = decode_record(pt)
    except Exception:
        return None
    chr_id = dig(rec, LEADER_CHR)
    if isinstance(chr_id, bytes):
        chr_id = chr_id.decode("utf-8", "replace")
    if not (isinstance(chr_id, str) and chr_id.startswith("chr-")):
        return None
    out = {"character": chr_id, "costume": None}
    cos_rec = dig(rec, LEADER_COSTUME)
    if isinstance(cos_rec, bytes):
        try:
            sub = decode_record(cos_rec)
            cos = sub.get(3, [b""])[0]
            if isinstance(cos, bytes) and cos.startswith(b"cos-"):
                out["costume"] = cos.decode()
        except Exception:
            pass
    return out


def _evaluation(pt):
    """The {unit score, power block, bonus block} record.

    On an EXACT plaintext it is simply top-level field 2 -- `{1: unit score, 4: power, 5: bonus}`.
    The offset scan below is what a lossy CBC plaintext needs, because losing the first 16 bytes
    moves the record to an unknown offset; it is kept as the fallback, not as the primary."""
    try:
        rec = decode_record(pt)
        direct = _evaluation_record(rec.get(2, [None])[0])
        if direct:
            return direct
    except Exception:
        pass
    for off in range(0, 400):
        try:
            got = _evaluation_record(pt[off:])
        except Exception:
            continue
        if got:
            return got
    return None


def _evaluation_record(blob):
    """`{unit score, power, bonus}` from one candidate record, or None if it is not one.

    Every guard the offset scan needs is here, so the exact path and the scan agree by
    construction rather than by two copies of the same conditions staying in step."""
    if not isinstance(blob, (bytes, bytearray)):
        return None
    rec = decode_record(blob)
    if not (1 in rec and 4 in rec and 5 in rec):
        return None
    if not (isinstance(rec[4][0], bytes) and isinstance(rec[5][0], bytes)):
        return None
    power, bonus = decode_record(rec[4][0]), decode_record(rec[5][0])
    if not (1 in power and 1 in bonus):
        return None
    if not (isinstance(power[1][0], int) and isinstance(bonus[1][0], int)):
        return None
    if not isinstance(rec[1][0], int):
        return None
    return {"unit_score": rec[1][0],
            "power": {POWER[k]: v[0] for k, v in power.items() if k in POWER},
            "overall_power": power[1][0],
            "bonus": {BONUS[k]: v[0] for k, v in bonus.items() if k in BONUS},
            "score_bonus_permil": bonus[1][0]}


def _member_power(pt):
    """Per-member Overall-Power contribution, keyed by slot.

    The ids-only member list at top-level field 1 encodes each entry `{1: slot, 2: card id,
    3: power}` -- and field 3 is that member's share of Overall Power. The five shares sum to
    Overall Power EXACTLY on every deck read, so it is the reply's OWN per-card decomposition of
    the power model -- free per-card ground truth on every capture, and a checksum on the
    team-sum identity. Returns `{}` on a lossy plaintext where field 1 is out of
    reach (the sum check simply won't fire), mirroring `_cards_structured`."""
    try:
        entries = decode_record(pt).get(1, [])
    except Exception:
        return {}
    found = {}
    for raw in entries:
        if not isinstance(raw, bytes):
            continue
        try:
            e = decode_record(raw)
        except Exception:
            continue
        slot, power = e.get(1, [None])[0], e.get(3, [None])[0]
        if isinstance(slot, int) and isinstance(power, int):
            found[slot] = power
    return {f"slot{s}": found[s] for s in sorted(found)}


def _cards_with_stats(pt):
    """The five member slots, keyed by slot number.

    Each deck entry is encoded `{1: slot, 2: card id, ...}`, which on the wire is the literal
    byte run `08 <slot> 12 19 card-...`. Matching that run is what makes this reliable, rather
    than scraping loose ids: it recovers the slot index too, so a truncated or reordered list is
    visible instead of silently collapsing.

    Two such lists appear per response -- one ids-only at top-level field 1, one with the stat
    triple at `3.100`. The second is the one that carries numbers, so it wins.

    On a lossy CBC plaintext slot 1 of the first list is missing: it sits in the 16 plaintext
    bytes CBC cannot recover. `decrypt_exact` recovers them, so the structured read below sees all
    five. The regex scan is kept for lossy plaintexts only.

    The LEADER IS NOT IN THIS LIST -- correctly, it is not a member. It is still on the payload,
    as a (character, costume) pair rather than a card id. See `_leader`. Overall Power is the sum
    of these five."""
    structured = _cards_structured(pt)
    if len(structured) == 5:
        return structured
    found = {}
    for m in CARD_RE.finditer(pt):
        i = m.start()
        pre = pt[i - 4:i]
        if len(pre) != 4 or pre[0] != 0x08 or pre[2] != 0x12:
            continue                       # not a `{1: slot, 2: id}` entry
        slot = pre[1]
        rec = None
        for span in (120, 90, 60):
            try:
                rec = decode_record(pt[i - 4:i + span])
            except Exception:
                continue
            break
        stats = []
        if rec:
            stats = [rec[f][0] for f in (3, 4, 5)
                     if f in rec and isinstance(rec[f][0], int) and 0 < rec[f][0] < 100000]
        entry = {"card": m.group().decode()}
        if len(stats) == 3:
            entry.update(performance=stats[0], technique=stats[1], sense=stats[2],
                         total=sum(stats))
        if slot not in found or "total" in entry:
            found[slot] = entry
    return {f"slot{s}": found[s] for s in sorted(found)}


def _cards_structured(pt):
    """The five slots read out of the message tree rather than scraped: `3.100` is a repeated
    `{1: slot, 2: card id, 3: performance, 4: technique, 5: sense}`.

    Returns `{}` unless it finds a clean set, so the caller falls back to the regex scan on a
    lossy plaintext instead of reporting a partial deck as if it were whole."""
    try:
        rec = decode_record(pt)
        block = rec.get(3, [None])[0]
        if not isinstance(block, bytes):
            return {}
        entries = decode_record(block).get(100, [])
    except Exception:
        return {}
    found = {}
    for raw in entries:
        if not isinstance(raw, bytes):
            continue
        try:
            e = decode_record(raw)
        except Exception:
            continue
        slot, cid = e.get(1, [None])[0], e.get(2, [None])[0]
        if not isinstance(slot, int) or not isinstance(cid, bytes):
            continue
        entry = {"card": cid.decode("utf-8", "replace")}
        stats = [e.get(f, [None])[0] for f in (3, 4, 5)]
        if all(isinstance(s, int) for s in stats):
            entry.update(performance=stats[0], technique=stats[1], sense=stats[2],
                         total=sum(stats))
        # 3.100.6/7/8 are per-member active-skill permil modifiers (fixed32 floats), emitted ONLY
        # for cards that carry an active skill: effect-up, activation-probability-up, cooltime-
        # shorten -- the server's OWN occupancy inputs to the Active bonus line (e.g.
        # card-00031-5 -> {80.0, 231.0, 40.0}). Decoded here so the Active model can be fed the
        # real per-member terms instead of tile-derived guesses. Absent field -> 0 default (sparse).
        # The value is stored UNROUNDED, and the raw fixed32 word is kept beside it under
        # `<key>_bits`. Rounding to 3 decimals would erase between ~8 and ~262 float32 ULPs
        # depending on magnitude -- exactly the sub-permil deviation a float accumulation on
        # the server would show. In the captures seen so far every word is an exact integer
        # permil (0x44398000 = 742.0, low mantissa clear); keeping the bits stops the harness
        # from silently discarding the evidence if that ever changes.
        floats = {6: "active_effect_up", 7: "active_prob_up", 8: "active_cool_shorten"}
        for f, key in floats.items():
            v = e.get(f, [None])[0]
            if isinstance(v, int):
                entry[key] = struct.unpack("<f", v.to_bytes(4, "little"))[0]
                entry[key + "_bits"] = v
        found[slot] = entry
    return {f"slot{s}": found[s] for s in sorted(found)}


def check(ev, draft):
    """What the extracted card data predicts, next to what the server said.

    Only the Active Skill component is modelled here, and only in shape: sum over the members of
    magnitude x duty cycle x activation probability. Everything else prints '--'."""
    ids = [e["card"] for e in ev["cards"].values()]
    if len(ids) != 5:
        return None, f"{len(ids)} member slots, expected 5 -- not comparing"
    missing = [i for i in ids if i not in draft]
    if missing:
        return None, f"{len(missing)} slot card(s) not in the draft -- not comparing"

    # Members only, and correctly so: the leader contributes its
    # Outfit Skill and its red Leader-scope nodes and NOTHING else -- no ACTIVE, SPECIAL or
    # PASSIVE. A leader that also holds a member slot contributes those as that member, once.
    #
    # A BRACKET, not a number. Every skill has exactly two levels and which one a given card is at
    # depends on the account's Bloom progress, which the payload does not carry. Quoting a single
    # figure would be arbitrary precision, so both ends are computed and the server's value is
    # simply checked for being inside. An interval that CONTAINS the truth is worth more than a
    # point estimate that happens to sit near it.
    levels = _active_levels()
    out = []
    for lvl in ("1", "2"):
        total = 0.0
        for i in ids:
            row = levels.get((_card_key(i), lvl))
            if row:
                mag, dur, cool, prob = row
                total += mag * dur / cool * prob
        out.append(round(total * 10))
    return {"Active Skill": tuple(out)}, None


def _card_key(card_id):
    """`card-00036-5-uniq-0029-00` -> `card-00036-5`, the prefix skill ids are keyed by."""
    return "-".join(card_id.split("-")[:3])


_LEVELS = None


def _active_levels():
    """(card prefix, level) -> (magnitude %, duration s, cooldown s, probability).

    Straight out of `LiveActiveSkillLevel`; no value here is carried over from the card draft, so
    the bracket is the game's own numbers at each of the two skill levels."""
    global _LEVELS
    if _LEVELS is not None:
        return _LEVELS
    import re
    import master_cache as mc
    _LEVELS = {}
    for r in mc.rows("LiveActiveSkillLevel"):
        sid = r["live_active_skill_id"]
        m = re.match(r"live_active_skill-(card-\d{5}-\d)", sid)
        if not m:
            continue
        d = r["data"]
        eff = d.get(4, [b""])[0]
        eff = eff.decode() if isinstance(eff, bytes) else str(eff)
        mag = re.search(r"(\d+)$", eff)
        if not mag:
            continue
        _LEVELS[(m.group(1), r["level"])] = (int(mag.group(1)) / 10,
                                             d.get(10, [0])[0] / 1000,
                                             d.get(7, [0])[0] / 1000,
                                             d.get(8, [0])[0] / 1000)
    return _LEVELS


def summary(evs, draft):
    """One row per deck: the duty-cycle bracket against what the server actually said.

    The point of collecting decks at all. A per-deck dump cannot show whether the server's Active
    Skill figure is a function of the duty sum -- that only appears when several decks spanning a
    wide range sit in one table, which is why the sampling is designed rather than opportunistic."""
    rows = []
    for n, ev in enumerate(evs, 1):
        pred, why = check(ev, draft)
        got = ev["bonus"].get("by_live_active_skill [Active Skill]", 0)
        if pred:
            lo, hi = pred["Active Skill"]
            rows.append((n, lo, hi, got))
        else:
            rows.append((n, None, None, got))
    if not rows:
        return
    print("\n=== Active Skill: predicted bracket vs the server, all decks ===")
    print(f"  {'deck':>4} {'lv1':>8} {'lv2':>8} {'server':>8}  {'server/lv1':>10} {'inside?':>8}")
    for n, lo, hi, got in rows:
        if lo is None:
            print(f"  {n:>4} {'--':>8} {'--':>8} {got / 10:>7.1f}%  (composition not readable)")
            continue
        ratio = got / lo if lo else 0
        inside = "yes" if lo <= got <= hi else "NO"
        print(f"  {n:>4} {lo / 10:>7.1f}% {hi / 10:>7.1f}% {got / 10:>7.1f}%  {ratio:>10.3f} {inside:>8}")
    print("  A constant server/lv1 across a wide duty range means the relationship is linear;")
    print("  drift means it is not, and the shape of the drift says what is missing.")


def report(evs, draft=None):
    for n, ev in enumerate(evs, 1):
        # CEILING, not round -- residue exactly 0 on every deck checked.
        # `round` with a <= 1 tolerance would mask BOTH the rounding rule and a
        # slightly biased K; an exact identity is the far stronger check, so this
        # demands 0 rather than <= 1.
        recomputed = math.ceil(ev["overall_power"] * (1 + ev["score_bonus_permil"] / 1000) * K)
        agree = "ok" if recomputed == ev["unit_score"] else "MISMATCH"
        print(f"\n=== deck {n}  ({ev['rpc'].rsplit('/', 1)[-1]}) ===")
        print(f"  Unit Score        {ev['unit_score']:>10}"
              f"   formula 9.1 gives {recomputed} -- {agree}")
        print(f"  Overall Power     {ev['overall_power']:>10}")
        print(f"  Score Bonus       {ev['score_bonus_permil'] / 10:>10.1f} %")
        print(f"  cards with stats  {len(ev['cards']):>10}")
        for label, block, unit in (("power", ev["power"], ""), ("bonus", ev["bonus"], " %")):
            for k2, v in block.items():
                shown = f"{v / 10:.1f}" if label == "bonus" else str(v)
                print(f"    {label:5} {k2:22} {shown:>10}{unit}")
        if draft is None:
            continue
        pred, why = check(ev, draft)
        if why:
            print(f"  model: {why}")
            continue
        for k2, (lo, hi) in pred.items():
            got = ev["bonus"].get(k2, 0)
            inside = "server INSIDE the bracket" if lo <= got <= hi else (
                f"server OUTSIDE by {(got - lo if got < lo else got - hi) / 10:+.1f}")
            print(f"  model {k2:22} skill lv1 {lo / 10:>5.1f} % .. lv2 {hi / 10:>5.1f} %"
                  f"   server {got / 10:>5.1f} %   {inside}")


def _merge(evs):
    """One entry per deck across all captures, keeping the RICHEST record for each.

    `evaluations` deduplicates within a capture, so the same deck seen in two captures arrives
    twice -- and the two are not interchangeable: a `SaveDeck` response carries the evaluation but
    **not** the leader, while the `GetDeck` for the same deck carries both. Taking whichever came
    first silently loses the leader on exactly the decks that have one, which is the same failure
    `_cards_with_stats` already guards against by letting the list that carries numbers win.

    Same rule here: more information wins, ties go to the first seen."""
    def rank(e):
        return (1 if e.get("leader") else 0,
                1 if (e.get("leader") or {}).get("costume") else 0,
                len(e.get("cards") or {}))

    best = {}                       # dict preserves first-seen order across replacements
    for e in evs:
        u = e["unit_score"]
        if u not in best or rank(e) > rank(best[u]):
            best[u] = e
    return list(best.values())


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    # EVERY capture, not just the ones named live/deck. A name filter would silently drop
    # captures such as the marathon ones, which carry many deck evaluations -- and a
    # no-argument run would rebuild a SHORTER oracle than the committed one without saying so.
    caps = args or sorted(os.path.join(HERE, "dumps", f)
                          for f in os.listdir(os.path.join(HERE, "dumps"))
                          if f.endswith(".mitm"))
    evs = []
    for c in caps:
        try:
            evs += evaluations(c)
        except Exception as exc:
            print(f"  {os.path.basename(c)}: {type(exc).__name__}: {exc}")
    evs = _merge(evs)
    withleader = sum(1 for e in evs if e.get("leader"))
    print(f"{len(evs)} distinct deck evaluations from {len(caps)} capture(s); "
          f"{withleader} carry a leader")

    draft = None
    if "--check" in sys.argv and os.path.exists(DRAFT):
        with open(DRAFT, encoding="utf-8") as fh:
            draft = {c["id"]: c for c in json.load(fh)["cards"]}
    report(evs, draft)
    if draft:
        summary(evs, draft)

    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(evs, fh, indent=1, ensure_ascii=False)
    print(f"\nwritten to {OUT}")


if __name__ == "__main__":
    main()
