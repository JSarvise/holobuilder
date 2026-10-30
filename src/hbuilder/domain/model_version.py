"""`model_version` — the one string every certificate and optimize response
carries.

Two numbers are comparable only if their `inputs_hash` match, and the
`inputs_hash` recipe folds this string in — so bumping it here invalidates
every older certificate on purpose. Bump it whenever a scoring decision or
the board-plan function changes; never for a refactor that reproduces the
same floats.

What each version covers:
  objective-v2.2        costume score_support riders are scored: a leader
                        outfit's `score_support` rider is a second, always-on,
                        team-wide support on the rider's own gate, folded
                        exactly like a principal costume score_support
                        (weight 1.0, occupancy 1.0, ACTIVE bucket of B).
                        Floats match v2.1 for every leader whose outfit has
                        no such rider.
  objective-v2.1        mobile is the default platform (FLICK 1050 permil;
                        PC 1000).
  objective-v2.0-draft  fixed board plan, per-member support, singer context.
"""
from __future__ import annotations

MODEL_VERSION = "objective-v2.2"

__all__ = ["MODEL_VERSION"]
