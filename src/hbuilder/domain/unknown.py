"""The absent-vs-zero seam (strict and explicit).

JSON `null` is the only on-disk encoding for "unknown / never populated", and
`UNKNOWN` (never Python's `None`) is its in-memory sentinel.
`UNKNOWN.__bool__` raises `TypeError` and `__float__`/`__int__` raise
`SchemaError`, so an unpopulated value can never be coerced into a falsy or zero
value downstream.
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
            "UNKNOWN has no truthiness — call is_known(value) before branching on it, never `if value:`"
        )

    def __float__(self) -> float:
        raise SchemaError("cannot convert UNKNOWN to float — this value was never populated")

    def __int__(self) -> int:
        raise SchemaError("cannot convert UNKNOWN to int — this value was never populated")

    def __reduce__(self) -> str:
        """Survive `pickle` and `copy` as the SAME object.

        `is_known` is an identity test, so a copy of the sentinel would read as
        *known* and a fabricated number would be scored silently. The process
        pool (`spawn`) ships every `Card` to its workers through `pickle`.
        Returning a bare string is pickle's singleton protocol: it records a
        lookup of the global `UNKNOWN` in this module, as for `None`; `copy` and
        `deepcopy` honour `__reduce__` too.
        """
        return "UNKNOWN"


UNKNOWN = Unknown()


def is_known(value: object) -> bool:
    """True unless `value` is the `UNKNOWN` sentinel."""
    return value is not UNKNOWN
