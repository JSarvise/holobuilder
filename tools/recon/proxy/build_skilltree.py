# Join the SkillTree* master tables into one explorer-ready JSON: the Holomem Board.
#
# GEOMETRY, pinned to ground truth.
# SkillTreeNodePosition stores a node as (f3, f4) = (x, y) with y pointing UP, and the game
# rotates the board a quarter turn to draw it, so
#
#     screen_x =  x        screen_y = -y
#
# which puts the R arm at the top, G at the bottom, and B/Y left and right. There is no single
# shared layout: Character.f24 picks one of four `tree-model-00N`, which differ only by mirroring
# the B/Y pair and/or the sub-branches of R, so every member has to be drawn with their own model.
#
# Verified against a screenshot of Ninomae Ina'nis (chr-04003, tree-model-002) plus her real
# unlocked set from dumps/board_state.json: the rendered rows match the capture cell for cell
# (e.g. screen row -8 is 16 nodes spanning x -6..+9, of which x -1..+5 are unlocked), and the
# 29 stored ids are the 28 the game reports plus the free starting node S-001.
#
# COLOUR = the group_id prefix, and it is the in-game colour. The game's own help text
# (LangHelpContent_Eng ::
# la-help_category-help-character-3-text) names the tile types, and every node's effect target
# agrees with it:
#     R  red     Leader type   - 63 nodes, all `set_live_leader`      (only while she leads)
#     B  blue    Member type   - 31 nodes, all `skill_tree_character` (her own card, always)
#     G  green   Support type  - 182 rows, `character_grouping`/`all`, always
#     Y  yellow  Support type  - 43 rows, Holowork/minigame rewards, always
#     S  ---     Connect nodes - the 4 slots you place a member card into
#
# CONNECT BONUS: a card placed on a Connect node multiplies the Holomem Board effects of the
# tiles in its extent ("Grants Holomem Board Effect UP 150% for all within range"). The extent is
# a set of cells relative to the slot, in the same coordinate system as the node positions. Each
# effect has two levels; level 2 is what CardPotential grants at upgrade 5 (effect_type 5, which
# exists only for rarity 4/5) -- i.e. the card at max Bloom. Only rarity 4/5 cards have one.
#
# Runs in proxyenv (needs master_cache -> sqlcipher). Output: dumps/skilltree.json
import json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import master_cache as mc

DUMPS = os.path.join(HERE, "dumps")
OUT = os.path.join(DUMPS, "skilltree.json")
BOARD = os.path.join(DUMPS, "board_state.json")
S = lambda v: v - 2**64 if isinstance(v, int) and v > 2**63 else v

# The extents use the same axis pairing as the node positions -- swapping the axes drops best-slot
# coverage from 499/505 tiles to 485. The signs are NOT determinable from the data (all four
# (+-x, +-y) combinations also cover 499/505, because the four tree-models ARE those mirrors), so
# both were settled by observing the game:
#   y: Mori Calliope 5* is center_area-001, whose raw cells are +y, and in game its buff points UP.
#   x: the six content_area-003 cards ([[-3,0],[-2,0],[-1,0]] here) buff 3 tiles to the LEFT.
# So the extents share the node table's convention exactly, and negating y is right.
EXTENT_SIGN = (1, -1)   # (x, y) -> (x, -y): same convention as the node positions, both axes checked in game

# Tile types, straight from the in-game help text. `hue` is the drawing colour.
#
# The hues are the GAME'S OWN: they are the `SkillLeaderLab` / `SkillMemberLab` /
# `SkillSupport1Lab` / `SkillSupport2Lab` entries of `uPalette-PaletteStore` inside
# `data.unity3d`. They agree to ~1/255 with the node sprite rims sampled independently.
# The store also settles the naming: the game calls the two Support branches Support1 (teal)
# and Support2 (amber), so the G/Y split below is its own.
# S has no game colour — a Connect socket draws the card in it — so its grey is a local choice.
BRANCHES = {
    "R": {"name": "Leader", "hue": "#F45E5A", "when": "only while this member is the unit Leader"},
    "B": {"name": "Member", "hue": "#4373FF", "when": "only while this member is in the unit"},
    "G": {"name": "Support", "hue": "#3BC799", "when": "always, even outside the unit"},
    "Y": {"name": "Support", "hue": "#FFB028", "when": "always — Holowork and minigame rewards"},
    "S": {"name": "Connect", "hue": "#e8eaf0", "when": "place a member card here"},
}
# short badge label for the buff family in an effect id's second segment
LABEL = {
    "all_parameter_up": "All", "all_parameter_up_permil_up": "All",
    "all_parameter_up_for_character_grouping": "All",
    "sense_up": "Sense", "sense_up_permil_up": "Sense",
    "technique_up": "Tech", "technique_up_permil_up": "Tech",
    "performance_up": "Perf", "performance_up_permil_up": "Perf",
    "live_active_skill_activation_probability_up_permil_up": "Act.Prob",
    "live_active_skill_effect_up_permil_up": "Act.Power",
    "live_active_skill_cool_time_shorten_permil_up": "Act.CD",
    "live_special_skill_effect_up_permil_up": "Spc.Power",
    "live_passive_skill_effect_up_permil_up": "Pas.Power",
    "live_score_bonus_add_permil_up_by_music_skill_tree_character": "Score",
    "score_up_permil_up": "Score", "life_up": "Life", "support_up": "Support",
    "work_reward_quantity_up_permil_up": "Work",
}
_EFF = re.compile(r"^skill_tree_effect-(?P<name>.+?)-(?P<value>-?\d+)-skill_tree_effect_target-"
                  r"(?P<rest>.+)$")


def icon_asset(eff_id, suffix=""):
    """The Octo asset that IS this node's icon.

    `SkillTreeEffect.iconAssetIdSuffix` (field 13) gives the game
    away: the id is built by concatenation. The rule, verified against every name in the
    decrypted catalogue --

        img_skill_icon_tree_<effect family, lowercased, underscores stripped><suffix>

    skill_tree_effect-life_up-100-...            -> img_skill_icon_tree_lifeup
    ...-by_music_..._singer_type-... with "-solo" -> ...bymusicskilltreecharacterandmusicsingertype-solo

    These assets are already finished: the magnitude chevrons, the reward composites (gift +
    music note / runner) and the gap that separates a modifier from its base are all painted in.
    Nothing has to be assembled, which is what skill_icons.py exploits.
    """
    core = re.sub(r"-\d+-.*$", "", eff_id.replace("skill_tree_effect-", ""))
    return "img_skill_icon_tree_" + core.lower().replace("_", "") + (suffix or "")


def skill_icon_asset(skill_id):
    """live_active_skill-skill_tree-life_recovery -> img_skill_icon_active_liferecovery"""
    return "img_skill_icon_active_" + skill_id.rsplit("-", 1)[-1].replace("_", "")


def effect(eff_id, texts):
    """'skill_tree_effect-sense_up-100-...' -> a drawable effect record."""
    if not eff_id:
        return None
    m = _EFF.match(eff_id)
    if not m:
        return {"t": eff_id, "v": 0, "p": False, "d": ""}
    name, value = m.group("name"), int(m.group("value"))
    permil = name.endswith("_permil_up")
    desc = texts.get(f"la-generated-{eff_id}-description", "")
    return {"t": LABEL.get(name, name.replace("_", " ")), "v": value, "p": permil,
            "tgt": m.group("rest"), "d": desc}


def lang(table):
    """id -> English string, for one of the generated Lang_Eng tables."""
    out = {}
    for r in mc.rows(table, "SELECT * FROM Lang"):
        txt = (r["data"].get(1000) or [""])[0]
        if txt:
            out[r["id"].rsplit("-description", 1)[0] + "-description"] = txt
    return out


def main():
    eff_text = lang("LangGeneratedSkillTreeEffect_Eng")
    con_text = lang("LangGeneratedSkillTreeConnectEffect_Eng")

    # --- effect -> icon enum ---------------------------------------------------------------
    # `SkillTreeEffect` field 2 is a clean 1:1 enum of the effect FAMILY across all 120 rows
    # (1 performance, 2 technique, 3 sense, 7 all-parameter, 9 life, 12 cooldown, 15 work, ...).
    # It is what picks the white glyph the game draws inside a node -- the board shows a
    # different icon per effect, not one icon for everything.
    # Field 7, where present, names the live_active_skill the node GRANTS -- and for those
    # nodes the board draws that skill's own icon, not one icon for the enum. Two nodes share
    # enum 16 ("Learn a Holomem Skill") and show different glyphs: R-024 grants
    # `judgement_enhance`, R-027 grants `life_recovery` and shows a heart. Only three rows in
    # the whole table carry field 7, but without it those nodes are unfixable.
    # Field 5 is the passive trigger, whose tail is the song's singer type -- `…-solo`,
    # `…-group`, `…-all`. The board draws that as the small badge in the corner of a score
    # bonus node (24 solo / 3 group / 3 all across the Y arm), so it has to be carried too.
    icon_of, skill_of, trig_of, suffix_of = {}, {}, {}, {}
    for r in mc.rows("SkillTreeEffect"):
        d = r["data"]
        eid = d.get(1, [None])[0]
        if eid:
            icon_of[eid] = d.get(2, [None])[0]
            sk = d.get(7, [None])[0]
            if sk:
                skill_of[eid] = sk
            tr = d.get(5, [None])[0]
            if tr:
                trig_of[eid] = str(tr).rsplit("-", 1)[-1]
            # Field 13 is `iconAssetIdSuffix` and it is NOT field 5. Field 5 is the passive
            # TRIGGER group (`…-music_skill_tree_character`), which is a game-logic condition
            # and appears in no asset name; only 13 is part of the id, and it already carries
            # its own leading dash ("-solo").
            sfx = d.get(13, [None])[0]
            if sfx:
                suffix_of[eid] = str(sfx)

    # --- nodes: shared metadata, plus the per-character variants some groups carry -------------
    nodes = {}
    for r in mc.rows("SkillTreeNode"):
        g = r["group_id"]
        d = r["data"]
        # `lv` picks the node icon: the game ships ui_710_ico_node_<colour>_lv1 (one star) and
        # _lv2 (two stars). Field 5 is that level -- 89 of 323 rows are lv2. Field 13 agrees on
        # every row except the four Connect nodes, which it puts in a category of their own (3),
        # and field 4 restates the branch numerically (R2 B3 G4 Y5, S1), matching both the
        # group-id prefix and the ui_710_ico_star_{leader,card,allmember,content} sprite names.
        nd = nodes.setdefault(g, {"br": g[0], "lv": d.get(5, [1])[0],
                                  "req": d.get(7, [None])[0], "chr": {}})
        eff_id = d.get(6, [None])[0]
        e = effect(eff_id, eff_text)
        if e is not None:
            e["ic"] = icon_of.get(eff_id)      # effect-type enum, kept for grouping/debugging
            if eff_id in skill_of:
                e["sk"] = skill_of[eff_id]
            if eff_id in trig_of:
                e["tr"] = trig_of[eff_id]
            # The icon is a finished Octo asset, not something to compose -- see skill_icons.py.
            # `icon` is the node's own; `icon_sk` is the granted skill's, which is what an
            # "addition" node shows (the game ships no tree icon for those at all).
            e["icon"] = icon_asset(eff_id, suffix_of.get(eff_id, ""))
            if eff_id in skill_of:
                e["icon_sk"] = skill_icon_asset(skill_of[eff_id])
        who = d.get(3, [None])[0]
        if who:
            nd["chr"][who] = e
        else:
            nd["e"] = e

    # --- one layout per tree-model, already rotated into screen space --------------------------
    models = {}
    for r in mc.rows("SkillTreeNodePosition"):
        x, y = S(r["data"].get(3, [0])[0]), S(r["data"].get(4, [0])[0])
        models.setdefault(r["group_id"], {})[r["skill_tree_node_group_id"]] = [x, -y]

    # --- members: name + which model the game draws for them -----------------------------------
    members = []
    for r in mc.rows("Character"):
        model = r["data"].get(24, [None])[0]
        if model in models:
            members.append({"chr": r["id"], "name": r["data"].get(4, ["?"])[0], "model": model})
    members.sort(key=lambda m: m["chr"])

    # --- connect-bonus extents, same rotation as the nodes --------------------------------------
    extents = {}
    for r in mc.rows("SkillTreeConnectEffectExtent"):
        name = r["group_id"].replace("skill_tree_connect_effect_extent-", "")
        extents.setdefault(name, []).append([EXTENT_SIGN[0] * int(r["position_x"]),
                                             EXTENT_SIGN[1] * int(r["position_y"])])
    for cells in extents.values():
        cells.sort()

    # --- connect effects: value + the game's own wording, per level (level 2 = max Bloom) -------
    connect = {}
    for r in mc.rows("SkillTreeConnectEffect"):
        d = r["data"]
        lv = int(r["level"])
        c = connect.setdefault(r["id"], {
            "area": d.get(4, [""])[0].replace("skill_tree_connect_effect_extent-", ""),
            "rare": r["id"].rsplit("-", 1)[-1], "levels": {}})
        c["levels"][lv] = {"v": d.get(5, [0])[0],
                           "d": con_text.get(f"la-generated-{r['id']}.{lv}-description", "")}
    for c in connect.values():
        c["levels"] = [c["levels"][k] for k in sorted(c["levels"])]

    # --- which card carries which connect effect (Card field 22) --------------------------------
    card_connect = {}
    for c in mc.rows("Card"):
        cid = (c["data"].get(1) or [None])[-1]
        eid = (c["data"].get(22) or [None])[0]
        if cid and eid in connect:
            card_connect[cid] = eid

    # --- the account's real board, if a capture has been decoded ---------------------------------
    # Two sources, two coverages: unlocked tiles come from User/Get (all holomem at once), the
    # slot placements only from SkillTree/ConnectNode, which reports one holomem per action. So
    # `placed` is normally a subset of `unlocked` -- see board_state.py for why.
    board = json.load(open(BOARD, encoding="utf-8")) if os.path.exists(BOARD) else {}
    unlocked = {c: (v if isinstance(v, list) else v.get("nodes", []))
                for c, v in board.items()}
    placed = {c: v["slots"] for c, v in board.items()
              if isinstance(v, dict) and v.get("slots")}

    out = {"branches": BRANCHES, "nodes": nodes, "models": models, "members": members,
           "extents": extents, "connect": connect, "card_connect": card_connect,
           "unlocked": unlocked, "placed": placed}
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    print(f"{len(nodes)} nodes x {len(models)} layouts, {len(members)} members, "
          f"{len(extents)} connect areas, {len(card_connect)} cards with a connect bonus, "
          f"{len(unlocked)} boards captured ({len(placed)} with slot data) -> {OUT}")


if __name__ == "__main__":
    main()
