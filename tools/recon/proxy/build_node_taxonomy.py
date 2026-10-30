"""Build the per-unit board-node taxonomy DB (read-only analysis).

Writes ONE file: dumps/board_node_taxonomy.json. Does not touch src/data/state
or the network. Classifies every node in skilltree.json, cross-validates the
classifier against src/hbuilder.domain.board on every node that appears in the
account's board.json, and records drawn state + account-bonus inventory.
"""
import json, sys, os
from pathlib import Path
from collections import Counter, defaultdict

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
from hbuilder.domain import board as B  # noqa

D = Path(__file__).resolve().parent / "dumps"
st = json.load(open(D / "skilltree.json", encoding="utf-8"))
bstate = json.load(open(D / "board_state.json", encoding="utf-8"))
bjson = json.load(open(D / "board.json", encoding="utf-8"))
snap = json.load(open(D / "account_snapshot_main.json", encoding="utf-8"))
try:
    col = json.load(open(ROOT / "state" / "collection.json", encoding="utf-8"))
except Exception:
    col = {}

branches = st["branches"]
nodes = st["nodes"]
members = {m["chr"]: m["name"] for m in st["members"]}
name_by_chr = members

COLOR = {"R": "red", "B": "blue", "G": "green", "Y": "yellow", "S": "connect"}

def scope_from_tgt(tgt, branch):
    """Faithful reimplementation of board._scope_for."""
    if tgt is None:
        return ("account", None)  # song nodes: forced account in parser
    if tgt.startswith("character_grouping-"):
        # character_grouping-grp-XXX-always
        grp = tgt[len("character_grouping-"):]
        grp = grp.rsplit("-always", 1)[0]
        return ("grouping", grp)
    if tgt.endswith("set_live_leader"):
        return ("leader", None)
    if tgt.startswith("skill_tree_character"):
        return ("member", None)
    if tgt.startswith("all"):
        return ("account", None)
    if branch == "Y":
        return ("account", None)
    return ("other", None)

def classify_effect(t, p, tgt, branch):
    """Return (kind, stat, scoping_class, conditional, ignored, family_label)."""
    scope, grouping = scope_from_tgt(tgt, branch)
    conditional = bool(tgt) and "passive_trigger" in tgt
    # song score bonus (Y): t holds the full effect id
    if isinstance(t, str) and t.startswith("skill_tree_effect-live_score_bonus_add_permil_up_by_music"):
        return ("song_score_bonus", "", "per_song/yellow", False, False, "live_score_bonus_add_permil_up_by_music")
    tl = (t or "").lower()
    statmap = {"all": "all", "sense": "sense", "perf": "performance", "tech": "technique"}
    if tl in statmap:
        kind = "rate" if p else "stat"
        fam = {"all": "all_parameter_up", "sense": "sense_up",
               "perf": "performance_up", "tech": "technique_up"}[tl]
        if p:
            fam += "_permil_up"
        return (kind, statmap[tl], _class(scope), conditional, False, fam)
    if tl == "act.power":
        return ("skill_effect_up", "", _class(scope), conditional, False,
                "live_active_skill_effect_up_permil_up")
    if tl == "life":
        return ("ignored", "", "ignored(reward/life)", conditional, True, "life_up")
    if tl.startswith("live reward") or tl.startswith("work") or "reward" in tl:
        return ("ignored", "", "ignored(reward/life)", conditional, True, "reward")
    if tl.startswith("live deck leader active skill"):
        return ("ignored", "", "ignored(reward/life)", conditional, True, "live_deck_leader_active_skill")
    if tl == "act.prob" or "activation rate" in tl:
        return ("activation_rate_up", "", _class(scope), conditional, False,
                "live_active_skill_activation_probability_up_permil_up")
    if tl == "act.cd" or "activation frequency" in tl or "cool" in tl:
        return ("cooldown_shorten", "", _class(scope), conditional, False,
                "live_active_skill_cool_time_shorten_permil_up")
    return ("unknown", "", _class(scope), conditional, False, "UNKNOWN:" + str(t))

def _class(scope):
    return {"leader": "leader_only/red", "member": "member/blue",
            "account": "collection_wide/green", "grouping": "grouping/green",
            "per_song": "per_song/yellow", "other": "other"}.get(scope, scope)

def node_effect(nid, n):
    """Return the representative effect dict (shared e, or first chr variant)."""
    if n.get("e"):
        return n["e"], False
    if n.get("chr"):
        return list(n["chr"].values())[0], True
    return None, False

# ---- Build template ----
template = {}
color_counter = defaultdict(Counter)
for nid, n in nodes.items():
    br = n["br"]
    color = COLOR.get(br, br)
    if br == "S":
        template[nid] = {"branch": br, "color": color, "tier": n.get("lv"),
                         "kind": "connect_slot", "scoping_class": "connect_slot",
                         "req": n.get("req")}
        color_counter[color]["connect_slot"] += 1
        continue
    e, per_char = node_effect(nid, n)
    if e is None:
        template[nid] = {"branch": br, "color": color, "tier": n.get("lv"),
                         "kind": "empty", "scoping_class": "other", "req": n.get("req")}
        continue
    t = e.get("t"); p = e.get("p"); tgt = e.get("tgt")
    # song nodes carry tgt inside t; grouping nodes carry tgt per-char
    kind, stat, sclass, cond, ign, fam = classify_effect(t, p, tgt, br)
    mag = e.get("v")
    template[nid] = {
        "branch": br, "color": color, "tier": n.get("lv"),
        "family": fam, "kind": kind, "stat": stat,
        "magnitude": mag, "permil": bool(p) if not per_char else None,
        "target": tgt if tgt else (e.get("tr")),
        "scoping_class": sclass, "conditional": cond, "ignored": ign,
        "per_character": per_char, "req": n.get("req"),
        "desc": e.get("d", "")[:80],
    }
    color_counter[color][sclass if not ign else "ignored(reward/life)"] += 1

# per-character variants for the 15 chr nodes
char_variants = defaultdict(dict)
for nid, n in nodes.items():
    if not n.get("chr"):
        continue
    for chrid, e in n["chr"].items():
        t = e.get("t"); p = e.get("p"); tgt = e.get("tgt")
        kind, stat, sclass, cond, ign, fam = classify_effect(t, p, tgt, n["br"])
        char_variants[chrid][nid] = {
            "magnitude": e.get("v"), "target": tgt, "scoping_class": sclass,
            "song_scope": e.get("tr"), "kind": kind,
        }

# ---- Cross-validate classifier vs board.py on every drawn effect id ----
mismatches = []
checked = 0
for holomem, rows in bjson["characters"].items():
    for row in rows:
        nid = row["node"]
        for eid in row["effects"]:
            try:
                be = B.parse_effect_id(eid, branch=nid[:1])
            except B.BoardError as exc:
                mismatches.append((nid, eid, "PARSE_ERROR", str(exc)))
                continue
            checked += 1
            # compare scope class
            code_scope = be.scope
            code_class = _class(code_scope if be.kind != "song_score_bonus" else "per_song")
            tmpl = template.get(nid, {})
            my_class = tmpl.get("scoping_class")
            # song nodes: parser forces scope account but class per_song;
            # ignored (reward/life) nodes: parser forces scope account; they are
            # labelled 'ignored(reward/life)' since they never reach power or score.
            if be.kind == "song_score_bonus":
                my_ok = my_class == "per_song/yellow"
            elif be.kind == "ignored":
                my_ok = my_class == "ignored(reward/life)"
            else:
                my_ok = my_class == code_class
            if not my_ok:
                mismatches.append((nid, eid, code_class, my_class))

# ---- Drawn state per unit ----
units = {}
for chrid, name in members.items():
    drawn = bstate.get(chrid, {}).get("nodes", [])
    slots = bstate.get(chrid, {}).get("slots", {})
    dc = Counter(x[0] for x in drawn)
    units[chrid] = {
        "name": name,
        "drawn_nodes": drawn,
        "drawn_by_color": {COLOR.get(k, k): v for k, v in dc.items()},
        "connect_slots": slots,
        "char_specific_nodes": char_variants.get(chrid, {}),
    }

# ---- Per-color scoping summary (stated rule vs code) ----
color_summary = {
    "green": {
        "branch": "G", "hue": branches["G"]["hue"],
        "owner_rule": "affects even when the unit is NOT on the team (collection-wide/always-on)",
        "code_rule": "board._scope_for: target 'all-always' -> scope 'account'; "
                     "resolve() iterates ALL characters' boards regardless of team "
                     "membership and _recipients returns all 5 team members for account "
                     "scope (board.py:1244,1287). So a green node on ANY unit's board "
                     "boosts the whole team even if that unit is benched.",
        "code_ref": "board.py:1244 (_scope_for 'all'->account), board.py:1287 (_recipients account->members), board.py:1327 (resolve loops all characters)",
        "matches_owner": True,
        "nuance": "grouping green (G-008/011/021) is 'account'-family but character_grouping "
                  "scope -> only reaches team members sharing the unlocking unit's generation "
                  "(board.py:1262 _reaches grouping). Still collection-wide in the sense the "
                  "OWNING unit need not be on the team.",
    },
    "yellow": {
        "branch": "Y", "hue": branches["Y"]["hue"],
        "owner_rule": "affects songs of that specific unit (per-song score bonus)",
        "code_rule": "board._scope_for: Y song nodes parse to family "
                     "live_score_bonus_add_permil_up_by_music -> kind song_score_bonus, "
                     "forced scope 'account' + song_scope in {solo,group,all} "
                     "(board.py:1212-1228). song_score_bonus_for() aggregates PER SONG over "
                     "that song's own singers filtered by musicSingerType (board.py:941). "
                     "The non-song Y nodes (ic=15 'Work') are Holowork REWARD families -> "
                     "kind 'ignored' (board.py:1160 _IGNORED_FAMILY_PREFIXES).",
        "code_ref": "board.py:1212-1228 (song parse), board.py:941 (song_score_bonus_for per song)",
        "matches_owner": True,
        "nuance": "Owner's 'that specific unit' is imprecise: the contributing set is the "
                  "SONG's cast, not one unit. A yellow node contributes to a song when its "
                  "OWNING holomem is among that song's credited singers, at scope solo(1)/"
                  "group(unit)/all(whole-roster). Also Y is DUAL: ~half the Y nodes are "
                  "Holowork rewards (ignored, never score).",
    },
    "red": {
        "branch": "R", "hue": branches["R"]["hue"],
        "owner_rule": "applies only if that unit is the LEADER (leader-scope)",
        "code_rule": "board._scope_for: target ends 'set_live_leader' -> scope 'leader'; "
                     "_recipients pays the WHOLE team but only when leader.member==holomem "
                     "(board.py:1240,1283). Applies to all 5 members, gated on leadership.",
        "code_ref": "board.py:1240 (_scope_for set_live_leader->leader), board.py:1283 (_recipients leader)",
        "matches_owner": True,
        "nuance": "R is a MIX of kinds: flat stat, permil rate, and the score-critical "
                  "skill_effect_up (Act.Power, R-002/032/034/049/051/053/061). R-002/044/062/063 "
                  "carry a passive_trigger 'music_skill_tree_character' -> conditional: only "
                  "fires when the leader is also a credited singer of the song "
                  "(conditional_skill_effect_up_for / conditional_stat_power_for). R also holds "
                  "Life + reward + leader-active-skill nodes -> ignored.",
    },
    "blue": {
        "branch": "B", "hue": branches["B"]["hue"],
        "owner_rule": "applies if that unit is a team MEMBER (member-scope)",
        "code_rule": "board._scope_for: target starts 'skill_tree_character' -> scope 'member'; "
                     "_reaches pays ONLY that holomem's own card when card.member==holomem "
                     "(board.py:1242,1260). So a blue node reaches only its owner's card, and "
                     "only while she is one of the five members.",
        "code_ref": "board.py:1242 (_scope_for skill_tree_character->member), board.py:1260 (_reaches member)",
        "matches_owner": True,
        "nuance": "Blue also supplies the ONLY activation_rate_up / cooldown_shorten nodes "
                  "(uptime axis, all branch B / scope member, board.py:530-540). Leader's own "
                  "blue nodes are UNKNOWN if she is not also a member; code default = no "
                  "(board.py docstring, 'in the unit' excludes the leader slot).",
    },
    "connect": {
        "branch": "S", "hue": branches["S"]["hue"],
        "owner_rule": "(not in the four color rules) place a member card here to multiply nodes",
        "code_rule": "S nodes are the 4 Connect slots; a placed card multiplies every unlocked "
                     "node in its fixed shape by (1 + connect_permil/1000), CEILED for stat "
                     "(board.py:289 scaled). Not a scoping class - a magnitude multiplier.",
        "code_ref": "board.py:289-337 (BoardEffect.scaled), board.py:80 (module docstring)",
        "matches_owner": True,
        "nuance": "Connect Bloom level is not recoverable from captured state; "
                  "connect_multiplier is an INPUT.",
    },
}

# ---- Account bonus inventory ----
player = col.get("player", {})
account_bonuses = {
    "memory_bonus": {
        "what": "Poster/collection bonus feeding the Member Parameter (power) line",
        "formula": "R permil = min(2t, t+30) where t=min(posters,50); "
                   "applied = ceil(MemberParameter * R/1000). Cap 80 permil (8.0%) at 50 posters.",
        "source_code": "collection.py:1013-1032 memory_bonus_permil; MATH.md 2.5; DECK-SCORE.md",
        "attested": "ATTESTED from master PosterCollectEffect (50 rows, min(2t,t+30), 0 violations)",
        "account_now": {"posters_player_block": player.get("posters"),
                        "posters_snapshot_user_poster_list": len(snap.get("posters", [])),
                        "memory_permil": B.__dict__ and None},
        "reconciled": "MEASURED 44.097-44.300 permil of Member Parameter on 5 decks, 0.46% spread "
                      "(DECK-SCORE.md L83,330). Formula bit-exact vs master; SUMMAND of Overall Power. "
                      "Not yet wired into src scoring (UNCOMPUTED in the live scorer path).",
    },
    "member_power_up": {
        "what": "'upgrade units to lvl 20+' bonus; card_training_evaluation line",
        "formula": "P permyriad = SUM over 5 member slots of p(rarity,level) step ladder; "
                   "Overall Power *= (1 + P/10000). Uniform ~x1.0035, ranking-safe. Cap 5000 permyriad (50%).",
        "source_code": "collection.py:1042-1069 max_member_power_up_permyriad + DerivedMemberPowerUp; "
                       "MATH.md 2.4; scoring_config.py",
        "attested": "ATTESTED CardLevel.live_deck_power_permyriad_up field5, 1890 rows/27 groups; "
                    "server ships LiveDeckPowerPermyriadUpByCardLevelUp directly. Client never computes it.",
        "account_now": {"power_up_permyriad_player_block": player.get("power_up_permyriad"),
                        "note": "'auto' -> derived from owned cards each run (derived_member_power_up_permyriad)"},
        "reconciled": "MEASURED 34.10-34.80 permyriad of OP on 5 decks (DECK-SCORE.md L84,329). "
                      "Uniform multiplier -> ranking-SAFE. Derived value replaces stale hand-typed 951.",
    },
    "poster_collection": {
        "what": "Posters own the Memory Bonus (above). Snapshot user_poster_list drives the count.",
        "source_code": "collection_from_snapshot.py merges player.posters (owner 2026-08-18); "
                       "collection.py MEMORY_BONUS_MAX_POSTERS=50",
        "account_now": {"owned_posters": len(snap.get("posters", [])),
                        "player_block_posters": player.get("posters")},
        "reconciled": "Poster COUNT is the only input; bonus math is memory_bonus above.",
    },
    "player_level": {
        "what": "Not a bonus; gates board node availability (cd-player_level-N, N in {10,15,20,25,30}).",
        "source_code": "collection.py:1072-1081 PLAYER_LEVEL_MAX=50",
        "account_now": {"level": player.get("level")},
        "reconciled": "ATTESTED PlayerLevel master funds no board budget, only availability.",
    },
}
# fill memory permil now
try:
    account_bonuses["memory_bonus"]["account_now"]["memory_permil"] = B.__dict__.get("memory_bonus_permil")  # placeholder
except Exception:
    pass
from hbuilder.domain.collection import memory_bonus_permil
account_bonuses["memory_bonus"]["account_now"]["memory_permil"] = memory_bonus_permil(player.get("posters"))

DB = {
    "_meta": {
        "generated": "2026-08-22",
        "purpose": "Foundation taxonomy for brute-force server-bonus extraction: per-unit "
                   "board node taxonomy with effect+scoping, color->scoping verified vs code, "
                   "account-bonus inventory.",
        "sources": "skilltree.json (template+per-char), board_state.json (drawn, same instant), "
                   "board.json (drawn effect ids), account_snapshot_main.json, state/collection.json, "
                   "src/hbuilder/domain/board.py + collection.py",
        "node_count": len(nodes),
        "unit_count": len(members),
        "classifier_validation": {
            "drawn_effect_ids_checked_vs_board_py": checked,
            "mismatches": mismatches,
            "verdict": "PASS - classifier agrees with board.parse_effect_id on all drawn nodes"
                       if not mismatches else "MISMATCHES FOUND",
        },
    },
    "branches": branches,
    "color_scoping_verification": color_summary,
    "node_template": template,
    "template_color_breakdown": {c: dict(cc) for c, cc in color_counter.items()},
    "units": units,
    "account_bonus_inventory": account_bonuses,
}

out = D / "board_node_taxonomy.json"
out.write_text(json.dumps(DB, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
print("checked", checked, "mismatches", len(mismatches))
for m in mismatches[:20]:
    print("  MISMATCH", m)
print("template colors:", {c: dict(cc) for c, cc in color_counter.items()})
print("wrote", out, out.stat().st_size, "bytes")
# quick sanity: distinct scoping classes present
allclasses = Counter(v.get("scoping_class") for v in template.values())
print("scoping classes:", dict(allclasses))
