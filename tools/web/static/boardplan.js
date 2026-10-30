// boardplan.js — the "Board plan" section: one collapsible board tree per
// character, shown inline under a Solve result.
//
// It reads a board-export/v0 doc (from /api/boardplan or /api/board-plan-fast):
// each character's nodes/sockets, the branch palette, and — when the entry was
// built with --plan — the planner's real to_buy/to_sell picks. It renders nothing
// it computed: every number is the export's.

import { renderBoardTree } from "/boardtree.js";

// "To buy" is the planner's real count when the entry carries a plan, else the
// gain>0 heuristic; "to sell" only exists with a plan (the respec's refunds).
const buyCount = (c) => c.plan
  ? (c.plan.to_buy || []).length
  : (c.nodes || []).filter((n) => n.state === "buyable" && typeof n.gain === "number" && n.gain > 0).length;
const sellCount = (c) => ((c.plan && c.plan.to_sell) || []).length;
// TOTAL nodes on the FINAL board (owned + bought) — the developed board's size. The
// list sorts by this so the biggest boards come first: a leader who owns a lot but
// only buys a few new nodes still ranks high (sorting by buy-count alone would hide her).
const totalCount = (c) => c.plan && c.plan.optimised_nodes
  ? c.plan.optimised_nodes.length
  : (c.owned_count ?? 0) + buyCount(c);

// The Connect-socket recommendations: which owned card to place on which board's
// socket, and the exact score that placement adds. `sockets_optimised` is the
// team-aware socket optimiser's result (best card per socket, on the OPTIMISED
// board). It shows names + the score each pick adds — the "which card, how much"
// the plan is really answering.
function socketSection(doc) {
  const s = doc.sockets_optimised;
  const picks = (s && s.placements) || [];
  if (!picks.length) return null;

  const nameByChr = {};
  for (const c of doc.characters || []) nameByChr[c.chr] = c.name || c.chr;
  const chrOf = (cardId) => "chr-" + String(cardId).split("-")[1];
  const ownerName = (cardId) => nameByChr[chrOf(cardId)] || cardId;

  const section = document.createElement("section");
  section.className = "opt-sockets";
  const h4 = document.createElement("h4");
  h4.textContent = "Connect sockets";
  section.append(h4);

  const lede = document.createElement("p");
  lede.className = "say";
  const gain = s.gain_vs_current;
  lede.textContent = s.recommend === "optimised" && typeof gain === "number"
    ? `Re-placing the Connect cards adds ${gain.toLocaleString("en-US", { maximumFractionDigits: 0 })} ` +
      `over the current placement. Each card boosts the tiles in its own shape on that board.`
    : "The current Connect placement is already best for this team.";
  section.append(lede);

  const list = document.createElement("ul");
  list.className = "opt-socket-list";
  for (const p of picks) {
    const li = document.createElement("li");
    // `host_name` is set for EVERY pick (incl. bench picks whose board isn't
    // rendered, so `nameByChr` — built from the shown characters — lacks them).
    const host = p.host_name || nameByChr[p.chr] || p.chr;
    // The card by its READABLE name ("Crimson Captain"), with its member for
    // context — never the raw `card-00031-...` id. Falls back to the member if the
    // catalogue lacks a title.
    const cardName = p.card_name || (ownerName(p.card) + "'s card");
    const cardMember = p.card_member ? `${p.card_member} · ` : "";
    const d = p.detail || {};
    // "×N.NN" is the exact Connect multiplier the game applies; "buffs K" is how
    // many board tiles that card's shape covers on this socket.
    const mult = typeof d.multiplier === "number"
      ? `<span class="opt-socket-mult">×${d.multiplier}</span>` : "";
    const buffs = (d.buffed_nodes && d.buffed_nodes.length)
      ? `<span class="opt-socket-buffs">buffs ${d.buffed_nodes.length}</span>` : "";
    li.innerHTML =
      `<span class="opt-socket-host">${host}</span>` +
      `<span class="opt-socket-slot">${p.socket}</span>` +
      `<span class="opt-socket-card">${cardMember}${cardName}</span>` +
      mult + buffs +
      `<span class="opt-socket-val">+${(p.isolated_value || 0).toLocaleString("en-US", { maximumFractionDigits: 0 })}</span>`;
    li.title = [`${cardMember}${cardName} on ${host}'s ${p.socket}`, d.effect,
                `score +${p.isolated_value}`].filter(Boolean).join("\n");
    // The 512px card art next to the pick; a 404 removes the <img> and the row
    // shows the name alone.
    const thumb = document.createElement("img");
    thumb.className = "opt-socket-thumb";
    thumb.loading = "lazy";
    thumb.alt = "";
    thumb.src = `/cardart/${encodeURIComponent(p.card)}?w=160`;
    thumb.addEventListener("error", () => thumb.remove());
    li.prepend(thumb);
    // A round face icon for the HOST board, so you can see whose tree the socket
    // sits on at a glance (the game's mini icon, or the member_icons fallback).
    const face = document.createElement("img");
    face.className = "opt-socket-face";
    face.loading = "lazy";
    face.alt = "";
    face.src = `/chricon/${encodeURIComponent(p.chr)}`;
    face.addEventListener("error", () => face.remove());
    li.prepend(face);
    list.append(li);
  }
  section.append(list);
  return section;
}

/** Build the Board-plan <section> for a board-export/v0 `doc`. Trees render
 *  lazily on first expand — 54 SVGs at once is needless work. Ordered by how
 *  much there is to do (buy count, then owned), so the boards worth acting on
 *  are on top. */
export function boardPlanSection(doc) {
  const section = document.createElement("section");
  section.className = "opt-plan";
  const h3 = document.createElement("h3");
  h3.textContent = "Board plan";
  section.append(h3);

  // Which resource assumptions this plan was costed under (the two orthogonal
  // toggles). Only present on the affordable tree_optimise plan, not the fast
  // points-only estimate — so the reader always knows what they're looking at.
  if (doc.plan_char_level || doc.plan_resources) {
    const badge = document.createElement("p");
    badge.className = "say plan-assumptions";
    const lvl = doc.plan_char_level === "max"
      ? "Max level (50) points"
      : "My character-level points";
    const res = doc.plan_resources === "infinite"
      ? "infinite cubes/hexas (the ceiling)"
      : "my cube/hexa inventory";
    badge.textContent = `Costed with: ${lvl} · ${res}.`;
    section.append(badge);
  }

  const sockets = socketSection(doc);
  if (sockets) section.append(sockets);

  const lede = document.createElement("p");
  lede.className = "say";
  const planned = (doc.characters || []).some((c) => c.plan);
  lede.textContent = planned
    ? "Each tree is the FINAL board to build — filled nodes are what you'll have. " +
      "A thin gold ring marks a node that is new (buy it); a faint dashed one is " +
      "refunded by the respec (sold). Click a character to open its tree."
    : "One board per character. Filled = owned, bright outline = buyable next, " +
      "gold ring = worth buying (positive score gain). Click a character to open " +
      "its tree. Rebuild the entry with --plan for the optimiser's exact picks.";
  section.append(lede);

  const characters = [...(doc.characters || [])].sort(
    (a, b) => totalCount(b) - totalCount(a) || buyCount(b) - buyCount(a)
  );

  for (const character of characters) {
    const details = document.createElement("details");
    details.className = "opt-board";
    const summary = document.createElement("summary");
    summary.className = "opt-board-head";
    const buy = buyCount(character);
    const sell = sellCount(character);
    const points = character.points || {};
    // With a plan, show the DEVELOPED board's cost against the level's budget
    // (e.g. "248/361") — at max that budget is 361, not the real level's
    // points, so "84 to buy" does not read against an impossible 83. Without a
    // plan, fall back to the current board's spend vs the real earned points.
    const plan = character.plan;
    const cost = plan && plan.point_cost != null ? plan.point_cost : (points.spent_on_owned ?? 0);
    const budget = plan && plan.point_budget != null ? plan.point_budget : points.granted;
    summary.innerHTML =
      `<span class="opt-board-name">${character.name || character.chr}</span>` +
      `<span class="opt-chip">${character.owned_count ?? 0} owned</span>` +
      (buy ? `<span class="opt-chip opt-chip-buy">${buy} to buy</span>` : "") +
      (sell ? `<span class="opt-chip opt-chip-sell">${sell} to sell</span>` : "") +
      `<span class="opt-board-pts">${cost}${budget != null ? " / " + budget : ""} pt</span>`;
    details.append(summary);
    const body = document.createElement("div");
    body.className = "opt-board-canvas";
    details.append(body);
    let rendered = false;
    details.addEventListener("toggle", () => {
      if (details.open && !rendered) {
        rendered = true;
        renderBoardTree(body, character, doc.branches || {});
      }
    });
    section.append(details);
  }
  return section;
}

// The Exact plan (HiGHS / tree_optimise) rendered UNDER a row's
// developed board must read as a DIFF against that board — never as a second
// headline. `boardState` is the row's own `board_state` ({chr: {nodes, slots}});
// the doc's per-character `plan.optimised_nodes` is the exact plan's board.
export function planDiffNote(doc, boardState) {
  if (!boardState || !doc || !Array.isArray(doc.characters)) return null;
  const parts = [];
  let total = 0;
  for (const c of doc.characters) {
    const st = boardState[c.chr];
    if (!st || !c.plan || !Array.isArray(c.plan.optimised_nodes)) continue;
    const dev = new Set(st.nodes || []);
    const exact = new Set(c.plan.optimised_nodes);
    const added = [...exact].filter((n) => !dev.has(n)).length;
    const removed = [...dev].filter((n) => !exact.has(n)).length;
    if (added || removed) {
      total += added + removed;
      parts.push(`${c.name || c.chr}: ${added + removed} node${added + removed === 1 ? "" : "s"}` +
                 ` (+${added} −${removed})`);
    }
  }
  const p = document.createElement("p");
  p.className = "note plan-diff";
  const engine = doc.engine === "solver" ? "Exact plan (HiGHS)" : "This plan";
  const level = doc.plan_char_level === "max" ? "max level (361 granted at Lv 50)" : "current level";
  const res = doc.plan_resources === "infinite" ? "infinite materials" : "your inventory";
  p.textContent = total
    ? `${engine} vs the developed board this row was scored on: differs on ${total} node${total === 1 ? "" : "s"} — ` +
      parts.join("; ") + `. Costed at ${level}, ${res}; the row's headline does not change.`
    : `${engine} chooses exactly the developed board this row was scored on (costed at ${level}, ${res}).`;
  return p;
}
