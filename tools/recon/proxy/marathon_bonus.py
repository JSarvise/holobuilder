# Read the EVENT BONUS rate tables out of a capture -- every rate, for every card, character,
# attribute and bloom level, as the server itself states them.
#
# WHY. The bonus types are named and typed in the client: `Vision.OutGame!
# EventBonusCardRate` holds exactly four dictionaries -- CardBonusRates (Member Bonus),
# CharacterBonusRates (holomem Bonus), AttributeRates (an attribute bonus the in-game tip does not
# even mention) and PotentialUpgradeRates (Bloom Bonus). None of them is in the master tables. They
# arrive on the wire, and `CardMarathonLiveEventBonusRatioCache._cachedBonuses` types the transport:
# `List<MarathonTopResponse...MarathonLiveMarathonScoreBonus>`.
#
# WHAT THIS READS. `Marathon/Top` is the RPC that carries that list -- but the SAME message type
# is also nested in `Event/ListEventInfo`
# (`EventListEventInfoResponse.EventInfo.MarathonInfo.MarathonChapter.MarathonLiveMarathonScoreBonus`).
# So this module reads BOTH, and the rate tables are recoverable from an `Event/ListEventInfo`
# capture alone. Run it on a fresh `Marathon/Top` capture to confirm the two agree.
#
# THE ONE THING TO BE CAREFUL ABOUT. A row is a PATTERN, not a record: an unset field is a
# WILDCARD, not "must equal zero". `CalculateBonusRatioPermil` @0x614b5e0 SUMS
# `marathonScoreQuantityUpPermilUp_` over every row whose set fields all match the card. So a row
# carrying only `character_id` fires on ANY card of that holomem, and a row carrying only
# {rarity, bloom} fires on every card at that rarity and bloom. Reading a row as a conjunction of
# all eight fields would make almost every row unmatchable and the whole bonus read as zero.
#
# Decryption goes through `decrypt_exact` (nonce-derived IV, nothing lost), NOT `gd.decrypt` --
# the latter drops the first 16 plaintext bytes to CBC, and that loss can read as false facts
# about the game. The lossy path is kept only as a per-body fallback and is flagged in the output
# when it fires.
#
#   proxyenv/Scripts/python marathon_bonus.py                    every dumps/*.mitm
#   proxyenv/Scripts/python marathon_bonus.py <capture.mitm> ...
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from master_db import decode_record  # noqa: E402  (kept for accepted rows; see _strict)

OUT = os.path.join(HERE, "dumps", "marathon_bonus.json")

# Attribute ids as this repo already keys them (import_to_cards.py).
ATTRIBUTE = {1: "Cute", 2: "Pure", 3: "Happy"}
# CardRarity is NOT the enum's ordinal. On the wire the rarity field carries 3/4/5
# literally, matching the star count, so no mapping table is applied to it.

# --- the message templates -------------------------------------------------------------
#
# Field numbers are the DECLARATION ORDER of the generated C# classes in the IL2CPP field map
# (`MarathonLiveMarathonScoreBonus` groupId_ @0x18, number_ @0x20, characterId_ @0x28, cardId_
# @0x30, cardAttributeType_ @0x38, cardRarity_ @0x3c, cardPotentialUpgradeCount_ @0x40,
# marathonScoreQuantityUpPermilUp_ @0x48). That 1..N mapping is inferred from the layout -- so it
# is never trusted blind: EVERY row of a group must validate against the template's own type and
# range rules before the group is accepted and named. A repeated field that validates under no
# template is simply not claimed -- the walk descends through it and it is never reported as a
# bonus table. On real captures the inference holds: `chr-` ids land in `character_id`, `card-`
# ids in `card_id`, and the rarity/bloom rows reproduce the independently hand-read bloom table
# (3* 1/1/2/2/3, 4* 6/7/8/9/10, 5* 15/18/22/26/30) exactly.
#
# Each slot is (name, kind); kind drives validation:
#   "grp"  ASCII group id      "int"  varint       "permil" varint > 0
#   "chr"  b"chr-..."          "card" b"card-..."  "music" b"m...."
#   "attr" varint in ATTRIBUTE "rar"  varint in {3,4,5}   "bloom" varint 0..10
TEMPLATES = {
    "live": {1: ("group_id", "grp"), 2: ("number", "int"), 3: ("character_id", "chr"),
             4: ("card_id", "card"), 5: ("attribute", "attr"), 6: ("rarity", "rar"),
             7: ("bloom", "bloom"), 8: ("permil", "permil")},
    "music": {1: ("group_id", "grp"), 2: ("music_id", "music"), 3: ("number", "int"),
              4: ("character_id", "chr"), 5: ("card_id", "card"), 6: ("attribute", "attr"),
              7: ("rarity", "rar"), 8: ("bloom", "bloom"), 9: ("permil", "permil")},
    "minigame": {1: ("group_id", "grp"), 2: ("number", "int"), 3: ("card_id", "card"),
                 4: ("rarity", "rar"), 5: ("bloom", "bloom"), 6: ("permil", "permil")},
}
# What each template's permil MEANS, for the printout. The live one raises Event Pt / Event Badge
# earned; the music one is `Event Score Up`, a different mechanic on the SCORE.
MEANING = {"live": "Event Pt / Event Badge up (the Event Bonus)",
           "music": "Event Score Up -- raises the SCORE on that song, not the points",
           "minigame": "minigame Event Pt up"}


def _strict(b):
    """protobuf -> {field: [values]}, or None if `b` is not a well-formed message.

    `master_db.decode_record` is deliberately lax -- it stops on a bad tag and returns what it had,
    which is right for a payload known to be a message and wrong for TESTING whether a blob is one.
    Every candidate row here is an arbitrary byte run pulled out of a tree walk, so it needs a
    parser that says no: unknown wire type, field 0, a length that runs off the end and any
    trailing byte all reject."""
    p, out = 0, {}
    while p < len(b):
        tag, p = _varint(b, p)
        if tag is None:
            return None
        fn, wt = tag >> 3, tag & 7
        if fn == 0:
            return None
        if wt in (0, 2):
            v, p = _varint(b, p)
            if v is None:
                return None
            if wt == 2:
                if p + v > len(b):
                    return None
                v, p = b[p:p + v], p + v
        elif wt == 5:
            if p + 4 > len(b):
                return None
            v, p = int.from_bytes(b[p:p + 4], "little"), p + 4
        elif wt == 1:
            if p + 8 > len(b):
                return None
            v, p = int.from_bytes(b[p:p + 8], "little"), p + 8
        else:
            return None
        out.setdefault(fn, []).append(v)
    return out


def _varint(b, p):
    s = r = 0
    while p < len(b):
        x = b[p]
        p += 1
        r |= (x & 0x7F) << s
        if not x & 0x80:
            return r, p
        s += 7
        if s > 63:
            return None, p
    return None, p


def _ascii(v):
    return isinstance(v, bytes) and v and all(0x20 <= c < 0x7F for c in v)


def _ok(kind, v):
    if kind == "grp":
        return _ascii(v) and len(v) > 4
    if kind == "chr":
        return _ascii(v) and v.startswith(b"chr-")
    if kind == "card":
        return _ascii(v) and v.startswith(b"card-")
    if kind == "music":
        return _ascii(v) and v.startswith(b"m")
    if kind == "int":
        return isinstance(v, int) and v < 1 << 32
    if kind == "permil":
        return isinstance(v, int) and 0 < v < 1_000_000
    if kind == "attr":
        return v in ATTRIBUTE
    if kind == "rar":
        return v in (3, 4, 5)
    if kind == "bloom":
        return isinstance(v, int) and v <= 10
    return False


def _as_row(rec, tmpl):
    """One decoded record against one template -> the named row, or None if it does not fit."""
    row = {}
    for fn, vals in rec.items():
        if fn not in tmpl or len(vals) != 1:
            return None
        name, kind = tmpl[fn]
        if not _ok(kind, vals[0]):
            return None
        v = vals[0]
        row[name] = v.decode() if isinstance(v, bytes) else v
    # group id and the permil are the two fields every real row carries: the group id because it
    # is what ties the row to a chapter, the permil because a row worth zero would be a no-op.
    if "group_id" not in row or "permil" not in row:
        return None
    return row


def groups(pt):
    """Every repeated bonus-row field anywhere in a plaintext -> {(group_id, kind): [rows]}.

    Walks the message tree rather than looking the list up by field number, because the same row
    type sits at different paths in different RPCs -- `1.14.4.21` in `Event/ListEventInfo`,
    somewhere else in `Marathon/Top` -- and a path hardcoded from one capture would silently find
    nothing in the other."""
    found = {}
    _walk(pt, found, 0)
    return found


def _walk(blob, found, depth):
    if depth > 12:
        return
    rec = _strict(blob)
    if not rec:
        return
    for vals in rec.values():
        raw = [v for v in vals if isinstance(v, bytes) and len(v) > 4]
        if not raw:
            continue
        decoded = [_strict(v) for v in raw]
        if all(decoded):
            for kind, tmpl in TEMPLATES.items():
                rows = [_as_row(d, tmpl) for d in decoded]
                if all(rows) and any("group_id" in r for r in rows):
                    key = (rows[0]["group_id"], kind)
                    if all(r["group_id"] == key[0] for r in rows):
                        found.setdefault(key, []).extend(rows)
                        break
            else:
                for v in raw:
                    _walk(v, found, depth + 1)
            continue
        for v in raw:
            _walk(v, found, depth + 1)


def tables(rows):
    """The rows of one group -> the four dictionaries `EventBonusCardRate` declares.

    Keyed exactly as the client keys them: the card/character/attribute ones by RATE (permil), the
    bloom one by rarity then bloom. `unmatched` catches any row shape not covered by the four --
    it should stay empty, and if it does not that is a finding, not a rounding error."""
    card, chrs, attr, bloom, other = {}, {}, {}, {}, []
    for r in rows:
        p = r["permil"]
        has = {k for k in ("character_id", "card_id", "attribute", "rarity", "bloom") if k in r}
        if has == {"card_id"}:
            card.setdefault(p, []).append(r["card_id"])
        elif has == {"character_id"}:
            chrs.setdefault(p, []).append(r["character_id"])
        elif has == {"attribute"}:
            attr.setdefault(p, []).append(ATTRIBUTE[r["attribute"]])
        elif has == {"rarity", "bloom"}:
            bloom.setdefault(r["rarity"], {})[r["bloom"]] = p
        else:
            other.append(r)
    return {"CardBonusRates": card, "CharacterBonusRates": chrs, "AttributeRates": attr,
            "PotentialUpgradeRates": bloom, "unmatched": other}


def _plaintexts(path):
    """`(rpc, plaintext, how)` per gRPC response in a capture, exact where possible.

    Degrades per body rather than dropping it, so a decrypt regression shows up as
    `how == "cbc-lossy"` in the report instead of as a silently shorter list of groups."""
    if str(path).endswith(".bin"):
        # account_snapshot.py --refresh-events: an already-decrypted Event/ListEventInfo body
        with open(path, "rb") as fh:
            yield "/rpc.api.Event/ListEventInfo", fh.read(), "exact"
        return
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
                yield fl.request.path, gd.decrypt(env[1], bytes.fromhex(gd.KEY_HEX)), "cbc-lossy"


def collect(paths):
    """Every bonus group across every capture, merged and deduplicated.

    A group seen in five responses is one group: rows are keyed by `_row_key` -- the song (on a
    song-bonus row) and the server's `number` -- so a duplicate response adds nothing, a newer
    capture of the same row replaces the older, and a genuinely different row is never
    swallowed. `number` alone is NOT a row's identity: on a song-bonus group it counts within
    each song (every solo song's single row is #1), and keying by it keeps only the last song's
    row (a 6-song "normal" event would collapse to 2). Two different rows of ONE response under
    one key would be the same loss again, so they are collected in `COLLISIONS` (main fails)."""
    merged, sources, lossy = {}, {}, set()
    for p in paths:
        try:
            for rpc, pt, how in _plaintexts(p):
                for key, rows in groups(pt).items():
                    seen = merged.setdefault(key, {})
                    here: dict = {}
                    for r in rows:
                        rk = _row_key(r, len(here))
                        if rk in here and here[rk] != r:
                            COLLISIONS.append((key[0], rk, here[rk], r))
                        here[rk] = r
                    seen.update(here)
                    sources.setdefault(key, set()).add(rpc.rsplit("/", 1)[-1])
                    if how != "exact":
                        lossy.add(key)
        except Exception as exc:
            print(f"  {os.path.basename(p)}: {type(exc).__name__}: {exc}")
    out = {}
    for key, byno in merged.items():
        out[key] = {"rows": [byno[n] for n in sorted(byno)],
                    "rpcs": sorted(sources[key]),
                    "decrypt": "cbc-lossy" if key in lossy else "exact"}
    return out


# rows of one response that shared a `_row_key` but differ: `(group, key, first, second)`
COLLISIONS: list = []


def _row_key(row: dict, position: int) -> tuple:
    """A bonus row's identity within its group: `(music id or "", number)`."""
    return (str(row.get("music_id") or ""), row.get("number", position))


def report(found):
    for (gid, kind), info in sorted(found.items()):
        rows = info["rows"]
        t = tables(rows)
        print(f"\n=== {gid}")
        print(f"    {kind}: {MEANING[kind]}")
        print(f"    {len(rows)} rows, from {', '.join(info['rpcs'])}, decrypt={info['decrypt']}")
        if kind != "live":
            for r in rows:
                print("      " + "  ".join(f"{k}={v}" for k, v in r.items() if k != "group_id"))
            continue
        print("    -- CharacterBonusRates (holomem Bonus): any card of these holomem")
        _rate_block(t["CharacterBonusRates"])
        print("    -- CardBonusRates (Member Bonus): these specific cards only")
        _rate_block(t["CardBonusRates"])
        print("    -- AttributeRates (unnamed in the in-game tip)")
        _rate_block(t["AttributeRates"])
        print("    -- PotentialUpgradeRates (Bloom Bonus): rarity -> bloom -> rate")
        if not t["PotentialUpgradeRates"]:
            print("       (none)")
        for rar in sorted(t["PotentialUpgradeRates"]):
            per = t["PotentialUpgradeRates"][rar]
            cells = "  ".join(f"{b}:{per[b] / 10:g}%" for b in sorted(per))
            print(f"       {rar}*  {cells}")
        if t["unmatched"]:
            print("    -- rows matching NO known shape (a finding, not noise):")
            for r in t["unmatched"]:
                print(f"       {r}")


def _rate_block(d):
    if not d:
        print("       (none)")
        return
    for permil in sorted(d, reverse=True):
        ids = ", ".join(sorted(d[permil]))
        print(f"       {permil / 10:>6g}%  {ids}")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    live = os.path.join(HERE, "dumps", "event_info_live.bin")
    caps = args or sorted(glob.glob(os.path.join(HERE, "dumps", "*.mitm")))
    if not args and os.path.exists(live):
        caps.append(live)
    if not caps:
        print("no captures given and none in dumps/")
        return
    print(f"reading {len(caps)} capture(s)")
    found = collect(caps)
    if not found:
        print("\nNO BONUS GROUPS FOUND.")
        print("Either no event was open when the capture was taken, or the client served the")
        print("event screen from its cache. See tools/recon/MARATHON.md for the click path that")
        print("forces Marathon/Top to be issued.")
        return
    report(found)

    # The groups the server lists as open RIGHT NOW (the live ListEventInfo fetch), so the
    # optimiser can pick the current event instead of guessing from the newest capture.
    current = sorted({gid for gid, _kind in collect([live])}) if os.path.exists(live) else []
    event_songs = _event_songs(live) if os.path.exists(live) else {}
    _report_songs(event_songs, found)
    payload = {"current_group_ids": current,
               "live_fetched_at": os.path.getmtime(live) if os.path.exists(live) else None,
               # each open chapter's songs as the server lists them (its per-song ranking,
               # field 17): every one earns Event Pt, with or without a song-score bonus row
               "event_songs": event_songs,
               "groups": [{"group_id": gid, "kind": kind, "rpcs": info["rpcs"],
                           "decrypt": info["decrypt"], "rows": info["rows"],
                           "tables": tables(info["rows"])}
                          for (gid, kind), info in sorted(found.items())]}
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1, ensure_ascii=False)
    print(f"\nwritten to {OUT}")
    if COLLISIONS:
        print(f"\n!! {len(COLLISIONS)} bonus row(s) shared a row key with a DIFFERENT row of the "
              "same response -- one of each pair is lost. The row identity (`_row_key`) needs "
              "another field:")
        for gid, rk, first, second in COLLISIONS[:10]:
            print(f"   {gid} {rk}: {first} / {second}")
        sys.exit(1)


def _event_songs(live: str) -> dict[str, list[str]]:
    """`chapter id -> [music ids]` of every marathon chapter in the live ListEventInfo body
    (its per-song ranking, `event_cutoffs.parse_event_info`'s `tiers.music`)."""
    import event_cutoffs  # noqa: PLC0415

    out = {}
    for _rpc, pt, _how in _plaintexts(live):
        for ch in event_cutoffs.parse_event_info(pt):
            songs = sorted(m for m in (ch["tiers"]["music"] or {}) if m and m != "?")
            if ch.get("chapter_id") and songs:
                out[ch["chapter_id"]] = songs
    return out


def _report_songs(event_songs: dict, found: dict) -> None:
    """Each chapter's songs against its song-score-bonus rows: a song of the event with no
    row is printed, so a decode or merge that loses songs cannot pass unseen again."""
    rows_by_chapter: dict[str, set] = {}
    for (gid, kind), info in found.items():
        if kind == "music":
            rows_by_chapter.setdefault(gid.split("-", 1)[1], set()).update(
                r.get("music_id") for r in info["rows"])
    for chapter, songs in sorted(event_songs.items()):
        have = rows_by_chapter.get(chapter, set())
        missing = [m for m in songs if m not in have]
        print(f"\n{chapter}: {len(songs)} event song(s), {len(songs) - len(missing)} with a "
              f"song-score bonus row" + (f"; NO row for {', '.join(missing)}" if missing else ""))


if __name__ == "__main__":
    main()
