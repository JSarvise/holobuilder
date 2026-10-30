"""The leaf's activation refinement (`RefineContext.refine_activation`, per batch
through `RefinePool.batch_score(refine_activation=)`) on the local account:
never below the plain develop, and the plain path unchanged by the option."""
from __future__ import annotations

import argparse
import dataclasses
import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CACHED = REPO / "tools/recon/proxy/dumps/two_stage/account_song_m0334.json"


def _account():
    spec = importlib.util.spec_from_file_location("_acct_test", REPO / "tools/recon/probes/two_stage_account.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.skipif(not CACHED.exists(), reason="the account answers cache is not on this checkout")
def test_refined_scores_are_never_below_the_plain_ones_and_plain_is_untouched():
    from hbuilder.domain.team import Team
    from hbuilder.solver import parallel_refine as pr

    built = _account().build(argparse.Namespace(song="m0334", budget=453, uptime="finite", platform="mobile"))
    by_id = built["by_id"]
    rows = json.loads(CACHED.read_text(encoding="utf-8"))["rows"][:3]
    teams = [Team(leader=by_id[r["leader"]], members=tuple(by_id[m] for m in r["members"])) for r in rows]
    with pr.RefinePool(dataclasses.replace(built["ctx"], use_plan_cache=False), workers=1) as pool:
        plain = pool.batch_score(teams)
        refined = pool.batch_score(teams, refine_activation=True)
        assert pool.batch_score(teams, refine_activation=False) == plain
        assert pool.batch_score(teams) == plain
    assert all(r >= p for r, p in zip(refined, plain))
    assert any(r > p for r, p in zip(refined, plain))      # about +0.92 % on the m0334 winner
