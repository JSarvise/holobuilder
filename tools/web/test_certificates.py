"""Certificates on every song-mode optimize response.

What is asserted, and why each assertion is not circular:

* a song-mode `/solve` payload carries `model_version` (the ONE constant,
  `hbuilder.domain.model_version.MODEL_VERSION`), `inputs_hash` and a
  `certificate_basis`; every row carries a `certificate` whose status is
  HEURISTIC when nothing on file matches — and it names the generator and N;
* an index seeded with one EXACT and one BOUNDED row (no filesystem: the
  package's no-writes guard forbids fixture files, so the rows are injected
  through a `CertificateIndex` subclass) upgrades the matching rows to the
  right status ONLY when the hash AND the basis tuple match; a hash mismatch
  leaves the row HEURISTIC with a `certificate_note` naming the difference —
  the "same instant, same basis" guard;
* a row that scores ABOVE an EXACT certificate on the same hash is reported
  as status "ERROR" (refined > exact on the same hash is a bug), never
  as a better team;
* the GUARD test: the server's `inputs_hash` for (Cards = maxed, uptime
  finite, platform mobile, song m0334) equals the hash the per-leader-table
  probe computes for the same basis (`leaf_inputs.load_context` +
  platform — the probe's own expression, imported, not re-typed). If the two
  ever differ the failure message prints the per-file digests so the differing
  field is visible, not guessed.

    python -m pytest tools/web/test_certificates.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import certificate as cert_mod
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))
from support.builders import make_scoring_config  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
MUSIC_ID = "m9991"
CALLI_PDW = "card-04001-5-uniq-0042-00"
M1_ROSTER = [
    CALLI_PDW,
    CALLI_PDW,
    "card-00032-5-uniq-0026-00",
    "card-04003-5-uniq-0044-00",
    "card-00007-5-uniq-0008-00",
    "card-00006-5-uniq-0007-00",
]


# Deliberately no test that a per-leader table on disk is still on today's basis --
# that would be a freshness alarm on a frozen research result, red after every new
# card. The hash RECIPE is pinned by `test_solve_song_mode_hash_equals_the_probes_hash`,
# and what the product does with a table on another basis (no certificate, a note
# naming the hash) by `test_hash_mismatch_is_heuristic_with_a_note_never_a_gap`.


# ---------------------------------------------------------------------------
# the members-first enumeration key: per-file lines + ONE chart
# ---------------------------------------------------------------------------
def test_enumeration_key_recipe_chart_digest_and_tolerance(tmp_path):
    import hashlib

    import members_first_store  # the recon-side writer (tools/web/ writes nothing)

    charts = {
        "schema_version": "charts-2",
        "difficulty": "EXPERT",
        "charts": {
            "m0001/EXPERT": {"music_id": "m0001", "difficulty": "EXPERT", "notes": [[1, 2], [3, 4]]},
            "m0002/EXPERT": {"music_id": "m0002", "difficulty": "EXPERT", "notes": [[5, 6]]},
        },
    }
    path = tmp_path / "charts.json"
    members_first_store.write_text_file(path, json.dumps(charts, indent=2))
    # chart_digest = sha256 of the canonical JSON of THAT song's entry
    canon = json.dumps(charts["charts"]["m0001/EXPERT"], sort_keys=True, separators=(",", ":"))
    assert cert_mod.chart_digest("m0001", charts_path=path) == hashlib.sha256(canon.encode()).hexdigest()
    assert cert_mod.chart_digest("m0002", charts_path=path) != cert_mod.chart_digest(
        "m0001", charts_path=path
    )
    assert cert_mod.chart_digest("m0003", charts_path=path) is None
    # a new song APPENDED does not change an existing song's digest
    charts["charts"]["m0009/EXPERT"] = {"music_id": "m0009", "difficulty": "EXPERT", "notes": []}
    path2 = tmp_path / "charts2.json"
    members_first_store.write_text_file(path2, json.dumps(charts))
    assert cert_mod.chart_digest("m0001", charts_path=path2) == cert_mod.chart_digest(
        "m0001", charts_path=path
    )
    # the list shape (entries with music_id) is read too
    path3 = tmp_path / "charts3.json"
    members_first_store.write_text_file(path3, json.dumps(list(charts["charts"].values())))
    assert cert_mod.chart_digest("m0001", charts_path=path3) == cert_mod.chart_digest(
        "m0001", charts_path=path
    )
    with pytest.raises(OSError):
        cert_mod.chart_digest("m0001", charts_path=tmp_path / "missing.json")
    # the keys: the recipe's lines (cards, collection, board, skilltree,
    # master_stn, cfg, [chart,] model_version), one per line, sha256
    per = {
        "cards": "a" * 64,
        "board": "b" * 64,
        "charts": "c" * 64,
        "skilltree": "d" * 64,
        "master_stn": "e" * 64,
        "collection": "f" * 64,
        "cfg": "g" * 64,
    }
    ref_lines = [
        f"cards={'a' * 64}",
        f"collection={'f' * 64}",
        f"board={'b' * 64}",
        f"skilltree={'d' * 64}",
        f"master_stn={'e' * 64}",
        f"cfg={'g' * 64}",
        "model_version=objective-v2.2",
    ]
    assert (
        cert_mod.reference_key(per, "objective-v2.2")
        == hashlib.sha256("\n".join(ref_lines).encode()).hexdigest()
    )
    cd = cert_mod.chart_digest("m0001", charts_path=path)
    own_lines = ref_lines[:-1] + [f"chart={cd}", "model_version=objective-v2.2"]
    assert (
        cert_mod.enumeration_key(per, cd, "objective-v2.2")
        == hashlib.sha256("\n".join(own_lines).encode()).hexdigest()
    )
    # the charts.json WHOLE-file digest is NOT in either key
    assert cert_mod.reference_key({**per, "charts": "z" * 64}) == cert_mod.reference_key(per)
    assert cert_mod.enumeration_key({**per, "charts": "z" * 64}, cd) == cert_mod.enumeration_key(per, cd)
    # every other line is
    for k in cert_mod.REFERENCE_KEY_LINES:
        assert cert_mod.reference_key({**per, k: "z" * 64}) != cert_mod.reference_key(per), k
    assert cert_mod.enumeration_key(per, "other-chart") != cert_mod.enumeration_key(per, cd)
    assert cert_mod.enumeration_key(per, cd, "objective-v3") != cert_mod.enumeration_key(per, cd)
    with pytest.raises(KeyError):
        cert_mod.reference_key({"cards": "a" * 64})
    with pytest.raises(ValueError):
        cert_mod.enumeration_key(per, "")
    # the tolerant wrapper: (own, ref, chart, error)
    ek, rk, c, err = cert_mod.try_enumeration_keys(per, "m0001", charts_path=path)
    assert (ek, rk, c, err) == (cert_mod.enumeration_key(per, cd), cert_mod.reference_key(per), cd, None)
    ek, rk, c, err = cert_mod.try_enumeration_keys(per, "m0003", charts_path=path)
    assert ek is None and rk == cert_mod.reference_key(per) and c is None
    assert "no EXPERT chart for m0003" in err
    assert cert_mod.try_enumeration_keys(None, "m0001", charts_path=path)[3].startswith(
        "run carries no per-file"
    )
    assert cert_mod.try_enumeration_keys({"cards": "x"}, "m0001", charts_path=path)[3]
    ek, rk, c, err = cert_mod.try_enumeration_keys(per, "m0001", charts_path=tmp_path / "missing.json")
    assert ek is None and rk and "chart digest of m0001" in err
    # and the real per-file dict `inputs_hash` returns feeds them unchanged
    cfg = make_scoring_config()
    h, real_per, err = cert_mod.try_inputs_hash(cfg, "maxed")
    if err:
        pytest.skip(f"no inputs_hash on this checkout: {err}")
    assert set(cert_mod.REFERENCE_KEY_LINES) <= set(real_per)
    assert cert_mod.reference_key(real_per) == cert_mod.reference_key(
        {k: real_per[k] for k in cert_mod.REFERENCE_KEY_LINES}
    )
