# Security

## Reporting a problem

Please **do not open a public issue** for a security problem. Use GitHub's private
[Report a vulnerability](../../security/advisories/new) form on this repository instead. You will get
an answer, and a fix will be released before the details are made public.

Worth reporting, for example:

- a way to read or change another user's data, or to use the app's server without an access key;
- an update or a data file the app would accept without the release signature;
- the local web app answering anyone but this PC, or writing outside its own folders;
- personal data (account files, keys, tokens) leaving the PC or ending up in logs or diagnostics.

## What the app does to stay safe

- The local web app listens on `127.0.0.1` only.
- Updates and shared data are checked against an Ed25519 signature before use; an older manifest
  than the one already held is refused.
- Your account file is read in place at each refresh and is sent only to the game's own servers.
  See [docs/privacy.md](docs/privacy.md).
- "Copy diagnostics" hides your account file path, Steam ID, access key, Windows user folder and
  anything that looks like a token.

## Supported versions

Only the latest app version. The app updates itself when it starts.
