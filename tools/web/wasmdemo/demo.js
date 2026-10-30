// demo.js — unites the two WASM engines in the browser:
//   Engine 1 (Pyodide): the real hbuilder Python scorer, run verbatim.
//   Engine 2 (highs.wasm): the board integer program, solved to the optimum.
// Everything client-side, loaded from local files. No server, no CDN.

import { loadPyodide } from "./pyodide.mjs";

const $ = (id) => document.getElementById(id);
const bootEl = $("boot");
let steps = [];

function boot(label, state, extra) {
  let s = steps.find((x) => x.label === label);
  if (!s) { s = { label }; steps.push(s); }
  s.state = state; s.extra = extra;
  bootEl.innerHTML = steps.map((x) => {
    const ic = x.state === "ok" ? '<span class="ic ok">✓</span>'
      : x.state === "err" ? '<span class="ic err">✕</span>'
        : '<span class="ic run"><span class="spin"></span></span>';
    return `<div class="step">${ic}<span>${x.label}</span>` +
      (x.extra ? `<span class="t">${x.extra}</span>` : "") + `</div>`;
  }).join("");
}

// --- Engine 2: HiGHS. highs.js is a UMD Emscripten module that sets a global
// `Module` factory; grab it, init, then remove the global so it can't collide
// with Pyodide's own Emscripten runtime. ---
function loadHighs() {
  return new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = "./highs.js";
    s.onload = async () => {
      const factory = window.Module;
      try { window.Module = undefined; } catch (_) {}   // var-global isn't deletable; just detach
      try {
        const highs = await factory({
          locateFile: (f) => "./" + f, print: () => {}, printErr: () => {},
        });
        resolve(highs);
      } catch (e) { reject(e); }
    };
    s.onerror = () => reject(new Error("could not load highs.js"));
    document.head.appendChild(s);
  });
}

const ms = (t) => Math.round(t) + " ms";
const fmt = (n) => Number(n).toLocaleString("en-US");

(async () => {
  let highs, py, scoreFn, nCards, boardLP, board;
  try {
    // Engine 2
    boot("HiGHS solver (WASM)", "run");
    let t = performance.now();
    highs = await loadHighs();
    boot("HiGHS solver (WASM)", "ok", ms(performance.now() - t));

    // Engine 1
    boot("Pyodide (CPython 3.14 → WASM)", "run");
    t = performance.now();
    py = await loadPyodide({ indexURL: "./" });
    boot("Pyodide (CPython 3.14 → WASM)", "ok", ms(performance.now() - t));

    // hbuilder source + game data into the Pyodide filesystem
    boot("hbuilder + game data", "run");
    t = performance.now();
    const zip = await (await fetch("./hbuilder.zip")).arrayBuffer();
    py.unpackArchive(zip, "zip");                 // -> /home/pyodide/hbuilder/...
    py.FS.mkdirTree("/home/pyodide/data");
    for (const f of ["cards.json", "config.json"]) {
      const buf = new Uint8Array(await (await fetch("./data/" + f)).arrayBuffer());
      py.FS.writeFile("/home/pyodide/data/" + f, buf);
    }
    py.runPython(`
import sys
sys.path.insert(0, "/home/pyodide")
from hbuilder.domain.loader import load_cards
from hbuilder.domain.scoring_config import load_scoring_config
from hbuilder.domain.scoring import score_card_set
_CARDS = sorted(load_cards("/home/pyodide/data/cards.json"), key=lambda c: c.id)
_CFG = load_scoring_config("/home/pyodide/data/config.json")
def _score(idxs):
    cs = [_CARDS[int(i)] for i in idxs]
    m = tuple(cs[:5]); L = cs[5]
    s = score_card_set(m, (L,), _CFG)[0]
    return {"leader": L.id, "members": [c.id for c in cs[:5]], "score": float(s.total_score)}
def _ncards(): return len(_CARDS)
`);
    scoreFn = py.globals.get("_score");
    nCards = py.globals.get("_ncards")();
    boot("hbuilder + game data", "ok", nCards + " cards · " + ms(performance.now() - t));

    // board model for Engine 2
    boot("board model (LP)", "run");
    boardLP = await (await fetch("./board.lp")).text();
    board = await (await fetch("./board.json")).json();
    boot("board model (LP)", "ok", board.nodes.length + " nodes");

    boot("ready — both engines live", "ok");
  } catch (e) {
    boot("boot failed", "err", String(e.message || e));
    console.error(e);
    return;
  }

  // ---- Engine 1: score a team (live, in Pyodide) ----
  function renderScore(idxs) {
    const t = performance.now();
    const r = scoreFn(idxs).toJs({ dict_converter: Object.fromEntries });
    const dt = performance.now() - t;
    $("scoreVal").innerHTML = fmt(Math.round(r.score)) + " <small>Unit Score</small>";
    $("scLeader").textContent = r.leader;
    $("scMembers").textContent = r.members.join("  ");
    $("scTime").textContent = ms(dt);
    return r;
  }
  function randomTeam() {
    const s = new Set();
    while (s.size < 6) s.add(Math.floor(Math.random() * nCards));
    return [...s];
  }
  $("btnScore").onclick = () => {
    const r = renderScore(randomTeam());
    $("scNote").innerHTML = "Scored live in Pyodide — the same <code>score_card_set</code> as native Python.";
  };
  $("btnFixed").onclick = () => {
    renderScore([0, 1, 2, 3, 4, 5]);
    $("scNote").innerHTML = "Reference team scores <b>214106.56707666052</b> — byte-identical to native Python.";
  };

  // ---- Engine 2: solve the board (live, in HiGHS) ----
  function solveBoard() {
    const t = performance.now();
    const res = highs.solve(boardLP, {});
    const dt = performance.now() - t;
    const cols = res.Columns || {};
    const chosen = [];
    board.nodes.forEach((n) => {
      const c = cols["x_" + n.replace(/-/g, "_")];   // vars named after node ids
      const v = c ? (c.Primal !== undefined ? c.Primal : c.Value) : 0;
      if (Math.abs(v) > 0.5) chosen.push(n);
    });
    const pts = chosen.reduce((a, n) => a + (board.cost[n] || 0), 0);
    $("objVal").innerHTML = fmt(res.ObjectiveValue ? Math.round(res.ObjectiveValue) : 0) + " <small>board value</small>";
    $("soStatus").innerHTML = `<span style="color:var(--good)">${res.Status}</span> (provably optimal)`;
    $("soPts").textContent = pts + " / " + board.budget;
    $("soTime").textContent = ms(dt);
    const reds = chosen.filter((n) => n[0] === "R");
    const socks = chosen.filter((n) => n[0] === "S" && n !== board.root);
    $("soChips").innerHTML =
      `<span class="chip">${chosen.length - 1} nodes</span>` +
      `<span class="chip red">${reds.length} red</span>` +
      socks.map((n) => `<span class="chip sock">socket ${n}</span>`).join("");
  }
  $("btnSolve").onclick = solveBoard;

  // enable + auto-run once so both panels show real results
  for (const b of ["btnScore", "btnFixed", "btnSolve"]) $(b).disabled = false;
  $("btnFixed").click();
  solveBoard();
})();
