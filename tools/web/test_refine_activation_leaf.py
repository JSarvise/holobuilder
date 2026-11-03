"""The leaf's activation refinement (`RefineContext.refine_activation`, per batch
through `RefinePool.batch_score(refine_activation=)`) on the local account:
never below the plain develop, and the plain path unchanged by the option."""
from __future__ import annotations

import argparse
import dataclasses
import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _account():
    spec = importlib.util.spec_from_file_location("_acct_test", REPO / "tools/recon/probes/two_stage_account.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.skipif(not (REPO / "data/owned.json").exists() or not (REPO / "state/collection.json").exists(),
                    reason="needs the account's data (gitignored)")
def test_refined_scores_are_never_below_the_plain_ones_and_plain_is_untouched():
    from hbuilder.domain.team import Team
    from hbuilder.solver import parallel_refine as pr

    built = _account().build(argparse.Namespace(song="m0334", budget=453, uptime="finite", platform="mobile"))
    by_id = built["by_id"]
    # the account's six strongest cards (the plan cache's nominal team), each of three leading
    lead, members = built["ctx"].plan_cache_seed
    six = [lead, *members]
    teams = [Team(leader=by_id[six[k]], members=tuple(by_id[c] for c in six if c != six[k]))
             for k in range(3)]
    with pr.RefinePool(dataclasses.replace(built["ctx"], use_plan_cache=False), workers=1) as pool:
        plain = pool.batch_score(teams)
        refined = pool.batch_score(teams, refine_activation=True)
        assert pool.batch_score(teams, refine_activation=False) == plain
        assert pool.batch_score(teams) == plain
    assert all(r >= p for r, p in zip(refined, plain))
    assert any(r > p for r, p in zip(refined, plain))
