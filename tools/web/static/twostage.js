// The "best team" panel: the two-stage answers cache for the selected song, or
// in general when no song is selected (GET /api/two-stage). Read-only. It says
// what the numbers are — SEARCHED, not certified; on which basis — and whether the answer still matches today's inputs (fresh / stale / missing),
// because a refresh rebuilds the cache in the background and that takes a while.
// The basis follows the Cards control: "My cards…" reads the ACCOUNT answers
// (your cards at their real level, your real board points), the two maxed
// choices read the maxed five-star catalogue's.
import { el } from "/dom.js";

const STYLE = `
.ts-panel { border: 1px solid var(--line, #3a3f4b); border-radius: 8px; padding: 12px 14px; margin: 12px 0; }
.ts-head { display: flex; flex-wrap: wrap; gap: 8px; align-items: baseline; margin-bottom: 6px; }
.ts-title { font-weight: 600; }
.ts-pill { font-size: 12px; padding: 1px 8px; border-radius: 999px; border: 1px solid currentColor; opacity: .85; }
.ts-pill.fresh { color: #4caf7a; } .ts-pill.stale { color: #d9a441; } .ts-pill.missing { color: #9aa0ab; }
.ts-note { font-size: 12px; opacity: .75; margin: 2px 0 8px; }
.ts-row { display: grid; grid-template-columns: 2em 8.5em minmax(12em, 1fr) minmax(16em, 2fr) 5.5em; gap: 8px;
          padding: 3px 0; border-top: 1px solid var(--line, #3a3f4b22); font-size: 13px; align-items: baseline; }
.ts-row .n { opacity: .6; } .ts-row .score { font-variant-numeric: tabular-nums; font-weight: 600; }
.ts-row .how { font-size: 11px; opacity: .7; text-align: right; }
.ts-points { margin-left: auto; display: inline-flex; gap: 4px; }
.ts-points button { font: inherit; font-size: 11px; padding: 1px 8px; border-radius: 999px; cursor: pointer;
  border: 1px solid var(--line, #3a3f4b); background: transparent; color: inherit; opacity: .7; }
.ts-points button.on { opacity: 1; border-color: currentColor; font-weight: 600; }
.ts-singers { margin-top: 10px; } .ts-singers > summary { cursor: pointer; font-size: 13px; opacity: .85; padding: 4px 0; }
.ts-row .sings { font-size: 11px; color: #4caf7a; margin-left: 6px; }
@media (max-width: 760px) { .ts-row { grid-template-columns: 2em 7em 1fr; } .ts-row .members, .ts-row .how { grid-column: 2 / -1; text-align: left; } }
`;

const fmt = (n) => (typeof n === "number" ? Math.round(n).toLocaleString("en-US") : "—");

function buildLine(build) {
  if (!build || !build.running) return "";
  const which = { account: " (your account)", "account-max": " (your account, max level)",
                  maxed: " (maxed catalogue)" }[build.basis] || "";
  if (build.stage === "general") return ` · rebuilding${which}: the in-general answer`;
  if (build.done != null && build.total) return ` · rebuilding${which}: ${build.done} of ${build.total} songs`;
  return " · rebuilding";
}

// On the account basis: the characters' board points as earned, or every
// character at max level (361 points) — "who would be best once everyone is
// levelled", which is a different answer from the one today's points give.
let maxLevel = false;
try { maxLevel = localStorage.getItem("ts-maxlevel") === "1"; } catch (_) { /* private mode */ }

function pointsSwitch(onChange) {
  const wrap = el("span", "ts-points");
  [["earned", "board points as earned", false], ["max", "every character at max level", true]].forEach(([key, label, on]) => {
    const b = el("button", maxLevel === on ? "on" : "", label);
    b.type = "button";
    b.dataset.points = key;
    b.addEventListener("click", () => {
      maxLevel = on;
      try { localStorage.setItem("ts-maxlevel", on ? "1" : "0"); } catch (_) { /* private mode */ }
      onChange();
    });
    wrap.append(b);
  });
  return wrap;
}

function render(box, data, onPoints) {
  box.replaceChildren();
  const head = el("div", "ts-head");
  head.append(el("span", "ts-title",
    (data.scope === "general" ? "Best team in general" : "Best team for this song")
      + ({ account: " — your account", "account-max": " — your account, characters at max level" }[data.basis_id]
         || " — maxed catalogue")));
  const state = data.state === "unverified" ? "stale" : data.state;
  const label = { fresh: "up to date", stale: "computed from older data", missing: "not computed yet" }[state] || state;
  head.append(el("span", `ts-pill ${state}`, label));
  head.append(el("span", "ts-pill", "searched, not certified"));
  if (data.basis_id === "account" || data.basis_id === "account-max") head.append(pointsSwitch(onPoints));
  box.append(head);
  const scored = data.exact_evals && data.legal_sets
    ? ` · ${fmt(data.exact_evals)} teams scored exactly out of ${fmt(data.legal_sets)} member sets` : "";
  const what = data.scope === "general" && data.charts
    ? `mean expected score over ${data.charts} charts` : "expected score on this song";
  box.append(el("p", "ts-note",
    data.state === "missing"
      ? `No answer in the cache for this yet${buildLine(data.build) || " · press Refresh to build it"}.`
      : `${what} · ${data.basis}${scored}${buildLine(data.build)}`));
  (data.rows || []).forEach((row, i) => {
    const line = el("div", "ts-row");
    line.append(el("span", "n", String(i + 1)));
    line.append(el("span", "score", fmt(row.best)));
    const leader = el("span", "leader", row.leader_name || row.leader);
    if (row.sings) leader.append(el("span", "sings", "sings"));
    line.append(leader);
    line.append(el("span", "members", (row.member_names || row.members || []).join(", ")));
    line.append(el("span", "how", row.status === "searched" ? "searched" : "short list"));
    box.append(line);
  });
}

// Under the in-general answer: every character's best team with HER as leader
// over the songs she sings (the cache's per-singer answers). Folded; it is a
// list to look someone up in, not the headline.
function renderSingers(box, data) {
  if (!data || !(data.rows || []).length) return;
  const fold = document.createElement("details");
  fold.className = "ts-singers";
  const state = data.state === "fresh" ? "" : " · computed from older data";
  const summary = document.createElement("summary");
  summary.textContent = `Best team per singer — ${data.rows.length} characters, each as leader on the songs she sings${state}`;
  fold.append(summary);
  data.rows.forEach((row, i) => {
    const line = el("div", "ts-row");
    line.append(el("span", "n", String(i + 1)));
    line.append(el("span", "score", fmt(row.best)));
    line.append(el("span", "leader", row.leader_name || row.leader));
    line.append(el("span", "members", (row.member_names || row.members || []).join(", ")));
    line.append(el("span", "how", `${row.songs} song${row.songs === 1 ? "" : "s"}`));
    fold.append(line);
  });
  box.append(fold);
}

export function mountTwoStage() {
  const box = document.getElementById("twostage");
  const song = document.getElementById("song");
  if (!box) return;
  if (!document.getElementById("ts-style")) {
    const style = document.createElement("style");
    style.id = "ts-style";
    style.textContent = STYLE;
    document.head.append(style);
  }
  box.classList.add("ts-panel");
  let token = 0;
  let timer = null;
  async function load() {
    const mine = ++token;
    clearTimeout(timer);
    const id = song ? song.value : "";
    const basis = document.getElementById("basis");
    const maxPoints = document.getElementById("maxpoints");
    const which = basis && basis.value !== "account" ? "maxed"
      : (maxLevel || (maxPoints && maxPoints.checked) ? "account-max" : "account");
    try {
      const res = await fetch(`/api/two-stage?song=${encodeURIComponent(id)}&basis=${which}`);
      const data = await res.json();
      if (mine !== token) return;                 // a newer selection won
      render(box, data, load);
      if (!id) {
        const singers = await (await fetch(`/api/two-stage?scope=singers&basis=${which}`)).json();
        if (mine !== token) return;
        renderSingers(box, singers);
      }
      // while the cache is being rebuilt, look again: the answer may land or go fresh
      if (data.build && data.build.running) timer = setTimeout(load, 15000);
    } catch (err) {
      if (mine === token) box.replaceChildren(el("p", "ts-note", `best-team panel unavailable: ${err}`));
    }
  }
  if (song) song.addEventListener("change", load);
  // the board step's "Assume max board points" shows the max-level answers too
  const maxPointsBox = document.getElementById("maxpoints");
  if (maxPointsBox) maxPointsBox.addEventListener("change", () => {
    maxLevel = maxPointsBox.checked;
    load();
  });
  const cards = document.getElementById("cardsmode");
  if (cards) cards.addEventListener("change", () => setTimeout(load, 0));   // after app.js moved #basis
  load();
}

mountTwoStage();
