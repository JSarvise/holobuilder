"""Exceptions shared by the library code and the command-line entry points.

Library code raises a normal exception and never ends the process: a web handler or a job
thread turns it into an answer the page shows, and only a command-line entry point turns it
into an exit (`run_cli`). A `MissingInput` that reaches the top of a script uncaught prints its
one-line message, without a traceback, and the process exits with code 1 (the excepthook below,
installed when this module is imported; every script imports it through `hbuilder.paths`).
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any, NoReturn


class MissingInput(RuntimeError):
    """A required input is missing or unreadable: a gitignored dump the refresh writes, the
    chart library, the account file, the game install. The message names it and, where
    there is one, the command that produces it."""


class CheckoutMismatch(RuntimeError):
    """`hbuilder` was imported from one checkout while the running script belongs to
    another, so every path would point at the other checkout's data."""


def run_cli(main: Callable[..., Any], *args: Any) -> NoReturn:
    """`raise SystemExit(main(*args))` for a command-line entry point. A `MissingInput`
    ends the process as `SystemExit(message)` does: the message on stderr, exit code 1."""
    try:
        code = main(*args)
    except MissingInput as exc:
        raise SystemExit(str(exc)) from None
    raise SystemExit(code)


def _missing_input_hook(kind, value, tb, _previous=sys.excepthook) -> None:
    """The process's uncaught-exception hook: a `MissingInput` prints its message only (the
    interpreter still exits with code 1); anything else keeps the full traceback."""
    if isinstance(value, MissingInput):
        print(str(value), file=sys.stderr)
        return
    _previous(kind, value, tb)


if sys.excepthook is sys.__excepthook__:
    sys.excepthook = _missing_input_hook
