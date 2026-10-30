// gacha.js — the "Gacha" view. Everything shown comes from /api/gacha (the append-only
// store data/gacha/, aggregated read-only by gacha_view.py). Nothing is computed from
// the game here except drawing. Absence is first-class: a banner without a saved rate
// table says so, pulls without an id are listed under "not identified".
//
// THE PALETTE IS NOT TASTE. The three rarity hues below were run through the dataviz
// skill's validator against this page's own panel surface (#12244f, dark mode):
//   node scripts/validate_palette.js "#d07e00,#8840c6,#0098ff" --mode dark --surface "#12244f"
//   Lightness band PASS · Chroma floor PASS · CVD separation PASS (worst adjacent
//   deutan ΔE 16.2) · Normal-vision floor PASS (ΔE 24.6) · Contrast WARN on the 4★
//   purple (2.58:1) — relieved, as the skill requires, by visible direct labels on
//   every mark plus the "Show the numbers" table under the chart.
// The game's own gold/violet/blue rarity language is kept; only the steps moved, because
// the game's bright #f2b73c/#b98bff/#7fb3d5 failed the band, the chroma floor AND the
// colour-blind separation check (blue↔violet collapsed to ΔE 7.9 under deuteranopia).
import { el, svgEl } from "/dom.js";

const RAR = ["5★", "4★", "3★"];
const COL = { "5★": "#d07e00", "4★": "#8840c6", "3★": "#0098ff" };
// The game's own star sprite (the card faces draw the same file, `ranking.js` CHROME.star).
// Rarity is the page's most repeated token, so it is spoken here in the game's language
// rather than in ours; the digit stays beside it, because a picture alone is unreadable
// to a screen reader and vanishes if the sprite 404s.
const STAR = "/chrome/rarity/ui_999_img_card_star.png";
const rank = (r) => { const n = parseInt(r, 10); return Number.isFinite(n) ? n : 0; };
const num = (n) => (typeof n === "number" ? n.toLocaleString("en-US") : "—");
const pct = (a, b) => (b ? ((100 * a) / b).toFixed(2) + "%" : "—");

// The wire mixes units: `last_sync`/`first_seen` are epoch SECONDS (written by this tool's
// sync), while a banner's `start_time`/`end_time` pass through the game's own values,
// which are MILLISECONDS. Normalising here is why one helper serves both.
const secs = (t) => (typeof t === "number" ? (Math.abs(t) > 1e12 ? t / 1000 : t) : null);
const day = (t) => { const s = secs(t); return s == null ? "—" : new Date(s * 1000).toISOString().slice(0, 10); };
const stamp = (t) => { const s = secs(t); return s == null ? null : new Date(s * 1000).toLocaleString(); };

// A permanent banner is not dated. On the live wire the holodori Gacha
// (`gacha-common-normal-001`) arrives with NO start_time, NO end_time and
// `is_end_time_hidden: 1` — that flag is the game saying "this one does not close".
// `— → —` said nothing; "permanent" says the thing. A 0 or a sentinel end far past any
// real schedule is read the same way. An open END alone is "since <date>", because that
// banner IS running. Two nulls and no flag stay unknown: a closed banner whose dates were
// never captured must not be relabelled permanent.
const FAR_FUTURE = Date.UTC(2100, 0, 1) / 1000;
function period(b) {
  const hasS = typeof b.start_time === "number";
  const hasE = typeof b.end_time === "number";
  const hidden = !!b.is_end_time_hidden;
  if (!hasS && !hasE && !hidden) return null;      // simply unknown — say nothing
  const sOpen = !hasS || b.start_time === 0;
  const eOpen = !hasE || b.end_time === 0 || hidden || secs(b.end_time) >= FAR_FUTURE;
  if (sOpen && eOpen) return "permanent";
  if (eOpen) return `since ${day(b.start_time)}`;
  if (sOpen) return `until ${day(b.end_time)}`;
  return `${day(b.start_time)} → ${day(b.end_time)}`;
}

async function fetchView() {
  const r = await fetch("/api/gacha");
  return r.json();
}

// THE DENOMINATOR. The comparison is the SERVER'S (`view.comparison`): it splits every
// pull into covered + excluded + uncovered, so observed and expected are always the same
// population and no pull can go missing. The client does not recompute it from the
// banner rows, which cannot see the ticket/tutorial exclusion rule at all.
const EMPTY_CMP = {
  covered_pulls: 0, observed_by_rarity: {}, expected_by_rarity: {},
  excluded_pulls: 0, uncovered_pulls: 0, excluded: [],
};
const comparisonOf = (v) => Object.assign({}, EMPTY_CMP, v.comparison || {});

// N star sprites in a row, with the rarity token beside them.
//
// `compact` is for the narrow pills (a banner card's `3 / 3.5`, an unidentified row's
// count), where "★★★★★ 5★" printed the same fact twice in a box a few characters wide.
// There the sprites ARE the label, so they stop being decorative: the row carries
// role=img + aria-label="5★" instead of aria-hidden, and if the sprite 404s the token
// comes back — the meaning is never allowed to leave with the picture.
function stars(r, compact) {
  const tag = el("span", "g-rar");
  const row = el("span", "g-stars");
  if (compact) {
    row.setAttribute("role", "img");
    row.setAttribute("aria-label", r);
  } else {
    row.setAttribute("aria-hidden", "true");
  }
  for (let i = 0, n = rank(r); i < n; i++) {
    const im = document.createElement("img");
    im.src = STAR;
    im.alt = "";
    // If the sprite cannot be served the row must not become an empty gap: the token
    // beside it already carries the meaning, so the broken images just leave.
    im.addEventListener("error", () => {
      row.remove();
      if (compact && !tag.querySelector(".g-rar-n")) tag.append(el("span", "g-rar-n", r));
    }, { once: true });
    row.append(im);
  }
  tag.append(row);
  if (!compact) tag.append(el("span", "g-rar-n", r));
  return tag;
}

// The same thing inside an SVG, where an <img> cannot go: the digit as text, then the
// sprites as <image>, laid out to END at `right`. Draws into `svg`; returns nothing.
function starsSvg(svg, r, right, cy) {
  const S = 11, GAP = 1.5, n = rank(r);
  let x = right - n * (S + GAP) + GAP;
  const t = svgEl("text", { x: x - 6, y: cy + 4, "text-anchor": "end", class: "g-ax" });
  t.textContent = String(rank(r));
  svg.append(t);
  for (let i = 0; i < n; i++) {
    const im = svgEl("image", { x, y: cy - S / 2, width: S, height: S, href: STAR });
    svg.append(im);
    x += S + GAP;
  }
}

// --- tiles ------------------------------------------------------------------

function tiles(v, cmp) {
  const wrap = el("div", "g-tiles");
  wrap.append(tile(el("span", "", "Pulls"), num(v.totals.pulls)));
  for (const r of RAR) {
    const got = v.totals.by_rarity[r] || 0;
    const exp = cmp.expected_by_rarity[r];
    // The big number is EVERY pull; the small line is the expectation for the covered
    // subset only, and says so — otherwise it reads as a comparison against the big
    // number, which is the mismatch this whole file now guards against.
    wrap.append(
      tile(stars(r), `${num(got)} · ${pct(got, v.totals.pulls)}`,
        exp == null || !cmp.covered_pulls
          ? "no saved rates"
          : `expected ${exp.toFixed(1)} of ${num(cmp.covered_pulls)} compared`,
        COL[r])
    );
  }
  return wrap;
}

function tile(label, big, small, color) {
  const t = el("div", "g-tile");
  if (color) t.style.borderTopColor = color;
  const l = el("div", "g-tile-l");
  l.append(label);
  t.append(l, el("div", "g-tile-b", big));
  if (small) t.append(el("div", "g-tile-s", small));
  return t;
}

// --- chart 1: observed vs expected, per rarity -------------------------------
//
// A bullet chart, not the two-bar grouped form: the question is "did I get what the
// game's own rate table predicts", which is one measure against one target. The bar
// is what happened; the tick is the prediction. Every bar carries its number, so
// nothing here is read from colour alone.

function ratesChart(v, cmp) {
  const box = el("div", "g-chartbox");
  box.append(el("h4", "g-ch-t", "Observed vs expected, by rarity"));

  // No banner carries a rate table: there is no expectation to compare against, and a
  // chart drawn anyway would be three bars against zero. Say it instead.
  if (!cmp.covered_pulls) {
    box.append(el("p", "note",
      "No banner in the store has a saved rate table yet, so there is nothing to " +
      "compare these pulls against. Sync while a banner is live to capture one."));
    return box;
  }

  const rows = RAR.map((r) => ({
    r,
    got: cmp.observed_by_rarity[r] || 0,
    exp: cmp.expected_by_rarity[r] == null ? null : cmp.expected_by_rarity[r],
  }));
  const max = Math.max(1, ...rows.flatMap((x) => [x.got, x.exp || 0])) * 1.12;

  const W = 540, ROW = 34, PADL = 88, PADR = 56, TOP = 8;
  const H = TOP + rows.length * ROW + 6;
  const BAR = 18;
  const x = (val) => PADL + ((W - PADL - PADR) * val) / max;

  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, class: "g-chart", role: "img" });
  svg.setAttribute("aria-label", "Pulls observed per rarity against the rate tables' expectation");

  rows.forEach((row, i) => {
    const y = TOP + i * ROW;
    const cy = y + ROW / 2;

    // Row label — the digit and the game's star sprites, never the series colour
    // (the mark carries identity; the label only names the row).
    starsSvg(svg, row.r, PADL - 10, cy);

    // The track the bar runs in, so an empty rarity still reads as a row.
    svg.append(svgEl("rect", {
      x: PADL, y: cy - BAR / 2, width: W - PADL - PADR, height: BAR,
      rx: 4, class: "g-track",
    }));

    const w = Math.max(row.got > 0 ? 3 : 0, x(row.got) - PADL);
    if (w > 0) {
      const bar = svgEl("rect", {
        x: PADL, y: cy - BAR / 2, width: w, height: BAR, rx: 4, fill: COL[row.r],
      });
      const t = svgEl("title", {});
      t.textContent = row.exp == null
        ? `${row.r}: ${row.got} pulled · no saved rate table`
        : `${row.r}: ${row.got} pulled · ${row.exp.toFixed(1)} expected (${(row.got - row.exp >= 0 ? "+" : "") + (row.got - row.exp).toFixed(1)})`;
      bar.append(t);
      svg.append(bar);
    }

    // The target tick: the rate tables' expectation, in ink, with a 2px surface gap
    // either side so it never reads as part of the fill.
    if (row.exp != null) {
      const tx = x(row.exp);
      svg.append(svgEl("line", {
        x1: tx, x2: tx, y1: cy - BAR / 2 - 4, y2: cy + BAR / 2 + 4, class: "g-target",
      }));
    }

    const val = svgEl("text", { x: W - PADR + 8, y: cy + 4, class: "g-val" });
    val.textContent = row.exp == null ? String(row.got) : `${row.got} / ${row.exp.toFixed(1)}`;
    svg.append(val);
  });

  const leg = el("div", "g-legend");
  // The bar key wears all three rarity hues, because the bars do: the legend is
  // saying "the filled bar", not "this colour".
  leg.append(
    legendKey("observed pulls",
      `linear-gradient(90deg, ${COL["5★"]} 0 33%, ${COL["4★"]} 33% 66%, ${COL["3★"]} 66% 100%)`, true),
    legendKey("expected by the saved rate tables", null, false)
  );
  box.append(leg, svg);
  // The denominator, stated: what was compared, what was deliberately left out, and
  // what simply has no rate table. The three add up to every pull in the store.
  const cap = el("p", "g-cap");
  cap.append(el("span", "",
    `compared: ${num(cmp.covered_pulls)} pulls from banners with a saved rate table`));
  if (cmp.excluded_pulls) {
    cap.append(el("span", "",
      ` · excluded: ticket/tutorial pulls (${num(cmp.excluded_pulls)})`));
  }
  if (cmp.uncovered_pulls) {
    cap.append(el("span", "",
      ` · no saved rates: ${num(cmp.uncovered_pulls)}`));
  }
  box.append(cap);
  if (cmp.excluded.length) {
    // Named, not just counted: "excluded" is a claim about specific pulls, and the
    // user must be able to check WHICH ones without opening the store.
    const list = el("p", "g-cap g-excl");
    list.append(el("span", "", "left out: "));
    cmp.excluded.forEach((x, i) => {
      if (i) list.append(el("span", "", " · "));
      list.append(el("span", "g-excl-n", `${x.gacha_name || "(no name)"} (${x.kind}, ${num(x.pulls)})`));
    });
    box.append(list);
  }
  box.append(ratesTable(v, cmp));
  return box;
}

// The legend keys: a swatch for the bar, a tick glyph for the target.
function legendKey(text, color, isBar) {
  const k = el("span", "g-key");
  const sw = el("i", isBar ? "g-key-bar" : "g-key-tick");
  if (color) sw.style.background = color;
  k.append(sw, el("span", "", text));
  return k;
}

// The table view the contrast WARN obliges (and the honest place for the raw numbers).
// Same population as the chart above it: the covered pulls, never the store total.
// A table that quietly used a different denominator would be worse than no table.
function ratesTable(v, cmp) {
  const d = el("details", "g-table");
  d.append(el("summary", "", `Show the numbers (${num(cmp.covered_pulls)} compared pulls)`));
  const t = document.createElement("table");
  const head = document.createElement("tr");
  for (const h of ["Rarity", "Observed", "Share", "Expected", "Difference"]) {
    head.append(el("th", "", h));
  }
  t.append(head);
  for (const r of RAR) {
    const got = cmp.observed_by_rarity[r] || 0;
    const exp = cmp.expected_by_rarity[r];
    const tr = document.createElement("tr");
    const key = el("td", "g-td-r");
    const dot = el("i", "g-dot");
    dot.style.background = COL[r];
    key.append(dot, stars(r));
    tr.append(key, el("td", "g-td-n", num(got)), el("td", "g-td-n", pct(got, cmp.covered_pulls)));
    tr.append(el("td", "g-td-n", exp == null ? "—" : exp.toFixed(1)));
    tr.append(el("td", "g-td-n", exp == null ? "—" : (got - exp >= 0 ? "+" : "") + (got - exp).toFixed(1)));
    t.append(tr);
  }
  d.append(t);
  const foot = el("p", "g-cap",
    `Observed here counts only the ${num(cmp.covered_pulls)} pulls the server compared — ` +
    `banners with a saved rate table, minus the ticket and tutorial pulls, which were not ` +
    `bought with a rate roll. The store holds ${num(v.totals.pulls)} pulls in all` +
    `${cmp.uncovered_pulls ? `, ${num(cmp.uncovered_pulls)} of them from banners whose rates were never saved` : ""}.`);
  d.append(foot);
  return d;
}

// --- chart 2: the timeline ---------------------------------------------------
//
// Pulls per day as columns, with a dot on the days that gave a ★5. The API sends only
// the days that HAVE pulls; a time axis with the empty days squeezed out lies about
// pace, so the gaps are filled in here and drawn as zero.

function fillDays(rows) {
  if (!rows.length) return [];
  const at = new Map(rows.map((r) => [r.day, r]));
  const toMs = (s) => Date.UTC(+s.slice(0, 4), +s.slice(5, 7) - 1, +s.slice(8, 10));
  const first = toMs(rows[0].day);
  const last = toMs(rows[rows.length - 1].day);
  const out = [];
  const DAY = 86400000;
  // A guard, not a formality: a corrupt date would otherwise spin this loop forever.
  for (let t = first; t <= last && out.length < 4000; t += DAY) {
    const key = new Date(t).toISOString().slice(0, 10);
    const row = at.get(key);
    out.push({ day: key, pulls: (row && row.pulls) || 0, five_star: (row && row.five_star) || 0 });
  }
  return out;
}

function timeline(v) {
  const box = el("div", "g-chartbox");
  box.append(el("h4", "g-ch-t", "Pulls per day"));

  const rows = fillDays(Array.isArray(v.timeline) ? v.timeline : []);
  if (!rows.length) {
    box.append(el("p", "note", "No dated pulls in the store yet."));
    return box;
  }

  const leg = el("div", "g-legend");
  const barKey = el("span", "g-key");
  const barSw = el("i", "g-key-bar");
  barSw.style.background = "var(--dori)";
  barKey.append(barSw, el("span", "", "pulls that day"));
  const dotKey = el("span", "g-key");
  const dotSw = el("i", "g-key-dot");
  dotSw.style.background = COL["5★"];
  dotKey.append(dotSw, el("span", "", "a ★5 came out (dot = how many)"));
  leg.append(barKey, dotKey);
  box.append(leg);

  const W = 720, H = 200, PADL = 34, PADR = 10, TOP = 22, BASE = H - 34;
  const max = Math.max(1, ...rows.map((r) => r.pulls));
  const slot = (W - PADL - PADR) / rows.length;
  // ≤24px thick, and a 2px surface gap between neighbours — the gap is what separates
  // touching columns; no stroke is drawn around them.
  const bw = Math.max(2, Math.min(24, slot - 2));
  const y = (val) => BASE - ((BASE - TOP) * val) / max;

  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, class: "g-chart g-chart-wide", role: "img" });
  svg.setAttribute("aria-label", "Pulls per day, with the days that produced a five-star marked");

  // Two round y ticks (0 and the max), hairline and recessive — the columns are not
  // individually labelled, so the axis carries the values the tooltips don't.
  for (const val of [0, max]) {
    svg.append(svgEl("line", { x1: PADL, x2: W - PADR, y1: y(val), y2: y(val), class: "g-grid" }));
    const t = svgEl("text", { x: PADL - 8, y: y(val) + 4, "text-anchor": "end", class: "g-ax" });
    t.textContent = num(val);
    svg.append(t);
  }

  // x labels: first, last, and every Nth day between — enough to date the shape
  // without a wall of text. The last day is ALWAYS labelled (it is the end of the
  // window and the caption quotes it), so it is placed first and any periodic label
  // that would collide with it is dropped rather than overprinted; the last one is
  // then right-aligned to the plot edge so it cannot run past it either.
  const step = Math.max(1, Math.ceil(rows.length / 7));
  const cxOf = (i) => PADL + i * slot + slot / 2;
  const LBL_GAP = 42;               // ~ the width of "08-17" plus breathing room
  const lastI = rows.length - 1;
  const marks = [];
  for (let i = 0; i < lastI; i += step) marks.push(i);
  while (marks.length && cxOf(lastI) - cxOf(marks[marks.length - 1]) < LBL_GAP) marks.pop();
  marks.push(lastI);
  const labelAt = new Set(marks);

  rows.forEach((row, i) => {
    const cx = PADL + i * slot + slot / 2;
    if (row.pulls > 0) {
      const h = Math.max(2, BASE - y(row.pulls));
      const bar = svgEl("rect", {
        x: cx - bw / 2, y: BASE - h, width: bw, height: h, rx: 4, fill: "var(--dori)",
      });
      const t = svgEl("title", {});
      t.textContent = `${row.day}: ${row.pulls} pull${row.pulls === 1 ? "" : "s"}` +
        (row.five_star ? ` · ${row.five_star} ★5` : "");
      bar.append(t);
      svg.append(bar);
    }
    if (row.five_star > 0) {
      const dy = row.pulls > 0 ? y(row.pulls) - 10 : BASE - 10;
      // r ≥ 4 with a 2px ring in the surface colour, so the dot survives sitting on
      // top of its own column.
      const dot = svgEl("circle", { cx, cy: dy, r: 5, fill: COL["5★"], class: "g-dot-mark" });
      const t = svgEl("title", {});
      t.textContent = `${row.day}: ${row.five_star} ★5 in ${row.pulls} pulls`;
      dot.append(t);
      svg.append(dot);
      if (row.five_star > 1) {
        const n = svgEl("text", { x: cx, y: dy - 9, "text-anchor": "middle", class: "g-val" });
        n.textContent = "×" + row.five_star;
        svg.append(n);
      }
    }
    if (labelAt.has(i)) {
      const last = i === lastI;
      const t = svgEl("text", {
        x: last ? Math.min(cx + 12, W - PADR) : cx,
        y: H - 12,
        "text-anchor": last ? "end" : "middle",
        class: "g-ax",
      });
      t.textContent = row.day.slice(5);
      svg.append(t);
    }
  });

  box.append(svg);
  const total = rows.reduce((a, r) => a + r.pulls, 0);
  const stars = rows.reduce((a, r) => a + r.five_star, 0);
  box.append(el("p", "g-cap",
    `${rows.length} days, ${rows[0].day} to ${rows[rows.length - 1].day} · ` +
    `${num(total)} pulls · ${num(stars)} ★5`));
  return box;
}

// --- banners -----------------------------------------------------------------

function bannerCard(b) {
  const c = el("button", "g-banner");
  c.type = "button";
  if (b.art && b.art.banner) {
    const img = document.createElement("img");
    img.className = "g-banner-img";
    img.alt = b.name;
    img.loading = "lazy";
    // ?w=640 — the card is ~320 CSS px, so this is the 2x it actually paints. The
    // master is a 2048x1024 PNG of 2-3 MB; downloading that and letting the browser
    // shrink it looks blurred.
    // Extension-less: the route serves the .png master or the .webp, whichever this
    // banner was saved as, so the page never has to know the format.
    img.src = `/gachaart/${b.gacha_id}/banner?w=640`;
    img.addEventListener("error", () => img.replaceWith(el("div", "g-banner-noart", b.name)));
    c.append(img);
  } else {
    c.append(el("div", "g-banner-noart", b.name));
  }

  const meta = el("div", "g-banner-meta");
  meta.append(
    el("div", "g-banner-name", b.name),
    el("div", "g-banner-dates",
      `${period(b) || "—"}${b.is_live_last_sync ? " · live" : ""}`)
  );

  const line = el("div", "g-banner-stats");
  line.append(el("span", "g-pill", `${num(b.pulls)} pulls`));
  for (const r of RAR) {
    const got = b.by_rarity[r] || 0;
    const exp = b.expected_by_rarity ? b.expected_by_rarity[r] : null;
    const s = el("span", "g-pill");
    s.style.borderColor = COL[r];
    const dot = el("i", "g-dot");
    dot.style.background = COL[r];
    s.append(dot, stars(r, true), el("span", "g-pill-n", `${got}${exp != null ? ` / ${exp.toFixed(1)}` : ""}`));
    line.append(s);
  }
  // The pity counter: pulls since the last multi-draw group that produced a ★5
  // (the group is the unit — order inside one multi-draw is unknown). Only shown
  // where it means something: a rate-rolled banner that has pulls at all.
  if (b.pulls > 0 && !b.excluded_from_rates && typeof b.pulls_since_last_5 === "number") {
    const p = el("span", "g-pill g-pity", `${num(b.pulls_since_last_5)} since last ★5`);
    p.title = "Pulls after the last multi-draw that contained a ★5 on this banner";
    line.append(p);
  }
  // A closed banner: the store holds its pulls (attributed through banner_aliases.json)
  // but the server will not serve rates for a banner that has ended, so there is no
  // expectation to draw. Both facts, said once.
  if (b.is_alias) line.append(el("span", "g-flag", "closed · rates not saved"));
  else if (!b.has_rates) line.append(el("span", "g-flag", "no rate table saved"));
  if (b.excluded_from_rates) {
    line.append(el("span", "g-pill g-dim", `${b.kind} · not in the comparison`));
  }
  if (b.counters && b.counters.gacha_point != null) {
    line.append(el("span", "g-pill g-dim", `points ${num(b.counters.gacha_point)}`));
  }
  meta.append(line);
  c.append(meta);
  c.addEventListener("click", () => openBanner(b));
  return c;
}

function openBanner(b) {
  const dlg = document.getElementById("gachadlg");
  const body = document.getElementById("gd-body");
  if (!dlg || !body) return;
  document.getElementById("gd-title").textContent = `${b.name} — ${num(b.pulls)} pulls`;
  body.replaceChildren();

  if (b.art && b.art.banner && b.gacha_id) {
    const im = document.createElement("img");
    im.className = "gd-hero";
    im.alt = b.name;
    im.src = `/gachaart/${b.gacha_id}/banner?w=1280`;
    im.addEventListener("error", () => im.remove());
    body.append(im);
  }

  const facts = el("div", "gd-facts");
  const per = period(b);
  if (per) facts.append(el("span", "g-pill g-dim", per));
  if (b.is_alias) facts.append(el("span", "g-flag", "closed · rates not saved"));
  else if (b.has_rates === false) facts.append(el("span", "g-flag", "no rate table saved — no expectation to compare against"));
  if (b.excluded_from_rates) {
    facts.append(el("span", "g-pill g-dim",
      `${b.kind} pulls — tracked, but not compared against a rate table`));
  }
  if (b.counters && b.counters.gacha_point != null) {
    facts.append(el("span", "g-pill g-dim", `${num(b.counters.gacha_point)} gacha points`));
  }
  if (b.counters && b.counters.drawn_count_total != null) {
    facts.append(el("span", "g-pill g-dim", `${num(b.counters.drawn_count_total)} draws counted by the game`));
  }
  if (facts.childElementCount) body.append(facts);

  let any = false;
  for (const r of RAR) {
    const cards = (b.cards || []).filter((c) => c.rarity === r);
    if (!cards.length) continue;
    any = true;
    cards.sort((a, z) => (z.is_pickup - a.is_pickup) || (z.count - a.count) || a.card_id.localeCompare(z.card_id));
    const sec = el("div", "gd-sec");
    const h = el("h4", "gd-sec-h");
    const dot = el("i", "g-dot");
    dot.style.background = COL[r];
    h.append(dot, stars(r), el("span", "gd-sec-n", `· ${cards.reduce((a, c) => a + c.count, 0)}`));
    sec.append(h);
    const grid = el("div", "gd-grid");
    for (const c of cards) {
      const t = el("div", "gd-card" + (c.is_pickup ? " pickup" : ""));
      const im = document.createElement("img");
      // EAGER, deliberately. `loading="lazy"` inside a <dialog> that has only just been
      // shown resolves against a zero-height viewport for the images below the fold, so
      // the first open of a big banner paints empty tiles: the browser never asks for them.
      im.loading = "eager";
      im.decoding = "async";
      im.src = `/cardart/thumb/${c.card_id}?w=96`;
      im.alt = c.card_id;
      im.addEventListener("error", () => {
        console.warn("gacha: thumb failed", im.src);
        im.replaceWith(el("div", "gd-noimg", c.card_id));
      });
      t.append(im, el("span", "gd-count", `×${c.count}`));
      if (c.is_pickup) t.append(el("span", "gd-pickup", "pickup"));
      t.title = c.card_id +
        (c.rate_ppm != null ? ` · rate ${(c.rate_ppm / 1e5).toFixed(3)}%` : " · no saved rate") +
        (c.is_pickup ? " · pickup" : "");
      grid.append(t);
    }
    sec.append(grid);
    body.append(sec);
  }
  if (!any) body.append(el("p", "note", "No cards recorded for this banner yet."));
  dlg.showModal();
}

// --- pulls whose banner was not identified -----------------------------------
//
// These are real pulls the store kept, from a result screen whose gacha_id the sync
// could not tie to a banner (a ticket draw, or a banner that had already ended when
// the store was first written). They are NOT dropped and they are NOT folded into a
// banner they might not belong to — they are shown as what they are.

function unidentified(v) {
  const wrap = el("div", "g-unid-list");
  for (const u of v.unidentified) {
    const row = el("button", "g-unid");
    row.type = "button";
    const head = el("div", "g-unid-head");
    head.append(el("span", "g-unid-name", u.gacha_name || "(no name)"));
    head.append(el("span", "g-pill", `${num(u.pulls)} pulls`));
    row.append(head);

    const line = el("div", "g-banner-stats");
    for (const r of RAR) {
      const got = u.by_rarity[r] || 0;
      if (!got) continue;
      const s = el("span", "g-pill");
      s.style.borderColor = COL[r];
      const dot = el("i", "g-dot");
      dot.style.background = COL[r];
      s.append(dot, stars(r, true), el("span", "g-pill-n", String(got)));
      line.append(s);
    }
    line.append(el("span", "g-flag", "no banner matched — no rates to compare"));
    row.append(line);

    row.addEventListener("click", () =>
      openBanner({
        name: u.gacha_name || "(no name)",
        gacha_id: null,
        pulls: u.pulls,
        by_rarity: u.by_rarity,
        expected_by_rarity: {},
        cards: u.cards || [],
        art: {},
        has_rates: false,
        is_alias: false,
        excluded_from_rates: false,
        counters: null,
        start_time: null,
        end_time: null,
      })
    );
    wrap.append(row);
  }
  return wrap;
}

// --- owned before tracking started -------------------------------------------
//
// The account holds cards the pull history never mentions: first copies acquired
// before the tracked window, and the tutorial's guaranteed ★5. They are shown because
// the collection shows them and the tracker should agree — and marked,
// because a tutorial card is NOT a pull and must never enter the rate comparison.

function ownedNotTracked(v) {
  const box = el("div", "g-chartbox");
  box.append(el("h4", "g-ch-t", "Owned before tracking started"));
  if (!v.tutorial) {
    box.append(el("p", "note",
      "This needs the account snapshot. Press “Refresh account” on the Collection tab " +
      "and this list fills in — nothing here is guessed from the pull history alone."));
    return box;
  }
  const rows = v.tutorial.owned_not_tracked || [];
  if (!rows.length) {
    box.append(el("p", "g-cap", "Every owned card appears in the tracked pulls."));
    return box;
  }
  const tut = rows.filter((r) => r.is_tutorial).length;
  box.append(el("p", "g-cap",
    `${num(rows.length)} owned cards are not in the tracked pulls — acquired before the ` +
    `window, or from a source the history does not report` +
    (tut ? `. ${num(tut)} came from the tutorial's guaranteed pull and count for nothing here.` : ".")));

  const grid = el("div", "g-own-grid");
  for (const c of rows) {
    const t = el("div", "g-own" + (c.is_tutorial ? " tut" : ""));
    const im = document.createElement("img");
    im.loading = "eager";
    im.decoding = "async";
    im.src = `/cardart/thumb/${c.card_id}?w=96`;
    im.alt = c.card_id;
    im.addEventListener("error", () => {
      console.warn("gacha: thumb failed", im.src);
      im.replaceWith(el("div", "gd-noimg", c.card_id));
    });
    const foot = el("div", "g-own-foot");
    foot.append(stars(c.rarity), el("span", "g-own-day", day(c.acquired)));
    t.append(im, foot);
    if (c.is_tutorial) t.append(el("span", "g-own-tag", "tutorial · not counted"));
    t.title = `${c.card_id} · acquired ${day(c.acquired)}` +
      (c.is_tutorial ? " · tutorial, not counted" : "");
    grid.append(t);
  }
  box.append(grid);
  return box;
}

// --- sync --------------------------------------------------------------------

async function doSync(btn, status, body) {
  btn.disabled = true;
  status.textContent = "syncing… (login + 2 reads + rate tables; watch the server console)";
  try {
    const r = await fetch("/api/gacha/sync", { method: "POST" });
    // The route answers 500 {"error": "..."} when the sync could not even run. Reading
    // straight through to `j.returncode` printed "sync exit undefined" and reloaded the
    // view as if nothing were wrong — a failure that looked like a no-op.
    let j = null;
    try { j = await r.json(); } catch { j = null; }
    if (!r.ok || !j || j.error) {
      status.textContent = "sync failed: " + ((j && j.error) || `HTTP ${r.status} ${r.statusText}`);
      return;
    }
    const s = j.summary;
    status.textContent = s
      ? `sync ok · +${s.pulls} pulls · +${s.banners} banners · ${s.rates} rate tables · exit ${j.returncode}`
        + (s.possible_gap
          ? " · ⚠ possible gap: the wire only serves the newest 100 draws and all of them were new"
          : "")
      : `sync exit ${j.returncode} — no summary; see console`;
    await load(body, status.textContent);
  } catch (e) {
    status.textContent = "sync failed: " + e;
  } finally {
    btn.disabled = false;
  }
}

// --- export ------------------------------------------------------------------
//
// The file is written by tools/recon/proxy/gacha_export.py (this package writes
// nothing); the route hands back the path it printed. No login is spent — the
// export reads the store, so pressing this is always safe.

async function doExport(btn, status) {
  btn.disabled = true;
  status.textContent = "exporting…";
  try {
    const r = await fetch("/api/gacha/export", { method: "POST" });
    let j = null;
    try { j = await r.json(); } catch { j = null; }
    if (!r.ok || !j || j.error) {
      status.textContent = "export failed: " + ((j && j.error) || `HTTP ${r.status} ${r.statusText}`);
      return;
    }
    status.textContent = j.path
      ? `wrote ${j.path}`
      : `export exit ${j.returncode} — see console`;
  } catch (e) {
    status.textContent = "export failed: " + e;
  } finally {
    btn.disabled = false;
  }
}

// --- render ------------------------------------------------------------------

function render(v, body, keepStatus) {
  body.replaceChildren();

  const head = el("div", "g-head");
  const btn = el("button", "btn g-sync", "Sync");
  btn.type = "button";
  const synced = stamp(v.last_sync);
  // The age matters more than the date: the wire serves only the newest 100 draws, so
  // a store left unsynced can lose history. Refresh now syncs too, but if neither has
  // run in days, say so where the user is looking instead of printing a quiet date.
  const ageDays = secs(v.last_sync) != null
    ? Math.floor((Date.now() / 1000 - secs(v.last_sync)) / 86400) : null;
  const ageTxt = ageDays == null ? "" : ageDays === 0 ? " (today)" : ` (${ageDays}d ago)`;
  const status = el("span", "g-status", keepStatus || (synced ? `last sync ${synced}${ageTxt}` : "never synced"));
  if (ageDays != null && ageDays >= 3 && !keepStatus) status.classList.add("g-stale");
  btn.addEventListener("click", () => doSync(btn, status, body));
  const exp = el("button", "btn ghost g-export", "Export HTML");
  exp.type = "button";
  exp.title = "Write one self-contained HTML file of this view under dumps/ (no login)";
  exp.addEventListener("click", () => doExport(exp, status));
  head.append(btn, exp, status);
  body.append(head);

  if (ageDays != null && ageDays >= 3) {
    body.append(el("p", "g-flag",
      `Last sync was ${ageDays} days ago. The game only serves the newest 100 draws, so ` +
      "pulls can age out of that window unrecorded — sync now (the main Refresh button " +
      "also syncs pulls since 2026-08-27)."));
  }

  // The one hand-maintained file in the store. If it did not parse, every alias silently
  // stopped working — say so where the user is looking rather than let banners quietly
  // fall back to "unidentified".
  if (v.aliases_error) {
    body.append(el("p", "g-flag",
      "banner_aliases.json could not be read: " + v.aliases_error));
  }

  if (!v.totals || !v.totals.pulls) {
    body.append(el("p", "note",
      "No pulls in the store yet. Press Sync to read this account's gacha history."));
    if (v.store_dir) body.append(el("p", "g-cap", "store: " + v.store_dir));
    return;
  }

  // Read once and handed to both: the tiles' small line and the chart must agree on
  // which pulls the expectation covers, and the server is the one that decided.
  const cmp = comparisonOf(v);
  body.append(tiles(v, cmp));
  const charts = el("div", "g-charts");
  charts.append(ratesChart(v, cmp), timeline(v));
  body.append(charts);

  body.append(el("h3", "g-h", "Banners"));
  if (v.banners.length) {
    const gal = el("div", "g-gallery");
    for (const b of v.banners) gal.append(bannerCard(b));
    body.append(gal);
  } else {
    body.append(el("p", "note", "No banner was identified for any pull yet."));
  }

  if (v.unidentified && v.unidentified.length) {
    body.append(el("h3", "g-h", "Pulls whose banner was not identified"));
    body.append(el("p", "g-cap",
      "A pull whose banner has closed can still be attributed by hand: add its name to " +
      "data/gacha/banner_aliases.json and it moves up into Banners, art and all."));
    body.append(unidentified(v));
  }

  body.append(el("h3", "g-h", "The rest of the collection"));
  body.append(ownedNotTracked(v));

  if (v.store_dir) body.append(el("p", "g-cap", "store: " + v.store_dir));
}

async function load(body, keepStatus) {
  let view;
  try {
    view = await fetchView();
  } catch (e) {
    body.replaceChildren(el("p", "note", "Could not reach /api/gacha: " + e));
    return;
  }
  render(view, body, keepStatus);
}

export function mountGacha() {
  const wrap = document.getElementById("gwrap");
  const body = document.getElementById("gbody");
  if (!wrap || !body) return;
  const dlg = document.getElementById("gachadlg");
  if (dlg) {
    const close = dlg.querySelector(".gd-close");
    if (close) close.addEventListener("click", () => dlg.close());
    // Backdrop click closes it (Escape is the platform's already). The <dialog> element
    // IS the backdrop, so a click whose target is the dialog itself — never the panel
    // inside it — is a click outside the content.
    dlg.addEventListener("click", (e) => { if (e.target === dlg) dlg.close(); });
  }
  let ever = false;
  wrap.addEventListener("toggle", function () {
    if (!this.open || ever) return;
    ever = true;
    load(body);
  });
}
