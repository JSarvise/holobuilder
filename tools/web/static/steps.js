// The Solve page as a sequence of steps, in the style of the community tool
// holodori.best: five numbered cards, each a collapsed header until the
// step before it has been answered, so the page opens on ONE question instead
// of twenty-nine controls.
//
//   1 What to build            the three goal cards (as before)
//   2 What to optimise for     the song / "across all songs", and the cached
//                              best team for that choice (twostage.js)
//   3 Options                  which cards, the pool, locked cards
//   4 Holomem boards           how each team's board is developed
//   5 Details                  score model, platform, how many teams to keep
//   — Solve                    a bar pinned under the steps, always in view
//
// NOTHING is recreated: the existing control blocks are MOVED into the new
// cards, ids and listeners intact, so app.js keeps driving them unchanged. A
// collapsed card shows what is currently selected in it, so nothing is hidden
// in the sense that matters — only folded.
import { el } from "/dom.js";

const $ = (id) => document.getElementById(id);
const block = (id) => { const n = $(id); return n ? (n.closest(".ctrl") || n) : null; };

function wrapBody(card) {
  // everything after the header becomes the foldable body
  const head = card.querySelector(":scope > .step-head");
  const body = el("div", "st-body");
  [...card.children].forEach((child) => { if (child !== head) body.append(child); });
  card.append(body);
  return body;
}

function newCard(title) {
  const card = el("div", "stepcard");
  const head = el("div", "step-head");
  head.append(el("span", "step-n", ""), document.createTextNode(" " + title));
  const body = el("div", "st-body");
  const grid = el("div", "ctrl-grid");
  body.append(grid);
  card.append(head, body);
  return { card, grid };
}

function summary(card) {
  const parts = [];
  const song = card.querySelector("#song");
  if (song) {
    const across = $("across");
    const text = song.selectedOptions[0] ? song.selectedOptions[0].textContent.trim() : "";
    parts.push(song.value ? text : (across && across.checked ? "Across all songs" : "Any song"));
  }
  // What the user SEES selected, in page order: the lit mode card, the lit
  // segment button, a visible select. A hidden <select> is only the value
  // holder a segment or app.js drives ("develop-account", "finite") — its
  // option text is an internal name, never a label.
  card.querySelectorAll(".mode-card.on b, .seg button.on, select").forEach((n) => {
    if (n.id === "song" || parts.length >= 3) return;
    if (n.closest("[hidden]")) return;                              // a holder, or a row app.js has switched off
    const shown = n.tagName === "SELECT" ? n.selectedOptions[0] : n;
    const text = shown ? shown.textContent.trim() : "";
    if (text && !parts.includes(text)) parts.push(text.length > 34 ? text.slice(0, 33) + "…" : text);
  });
  const keep = card.querySelector("#top");
  if (keep && keep.value && parts.length < 3) parts.push("keep " + keep.value);
  return parts.join(" · ");
}

export function mountSteps() {
  const section = document.querySelector(".setup .controls") || document.querySelector(".setup");
  const cards = section ? [...section.querySelectorAll(":scope > .stepcard")] : [];
  if (cards.length !== 3 || !$("solve") || !$("song")) return;   // not the layout this was written for
  const [build, options, find] = cards;

  const goal = newCard("What are you optimising for?");
  const boards = newCard("Holomem boards");
  const details = newCard("Details");
  const songBlock = block("song");
  if (songBlock) goal.grid.append(songBlock);
  const panel = $("twostage");
  if (panel) goal.card.querySelector(".st-body").append(panel);
  const boardBlock = block("boardprimary");
  if (boardBlock) boards.grid.append(boardBlock);
  ["uptime", "platform"].forEach((id) => { const b = block(id); if (b) details.grid.append(b); });
  const keep = $("topctrl");
  if (keep) details.grid.append(keep);
  const sort = $("sortctrl");
  if (sort) details.grid.append(sort);

  build.after(goal.card);
  options.after(boards.card);
  boards.card.after(details.card);
  // Solve is NOT a step: inside a sixth card it would stay folded until the
  // five before it were answered, so the one button the page exists for would
  // be the last thing to appear. It is a bar pinned to the bottom of the
  // steps, there whatever is folded; the card's help text follows the steps,
  // and the card itself goes.
  const go = el("div", "st-go");
  const goRow = find.querySelector(".go-row");
  if (goRow) go.append(goRow);
  // "Estimate cost" is a developer's question, not a user's; app.js still owns
  // the button, it is just not shown.
  const estimate = $("estimate");
  if (estimate) estimate.style.display = "none";
  details.card.after(go);
  const help = find.querySelector(".ctrl-help");
  if (help) go.after(help);
  find.remove();
  $("solve").addEventListener("click", () => {
    const results = document.querySelector(".resline");
    if (results) results.scrollIntoView({ behavior: "smooth", block: "start" });
  });
  const steps = [build, goal.card, options, boards.card, details.card];
  wrapBody(build);
  wrapBody(options);

  let reached = 0;
  const setOpen = (index, open) => {
    const card = steps[index];
    card.classList.toggle("st-closed", !open);
    card.querySelector(".step-head").setAttribute("aria-expanded", String(open));
    if (!open) card.querySelector(".st-sum").textContent = summary(card);
    if (open) reached = Math.max(reached, index);
  };

  steps.forEach((card, index) => {
    card.classList.add("st");
    const head = card.querySelector(":scope > .step-head");
    head.querySelector(".step-n").textContent = String(index + 1);
    head.append(el("span", "st-sum", ""), el("span", "st-chev", "▾"));
    head.setAttribute("role", "button");
    head.tabIndex = 0;
    const toggle = () => setOpen(index, card.classList.contains("st-closed"));
    head.addEventListener("click", toggle);
    head.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); toggle(); }
    });
    // answering a step opens the next one — once; after that the headers are the user's
    const advance = () => { if (index === reached && index + 1 < steps.length) setOpen(index + 1, true); };
    const body = card.querySelector(":scope > .st-body");
    body.addEventListener("change", advance);
    body.addEventListener("click", (event) => {
      if (event.target.closest("button, .mode-card, [data-val]")) advance();
    });
    setOpen(index, index === 0);
  });
  reached = 0;

  const tools = el("div", "st-tools");
  const all = el("button", "st-all", "Show every step");
  all.type = "button";
  all.addEventListener("click", () => {
    const anyClosed = steps.some((card) => card.classList.contains("st-closed"));
    steps.forEach((_card, index) => setOpen(index, anyClosed));
    all.textContent = anyClosed ? "Fold the steps" : "Show every step";
  });
  tools.append(all);
  build.before(tools);

  const results = document.querySelector(".resline .step-n");
  if (results) results.textContent = String(steps.length + 1);
  // a summary can go stale while its card is closed (the song picker lives in a dialog)
  document.addEventListener("change", () => {
    steps.forEach((card) => {
      if (card.classList.contains("st-closed")) card.querySelector(".st-sum").textContent = summary(card);
    });
  });
}

mountSteps();
