// boardstate.js — the developed board of ONE ranked team, per character
// (REDESIGN-PLAN.md §2.5). Principle: one board per row — the one it was scored
// on (`team.board_state`); everything here is the server's own
// `board_plan_summary` for that state (same instant, same basis) and, on
// expand, the `/api/board-doc` overlay of that same state drawn by boardtree.js.
// Nothing is computed client-side against a cached export.
import { el } from "/dom.js";
import { renderBoardTree } from "/boardtree.js";

const num = (n) => (typeof n === "number" ? n.toLocaleString("en-US") : String(n ?? "—"));

function budgetLabel(summary) {
  if (summary.budget_kind === "full_tree") return "full tree (453 flat, what-if)";
  if (summary.budget_kind === "flat") return "flat per-character cap";
  return "real granted points";
}

// `team`: a ranked team carrying `board_plan_summary` + `board_state`;
// `ctx`: { cards (faces by id), basis, budget ("real" | int), song }.
// `opts.whatif`: this section IS the labelled what-if (the per-team greedy
// re-develop) — title and lede say so; no nested what-if is offered.
export function boardSection(team, ctx, opts) {
  const isWhatif = !!(opts && opts.whatif);
  const summary = team.board_plan_summary;
  const section = el("section", "bstate" + (isWhatif ? " bstate-whatif" : ""));
  const planCache = summary && summary.leaf === "plan_cache";
  const title = isWhatif
    ? "What-if board — greedy re-develop for this team (NOT the one this score used)"
    : planCache
      ? "Fixed-plan board — the one this score used (the objective)"
      : "Developed board — the one this score used";
  const h3 = el("h3", null, title);
  section.append(h3);
  if (!summary || !Array.isArray(summary.per_character)) {
    section.append(el("p", "note",
      "This row carries no developed board (boards were off or as captured for this run)."));
    return section;
  }
  const kind = summary.develop_only ? "develop (adds only)" : "respec plan";
  const head = el("p", "lede sm");
  head.innerHTML =
    `Board off <b>${num(summary.board_off)}</b> → ${isWhatif ? "re-developed" : "developed"} <b>${num(summary.developed)}</b> ` +
    `(${summary.gain_pct >= 0 ? "+" : ""}${summary.gain_pct} %) · ${budgetLabel(summary)} · ${kind}` +
    (summary.metric === "song_est" ? " · per-song score" : " · Unit Score") +
    (planCache ? " · fixed plan per character (plan-cache leaf, OBJECTIVE-v2 §3)"
      : isWhatif ? " · per-team greedy develop (what-if)" : "");
  section.append(head);

  // materials for the buys (item id -> qty), when the price table was readable
  if (summary.materials && Object.keys(summary.materials).length) {
    const mats = el("div", "bstate-mats");
    for (const [item, qty] of Object.entries(summary.materials)) {
      const chip = el("span", "opt-chip", `${num(qty)}`);
      const img = document.createElement("img");
      img.className = "bstate-mat-icon";
      img.src = "/itemicon/" + encodeURIComponent(item);
      img.alt = item;
      img.title = item;
      img.addEventListener("error", () => img.replaceWith(el("span", "mono", item + " ")));
      chip.prepend(img);
      mats.append(chip);
    }
    const lab = el("span", "bstate-mats-lab", "materials for the buys:");
    mats.prepend(lab);
    section.append(mats);
  }

  // controls: show only changes (default on) + copy as text
  const controls = el("div", "bstate-controls");
  const onlyChanges = document.createElement("label");
  const cb = document.createElement("input");
  cb.type = "checkbox";
  cb.checked = true;
  onlyChanges.append(cb, document.createTextNode(" show only characters with changes"));
  const copy = document.createElement("button");
  copy.type = "button";
  copy.className = "planbtn ghost";
  copy.textContent = "Copy plan as text";
  copy.addEventListener("click", async () => {
    const text = planAsText(team, summary, ctx);
    try { await navigator.clipboard.writeText(text); copy.textContent = "Copied ✓"; }
    catch (_) { copy.textContent = "Copy failed — select the rows instead"; }
    setTimeout(() => { copy.textContent = "Copy plan as text"; }, 2000);
  });
  controls.append(onlyChanges, copy);
  section.append(controls);

  const list = el("div", "bstate-list");
  section.append(list);
  const rows = [];
  for (const c of summary.per_character) {
    const changed = (c.buy || []).length || (c.sell || []).length;
    const row = el("details", "opt-board bstate-row");
    row.dataset.changed = changed ? "1" : "0";
    const sum = el("summary", "opt-board-head");
    const face = document.createElement("img");
    face.className = "bstate-avatar";
    face.src = "/chricon/" + encodeURIComponent(c.chr);
    face.alt = "";
    face.addEventListener("error", () => face.remove());
    sum.append(face);
    sum.append(el("span", "opt-board-name", c.name || c.chr));
    sum.append(el("span", "opt-chip", `${(c.keep || []).length + (c.buy || []).length} nodes`));
    if ((c.buy || []).length) sum.append(el("span", "opt-chip opt-chip-buy", `${c.buy.length} to buy`));
    if ((c.sell || []).length) sum.append(el("span", "opt-chip opt-chip-sell", `${c.sell.length} to sell`));
    if (!changed) sum.append(el("span", "opt-chip", "unchanged"));
    for (const s of c.sockets || []) {
      const chip = el("span", "opt-chip bstate-socket");
      const art = document.createElement("img");
      art.className = "bstate-socket-art";
      art.src = "/cardart/thumb/" + encodeURIComponent(s.card_id) + "?w=96";
      art.alt = "";
      art.addEventListener("error", () => art.remove());
      const cardName = (ctx.cards && ctx.cards[s.card_id] && ctx.cards[s.card_id].member) || s.card_id;
      chip.append(art, document.createTextNode(` ${s.slot}: ${cardName}`));
      chip.title = `Connect socket ${s.slot} ← ${s.card_id}`;
      sum.append(chip);
    }
    const pts = el("span", "opt-board-pts",
      `${num(c.points_used)} / ${num(c.points_max)} pt`);
    pts.title = `points used by the developed board / ${budgetLabel(summary)}`;
    sum.append(pts);
    row.append(sum);
    const canvas = el("div", "opt-board-canvas");
    row.append(canvas);
    let drawn = false;
    row.addEventListener("toggle", async () => {
      if (!row.open || drawn) return;
      drawn = true;
      canvas.textContent = "Loading the board…";
      try {
        const doc = await fetchDoc(team, ctx);
        const character = (doc.characters || []).find((x) => x.chr === c.chr);
        canvas.textContent = "";
        if (!character) { canvas.append(el("p", "note", "no board geometry for this character")); return; }
        renderBoardTree(canvas, character, doc.branches || {});
      } catch (error) {
        canvas.textContent = "";
        canvas.append(el("p", "note", `Board doc failed: ${error.message || error}`));
      }
    });
    rows.push(row);
    list.append(row);
  }
  const applyFilter = () => {
    for (const row of rows) row.hidden = cb.checked && row.dataset.changed === "0";
    const hidden = rows.filter((r) => r.hidden).length;
    filterNote.textContent = cb.checked && hidden
      ? `${hidden} unchanged character${hidden === 1 ? "" : "s"} hidden`
      : "";
  };
  const filterNote = el("p", "note");
  section.append(filterNote);
  cb.addEventListener("change", applyFilter);
  applyFilter();

  // The labelled alternative (OBJECTIVE-v2 D2): the per-team greedy
  // re-develop's board, folded, named as a what-if, with its delta against
  // the objective. Rendered through this same function on a view of the team
  // whose board fields are the what-if's (same renderer, same instant).
  if (!isWhatif && team.board_plan_summary_develop && team.board_state_develop) {
    const wi = team.develop_whatif || {};
    const devValue = typeof wi.song_est === "number" ? wi.song_est
      : typeof wi.across_score === "number" ? wi.across_score : null;
    const pct = typeof wi.delta_pct === "number" ? wi.delta_pct : 0;
    const fold = el("details", "bstate-whatif-fold");
    const sum = el("summary", null,
      `what-if: greedy re-develop for this team` +
      (devValue !== null ? ` → ${num(devValue)}` : "") +
      ` (${pct >= 0 ? "+" : ""}${pct.toFixed(2)} %)` +
      ` — a different board, not the one this row is scored on`);
    fold.append(sum);
    const view = Object.assign({}, team, {
      board_plan_summary: team.board_plan_summary_develop,
      board_state: team.board_state_develop,
      board_plan_summary_develop: undefined, board_state_develop: undefined,
      develop_whatif: undefined,
    });
    let drawnWhatif = false;
    fold.addEventListener("toggle", () => {
      if (!fold.open || drawnWhatif) return;
      drawnWhatif = true;
      fold.append(boardSection(view, ctx, { whatif: true }));
    });
    section.append(fold);
  }
  return section;
}

// The /api/board-doc fetch is shared by every character row of the team.
const docCache = new WeakMap();
async function fetchDoc(team, ctx) {
  if (docCache.has(team)) return docCache.get(team);
  const p = (async () => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 120000);
    try {
      const r = await fetch("/api/board-doc", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          board_state: team.board_state, cards: [team.leader_id, ...(team.member_ids || [])],
          basis: ctx.basis || "account", budget: ctx.budget == null ? "real" : ctx.budget,
        }),
        signal: controller.signal,
      });
      if (!r.ok) {
        let msg = `HTTP ${r.status}`;
        try { const e = await r.json(); if (e.error) msg = e.error; } catch (_) {}
        throw new Error(msg);
      }
      return await r.json();
    } finally {
      clearTimeout(timer);
    }
  })();
  docCache.set(team, p);
  return p;
}

function planAsText(team, summary, ctx) {
  const lines = [];
  lines.push(`Developed board for ${team.leader_id} + ${(team.member_ids || []).join(", ")}`);
  lines.push(`board off ${num(summary.board_off)} -> developed ${num(summary.developed)} ` +
             `(${summary.gain_pct} %) · ${budgetLabel(summary)}`);
  for (const c of summary.per_character) {
    lines.push(`${c.name || c.chr} (${c.chr}): ${num(c.points_used)}/${num(c.points_max)} pt`);
    if ((c.buy || []).length) lines.push(`  buy:  ${c.buy.join(", ")}`);
    if ((c.sell || []).length) lines.push(`  sell: ${c.sell.join(", ")}`);
    if ((c.keep || []).length) lines.push(`  keep: ${c.keep.join(", ")}`);
    for (const s of c.sockets || []) lines.push(`  socket ${s.slot}: ${s.card_id}`);
  }
  if (summary.materials) {
    lines.push("materials: " + Object.entries(summary.materials).map(([k, v]) => `${k} x${v}`).join(", "));
  }
  return lines.join("\n");
}
