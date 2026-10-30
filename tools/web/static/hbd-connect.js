// The Connect sockets of one holomem's board, shared by the Holomem Board page and the
// Optimizer's board plan: the strip of the four sockets under a board, and the
// detail of one socket -- its card, its effect, every node in its range with the value
// before and after the boost.
//
// Works off the board-export doc alone. The Holomem Board page also loads the account's
// boards (`D.chars`, the base node values) and collection (`D.coll`, card titles); where
// those are absent (the Optimizer) the doc's own node and socket fields stand in.
import { esc, fmt, plural, D, BRANCH, WHO, effectShort, boosted, fmtValue, connectView, ico } from "/hbd-util.js";
import { viewSets, socketCardFn } from "/hbd-draw.js";

const CONNECT_COLOR = "var(--connect)";   // the game's Blu2nd connect colour (hb.css --connect)

/** `{member, title}` of a card id: the collection when loaded, else the socket's own names. */
function cardName(id, s) {
  const c = ((D.coll && D.coll.cards) || []).find(x => x.id === id);
  if (c) return { member: c.member, title: c.title || c.costume_name || "" };
  if (s && id && id === s.recommended_card && s.recommended_card_name)
    return { member: s.recommended_card_member || "", title: s.recommended_card_name };
  if (s && id && id === s.placed_card && s.placed_card_name)
    return { member: s.placed_card_member || "", title: s.placed_card_name };
  return null;
}

/** A node's base (unboosted) value: the account's board when loaded, else the doc's node. */
function baseNode(ch, nid) {
  const acct = D.chars && D.chars.get ? D.chars.get(ch.chr) : null;
  return ((acct || {}).nodes || []).find(x => x.id === nid)
    || (ch.nodes || []).find(x => x.id === nid) || {};
}

/** The strip under a board: one card per socket (`data-socket`, hover to read it). */
export function connectStripHTML(ch, mode) {
  const V2 = viewSets(ch, mode);
  const cardOf = socketCardFn(ch, mode);
  const CV = connectView(ch, cardOf);
  const rows = (ch.sockets || []).map((s, i) => {
    const on = V2.final.has(s.socket) || s.socket === "S-001";
    const id = cardOf(s);
    const info = id ? cardName(id, s) : null;
    const moved = V2.plan && id && id !== s.placed_card;
    const con = CV.bySocket.get(s.socket);
    const eff = con ? `<span class="eff"><b>+${fmt(con.info.permil / 10)}%</b> to ${plural(con.covers.length, "node")} in range · Connect Lv ${con.info.level}</span>`
      : id ? `<span class="eff">No Connect effect for this card</span>` : "";
    return `<div class="hbd-sock${id ? " has" : " empty"}${moved ? " moved" : ""}" data-socket="${esc(s.socket)}">
      <div class="art">${id ? `<img src="/cardart/thumb/${encodeURIComponent(id)}?w=160" alt="" loading="lazy" onerror="this.style.visibility='hidden'">` : ico(on ? "plus" : "lock")}</div>
      <div class="tx"><small>Connect ${i + 1}${s.point_cost ? ` · ${s.point_cost} Pt` : ""}${moved ? ` · <em>${s.placed_card ? "change" : "place"}</em>` : ""}</small>
        ${id ? `<b>${esc(info ? info.title || id : id)}</b><span>${esc(info ? info.member : "")}</span>${eff}`
        : `<b class="dim">${on ? "Empty" : "Not unlocked"}</b>`}</div>
    </div>`;
  }).join("");
  return `<h4>Connect</h4><div class="hbd-socks">${rows}</div>`;
}

/** One socket read out: its card and effect, and every node in range, base -> boosted. */
export function connectInfoHTML(ch, id, mode) {
  const s = (ch.sockets || []).find(x => x.socket === id) || {};
  const V2 = viewSets(ch, mode);
  const cardOf = socketCardFn(ch, mode);
  const CV = connectView(ch, cardOf);
  const on = V2.final.has(id) || id === "S-001";
  const con = CV.bySocket.get(id);
  const card = cardOf(s);
  const nm = card ? cardName(card, s) : null;
  const where = `<p class="dim">${s.point_cost ? `${s.point_cost} Pt to unlock.` : "Free: every board starts here."}${on ? "" : " Not unlocked."}</p>`;
  if (!con) {
    return `<div class="hbd-ni" style="--bh:${CONNECT_COLOR}"><span class="tag">Connect · ${esc(id)}</span>
      <h4>${card ? esc(nm ? `${nm.member} · ${nm.title}` : card) : on ? "Empty" : "Locked"}</h4>
      <p>${card ? "This card has no Connect effect." : esc(WHO.S)}</p>${where}</div>`;
  }
  const byIdN = new Map((ch.nodes || []).map(n => [n.id, n]));
  const items = con.covers.map(nid => {
    const n = byIdN.get(nid) || {}, bn = baseNode(ch, nid);
    const b = BRANCH[n.branch] || BRANCH.S;
    const has = V2.final.has(nid);
    const permil = CV.permil.get(nid) || 0;
    const val = bn.value != null && bn.value_unit
      ? `${esc(fmtValue(bn.value, bn.value_unit))} → <b>${esc(fmtValue(boosted(bn.value, permil), bn.value_unit))}</b>` : `<span class="dim">no value</span>`;
    return `<li class="${has ? "" : "off"}" data-goto="${esc(nid)}" style="--g1:${b.g1};--g2:${b.g2};--bh:${b.ink}" title="${has ? "Unlocked" : "Not unlocked: the boost applies once it is"}">
      <i></i><span>${esc(effectShort(bn.effect || n.effect, ch.name, fmtValue(bn.value, bn.value_unit)))}</span><em>${val}</em></li>`;
  }).join("");
  const lv = con.info.level_basis === "assumed" ? " (bloom not recorded: base level assumed)" : con.info.level_basis === "maxed" ? " (maxed account)" : "";
  const owned = con.covers.filter(x => V2.final.has(x)).length;
  const mult = (1 + con.info.permil / 1000).toFixed(2).replace(/0$/, "");
  return `<div class="hbd-ni" style="--bh:${CONNECT_COLOR}"><span class="tag">Connect · ${esc(id)}</span>
    <div class="card"><img src="/cardart/thumb/${encodeURIComponent(card)}?w=160" alt="" onerror="this.style.visibility='hidden'">
      <span><b>${esc(nm ? nm.title || card : card)}</b><small>${esc(nm ? nm.member : "")}</small></span></div>
    <p class="eff">${esc(con.info.text)}</p>
    <p class="dim">Connect Lv ${con.info.level}${esc(lv)} · ×${mult} on every node in range.</p>
    <h5>${plural(con.covers.length, "node")} in range · ${owned} ${mode === "plan" ? "on the target board" : "unlocked"}</h5>
    <ul class="hbd-cov">${items}</ul>${where}</div>`;
}
