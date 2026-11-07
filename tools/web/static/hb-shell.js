// The shared shell: the section rail, the same on every page. A page includes
// <nav class="rail" data-page="<id>"></nav> and this script. Icons are the game's OFFICIAL
// white-silhouette sprites (dumps/ui_official, served by GET /ui/<category>/<sprite>.png),
// tinted with CSS masks (`.ico`), never hand-drawn.
(function () {
  const SECTIONS = [
    { id: "optimizer", label: "Optimizer", href: "/optimizer", icon: "nav/ui_999_ico_unit.png" },
    { id: "event", label: "Event", href: "/event", icon: "nav/ui_999_ico_event.png" },
    { id: "collection", label: "Collection", href: "/cards", icon: "nav/ui_999_ico_book_open_l.png" },
    { id: "board", label: "Holomem Board", href: "/holoboard", icon: "board/ui_710_ico_star_all_member.png" },
    { id: "compare", label: "Compare", href: "/versus", icon: "nav/ui_999_ico_trophy.png" },
    { id: "gacha", label: "Gacha", href: "/pulls", icon: "nav/ui_999_ico_gift.png" },
    { id: "park", label: "Park", href: "/parkview", icon: "nav/ui_999_ico_map_marker.png" },
    { id: "resources", label: "Inventory", href: "/inventory", icon: "nav/ui_999_ico_resource_coin.png" },
  ];
  const ico = path => `<i class="ico" style="--ico:url(/ui/${path})" aria-hidden="true"></i>`;
  function render(nav) {
    const current = nav.dataset.page;
    nav.setAttribute("aria-label", "Sections");
    nav.innerHTML = `<a class="mark" href="/optimizer" aria-label="hololive builder">${ico("nav/ui_999_ico_unit.png")}</a>` + SECTIONS.map(s =>
      `<a href="${s.href}"${s.id === current ? ' aria-current="page"' : ""}>
        ${ico(s.icon)}${s.label}</a>`).join("");
    // Inside the desktop app (tools/app/launcher.py) the window exposes its Settings; in a
    // browser there is no such API and no button. pywebview injects the API after load.
    const settings = () => {
      if (!window.pywebview || nav.querySelector(".app-settings")) return;
      const a = document.createElement("a");
      a.href = "#"; a.className = "app-settings"; a.style.marginTop = "auto";
      a.innerHTML = `${ico("nav/ui_999_ico_menu.png")}Settings`;
      a.onclick = e => { e.preventDefault(); window.pywebview.api.open_settings(); };
      nav.appendChild(a);
    };
    settings();
    window.addEventListener("pywebviewready", settings);
    // a new version published while the app is open: the launcher asks the server every
    // 10 min and calls hbUpdate on the open page; a page opened later asks it itself
    const pending = () => window.pywebview && window.pywebview.api.pending_update &&
      window.pywebview.api.pending_update().then(u => u && window.hbUpdate(u)).catch(() => {});
    pending();
    window.addEventListener("pywebviewready", pending);
    // the admin console: only for this PC's admin key, with the number of people waiting
    fetch("/api/admin/me").then(r => r.ok ? r.json() : {}).then(me => {
      if (!me.admin) return;
      const a = document.createElement("a");
      a.href = "/users";
      if (current === "users") a.setAttribute("aria-current", "page");
      a.innerHTML = `${ico("nav/ui_999_ico_player_rank.png")}Users`;
      a.style.position = "relative";
      nav.insertBefore(a, nav.querySelector(".app-settings"));
      fetch("/api/admin/users").then(r => r.ok ? r.json() : {}).then(d => {
        const n = (d.users || []).filter(u => u.status === "pending").length;
        if (n) a.insertAdjacentHTML("beforeend", `<b style="position:absolute;top:4px;right:10px;min-width:18px;height:18px;border-radius:9px;background:var(--warn);color:#2A2950;font-size:11px;display:grid;place-items:center;padding:0 5px">${n}</b>`);
      });
    }).catch(() => {});
  }
  // the update notice (desktop app only): a band across the top of every page, from the start
  // check or a poll while open, until "Update now" or the close button (the launcher remembers
  // the dismissed version for every page; a newer one shows again)
  window.hbUpdate = u => {
    if (!u || document.getElementById("hb-update")) return;
    const host = document.querySelector("main") || document.body;
    const el = document.createElement("div");
    el.id = "hb-update"; el.className = "hb-update"; el.setAttribute("role", "status");
    const Q = String.fromCharCode(34);   // spelled out: a quote inside a regex trips test_js_syntax
    const esc = s => String(s ?? "").replace(new RegExp("[&<>" + Q + "]", "g"), c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", [Q]: "&quot;" }[c]));
    el.innerHTML = `<div class="hb-update-t">
        <b>holobuilder ${esc(u.latest)} is out${u.state === "required" ? " (required)" : ""}</b>
        ${u.notes ? `<span class="hb-update-n" title="${esc(u.notes)}">${esc(u.notes)}</span>` : ""}
        <small>The app closes, updates in a few seconds and opens again. Your data stays.</small></div>
      <button class="btn small" type="button" data-now>Update now</button>
      <button class="hb-update-x" type="button" data-later aria-label="Dismiss this update notice" title="Dismiss">×</button>`;
    el.querySelector("[data-later]").onclick = () => {
      el.remove();
      if (window.pywebview && window.pywebview.api.dismiss_update) window.pywebview.api.dismiss_update().catch(() => {});
    };
    el.querySelector("[data-now]").onclick = e => { e.target.disabled = true; e.target.textContent = "Updating…"; window.pywebview.api.update_now(); };
    host.prepend(el);
  };
  document.querySelectorAll("nav.rail[data-page]").forEach(render);
  window.HBShell = { SECTIONS, idle: () => idle };

  // Idle: no pointer, key, wheel or touch input for IDLE_MS, or the window in the
  // background, or the page hidden. The pages' decorative motion stops while idle (the Live2D
  // leader in hb-live2d.js, the cards' glint in hb-card.css): drawn at the display's refresh
  // rate, it kept the GPU and renderer processes at about two cores on a page nobody touched.
  // Work in progress (the search bar, the chibi runner) keeps moving. `<html data-idle>` and
  // the `hb:idle` / `hb:active` events on `document` carry the state.
  const IDLE_MS = 30000;
  let idle = false, timer = 0, last = -Infinity;
  const set = v => {
    if (v === idle) return;
    idle = v;
    document.documentElement.toggleAttribute("data-idle", v);
    document.dispatchEvent(new Event(v ? "hb:idle" : "hb:active"));
  };
  const rest = () => { clearTimeout(timer); set(true); };
  const poke = () => {
    const now = performance.now();
    if (!idle && now - last < 1000) return;      // a pointer moving fires dozens a second
    last = now;
    clearTimeout(timer);
    set(false);
    timer = setTimeout(rest, IDLE_MS);
  };
  // not `scroll`: the pages scroll themselves (a search's results), and a person scrolling
  // also sends a wheel, key, pointer or touch event
  for (const ev of ["pointermove", "pointerdown", "keydown", "wheel", "touchstart"])
    window.addEventListener(ev, poke, { capture: true, passive: true });
  window.addEventListener("focus", poke);
  window.addEventListener("blur", rest);
  document.addEventListener("visibilitychange", () => (document.hidden ? rest() : poke()));
  poke();
})();
