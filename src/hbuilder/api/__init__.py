"""hbuilder.api: the server-request side of the tool.

Everything here is acquisition/read-shaped, kept apart from the solver so the
solver's `dependencies = []` install is never coupled to it. Nothing in this
subpackage reads or writes `data/owned.json` or `state/collection.json` (the
user's own account data), and nothing here is
wired into the `solve`/`gates` command path.

Currently the only module is `signing`, the scaffold for the
`x-app-request-signature` HMAC that score-submission RPCs require. It refuses to
run until a real capture confirms the algorithm; see its docstring.
"""
