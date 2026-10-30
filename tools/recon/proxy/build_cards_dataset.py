# Join the decrypted Card master + Character master + gRPC Card/GetParameters stats into a
# single enriched card dataset for review. Writes tools/recon/proxy/dumps/cards_enriched.json.
#
# Does NOT write data/cards.json (hand-authored and authoritative) -- this is a
# machine-extracted source to review and merge by hand.
#
# Sources (all decrypted offline):
#   - Card master  (SQLCipher .db, key = per-file seed): id, chr-id, rarity rank, attribute,
#                   passive/active/special skill ids, costume, name localization key
#   - Character master: chr-id -> member name, short name, generation
#   - card_params.json (from grpc_decrypt/parse_card_params): id -> 3 stats + total
#
# Usage: proxyenv/Scripts/python build_cards_dataset.py
import sys, json, os, re, math, struct
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
def _f32(value):
    """Round to IEEE-754 binary32, the way an `ss` SSE instruction does."""
    return struct.unpack("<f", struct.pack("<f", value))[0]

from master_db import decode_record
from master_cache import open_table
import skills

HERE = os.path.dirname(os.path.abspath(__file__))
DUMPS = os.path.join(HERE, "dumps")

def _s(f, k):
    v = f.get(k, [None])[0]
    return v.decode("utf-8", "replace") if isinstance(v, bytes) else v

def _alllevel_map(idx, table):
    """skill_id -> {level: decoded record}, EVERY level (skills cap at level 2).

    `data/cards.json` describes the highest level -- a card at bloom >= 4. A card
    below its bloom step does not have that skill: Ina'nis and Lui at bloom 0
    carry a 32 % PASSIVE the game scores at 24 %. The lower rows give the right
    value, so they are kept.

    ⚠ The two levels are NOT the same row at a different magnitude: 38 of 170
    ACTIVE ids change `duration_ms` across the levels and 2 change the
    activation probability. The consumer substitutes the whole row."""
    db = open_table(table, idx)
    sql = next(db.query("SELECT sql FROM sqlite_master WHERE type='table'"))["sql"].decode()
    tbl = re.search(r"TABLE (\w+)", sql).group(1)
    idcol = re.search(r"`(\w+)` TEXT", sql).group(1)
    out = {}
    for r in db.query(f"SELECT {idcol},level,data FROM {tbl}"):
        out.setdefault(r[idcol].decode(), {})[int(r["level"])] = decode_record(r["data"])
    db.close()
    return out


def _maxlevel(by_level):
    """The highest-level record of an `_alllevel_map` entry."""
    return by_level[max(by_level)]

def _cardlevel_maxmult(idx):
    """card-level group_id -> (max_level, mult) from CardLevel."""
    db = open_table("CardLevel", idx)
    g = {}
    for r in db.query("SELECT group_id,level,data FROM CardLevel"):
        gid = r["group_id"].decode(); lv = int(r["level"]); mult = decode_record(r["data"])[3][0]
        if gid not in g or lv > g[gid][0]:
            g[gid] = (lv, mult)
    db.close()
    return g

def _bloom_mult(idx):
    """rarity potential group_id -> full-bloom stat multiplier (1 + sum(effect_type 4 permil)/1000)."""
    db = open_table("CardPotential", idx)
    perm = {}
    for r in db.query("SELECT group_id,effect_type,data FROM CardPotential"):
        gid = r["group_id"].decode(); f = decode_record(r["data"])
        if f.get(3, [None])[0] == 4:              # effect_type 4 == all-parameter permil bonus
            perm[gid] = perm.get(gid, 0) + f.get(4, [0])[0]
    db.close()
    return {gid: _f32(_f32(1.0) + _f32(_f32(v) / _f32(1000.0))) for gid, v in perm.items()}

def _max_stats(ratio, level_group, potential_group, GM, BM):
    """maxed stat = ceil(ceil(ratio_i * (CardLevel_mult(maxlvl)/1000)) * bloom_mult), binary32.

    The game rounds UP, not to nearest: with round() 32 of the 50 transcribed SSR cards came
    out exactly 1 low on their two smaller stats and exact on the largest (which lands on an
    integer). ceil() reproduces all three stats exactly.

    Single precision and this operand order follow the game's `CardStatus::.ctor`
    (`Vision.Common.Core`, RVA 0x52acff0) -- see `src/hbuilder/domain/collection.py`. At MAX
    level and full bloom it happens to agree with a binary64 evaluation on every card, but
    it diverges at some lower-level points, so the chain is kept identical to the domain
    module's rather than left to drift.

    ⛔ TWO ceilings. The level scaling becomes an integer BEFORE the bloom multiplier
    applies. Against the wire over `dumps/deck_oracle.json`, one ceiling reproduces about
    half the decks' Member Parameter and two reproduce all but a few stale captures. At
    max level and full bloom the second ceiling moves over half the cells, every one by
    exactly +1.

    ⚠ A check of this chain against `src/hbuilder/domain/collection.py` is CIRCULAR: both
    implement the same assumption and agree whatever the rule. The check that means
    something compares against the SERVER's own `card_potential_parameter`, the only input
    in this loop that is not computed locally. Keep it that way."""
    if not (ratio and level_group in GM and potential_group in BM):
        return None
    mult = GM[level_group][1]; bloom = BM[potential_group]
    per_mille = _f32(_f32(mult) / _f32(1000.0))
    s = [math.ceil(_f32(_f32(math.ceil(_f32(_f32(r) * per_mille))) * _f32(bloom)))
         for r in ratio]
    return {"performance": s[0], "technique": s[1], "sense": s[2], "total": sum(s),
            "at": {"level": GM[level_group][0], "bloom_mult": round(bloom, 3)}}

_TAG_RE = re.compile(r"\[/?[a-z]+(?:=[a-z_0-9]+)?\]")

def _lang(table, idx):
    """A Lang*_Eng master -> {la-key: English text}. These 124 `Lang*_Eng` tables are where
    every `la-*` key resolves; they are ordinary SQLCipher masters listed by Master/Get, which
    is why the text is in no asset bundle, no Octo asset and no metadata string literal."""
    db = open_table(table, idx)
    out = {}
    for r in db.query("SELECT id,data FROM Lang"):
        f = decode_record(r["data"])
        v = f.get(1000, [None])[0]
        if isinstance(v, bytes):
            out[r["id"].decode()] = _TAG_RE.sub("", v.decode("utf-8", "replace")).strip()
    db.close()
    return out


# Costume.f4 is the outfit skill id, and it is spelled `live_leader_skill-<card id>` --
# it carries the WHOLE card id, which is what makes the card -> costume direction
# recoverable exactly rather than by string surgery on the id's rarity segment.
_LEADER_SKILL_PREFIX = "live_leader_skill-"


def _costume_by_card(costumes):
    """card id -> costume id, inverted from Costume.f4.

    ⚠ `Card.f23` is EMPTY on every 3★ card, and resolving the outfit through it alone
    drops their outfit skills. The 3★ card wears the character's `Default` outfit rather
    than a card-specific one, so the game leaves the card's own costume reference blank --
    but the Costume row and its LiveLeaderSkill both exist, and that skill is worth
    **+15.0 % all stats, ungated, target all** on every one of them, exactly what
    `data/cards.json` carries.

    Inverting Costume.f4 resolves every card with no duplicates and no orphans, and
    **agrees with `Card.f23` on every card where f23 is populated** -- so it is a strict
    superset of the f23 path. `_costume` asserts that agreement per card rather than
    trusting this comment."""
    by_card = {}
    for costume_id, cos in costumes.items():
        leader_skill_id = cos["leader_skill_id"]
        if not leader_skill_id or not leader_skill_id.startswith(_LEADER_SKILL_PREFIX):
            continue
        card_id = leader_skill_id[len(_LEADER_SKILL_PREFIX):]
        if card_id in by_card:
            raise ValueError(
                f"two costumes claim card {card_id!r}: {by_card[card_id]!r} and {costume_id!r}"
            )
        by_card[card_id] = costume_id
    return by_card


def _costume(card_id, f23, by_card, costumes, LEAD, LANG_COS):
    """The outfit the card ships with, plus its resolved outfit skill.

    LiveLeaderSkill IS the costume/outfit skill: every card carries one, every effect
    targets `all`, and some are gated. `name_key` is a `la-cos-*` localization key -- the
    display name `name` is resolved through LangCostume_Eng, so it is the real in-game
    name (`Default` for the 3★ outfits).

    Resolution is by the Costume.f4 inversion (see `_costume_by_card`), with `Card.f23`
    kept as a cross-check: where the card names a costume itself, the two MUST agree."""
    costume_id = by_card.get(card_id)
    if f23 and f23 != costume_id:
        raise ValueError(
            f"card {card_id!r}: Card.f23 says costume {f23!r} but Costume.f4 inverts to "
            f"{costume_id!r} -- the two masters disagree, refusing to pick one"
        )
    if not costume_id:
        return None
    cos = costumes.get(costume_id)
    if not cos:
        return {"id": costume_id, "name_key": None, "name": None, "skill": None}
    lead = LEAD.get(cos["leader_skill_id"])
    return {"id": costume_id, "name_key": cos["name_key"],
            "name": LANG_COS.get(cos["name_key"]),
            "skill": skills.resolve_leader(lead) if lead else None}


def main():
    idx = json.load(open(os.path.join(DUMPS, "master_index.json")))
    # skills: id -> {level: record}. `cards_enriched.json`'s own `skills` block
    # stays at max level (it is what `data/cards.json` describes); the full
    # ladder rides alongside it as `skills_by_level` for the bloom down-level.
    ACT = _alllevel_map(idx, "LiveActiveSkillLevel")
    PAS = _alllevel_map(idx, "LivePassiveSkillLevel")
    SPE = _alllevel_map(idx, "LiveSpecialSkillLevel")
    GM = _cardlevel_maxmult(idx)     # level_group -> (maxlvl, mult)
    BM = _bloom_mult(idx)            # potential_group -> full-bloom multiplier
    # Costume master: costume id -> (name localization key, leader/outfit skill id).
    # The outfit skill a costume grants is Costume.f4, and inverting it is how a card
    # finds its costume -- Card.f23 is blank on every 3★ card (see `_costume_by_card`).
    costumes = {}
    kdb = open_table("Costume", idx)
    for row in kdb.query("SELECT id,data FROM Costume"):
        f = decode_record(row["data"])
        costumes[row["id"].decode()] = {"name_key": _s(f, 3), "leader_skill_id": _s(f, 4)}
    kdb.close()
    COSTUME_BY_CARD = _costume_by_card(costumes)
    # localization: card titles, costume names, and the generated skill description text
    LANG_CARD = _lang("LangCard_Eng", idx)
    LANG_COS = _lang("LangCostume_Eng", idx)
    LANG_DESC = {}
    for t in ("LangGeneratedLiveActiveSkillLevel_Eng", "LangGeneratedLivePassiveSkillLevel_Eng",
              "LangGeneratedLiveSpecialSkillLevel_Eng", "LangGeneratedLiveLeaderSkill_Eng"):
        LANG_DESC.update(_lang(t, idx))
    LEAD = {}
    ldb = open_table("LiveLeaderSkill", idx)
    for row in ldb.query("SELECT id,data FROM LiveLeaderSkill"):
        LEAD[row["id"].decode()] = decode_record(row["data"])
    ldb.close()
    # Character: chr-id -> member
    chars = {}
    cdb = open_table("Character", idx)
    for row in cdb.query("SELECT id,data FROM Character"):
        f = decode_record(row["data"])
        chars[row["id"].decode()] = {
            "member": _s(f, 4), "member_short": _s(f, 5), "generation": _s(f, 30),
            "generation_group": _s(f, 1000),
        }
    cdb.close()
    # gRPC stats: card id -> stats
    # stat label order CONFIRMED against the in-game card screen (Aki 5-star:
    # 3765/4227/6157 == in-game 3780/4242/6172 minus a flat +15/stat account bonus):
    #   stat3 = Performance, stat4 = Technique, stat5 = Sense.
    # These are BASE card stats; the in-game display adds account-wide bonuses (green
    # skill-tree nodes / Connect Bonus), which cards.json should model separately.
    stats = {}
    cp = os.path.join(DUMPS, "card_params.json")
    if os.path.exists(cp):
        for r in json.load(open(cp)):
            stats[r["id"]] = {"performance": r["stat3"], "technique": r["stat4"],
                              "sense": r["stat5"], "total": r["total"]}
    # Card master -> join
    mdb = open_table("Card", idx)
    out = []
    for row in mdb.query("SELECT id,data FROM Card"):
        cid = row["id"].decode(); f = decode_record(row["data"])
        chr_id = _s(f, 2)
        ch = chars.get(chr_id, {})
        rec = {
            "id": cid,
            "card_num": cid.split("-")[1],
            "rarity": cid.split("-")[3],
            "rarity_rank": _s(f, 4),
            "attribute": _s(f, 5),                 # card type/attribute (1/2/3)
            "chr_id": chr_id,
            "member": ch.get("member"),
            "member_short": ch.get("member_short"),
            "generation": ch.get("generation"),
            "name_key": _s(f, 3),                  # localization key -> display title (text master)
            "stat_ratio_permil": [f.get(7, [None])[0], f.get(8, [None])[0], f.get(9, [None])[0]],
            "stats": stats.get(cid),               # current owned-card stats (gRPC), None if unowned
            "stats_max": _max_stats([f.get(7, [0])[0], f.get(8, [0])[0], f.get(9, [0])[0]],
                                    _s(f, 6), _s(f, 14), GM, BM),  # computed fully-maxed stats
            "title": LANG_CARD.get(_s(f, 3)),
            "costume": _costume(cid, _s(f, 23), COSTUME_BY_CARD, costumes, LEAD, LANG_COS),
            # the game's own English skill text, for eyeball review of every mapping decision
            "descriptions": {
                "ACTIVE": LANG_DESC.get(f"la-generated-live_active_skill-{cid}.2-description"),
                "PASSIVE": LANG_DESC.get(f"la-generated-live_passive_skill-{cid}.2-description"),
                "SPECIAL": LANG_DESC.get(f"la-generated-live_special_skill-{cid}.2-description"),
                "COSTUME": LANG_DESC.get(f"la-generated-live_leader_skill-{cid}-description"),
            },
            "skills": {
                "ACTIVE":  skills.resolve_active(_maxlevel(ACT[_s(f, 12)]))  if _s(f, 12) in ACT else None,
                "PASSIVE": skills.resolve_passive(_maxlevel(PAS[_s(f, 11)])) if _s(f, 11) in PAS else None,
                "SPECIAL": skills.resolve_special(_maxlevel(SPE[_s(f, 13)])) if _s(f, 13) in SPE else None,
            },
            # every level of each slot, keyed by level. The bloom step that buys
            # each kind is in `domain/collection.SKILL_BLOOM_STEP`; a card below
            # it has the LOWER row, not a scaled version of the higher one.
            "skills_by_level": {
                "ACTIVE": {str(lv): skills.resolve_active(rec)
                           for lv, rec in ACT.get(_s(f, 12), {}).items()},
                "PASSIVE": {str(lv): skills.resolve_passive(rec)
                            for lv, rec in PAS.get(_s(f, 11), {}).items()},
                "SPECIAL": {str(lv): skills.resolve_special(rec)
                            for lv, rec in SPE.get(_s(f, 13), {}).items()},
            },
        }
        out.append(rec)
    mdb.close()
    out.sort(key=lambda r: r["id"])
    dst = os.path.join(DUMPS, "cards_enriched.json")
    json.dump(out, open(dst, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
    with_stats = sum(1 for r in out if r["stats"])
    print(f"{len(out)} cards -> {dst}  ({with_stats} with gRPC stats, {len(out)-with_stats} metadata-only)")
    print(f"members resolved: {len(set(r['member'] for r in out if r['member']))}")

if __name__ == "__main__":
    main()
