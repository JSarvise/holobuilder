# Your own server

This page is for building and serving **your own installer**: the official app downloads its
updates and the shared game data from a server, and you can run your own.

To simply use holobuilder on your PC without any server, you do not need this:
see [Run it yourself](run-locally.md).

## The pieces

They are all in this repository:

| Piece | Where | Configure with |
|---|---|---|
| Sign-in and approval (Google ID tokens, per-PC access keys, admin routes) | `tools/app/server/hb_auth.py` | `HB_GOOGLE_CLIENT_IDS`, `HB_ADMIN_EMAILS`, `HB_API_TOKEN`, `HB_AUTH_DB`, `HB_AUTH_PORT` |
| Web front (TLS, `forward_auth` to hb_auth's `/check`, static `/app` and `/content`) | any reverse proxy, e.g. Caddy | see the routes `hb_auth.py` documents |
| Release signing (Ed25519) | `tools/app/signing.py` | `python tools/app/signing.py --new-key` once; the public key goes in `RELEASE_PUBKEY` |
| Publishing the shared game data | `tools/app/publish.py` | `HB_PUBLISH_SSH=user@host` (content-addressed store under `/srv/holobuilder/content`) |
| Building the app and the installer | `tools/app/build_app.py`, `tools/app/build_setup.py` | `HB_SERVER_URL=https://your.server` when building |
| Event borders tracker (optional) | `tools/recon/proxy/event_tracker.py` | guest accounts and, if your host's IP is refused by the game, `HB_API_PROXY` |

## Typical order

1. Set up a clone on a PC with the game and refresh it, as in [Run it yourself](run-locally.md):
   the shared data you publish is what that refresh builds.
2. Create a Google OAuth *desktop* client and put it in `state/google_oauth.json`.
3. Make your signing key (`signing.py --new-key`).
4. Start `hb_auth.py` behind your reverse proxy.
5. `publish.py` the shared data.
6. Build and publish the app (`build_app.py X.Y.Z --publish`, which runs the tests and a smoke
   start first) and the installer, both with your `HB_SERVER_URL`.

Keep `state/` private: it holds your credentials, your signing key and your server's token.
