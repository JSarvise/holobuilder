# Build a self-contained card explorer from the extracted dataset.
#
# Reads tools/recon/proxy/dumps/cards_review.json (the lossless review file) plus whatever card art
# has been dumped to dumps/card_images/, and writes ONE standalone HTML file with data and images
# inlined -- no server, no build step, no dependencies, no network. Open it in a browser.
#
# A browser over the cards depends on nothing in the scoring model, and it is the fastest way to
# eyeball the extracted values against the game before they are merged into data/cards.json.
#
# Art is optional and may be partial: card_images.py --dump only collects the assets the game
# actually requested during a capture. Cards without it get a typed
# placeholder, and the art directory is globbed at build time -- a future capture drops files in
# and they appear here with no code change.
#
# The generated HTML contains extracted game data and is gitignored, like everything under
# dumps/. This generator is the committable half.
#
# Not quite ONE file: the card portraits AND the card squares are written as separate
# webp files into a `<page>_art/` directory beside the HTML and referenced by relative url. That is
# forced, not preferred -- at native resolution they are far past what base64 can carry, and base64
# is parsed before first paint. Everything else -- chrome, ribbons, badges, the
# font -- is still inlined. The page still opens straight off the filesystem; keep the two together
# when moving them.
#
# Usage: python build_explorer.py [-o cards_explorer.html]
import base64, glob, io, json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import card_state  # noqa: E402  -- hand-authored Lv/bloom; see that module for why


def RARITY_RANK(rarity):
    """'3★' -> 3. The dataset writes the rank as a digit followed by a star."""
    s = str(rarity or "").strip()
    return int(s[0]) if s[:1].isdigit() else None


REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
DUMPS = os.path.join(REPO, "tools", "recon", "proxy", "dumps")
SRC = os.path.join(DUMPS, "cards_review.json")
ART_DIR = os.path.join(DUMPS, "card_images")
TREE_SRC = os.path.join(DUMPS, "skilltree.json")   # optional; produced by build_skilltree.py

# img_card_full_00002-4-cmmn-0000-00.webp  <->  card-00002-4-cmmn-0000-00
ART_PREFIX = "img_card_full_"
SQ_PREFIX = "img_card_thumb_"   # the square the game shows in lists and on Connect nodes
VERT_PREFIX = "img_card_vert_"  # the portrait the game uses for a card in the roster
ART_SUFFIX = ".webp"

# The portrait at NATIVE resolution. card_images/ holds only the game's 320x640 webp variant,
# and shrinking that to 120x240 while the grid draws tiles at >=392 device px under-samples it
# 3.27x, which shows as blur. The 512x1024 PNGs are 90.8 MB as PNG and 10.45 MB re-encoded at webp q80,
# so they cannot be embedded: the page is already ~10 MB and base64 is parsed before first paint.
# They are written as separate files beside the page instead and fetched lazily, per tile.
VERT_NATIVE_DIR = os.path.join(DUMPS, "card_art", "img_card_vert", "512x1024")
VERT_NATIVE_SUFFIX = ".png"
PORTRAIT_QUALITY = 80

# The square has the same two-stage loss. Native 512x512 exists for all 167 cards;
# card_images/ holds only the 320 px webp, which the embedded path shrinks to 160. Derived from
# the drawn sizes rather than picked, the square's largest consumer needs:
#
#   consumer                                     CSS px           x DPR 2   verdict at 160 px
#   card face, portrait missing (grid minmax)    >=196            >=392     under-sampled 2.45x
#   Connect node on the board, 2R+6=28 u @ ZMAX 4 112             224       under-sampled 1.40x
#   .sqchip in the Connect card list             34               68        fine
#
# so 392 is the floor and 512 is the next thing that actually exists. Same treatment as the
# portraits: sidecar files beside the page, relative url, fetched per tile.
SQ_NATIVE_DIR = os.path.join(DUMPS, "card_art", "img_card_thumb", "512x512")
SQ_NATIVE_SUFFIX = ".png"
SQUARE_QUALITY = 80

# Only the degraded build (no natives on disk, or no Pillow) still embeds portraits, at this
# size. The correct value -- the tile's >=196 CSS px x a 2x device ratio, which the 320 px source
# cannot even reach, so no shrink at all -- gives a ~17 MB page for 167 cards. There is no good
# embedded answer at any number, which is the entire argument for the sidecar directory.
EMBED_FALLBACK_MAX_PX = 240
# Same idea for the square: the degraded build keeps this size, blur included,
# because raising it embeds megabytes and the fix is the sidecar, not a bigger constant.
SQ_EMBED_FALLBACK_MAX_PX = 160

# The game's own UI sprites, exported from its Addressables bundles. Embedding these is what makes a tile read as a real card
# instead of an approximation.
CHROME_DIR = os.path.join(DUMPS, "card_chrome")
CHROME = {
    "cute":  "element/ui_999_ico_element_cute_cl.png",
    "pure":  "element/ui_999_ico_element_pure_cl.png",
    "happy": "element/ui_999_ico_element_happy_cl.png",
    "r3":    "rarity/ui_999_thm_card_rarity_r3.png",
    "r4":    "rarity/ui_999_thm_card_rarity_r4.png",
    "r5":    "rarity/ui_999_thm_card_rarity_r5.png",
    "star":  "rarity/ui_999_img_card_star.png",
    # The bloom badge. `FlowerIcon` in the prefab resolves to exactly these two -- filled when
    # the card has any potential upgrades, blank at zero. NOT `ui_999_ico_potential`, which is
    # an all-white silhouette belonging to the character panel.
    "flower":      "bloom/ui_999_img_flower.png",
    "flowerBlank": "bloom/ui_999_img_flower_blank.png",
}

# The generation ribbon is a horizontal 9-slice: 65x96 with border L40 / R25, i.e. a *zero-width*
# middle -- the two caps tile the whole sprite and the seam column is what gets stretched. That
# is why it is cut into caps here instead of being used with CSS `border-image`, which renders an
# empty middle when the slices meet. The seam column is opaque top to bottom, so the stretched
# middle is a plain full-height bar and a solid mask layer reproduces it exactly.
# Skill-tree chrome, from the game's SkillTreeAtlas. The node icons come in exactly the four
# branch colours, and the game's own `ui_710_ico_star_{leader,card,allmember,content}` sprite
# names are what pin the mapping down: R=leader/red, B=card/blue, G=allmember/green,
# Y=content/yellow. `master_SkillTreeNode` field 4 restates it numerically (R2 B3 G4 Y5, S1),
# so three independent sources agree.
SPRITE_DIR = os.path.join(DUMPS, "ui_sprites")
TREE_DIR = os.path.join(SPRITE_DIR, "SkillTreeAtlas")
TREE_BRANCH_COLOUR = {"R": "red", "B": "blue", "G": "green", "Y": "yellow"}
TREE_CHROME = {
    "socket": "ui_710_ico_socket.png",
    "socketlock": "ui_710_ico_socket_lock.png",
    "star": "ui_710_img_star.png",
    "vector": "connect_vector.png",
    "connectstar": "ui_710_img_connect_star.png",
}

# A board node is a plain tinted disc plus a white GLYPH for its effect. That glyph is not
# assembled from generic UI sprites -- it is a finished Octo asset whose id the game builds by
# concatenation, `img_skill_icon_tree_<effect family><iconAssetIdSuffix>`. skill_icons.py pulls
# them out of the local Octo cache; build_skilltree.py emits the name per node as `icon`.
#
# Two earlier passes tried to reconstruct these from CommonIconAtlas and got several wrong,
# because the real assets already contain things no reconstruction would think to add: the
# magnitude chevrons, the reward composites (gift + music note / runner), and the transparent
# GAP that keeps a modifier readable against the base it overlaps.
SKILL_ICON_DIR = os.path.join(DUMPS, "skill_icons")

# 23 of the 24 the master asks for can be on disk. Icons the client has never fetched (e.g. a
# green arm gated behind player level 30) come from the CDN via octo_fetch.py, md5-verified
# against the catalogue.
#
# The 24th, `live_deck_leader_active_skill_addition`, does not exist to be fetched and never
# will: such a node draws the icon of the skill it GRANTS, so the game has no tree icon for it.
# `icon_sk` covers those, which is why this map is empty -- kept as the escape hatch for the
# next asset that turns out to be missing.
TREE_ICON_ALIAS = {}

# `…_level_up` is a hollow FRAME with "Lv" on its base, not a finished icon: the granted skill's
# own icon is meant to sit inside it. That is what distinguishes the pair on the board -- an
# "addition" node shows the skill icon bare, a "level up" node shows the same icon framed. Left
# unfilled, the tile reads as an empty box.
TREE_FRAME_ICONS = ("img_skill_icon_tree_livedeckleaderactiveskilllevelup",)
TREE_GLYPH_FALLBACK = "ui_999_ico_stars"

# The prefab draws these two as siblings, outline on top of base -- so the ribbon is a tinted
# body with a white stroke around it, which is why the tag reads as an outlined ribbon in game.
RIBBON = "label/ui_999_lab_unit_base_wh_9s.png"
RIBBON_OUTLINE = "label/ui_999_lab_unit_outline_wh_9s.png"
RIBBON_BORDER = (40, 25)        # left cap, right cap -- in sprite pixels, read off m_Border

# Generations: label, tint and order all come from the game's own `CharacterGrouping` master
# table via gen_groups.py (-> dumps/gen_groups.json), keyed by chr id (e.g. the label is
# **GAMERS**, not "Gamers"; the ID Gen 1..3 labels are the game's own strings).
GEN_GROUPS = os.path.join(DUMPS, "gen_groups.json")

# Fallback only, for when gen_groups.json has not been generated. The source data carries the
# Japanese unit names ('0期生', 'ゲーマーズ'); these are the English tags the client shows.
GEN_LABEL = {
    "0期生": "Gen 0", "1期生": "Gen 1", "2期生": "Gen 2", "3期生": "Gen 3",
    "4期生": "Gen 4", "5期生": "Gen 5", "ゲーマーズ": "GAMERS",
    "ID1": "ID Gen 1", "ID2": "ID Gen 2", "ID3": "ID Gen 3",
}


def load_gen_groups():
    """{'by_chr': {chr: [grp,…]}, 'groups': {grp: {name, color, …}}} or None."""
    if not os.path.exists(GEN_GROUPS):
        return None
    return json.load(open(GEN_GROUPS, encoding="utf-8"))


def gen_label(gens):
    """['5期生'] -> 'Gen 5'.  Unknown tags (Myth, holoX, ReGLOSS, Advent...) pass through."""
    return " / ".join(GEN_LABEL.get(g, g) for g in gens)


def card_gens(chr_id, raw_gens, groups):
    """[(label, colour, colour2)] for a card, in the game's own display order.

    Prefers the master table joined on chr id; falls back to the Japanese->English map with no
    colour, which makes the CSS fall back to tinting by card type.

    `colour2` is CharacterGrouping field 101, the RIGHT stop of the ribbon's horizontal
    gradient (field 100 is the left) -- see the `.face .gen i` rule for the measurement that
    settled what 101 is for.
    """
    if groups:
        ids = groups["by_chr"].get(chr_id) or []
        if ids:
            return [(groups["groups"][g]["name"], groups["groups"][g]["color"],
                     groups["groups"][g].get("color2")) for g in ids]
    return [(gen_label([g]), None, None) for g in raw_gens]

# Raw: the JS below carries regex escapes (\[, \/) that Python must not touch.
PAGE = r"""<!doctype html>
<meta charset="utf-8">
<title>hololive Dreams — card explorer</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
/* LINE Seed Sans Extra Bold, the face the game actually sets its card chrome in -- the build's
   only Latin TMP atlas is `LINESeedSans_A_XBd`. Embedded rather than assumed-installed so the
   page looks the same anywhere; SIL OFL 1.1 permits that. `fetch_font.py` puts the file in
   place and records its source and hash; when it is missing this rule is simply omitted and the
   stack falls through to the next heavy geometric sans. */
__FONT__
:root {
  --bg:#0d0f16; --panel:#161a24; --sunk:#0a0c12; --line:#252b3a; --fg:#e8eaf0; --dim:#9099ae;
  /* Element colours are the GAME'S, sampled 2026-08-06 from the type-badge sprites in
     `dumps/card_chrome/element/` (`CARD-UI-ASSETS.md` §82): modal opaque pixel of each
     `ui_999_ico_element_*_cl.png` over saturated pixels only (max-min > 100), so the
     white glyph and the cream inner glow do not win. Was #FF6FAE / #4FD1C5 / #FFB340 --
     the middle one mattered, since the game's Pure is a GREEN leaf, not aquamarine. */
  --cute:242,64,104; --pure:13,193,52; --happy:255,167,18; --owned:125,216,125;
  --radius:13px;
}
@media (prefers-color-scheme: light) {
  :root { --bg:#eef0f6; --panel:#fff; --sunk:#f3f5fa; --line:#dde1ec; --fg:#171a23; --dim:#616a80; }
}
* { box-sizing:border-box; }
html { color-scheme:dark light; }
body { margin:0; background:var(--bg); color:var(--fg);
  font:15px/1.5 ui-sans-serif,system-ui,"Segoe UI",Roboto,sans-serif; overflow-x:hidden; }

/* ---- chrome ---------------------------------------------------------- */
/* The drawer deliberately does NOT push the page. Shrinking main animates margin-right, which
   relays out all 167 tiles every frame and drops the grid from 6 columns to 4 -- visibly janky.
   Floating it over the right edge keeps the animation to a single composited transform. */
header { padding:18px 24px 12px;
  background:linear-gradient(180deg,rgba(var(--cute),.10),transparent 70%); }
h1 { margin:0 0 3px; font-size:19px; font-weight:650; letter-spacing:-.01em; }
.sub { color:var(--dim); font-size:13px; }
.controls { display:flex; flex-wrap:wrap; gap:8px; padding:12px 24px; align-items:center;
  position:sticky; top:0; background:color-mix(in srgb, var(--bg) 92%, transparent);
  backdrop-filter:blur(10px); border-bottom:1px solid var(--line); z-index:5; }
input,select { background:var(--panel); color:var(--fg); border:1px solid var(--line);
  border-radius:8px; padding:7px 10px; font:inherit; font-size:13px; }
input:focus-visible,select:focus-visible { outline:2px solid rgba(var(--pure),.7); outline-offset:1px; }
input[type=search] { min-width:210px; flex:1; }
label.chk { display:flex; align-items:center; gap:6px; color:var(--dim); font-size:13px;
  cursor:pointer; user-select:none; }
.count { color:var(--dim); font-size:13px; margin-left:auto; white-space:nowrap; }

/* ---- grid ------------------------------------------------------------ */
main { padding:18px 24px 70px; display:grid; gap:14px;
  grid-template-columns:repeat(auto-fill,minmax(196px,1fr)); }
.tile, aside { --tc:120,130,150; }
/* Equal specificity to the line above, declared after it, so the type wins where one is set. */
.Cute { --tc:var(--cute); } .Pure { --tc:var(--pure); } .Happy { --tc:var(--happy); }
/* overflow:visible so the face's intentional overhangs survive; the face clips its own
   background to its own radius, and `container-type:inline-size` contains layout, not paint. */
.tile { background:var(--panel); border-radius:var(--radius); overflow:visible; cursor:pointer;
  border:1px solid rgba(var(--tc),.30); box-shadow:0 1px 2px rgba(0,0,0,.25);
  transition:transform .13s ease, box-shadow .13s ease, border-color .13s ease; }
.tile:hover { transform:translateY(-3px); border-color:rgba(var(--tc),.75);
  box-shadow:0 8px 22px -8px rgba(var(--tc),.45), 0 2px 6px rgba(0,0,0,.35); }
.tile:focus-visible { outline:2px solid rgba(var(--tc),.9); outline-offset:2px; }
.tile.sel { border-color:rgb(var(--tc)); box-shadow:0 0 0 1px rgb(var(--tc)),
  0 8px 22px -8px rgba(var(--tc),.5); }

/* ---- card face: the game's own layout ---------------------------------
   Every number below is read off the game's own `VerticalCardThumbnail_224` prefab, whose root
   is 224x320 -- so percentages here are <prefab px>/224 across and /320 down, and nothing is
   eyeballed. See tools/recon/CARD-UI-ASSETS.md sec. 4 for the dump and how it was obtained.

   The frame is `ui_999_outline_w4_r40_9s` over a `ui_999_base_r40_9s` mask: a plain rounded
   rectangle, radius 40, stroke 4. It is NOT the hole-punch silhouette in
   ui_800_img_card_base_wh_9s, which belongs to some other screen. Because the shape is a plain
   rounded rect, CSS reproduces it exactly with border-radius -- no sprite needed. */
/* 5:7, matching the game -- NOT the 1:2 of the source portrait. The game crops the tall art to
   a squarer card; using the art's own ratio makes a sliver that looks nothing like it. */
/* radius 40 on a 224x320 box = 17.857% of the width and 12.5% of the height. Both terms are
   required: a single percentage would be 17.857% across but 17.857% down, i.e. an ellipse. */
/* NOT overflow:hidden. The badge (-4), the ribbons (+8) and the stars (+6.5) all overhang the
   card on purpose, and clipping them also flattened the ribbon's rounded end against the card
   edge. A background is clipped by border-radius on its own, so only the rarity strip -- a real
   child element -- needs a clipper, and it gets one in `.clip`. */
.face { position:relative; aspect-ratio:5/7; background-size:cover; background-position:center top;
  background-color:var(--sunk); border-radius:17.857% / 12.5%; }
.face .clip { position:absolute; inset:0; overflow:hidden; border-radius:inherit;
  pointer-events:none; }
/* `ui_999_shadow_b16y8_a35_r40_9s` -- blur 16, y+8, black 35%, same radius 40. */
.tile { container-type:inline-size; }
.face { box-shadow:0 3.57cqw 7.14cqw rgba(0,0,0,.35); }
/* The art is a FILE on disk now, not a data: URI -- see WEB-STACK.md 4d.
   SETTLED FROM THE GAME'S OWN CODE, three independent routes agreeing -- CARD-UI-ASSETS.md 5e:
     ASSET     `VerticalCardThumbnail::SetCardTexture` @5cc11d0 -> GetUpperThumbAddress ->
               Card::GetUpperThumb -> the literal `img_card_vert_{0}`. The PORTRAIT.
     PLACEMENT `CardImage` is a **RawImage**, not an Image. RawImage has no m_PreserveAspect field
               at all, and `RawImageTextureSetter::SetToImage` writes uvRect = (0,0,1,1) -- a
               constant, so there is NO crop, ever. The texture is stretched full-bleed onto the
               224x320 rect. That is `object-fit:fill`, not `cover`.
   MEASURED confirmation with ZERO free parameters: the stretched portrait scores NCC 0.622 mean
   over five roster cards, against 0.450 for a square with three parameters fitted and 0.368 for a
   portrait with three fitted. A model that wins without being tuned is the one to believe.
   RETRACTED: an earlier reading here said the SQUARE, from NCC 0.85-0.97. That sample covered only
   the upper-middle of the card, where a square at 1.082x happens to align with the stretched
   portrait. Overfitting the window, and the window was mine.
   The radius goes on the <img> ITSELF. A wrapper with overflow:hidden was tried and leaked square
   corners past the rounded card: border-radius:inherit on an absolutely-positioned wrapper does not
   clip an absolutely-positioned image inside it. */
.face .port { position:absolute; inset:0; width:100%; height:100%; display:block;
  object-fit:fill; border-radius:inherit; }
/* The white inner frame used to be `inset 0 0 0 1.79cqw` in the rule above. An inset shadow is
   painted with the element's own background layer, i.e. UNDER every child -- fine for a
   background-image, fatal for an <img> child, which would have covered it. So the frame became a
   real element. Painting order here is DOM order: .port, then .ring, then the badges -- which is
   why the element icon and the gen ribbons still overhang the frame the way the game draws them. */
.face .ring { position:absolute; inset:0; border-radius:inherit; pointer-events:none;
  box-shadow:inset 0 0 0 1.79cqw rgba(255,255,255,.92); }
.face:not(.has) {
  background-image:
    repeating-linear-gradient(115deg, rgba(255,255,255,.045) 0 2px, transparent 2px 11px),
    linear-gradient(140deg, rgba(var(--tc),.55), rgba(var(--tc),.16) 58%, rgba(8,10,16,.55)),
    linear-gradient(#11141d,#11141d); }
/* ico_element: 64x64 at anchor top-left, offset (-4, +4) -- the badge deliberately overhangs
   the corner by 4px on both axes. */
.face .el { position:absolute; top:-1.25%; left:-1.786%; width:28.571%; aspect-ratio:1;
  filter:drop-shadow(0 1px 2px rgba(0,0,0,.55)); }
/* The game tints this tag per GENERATION, not per card type. Colours read off in-game
   screenshots (Gen 2 blue, Gen 3/5 green, Gen 4 orange, ReGLOSS red, Myth gold) -- observed,
   not sourced from data, so the palette is a best match rather than the game's own values.
   `--gc` falls back to the card type where a unit has no entry. */
/* CharacterGroupingLabel: 98.7 x 46 each, stacked from the top-right at a 40px pitch, with the
   group itself offset (+8, +4) -- so it too overhangs the right edge. The shape is the game's
   `ui_999_lab_unit_base_wh_9s`, a left-pointing ribbon, applied as a three-layer mask: left cap
   pinned left at its own aspect, right cap pinned right, solid bar between them. `--gc` is the
   tint; it falls back to the card type where a unit has no colour yet. */
.face .gens { position:absolute; top:-1.25%; right:-3.571%; width:44.06%; }
/* 98.7 x 46 gives the aspect directly; stacking at a 40px pitch means overlapping by 6 of the
   ribbon's own 98.7px width, which is what a percentage margin resolves against. */
.face .gen { position:relative; width:100%; aspect-ratio:98.7/46;
  filter:drop-shadow(0 1px 2px rgba(0,0,0,.45)); }
.face .gen + .gen { margin-top:-6.079%; }
.face .gen i, .face .gen u { position:absolute; inset:0;
  -webkit-mask-repeat:no-repeat; mask-repeat:no-repeat;
  -webkit-mask-composite:source-over; mask-composite:add; }
/* `--gc` is a full colour from CharacterGrouping; the `.g-*` classes below only apply when
   gen_groups.json is missing, and then it falls back to tinting by card type.
   MEASURED 2026-08-02 off `test/test_live/team selection inside song.png`: the ribbon is a
   LINEAR HORIZONTAL GRADIENT, field 100 at the left edge and field 101 at the right -- not a
   solid fill, and not the vertical gradient it looks like at a glance. Every column through a
   ribbon is flat and the row ramps smoothly (Gen 3: #63CA36 -> #BFEB8A across 96 px, against
   master's #5BC72E / #C6EE91). Checked on Myth too, where the polarity is reversed -- 100 is
   the PALE one there (#D4BB71 -> #C29F37 sampled, #D6BE78 / #C19D33 in master) -- so this is
   the field order, not "light to dark". `--gc2` falls back to `--gc`, which reproduces the old
   solid fill when only one tint is known. */
.face .gen i { background:linear-gradient(90deg,
    var(--gc, rgb(var(--tc))), var(--gc2, var(--gc, rgb(var(--tc))))); }
.face .gen u { background:#fff; }                  /* ui_999_lab_unit_outline_wh_9s */
.face .gen.plain i { border-radius:999px; }        /* no sprite dump -> plain pill */
/* The game sets this in LINE Seed Sans Extra Bold -- the only Latin face in the build is
   `LINESeedSans_A_XBd SDF Atlas` (the CJK subsets are Noto). TextMeshPro bakes an SDF atlas
   rather than shipping a font file, so there is nothing to extract; LINE Seed is published
   free, so name it first and let it apply wherever it is installed. The fallbacks are the
   nearest heavy geometric sans available by default. */
.face .gen b { position:absolute; inset:0; display:flex; align-items:center;
  justify-content:center; padding-left:6%; color:#fff;
  font-family:"LINE Seed Sans","LINE Seed JP","LINESeedSans",
    "Century Gothic","Futura","Trebuchet MS",system-ui,sans-serif;
  font-weight:800; font-size:clamp(8px,4.2cqw,12px); letter-spacing:.01em; white-space:nowrap;
  /* The game really does draw a face outline, so this is now the game's, not ours. MEASURED off
     the same screenshot: it is a GREY, not black. On Myth -- the pale tint where the stroke is
     thickest and least contaminated -- the purest outline pixel is #81777C, i.e. a neutral grey
     whose blue channel is HIGHER than the gold background's, which a black outline could not
     produce. On Gen 3 the same stroke samples #649367, greener only because a ~2 px stroke at
     this size never reaches full opacity. Against a saturated dark tint (Gen 2 #225AFF) no
     pixel is darker than the background at all, which is consistent with a light-grey outline.
     `paint-order` puts the stroke behind the fill so the glyph keeps its weight; the stroke is
     centred on the outline, so the width is doubled to leave ~0.065em showing outside. */
  paint-order:stroke fill;
  -webkit-text-stroke:.13em rgba(129,119,124,.92); }
/* Fallback palette only (no gen_groups.json). The real values are the game's own, in
   CharacterGrouping fields 100/101, and arrive inline as `--gc`. */
.g-gen0 { --gc:#93C2FF; } .g-gen1 { --gc:#FF6FC6; } .g-gen2 { --gc:#225AFF; }
.g-gen3 { --gc:#5BC72E; } .g-gen4 { --gc:#FFB22F; } .g-gen5 { --gc:#FF79AB; }
.g-gamers { --gc:#63B6FF; } .g-myth { --gc:#D6BE78; } .g-promise { --gc:#E6F37F; }
.g-advent { --gc:#7C7EC9; } .g-holox { --gc:#BD5BE4; } .g-regloss { --gc:#FF5B4C; }
.g-id { --gc:#FF7E06; }
/* NOT `.bar` -- that class already exists for the ownership progress bar and forces height:3px,
   which silently flattened this strip to an invisible sliver. */
/* RarityEdgeDecoration: 224 x 47 full width along the bottom, i.e. 14.6875% of the height --
   but do NOT use that number here. The sprite's rect is 480x100 while its textureRect is only
   480x84 at offset (0,0): 16px of the rect is transparent padding at the TOP, and the exported
   PNG is the tight 84. So the drawn artwork is 84/100 of those 47px, flush with the bottom
   edge: 39.4/320 = 12.31%. Stretching the trimmed PNG to 14.6875% would make it 19% too tall.
   The face's overflow:hidden clips it to the rounded corners, as the game's own Mask node does. */
.face .rbar { position:absolute; left:0; right:0; bottom:0; width:100%; height:12.31%;
  object-fit:fill; display:block; }
/* ---- Lv and bloom, bottom-left ---------------------------------------
   Prefab-exact, and NOT drawn with the `ui_999_num_*` digit sprites this project assumed for
   months. resolve_images.py on `LevelValue` and `PotentialUpgradeCount_On/Off` returns TMP font
   MATERIALS (LINESeedSans_A_XBd, and TT-RoGSanSrfStdN-Bd for the small "Lv"), never a Sprite --
   so these are live text and the sprite digits belong to some other screen.

   Level      117 x 87.75 at bottom-left (0,0), pivot 0,0
     LevelLabel  48 x 28 at (0, 56)     -- the small "Lv", ABOVE the number
     LevelValue 140 x 56 at (0,  0)     -- the number, overflowing its 117-wide parent
   CardPotentialUpgradeCountIcon  52 x 52 at (0, 95)
     FlowerIcon               stretched to fill   -- ui_999_img_flower / _blank
     PotentialUpgradeCount_On 50 x 48 centred     -- the count, over the flower

   The stack therefore runs, bottom up: value 0-56, "Lv" 56-84, flower 95-147, all flush left.
   Fractions are px/224 across and px/320 down, as everywhere else in this file.

   `.lvl` is the union of the two, 140 x 84, so the percentages BELOW resolve against it and not
   against the face: value = 56/84 = 66.667% of the box, "Lv" the 33.333% above it. Getting that
   wrong is what put the label off the bottom of the card on the first pass. */
.face .lvl { position:absolute; left:0; bottom:0; width:62.5%; height:26.25%;
  pointer-events:none; }
/* White with a heavy navy outline, measured off the same screenshot as the ribbon's. `paint-
   order` keeps the stroke behind the fill so the digits do not thin out.
   Sizes are measured, not taken from the slot heights: on a card drawn at its native 224x320 in
   `team selection inside song.png`, the value's digits are 25 px cap-height sitting 20-45 px
   above the card edge, and "Lv" is 17 px sitting 59-76 px up. So the text is CENTRED in each
   slot rather than sitting on its floor, and the value's em box is ~34 px (15.2cqw), well under
   the 56 px slot -- bottom-aligning it instead pushed the digits into the card's edge. */
.face .lvl b, .face .lvl span {
  position:absolute; left:3%; right:0; display:flex; align-items:center;
  color:#fff; line-height:1;
  font-family:"LINE Seed Sans","LINE Seed JP","LINESeedSans",
    "Century Gothic","Futura","Trebuchet MS",system-ui,sans-serif;
  font-weight:800; paint-order:stroke fill;
  -webkit-text-stroke:.16em rgba(60,58,96,.95); }
.face .lvl span { bottom:66.667%; height:33.333%; font-size:10.5cqw;
  letter-spacing:.02em; }                                        /* "Lv"   48x28 at (0,56) */
.face .lvl b    { bottom:0;       height:66.667%; font-size:15.2cqw; }  /* value 140x56 at 0 */
/* 52x52 at (0,95): the flower is the bloom badge. `ui_999_img_flower` is the filled state and
   `ui_999_img_flower_blank` the empty one -- the prefab holds both, and the node pair
   PotentialUpgradeCount_On/_Off is the count drawn over whichever applies. */
.face .bloom { position:absolute; left:0; bottom:29.688%; width:23.214%; aspect-ratio:1;
  pointer-events:none; }
.face .bloom img { width:100%; height:100%; display:block;
  filter:drop-shadow(0 1px 2px rgba(0,0,0,.4)); }
/* The count sits ON the petals, so it is dark rather than outlined white -- sampled #3E3F6B-ish
   in game. Off (bloom 0) keeps the same ink on the blank flower. */
.face .bloom em { position:absolute; inset:0; display:flex; align-items:center;
  justify-content:center; font-style:normal; color:#3E3F6B; font-weight:800;
  font-size:11cqw; line-height:1;
  font-family:"LINE Seed Sans","LINE Seed JP","LINESeedSans",
    "Century Gothic","Futura","Trebuchet MS",system-ui,sans-serif; }
.face .bloom.off em { color:#6B6E93; }
/* CardRarityStars: group anchored bottom-right at (+8, +16). Read the SCALE, not just the
   sizes -- each `star` node carries localScale 0.6, so the 75px Star draws at 45 and the 80px
   layout cell at 48. That 48 is exactly the group's own 48x48, which is the confirmation the
   numbers are being read right. Consequence: the stars sit 3px APART, they do not overlap the
   way they appear to. Column = 45 + 4*48 = 237 of the 320, clearing the strip.
   The 45px star centred in a 48px cell puts its right edge 6.5px past the card, not 8. */
.face .stars { position:absolute; right:-2.902%; bottom:5%; width:20.089%;
  display:flex; flex-direction:column; }
/* 3px of gap on a 45px star; a percentage margin resolves against the container's width,
   which is exactly those 45px. */
/* Glint: a highlight swept across each star. The star's own alpha is the mask, so the sweep
   never spills into the sprite's square, and `overflow:hidden` keeps it inside the tile.
   Staggered bottom-up (`--i` counts from the last star) so it runs up the column, then rests
   for most of the cycle.
   It animates TRANSFORM, not background-position: a full grid is ~670 stars, and a
   paint-driven property at that count is the difference between smooth and janky. */
.face .stars span { position:relative; display:block; width:100%; aspect-ratio:148/141;
  overflow:hidden; filter:drop-shadow(0 1px 2px rgba(0,0,0,.55));
  -webkit-mask-image:var(--star); mask-image:var(--star);
  -webkit-mask-size:100% 100%; mask-size:100% 100%; }
.face .stars span + span { margin-top:6.667%; }
.face .stars img { width:100%; height:100%; display:block; }
.face .stars i { position:absolute; top:-25%; bottom:-25%; left:-60%; width:55%;
  pointer-events:none;
  background:linear-gradient(100deg, transparent, rgba(255,255,255,.95), transparent);
  animation:glint 4.2s ease-in-out infinite; animation-delay:calc(var(--i,0) * .11s); }
@keyframes glint {
  0%, 62% { transform:translateX(0); }
  86%, 100% { transform:translateX(360%); }
}
@media (prefers-reduced-motion: reduce) { .face .stars i { animation:none; opacity:0; } }
/* The game shows Lv and bloom low-left here; we have neither in the dataset (see cardFace). */
.face .own { top:auto; left:6px; bottom:14px; }
.cap { padding:7px 9px 8px; }
.cap .name { color:var(--fg); font-size:12.5px; text-shadow:none; }
.cap .who { color:var(--dim); font-size:10.5px; text-shadow:none; }

/* ---- art / placeholder ----------------------------------------------- */
.art { position:relative; aspect-ratio:2/1; background-size:cover; background-position:center top;
  background-color:var(--sunk); display:flex; align-items:center; justify-content:center; }
/* The art slot stays dark in BOTH themes -- it is a media surface, and it is the only thing that
   keeps the white placeholder name legible once the page is in light mode. The last layer is the
   opaque floor; the tint and the hatch ride on top of it. */
.art:not(.has) {
  background-image:
    repeating-linear-gradient(115deg, rgba(255,255,255,.045) 0 2px, transparent 2px 11px),
    linear-gradient(140deg, rgba(var(--tc),.55), rgba(var(--tc),.16) 58%, rgba(8,10,16,.55)),
    linear-gradient(#11141d,#11141d); }
.ph { display:none; padding:0 14px 34px; font-size:clamp(15px,3.4vw,21px); font-weight:700;
  letter-spacing:-.015em; text-align:center; text-wrap:balance;
  color:rgba(255,255,255,.60); text-shadow:0 1px 4px rgba(0,0,0,.45); }
.art:not(.has) .ph { display:block; }
.scrim { position:absolute; inset:auto 0 0 0; padding:22px 12px 10px; pointer-events:none;
  background:linear-gradient(transparent, rgba(6,8,14,.62) 42%, rgba(6,8,14,.90)); }
.name { display:block; color:#fff; font-weight:640; font-size:14.5px; line-height:1.25;
  text-shadow:0 1px 3px rgba(0,0,0,.75); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.who { display:block; color:rgba(255,255,255,.72); font-size:11.5px; margin-top:2px;
  text-shadow:0 1px 3px rgba(0,0,0,.8); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.badges { position:absolute; top:8px; right:8px; display:flex; gap:5px; }
.badges .b { background:rgba(6,8,14,.62); backdrop-filter:blur(4px); border-radius:999px;
  padding:2px 8px; font-size:11px; font-weight:600; color:#fff; letter-spacing:.02em;
  box-shadow:inset 0 0 0 1px rgba(255,255,255,.14); }
.b.star { color:#ffd76a; }
.b.type { color:rgb(var(--tc)); box-shadow:inset 0 0 0 1px rgba(var(--tc),.55); }
.own { position:absolute; top:8px; left:8px; width:9px; height:9px; border-radius:50%;
  background:rgb(var(--owned)); box-shadow:0 0 0 2px rgba(6,8,14,.55), 0 0 9px rgb(var(--owned)); }
.own.inl { position:static; display:inline-block; margin-right:6px; box-shadow:none; }

/* ---- stat strip ------------------------------------------------------ */
.stats { display:grid; grid-template-columns:1fr 1fr 1fr 1.2fr; gap:1px; background:var(--line); }
.stat { background:var(--panel); padding:7px 4px 6px; text-align:center; }
.stat b { display:block; font-size:14px; font-weight:640; font-variant-numeric:tabular-nums; }
.stat span { color:var(--dim); font-size:9.5px; text-transform:uppercase; letter-spacing:.07em; }
.stat.tot { background:var(--sunk); } .stat.tot b { color:rgb(var(--tc)); }
.grow { display:flex; align-items:center; gap:7px; padding:5px 10px 6px; background:var(--sunk);
  border-top:1px solid var(--line); font-size:11px; color:var(--dim); }
.bar { flex:1; height:3px; border-radius:2px; background:var(--line); overflow:hidden; }
.bar i { display:block; height:100%; background:rgb(var(--owned)); border-radius:2px; }
.empty { color:var(--dim); padding:56px 24px; grid-column:1/-1; text-align:center; }

/* ---- drawer ---------------------------------------------------------- */
.back { position:fixed; inset:0; background:rgba(3,5,10,.55); opacity:0; pointer-events:none;
  transition:opacity .2s; z-index:8; }
body.open .back { opacity:1; pointer-events:auto; }
aside { position:fixed; top:0; right:0; z-index:9; width:min(420px,100%); height:100dvh;
  background:var(--panel); border-left:1px solid var(--line); overflow-y:auto;
  transform:translateX(101%); transition:transform .2s ease; will-change:transform;
  box-shadow:-18px 0 46px -20px rgba(0,0,0,.9); }
body.open aside { transform:none; }
aside .art { aspect-ratio:2/1; border-bottom:1px solid var(--line); }
.close { position:absolute; top:10px; right:10px; z-index:2; border:0; cursor:pointer;
  width:30px; height:30px; border-radius:50%; background:rgba(6,8,14,.62); color:#fff;
  font-size:17px; line-height:1; backdrop-filter:blur(4px);
  box-shadow:inset 0 0 0 1px rgba(255,255,255,.16); }
.body { padding:14px 18px 40px; }
.body h2 { margin:0 0 2px; font-size:18px; font-weight:660; letter-spacing:-.01em; }
.meta { color:var(--dim); font-size:12.5px; margin-bottom:12px; }
.body .stats { border:1px solid var(--line); border-radius:9px; overflow:hidden; margin-bottom:10px; }
.cur { background:var(--sunk); border:1px solid var(--line); border-radius:9px; padding:8px 11px;
  font-size:12.5px; color:var(--dim); margin-bottom:14px; }
.cur b { color:var(--fg); font-weight:620; font-variant-numeric:tabular-nums; }
.skill { border-top:1px solid var(--line); padding:10px 0 2px; }
.skill em { font-style:normal; font-size:10.5px; text-transform:uppercase; letter-spacing:.08em;
  font-weight:700; color:rgb(var(--tc)); display:block; margin-bottom:3px; }
.skill.cos em { color:var(--dim); }
.skill p { margin:0; font-size:13.5px; }
.none { color:var(--dim); font-style:italic; }
.idline { margin-top:20px; color:var(--dim); font-size:11px; word-break:break-all;
  font-family:ui-monospace,"Cascadia Mono",Consolas,monospace; }

/* Wide: no backdrop, so the grid stays live and you can click straight from card to card.
   Narrow: the drawer covers most of the screen, so it gets a backdrop and closes on tap. */
@media (min-width:1100px) { .back { display:none; } }
@media (max-width:560px) { header,.controls,main { padding-left:14px; padding-right:14px; } }

/* ---- skill tree overlay ---------------------------------------------- */
.treebtn { background:var(--panel); color:var(--fg); border:1px solid var(--line);
  border-radius:8px; padding:7px 11px; font:inherit; font-size:13px; cursor:pointer; }
.treebtn:hover { border-color:rgba(var(--pure),.7); }
.treewrap { position:fixed; inset:0; z-index:20; background:var(--bg); display:none;
  grid-template-rows:auto 1fr; grid-template-columns:1fr 300px; grid-template-areas:"bar bar" "main side"; }
body.tree .treewrap { display:grid; }
.treebar { grid-area:bar; display:flex; gap:12px; align-items:center; padding:12px 18px;
  border-bottom:1px solid var(--line); }
.treebar select { background:var(--panel); color:var(--fg); border:1px solid var(--line);
  border-radius:8px; padding:6px 10px; font:inherit; font-size:13px; }
.treebar .close { position:static; margin-left:auto; }
.treehint { color:var(--dim); font-size:12px; }
/* The board is bigger than the window at any useful zoom, so this scrolls and the SVG is
   sized in px by the zoom factor. `max-width:100%` would fight the zoom -- it silently caps
   the board at window width and nothing appears to happen past 100 %. */
.treemain { grid-area:main; overflow:auto; padding:16px; position:relative;
  overscroll-behavior:contain; touch-action:none; }
.treemain.grabbing { cursor:grabbing; user-select:none; }
.treemain svg { display:block; margin:auto; }
.zoomer { display:flex; align-items:center; gap:4px; }
.zbtn { width:30px; padding:6px 0; text-align:center; font-size:15px; line-height:1;
  font-variant-numeric:tabular-nums; }
.zlvl { min-width:58px; padding:6px 4px; text-align:center; font-size:12px;
  color:var(--dim); font-variant-numeric:tabular-nums; }
.treeside { grid-area:side; border-left:1px solid var(--line); overflow-y:auto; padding:12px 14px; }
.treeside h3 { margin:0 0 8px; font-size:13px; color:var(--dim); font-weight:600;
  text-transform:uppercase; letter-spacing:.05em; }
.tnode { cursor:pointer; }
/* Only the fallback shapes fade; a real node shows lock state by going pale grey, as in game. */
rect.tnode.locked { opacity:.28; }
/* A node is a stack -- disc, glyph, star badges, then a transparent circle on top that takes
   the clicks. Only that top circle is `.tnode`, so the rest must not swallow pointer events. */
.tdisc, .tglyph, .tstar { pointer-events:none; }
.tdisc { transition:fill .12s ease; }
.tglyph { opacity:.97; }
circle.tnode { transition:opacity .1s ease; }
circle.tnode:hover { fill:#fff !important; opacity:.18; }
/* The links are ours -- SkillTreeAtlas has no line art (connect_vector belongs to the Connect
   bonus UI, not the node links) -- so they stay thin and recessive under the game's node art. */
.tedge { stroke:var(--line); stroke-width:2.5; stroke-linecap:round; }
.tboost { fill:none; stroke:rgb(var(--cute)); stroke-width:2.5; pointer-events:none; }
.tslot { cursor:pointer; }
/* accent, not #fff: the ring has to read against both the light and the dark theme */
.cbnode { fill:none; stroke:rgb(var(--cute)); stroke-width:3; stroke-dasharray:4 3; }
.cbbeam { stroke:rgb(var(--cute)); stroke-width:1.5; opacity:.45; }
.slotlbl { font:700 9px ui-sans-serif,system-ui; fill:#12141c; text-anchor:middle;
  dominant-baseline:central; pointer-events:none; }
.cbtools { display:flex; gap:8px; align-items:center; margin-bottom:8px; }
.cbtools input[type=search] { flex:1; min-width:0; background:var(--sunk); color:var(--fg);
  border:1px solid var(--line); border-radius:7px; padding:5px 8px; font:inherit; font-size:12px; }
.cbtools .chk { font-size:11.5px; color:var(--dim); white-space:nowrap; }
#cblist { max-height:38vh; overflow-y:auto; margin:0 -2px 4px; padding:0 2px; }
.cbtag { font-style:normal; font-size:10px; border:1px solid var(--line); border-radius:5px;
  padding:1px 4px; color:var(--dim); white-space:nowrap; }
.cbtag.me { border-color:rgb(var(--cute)); color:rgb(var(--cute)); }
.cbinfo h4 span { font-weight:400; color:var(--dim); }
.shape { display:flex; gap:10px; align-items:center; }
.shape p { margin:0; font-size:11.5px; }
.slotrow { display:flex; flex-wrap:wrap; gap:5px; margin-top:8px; }
.slotbtn { background:var(--panel); color:var(--fg); border:1px solid var(--line);
  border-radius:7px; padding:4px 7px; font:inherit; font-size:11.5px; cursor:pointer; }
.slotbtn.on { border-color:rgb(var(--cute)); }
.slotbtn span { color:var(--dim); margin-left:3px; }
.cbcard { border:1px solid var(--line); border-radius:8px; padding:7px 9px; margin-bottom:7px;
  font-size:12.5px; cursor:pointer; display:flex; gap:9px; align-items:center; }
.cbcard > div { min-width:0; }
.sqchip { width:34px; height:34px; border-radius:7px; flex:0 0 auto; background-size:cover;
  background-position:center; background-color:var(--sunk); }
.cbcard.sel { border-color:rgb(var(--cute)); background:var(--panel); }
.cbcard b { font-weight:620; } .cbcard span { color:var(--dim); }
.legend { display:flex; flex-wrap:wrap; gap:4px 14px; font-size:11.5px; color:var(--dim); }
.legend i { font-style:normal; display:inline-flex; align-items:center; gap:5px; }
.legend u { text-decoration:none; width:10px; height:10px; border-radius:50%; display:inline-block; }
.cbinfo { border-top:1px solid var(--line); margin-top:12px; padding-top:10px; font-size:12.5px; }
.cbinfo h4 { margin:0 0 6px; font-size:12.5px; font-weight:620; }
.cbinfo p { margin:0 0 7px; color:var(--dim); }
.cbinfo .lv { display:flex; justify-content:space-between; gap:10px; margin-bottom:4px; }
.cbinfo .lv b { font-variant-numeric:tabular-nums; }
.cbinfo .max b { color:rgb(var(--cute)); }
.cbinfo svg { display:block; margin:8px 0; }
.treeside .sub { font-size:11.5px; color:var(--dim); margin:-4px 0 10px; }
@media (max-width:820px){ .treewrap{ grid-template-columns:1fr; grid-template-areas:"bar" "main" "side"; } }
</style>
__ART__
<header>
  <h1>hololive Dreams — card explorer</h1>
  <div class="sub" id="sub"></div>
</header>
<div class="controls">
  <input type="search" id="q" placeholder="Search title, member, skill text...">
  <select id="type"><option value="">All types</option></select>
  <select id="rar"><option value="">All rarities</option></select>
  <select id="gen"><option value="">All generations</option></select>
  <select id="sort">
    <option value="total">Sort: total stats</option>
    <option value="performance">Sort: performance</option>
    <option value="technique">Sort: technique</option>
    <option value="sense">Sort: sense</option>
    <option value="member">Sort: member</option>
  </select>
  <label class="chk"><input type="checkbox" id="own"> Only mine</label>
  <button id="treebtn" class="treebtn" title="Per-member skill tree">🌳 Skill tree</button>
  <span class="count" id="count"></span>
</div>
<main id="grid"></main>
<div class="back" id="back"></div>
<aside id="drawer" aria-hidden="true"><button class="close" id="close" title="Close (Esc)">×</button>
  <div id="dbody"></div></aside>
<div class="treewrap" id="treewrap" aria-hidden="true">
  <div class="treebar">
    <strong>Holomem Board</strong>
    <select id="treemem"></select>
    <span id="treecount" class="treehint"></span>
    <button id="treereset" class="treebtn" title="Discard local edits for this member">reset</button>
    <div class="zoomer">
      <button id="zoomout" class="treebtn zbtn" title="Zoom out (−)">−</button>
      <button id="zoomlvl" class="treebtn zlvl" title="Fit to window (0)">100%</button>
      <button id="zoomin" class="treebtn zbtn" title="Zoom in (+)">+</button>
    </div>
    <div class="legend" id="treelegend"></div>
    <button id="treeclose" class="close" title="Close (Esc)">×</button>
  </div>
  <div class="treemain" id="treemain"><svg id="treesvg" xmlns="http://www.w3.org/2000/svg"></svg></div>
  <div class="treeside" id="treeside"></div>
</div>
<script type="application/json" id="tree">__TREE__</script>
<script type="application/json" id="chrome">__CHROME__</script>
<script type="application/json" id="data">__DATA__</script>
<script>
const CARDS = JSON.parse(document.getElementById('data').textContent);
const BY_ID = new Map(CARDS.map(c => [c.id, c]));
const $ = id => document.getElementById(id);
const uniq = xs => [...new Set(xs)].sort();
const esc = s => String(s).replace(/[&<>"]/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[m]));
const n = v => v.toLocaleString();

for (const [sel, vals] of [
  ['type', uniq(CARDS.map(c => c.type))],
  ['rar',  uniq(CARDS.map(c => c.rarity))],
  ['gen',  uniq(CARDS.flatMap(c => c.gens))],
]) for (const v of vals) $(sel).add(new Option(v, v));

const ownedCount = CARDS.filter(c => c.owned).length;
const artCount = CARDS.filter(c => c.art).length;
$('sub').textContent = `${CARDS.length} cards · ${ownedCount} owned · `
  + `${uniq(CARDS.map(c => c.member)).length} members · art for ${artCount} · `
  + `stats shown are the fully-maxed ceiling`;

// The art lives in a generated <style> block as one .a-<id> rule per card, so re-rendering the
// grid on every keystroke moves class names around instead of megabytes of base64.
const artBox = c => `<div class="art a-${c.id}${c.art ? ' has' : ''}">`
  + `<span class="ph">${esc(c.member)}</span>`;

const stars = r => '★'.repeat(parseInt(r, 10) || 0);

function statStrip(s, tot) {
  return `<div class="stats">
    <div class="stat"><b>${n(s.performance)}</b><span>Perf</span></div>
    <div class="stat"><b>${n(s.technique)}</b><span>Tech</span></div>
    <div class="stat"><b>${n(s.sense)}</b><span>Sense</span></div>
    <div class="stat tot"><b>${n(tot)}</b><span>Total</span></div></div>`;
}

// The card face, laid out the way the game does it: portrait art, element badge top-left,
// generation tag top-right, rarity strip along the bottom with one star per rank. The sprites
// are the game's own (see tools/recon/CARD-UI-ASSETS.md), so this reads as the real thing
// rather than an approximation. Falls back to the square when a portrait is missing.
const CHROME = JSON.parse(document.getElementById('chrome').textContent);
const RANK = r => (r || '').charCodeAt(0) - 48;          // "5★" -> 5

// "Gen 5" -> g-gen5, "ID Gen 2" -> g-id, "Gen 1 / Gamers" -> the first unit's class.
function genClass(g) {
  const first = String(g).split(' / ')[0].trim();
  if (/^ID/i.test(first)) return 'g-id';
  return 'g-' + first.toLowerCase().replace(/[^a-z0-9]/g, '');
}

// The game stacks ONE ribbon per unit (a Gen 1 Gamers card really does show two), which is why
// `gens` is per-unit rather than the joined `gen` label. Falls back to a plain tinted pill when
// the ribbon caps are not in the payload -- i.e. when the sprite dump is missing.
// A zero-middle 9-slice as three mask layers: left cap at its own aspect pinned left, right cap
// pinned right, and the one-pixel seam column stretched between them. The middle must be
// POSITIONED, not centred -- the caps are asymmetric (~19.4% vs ~12.1%), so `center` leaves a
// notch on the right. A percentage mask-position puts an image of width W at p*(100-W)/100,
// hence p = left/(left+right).
function ribStyle(p) {
  const L = CHROME[p + 'L'], R = CHROME[p + 'R'], M = CHROME[p + 'M'];
  if (!L || !R || !M) return '';
  const imgs = `url(${L}),url(${R}),url(${M})`;
  const size = `auto 100%,auto 100%,${100 - CHROME.ribLpct - CHROME.ribRpct}% 100%`;
  const pos = `left center,right center,${CHROME.ribMpos}% center`;
  return `-webkit-mask-image:${imgs};mask-image:${imgs};`
    + `-webkit-mask-size:${size};mask-size:${size};`
    + `-webkit-mask-position:${pos};mask-position:${pos};`;
}
const RIB = ribStyle('rib'), RIBO = ribStyle('ribo');

function genRibbons(c) {
  const list = (c.gens && c.gens.length ? c.gens : (c.gen ? [c.gen] : []));
  if (!list.length) return '';
  return `<div class="gens">${list.map((g, i) => {
    // Colour comes from the game's CharacterGrouping master table. `genClass` stays as the
    // fallback for when gen_groups.json is missing, and only then does the CSS palette apply.
    // 100 is the gradient's left stop, 101 the right. When 101 is absent the CSS falls back to
    // --gc for both stops, i.e. the solid fill this used to draw.
    const col = (c.gcol || [])[i], col2 = (c.gcol2 || [])[i];
    return `<div class="gen ${col ? '' : genClass(g)}${RIB ? '' : ' plain'}"`
      + `${col ? ` style="--gc:${col}${col2 ? `;--gc2:${col2}` : ''}"` : ''}>`
      + `<i style="${RIB}"></i>${RIBO ? `<u style="${RIBO}"></u>` : ''}`
      + `<b>${esc(g)}</b></div>`;
  }).join('')}</div>`;
}

function cardFace(c) {
  const rank = RANK(c.rarity) || 3;
  const el = CHROME[(c.type || '').toLowerCase()];
  const bar = CHROME['r' + Math.min(Math.max(rank, 3), 5)];
  const star = CHROME.star;
  // Four sources, best first, and every card lands on one of them: the native 512-px portrait
  // written beside this page (c.vtsrc), the native 512-px SQUARE written beside it (c.sqsrc),
  // the embedded portrait of the degraded build (c.vt), the embedded square (c.sq). None ->
  // the typed placeholder gradient, `.face:not(.has)`.
  // Both file sources go through <img>, not a background, so they are lazy and per-tile. The
  // square is 1:1 in a 5:7 face; object-fit:cover + object-position:center top on `.port`
  // reproduces exactly the crop `background-size:cover; background-position:center top` gave it.
  // The PORTRAIT first: `VerticalCardThumbnail::SetCardTexture` builds `img_card_vert_{0}`, read
  // from the game's own code -- CARD-UI-ASSETS.md 5e. The square is the fallback for a card whose
  // portrait is missing; 167/167 portraits exist, so it should never fire.
  const port = c.vtsrc || c.sqsrc;
  const shape = port ? '' : (c.vt ? `vt-${c.id}` : (c.sq ? `sq-${c.id}` : ''));
  return `<div class="face r${rank} ${shape}${port || c.vt || c.sq ? ' has' : ''}">
    ${port ? `<img class="port" src="${esc(port)}" alt="" loading="lazy" decoding="async">` : ''}
    <span class="ring"></span>
    ${el ? `<img class="el" src="${el}" alt="${esc(c.type)}">` : ''}
    ${genRibbons(c)}
    ${bar ? `<span class="clip"><img class="rbar" src="${bar}" alt=""></span>` : ''}
    ${star ? `<div class="stars" style="--star:url(${star})">${Array.from({length: rank},
        (_, i) => `<span style="--i:${rank - 1 - i}"><img src="${star}" alt=""><i></i></span>`
      ).join('')}</div>` : ''}
    ${bloomBadge(c)}
    ${c.lv != null ? `<div class="lvl"><span>Lv</span><b>${c.lv}</b></div>` : ''}
    ${c.owned ? '<span class="own" title="owned"></span>' : ''}
  </div>`;
  // Lv and bloom come from card_state.json, hand-authored, because they are in no capture and
  // cannot be inverted out of the stats -- card_state.py explains it. A card with no state
  // draws neither badge rather than showing a made-up number.
}

// The flower IS the bloom badge: filled when the card has any potential upgrades, blank at 0.
// Both sprites ship in the prefab, which is what settles that `ui_999_ico_potential` -- the
// icon this project used to name here -- belongs to some other screen.
function bloomBadge(c) {
  if (c.bloom == null) return '';
  const on = c.bloom > 0, img = on ? CHROME.flower : CHROME.flowerBlank;
  if (!img) return '';
  return `<div class="bloom${on ? '' : ' off'}"><img src="${img}" alt="">`
    + `<em>${c.bloom}</em></div>`;
}

function tile(c) {
  const pct = c.current ? Math.round(c.current.total / c.stats.total * 100) : 0;
  return `<article class="tile ${c.type}" data-id="${c.id}" tabindex="0"
      aria-label="${esc(c.title || c.id)}">
    ${cardFace(c)}
    <div class="cap"><span class="name">${esc(c.title || c.id)}</span>
      <span class="who">${esc(c.member)}</span></div>
    ${statStrip(c.stats, c.stats.total)}
    ${c.current ? `<div class="grow"><span>yours ${n(c.current.total)}</span>
      <span class="bar"><i style="width:${pct}%"></i></span><span>${pct}%</span></div>` : ''}
  </article>`;
}

function skillBlock(label, text, cls) {
  const body = text ? esc(text) : '<span class="none">none</span>';
  return `<div class="skill ${cls || ''}"><em>${label}</em><p>${body}</p></div>`;
}

let selected = null;

function openCard(id) {
  const c = BY_ID.get(id);
  if (!c) return closeCard();
  selected = id;
  $('dbody').innerHTML = `
    ${artBox(c)}<div class="badges"><span class="b type">${c.type}</span>
      <span class="b star">${stars(c.rarity)}</span></div></div>
    <div class="body ${c.type}">
      <h2>${esc(c.title || c.id)}</h2>
      <div class="meta">${c.owned ? '<span class="own inl"></span>' : ''}${esc(c.member)}
        · ${esc(c.gen)}
        ${c.costume_name ? ' · outfit ' + esc(c.costume_name) : ''}</div>
      ${statStrip(c.stats, c.stats.total)}
      ${c.current ? `<div class="cur">yours now <b>${n(c.current.total)}</b> of
        ${n(c.stats.total)} — ${n(c.current.performance)} perf / ${n(c.current.technique)} tech
        / ${n(c.current.sense)} sense</div>` : ''}
      ${skillBlock('Special', c.desc.SPECIAL)}
      ${skillBlock('Active', c.desc.ACTIVE)}
      ${skillBlock('Passive', c.desc.PASSIVE)}
      ${skillBlock('Outfit', c.desc.COSTUME, 'cos')}
      <div class="idline">${esc(c.id)}${c.art ? '' : ' · no art dumped yet'}</div>
    </div>`;
  // Type colour is a tile-level custom property; the drawer needs its own copy.
  $('drawer').className = c.type;
  document.body.classList.add('open');
  $('drawer').setAttribute('aria-hidden', 'false');
  markSelected();
}

function closeCard() {
  selected = null;
  document.body.classList.remove('open');
  $('drawer').setAttribute('aria-hidden', 'true');
  markSelected();
}

function markSelected() {
  for (const el of $('grid').children)
    el.classList.toggle('sel', el.dataset.id === selected);
}

function render() {
  const q = $('q').value.trim().toLowerCase();
  const [t, r, g, own, sort] = [$('type').value, $('rar').value, $('gen').value,
                                $('own').checked, $('sort').value];
  let rows = CARDS.filter(c =>
    (!t || c.type === t) && (!r || c.rarity === r) &&
    (!g || c.gens.includes(g)) && (!own || c.owned) &&
    (!q || c.haystack.includes(q)));
  rows.sort(sort === 'member'
    ? (a, b) => a.member.localeCompare(b.member) || b.stats.total - a.stats.total
    : (a, b) => (b.stats[sort] ?? 0) - (a.stats[sort] ?? 0));
  $('count').textContent = `${rows.length} shown`;
  $('grid').innerHTML = rows.length ? rows.map(tile).join('')
    : '<div class="empty">Nothing matches those filters.</div>';
  // A card filtered out from under an open drawer keeps its detail up; only the highlight goes.
  markSelected();
}

$('grid').addEventListener('click', e => {
  const el = e.target.closest('.tile');
  if (el) el.dataset.id === selected ? closeCard() : openCard(el.dataset.id);
});
$('grid').addEventListener('keydown', e => {
  const el = e.target.closest('.tile');
  if (el && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); openCard(el.dataset.id); }
});
$('close').addEventListener('click', closeCard);
$('back').addEventListener('click', closeCard);
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeCard(); });
for (const id of ['q','type','rar','gen','sort','own'])
  $(id).addEventListener(id === 'q' ? 'input' : 'change', render);
render();

// ---- Holomem Board ---------------------------------------------------------
// Every member is drawn with her OWN layout: Character.f24 picks one of four tree-models, which
// mirror the blue/yellow arms and the red sub-branches. Positions arrive already rotated into
// screen space by build_skilltree.py, so red is up and green is down, as in the game.
const TREE = JSON.parse(document.getElementById('tree').textContent);
const MEMBERS = new Map((TREE.members || []).map(m => [m.chr, m]));
const key = (x, y) => x + ',' + y;
const SLOTS = ['S-001', 'S-002', 'S-003', 'S-004'];
let treeChr = null, treeCard = null, treeSlot = 'S-001', treeGeom = null;

// The captured board state is the truth; a member gets a localStorage entry only once edited.
const unlockKey = c => 'hbtree:' + c;
function getUnlocked(c) {
  const raw = localStorage.getItem(unlockKey(c));
  if (raw) { try { return new Set(JSON.parse(raw)); } catch (e) { /* fall through */ } }
  return new Set((TREE.unlocked || {})[c] || []);
}
const setUnlocked = (c, s) => localStorage.setItem(unlockKey(c), JSON.stringify([...s]));
const isEdited = c => localStorage.getItem(unlockKey(c)) !== null;

// effect descriptions are templates: [value], [value/10], [character], and a [highlight] wrapper
function fmtDesc(d, v, who) {
  return String(d || '').replace(/\[\/?highlight\]/g, '')
    .replace(/\[value\/10\]/g, ((v || 0) / 10).toLocaleString())
    .replace(/\[value\]/g, (v || 0).toLocaleString())
    .replace(/\[character\]/g, who || 'this member');
}
function nodeEffect(g, chr) {          // some groups carry one effect per character
  const nd = TREE.nodes[g];
  return nd ? ((nd.chr && nd.chr[chr]) || nd.e || null) : null;
}
const connectOf = c => TREE.connect[TREE.card_connect[c.id]] || null;

// The square art is emitted as CSS rules at build time, never as JSON, so the board borrows it
// back through a throwaway element rather than duplicating megabytes into the data island.
// The rule now normally holds a RELATIVE url to a sidecar file rather than a data: URI, so the
// pattern must accept both -- getComputedStyle resolves a relative url to an absolute one, which
// an SVG <image href> takes happily. Matching only `data:` here would have silently blanked every
// Connect node the moment the squares moved out of base64.
const _sqCache = new Map();
function squareURI(id) {
  if (_sqCache.has(id)) return _sqCache.get(id);
  const probe = document.createElement('div');
  probe.className = 'sq-' + id;
  probe.style.cssText = 'position:absolute;left:-9999px;width:1px;height:1px';
  document.body.appendChild(probe);
  const m = /url\("?([^")]+)"?\)/.exec(getComputedStyle(probe).backgroundImage || '');
  probe.remove();
  const uri = m ? m[1] : null;
  _sqCache.set(id, uri);
  return uri;
}

// SkillTreeAtlas pieces, keyed `<branch><lv>` for an unlocked node and `<branch><lv>off` for the
// game's own greyed-out art. Empty when the sprite dump is missing, and every call site falls
// back to the flat circles the tree drew before.
const TCH = CHROME.tree || {};
const TGLYPH = TCH.glyph || {}, TALIAS = TCH.alias || {};
// The board draws a different white glyph per effect, picked by the SkillTreeEffect enum that
// build_skilltree.py emits as `ic`. Falling back to one shared icon is what made every node
// look identical, so an unmapped effect gets the generic sparkle rather than the wrong letter.
// A node that GRANTS a skill draws that skill's icon, so `sk` outranks the effect enum --
// two nodes share enum 16 and show different glyphs (see TREE_SKILL_GLYPH).
// The node's own asset first; then the granted skill's, which is what an "addition" node shows
// (the game ships no tree icon for those, so nothing to fall back FROM); then an alias for the
// handful never downloaded; then a neutral sparkle.
function glyphURI(e) {
  if (!e) return TGLYPH['0'] || null;
  // A "level up" node is a hollow frame around the granted skill's icon, so the pair is one
  // glyph. Ask for it first, or the tile renders as an empty box with "Lv" under it.
  if (e.icon && e.icon_sk) {
    const pair = TGLYPH[e.icon + '|' + e.icon_sk];
    if (pair) return pair;
  }
  for (const key of [e.icon, e.icon_sk]) {
    if (!key) continue;
    const hit = TGLYPH[key] || TGLYPH[TALIAS[key]];
    if (hit) return hit;
  }
  return TGLYPH['0'] || null;
}

// One star per node level, sitting on the bottom edge of the disc as the game draws them.
function starBadges(cx, cy, rad, lv) {
  if (!TCH.star) return '';
  const s = rad * 0.62, n = lv === 2 ? 2 : 1, gap = s * 0.86;
  let out = '';
  for (let i = 0; i < n; i++) {
    const x = cx + (i - (n - 1) / 2) * gap;
    out += `<image class="tstar" href="${TCH.star}" x="${x - s / 2}" y="${cy - s / 2}"
        width="${s}" height="${s}"/>`;
  }
  return out;
}

/* ---- board zoom ------------------------------------------------------------------------
   The SVG keeps a fixed viewBox and is resized in px, so nothing has to be redrawn to zoom
   and the node hit targets scale with it for free. `fit` means "recompute on resize until the
   user picks a zoom themselves" -- opening the board on a small window otherwise shows a
   corner of the tree with no clue the rest exists. */
const ZMIN = 0.25, ZMAX = 4;
let treeBase = { w: 0, h: 0 }, treeZoom = 1, treeFit = true;

function fitZoom() {
  const box = $('treemain');
  if (!treeBase.w || !box.clientWidth) return 1;
  const pad = 32;
  return clampZoom(Math.min((box.clientWidth - pad) / treeBase.w,
                            (box.clientHeight - pad) / treeBase.h));
}
const clampZoom = z => Math.min(ZMAX, Math.max(ZMIN, z));

function applyZoom(z, anchor) {
  const box = $('treemain'), svg = $('treesvg');
  if (!treeBase.w) return;
  if (z === undefined) {                       // re-fit (first draw, or window resized)
    if (!treeFit) { z = treeZoom; } else { z = fitZoom(); }
  } else {
    treeFit = false;
  }
  z = clampZoom(z);
  // Keep the point under the cursor (or the view centre) still, or zooming walks the board
  // out from under you: work out where that point is in board space first.
  const a = anchor || { x: box.clientWidth / 2, y: box.clientHeight / 2 };
  const before = treeZoom || 1;
  const bx = (box.scrollLeft + a.x) / before, by = (box.scrollTop + a.y) / before;

  treeZoom = z;
  svg.setAttribute('width', treeBase.w * z);
  svg.setAttribute('height', treeBase.h * z);
  const lvl = $('zoomlvl');
  if (lvl) lvl.textContent = Math.round(z * 100) + '%';

  box.scrollLeft = bx * z - a.x;
  box.scrollTop = by * z - a.y;
}

function initZoomControls() {
  const box = $('treemain');
  $('zoomin').onclick = () => applyZoom(treeZoom * 1.25);
  $('zoomout').onclick = () => applyZoom(treeZoom / 1.25);
  $('zoomlvl').onclick = () => { treeFit = true; applyZoom(); };

  // Plain wheel zooms: this pane is a board, not a document, and there is nothing below it to
  // scroll to. passive:false so preventDefault actually stops the page from scrolling too.
  box.addEventListener('wheel', ev => {
    ev.preventDefault();
    const r = box.getBoundingClientRect();
    applyZoom(treeZoom * (ev.deltaY < 0 ? 1.12 : 1 / 1.12),
              { x: ev.clientX - r.left, y: ev.clientY - r.top });
  }, { passive: false });

  // Drag to pan. The board is also clickable, so panning only starts past a few px of
  // movement and a real drag swallows the click that follows it.
  let down = null, moved = false;
  box.addEventListener('pointerdown', ev => {
    if (ev.button !== 0 && ev.button !== 1) return;
    down = { x: ev.clientX, y: ev.clientY, sl: box.scrollLeft, st: box.scrollTop };
    moved = false;
  });
  box.addEventListener('pointermove', ev => {
    if (!down) return;
    const dx = ev.clientX - down.x, dy = ev.clientY - down.y;
    if (!moved && Math.abs(dx) + Math.abs(dy) < 5) return;
    moved = true;
    box.classList.add('grabbing');
    box.scrollLeft = down.sl - dx;
    box.scrollTop = down.st - dy;
  });
  const end = () => { down = null; box.classList.remove('grabbing'); };
  box.addEventListener('pointerup', end);
  box.addEventListener('pointercancel', end);
  box.addEventListener('pointerleave', end);
  box.addEventListener('click', ev => {
    if (moved) { ev.stopPropagation(); ev.preventDefault(); moved = false; }
  }, true);

  addEventListener('resize', () => { if (treeFit) applyZoom(); });
}

function drawTree(chr) {
  treeChr = chr;
  const mem = MEMBERS.get(chr);
  const layout = TREE.models[mem.model];
  const gs = Object.keys(layout);
  const xs = gs.map(g => layout[g][0]), ys = gs.map(g => layout[g][1]);
  const minx = Math.min(...xs), maxx = Math.max(...xs);
  const miny = Math.min(...ys), maxy = Math.max(...ys);
  // R is the fallback circle radius; NR is the sprite ring's diameter/2, a little larger so the
  // node icons nearly touch on the 30px grid the way they do in game. PAD grew with them.
  const S = 30, R = 11, NR = 13, PAD = 30;
  const W = (maxx - minx) * S + PAD * 2, H = (maxy - miny) * S + PAD * 2;
  const X = x => PAD + (x - minx) * S, Y = y => PAD + (y - miny) * S;
  const svg = $('treesvg');
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  treeBase = { w: W, h: H };            // the zoom scales these; the viewBox never changes
  applyZoom();

  const at = {};
  for (const g of gs) at[key(layout[g][0], layout[g][1])] = g;
  const unlocked = getUnlocked(chr);
  const board = getAssign()[chr] || {};

  // How a connect bonus actually applies, ATTESTED in game: the tile's own value is multiplied by
  // (1 + connect/1000). Calliope's 140% turned "+50 to All" into +120, "Sense +100" into +240 and
  // "Score Support 10%" into 24% -- x2.4 across all three, so the multiplier hits the NUMBER
  // regardless of whether its unit is flat points or per-mille. Note it is not additive in the
  // obvious way: 140% does not add 140, it adds 140% OF the value.
  // Level 1 is used because per-card Bloom state is not something any capture gives us.
  // Two cards can never boost the same tile, so no stacking rule is needed: the longest reach of
  // any shape is 3 tiles and the closest two Connect nodes are 7 apart, leaving 6 tiles between
  // them -- exactly enough for two maximum-reach shapes to meet in the middle and share nothing.
  // Checked exhaustively: 6936 (slot, shape) x (slot, shape) pairs across all four models, zero
  // collisions. The accumulate below is therefore only ever a single write.
  const boost = new Map();
  for (const [slot, id] of Object.entries(board)) {
    const cb = TREE.connect[TREE.card_connect[id]], org = layout[slot];
    if (!cb || !org) continue;
    for (const [dx, dy] of TREE.extents[cb.area] || []) {
      const k = key(org[0] + dx, org[1] + dy);
      boost.set(k, (boost.get(k) || 0) + cb.levels[0].v);
    }
  }

  let edges = '', dots = '', defs = '';
  for (const g of gs) {                    // link grid-adjacent tiles of the same arm
    const [x, y] = layout[g];
    for (const [dx, dy] of [[1, 0], [0, 1]]) {
      const m = at[key(x + dx, y + dy)];
      if (m && (m[0] === g[0] || m[0] === 'S' || g[0] === 'S'))
        // Class, not an inline stroke: the old hardcoded #2a3040 was picked for the dark theme
        // and read as heavy scaffolding in the light one, competing with the node art.
        edges += `<line class="tedge" x1="${X(x)}" y1="${Y(y)}"`
               + ` x2="${X(x + dx)}" y2="${Y(y + dy)}"/>`;
    }
  }
  for (const g of gs) {
    const [x, y] = layout[g], br = TREE.branches[g[0]] || {}, on = unlocked.has(g);
    const e = nodeEffect(g, chr);
    if (g[0] === 'S') {                    // Connect node: the only tile a card can sit on
      const held = board[g] ? BY_ID.get(board[g]) : null;
      const tip = `${g} · Connect node — ` + (held
        ? `${held.title || held.id} (${held.member}) is placed here`
        : 'click to place the selected card');
      const uri = held ? squareURI(held.id) : null;
      if (uri) defs += `<pattern id="sq-${g}" patternContentUnits="objectBoundingBox"
          width="1" height="1"><image href="${uri}" width="1" height="1"
          preserveAspectRatio="xMidYMid slice"/></pattern>`;
      // Empty slot -> the game's own socket art (dashed square with a +, or a padlock when the
      // slot is still locked). A held card keeps its thumbnail, which the socket has no art for.
      const sock = !held && (on ? TCH.socket : (TCH.socketlock || TCH.socket));
      const SD = 2 * R + 8;
      if (sock) {
        dots += `<image class="tnode tslot" data-g="${g}" href="${sock}"
            x="${X(x) - SD / 2}" y="${Y(y) - SD / 2}" width="${SD}" height="${SD}"
            ><title>${esc(tip)}</title></image>`;
        continue;
      }
      dots += `<rect class="tnode tslot ${on ? '' : 'locked'}" data-g="${g}"
          x="${X(x) - R - 3}" y="${Y(y) - R - 3}" width="${2 * R + 6}" height="${2 * R + 6}"
          rx="5" fill="${uri ? `url(#sq-${g})` : held ? 'rgb(var(--cute))' : br.hue}"
          stroke="#12141c" stroke-width="2"><title>${esc(tip)}</title></rect>`
        + (held && !uri
           ? `<text class="slotlbl" x="${X(x)}" y="${Y(y)}">${esc(initials(held))}</text>` : '');
      continue;
    }
    const permil = boost.get(key(x, y)) || 0;
    const mult = 1 + permil / 1000;
    const show = v => fmtDesc(e.d, v, mem.name) || `${e.t} +${v}`;
    let tip = `${g} · ${br.name} (${g[0]})`;
    if (e) {
      tip += '\n' + show(e.v);
      if (permil) tip += `\n→ boosted ×${mult.toFixed(2)}: ${show(Math.round(e.v * mult))}`;
    }
    // A node as the game builds it: a disc tinted by branch, a white glyph for THIS effect,
    // and one star per level hanging off the bottom edge. Locked tiles keep the shape but drop
    // to the board's pale grey, which is what the game shows.
    const lv = (TREE.nodes[g] && TREE.nodes[g].lv) === 2 ? 2 : 1;
    const gl = glyphURI(e);
    if (gl) {
      const rad = NR * (lv === 2 ? 1.18 : 1);      // lv2 tiles are visibly larger in game
      const GD = rad * 1.15;                       // glyph fills most of the disc
      // A locked tile goes pale but KEEPS its branch-coloured ring -- that is how the game
      // still tells the four arms apart when almost nothing is unlocked. Filling and stroking
      // it all grey loses that, and the board reads as one undifferentiated mass.
      const fill = on ? (br.hue || '#888') : '#c3ced9';
      dots += `<circle class="tdisc" cx="${X(x)}" cy="${Y(y)}" r="${rad}" fill="${fill}"
            stroke="${br.hue || '#888'}" stroke-width="${on ? 1.5 : 2}"/>`
        + (permil ? `<circle class="tboost" cx="${X(x)}" cy="${Y(y)}" r="${rad + 2.5}"/>` : '')
        + `<image class="tglyph" href="${gl}" x="${X(x) - GD / 2}" y="${Y(y) - GD / 2}"
            width="${GD}" height="${GD}"/>`
        + starBadges(X(x), Y(y) + rad * 0.72, rad, lv)
        + `<circle class="tnode ${on ? '' : 'locked'}" data-g="${g}" cx="${X(x)}" cy="${Y(y)}"
            r="${rad}" fill="transparent"><title>${esc(tip)}</title></circle>`;
      continue;
    }
    dots += `<circle class="tnode ${on ? '' : 'locked'}" data-g="${g}" cx="${X(x)}" cy="${Y(y)}"
        r="${R}" fill="${br.hue || '#888'}"
        stroke="${permil ? 'rgb(var(--cute))' : '#12141c'}" stroke-width="${permil ? 3 : 2}">
        <title>${esc(tip)}</title></circle>`;
  }
  svg.innerHTML = `<defs>${defs}</defs>` + edges + '<g id="cbhi"></g>' + dots;
  svg.onclick = ev => {
    const el = ev.target.closest('.tnode'); if (!el) return;
    const g = el.dataset.g;
    if (g[0] === 'S') {                    // a Connect node holds a card, it is not a buff tile
      const here = (getAssign()[treeChr] || {})[g];
      if (treeCard) { placeCard(treeCard, treeChr, g); treeSlot = g; drawTree(treeChr); return; }
      if (here) { treeCard = here; treeSlot = g; buildSide(); return; }
    }
    const u = getUnlocked(treeChr);
    u.has(g) ? u.delete(g) : u.add(g);
    setUnlocked(treeChr, u);
    const nowOn = u.has(g);
    el.classList.toggle('locked', !nowOn);
    // The hit target is a transparent circle sitting over the node, so the thing that actually
    // changes colour is the disc behind it -- repaint that, or the click does nothing visible.
    const disc = el.parentNode.querySelector(
      `circle.tdisc[cx="${el.getAttribute('cx')}"][cy="${el.getAttribute('cy')}"]`);
    if (disc) {
      disc.setAttribute('fill',
        nowOn ? ((TREE.branches[g[0]] || {}).hue || '#888') : '#c3ced9');
      disc.setAttribute('stroke-width', nowOn ? 1.5 : 2);
    }
    updateTreeCount(); if (treeCard) showConnect();
  };
  treeGeom = { layout, at, X, Y, R, total: gs.length };
  buildSide();
  updateTreeCount();
}

function updateTreeCount() {
  const u = getUnlocked(treeChr), n = treeGeom ? treeGeom.total : 0;
  const src = isEdited(treeChr) ? 'edited here'
    : ((TREE.unlocked || {})[treeChr] ? 'from capture' : 'no capture for her');
  $('treecount').textContent = `${u.size} / ${n} tiles · ${src}`;
}

// --- connect bonus ----------------------------------------------------------
// A card sits ON a Connect node and boosts the tiles in its extent, which is anchored at that
// node and generally does NOT surround it (Calliope 5★ boosts a diamond two tiles further up).
// ANY member's card can go on ANY board -- the extent is a property of the card, not of the
// member -- but a card is in one place at a time, so placing it here lifts it from wherever it
// was. That exclusivity is why placements live in one global map rather than per board.
// Same rule as the tiles: the capture is the truth until you edit, and the first edit
// materialises the captured map so exclusivity keeps working across every board.
const ASSIGN_KEY = 'hbslots';
const captured = () => JSON.parse(JSON.stringify(TREE.placed || {}));
function getAssign() {
  const raw = localStorage.getItem(ASSIGN_KEY);
  if (raw) { try { return JSON.parse(raw); } catch (e) { /* fall through */ } }
  return captured();
}
const setAssign = a => localStorage.setItem(ASSIGN_KEY, JSON.stringify(a));
function whereIs(id) {
  const a = getAssign();
  for (const c of Object.keys(a)) for (const s of Object.keys(a[c]))
    if (a[c][s] === id) return { chr: c, slot: s };
  return null;
}
function placeCard(id, chr, slot) {
  const a = getAssign();
  for (const c of Object.keys(a)) for (const s of Object.keys(a[c]))
    if (a[c][s] === id) delete a[c][s];          // one board at a time
  (a[chr] = a[chr] || {})[slot] = id;
  setAssign(a);
}
function liftCard(id) {
  const a = getAssign(), at = whereIs(id);
  if (at) { delete a[at.chr][at.slot]; setAssign(a); }
}
const initials = c => (c.member || '?').split(/\s+/).map(w => w[0]).join('').slice(0, 2).toUpperCase();

function coverage(cells, slot) {
  const { layout, at } = treeGeom;
  const org = layout[slot]; if (!org) return { hit: [], on: 0, tiles: 0 };
  const u = getUnlocked(treeChr), hit = [];
  for (const [dx, dy] of cells) {
    const p = [org[0] + dx, org[1] + dy], g = at[key(p[0], p[1])];
    hit.push({ p, g: g || null, on: !!g && u.has(g) });
  }
  return { hit, on: hit.filter(h => h.on).length, tiles: hit.filter(h => h.g).length };
}

// Beams from the slot to each boosted tile: without them a shape that sits two cells away reads
// as if the card itself went there, which is not a placement the game allows.
function beams(slot, cells, opacity) {
  const { X, Y, R, layout } = treeGeom, org = layout[slot];
  if (!org) return '';
  const cov = coverage(cells, slot);
  return `<g opacity="${opacity}">` + cov.hit.map(h =>
    `<line class="cbbeam" x1="${X(org[0])}" y1="${Y(org[1])}" x2="${X(h.p[0])}" y2="${Y(h.p[1])}"/>`
    + `<circle class="cbnode" cx="${X(h.p[0])}" cy="${Y(h.p[1])}" r="${R + 4}"
        opacity="${h.g ? 1 : .3}"/>`).join('') + '</g>';
}

function renderBoost() {
  const hi = document.getElementById('cbhi');
  if (!hi || !treeGeom) return;
  const board = getAssign()[treeChr] || {};
  let out = '';
  for (const [slot, id] of Object.entries(board)) {
    const cb = TREE.connect[TREE.card_connect[id]];
    if (cb) out += beams(slot, TREE.extents[cb.area] || [], id === treeCard ? 1 : .45);
  }
  const held = treeCard && BY_ID.get(treeCard);
  if (held && !Object.values(board).includes(treeCard) && treeSlot)   // preview before placing
    out += beams(treeSlot, TREE.extents[connectOf(held).area] || [], .8);
  hi.innerHTML = out;
}

let cbBuilt = false;
function buildSide() {
  const side = $('treeside');
  if (!cbBuilt) {
    side.innerHTML = `<h3>Connect bonus</h3>
      <p class="sub">Any member's card fits any Connect node — the shape belongs to the card. A
      card can only be on one board at a time, so placing it here lifts it from where it was.</p>
      <div class="cbtools"><input id="cbq" type="search" placeholder="search card or member">
        <label class="chk"><input type="checkbox" id="cbown" checked> only mine</label></div>
      <div id="cblist"></div><div class="cbinfo" id="cbinfo"></div>`;
    $('cbq').addEventListener('input', renderList);
    $('cbown').addEventListener('change', renderList);
    side.addEventListener('click', ev => {
      const el = ev.target.closest('.cbcard');
      if (el) {
        treeCard = el.dataset.id === treeCard ? null : el.dataset.id;
        treeSlot = null;
        renderList(); showConnect(); return;
      }
      const act = ev.target.closest('[data-act]');
      if (!act || !treeCard) return;
      if (act.dataset.act === 'lift') liftCard(treeCard);
      else placeCard(treeCard, treeChr, act.dataset.act);
      treeSlot = act.dataset.act === 'lift' ? treeSlot : act.dataset.act;
      drawTree(treeChr);
    });
    cbBuilt = true;
  }
  renderList();
  showConnect();
}

function renderList() {
  const q = ($('cbq').value || '').trim().toLowerCase(), mineOnly = $('cbown').checked;
  const rows = CARDS.filter(c => TREE.card_connect[c.id])
    .filter(c => !mineOnly || c.owned)
    .filter(c => !q || (c.title || '').toLowerCase().includes(q)
                    || c.member.toLowerCase().includes(q));
  const here = getAssign()[treeChr] || {};
  const rank = c => (Object.values(here).includes(c.id) ? 0 : whereIs(c.id) ? 1 : 2);
  rows.sort((a, b) => rank(a) - rank(b) || a.member.localeCompare(b.member)
                   || (b.rarity || '').localeCompare(a.rarity || ''));
  $('cblist').innerHTML = rows.length ? rows.map(c => {
    const cb = connectOf(c), max = cb.levels[cb.levels.length - 1], at = whereIs(c.id);
    const tag = at ? `<em class="cbtag${at.chr === treeChr ? ' me' : ''}">on ${at.chr === treeChr
      ? 'this board' : esc((MEMBERS.get(at.chr) || {}).name || at.chr)} · ${at.slot}</em>` : '';
    return `<div class="cbcard${c.id === treeCard ? ' sel' : ''}" data-id="${c.id}">
      <i class="sqchip${c.sq ? ' sq-' + c.id : ''}"></i>
      <div><b>${esc(c.title || c.id)}</b> ${tag}<br><span>${esc(c.member)} ·
      ${cb.rare.replace('rare_', '')}★ · up to ${(max.v / 10).toLocaleString()}%</span></div></div>`;
  }).join('') : '<p class="sub">No card matches.</p>';
}

function showConnect() {
  const box = $('cbinfo');
  if (!box) return;
  if (!treeCard) { box.innerHTML = ''; renderBoost(); return; }
  const card = BY_ID.get(treeCard), cb = connectOf(card), cells = TREE.extents[cb.area] || [];
  const mem = MEMBERS.get(treeChr), at = whereIs(treeCard);
  const ranked = SLOTS.filter(s => treeGeom.layout[s]).map(s => ({ s, ...coverage(cells, s) }))
    .sort((a, b) => b.on - a.on || b.tiles - a.tiles);
  if (at && at.chr === treeChr) treeSlot = at.slot;
  else if (!ranked.some(r => r.s === treeSlot)) treeSlot = ranked[0].s;
  const cur = ranked.find(r => r.s === treeSlot);

  // the shape on its own grid, with the Connect node it hangs off marked at the centre
  const G = 15, span = 3;
  const shape = `<svg width="${(2 * span + 1) * G}" height="${(2 * span + 1) * G}">`
    + cells.map(([x, y]) => `<rect x="${(x + span) * G + 1}" y="${(y + span) * G + 1}"
        width="${G - 2}" height="${G - 2}" rx="2" fill="rgb(var(--cute))"/>`).join('')
    + `<rect x="${span * G + 1}" y="${span * G + 1}" width="${G - 2}" height="${G - 2}" rx="2"
        fill="none" stroke="currentColor" stroke-width="1.5"/></svg>`;

  // Two levels only: the upgrade lands at Bloom 5 (CardPotential effect_type 5, upgrade_count 5),
  // which is why it exists for 4★/5★ cards and no others.
  const names = ['Base (Bloom 0–4)', 'Bloom 5 (max)'];
  const lv = cb.levels.map((l, i) => {
    const last = i === cb.levels.length - 1;
    return `<div class="lv${last ? ' max' : ''}"><span>${names[i] || 'Level ' + (i + 1)}</span>`
      + `<b>${(l.v / 10).toLocaleString()}%</b></div>`;
  }).join('');

  const placedHere = at && at.chr === treeChr;
  const actions = SLOTS.filter(s => treeGeom.layout[s]).map(s => {
    const r = ranked.find(x => x.s === s);
    return `<button class="slotbtn${s === treeSlot ? ' on' : ''}" data-act="${s}">${s}
      <span>${r.on}/${r.tiles}</span></button>`;
  }).join('') + (placedHere ? '<button class="slotbtn" data-act="lift">lift off</button>' : '');

  box.innerHTML = `<h4>${esc(card.title || card.id)} <span class="sub">${esc(card.member)}</span></h4>
    <div class="shape">${shape}<p>Card sits on the outlined tile;
      the filled ones get the boost.</p></div>
    <p>${esc(fmtDesc(cb.levels[cb.levels.length - 1].d, 0, mem.name))}</p>
    ${lv}
    <p style="margin-top:8px">${placedHere ? `Placed on <b>${treeSlot}</b>` :
      `<b>${treeSlot}</b> would be best`}: boosts ${cur.on} unlocked of
      ${cur.tiles} tile${cur.tiles === 1 ? '' : 's'} in range.</p>
    <div class="slotrow">${actions}</div>`;
  renderBoost();
}

function openTree() {
  const sel = $('treemem');
  if (!sel.options.length) {
    const owned = new Set(CARDS.filter(c => c.owned).map(c => c.chr));
    for (const m of TREE.members)
      sel.add(new Option(m.name + (owned.has(m.chr) ? '' : ' ·'), m.chr));
    $('treelegend').innerHTML = ['R', 'B', 'G', 'Y', 'S'].map(b => {
      const x = TREE.branches[b];
      return `<i title="${esc(x.when)}"><u style="background:${x.hue}"></u>${esc(x.name)}</i>`;
    }).join('');
  }
  document.body.classList.add('tree');
  $('treewrap').setAttribute('aria-hidden', 'false');
  // The pane has no size until it is displayed, so bind the zoom (and take the first fit)
  // only once it is visible -- doing it at load gives fitZoom a clientWidth of 0.
  if (!zoomReady) { initZoomControls(); zoomReady = true; }
  treeFit = true;
  drawTree(sel.value || TREE.members[0].chr);
}
let zoomReady = false;
function closeTree() { document.body.classList.remove('tree'); $('treewrap').setAttribute('aria-hidden', 'true'); }
$('treebtn').addEventListener('click', openTree);
$('treeclose').addEventListener('click', closeTree);
$('treemem').addEventListener('change', e => drawTree(e.target.value));
$('treereset').addEventListener('click', () => {
  localStorage.removeItem(unlockKey(treeChr));            // tiles: back to the capture
  const a = getAssign(), mine = (TREE.placed || {})[treeChr] || {};
  for (const c of Object.keys(a)) for (const s of Object.keys(a[c]))
    if (Object.values(mine).includes(a[c][s])) delete a[c][s];   // free those cards elsewhere
  a[treeChr] = JSON.parse(JSON.stringify(mine));
  setAssign(a);
  drawTree(treeChr);
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') { closeTree(); return; }
  if (!document.body.classList.contains('tree')) return;
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;
  // `=` because + is shift-equals on most layouts, and NumpadAdd reports "+"
  if (e.key === '+' || e.key === '=') { applyZoom(treeZoom * 1.25); e.preventDefault(); }
  else if (e.key === '-' || e.key === '_') { applyZoom(treeZoom / 1.25); e.preventDefault(); }
  else if (e.key === '0') { treeFit = true; applyZoom(); e.preventDefault(); }
});
</script>
"""


def load_art(prefix=ART_PREFIX, max_px=None):
    """Map card id -> base64 data URI for every dumped image. Missing dir is normal, not an error.

    `max_px` shrinks oversized sources at embed time instead of re-rendering them, which leaves
    dumps/ untouched (another process may be regenerating those files). Silently
    skipped if Pillow is absent.

    A `max_px` must be derived from the drawn size x the device pixel ratio, never hardcoded next
    to a layout value that will be retuned without it; otherwise the portraits end up 3.27x
    under-sampled and the squares 2.45x, which is why NEITHER comes through this function on a
    healthy build -- both go to sidecar files (see export_art). What is left here is the
    2:1 banner `img_card_full_`, and the degraded path for the other two."""
    art = {}
    if not os.path.isdir(ART_DIR):
        return art
    shrink = None
    if max_px:
        try:
            from PIL import Image
            shrink = Image
        except ImportError:
            pass
    for name in sorted(os.listdir(ART_DIR)):
        if not (name.startswith(prefix) and name.endswith(ART_SUFFIX)):
            continue
        path = os.path.join(ART_DIR, name)
        raw = open(path, "rb").read()
        if shrink:
            img = shrink.open(io.BytesIO(raw))
            if max(img.size) > max_px:
                img.thumbnail((max_px, max_px), shrink.Resampling.LANCZOS)
                buf = io.BytesIO()
                img.save(buf, "WEBP", quality=82)
                raw = buf.getvalue()
        art["card-" + name[len(prefix):-len(ART_SUFFIX)]] = \
            "data:image/webp;base64," + base64.b64encode(raw).decode("ascii")
    return art


def _native_sources(src_dir, prefix, suffix, ids):
    """card id -> native file path, for every card in `ids` that has one. {} if the dir is absent."""
    sources = {}
    if not os.path.isdir(src_dir):
        return sources
    for name in sorted(os.listdir(src_dir)):
        if not (name.startswith(prefix) and name.endswith(suffix)):
            continue
        cid = "card-" + name[len(prefix):-len(suffix)]
        if cid in ids:
            sources[cid] = os.path.join(src_dir, name)
    return sources


def export_art(dst_html, ids):
    """Re-encode the native 512-px art into ONE sidecar directory beside the page.

    Two families, and the square is here for the same reason the portrait is: the page shrank a
    320 px source to 160 px and then drew it at up to 392 device px (see SQ_NATIVE_DIR for
    the per-consumer arithmetic). Portraits land as `<id>.webp`, squares as
    `<id>.sq.webp`.

    Returns `(portraits, squares, note)` where each map is card id -> a RELATIVE url, so the page
    keeps working opened straight off the filesystem. An empty map means that family's natives
    could not be used and the caller must fall back to the embedded path for it -- a degraded
    build, not an error, exactly like a missing art dir. The two degrade independently.

    The directory is purged of anything it should not contain before a single byte is written, and
    the purge is computed over BOTH families at once -- doing it per family would have the second
    export delete the first one's output. A stale file left over from a previous run is
    indistinguishable from a fresh one by inspection, so a generator's own count is not trusted
    over what is on disk."""
    try:
        from PIL import Image
    except ImportError:
        return {}, {}, "Pillow is not installed, so the native PNGs cannot be re-encoded"

    out_dir = os.path.splitext(dst_html)[0] + "_art"
    rel_root = os.path.basename(out_dir)
    families = [
        ("portrait", ".webp", PORTRAIT_QUALITY, VERT_NATIVE_DIR,
         _native_sources(VERT_NATIVE_DIR, VERT_PREFIX, VERT_NATIVE_SUFFIX, ids)),
        ("square", ".sq.webp", SQUARE_QUALITY, SQ_NATIVE_DIR,
         _native_sources(SQ_NATIVE_DIR, SQ_PREFIX, SQ_NATIVE_SUFFIX, ids)),
    ]
    if not any(sources for *_, sources in families):
        return {}, {}, ("no native art matching a known card under "
                        + " or ".join(d for _, _, _, d, _ in families))

    os.makedirs(out_dir, exist_ok=True)
    keep = {cid + ext for _, ext, _, _, sources in families for cid in sources}
    for stale in os.listdir(out_dir):
        p = os.path.join(out_dir, stale)
        if os.path.isfile(p) and stale not in keep:
            os.remove(p)

    out, notes = [], []
    for label, ext, quality, src_dir, sources in families:
        if not sources:
            out.append({})
            notes.append(f"no native {label}s under {src_dir}")
            continue
        urls, written = {}, 0
        for cid, src in sorted(sources.items()):
            dst = os.path.join(out_dir, cid + ext)
            # Re-encoding 334 files costs real seconds, and the natives change only when a new
            # capture lands, so an up-to-date file is left alone. mtime, not size: size collides.
            if not (os.path.exists(dst) and os.path.getmtime(dst) >= os.path.getmtime(src)):
                with Image.open(src) as im:
                    im.save(dst, "WEBP", quality=quality)
                written += 1
            urls[cid] = rel_root + "/" + cid + ext
        out.append(urls)
        notes.append(f"{len(urls)} {label}(s), {written} re-encoded")
    return out[0], out[1], "; ".join(notes)


def art_style(art, square, vert, ids):
    """One CSS rule per card that has art. Emitted after the main stylesheet so these win over the
    placeholder gradient, and referenced by class so the grid can re-render without carrying the
    image bytes through innerHTML.

    Two shapes: `.a-` is the 2:1 banner, `.sq-` the square the game itself uses for a card in a
    list. The board reads the square straight back out of these rules via getComputedStyle, so even
    the SVG never has to carry image bytes."""
    rules = [f".a-{cid}{{background-image:url({uri})}}"
             for cid, uri in art.items() if cid in ids]
    rules += [f".sq-{cid}{{background-image:url({uri})}}"
              for cid, uri in square.items() if cid in ids]
    rules += [f".vt-{cid}{{background-image:url({uri})}}"
              for cid, uri in vert.items() if cid in ids]
    return "<style>\n" + "\n".join(rules) + "\n</style>" if rules else "<style></style>"


def _uri(png_bytes):
    return "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")


def load_tree_chrome(max_px=96):
    """name -> data URI for the SkillTreeAtlas pieces the tree draws.

    The sources are 260-372 px but a node renders at ~30, so they are downscaled on the way in;
    at 96 they still hold up on a hi-dpi screen and the whole set costs well under the art.
    Keys are `<colour><lv>` / `<colour><lv>off` for the node icons, plus TREE_CHROME's names.
    Missing dir is not an error -- the tree falls back to flat circles.
    """
    if not os.path.isdir(TREE_DIR):
        return {}
    try:
        from PIL import Image
    except ImportError:
        return {}

    def grab(fname):
        p = os.path.join(TREE_DIR, fname)
        if not os.path.exists(p):
            return None
        im = Image.open(p).convert("RGBA")
        if max(im.size) > max_px:
            im.thumbnail((max_px, max_px), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "PNG")
        return _uri(buf.getvalue())

    def _mask(im):
        """The glyph's shape, as an alpha mask.

        Two kinds of sprite live in this set and they need opposite treatment:

        - a normal icon is drawn *as* its alpha (the stat letters, the heart), so the alpha
          channel is the shape;
        - a BADGE like `ui_840_icon_allparameterup` is a filled blue disc with the ring and the
          word "All" painted on it in white. Its alpha is ~78 % solid, so using the alpha turns
          the glyph into a plain white circle -- which is exactly how the "All" nodes lost their
          icon. For those the shape is the white INK, not the outline.
        """
        a = im.split()[3]
        w, h = im.size
        pa = a.load()
        opaque = sum(1 for y in range(0, h, 2) for x in range(0, w, 2) if pa[x, y] > 128)
        if opaque <= (w // 2) * (h // 2) * 0.70:
            return a
        rgb = im.convert("RGB").load()
        out = Image.new("L", (w, h), 0)
        o = out.load()
        for y in range(h):
            for x in range(w):
                if pa[x, y] < 128:
                    continue
                r, g, b = rgb[x, y]
                mn, mx = min(r, g, b), max(r, g, b)
                if mn > 165 and mx - mn < 60:          # white ink on the badge
                    o[x, y] = 255
        return out

    def white_glyph(name, px=72, overlay=None):
        """A sprite as a pure-white silhouette -- how the board draws every glyph.

        `overlay` composites a second, smaller sprite into the bottom-right, which is how the
        game builds its reward icons (a gift with a music note for "EXP from Live", and so on).
        """
        hits = glob.glob(os.path.join(SPRITE_DIR, "*", name + ".png"))
        if not hits:
            return None
        im = Image.open(hits[0]).convert("RGBA")
        if max(im.size) > px:
            im.thumbnail((px, px), Image.LANCZOS)
        white = Image.new("RGBA", im.size, (255, 255, 255, 0))
        white.putalpha(_mask(im))

        if overlay:
            oh = glob.glob(os.path.join(SPRITE_DIR, "*", overlay + ".png"))
            if oh:
                ov = Image.open(oh[0]).convert("RGBA")
                side = max(int(max(white.size) * 0.52), 8)
                ov.thumbnail((side, side), Image.LANCZOS)
                tint = Image.new("RGBA", ov.size, (255, 255, 255, 0))
                tint.putalpha(_mask(ov))
                # the base shrinks up-left so the pair still fits the disc
                base = white.resize((int(white.width * 0.84), int(white.height * 0.84)),
                                    Image.LANCZOS)
                canvas = Image.new("RGBA", white.size, (255, 255, 255, 0))
                canvas.alpha_composite(base, (0, 0))
                canvas.alpha_composite(tint, (canvas.width - tint.width,
                                              canvas.height - tint.height))
                white = canvas

        buf = io.BytesIO()
        white.save(buf, "PNG")
        return _uri(buf.getvalue())

    out = {}
    for key, fname in TREE_CHROME.items():
        u = grab(fname)
        if u:
            out[key] = u
    # The node glyphs are the game's own finished assets (skill_icons.py pulls them out of the
    # Octo cache), keyed by asset name. They already contain the magnitude chevrons, the reward
    # composites and the transparent gap that separates a modifier from its base -- so there is
    # nothing here to assemble or tint, only to scale and embed.
    def hole(im):
        """The transparent area *enclosed* by a frame, as a box, or None if it is not hollow.

        Flood-fills transparency inward from the border: whatever transparency the fill cannot
        reach is the interior. Measured rather than hardcoded, so a different frame still lands
        its contents correctly.
        """
        from PIL import ImageDraw
        a = im.split()[3].point(lambda v: 0 if v > 60 else 255)   # 255 = transparent
        w, h = a.size
        ImageDraw.floodfill(a, (0, 0), 128)                       # exterior -> 128
        for seed in ((w - 1, 0), (0, h - 1), (w - 1, h - 1)):
            ImageDraw.floodfill(a, seed, 128)
        inner = a.point(lambda v: 255 if v == 255 else 0)
        box = inner.getbbox()
        if not box:
            return None
        # The bbox alone is too tall: the notches either side of the "Lv" bar are enclosed too,
        # so it reaches the frame's floor and the contents end up oversized and sitting on the
        # text. Keep only the contiguous run of rows that are (nearly) the full interior width.
        px = inner.load()
        widths = []
        for y in range(box[1], box[3]):
            run = [x for x in range(box[0], box[2]) if px[x, y]]
            widths.append((y, (run[-1] - run[0] + 1) if run else 0))
        full = max(w for _, w in widths)
        rows = [y for y, w in widths if w >= full * 0.8]
        if not rows:
            return box
        top = rows[0]
        bottom = top
        for y in rows:
            if y - bottom > 1:
                break
            bottom = y
        return (box[0], top, box[2], bottom + 1)

    raw = {}
    if os.path.isdir(SKILL_ICON_DIR):
        for fn in sorted(os.listdir(SKILL_ICON_DIR)):
            if fn.endswith(".png"):
                raw[fn[:-4]] = Image.open(os.path.join(SKILL_ICON_DIR, fn)).convert("RGBA")

    # A frame plus each skill that can go in it. Keyed "<frame>|<skill>" so the page can ask
    # for the exact pair; the bare frame stays available as a fallback.
    for frame in TREE_FRAME_ICONS:
        fim = raw.get(frame)
        box = hole(fim) if fim else None
        if not box:
            continue
        bw, bh = box[2] - box[0], box[3] - box[1]
        for name, sim in list(raw.items()):
            if not name.startswith("img_skill_icon_active_"):
                continue
            inner = sim.copy()
            inner.thumbnail((int(bw * 0.86), int(bh * 0.86)), Image.LANCZOS)
            canvas = Image.new("RGBA", fim.size, (255, 255, 255, 0))
            canvas.alpha_composite(inner, (box[0] + (bw - inner.width) // 2,
                                           box[1] + (bh - inner.height) // 2))
            canvas.alpha_composite(fim)
            raw[f"{frame}|{name}"] = canvas

    glyphs = {}
    for name, im in raw.items():
        if max(im.size) > 96:
            im = im.copy()
            im.thumbnail((96, 96), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "PNG")
        glyphs[name] = _uri(buf.getvalue())
    u = white_glyph(TREE_GLYPH_FALLBACK)
    if u:
        glyphs["0"] = u                    # nothing matched: a neutral sparkle
    out["glyph"] = glyphs
    out["alias"] = TREE_ICON_ALIAS
    return out


FONT_FILE = os.path.join(HERE, "fonts", "line-seed-sans-xbd-latin.woff2")


def load_font():
    """The @font-face rule with the woff2 inlined, or '' when the file has not been fetched.

    Weight is declared 700-900 rather than 800 so the `font-weight:800` the chrome asks for and
    any `bold` both land on this one file instead of being synthesised.
    """
    if not os.path.exists(FONT_FILE):
        return ("/* LINE Seed Sans not embedded -- run `python fetch_font.py`. "
                "Falling back to the next font in the stack. */")
    uri = "data:font/woff2;base64," + base64.b64encode(open(FONT_FILE, "rb").read()).decode()
    return ("@font-face { font-family:'LINE Seed Sans'; font-style:normal;"
            f" font-weight:700 900; font-display:swap; src:url({uri}) format('woff2'); }}")


def load_chrome():
    """name -> data URI for the game's own card-frame sprites. Missing dir is not an error."""
    out = {}
    for key, rel in CHROME.items():
        p = os.path.join(CHROME_DIR, rel)
        if os.path.exists(p):
            out[key] = _uri(open(p, "rb").read())

    # Ribbon caps, for the tinted body and its white stroke. Pillow only ships here as a UnityPy
    # dependency, so a missing install must degrade to the plain CSS pill rather than fail.
    p = os.path.join(CHROME_DIR, RIBBON)
    if os.path.exists(p):
        try:
            from PIL import Image
        except ImportError:
            return out
        l, r = RIBBON_BORDER
        for prefix, rel in (("rib", RIBBON), ("ribo", RIBBON_OUTLINE)):
            q = os.path.join(CHROME_DIR, rel)
            if not os.path.exists(q):
                continue
            im = Image.open(q).convert("RGBA")
            # L / R are the caps; M is the one-pixel seam column the 9-slice stretches. Emitting
            # M rather than filling the middle with a solid bar matters for the outline: its seam
            # is two thin lines (top and bottom edge), not a filled column, so a solid middle
            # would paint over the hole the stroke is supposed to leave.
            for key, box in ((prefix + "L", (0, 0, l, im.height)),
                             (prefix + "R", (im.width - r, 0, im.width, im.height)),
                             (prefix + "M", (l, 0, l + 1, im.height))):
                buf = io.BytesIO()
                im.crop(box).save(buf, "PNG")
                out[key] = _uri(buf.getvalue())
        im = Image.open(p).convert("RGBA")
        # cap widths as a fraction of the ribbon's drawn width: the prefab draws the ribbon
        # 98.7 x 46 while the sprite is 65 x 96, so a cap keeps its aspect at the drawn height.
        h = 46.0 / im.height
        lp = l * h / 98.7 * 100
        rp = r * h / 98.7 * 100
        out["ribLpct"] = round(lp, 3)
        out["ribRpct"] = round(rp, 3)
        # The middle bar must start exactly where the left cap ends and stop exactly where the
        # right cap starts -- the caps are asymmetric (~19.4% vs ~12.1%), so centring it leaves a
        # notch on the right. A percentage mask-position places an image of width W at
        # p*(100-W)/100, hence p = left / (left + right).
        out["ribMpos"] = round(lp / (lp + rp) * 100, 3)
    return out


def build(cards, art, square, vert, groups=None, state=None, portraits=None, squares=None):
    out = []
    state = state or {}
    portraits = portraits or {}
    squares = squares or {}
    for c in cards:
        ex = c["_extracted"]
        stats = dict(c["stats"] or {"performance": 0, "technique": 0, "sense": 0})
        stats["total"] = sum(stats.values())
        desc = ex.get("descriptions") or {}
        cur = ex.get("stats_current_grpc")
        gens = card_gens("chr-" + c["id"].split("-")[1], c["generation"], groups)
        haystack = " ".join(filter(None, [
            c.get("title"), c["member"], c["type"], c["rarity"], c["id"],
            c.get("costume_name"), *c["generation"], *[g for g, _, _ in gens],
            *[v for v in desc.values() if v],
        ])).lower()
        out.append({
            # card-04003-5-uniq-... belongs to chr-04003; that key joins a card to its board
            "id": c["id"], "chr": "chr-" + c["id"].split("-")[1],
            "title": c.get("title"), "member": c["member"],
            # `generation` stays raw (the Japanese unit ids) so the search box still matches
            # what the dataset says. `gens` stays a list of plain labels because the filter
            # dropdown compares against it; the tints ride alongside in `gcol`, index-matched.
            "generation": c["generation"],
            "gens": [g for g, _, _ in gens], "gcol": [col for _, col, _ in gens],
            "gcol2": [c2 for _, _, c2 in gens],
            "gen": " / ".join(g for g, _, _ in gens),
            "type": c["type"], "rarity": c["rarity"],
            "costume_name": c.get("costume_name"), "stats": stats,
            "owned": cur is not None, "current": cur, "desc": desc, "haystack": haystack,
            "art": c["id"] in art, "sq": c["id"] in square, "vt": c["id"] in vert,
            # Relative urls to real files, not data: URIs -- present only when the native art was
            # exported. The face takes vtsrc, then sqsrc, then the embedded `vt`, then `sq`.
            **({"vtsrc": portraits[c["id"]]} if c["id"] in portraits else {}),
            **({"sqsrc": squares[c["id"]]} if c["id"] in squares else {}),
            # Hand-authored, from card_state.json -- see card_state.py for why it cannot come
            # from a capture. Absent keys mean "unknown", and the face draws neither badge.
            **{k: v for k, v in (state.get(c["id"]) or {}).items()
               if k in ("lv", "bloom") and v is not None},
        })
    return out


def main():
    art = load_art()
    chrome = load_chrome()
    chrome["tree"] = load_tree_chrome()      # nested so CHROME.<key> lookups stay unambiguous
    groups = load_gen_groups()
    raw = json.load(open(SRC, encoding="utf-8"))
    ids = {c["id"] for c in raw}             # build() is 1:1 on ids, so this is the final set

    dst = sys.argv[sys.argv.index("-o") + 1] if "-o" in sys.argv \
        else os.path.join(HERE, "cards_explorer.html")
    # Portraits AND squares go to files beside the page at native 512 px. The embedded path runs
    # only when that is impossible, and then it embeds at the fallback sizes -- blur included.
    # See EMBED_FALLBACK_MAX_PX / SQ_EMBED_FALLBACK_MAX_PX for why they are not simply raised.
    portraits, squares, art_note = export_art(dst, ids)
    vert = {} if portraits else load_art(VERT_PREFIX, max_px=EMBED_FALLBACK_MAX_PX)
    # `square` is what the .sq- CSS rules carry -- the sidecar urls when available, the
    # embedded data URIs otherwise. Both the board's Connect-node pattern and the .sqchip in the
    # Connect list read those rules back, so they keep working either way.
    square = squares or load_art(SQ_PREFIX, max_px=SQ_EMBED_FALLBACK_MAX_PX)

    # Lv and bloom are hand-authored (card_state.json) because they are in no capture; `max`
    # resolves against the game's own ceilings from card_caps.py. Warnings are printed rather
    # than swallowed -- a typo'd card id or an out-of-range level should be visible.
    state, state_warnings = card_state.resolve([
        (c["id"], RARITY_RANK(c["rarity"]), (c["_extracted"] or {}).get("attribute"))
        for c in raw
    ])
    cards = build(raw, art, square, vert, groups, state, portraits, squares)
    # A literal </script> inside the JSON would close the tag early; the only escape it needs.
    esc = lambda o: json.dumps(o, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    tree = json.load(open(TREE_SRC, encoding="utf-8")) if os.path.exists(TREE_SRC) \
        else {"branches": {}, "nodes": {}, "models": {}, "members": [], "extents": {},
              "connect": {}, "card_connect": {}, "unlocked": {}}
    page = (PAGE.replace("__FONT__", load_font())
                .replace("__ART__", art_style(art, square, vert, ids))
                .replace("__TREE__", esc(tree))
                .replace("__CHROME__", esc(chrome))
                .replace("__DATA__", esc(cards)))
    open(dst, "w", encoding="utf-8").write(page)

    owned = sum(1 for c in cards if c["owned"])
    with_art = sum(1 for c in cards if c["art"])
    orphans = sorted(set(art) - ids)
    print(f"{len(cards)} cards ({owned} owned, {with_art} with art) -> {dst}  "
          f"[{os.path.getsize(dst)/1024:.0f} KB]")
    if portraits or squares:
        art_dir = os.path.splitext(dst)[0] + "_art"
        # Sized off the directory itself, not off what the loop believes it wrote.
        files = os.listdir(art_dir)
        total = sum(os.path.getsize(os.path.join(art_dir, f)) for f in files)
        print(f"  sidecar art: {art_note} at 512 px native -> {art_dir}/  "
              f"[{len(files)} files, {total/1024/1024:.2f} MB]")
    if not portraits:
        print(f"  portraits: EMBEDDED FALLBACK -- {art_note}")
    if not squares:
        print(f"  squares: EMBEDDED FALLBACK -- {art_note}")
    if orphans:
        print(f"  warning: {len(orphans)} image(s) match no card: {', '.join(orphans[:5])}")

    with_lv = sum(1 for c in cards if c.get("lv") is not None)
    with_bloom = sum(1 for c in cards if c.get("bloom") is not None)
    manual = sum(1 for s in state.values() if s["src"] == "manual")
    print(f"  card state: {with_lv} with Lv, {with_bloom} with bloom "
          f"({manual} entered by hand, rest from default "
          f"{card_state.load_state()['default']!r})")
    for w in state_warnings:
        print(f"  warning: {w}")


if __name__ == "__main__":
    main()
