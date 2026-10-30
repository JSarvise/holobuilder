// hb-grade.js — the game's RankIcon, reproduced from its prefab (Night Deck component).
//
// PORTED, not designed. Every number below is read out of the shipped client
// (docs/redesign/IDENTITY.md §Grade badge, tools/recon/UI-OFFICIAL-ASSETS.md §3a/§4a):
//  * RectTransforms of `RankIcon_180` (LiveDeckStatusDetailConfirmDialog c7b8e1b98029,
//    LiveDeckEditScreen 051497f6ed47, PrePlay ba7144946fd1, song select HighScoreRank
//    16ba0fd3c171: all identical) and `RankIcon_120` (ParkLiveQuestScreen 04c5217a269a).
//  * Sprite rects: every letter is an 800x800 sprite (live letters 304x304), tight-packed;
//    the PNGs in dumps/ui_official are CROPPED, so the padding is restored here from
//    m_RD.textureRect / textureRectOffset. Drawing the crop into the box is what made the
//    v3 mock's "+" and digit sit wrong.
//  * `RankIcon.Set(rankType, plus)` @rva 62bd770 (disassembled): letter by rankType 0..4 =
//    D,C,B,A,S; `_withNumberValueChanges.Change(Math.Min(plus, 2))` (0 none, 1 Plus,
//    2 PlusWithNumber); `_rankTypeValueChanges.Change(rankType)` picks the "+"/digit colour
//    from GraphicColorChange._values — the "+" is NOT always #805DFF, only on S.
//  * The digit: TMP LINESeedSans_A_XBd SDF (faceInfo pointSize 60, descentLine -16.44,
//    digits tabular, advance 38.94), 56 px, right/bottom aligned, margin.w -9,
//    TextOutlineModifier 6.43 px white.
// Coordinates: the RankIcon box, origin top-left, y down, in prefab units.

const LETTERS = ["D", "C", "B", "A", "S"];
// GraphicColorChange._values by rankType (Color floats -> hex)
export const PLUS_COLOR = { D: "#57ABFF", C: "#91DB10", B: "#F9BD00", A: "#F978C5", S: "#805DFF" };

// [rect, offX, offYfromBottom, w, h] per sprite (m_Rect / m_RD.textureRectOffset / textureRect)
const SPRITE = {
  deck: { // ui_999_txt_score_* (RankAtlas) — LiveDeckStatusUtility.GetRankSpriteAddress
    D: [800, 60.08, 32.08, 706.8, 727.8], C: [800, 29.08, 31.08, 742.8, 731.8],
    B: [800, 81.08, 33.08, 636.8, 726.8], A: [800, 67.08, 33.08, 665.8, 726.8],
    S: [800, 95.08, 39.08, 610.8, 724.8],
  },
  live: { // ui_410_txt_score_* — RankIcon.SetMultiEvaluationRank
    D: [304, 8.08, 11.08, 295.9, 292.8], C: [304, 0, 11.08, 304, 292.8],
    B: [304, 15.08, 11.08, 273.8, 292.8], A: [304, 10.08, 12.08, 283.8, 291.8],
    S: [304, 22.08, 13.08, 259.8, 288.8],
  },
  plus: [256, 30.08, 30.08, 195.8, 195.8],          // ui_999_ico_plus
  outline: [256, 1.076, 1.013, 253.8, 254.0],       // ui_999_ico_plus_outline
};

// Prefab geometry per RankIcon size. plus: the bare "+" node; pwn: PlusWithNumber.
const LAYOUT = {
  180: { box: 180, plus: { right: 6, bottom: 16, size: 48, outline: 49.23, glyph: 44.31 },
         pwn: { right: 10.3, bottom: 16.15, h: 66, root: 32, outline: 32.82, glyph: 29.54, font: 56 } },
  120: { box: 120, plus: { right: 0, bottom: 0, size: 40, outline: 41.03, glyph: 36.92 },
         pwn: { right: 0, bottom: 0, h: 66, root: 32, outline: 32.82, glyph: 29.54, font: 40 } },
};
const FACE = { pointSize: 60, descent: -16.44, advance: 38.94 }; // TMP faceInfo
const MARGIN_BOTTOM = -9, OUTLINE_PX = 6.43;

const BASE = "/ui/grade/";
let seq = 0;
const f = n => +n.toFixed(3);

// a sprite drawn the way Unity draws a tight-packed Simple Image: the FULL sprite rect
// stretched into (x,y,w,h), the cropped texture placed at its offset inside it
function placed(spr, x, y, w, h) {
  const [R, ox, oy, tw, th] = spr, sx = w / R, sy = h / R;
  return { x: f(x + ox * sx), y: f(y + (R - oy - th) * sy), w: f(tw * sx), h: f(th * sy) };
}
function img(href, b, extra = "") {
  return `<image href="${href}" x="${b.x}" y="${b.y}" width="${b.w}" height="${b.h}" preserveAspectRatio="none"${extra}/>`;
}
// the white "+" silhouette tinted (Unity vertex colour = multiply on white = the colour)
function tinted(href, b, color) {
  const id = `hbg-m${++seq}`;
  return `<mask id="${id}" style="mask-type:alpha" maskUnits="userSpaceOnUse">${img(href, b)}</mask>` +
    `<rect x="${b.x}" y="${b.y}" width="${b.w}" height="${b.h}" fill="${color}" mask="url(#${id})"/>`;
}
function plusPair(cx, cy, outline, glyph, color) {
  const o = placed(SPRITE.outline, cx - outline / 2, cy - outline / 2, outline, outline);
  const g = placed(SPRITE.plus, cx - glyph / 2, cy - glyph / 2, glyph, glyph);
  return img(BASE + "ui_999_ico_plus_outline.png", o) + tinted(BASE + "ui_999_ico_plus.png", g, color);
}

/** The badge as an inline SVG string.
 *  grade: {letter:"S", plus:2} (from rankOf) or "S+2"; opts.size px (default 48),
 *  opts.kind "deck" (ui_999, default: Unit Score / Overall Power / Score Bonus / song
 *  select high score) or "live" (ui_410, multi-live result); opts.variant 180 | 120. */
export function badge(grade, opts = {}) {
  const g = typeof grade === "string" ? parse(grade) : grade;
  if (!g || !LETTERS.includes(g.letter)) return "";
  const L = LAYOUT[opts.variant === 120 ? 120 : 180], B = L.box;
  const kind = opts.kind === "live" ? "live" : "deck";
  const size = opts.size || 48, color = PLUS_COLOR[g.letter];
  const plus = Math.max(0, g.plus | 0);
  const label = `${g.letter}${plus ? (plus === 1 ? "+" : "+" + plus) : ""}`;
  const file = kind === "live" ? `ui_410_txt_score_${g.letter.toLowerCase()}.png` : `ui_999_txt_score_${g.letter.toLowerCase()}.png`;
  let s = img(BASE + file, placed(SPRITE[kind][g.letter], 0, 0, B, B));
  if (Math.min(plus, 2) === 1) {
    const p = L.plus, cx = B - p.right - p.size / 2, cy = B - p.bottom - p.size / 2;
    s += plusPair(cx, cy, p.outline, p.glyph, color);
  } else if (plus >= 2) {
    const p = L.pwn, digits = String(plus), numW = digits.length * FACE.advance * p.font / FACE.pointSize;
    const right = B - p.right, bottom = B - p.bottom;
    const rootCx = right - numW - p.root / 2, rootCy = bottom - p.root / 2;
    s += plusPair(rootCx, rootCy, p.outline, p.glyph, color);
    // TMP Bottom alignment: baseline = (box bottom + margin.w) - descentLine * scale
    const baseline = bottom - (-FACE.descent * p.font / FACE.pointSize + MARGIN_BOTTOM);
    s += `<text x="${f(right)}" y="${f(baseline)}" text-anchor="end" font-family="'LINE Seed Sans',sans-serif" font-weight="800" ` +
      `font-size="${p.font}" fill="${color}" stroke="#fff" stroke-width="${OUTLINE_PX * 2}" stroke-linejoin="round" paint-order="stroke">${digits}</text>`;
  }
  const t = opts.title === false ? "" : `<title>${opts.title || "Grade " + label}</title>`;
  return `<svg class="hb-grade${opts.cls ? " " + opts.cls : ""}" viewBox="0 0 ${B} ${B}" width="${size}" height="${size}" role="img" aria-label="Grade ${label}" data-grade="${label}" overflow="visible">${t}${s}</svg>`;
}

export function parse(str) {
  const m = /^([DCBAS])(\+(\d*))?$/.exec(String(str || "").trim());
  return m ? { letter: m[1], plus: m[2] ? (m[3] ? +m[3] : 1) : 0 } : null;
}

/* ---- thresholds: GET /api/ranks (master LiveDeckEvaluationRank / LiveScoreEvaluationRank) ---- */
let RANKS = null, pending = null;
export function loadRanks() {
  if (RANKS) return Promise.resolve(RANKS);
  if (!pending) pending = fetch("/api/ranks").then(r => r.json())
    .then(r => (RANKS = r && r.available !== false ? r : null)).catch(() => null);
  return pending;
}
const asc = rows => (rows || []).slice().sort((a, b) => a.threshold - b.threshold);
export const unitRows = () => asc(RANKS && RANKS.unit_score);
export function songRows(musicId) {
  const s = RANKS && RANKS.song; if (!s) return [];
  return asc(s.groups[(s.exceptions || {})[musicId] || s.default_group]);
}
/** The grade a value earns under a threshold table (rows from /api/ranks), plus the next
 *  step. null when the table is empty: no badge, never a guess. */
export function rankOf(value, rows) {
  let i = -1;
  for (let k = 0; k < rows.length; k++) if (value >= rows[k].threshold) i = k;
  if (i < 0) return null;
  const r = rows[i], n = rows[i + 1];
  return { letter: r.letter, plus: r.plus, grade: r.grade, threshold: r.threshold,
           next: n ? { grade: n.grade, threshold: n.threshold, short: n.threshold - value } : null };
}

if (typeof window !== "undefined") window.HBGrade = { badge, parse, rankOf, loadRanks, unitRows, songRows, PLUS_COLOR };

/** A score as the game prints it: padded to `width` digits (8 = the result screen's
 *  ScoreValue, `<color=#FFFFFF59>0000000</color>0`), thousands commas, the leading zeros
 *  wrapped in <span class="z"> (dimmed by colour in hb.css). Use it for EVERY score, or
 *  for none: mixed padded/unpadded figures read as different units. */
export function score(value, width = 8) {
  const s = String(Math.max(0, Math.round(+value || 0))).padStart(width, "0");
  const lead = (s.match(/^0*/)[0].length === s.length) ? s.length - 1 : s.match(/^0*/)[0].length;
  const g = s.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  let k = 0, out = "", open = false;
  for (const ch of g) {
    const dim = ch === "," ? k < lead : k++ < lead;
    if (dim && !open) { out += '<span class="z">'; open = true; }
    if (!dim && open) { out += "</span>"; open = false; }
    out += ch;
  }
  return `<span class="n">${open ? out + "</span>" : out}</span>`;
}
if (typeof window !== "undefined") window.HBGrade.score = score;
