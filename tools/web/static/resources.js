// resources.js — the "Resources" view: everything the account holds, rendered in
// the GAME's own item-inventory style — the real item art on a soft green tile
// with a white count pill, so it reads like the in-game bag rather than a table.
//
// Two families, read from the captured account state (never computed here): the
// BOARD materials (cubes/hexas already spent on nodes + the unspent wallet, with a
// slim spent|wallet bar) and the UPGRADE currencies (beads/crystals per attribute,
// the S/M/L bundles as their own items with the real bottle art, hololium, lesson
// points, hologold). Served by /api/resources. Counts are shown at FULL precision
// (2,955 — never 3.0K), which is the point of reading them here. Absent is
// first-class — an empty capture shows the reason and the Refresh-account hint.

import { el } from "/dom.js";

const num = (n) => (typeof n === "number" ? n.toLocaleString("en-US") : "—");

// Branch (cube/hexa) and attribute (bead/crystal) palettes for the SVG fallback
// glyph — the game's own colour language, used only when an icon asset is missing.
const BRANCH = { Red: "#f2555a", Blue: "#2fb6f0", Green: "#37c98a", Yellow: "#f2b73c" };
const ATTR = { Cute: "#ff77b6", Pure: "#33cbc0", Happy: "#f6a93b" };

const cube = (c) =>
  `<svg viewBox="0 0 24 24" width="40" height="40"><rect x="4" y="4" width="16" height="16" rx="4" fill="${c}"/><rect x="4" y="4" width="16" height="7" rx="4" fill="#fff" opacity=".22"/></svg>`;
const hexa = (c) =>
  `<svg viewBox="0 0 24 24" width="40" height="40"><path d="M12 3l7 4v8l-7 4-7-4V7z" fill="${c}"/><path d="M12 3l7 4-7 4-7-4z" fill="#fff" opacity=".22"/></svg>`;
const bead = (c) =>
  `<svg viewBox="0 0 24 24" width="38" height="38"><circle cx="12" cy="12" r="8" fill="${c}"/><circle cx="9.5" cy="9.5" r="2.4" fill="#fff" opacity=".4"/></svg>`;
const crystal = (c) =>
  `<svg viewBox="0 0 24 24" width="38" height="38"><path d="M12 3l6 6-6 12-6-12z" fill="${c}"/><path d="M12 3l6 6H6z" fill="#fff" opacity=".28"/></svg>`;
const GENERIC = {
  Hololium: `<svg viewBox="0 0 24 24" width="38" height="38"><path d="M12 2l7 5-2.6 12H7.6L5 7z" fill="#b98bff"/><path d="M12 2l7 5-7 3-7-3z" fill="#fff" opacity=".3"/></svg>`,
  "Lesson Pt": `<svg viewBox="0 0 24 24" width="38" height="38"><path d="M12 3l2.6 5.6 6.1.7-4.5 4.2 1.2 6L12 16.9 6.6 19.5l1.2-6L3.3 9.3l6.1-.7z" fill="#5fb2ff"/></svg>`,
  Hologold: `<svg viewBox="0 0 24 24" width="38" height="38"><circle cx="12" cy="12" r="9" fill="#f2c94c"/><circle cx="12" cy="12" r="6" fill="none" stroke="#fff" stroke-width="1.4" opacity=".55"/></svg>`,
};

function glyphSvg(name) {
  const [attr] = name.split(" ");
  if (/Bundle/.test(name)) return bead(ATTR[attr] || "#8aa"); // beads/crystals bundle
  if (/Jar of Crystals/.test(name)) return crystal("#8aa");
  if (/Beads/.test(name)) return bead(ATTR[attr] || "#8aa");
  if (/Crystals/.test(name)) return crystal(ATTR[attr] || "#8aa");
  if (/Cube/.test(name)) return cube(BRANCH[attr] || "#8aa");
  if (/Hexa/.test(name)) return hexa(BRANCH[attr] || "#8aa");
  if (GENERIC[name]) return GENERIC[name];
  return `<svg viewBox="0 0 24 24" width="34" height="34"><circle cx="12" cy="12" r="8" fill="#8aa" opacity=".5"/></svg>`;
}

// The item art for a row: the game's own icon from /itemicon/<key>, where key is
// the row's explicit `icon` asset (bundles carry one, since their id doesn't
// reverse-map) or else its item_id. Falls back to a coloured glyph if it 404s.
function iconEl(row) {
  const holder = el("span", "gt-glyph");
  const key = (row && (row.icon || row.item_id)) || "";
  if (key) {
    const img = document.createElement("img");
    img.className = "gt-img";
    img.alt = "";
    img.loading = "lazy";
    img.src = "/itemicon/" + encodeURIComponent(key);
    img.addEventListener("error", () => { holder.innerHTML = glyphSvg(row.name); });
    holder.append(img);
  } else {
    holder.innerHTML = glyphSvg(row.name);
  }
  return holder;
}

// A tiny inventory-chest glyph, like the one in the game's count pill.
const CHEST =
  `<svg class="gt-chest" viewBox="0 0 24 24" width="15" height="15" aria-hidden="true">` +
  `<path fill="currentColor" d="M4 10a3 3 0 0 1 3-3h10a3 3 0 0 1 3 3v1H4v-1Zm0 3h16v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2v-4Zm7 0h2v3h-2v-3Z"/></svg>`;

// The shared game tile: the green art square with the item icon, a white count
// pill hugging its bottom edge, the name, and any extra rows (bar/legend/note).
function gameTile(row, count, extras) {
  const tile = el("div", "gt");
  const art = el("div", "gt-art");
  art.append(iconEl(row));
  tile.append(art);

  const pill = el("div", "gt-pill");
  pill.innerHTML = CHEST + `<span>${num(count)}</span>`;
  tile.append(pill);

  tile.append(el("div", "gt-name", row.name));
  for (const node of extras || []) if (node) tile.append(node);
  return tile;
}

// A board-material tile: the total in the pill, plus a slim invested|inventory
// bar and its legend so "what I've spent vs what's left" still reads at a glance.
function boardTile(m) {
  const denom = m.total || 1;
  const bar = el("div", "gt-bar");
  const spent = el("span", "gt-bar-spent");
  spent.style.width = ((m.spent / denom) * 100).toFixed(1) + "%";
  const wallet = el("span", "gt-bar-wallet");
  wallet.style.width = ((m.wallet / denom) * 100).toFixed(1) + "%";
  bar.append(spent, wallet);

  const legend = el("div", "gt-legend");
  legend.innerHTML =
    `<span><i class="gt-dot sp"></i>invested ${num(m.spent)}</span>` +
    `<span><i class="gt-dot wl"></i>inventory ${num(m.wallet)}</span>`;

  return gameTile(m, m.total, [bar, legend]);
}

// A currency tile: held count in the pill, plus the grant/use note.
function currencyTile(c) {
  const note = c.note ? el("div", "gt-note", c.note) : null;
  return gameTile(c, c.held, [note]);
}

// A section header row: the title, plus optional muted trailing notes (the board
// points chip, the capture time) kept on the same line to save vertical space.
function sectionHead(text, notes) {
  const h = el("div", "rs-sec-h");
  h.append(el("span", "rs-sec-t", text));
  for (const n of notes || []) if (n) h.append(n);
  return h;
}

function render(body, data) {
  body.textContent = "";

  if (!data || data.available === false) {
    body.append(
      Object.assign(el("p", "note"), {
        textContent:
          (data && data.reason) ||
          "No account capture to read. Pull it with Refresh account, then reopen this.",
      })
    );
    return;
  }

  const captured = data.captured_at
    ? Object.assign(el("span", "rs-meta"), { textContent: "captured " + data.captured_at })
    : null;

  if (Array.isArray(data.board_materials) && data.board_materials.length) {
    const notes = [];
    if (typeof data.board_points_spent === "number") {
      notes.push(
        Object.assign(el("span", "rs-points"), {
          innerHTML: `<b>${num(data.board_points_spent)}</b> board points invested`,
        })
      );
    }
    if (captured) notes.push(captured);
    body.append(sectionHead("Board materials", notes));
    const grid = el("div", "rs-grid");
    for (const m of data.board_materials) grid.append(boardTile(m));
    body.append(grid);
  }

  if (Array.isArray(data.currencies) && data.currencies.length) {
    const of = (g) => data.currencies.filter((c) => c.group === g);
    const gridOf = (rows) => {
      const grid = el("div", "rs-grid");
      for (const c of rows) grid.append(currencyTile(c));
      return grid;
    };

    // Beads & Crystals on one row, then a line for what opening every bundle
    // would add (bundles are attribute-selectable, so it's a free-choice pool).
    const beadsCrystals = [...of("beads"), ...of("crystals")];
    if (beadsCrystals.length) {
      body.append(sectionHead("Beads & Crystals"));
      body.append(gridOf(beadsCrystals));
      let beadsB = 0, crysB = 0;
      for (const b of of("bundle")) {
        const g = (b.grant_each || 0) * (b.held || 0);
        if (b.grant_kind === "crystals") crysB += g;
        else beadsB += g;
      }
      if (beadsB || crysB) {
        const parts = [];
        if (beadsB) parts.push(`<b>+${num(beadsB)}</b> beads`);
        if (crysB) parts.push(`<b>+${num(crysB)}</b> crystals`);
        body.append(
          Object.assign(el("p", "rs-hint"), {
            innerHTML:
              `Opening every bundle would add ${parts.join(" · ")} ` +
              `<span class="rs-hint-dim">(pick any attribute)</span>`,
          })
        );
      }
    }

    // Bundles & Currencies on one row.
    const bundlesCurrencies = [...of("bundle"), ...of("other")];
    if (bundlesCurrencies.length) {
      body.append(sectionHead("Bundles & Currencies"));
      body.append(gridOf(bundlesCurrencies));
    }
  }
}

async function load(body) {
  let data;
  try {
    const response = await fetch("/api/resources");
    data = await response.json();
  } catch (error) {
    data = { available: false, reason: "Could not reach /api/resources: " + error };
  }
  render(body, data);
}

export function mountResources() {
  const wrap = document.getElementById("rwrap");
  const body = document.getElementById("rbody");
  if (!wrap || !body) return;
  let everLoaded = false;
  wrap.addEventListener("toggle", function () {
    if (!this.open || everLoaded) return;
    everLoaded = true;
    load(body);
  });
}
