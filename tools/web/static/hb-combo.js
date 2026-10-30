/* Type-to-search for every <select> on the page, with inline prediction. Each select stays in the DOM, hidden, and remains the source of truth:
   the page's own code keeps reading `.value` and listening for `change`. In front of
   it sits an input: typing filters the options (prefix matches first, then word
   starts, then anywhere), the best prefix match is completed inline (the predicted
   rest is selected, so typing on replaces it), arrows move, Enter picks, Escape
   restores. Selects added later (re-rendered panels) are enhanced by an observer;
   `data-hbc="off"` opts one out. */
(() => {
  "use strict";
  const NATIVE = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value");
  const NATIVE_IDX = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "selectedIndex");
  const QUOTE = String.fromCharCode(34), APOS = String.fromCharCode(39);
  const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", [QUOTE]: "&quot;", [APOS]: "&#39;" };
  const ESC_RE = new RegExp("[&<>" + QUOTE + APOS + "]", "g");
  const esc = s => String(s ?? "").replace(ESC_RE, c => ESC[c]);
  let seq = 0;
  const live = new Set();   // {wrap, list}: a removed select takes its popup with it

  function options(sel) {
    const out = [];
    for (const o of sel.options) {
      if (o.hidden) continue;
      const g = o.parentElement && o.parentElement.tagName === "OPTGROUP" ? o.parentElement.label : "";
      out.push({ value: o.value, label: o.textContent.trim(), disabled: o.disabled || (o.parentElement && o.parentElement.disabled), group: g, el: o });
    }
    return out;
  }

  // rank: 0 prefix of the whole label, 1 a word starts with it, 2 anywhere; -1 no match
  function rank(label, q) {
    if (!q) return 0;
    const l = label.toLowerCase();
    if (l.startsWith(q)) return 0;
    const at = l.indexOf(q);
    if (at < 0) return -1;
    return /[\s\-(/·:,]/.test(l[at - 1] || "") ? 1 : 2;
  }

  function mark(label, q) {
    if (!q) return esc(label);
    const at = label.toLowerCase().indexOf(q);
    if (at < 0) return esc(label);
    return esc(label.slice(0, at)) + "<mark>" + esc(label.slice(at, at + q.length)) + "</mark>" + esc(label.slice(at + q.length));
  }

  function enhance(sel) {
    if (sel.dataset.hbc || sel.multiple || sel.size > 1) return;
    sel.dataset.hbc = "on";
    const id = "hbc" + (++seq);
    const wrap = document.createElement("span");
    wrap.className = "hbc";
    const input = document.createElement("input");
    input.type = "text";
    input.className = "hbc-in";
    input.autocomplete = "off";
    input.spellcheck = false;
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-autocomplete", "both");
    input.setAttribute("aria-expanded", "false");
    input.setAttribute("aria-controls", id);
    // a <label> that wraps the select also holds its options: name it by the label's own text
    let label = sel.getAttribute("aria-label") || "";
    if (!label && sel.labels && sel.labels[0]) {
      const c = sel.labels[0].cloneNode(true);
      c.querySelectorAll("select,.hbc").forEach(n => n.remove());
      label = c.textContent.replace(/\s+/g, " ").trim();
    }
    if (label) input.setAttribute("aria-label", label);
    if (sel.title) input.title = sel.title;
    const list = document.createElement("ul");
    list.className = "hbc-list";
    list.id = id;
    list.setAttribute("role", "listbox");
    list.setAttribute("popover", "manual");   // the top layer: above an open <dialog>, never clipped by it
    sel.parentNode.insertBefore(wrap, sel);
    wrap.append(input, sel);
    document.body.appendChild(list);
    sel.classList.add("hbc-native");
    sel.tabIndex = -1;
    if (sel.id) {   // a <label for> on the select focuses the input instead
      document.querySelectorAll(`label[for="${CSS.escape(sel.id)}"]`).forEach(l => l.addEventListener("click", e => { e.preventDefault(); input.focus(); }));
    }

    let shown = [], active = -1, open = false;

    const current = () => sel.options[NATIVE_IDX.get.call(sel)];
    const sync = () => {
      const o = current();
      if (document.activeElement !== input || !open) input.value = o ? o.textContent.trim() : "";
      input.disabled = sel.disabled;
      wrap.classList.toggle("off", sel.disabled);
      input.placeholder = o ? "" : "Type to search";
    };

    // the page sets `.value` / `.selectedIndex` from code: keep the input in step
    Object.defineProperty(sel, "value", { configurable: true,
      get() { return NATIVE.get.call(this); },
      set(v) { NATIVE.set.call(this, v); sync(); } });
    Object.defineProperty(sel, "selectedIndex", { configurable: true,
      get() { return NATIVE_IDX.get.call(this); },
      set(v) { NATIVE_IDX.set.call(this, v); sync(); } });
    new MutationObserver(() => { sync(); if (open) draw(filterText()); })
      .observe(sel, { childList: true, subtree: true, attributes: true, attributeFilter: ["disabled", "selected", "label"] });

    function place() {
      const r = input.getBoundingClientRect();
      const below = innerHeight - r.bottom - 12, above = r.top - 12;
      const up = below < 220 && above > below;
      const h = Math.max(120, Math.min(340, up ? above : below));
      list.style.left = Math.max(8, Math.min(r.left, innerWidth - Math.max(r.width, 200) - 8)) + "px";
      list.style.minWidth = r.width + "px";
      list.style.maxHeight = h + "px";
      list.style.top = up ? "" : (r.bottom + 4) + "px";
      list.style.bottom = up ? (innerHeight - r.top + 4) + "px" : "";
    }

    let typed = null;   // what the person typed (the input may also hold a prediction)
    const filterText = () => (typed ?? "").trim().toLowerCase();

    function draw(q) {
      const all = options(sel);
      shown = all.map((o, i) => ({ ...o, r: rank(o.label, q), i }))
        .filter(o => o.r >= 0)
        .sort((a, b) => q ? (a.r - b.r) || (a.i - b.i) : a.i - b.i);
      const cur = current();
      if (active >= shown.length) active = shown.length - 1;
      if (active < 0 && shown.length) {
        const ci = shown.findIndex(o => o.el === cur);
        active = q ? shown.findIndex(o => !o.disabled) : Math.max(0, ci);
      }
      let lastGroup = null;
      list.innerHTML = shown.length ? shown.map((o, k) => {
        const head = !q && o.group && o.group !== lastGroup ? `<li class="hbc-g" role="presentation">${esc(o.group)}</li>` : "";
        lastGroup = o.group;
        return head + `<li role="option" id="${id}-${k}" data-k="${k}" class="${k === active ? "on" : ""}${o.disabled ? " dis" : ""}"
          aria-selected="${o.el === cur}"${o.disabled ? ' aria-disabled="true"' : ""}>${mark(o.label, q)}</li>`;
      }).join("") : `<li class="hbc-none" role="presentation">No match for “${esc(typed)}”</li>`;
      input.setAttribute("aria-activedescendant", active >= 0 ? `${id}-${active}` : "");
      const on = list.querySelector("li.on");
      if (on) on.scrollIntoView({ block: "nearest" });
    }

    function show() {
      if (sel.disabled) return;
      open = true; input.setAttribute("aria-expanded", "true");
      // inside an open modal <dialog> the list must live in the dialog's subtree: everything
      // outside a modal dialog is inert, so a list hung on <body> shows but takes no click
      // (e.g. the leader dropdown in More options); the popover keeps it in the
      // top layer either way. Re-shown on every open so it is the front-most top-layer element.
      const host = sel.closest("dialog[open]") || document.body;
      if (list.matches(":popover-open")) list.hidePopover();
      if (list.parentNode !== host) host.appendChild(list);
      list.showPopover();
      place(); draw(filterText());
    }
    function hide(restore = true) {
      open = false; input.setAttribute("aria-expanded", "false");
      if (list.matches(":popover-open")) list.hidePopover();
      typed = null; active = -1;
      if (restore) { const o = current(); input.value = o ? o.textContent.trim() : ""; }
    }
    function pick(o) {
      if (!o || o.disabled) return;
      const changed = NATIVE.get.call(sel) !== o.value;
      NATIVE.set.call(sel, o.value);
      hide();
      if (changed) {
        sel.dispatchEvent(new Event("input", { bubbles: true }));
        sel.dispatchEvent(new Event("change", { bubbles: true }));
      }
    }

    input.addEventListener("focus", () => { typed = ""; show(); input.select(); });
    input.addEventListener("mousedown", () => { if (document.activeElement === input && !open) { typed = ""; show(); } });
    input.addEventListener("input", e => {
      typed = input.value;
      active = -1;
      if (!open) show(); else draw(filterText());
      // predict: complete the best prefix match inline, the guessed rest selected
      const q = filterText();
      if (q && e.inputType && e.inputType.startsWith("insert") && shown.length && shown[0].r === 0 && !shown[0].disabled) {
        const full = shown[0].label;
        if (full.length > typed.length) { input.value = typed + full.slice(typed.length); input.setSelectionRange(typed.length, full.length); }
      }
    });
    input.addEventListener("keydown", e => {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        if (!open) { typed = ""; show(); return; }
        if (!shown.length) return;
        const step = e.key === "ArrowDown" ? 1 : -1;
        let k = active;
        for (let n = 0; n < shown.length; n++) { k = (k + step + shown.length) % shown.length; if (!shown[k].disabled) break; }
        active = k; draw(filterText());
      } else if (e.key === "Enter") {
        if (open) { e.preventDefault(); pick(shown[active] || shown.find(o => !o.disabled)); }
      } else if (e.key === "Escape") {
        if (open) { e.preventDefault(); e.stopPropagation(); hide(); input.select(); }
      } else if (e.key === "Tab") {
        if (open && typed && shown[active]) pick(shown[active]); else hide();
      }
    });
    input.addEventListener("blur", () => setTimeout(() => { if (document.activeElement !== input) hide(); }, 120));
    list.addEventListener("mousedown", e => e.preventDefault());   // keep the focus on the input
    list.addEventListener("click", e => { const li = e.target.closest("li[data-k]"); if (li) pick(shown[+li.dataset.k]); });
    addEventListener("resize", () => open && place());
    addEventListener("scroll", e => { if (open && !list.contains(e.target)) place(); }, true);
    live.add({ wrap, list });
    sync();
  }

  function scan(root) {
    if (root.tagName === "SELECT") { if (root.dataset.hbc !== "off") enhance(root); return; }
    if (root.querySelectorAll) root.querySelectorAll("select:not([data-hbc])").forEach(enhance);
  }
  function start() {
    scan(document.body);
    new MutationObserver(ms => {
      let gone = false;
      for (const m of ms) { for (const n of m.addedNodes) if (n.nodeType === 1) scan(n); if (m.removedNodes.length) gone = true; }
      if (gone) for (const e of live) if (!e.wrap.isConnected) { e.list.remove(); live.delete(e); }
    }).observe(document.body, { childList: true, subtree: true });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
  window.HBCombo = { enhance };
})();
