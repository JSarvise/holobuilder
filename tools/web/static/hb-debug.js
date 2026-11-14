// Export search: a JSON file with what the page asked the server and what it got back, to
// reproduce a search exactly when something looks wrong. Loaded before the page's own scripts:
// it keeps the page's last POST /api/* requests (body, status, answer) and the job streams'
// plan / result / error events, and `hbExportSearch()` saves them with the app version and the
// page's options. Nothing leaves the PC: the file is saved where the browser saves downloads.
(() => {
  const KEEP = 40; // requests kept, newest last
  const log = [];
  const jobs = new Map(); // job id -> {plan, result, errors}
  const parse = s => { try { return typeof s === "string" ? JSON.parse(s) : s ?? null; } catch { return s; } };
  const push = entry => { log.push(entry); if (log.length > KEEP) log.shift(); };

  const fetch0 = window.fetch.bind(window);
  window.fetch = async (url, opts) => {
    const u = String(url instanceof Request ? url.url : url);
    const method = ((opts && opts.method) || "GET").toUpperCase();
    if (method !== "POST" || !u.startsWith("/api/")) return fetch0(url, opts);
    const t0 = performance.now();
    const entry = { at: new Date().toISOString(), method, url: u, body: parse(opts && opts.body) };
    push(entry);
    try {
      const res = await fetch0(url, opts);
      entry.status = res.status;
      entry.ms = Math.round(performance.now() - t0);
      res.clone().text().then(t => { entry.answer = parse(t); }).catch(() => {});
      return res;
    } catch (err) {
      entry.error = String(err);
      throw err;
    }
  };

  const ES0 = window.EventSource;
  if (ES0) {
    window.EventSource = function (url, init) {
      const es = new ES0(url, init);
      const m = String(url).match(/\/events\/([^/?#]+)/);
      if (m) {
        const job = { plan: null, result: null, errors: [] };
        jobs.set(m[1], job);
        if (jobs.size > KEEP) jobs.delete(jobs.keys().next().value);
        es.addEventListener("message", e => {
          const ev = parse(e.data);
          if (!ev || typeof ev !== "object") return;
          if (ev.type === "plan") job.plan = ev;
          else if (ev.type === "result") job.result = ev;
          else if (ev.type === "error" || ev.type === "cancelled") job.errors.push(ev);
        });
      }
      return es;
    };
    window.EventSource.prototype = ES0.prototype;
  }

  const stamp = d => d.toISOString().slice(0, 16).replace(/[-:]/g, "").replace("T", "-");
  window.hbExportSearch = async () => {
    let version = null;
    try { version = (await fetch0("/api/version").then(r => r.json())).version; } catch { /* an older server */ }
    let options = null;
    try { options = JSON.parse(localStorage.getItem("hb.optimizer") || "null"); } catch { /* storage blocked */ }
    const doc = {
      kind: "holobuilder-search-export", format: 1, version, exported_at: new Date().toISOString(),
      page: location.pathname + location.search, options,
      requests: log, jobs: Object.fromEntries(jobs),
    };
    const goal = new URLSearchParams(location.search).get("goal") || "score";
    const blob = new Blob([JSON.stringify(doc, null, 1)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `holobuilder-${goal}-search-${stamp(new Date())}.json`;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 4000);
  };
  // the button any page puts next to an answer: `hbExportButton()` is its markup
  window.hbExportButton = () => `<button class="btn small hb-xsearch" type="button" data-xsearch
    title="Save this search as a .json file: your options, every request this page made and what it got back. Attach it when you report a problem."><svg class="i s"><use href="#i-down"/></svg>Export search</button>`;
  document.addEventListener("click", e => {
    const b = e.target.closest && e.target.closest("[data-xsearch]");
    if (!b) return;
    e.preventDefault();
    b.disabled = true;
    window.hbExportSearch().finally(() => { b.disabled = false; });
  });
})();
