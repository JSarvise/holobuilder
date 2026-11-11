"""`model_version`: the one string every certificate and optimize response
carries.

Two numbers are comparable only if their `inputs_hash` match, and the
`inputs_hash` recipe folds this string in, so bumping it invalidates every
older certificate on purpose. Bump it whenever a scoring decision or the
board-plan function changes; never for a refactor that reproduces the same
floats.

The current version's objective: a fixed board plan per (character, role,
budget, singer context), per-member support, mobile as the default platform
(FLICK 1050 permil; PC 1000), and a leader outfit's `score_support` rider scored
as a second, always-on, team-wide support on its own gate (weight 1.0, occupancy
1.0, ACTIVE bucket of B).
"""

from __future__ import annotations

MODEL_VERSION = "objective-v2.2"

__all__ = ["MODEL_VERSION"]
