"""Brute-force server-bonus extraction engine -> dumps/bonus_extract_db.jsonl (own account, MAIN).

WHY THIS TOOL EXISTS
--------------------
The deck screen makes the server return the COMPLETE Unit Score decomposition for a team --
the twelve numbers of the in-game "Unit Score Details" panel -- with no song, no play, no cost.
A saved-then-read deck is therefore a fully-labelled example: inputs (leader + costume + 5 cards)
and the game's OWN answer (every power line, every bonus line, per-member power shares, per-card
stats and the per-card active-skill occupancy floats). This tool acquires that decomposition
SYSTEMATICALLY, across designed experiment families, so the bonus formulas can be reversed to the
best achievable precision. It reads only the user's own account.

It does NOT re-implement the wire parse: login and envelope crypto come from account_snapshot, the
SaveDeck request builder and
`fire` from deck_probe, and every decode from deck_oracle.

FULL-RECORD SCHEMA (one JSONL row per deck)
-------------------------------------------
  spec_hash        canonical dedup key: sha1(leader|costume|music|members-in-slot-order)[:16]
  capture_index    monotonically-increasing integer across the whole DB
  captured_at      local ISO timestamp of the GetDeck read (selects the board revision)
  family           which experiment family generated this deck
  leader           chr-... (the leader character)
  costume          cos-... (the leader's costume)
  deck_number      the scratch slot the deck was saved to (NOT part of identity)
  music_id         music-... or null (these decks carry no song)
  members          [card_id, ...] the 5 (or fewer) members in slot order
  unit_score       server's Unit Score
  overall_power    server's Overall Power
  power            {member_parameter, outfit, board, passive, memory, power_up}  (Power lines)
  score_bonus_permil  server's total Score Bonus, permil
  bonus            {active, outfit, board, passive, special}                     (Bonus lines)
  member_power     {slot1: share, ...}  per-slot share of Overall Power (sums to overall_power)
  per_card         [{slot, card_id, performance, technique, sense,
                     active_effect_up, active_prob_up, active_cool_shorten}, ...]

EXPERIMENT FAMILIES (each is a generator of deck specs; the plan is dedup-checked vs the DB)
-------------------------------------------------------------------------------------------
  singles       every owned card as the lone member under a neutral (0-contribution) leader ->
                per-card baselines: stats, active occupancy, and each line's single-card value.
  green_offteam paired decks: a base 5-member team that OMITS a green-node-heavy benched unit,
                and the same team with that unit swapped IN. Green nodes are collection-wide
                (always on even benched), so the pair isolates the unit's member-scope delta
                against a constant green baseline. Units chosen from board_node_taxonomy.json.
  red_leader    the SAME 5 members with the LEADER rotated across every owned (character,costume)
                -> isolates leader-scope (red) board + costume contribution.
  blue_member   4 fixed members + a rotating 5th over characters differing in drawn blue nodes
                -> isolates member-scope (blue) board contribution.
  pairs         systematic 2-card decks (distinct characters) under a neutral leader -> the
                Active E[max] combine corpus, extending emax_*_db.
  random_teams  N seeded-random REALISTIC 6-card teams (owned leader + 5 distinct-character
                members), uniform over the owned pool so leaders/types/generations/rarities/
                cooldown intervals are MIXED -> the realistic decks the optimizer actually ranks,
                not the controlled single-pool decks the other families isolate. Seed 20260822,
                default N=1500 (--n override); dedup-checked vs the DB so it is resumable.

SAFETY RAILS (enforced in code, not by convention)
--------------------------------------------------
  * READ vs WRITE: only Live/SaveDeck writes; the evaluation is always READ back with Live/GetDeck.
    Both are temp-added to account_snapshot.ALLOWED IN MEMORY only; the on-disk read-only
    whitelist is never touched. No other RPC than Master/Get, Auth/Login, User/Get, SaveDeck, GetDeck.
  * SCRATCH SLOTS ONLY: SaveDeck targets a slot that is (a) genuinely free, or (b) a 'zz'-named
    scratch deck. `choose_slot` returns (None, reason) otherwise and a KEPT deck HARD-STOPS the run
    (see the guard in `resolve_slot`). A kept deck is never overwritten.
  * OWNED CARDS ONLY: every card in every spec is validated against data/owned.json before any
    SaveDeck; an unowned card is refused.
  * DISTINCT CHARACTERS: members are distinct characters and the leader's character is never also
    a member (the game rejects duplicate characters). Enforced in `validate_spec`.
  * RATE LIMIT: time.sleep(0.3) after every SaveDeck and every GetDeck.
  * APPEND-ONLY, RESUMABLE: rows append to dumps/bonus_extract_db.jsonl and specs whose spec_hash
    is already present are skipped, so a run can be interrupted and re-run with no loss or dup.
  * data/, state/, src/ are never written.

USAGE (from the repo root, proxyenv python)
-------------------------------------------
  proxyenv/Scripts/python tools/recon/proxy/bonus_extract.py --dry-run --all
  proxyenv/Scripts/python tools/recon/proxy/bonus_extract.py --family singles
  proxyenv/Scripts/python tools/recon/proxy/bonus_extract.py --family blue_member --limit 20
  proxyenv/Scripts/python tools/recon/proxy/bonus_extract.py --family pairs --leader chr-00001/cos-00001-cmmn-0000-00
"""
import argparse
import hashlib
import json
import os
import random
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import account_snapshot as snap
import deck_probe as dp
import deck_oracle as do

SAVEDECK = "/rpc.api.Live/SaveDeck"
GETDECK = "/rpc.api.Live/GetDeck"
# In-memory only. The on-disk read-only whitelist is untouched. frozenset -> no .add past this.
snap.ALLOWED = frozenset(set(snap.ALLOWED) | {SAVEDECK, GETDECK})

DB = os.path.join(HERE, "dumps", "bonus_extract_db.jsonl")
TAXONOMY = os.path.join(HERE, "dumps", "board_node_taxonomy.json")
SONGS = os.path.join(HERE, "dumps", "songs.json")
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
CARDS = os.path.join(REPO, "data", "cards.json")

# Two leaders measured to contribute 0 leader-board and 0 costume effect (deck_probe.py:
# NULL_LEADER, ALT). A neutral leader lets a family read the MEMBERS' contribution with no
# leader term folded in. Two of them so a member that shares one leader's character can still be
# probed under the other.
NEUTRAL_A = ("chr-00001", "cos-00001-cmmn-0000-00")
NEUTRAL_B = ("chr-00004", "cos-00004-cmmn-0000-00")


# ----------------------------------------------------------------- owned-card helpers
def owned_cards():
    return json.load(open(os.path.join(REPO, "data", "owned.json"), encoding="utf-8"))["owned"]


def char_of(cid):
    return cid.split("-")[1]


def leader_of(cid):
    """A card id -> (leader chr, costume). 'card-04003-5-uniq-0044-00' -> ('chr-04003',
    'cos-04003-uniq-0044-00')."""
    p = cid.split("-")
    return f"chr-{p[1]}", f"cos-{p[1]}-{p[3]}-{p[4]}-{p[5]}"


def representatives(owned):
    """One owned card per character (highest rarity digit) -- the member/leader board is a
    per-character skill tree shared by every card of that character, so one representative
    probes it."""
    by_char = {}
    for c in owned:
        ch = char_of(c)
        if ch not in by_char or c.split("-")[2] > by_char[ch].split("-")[2]:
            by_char[ch] = c
    return by_char


def neutral_leader_for(member_chars):
    """A neutral (0-contribution) leader whose character is NOT among `member_chars`."""
    for lead in (NEUTRAL_A, NEUTRAL_B):
        if lead[0].split("-")[1] not in member_chars:
            return lead
    return None


# ----------------------------------------------------------------- catalogue metadata helpers
_CARD_META = None


def card_meta():
    """`{card_id: {"type": str, "gen": (generation, ...)}}` from data/cards.json (READ-ONLY).

    Type is one of the game's three (Pure/Cute/Happy); generation is a tuple because a few units
    carry two (e.g. `1期生`+`ゲーマーズ`). Used by the `conditional` family to compose teams that
    toggle type/generation-scoped passive and board conditionals."""
    global _CARD_META
    if _CARD_META is not None:
        return _CARD_META
    _CARD_META = {}
    cards = json.load(open(CARDS, encoding="utf-8"))
    cards = cards["cards"] if isinstance(cards, dict) else cards
    for c in cards:
        g = c.get("generation")
        g = tuple(g) if isinstance(g, list) else (g,) if g else ()
        _CARD_META[c["id"]] = {"type": c.get("type"), "gen": g}
    return _CARD_META


def songs():
    """The 188-song catalogue from dumps/songs.json (READ-ONLY): each `{id, singer{type,
    character_ids}, ...}`. `singer.type` is SOLO / GROUP / ALL."""
    if not os.path.exists(SONGS):
        return []
    return json.load(open(SONGS, encoding="utf-8")).get("songs", [])


_COOLDOWN = None


def cooldown_of(card_id):
    """Active-skill cooldown (seconds, level-1) for a card, via deck_oracle's own master read, or
    None. Lets the `triples`/`quads` families prioritise DISTINCT cooldown intervals so the ACTIVE
    E[max] combine corpus samples varied interval lattices rather than near-duplicates."""
    global _COOLDOWN
    if _COOLDOWN is None:
        _COOLDOWN = {}
        try:
            lv = do._active_levels()
            for (prefix, level), row in lv.items():
                if level == "1":
                    _COOLDOWN[prefix] = row[2]      # (mag, dur, cooldown, prob)
        except Exception:
            pass
    return _COOLDOWN.get(do._card_key(card_id))


# ----------------------------------------------------------------- spec + validation
def spec_hash(leader, costume, music_id, members):
    canon = f"{leader}|{costume}|{music_id or ''}|{'|'.join(members)}"
    return hashlib.sha1(canon.encode()).hexdigest()[:16]


def validate_spec(spec, owned_set):
    """Return None if the spec is safe to fire, else a reason string. Enforces owned-only and
    distinct-character rails BEFORE any SaveDeck."""
    members = spec["members"]
    if not (1 <= len(members) <= 5):
        return f"{len(members)} members (need 1..5)"
    bad = [c for c in members if c not in owned_set]
    if bad:
        return f"unowned card(s): {bad[:3]}"
    mchars = [char_of(c) for c in members]
    if len(set(mchars)) != len(mchars):
        return f"duplicate member characters: {mchars}"
    if spec["leader"].split("-")[1] in mchars:
        return f"leader character {spec['leader']} is also a member"
    return None


# ----------------------------------------------------------------- family generators
def fam_singles(owned, reps):
    """Every owned card as the lone member under a neutral leader."""
    out = []
    for c in sorted(owned):
        lead = neutral_leader_for({char_of(c)})
        if not lead:
            continue
        out.append({"family": "singles", "leader": lead[0], "costume": lead[1],
                    "music_id": None, "members": [c]})
    return out


def fam_pairs(owned, reps):
    """Systematic 2-card decks (distinct characters) under a neutral leader. Pool excludes the
    two neutral-leader characters so NEUTRAL_A always works and no pair collides with the leader."""
    skip = {NEUTRAL_A[0].split("-")[1], NEUTRAL_B[0].split("-")[1]}
    pool = sorted(reps[ch] for ch in reps if ch not in skip)
    out = []
    for i in range(len(pool)):
        for j in range(i + 1, len(pool)):
            out.append({"family": "pairs", "leader": NEUTRAL_A[0], "costume": NEUTRAL_A[1],
                        "music_id": None, "members": [pool[i], pool[j]]})
    return out


def fam_red_leader(owned, reps):
    """Same 5 members, LEADER rotated across every distinct owned (character, costume)."""
    seen, leaders = set(), []
    for cid in sorted(owned):
        lead = leader_of(cid)
        if lead not in seen:
            seen.add(lead)
            leaders.append(lead)
    out = []
    for leader, costume in leaders:
        members = [reps[ch] for ch in sorted(reps) if ch != leader.split("-")[1]][:5]
        if len(members) < 5:
            continue
        out.append({"family": "red_leader", "leader": leader, "costume": costume,
                    "music_id": None, "members": members})
    return out


def fam_blue_member(owned, reps):
    """4 fixed base members + a rotating 5th over every other owned character, under a neutral
    leader. Isolates the swapped-in character's member-scope (blue) board contribution."""
    lead = NEUTRAL_A
    lead_ch = lead[0].split("-")[1]
    pool = [ch for ch in sorted(reps) if ch != lead_ch]
    base_chars = pool[:4]
    base = [reps[ch] for ch in base_chars]
    out = []
    for ch in pool[4:]:
        out.append({"family": "blue_member", "leader": lead[0], "costume": lead[1],
                    "music_id": None, "members": base + [reps[ch]]})
    return out


def fam_green_offteam(owned, reps):
    """Paired decks around each green-node-heavy benched unit: a base 5-member team that OMITS it,
    and the same team with the unit swapped IN. Green is collection-wide, so the pair isolates the
    unit's member-scope delta against a constant green baseline. Units picked from the taxonomy's
    drawn green counts, restricted to owned characters."""
    if not os.path.exists(TAXONOMY):
        return []
    tax = json.load(open(TAXONOMY, encoding="utf-8"))
    lead = NEUTRAL_A
    lead_ch = lead[0].split("-")[1]
    # green-heavy owned units, most drawn-green first
    ranked = sorted(
        ((v.get("drawn_by_color", {}).get("green", 0), ch)
         for ch, v in tax.get("units", {}).items()
         if ch.split("-")[1] in reps and ch.split("-")[1] != lead_ch),
        reverse=True)
    targets = [ch for g, ch in ranked if g >= 5][:6]
    # a fixed base of 5 neutral members that avoids the leader AND all target characters
    target_chars = {ch.split("-")[1] for ch in targets}
    base_chars = [ch for ch in sorted(reps)
                  if ch != lead_ch and ch not in target_chars][:5]
    base = [reps[ch] for ch in base_chars]
    out = []
    for ch in targets:
        tnum = ch.split("-")[1]
        include = base[:4] + [reps[tnum]]        # swap the 5th base slot for the target unit
        out.append({"family": "green_offteam", "leader": lead[0], "costume": lead[1],
                    "music_id": None, "members": base, "_pair": f"{tnum}:omit"})
        out.append({"family": "green_offteam", "leader": lead[0], "costume": lead[1],
                    "music_id": None, "members": include, "_pair": f"{tnum}:include"})
    return out


# Score-support outfit leaders (measured magnitudes in the outfit line): a card the account owns
# supplies each. Varying the 5 members under a FIXED such leader reads the outfit Score-Bonus
# line's dependence on team composition -> populates bonus.outfit.
OUTFIT_LEADERS = [
    ("chr-04003", "cos-04003-uniq-0044-00"),   # m=60
    ("chr-04016", "cos-04016-cmmn-0000-00"),   # m=50
]


def fam_outfit_score(owned, reps):
    """A score-support-outfit leader, systematically varied 5-member sets. For each outfit leader,
    rotate the members broadly over the owned pool (three stride passes with wraparound) so the
    outfit Score-Bonus line is read against many compositions. Member-sets deduped per leader."""
    out = []
    for leader, costume in OUTFIT_LEADERS:
        lch = leader.split("-")[1]
        pool = sorted(reps[ch] for ch in reps if ch != lch)
        n = len(pool)
        if n < 5:
            continue
        seen = set()
        for step in (1, 2, 3):
            for i in range(n):
                members = [pool[(i + step * k) % n] for k in range(5)]
                if len(set(char_of(c) for c in members)) != 5:
                    continue
                key = frozenset(members)
                if key in seen:
                    continue
                seen.add(key)
                out.append({"family": "outfit_score", "leader": leader, "costume": costume,
                            "music_id": None, "members": members})
    return out


def fam_conditional(owned, reps):
    """Vary the TYPE / GENERATION composition of the 5 members to toggle conditional passive and
    board score bonuses. For each type (Pure/Cute/Happy) and each generation with enough members,
    build decks holding N=1..5 members of that key (rest drawn from OTHER keys), several member
    variants per N, under a neutral leader. The N-sweep is what exposes a count-thresholded
    conditional (\"3+ Cute members\" etc.) that a fixed composition cannot see."""
    meta = card_meta()
    lead = neutral_leader_for(set())
    lch = lead[0].split("-")[1]
    # rep pool with metadata, excluding the neutral leader's character
    pool = [reps[ch] for ch in sorted(reps) if ch != lch]
    by_type, by_gen = {}, {}
    for c in pool:
        m = meta.get(c, {})
        if m.get("type"):
            by_type.setdefault(m["type"], []).append(c)
        for g in m.get("gen", ()):
            by_gen.setdefault(g, []).append(c)

    out, seen = [], set()

    def emit(members):
        if len(members) != 5 or len(set(char_of(c) for c in members)) != 5:
            return
        key = frozenset(members)
        if key in seen:
            return
        seen.add(key)
        out.append({"family": "conditional", "leader": lead[0], "costume": lead[1],
                    "music_id": None, "members": members})

    def sweep(groups, min_group=3, variants=4):
        for keyname, ingroup in sorted(groups.items()):
            others = [c for c in pool if c not in set(ingroup)]
            if len(ingroup) < 1 or len(others) < 4:
                continue
            maxn = min(5, len(ingroup))
            for nsel in range(1, maxn + 1):
                for v in range(variants):
                    ins = [ingroup[(v + j) % len(ingroup)] for j in range(nsel)]
                    if len(set(ins)) != nsel:
                        continue
                    need = 5 - nsel
                    fill, used = [], set(char_of(c) for c in ins)
                    for c in others[v:] + others[:v]:
                        if len(fill) >= need:
                            break
                        if char_of(c) not in used:
                            fill.append(c); used.add(char_of(c))
                    if len(fill) == need:
                        emit(ins + fill)

    sweep(by_type, variants=6)
    sweep({g: cs for g, cs in by_gen.items() if len(cs) >= 3}, variants=3)
    return out


# Curated per-song set: solo / unit(group) / whole-roster(ALL), so the yellow per-song board line
# and the singer-conditional nodes are pinned across cast sizes. Owned-character solos are included
# so a team member IS the singer (that is what fires singer-conditional nodes).
def _per_song_music_ids(team_chars):
    ss = songs()
    solo, group, allr = [], [], []
    for s in ss:
        if s.get("is_tutorial"):
            continue
        t = s.get("singer", {}).get("type")
        cids = s.get("singer", {}).get("character_ids") or []
        (allr if t == "ALL" else group if t == "GROUP" else solo).append((s["id"], cids))
    # prioritise solos sung by a team member, then spread the rest
    own_solo = [mid for mid, cids in solo if set(cids) & team_chars]
    other_solo = [mid for mid, cids in solo if not (set(cids) & team_chars)]
    picked = []
    picked += own_solo
    picked += other_solo[::3]                 # ~1/3 of the remaining solos, evenly spread
    picked += [mid for mid, _ in group][::2]  # half the group songs
    picked += [mid for mid, _ in allr]        # every whole-roster song
    # the live marathon event songs (data/marathon.json) if present
    try:
        mj = json.load(open(os.path.join(REPO, "data", "marathon.json"), encoding="utf-8"))
        for s in mj.get("channels", {}).get("music", []):
            if s.get("music_id"):
                picked.append(s["music_id"])
    except Exception:
        pass
    # dedup, keep order, cap
    outids, seen = [], set()
    for m in picked:
        if m not in seen:
            seen.add(m); outids.append(m)
    return outids[:120]


def fam_per_song(owned, reps):
    """One FIXED team, the same across every song, evaluated under a spread of music_ids (GetDeck
    field 3, LiveGetDeckRequest.music_id). The panel Active
    line is song-invariant but the yellow per-song board line and the singer-conditional nodes are
    NOT, so this pins them. The team is chosen to contain characters that have solo songs so a
    member can be the singer."""
    lead = neutral_leader_for(set())
    lch = lead[0].split("-")[1]
    pool = [reps[ch] for ch in sorted(reps) if ch != lch]
    # prefer a team of characters that each have a solo song, to hit singer-conditionals
    solo_chars = set()
    for s in songs():
        if s.get("singer", {}).get("type") == "SOLO":
            for cid in s.get("singer", {}).get("character_ids") or []:
                solo_chars.add(cid.split("-")[1])
    preferred = [c for c in pool if char_of(c) in solo_chars]
    team = (preferred + pool)[:5]
    team_chars = {char_of(c) for c in team}
    out = []
    for mid in _per_song_music_ids(team_chars):
        out.append({"family": "per_song", "leader": lead[0], "costume": lead[1],
                    "music_id": mid, "members": list(team)})
    return out


def _interval_ranked_combos(pool, size, cap):
    """Up to `cap` distinct-character `size`-combos from `pool`, ranked so combinations with MORE
    distinct active-skill cooldown intervals come first (varied E[max] combine lattices). Falls
    back to a stride spread if no cooldowns are known. Deterministic -> resumable."""
    import itertools
    cds = {c: cooldown_of(c) for c in pool}
    combos = list(itertools.combinations(pool, size))
    if any(cds.values()):
        def rank(combo):
            vals = [cds[c] for c in combo if cds[c] is not None]
            # more distinct intervals first; wider spread next; index sum as a stable spreader
            spread = (max(vals) - min(vals)) if vals else 0
            return (-len(set(vals)), -spread, hash(combo) & 0xffff)
        combos.sort(key=rank)
    else:
        combos = combos[:: max(1, len(combos) // cap)]
    return combos[:cap]


def fam_triples(owned, reps):
    """~400 3-card distinct-character decks under a neutral leader, prioritising varied cooldown
    intervals -> extends the ACTIVE E[max] combine corpus past pairs."""
    lch = {NEUTRAL_A[0].split("-")[1], NEUTRAL_B[0].split("-")[1]}
    pool = sorted(reps[ch] for ch in reps if ch not in lch)
    out = []
    for combo in _interval_ranked_combos(pool, 3, 400):
        out.append({"family": "triples", "leader": NEUTRAL_A[0], "costume": NEUTRAL_A[1],
                    "music_id": None, "members": list(combo)})
    return out


def fam_quads(owned, reps):
    """~300 4-card distinct-character decks under a neutral leader, prioritising varied cooldown
    intervals -> extends the ACTIVE E[max] combine corpus to 4-way overlaps."""
    lch = {NEUTRAL_A[0].split("-")[1], NEUTRAL_B[0].split("-")[1]}
    pool = sorted(reps[ch] for ch in reps if ch not in lch)
    out = []
    for combo in _interval_ranked_combos(pool, 4, 300):
        out.append({"family": "quads", "leader": NEUTRAL_A[0], "costume": NEUTRAL_A[1],
                    "music_id": None, "members": list(combo)})
    return out


# A score-support outfit leader that contributes NO Active Skill (leaders contribute only Outfit +
# red leader-scope nodes), so bonus.active reads purely off the 5
# members' active skills. chr-04016/cos-04016-cmmn-0000-00 is measured m=50 (OUTFIT_LEADERS above).
SUPPORT_LEADER = ("chr-04016", "cos-04016-cmmn-0000-00")


def _active_int_dur(card_id):
    """(interval_s, duration_s) rounded to whole seconds for a card's level-1 active skill, or
    None if the card has no active skill. Straight from LiveActiveSkillLevel via deck_oracle."""
    row = do._active_levels().get((do._card_key(card_id), "1"))
    if not row:
        return None
    mag, dur, cool, prob = row
    return (round(cool), round(dur))


def _same_interval_pools(owned, exclude_char):
    """{(interval_s, duration_s): [card_id, ...]} over OWNED active cards, one card per character
    (highest rarity for that character within the pool), excluding `exclude_char`. The active skill
    is keyed by card PREFIX (card-NNNNN-R), so different rarities of one character can land in
    different interval pools -- which is why the pools span all owned cards, not just
    representatives."""
    pools = {}
    for c in owned:
        ch = char_of(c)
        if ch == exclude_char:
            continue
        key = _active_int_dur(c)
        if not key:
            continue
        d = pools.setdefault(key, {})
        if ch not in d or c.split("-")[2] > d[ch].split("-")[2]:
            d[ch] = c
    return {k: sorted(v.values()) for k, v in pools.items()}


def fam_same_interval_multi(owned, reps):
    """SAME-INTERVAL multi-active families: decks whose k=2..5 members ALL share ONE active-skill
    cooldown interval, under a fixed score-support leader (chr-04016) for a clean bonus.active
    readout. Tests the same-interval E[max] discount (~-0.72%, the density floor) and its
    saturation at ceil(interval/duration) window-widths at 3/4/5-card scale, which the
    triples/quads corpus -- all DIFFERENT-interval -- does not probe.

    For each owned same-interval pool with >=3 distinct characters it emits a NESTED chain: the
    k=1 single of each chain card (its own bonus.active baseline) and the cumulative k=2..K decks
    (members = chain[:k]), so obs/add(k) = bonus.active(k) / sum(single bonus.active of chain[:k])
    is read straight off the DB. The two largest pools get a second disjoint chain to confirm the
    curve is composition- not card-specific. A handful of MIXED decks (same-interval subgroup +
    different-interval singles) probe the group boundary: different-interval groups ADD while the
    same-interval subgroup saturates."""
    lead, cos = SUPPORT_LEADER
    lch = lead.split("-")[1]
    pools = _same_interval_pools(owned, lch)
    out = []

    def emit(members, tag):
        # guard: distinct characters (the leader's char is excluded from every pool already)
        if len(set(char_of(c) for c in members)) != len(members):
            return
        out.append({"family": "same_interval_multi", "leader": lead, "costume": cos,
                    "music_id": None, "members": list(members), "_grp": tag})

    ranked = sorted(pools.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    # Pools with >=4 distinct characters reach k=4/5; the two biggest (n>=2K) get a second disjoint
    # chain. This keeps the plan ~60-75 decks while spanning several distinct intervals and covering
    # k=2..5 same-interval. (n=3 pools would only add k=2,3, already densely covered by the big
    # pools; the pairs corpus already has same-interval k=2.)
    ranked = [(k, v) for k, v in ranked if len(v) >= 4]

    for (cool, dur), cards in ranked:
        K = min(5, len(cards))
        chains = [cards[:K]]
        if len(cards) >= 2 * K:                 # a second disjoint chain for the big pools
            chains.append(cards[K:2 * K])
        for ci, chain in enumerate(chains):
            for c in chain:                     # per-card baseline (k=1)
                emit([c], f"i{cool}d{dur}c{ci}k1")
            for k in range(2, len(chain) + 1):  # cumulative nested k=2..K, same interval
                emit(chain[:k], f"i{cool}d{dur}c{ci}k{k}")

    # ------ MIXED decks: same-interval subgroup(s) + different-interval singles ------
    # A/B/C = the three largest same-interval pools. Compositions chosen so every component
    # (each single and each same-interval pair) is already predicted by the nested chains above.
    if len(ranked) >= 3:
        (ca, da), A = ranked[0]
        (cb, db), B = ranked[1]
        (cc, dc_), C = ranked[2]
        mixes = [
            (A[:3] + B[:1] + C[:1], "mix_3A+1B+1C"),
            (A[:4] + B[:1],          "mix_4A+1B"),
            (A[:2] + B[:2] + C[:1],  "mix_2A+2B+1C"),
            (B[:3] + A[:1],          "mix_3B+1A"),
            (A[:2] + B[:1] + C[:1] + C[1:2], "mix_2A+1B+2C"),
        ]
        for members, tag in mixes:
            if len(members) <= 5 and len(set(char_of(c) for c in members)) == len(members):
                emit(members, tag)
    return out


# ----------------------------------------------------------------- random_teams family
# Seeded so the plan is reproducible AND resumable: the generator always walks the SAME
# random sequence, dedups each candidate against the DB, and keeps the first N-not-yet-in-DB
# realistic teams. A re-run replays the identical sequence, skips the rows already captured,
# and continues past them -- so an interrupted run resumes exactly where it stopped and two
# runs never disagree about which team was the k-th team.
RANDOM_SEED = 20260822
RANDOM_N = 1500


def _rarity_digit(cid):
    """The rarity digit of a card id, e.g. 'card-04003-5-uniq-...' -> '5'."""
    return cid.split("-")[2]


def fam_random_teams(owned, reps):
    """N seeded-random REALISTIC 6-card teams: a leader (any owned card -- every holomem can
    lead) plus 5 distinct-character members drawn from the owned pool. Uniform draws over owned
    cards naturally MIX leaders, types, generations, rarities and active-skill cooldown intervals
    -- i.e. the realistic decks the optimizer actually ranks, NOT the controlled single-pool decks
    the other families cover. Each candidate is dedup-checked against the DB (and within the plan)
    so the family grows the DB by up to N genuinely-new teams; the fixed seed makes that set
    reproducible and the run resumable."""
    rng = random.Random(RANDOM_SEED)
    done, _ = load_done()

    by_char = {}
    for c in owned:
        by_char.setdefault(char_of(c), []).append(c)
    for ch in by_char:
        by_char[ch].sort()                        # stable pools -> deterministic rng.choice
    chars = sorted(by_char)
    if len(chars) < 6:
        return []

    out, seen = [], set()
    # generous attempt budget: the combinatorial space (C(53,5) x rarities x leaders) dwarfs N,
    # so this only guards against an accidental infinite loop, never limits realistic coverage.
    attempts, max_attempts = 0, RANDOM_N * 200
    while len(out) < RANDOM_N and attempts < max_attempts:
        attempts += 1
        leader_char = rng.choice(chars)
        leader_card = rng.choice(by_char[leader_char])
        leader, costume = leader_of(leader_card)
        member_chars = rng.sample([c for c in chars if c != leader_char], 5)
        members = [rng.choice(by_char[mc]) for mc in member_chars]
        h = spec_hash(leader, costume, None, members)
        if h in done or h in seen:
            continue                              # already captured, or already in this plan
        seen.add(h)
        out.append({"family": "random_teams", "leader": leader, "costume": costume,
                    "music_id": None, "members": members, "spec_hash": h})
    return out


FAMILIES = {
    "random_teams": fam_random_teams,
    "singles": fam_singles,
    "same_interval_multi": fam_same_interval_multi,
    "green_offteam": fam_green_offteam,
    "red_leader": fam_red_leader,
    "blue_member": fam_blue_member,
    "pairs": fam_pairs,
    "outfit_score": fam_outfit_score,
    "conditional": fam_conditional,
    "per_song": fam_per_song,
    "triples": fam_triples,
    "quads": fam_quads,
}


# ----------------------------------------------------------------- full-record decode
_POWER = {"[Member Parameter]": "member_parameter", "[Outfit Skill]": "outfit",
          "[Passive Skill]": "passive", "[Holomem Board]": "board",
          "[Memory Bonus]": "memory", "[Member Power-Up]": "power_up"}
_BONUS = {"[Active Skill]": "active", "[Outfit Skill]": "outfit", "[Passive Skill]": "passive",
          "[Holomem Board]": "board", "[Special Skill]": "special"}


def _clean(block, mapping):
    out = {v: 0 for v in mapping.values()}
    for k, val in block.items():
        for sub, name in mapping.items():
            if sub in k:
                out[name] = val
    return out


def leader_card_id(costume, owned=None):
    """The leader's CARD id, from the costume id.

    A deck spec names the leader by CHARACTER (`chr-04016`), but bloom, level and
    limit-break are properties of a CARD. The costume carries the discriminators:
    `cos-04001-nrml-0042-00` -> the `nrml` card with costume number `0042`, i.e.
    `card-04001-3-nrml-0042-00`.

    WHY: passing the `chr-` id to `card_state_at_capture([spec["leader"]] + members)` would
    silently record the five members and **not the leader** -- the one card whose bloom the
    leader-skill question is actually about.
    """
    parts = str(costume).split("-")
    if len(parts) < 5 or parts[0] != "cos":
        return None
    chr_num, kind, cos_num = parts[1], parts[2], parts[3]
    if owned is None:
        owned = owned_cards()
    # `card-04001-3-nrml-0042-00` -> [card, 04001, RARITY, nrml, 0042, 00]. The card id
    # carries a rarity segment the costume id does not, so `kind` is at index 3 and the
    # costume number at index 4 -- one further along than in `cos-04001-nrml-0042-00`.
    hits = []
    for c in owned:
        parts_c = str(c).split("-")
        if len(parts_c) < 6:
            continue
        if (parts_c[1] == chr_num and parts_c[3] == kind and parts_c[4] == cos_num):
            hits.append(c)
    return hits[0] if len(hits) == 1 else None


def card_state_at_capture(card_ids):
    """`{card_id: {level, bloom, limit_break}}` read from the collection RIGHT NOW.

    WHY. `deck_panel_params.panel_cards` takes a row's effect-ups and
    board buffs from the row's own `per_card`, but resolves each card's **bloom**
    from TODAY's `state/collection.json` -- and bloom sets the skill level, which
    sets `magnitude_permil`. A row captured before a card was bloomed is therefore
    re-scored with a magnitude the server never used, and **nothing on disk can
    detect it retroactively**: `per_card`'s performance/technique/sense are the
    card's DECK-EFFECTIVE stats, not a card fingerprint.

    Two candidate fingerprints were tested and both fail:

    * `per_card.stat / card_params.stat` -- the within-row spread is 0.136, so the
      deck multiplier is not card-uniform and a single card's ratio says nothing.
    * per-card stat drift inside a repeated `spec_hash` -- this DOES fire (56 cards,
      exactly dated, no modelling), but every transition it finds goes **downward**
      by ~0.03%, and level / bloom / limit-break only ever increase. So it is a real
      staleness vector of another kind, and it is not this one.

    So the only remedy is to record the state at capture time, which is what this does.
    Read-only: `state/collection.json` is the user's file and nothing here writes it.
    """
    path = os.path.normpath(
        os.path.join(HERE, "..", "..", "..", "state", "collection.json")
    )
    try:
        with open(path, encoding="utf-8") as fh:
            cards = (json.load(fh) or {}).get("cards") or {}
    except (OSError, ValueError):
        return None
    out = {}
    for cid in card_ids:
        entry = cards.get(cid)
        if isinstance(entry, dict):
            out[cid] = {k: entry.get(k) for k in ("level", "bloom", "limit_break")}
    return out or None


def _f32(v):
    return struct.unpack("<f", int(v).to_bytes(4, "little"))[0]


def read_eff3(pt):
    """`LiveGetDeckResponse.3.3` -- the ONLY confirmed FRACTIONAL channel the server exposes.

    Every score field in all 312 API methods is an int64; this one field is a float32,
    and it is the per-song board bonus. `None` means the field is structurally absent
    (a song-less GetDeck), which is NOT the same as a present 0.0 -- a distinction the
    caller needs, since `music_id=None` and "this song grants nothing" are different
    facts.
    """
    try:
        eff = do.decode_record(do.decode_record(pt)[3][0])
    except Exception:
        return None
    raw3 = eff.get(3, [None])[0]
    return _f32(raw3) if isinstance(raw3, int) else None


def read_leader_skill_levels(pt):
    """`LiveGetDeckResponse.3.2` -- the SERVER's own statement of the LEADER's active-skill levels.

    ```proto
    message LiveDeckInGameEffect {                       // = LiveGetDeckResponse field 3
        optional int32 life_up = 1;
        repeated LiveActiveSkillLevel live_leader_active_skill_levels = 2;
        optional float live_score_bonus_permil_up_by_skill_tree = 3;   // eff3
        repeated LiveDeckPosition live_deck_positions = 100;
    }
    message LiveActiveSkillLevel { optional string live_active_skill_id = 1;
                                   optional int32  level                = 2; }
    ```

    WHY IT WOULD MATTER. It is the `bloom -> skill level` half of the
    `bloom -> level -> magnitude_permil` chain, stated by the server rather than derived
    from `state/collection.json`, and that chain is the one staleness vector nothing on
    disk can measure (see `card_state_at_capture`).

    ⛔ **`GetDeck` NEVER POPULATES THIS FIELD.**
    Probed on the raw wire (not through this helper, so a decode bug could not hide it): the
    field-3 submessage of a `GetDeck` response contains **only field 100**, the five deck
    positions. Fields 1 (`life_up`), 2 (this one) and 3 (`eff3`) are all absent by default,
    and only `eff3` ever appears -- when, and only when, the request carries a `music_id`.
    Tested with four different leaders (chr-04016, chr-04003, chr-00001, chr-04001), with a
    song and without, with a board-carrying deck and without. Zero hits.

    So the `bloom -> skill level` chain is **not** readable from the deck screen: the
    captures carry the field's absence, not its value. If those levels are anywhere, it is
    in a live context (`StartSingle`), not here.

    ⚠ `life_up` cannot be used as a witness to tell "the server leaves it empty" from "it is
    genuinely zero": the account has **no life-family board nodes drawn at all**, so proto3
    would omit a true zero anyway.

    ⚠ It is the **LEADER's** skills only -- the field name says so. Whether the members'
    levels are anywhere on the wire is not known.

    Returns `[{"skill_id": str, "level": int}, ...]`, or `None` if the block is absent --
    which, on `GetDeck`, is always. Kept in `read_full` anyway: it costs nothing, and a
    future server build that starts populating it will show up in the corpus by itself.
    """
    try:
        eff = do.decode_record(do.decode_record(pt)[3][0])
    except Exception:
        return None
    out = []
    for raw in eff.get(2, []):
        if not isinstance(raw, (bytes, bytearray)):
            continue
        rec = do.decode_record(raw)
        sid = rec.get(1, [None])[0]
        lvl = rec.get(2, [None])[0]
        out.append({
            "skill_id": sid.decode("utf-8", "replace") if isinstance(sid, (bytes, bytearray)) else sid,
            "level": lvl,
        })
    return out or None


def read_full(pt):
    """The complete server decomposition from a GetDeck plaintext, using deck_oracle's decoders
    (never a hand-rolled parse). Returns None if the evaluation cannot be decoded.

    ⚠ `eff3` must be part of this return: without it the field is absent from every row
    even though `get_deck_full` carries `music_id` -- the transport is fine, the decode
    never happens. See `read_eff3`.
    """
    ev = do._evaluation(pt)
    if not ev:
        return None
    cards = do._cards_structured(pt) or do._cards_with_stats(pt)
    per_card = []
    for slotname, c in sorted(cards.items(), key=lambda kv: int(kv[0].replace("slot", ""))):
        per_card.append({
            "slot": int(slotname.replace("slot", "")),
            "card_id": c.get("card"),
            "performance": c.get("performance"),
            "technique": c.get("technique"),
            "sense": c.get("sense"),
            "active_effect_up": c.get("active_effect_up"),
            "active_prob_up": c.get("active_prob_up"),
            "active_cool_shorten": c.get("active_cool_shorten"),
        })
    return {
        "unit_score": ev["unit_score"],
        "overall_power": ev["overall_power"],
        "power": _clean(ev["power"], _POWER),
        "score_bonus_permil": ev["score_bonus_permil"],
        "bonus": _clean(ev["bonus"], _BONUS),
        "member_power": do._member_power(pt),
        "per_card": per_card,
        "eff3": read_eff3(pt),
        "leader_skill_levels": read_leader_skill_levels(pt),
    }


def get_deck_full(host, token, ident, mver, ch, slot, music_id=None):
    """LiveGetDeckRequest { character_id=1, number=2, music_id=3 }. music_id at field 3 makes the
    server return the SONG-SPECIFIC evaluation (yellow per-song board line + singer-conditional
    nodes); omitting it is the base song-invariant deck-screen read."""
    req = snap.pb_bytes_field(1, ch.encode()) + bytes([0x10]) + snap.write_varint(int(slot))
    if music_id:
        req += snap.pb_bytes_field(3, music_id.encode())
    pt, err = dp.fire(host, GETDECK, req, token, ident, mver)
    if err:
        return None, err
    return read_full(pt), None


# ----------------------------------------------------------------- slot safety
def choose_slot(sd, ch):
    """A SAFE slot for character ch: a genuinely-free slot, else a 'zz'-named scratch slot.
    Returns (slot, kind) or (None, reason). NEVER returns a kept (non-zz, in-use) deck."""
    used = {d.get("number"): d for d in sd.get("decks", []) if d.get("character_id") == ch}
    for n in range(1, 11):
        if n not in used:
            return n, "free"
    zz = sorted((n for n, d in used.items() if str(d.get("name", "")).startswith("zz")),
                reverse=True)
    if zz:
        return zz[0], "zz-scratch:" + str(used[zz[0]].get("name"))
    return None, "no free or zz-scratch slot"


def resolve_slot(sd, ch, slot_cache):
    """One cached safe scratch slot per leader character. HARD-STOPS on a kept deck."""
    if ch in slot_cache:
        return slot_cache[ch]
    slot, kind = choose_slot(sd, ch)
    if slot is None:
        print(f"STOP: no safe slot for {ch}: {kind}")
        return None
    inuse = {d.get("number"): d for d in sd.get("decks", []) if d.get("character_id") == ch}
    # HARD GUARD: if the target slot is in use, its name MUST start with 'zz'.
    if slot in inuse and not str(inuse[slot].get("name", "")).startswith("zz"):
        print(f"STOP: slot {slot} on {ch} is a KEPT deck {inuse[slot].get('name')!r}; refusing.")
        return None
    print(f"  slot for {ch}: {slot}  ({kind})")
    slot_cache[ch] = slot
    return slot


# ----------------------------------------------------------------- DB
def load_done():
    done, maxidx = set(), -1
    if os.path.exists(DB):
        for line in open(DB, encoding="utf-8"):
            try:
                r = json.loads(line)
                done.add(r["spec_hash"])
                maxidx = max(maxidx, r.get("capture_index", -1))
            except Exception:
                pass
    return done, maxidx


# ----------------------------------------------------------------- plan
def build_plan(names, owned, reps):
    plan, seen = [], set()
    for name in names:
        for spec in FAMILIES[name](owned, reps):
            spec["spec_hash"] = spec_hash(spec["leader"], spec["costume"],
                                          spec["music_id"], spec["members"])
            plan.append(spec)
    return plan


# ----------------------------------------------------------------- run
def _snapshot_collection():
    """Keep the account's collection beside the DB: rows carry no card state, so the tests
    that replay them (deck_panel_params) must score them on the levels they were captured at.
    Written with a DB's first row; a later run on a changed collection only warns."""
    src = os.path.join(HERE, "..", "..", "..", "state", "collection.json")
    snap = DB[:-len(".jsonl")] + ".collection.json"
    try:
        current = open(src, "rb").read()
    except OSError:
        return
    if not os.path.exists(snap):
        with open(snap, "wb") as fh:
            fh.write(current)
    elif open(snap, "rb").read() != current:
        print(f"  !! the collection changed since {os.path.basename(snap)} was taken: new rows "
              "are replayed on the old levels; start a new DB to capture on this one")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--family", nargs="+", choices=sorted(FAMILIES),
                    help="one or more family names to run")
    ap.add_argument("--all", action="store_true", help="run every family")
    ap.add_argument("--limit", type=int, default=None, help="cap decks actually fired this run")
    ap.add_argument("--n", type=int, default=None,
                    help="number of teams for the random_teams family (default 1500)")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, fire nothing")
    ap.add_argument("--leader", default=None,
                    help="override neutral leader as chr-XXXXX/cos-... (families that use one)")
    args = ap.parse_args()

    if args.all:
        names = sorted(FAMILIES)
    elif args.family:
        names = args.family
    else:
        ap.error("pass --family NAME [NAME ...] or --all")

    if args.n is not None:
        global RANDOM_N
        RANDOM_N = args.n

    owned = owned_cards()
    owned_set = set(owned)
    reps = representatives(owned)

    if args.leader:
        ch, cos = args.leader.split("/")
        global NEUTRAL_A
        NEUTRAL_A = (ch, cos)

    plan = build_plan(names, owned, reps)
    done, maxidx = load_done()

    # per-family plan/new counts (dedup-checked against the DB)
    per_fam = {}
    for spec in plan:
        f = spec["family"]
        d = per_fam.setdefault(f, {"planned": 0, "new": 0})
        d["planned"] += 1
        if spec["spec_hash"] not in done:
            d["new"] += 1
    print(f"DB: {len(done)} rows already present (append -> {DB})\n")
    print("=== plan (per family: total planned / not-yet-in-DB) ===")
    tot_p = tot_n = 0
    for f in names:
        d = per_fam.get(f, {"planned": 0, "new": 0})
        print(f"  {f:<14} {d['planned']:>6} planned   {d['new']:>6} new")
        tot_p += d["planned"]
        tot_n += d["new"]
    print(f"  {'TOTAL':<14} {tot_p:>6} planned   {tot_n:>6} new")

    if args.dry_run:
        print("\n--dry-run: nothing fired.")
        return

    # ------ live run ------
    host, cred = snap.load_credential("main")
    ident = snap.client_headers("main")
    mver = snap.fetch_master_version(host, ident)
    token = snap.login(host, cred, ident, mver)
    sd = snap.decode_snapshot(snap.fetch_userdata(host, token, ident, mver))
    print(f"\nlogin OK; snapshot {sum(1 for _ in sd.get('decks', []))} decks known\n")

    slot_cache, idx, fired, skipped = {}, maxidx, 0, 0
    saved = {}                              # (slot, leader, costume, members) already on the slot
    for spec in plan:
        if args.limit is not None and fired >= args.limit:
            break
        if spec["spec_hash"] in done:
            skipped += 1
            continue
        reason = validate_spec(spec, owned_set)
        if reason:
            print(f"  SKIP (unsafe/invalid): {spec['family']} {spec['members']} -- {reason}")
            continue
        slot = resolve_slot(sd, spec["leader"], slot_cache)
        if slot is None:
            return                          # hard stop already reported
        # Only SaveDeck when the TEAM on this slot actually changes. per_song reuses one team
        # across many music_ids (the song varies only in the GetDeck read), so this avoids a
        # redundant write per song. Same slot, same rails.
        team_key = (slot, spec["leader"], spec["costume"], tuple(spec["members"]))
        if saved.get(slot) != team_key:
            pt, err = dp.fire(host, SAVEDECK,
                              dp.save_req(spec["leader"], spec["costume"], slot,
                                          spec["members"], "zzBX"),
                              token, ident, mver)
            if err:
                print(f"STOP: SaveDeck REJECTED for {spec['members']}: {err}")
                return
            saved[slot] = team_key
            time.sleep(0.3)
        full, gerr = get_deck_full(host, token, ident, mver, spec["leader"], slot,
                                   spec.get("music_id"))
        time.sleep(0.3)                     # rate-limit the READ too (per_song reads back-to-back)
        if gerr:
            print(f"STOP: GetDeck error for {spec['members']}: {gerr}")
            return
        if not full:
            print(f"STOP: no evaluation decoded for {spec['members']}")
            return
        idx += 1
        # `captured_at`: deck_panel_params resolves the BOARD REVISION a row was captured
        # under from it. A row without one resolves to the oldest revision, applying an old
        # board to a deck captured months later.
        row = {"spec_hash": spec["spec_hash"], "capture_index": idx, "family": spec["family"],
               "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "leader": spec["leader"], "costume": spec["costume"], "deck_number": slot,
               "music_id": spec["music_id"], "members": spec["members"]}
        row.update(full)
        if "_pair" in spec:
            row["pair"] = spec["_pair"]
        if "_grp" in spec:
            row["grp"] = spec["_grp"]
        if fired == 0:
            _snapshot_collection()
        with open(DB, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        done.add(spec["spec_hash"])
        fired += 1
        print(f"  [{fired}] {spec['family']:<13} slot{slot} "
              f"US={full['unit_score']:>8} OP={full['overall_power']:>7} "
              f"SB={full['score_bonus_permil']:>4}permil  {spec['members']}")

    print(f"\nDONE. fired={fired}, skipped(in DB)={skipped}. DB now has {len(done)} rows.")
    print("Probe decks sit in scratch slots (zzBX / free); data is safe in the DB.")


if __name__ == "__main__":
    main()
