// The session key: this server answers a change (any request but GET or HEAD) only when it
// carries the key it put in this page's <meta name="hb-session">, so another page open in the
// same browser cannot change anything here (hbweb/session.py). Loaded first in every page, it
// adds the key to every non-GET request this page makes to its own server.
(function () {
  const meta = document.querySelector('meta[name="hb-session"]');
  const key = meta ? meta.content : "";
  if (!key) return;
  const send = window.fetch.bind(window);
  window.fetch = function (input, init) {
    const request = input instanceof Request ? input : null;
    const method = String((init && init.method) || (request && request.method) || "GET").toUpperCase();
    const url = new URL(request ? request.url : String(input), location.href);
    if (method === "GET" || method === "HEAD" || url.origin !== location.origin) return send(input, init);
    const headers = new Headers((init && init.headers) || (request ? request.headers : undefined));
    headers.set("X-HB-Session", key);
    return send(input, Object.assign({}, init, { headers }));
  };
})();
