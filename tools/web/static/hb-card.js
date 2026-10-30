// The game's card face as an HTML string. Same structure as tools/web/static/
// ranking.js cardTile's face, minus the page-specific footer. Needs gens.js
// (window.HB_GENS: CharacterGrouping colours and English labels by chr id).
(function () {
  const CHROME = {
    el: t => `/chrome/element/ui_999_ico_element_${String(t || "").toLowerCase()}_cl.png`,
    rbar: r => `/chrome/rarity/ui_999_thm_card_rarity_r${r}.png`,
    star: "/chrome/rarity/ui_999_img_card_star.png",
    flower: on => `/chrome/bloom/ui_999_img_flower${on ? "" : "_blank"}.png`,
  };
  const QUOTE = String.fromCharCode(34);   // spelled out: a quote inside a regex trips test_js_syntax
  const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", [QUOTE]: "&quot;" };
  const ESC_RE = new RegExp("[&<>" + QUOTE + "]", "g");
  const esc = s => String(s ?? "").replace(ESC_RE, c => ESC[c]);
  const chrOf = id => "chr-" + String(id).split("-")[1];

  function gens(card) {
    const by = (window.HB_GENS && window.HB_GENS.by) || {};
    const list = (by[chrOf(card.id)] || []).filter(g => g.reg).slice(0, 2);
    if (!list.length) return "";
    return `<div class="gc-gens">${list.map(g =>
      `<div class="gc-gen" style="--gc:${g.c};--gc2:${g.c2}"><i></i><u></u><b>${esc(g.n)}</b></div>`).join("")}</div>`;
  }

  // card: {id, member, type, rarity (int), level, bloom}; opts: {lead, w}
  function face(card, opts) {
    opts = opts || {};
    // rarity arrives as 5 or as "5★" (/api/collection): the digit names the sprite, the class
    // and the star count; a "★" left in the URL makes the rarity band request fail
    const r = parseInt(String(card.rarity == null ? "" : card.rarity), 10) || 3;
    const w = opts.w || 400;
    let stars = "";
    for (let i = 0; i < r; i++) stars += `<span style="--k:${r - 1 - i}"><img src="${CHROME.star}" alt=""><i></i></span>`;
    const bloom = !card.bloom ? "" :
      `<div class="gc-bloom${card.bloom > 0 ? "" : " off"}"><img src="${CHROME.flower(card.bloom > 0)}" alt=""><em>${card.bloom}</em></div>`;
    const lvl = card.level == null ? "" : `<div class="gc-lvl"><span>Lv</span><b>${card.level}</b></div>`;
    const role = opts.role === false ? "" :
      `<div class="gc-role${opts.lead ? "" : " gc-role-empty"}">${opts.lead ? (opts.roleText || "Leader") : "&nbsp;"}</div>`;
    return `<div class="gc ${esc(card.type)} gc-r${r}${opts.lead ? " is-lead" : ""}">${role}
      <div class="gc-face">
        <img class="gc-port" src="/cardart/vert/${esc(card.id)}?w=${w}" alt="${esc(card.member)}" loading="lazy"
          onload="this.parentNode.classList.add('has')" onerror="this.remove()">
        <div class="gc-ring"></div>
        <img class="gc-el" src="${CHROME.el(card.type)}" alt="${esc(card.type)}">
        ${gens(card)}
        <span class="gc-clip"><img class="gc-rbar" src="${CHROME.rbar(r)}" alt=""></span>
        <div class="gc-stars">${stars}</div>
        ${bloom}${lvl}
      </div></div>`;
  }
  window.HBCard = { face };
})();
