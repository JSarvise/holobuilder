# Privacy

## What stays on your PC

Both with the app and when you run it yourself:

- **Your game account file.** It is read on each refresh and used only to log into the game's own
  servers. It is never copied, uploaded or sent to the holobuilder server.
- **Your account data** (cards, levels, boards, items, pulls). It is read straight from the game's
  servers onto your PC. The holobuilder server never sees it.
- **Settings, search results, the pictures.**

## What goes to the game's servers

The same requests the game's own client makes to read your account, sent from your PC with your
credential, in the game client's own format. When you run it yourself, the refresh also downloads
the game's data (master tables, charts, pictures) from the game's own servers, one request per
second, the way the client downloads them. Using third-party tools may go against the game's
terms of service; use holobuilder at your own risk.

## Running it yourself

Nothing at all is sent to the holobuilder server: there is no sign-in, and the game data and
pictures come from your game install and the game's own servers. The other downloads are the
ones every page makes in the browser: open-source JavaScript libraries (pixi.js, three.js and a
Live2D renderer) from the jsDelivr CDN and fonts from Google Fonts, plus the Live2D Cubism Core,
which the local server fetches once from Live2D's own site.

## The app: what the holobuilder server keeps

The server only decides who may download the app and its shared game data. Per user it keeps:

- your Google account's **email** and its internal id (from Google sign-in),
- the **username** you choose (not your real name),
- your approval status, when you joined and were last seen, and which app **version** you use,
- for each PC, a **hash** of its access key (never the key itself).

It stores **no IP addresses** and keeps no access logs. Requests pass through Cloudflare, which
protects the server and, like any network provider, sees the address a request comes from.

## The app: what it downloads

- the app and its updates (signed; anything unsigned is refused),
- the shared game data and art (the same for every user, checked against signed hashes),
- the Live2D Cubism Core, straight from Live2D's own site,
- like any page, the JavaScript libraries and fonts the pages use (jsDelivr, Google Fonts).

## Leaving

- **The app:** uninstall it from Windows' *Apps* list. To have your user removed from the server,
  open an issue or contact the maintainer.
- **Running it yourself:** delete the folder. Nothing was ever registered anywhere.
