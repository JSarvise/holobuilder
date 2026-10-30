// boardtree.js — a READ-ONLY Holomem-Board tree renderer for the Optimise view.
//
// The Board tab's board.js is an EDITOR: it owns click-to-buy, a module `state`,
// pan/zoom and the socket picker. This renders the SAME geometry with none of
// that — it just PAINTS a board-export/v0 character (nodes with pos/branch/state,
// sockets with placed_card) so the page can SHOW what the optimiser bought. It
// takes the branch palette from the export (the game's own hues), so it cannot
// drift from build_skilltree.py, and it invents no number: every node's state
// and gain are the export's.
//
// Geometry mirrors board.js exactly: `pos` is screen-like [x, y] (models already
// store [x, -y]; no flip — Leader is the top row), adjacency is the orthogonal
// 4-neighbour grid, and the same CELL/px mapping. It draws colours, states,
// glyphs and tooltips; pan/zoom are not needed to read the plan.

const SVG_NS = "http://www.w3.org/2000/svg";
const CELL = 30;
const PAD = 22;
const NODE_R = 9;
const SOCKET_R = 12;

function svg(tag, attrs) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  return node;
}

// Orthogonal 4-neighbour adjacency over a {"x,y": id} cell map — the game's
// rule (board.js: diagonals are not edges).
function neighbourLines(cells) {
  const lines = [];
  const seen = new Set();
  for (const [key, a] of cells) {
    const [x, y] = key.split(",").map(Number);
    for (const [dx, dy] of [[1, 0], [0, 1]]) {  // right + down covers each edge once
      const b = cells.get(`${x + dx},${y + dy}`);
      if (b) lines.push([a, b]);
    }
  }
  return lines;
}

/** Render `character` (a board-export/v0 character) into `host` as an SVG tree.
 *  `branches` is the export's palette (code -> {name, hue}). */
export function renderBoardTree(host, character, branches) {
  host.replaceChildren();
  const nodes = character.nodes || [];
  const sockets = character.sockets || [];
  const all = [...nodes, ...sockets.map((s) => ({ ...s, id: s.socket, socket: true }))];
  if (!all.length) {
    host.append(Object.assign(document.createElement("p"), {
      className: "note", textContent: "no board geometry for this character",
    }));
    return;
  }

  // The planner's real picks when the entry carries them (`--plan`): to_buy is
  // the gold "buy this", to_sell the respec drops. Falls back to the gain>0
  // heuristic when no plan was run.
  const plan = character.plan || null;
  const toBuy = new Set(plan ? plan.to_buy || [] : []);
  const toSell = new Set(plan ? plan.to_sell || [] : []);
  // The FINAL board — what you'll have after the plan. This is the PRIMARY thing
  // the tree shows (filled); buy/sell are secondary marks on top of it. Without a
  // plan the final board is just what's currently owned.
  const finalNodes = plan ? new Set(plan.optimised_nodes || []) : null;
  // Nodes a recommended Connect card's shape amplifies — drawn with a cyan accent
  // so you can see WHAT each socket buffs.
  const buffed = new Set(character.connect_buffed_nodes || []);

  const byId = new Map(all.map((n) => [n.id, n]));
  const cells = new Map(all.map((n) => [`${n.pos[0]},${n.pos[1]}`, n.id]));
  const xs = all.map((n) => n.pos[0]);
  const ys = all.map((n) => n.pos[1]);
  const minX = Math.min(...xs), minY = Math.min(...ys);
  const w = (Math.max(...xs) - minX) * CELL + PAD * 2;
  const h = (Math.max(...ys) - minY) * CELL + PAD * 2;
  const px = (x) => (x - minX) * CELL + PAD;
  const py = (y) => (y - minY) * CELL + PAD;

  const root = svg("svg", { viewBox: `0 0 ${w} ${h}`, width: w, height: h, class: "botree" });

  // Lines first, under the nodes. A link between two nodes on the FINAL board
  // reads "on" — so the lit skeleton IS the board you'll have.
  const isFinal = (id) => finalNodes ? finalNodes.has(id) : byId.get(id)?.state === "owned";
  for (const [aId, bId] of neighbourLines(cells)) {
    const a = byId.get(aId), b = byId.get(bId);
    const on = isFinal(aId) && isFinal(bId);
    root.append(svg("line", {
      x1: px(a.pos[0]), y1: py(a.pos[1]), x2: px(b.pos[0]), y2: py(b.pos[1]),
      class: "botree-link" + (on ? " on" : ""),
    }));
  }

  for (const n of all) {
    const hue = (branches[n.branch]?.hue) || (n.socket ? "#e8eaf0" : "#888");
    const cx = px(n.pos[0]), cy = py(n.pos[1]);
    const r = n.socket ? SOCKET_R : NODE_R;
    const owned = n.state === "owned";
    const buyable = n.state === "buyable";
    // The tree shows the FINAL board. A node is "on" (filled) if it's in the
    // optimised board; with no plan, if it's currently owned.
    const onFinal = finalNodes ? finalNodes.has(n.id) : owned;
    // Secondary marks: NEW = in the final board but not owned now (you buy it);
    // SELL = owned now but not in the final board (the respec refunds it). Without
    // a plan, "new" falls back to the gain>0 heuristic on buyable nodes.
    const isNew = plan ? (toBuy.has(n.id)) : (buyable && typeof n.gain === "number" && n.gain > 0);
    const sell = plan && toSell.has(n.id);

    const g = svg("g", { class: "botree-node" });

    // A SOLD node is not part of the final board, so it's ghosted — faint, thin
    // dashed red — to read as "was here, being removed" without competing with
    // the board you're keeping.
    if (sell) {
      g.append(svg("circle", {
        cx, cy, r, fill: "none", stroke: "#FF6B6B", "stroke-width": 1.4,
        "stroke-dasharray": "2 2", opacity: 0.5,
      }));
    }

    g.append(svg("circle", {
      cx, cy, r,
      // ON the final board = filled with its branch hue; off it = dim empty.
      fill: onFinal ? hue : "#161a24",
      stroke: onFinal ? hue : "#3a3a4a",
      "stroke-width": 1.4,
      opacity: onFinal ? 1 : (sell ? 0.25 : 0.35),
    }));

    // A node a recommended Connect card amplifies gets a soft cyan halo, so you
    // can SEE which tiles the socket buffs.
    if (buffed.has(n.id) && onFinal && !n.socket) {
      g.append(svg("circle", {
        cx, cy, r: r + 4, fill: "none", stroke: "#28C3FF", "stroke-width": 1.3,
        opacity: 0.55,
      }));
    }

    // A NEW node (bought by the plan) gets a thin gold accent ring — subtle, so
    // the final board reads as one board, not a field of halos.
    if (isNew && onFinal) {
      g.append(svg("circle", {
        cx, cy, r: r + 2.5, fill: "none", stroke: "#FFD54A", "stroke-width": 1.6,
        opacity: 0.9,
      }));
    }

    // A socket gets the CARD'S 512px square art when it holds — or is RECOMMENDED
    // to hold — a card, clipped into the socket circle so the placement is visible
    // on the board itself. A cyan fill sits under it as the fallback if the thumb
    // 404s; the ring is gold for a recommended pick, cyan for a current one.
    //
    // A CURRENT card (`placed_card`) only belongs on a socket the shown board
    // actually has: with a plan, the socket must be ON the final board — a respec
    // that does not re-buy S-002/3/4 leaves NO card there, and pinning the current
    // card to a socket the plan never reached would show a card where the plan
    // puts none. A recommended pick is already gated to a bought socket by
    // the optimiser. Without a plan, the current board IS what's shown, so its
    // placed cards always belong.
    const socketReached = finalNodes ? finalNodes.has(n.id) : true;
    const socketCard = n.socket
      ? (n.recommended_card || (socketReached ? n.placed_card : null))
      : null;
    if (socketCard) {
      const rr = r - 2;
      g.append(svg("circle", { cx, cy, r: rr, fill: "#28C3FF", opacity: 0.55 }));
      const clipId = "sk" + n.id.replace(/[^a-z0-9]/gi, "") + Math.round(cx) + "_" + Math.round(cy);
      const clip = svg("clipPath", { id: clipId });
      clip.append(svg("circle", { cx, cy, r: rr }));
      g.append(clip);
      g.append(svg("image", {
        href: `/cardart/thumb/${socketCard}?w=96`,
        x: cx - rr, y: cy - rr, width: rr * 2, height: rr * 2,
        "clip-path": `url(#${clipId})`, preserveAspectRatio: "xMidYMid slice",
      }));
      g.append(svg("circle", {
        cx, cy, r: rr, fill: "none",
        stroke: n.recommended_card ? "#FFD54A" : "#28C3FF", "stroke-width": 1.6,
        opacity: 0.9,
      }));
    }

    // The game's own skill glyph (dumps/skill_icons via /icon/), the identity
    // mapping from the node's effect_icon. Sockets grant nothing so carry none;
    // an unbought node keeps its glyph but dimmed, so the board reads as a plan
    // rather than a wall of identical circles.
    if (!n.socket && n.effect_icon) {
      const side = r * 1.5;
      g.append(svg("image", {
        href: `/icon/${n.effect_icon}.png`,
        x: cx - side / 2, y: cy - side / 2, width: side, height: side,
        opacity: onFinal ? 1 : 0.4, class: "botree-glyph",
      }));
    }

    const title = svg("title", {});
    const parts = [`${n.id} — ${branches[n.branch]?.name || (n.socket ? "Connect socket" : n.branch)}`];
    if (!n.socket && n.cost?.points != null) parts.push(`${n.cost.points} pt`);
    if (n.effect) parts.push(n.effect);
    if (n.socket) {
      if (n.recommended_card) {
        const label = n.recommended_card_member
          ? `${n.recommended_card_member} · ${n.recommended_card_name}`
          : (n.recommended_card_name || n.recommended_card);
        parts.push(`▶ place: ${label}` +
          (n.recommended_multiplier ? ` (×${n.recommended_multiplier})` : ""));
        if (n.recommended_effect) parts.push(n.recommended_effect);
      } else if (n.placed_card && socketReached) {
        parts.push(`card: ${n.placed_card}`);
      } else if (n.placed_card && !socketReached) {
        // A card sits here on the CURRENT board, but the plan does not buy this
        // socket — so it holds nothing after the respec. Say so rather than imply
        // the card is on the planned board.
        parts.push("socket not bought by the plan (its current card is refunded)");
      } else {
        parts.push("empty socket");
      }
    } else {
      parts.push(onFinal ? "in the final board" : n.state);
    }
    if (buffed.has(n.id) && !n.socket) parts.push("◆ amplified by a Connect card");
    if (isNew) parts.push(plan ? "▶ BUY (new in the plan)" : "▶ worth buying");
    if (sell) parts.push("◀ SELL (refunded by the respec)");
    if (typeof n.gain === "number" && n.gain > 0) {
      parts.push(`gain +${n.gain.toLocaleString("en-US", { maximumFractionDigits: 1 })}`);
    }
    title.textContent = parts.join("\n");
    g.append(title);

    root.append(g);
  }

  const scroll = document.createElement("div");
  scroll.className = "botree-scroll";
  scroll.append(root);
  host.append(scroll);
}
