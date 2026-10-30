"""The absent-vs-zero seam (strict and explicit).

Every schema field must be present in every card entry in `cards.json`. A
missing key is a `SchemaError` naming the offending card id and field. JSON
`null` is the *only* on-disk encoding for "unknown / never populated", and
`UNKNOWN` — never Python's `None` — is the in-memory sentinel for that
state.

`UNKNOWN.__bool__` raises `TypeError` and `UNKNOWN.__float__`/`__int__`
raise `SchemaError`, so a value that was never populated cannot be silently
coerced into a falsy/zero value anywhere downstream — the failure is loud
by construction, not merely discouraged by convention.
"""

from __future__ import annotations

from hbuilder.domain.schema import SchemaError


class Unknown:
    """The type of the `UNKNOWN` sentinel. Never instantiate directly —
    import and use the module-level `UNKNOWN` singleton."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "UNKNOWN"

    def __bool__(self) -> bool:
        raise TypeError(
            "UNKNOWN has no truthiness — call is_known(value) before "
            "branching on it, never `if value:`"
        )

    def __float__(self) -> float:
        raise SchemaError(
            "cannot convert UNKNOWN to float — this value was never populated"
        )

    def __int__(self) -> int:
        raise SchemaError(
            "cannot convert UNKNOWN to int — this value was never populated"
        )

    def __reduce__(self) -> str:
        """Survive `pickle` and `copy` as the SAME object.

        `is_known` is `value is not UNKNOWN` — an identity test — so a copy of
        the sentinel is not the sentinel, and every unpopulated field in a
        copied `Card` would silently read as *known*. Without this, the failure
        is not an exception: `is_known` returns `True`, the branch that exists
        to refuse a fabricated number is skipped, and the run produces a score.

        That is not hypothetical. `multiprocessing` on Windows uses `spawn`, so
        a parallel exact search ships every `Card` to its workers through
        `pickle`; without this method, `pickle.loads(pickle.dumps(UNKNOWN))`
        produces a second `Unknown` instance and `is_known` returns `True` for
        it.

        Returning a bare string is pickle's documented singleton protocol: the
        pickle records a lookup of the global named `UNKNOWN` in this module
        rather than an instance to reconstruct, which is exactly how `None`,
        `Ellipsis` and `NotImplemented` survive a round trip. `copy` honours
        `__reduce__` too, so `copy`/`deepcopy` are covered by the same three
        lines.
        """
        return "UNKNOWN"


UNKNOWN = Unknown()


def is_known(value: object) -> bool:
    """True unless `value` is the `UNKNOWN` sentinel."""
    return value is not UNKNOWN


def require_known(value: object, card_id: str, field: str) -> float:
    """Return `value` coerced to float, or raise `SchemaError` naming
    `card_id` and `field` when `value` is `UNKNOWN`."""
    if value is UNKNOWN:
        raise SchemaError(
            f"field '{field}' was never populated (UNKNOWN)",
            card_id=card_id,
            field=field,
        )
    return float(value)
