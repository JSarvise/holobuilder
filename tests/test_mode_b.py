"""MODE B — ranking the account's REAL collection instead of the maxed one.

`data/cards.json` holds **maxed** stats, so a ranking on it answers "what is
the best team if every card were maxed?".
`tools/recon/proxy/dumps/card_params.json` already *is* the account's real
per-card stats (91 rows, straight off the decrypted
`rpc.api.Card/GetParameters` response). `tools/recon/proxy/build_mode_b.py`
joins the two into a mode-B collection document; `hbuilder solve
--real-stats <path>` ranks on it.

Three things are pinned here because getting any of them wrong is silent:

1. **The join is exactly 91 of 167, by card id, with nothing dropped.** An
   unmatched stat row must be named and must fail the build.
2. **Neither mode writes `data/cards.json` or `data/owned.json`.** They are
   authoritative and hand-edited; sha256 + mtime are compared across a real
   subprocess run of both modes. (`tests/test_source_of_truth.py` pins the
   same rule structurally; this pins it behaviourally.)
3. **A mode-B solve differs from a maxed solve on the same pool, and says
   so.** Mode A stays the default and its output is unchanged; mode B
   announces itself on stdout, on stderr, and in the `--json` payload,
   because confusing the two is easy and silent.

The tests that need the real 91-row capture are skipped when it is absent —
`tools/recon/proxy/dumps/` is gitignored and holds the user's own account
data, which is never committed. Everything that pins a *contract* rather
than a *measurement* runs against synthetic documents built by the same
builder, so a fresh checkout still verifies the behaviour.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROXY_DIR = REPO_ROOT / "tools" / "recon" / "proxy"
if str(PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(PROXY_DIR))

import build_mode_b  # noqa: E402

from hbuilder.cli import build_parser  # noqa: E402
from hbuilder.domain.loader import (  # noqa: E402
    card_provenance,
    card_provenance_of,
    stale_source_note,
)
from hbuilder.domain.schema import SchemaError  # noqa: E402

from conftest import FIXTURE_CARDS_PATH, FIXTURE_OWNED_PATH  # noqa: E402

# The two authoritative hand-edited files, taken from the CLI's own declared
# defaults rather than written out as literals: `tests/test_fixture_isolation.py`
# forbids a hardcoded `data/` path anywhere in `tests/` so that the user's real
# collection can live there without coupling a single test to its contents, and
# these tests are about those files' *inviolability*, not their contents. Reading
# the defaults keeps the reference honest and self-updating.
_SOLVE_DEFAULTS = build_parser().parse_args(["solve"])
CARDS_PATH = REPO_ROOT / _SOLVE_DEFAULTS.cards
OWNED_PATH = REPO_ROOT / _SOLVE_DEFAULTS.owned
PARAMS_PATH = PROXY_DIR / "dumps" / "card_params.json"

# The capture is the user's own account data and is gitignored, so every
# test that asserts a *number* about it is gated on its presence. A number
# that silently becomes "0 of 0" when a file is missing is worse than a skip.
requires_capture = pytest.mark.skipif(
    not PARAMS_PATH.exists(),
    reason=f"{PARAMS_PATH} absent (gitignored capture); regenerate with parse_card_params.py",
)

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_INPUT_DATA = 3


def _read_json(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _synthetic_params(cards_doc: dict) -> list[dict]:
    """Stand-in for `card_params.json`: one row per fixture card, each scaled
    by a *different* factor per card and per stat.

    Deliberately not a uniform scale. A uniform scale would leave every
    ranking identical and the "mode B differs from maxed" test would prove
    nothing; real collections genuinely do hold cards at wildly different
    levels of investment, which is the entire reason mode B exists."""
    rows = []
    for index, record in enumerate(cards_doc["cards"]):
        stats = record["stats"]
        # Ascending, so the card the maxed answer leaves OUT of its best five
        # is the one the real collection has actually invested in. That is the
        # whole failure mode mode B exists to fix, in miniature.
        factor = 0.3 + 0.12 * index
        rows.append(
            {
                "id": record["id"],
                "card_num": record["id"],
                "rarity": record.get("rarity", ""),
                "stat3": int(stats["performance"] * factor),
                "stat4": int(stats["technique"] * factor * 0.95),
                "stat5": int(stats["sense"] * factor * 1.05),
                "total": 0,
                "total_matches_sum": False,
            }
        )
    return rows


def _mode_b_fixture_document(tmp_path: Path) -> Path:
    """Build a real mode-B document (through the real builder) from the test
    fixtures, so the CLI-level tests need no capture on disk."""
    cards_doc = _read_json(FIXTURE_CARDS_PATH)
    params_path = tmp_path / "params.json"
    params_path.write_text(
        json.dumps(_synthetic_params(cards_doc)), encoding="utf-8"
    )
    document, unmatched = build_mode_b.build_document(FIXTURE_CARDS_PATH, params_path)
    assert unmatched == []
    out_path = tmp_path / "cards.real.json"
    out_path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return out_path


def _run(cwd: Path, argv: list[str]) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    return subprocess.run(
        [sys.executable, "-m", "hbuilder.cli", *argv],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


def _fingerprint(path: Path) -> tuple[str, int]:
    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns


# --- 1. the join --------------------------------------------------------------


@requires_capture
def test_the_real_join_covers_every_captured_card_with_no_unmatched_stat_rows():
    # No card counts are pinned: the account's pool grows when a card is pulled
    # and the catalogue grows when the game ships one, so a literal count would
    # fail while nothing is broken. The invariants are stated directly: the join
    # is TOTAL (every captured stat row finds a cards.json id) and LOSSLESS (one
    # record out per row in).
    #
    # The invariant is that the catalogue is COMPLETE against the game's own Card
    # master. `dumps/cards_enriched.json` is built from that master, so comparing
    # against it says "no card the game ships is missing from cards.json" and
    # keeps saying it as the game grows. `dumps/` is gitignored, so a checkout
    # without it falls back to the join checks below — which are the real
    # totality guarantee anyway.
    cards_doc = _read_json(CARDS_PATH)
    params = _read_json(PARAMS_PATH)
    owned = set(_read_json(OWNED_PATH)["owned"])

    records, unmatched = build_mode_b.join(cards_doc, params)

    enriched_path = REPO_ROOT / "tools" / "recon" / "proxy" / "dumps" / "cards_enriched.json"
    if enriched_path.exists():
        enriched = _read_json(enriched_path)
        enriched_ids = {c["id"] for c in (enriched["cards"] if isinstance(enriched, dict) else enriched)}
        catalogue_ids = {c["id"] for c in cards_doc["cards"]}
        assert enriched_ids <= catalogue_ids, (
            "data/cards.json is missing cards the game ships — hand-merge them "
            f"(see refresh_all.py's hand-merge check): {sorted(enriched_ids - catalogue_ids)}"
        )
    assert unmatched == [], (
        f"every captured stat row must join a cards.json id; unmatched: {unmatched}"
    )
    assert len(records) == len(params), "the join must not drop or duplicate rows"
    # The capture is of the user's own account, so it must cover exactly what they own.
    captured = {row["id"] for row in params}
    assert captured == owned, (
        "card_params.json is stale — refresh it with "
        "`account_snapshot.py --account main --snapshot --refresh-card-params`. "
        f"owned but uncaptured: {sorted(owned - captured)}; "
        f"captured but not owned: {sorted(captured - owned)}"
    )


@requires_capture
def test_every_merged_card_carries_the_captured_stats_not_the_maxed_ones():
    cards_doc = _read_json(CARDS_PATH)
    params = _read_json(PARAMS_PATH)
    maxed_by_id = {record["id"]: record["stats"] for record in cards_doc["cards"]}
    captured_by_id = {row["id"]: row for row in params}

    records, _ = build_mode_b.join(cards_doc, params)

    changed = 0
    for record in records:
        row = captured_by_id[record["id"]]
        assert record["stats"] == {
            "performance": row["stat3"],
            "technique": row["stat4"],
            "sense": row["stat5"],
        }
        if record["stats"] != maxed_by_id[record["id"]]:
            changed += 1
    # Every real card is below its maxed self; a merge that changed nothing
    # would mean the join silently kept the maxed numbers.
    assert changed == len(records)


@requires_capture
def test_the_stat_field_mapping_is_re_derived_from_the_data_not_taken_on_trust():
    # stat3/stat4/stat5 -> performance/technique/sense is asserted in
    # parse_card_params.py against the in-game card screen. This re-derives it:
    # one card sits at one level and one bloom stage, so its three real/maxed
    # ratios should agree; under a wrong permutation they scatter.
    ranking = build_mode_b.mapping_report(_read_json(CARDS_PATH), _read_json(PARAMS_PATH))
    (best, best_spread), (_, runner_up_spread) = ranking[0], ranking[1]

    assert best == ("performance", "technique", "sense")
    assert runner_up_spread > 3 * best_spread, (
        f"the canonical mapping must win decisively; best={best_spread:.4f} "
        f"runner-up={runner_up_spread:.4f}"
    )


def test_an_unmatched_stat_row_is_named_and_never_silently_dropped():
    cards_doc = _read_json(FIXTURE_CARDS_PATH)
    params = _synthetic_params(cards_doc)
    params.append(dict(params[0], id="card-does-not-exist"))

    records, unmatched = build_mode_b.join(cards_doc, params)

    assert unmatched == ["card-does-not-exist"]
    assert len(records) == len(cards_doc["cards"])


def test_the_document_records_the_skills_file_it_actually_read(tmp_path):
    # Not the file it "should" have read. hbuilder resolves this path to decide
    # whether the hand-edited source has changed since the build; a hardcoded
    # value here would silently compare the wrong two files and either cry wolf
    # or, worse, stay quiet while the document went stale.
    real_stats = _mode_b_fixture_document(tmp_path)
    provenance = _read_json(real_stats)["provenance"]

    assert provenance["skills_source"] == FIXTURE_CARDS_PATH.resolve().relative_to(
        REPO_ROOT
    ).as_posix()
    assert provenance["skills_source_sha256"] == hashlib.sha256(
        FIXTURE_CARDS_PATH.read_bytes()
    ).hexdigest()


def test_the_builder_refuses_to_emit_when_a_stat_row_does_not_join(tmp_path):
    cards_doc = _read_json(FIXTURE_CARDS_PATH)
    params = _synthetic_params(cards_doc)
    params.append(dict(params[0], id="card-does-not-exist"))
    params_path = tmp_path / "params.json"
    params_path.write_text(json.dumps(params), encoding="utf-8")
    out_path = tmp_path / "out.json"

    exit_code = build_mode_b.main(
        [
            "--cards",
            str(FIXTURE_CARDS_PATH),
            "--params",
            str(params_path),
            "-o",
            str(out_path),
        ]
    )

    assert exit_code == 1
    assert not out_path.exists(), "a failed join must leave no output file behind"


# Parametrised over the builder's own list of protected paths rather than a
# second copy of it here: a path dropped from that list must fail here, and
# `tests/test_fixture_isolation.py` forbids re-hardcoding those literals anyway.
@pytest.mark.parametrize("protected", build_mode_b.PROTECTED)
def test_the_builder_refuses_to_write_an_authoritative_file(protected):
    with pytest.raises(SystemExit) as excinfo:
        build_mode_b._guard_output(REPO_ROOT / protected)

    assert protected in str(excinfo.value)


# --- 2. the authoritative files stay untouched --------------------------------


def test_neither_mode_writes_cards_json_or_owned_json(tmp_path):
    # Behavioural half of the no-write rule. tests/test_source_of_truth.py proves no
    # write call *exists* in src/hbuilder; this proves a real run of both
    # modes leaves both files byte-identical and mtime-identical.
    real_stats = _mode_b_fixture_document(tmp_path)
    before = {path: _fingerprint(path) for path in (CARDS_PATH, OWNED_PATH)}

    mode_a = _run(
        REPO_ROOT,
        [
            "solve",
            "--cards",
            str(FIXTURE_CARDS_PATH),
            "--owned",
            str(FIXTURE_OWNED_PATH),
            "--top",
            "2",
        ],
    )
    mode_b = _run(
        REPO_ROOT,
        [
            "solve",
            "--real-stats",
            str(real_stats),
            "--owned",
            str(FIXTURE_OWNED_PATH),
            "--top",
            "2",
            "--json",
            str(tmp_path / "out.json"),
        ],
    )

    assert mode_a.returncode == EXIT_OK, mode_a.stderr
    assert mode_b.returncode == EXIT_OK, mode_b.stderr
    for path in (CARDS_PATH, OWNED_PATH):
        assert _fingerprint(path) == before[path], (
            f"{path} changed during a solve — it is authoritative and "
            "hand-edited, and no code path may write it"
        )


# --- 3. mode B is opt-in, announced, and different ----------------------------


def test_a_mode_b_solve_ranks_the_same_pool_differently_from_a_maxed_solve(tmp_path):
    real_stats = _mode_b_fixture_document(tmp_path)

    maxed_out = tmp_path / "maxed.json"
    real_out = tmp_path / "real.json"
    # `--no-board`: a solve run from the repo root loads the account's board by default, and
    # the fixture cards carry real holomem names, so the account's bought nodes would score on
    # these six synthetic cards. A large enough board makes the maxed and the real answer
    # converge on the same five. A fixture test must not read account state.
    _run(
        REPO_ROOT,
        ["solve", "--cards", str(FIXTURE_CARDS_PATH), "--owned",
         str(FIXTURE_OWNED_PATH), "--no-board", "--json", str(maxed_out)],
    )
    _run(
        REPO_ROOT,
        ["solve", "--real-stats", str(real_stats), "--owned",
         str(FIXTURE_OWNED_PATH), "--no-board", "--json", str(real_out)],
    )

    maxed = json.loads(maxed_out.read_text(encoding="utf-8"))
    real = json.loads(real_out.read_text(encoding="utf-8"))

    maxed_scores = [team["score"] for team in maxed["teams"]]
    real_scores = [team["score"] for team in real["teams"]]
    assert maxed_scores != real_scores, (
        "a mode-B solve that scores identically to the maxed solve means the "
        "real stats never reached the scorer"
    )
    assert maxed["teams"][0]["member_ids"] != real["teams"][0]["member_ids"], (
        "the maxed answer and the real answer pick the same best five here — "
        "the fixture scaling was meant to make them disagree, and a mode-B run "
        "that always agrees with the maxed run is answering the wrong question"
    )


@requires_capture
def test_on_the_owners_real_collection_the_maxed_answer_is_not_the_real_answer(tmp_path):
    # The claim mode B exists to make, measured on the real 91-card pool
    # rather than a fixture. Four cards are locked so the run is ~7,900
    # arrangements instead of ~4.9 billion.
    document, unmatched = build_mode_b.build_document(CARDS_PATH, PARAMS_PATH)
    assert unmatched == []
    real_stats = tmp_path / "cards.real.json"
    real_stats.write_text(json.dumps(document, indent=2), encoding="utf-8")

    locks = [record["id"] for record in document["cards"][:4]]
    lock_argv = [flag for card_id in locks for flag in ("--require", card_id)]

    maxed_out = tmp_path / "maxed.json"
    real_out = tmp_path / "real.json"
    maxed = _run(
        REPO_ROOT,
        ["solve", "--cards", str(CARDS_PATH), "--owned", str(OWNED_PATH),
         "--json", str(maxed_out), *lock_argv],
    )
    real = _run(
        REPO_ROOT,
        ["solve", "--real-stats", str(real_stats), "--owned", str(OWNED_PATH),
         "--json", str(real_out), *lock_argv],
    )

    assert maxed.returncode == EXIT_OK, maxed.stderr
    assert real.returncode == EXIT_OK, real.stderr
    maxed_payload = json.loads(maxed_out.read_text(encoding="utf-8"))
    real_payload = json.loads(real_out.read_text(encoding="utf-8"))

    assert [team["score"] for team in maxed_payload["teams"]] != [
        team["score"] for team in real_payload["teams"]
    ]


def test_a_mode_b_run_announces_itself_on_stdout_and_on_stderr(tmp_path):
    real_stats = _mode_b_fixture_document(tmp_path)

    result = _run(
        REPO_ROOT,
        ["solve", "--real-stats", str(real_stats), "--owned",
         str(FIXTURE_OWNED_PATH), "--top", "1"],
    )

    assert result.returncode == EXIT_OK, result.stderr
    # stdout, so a redirected report carries the label; stderr, so a run whose
    # stdout is piped into a file or discarded is still labelled on the console.
    assert "MODE B" in result.stdout
    assert "MODE B" in result.stderr
    assert "NOT the maxed default" in result.stdout
    # The document names the file it actually copied skills from, so the
    # staleness check compares the right two files and stays quiet here.
    assert "STALE" not in result.stdout


def test_a_mode_b_json_payload_carries_the_stats_basis_and_a_default_one_does_not(tmp_path):
    real_stats = _mode_b_fixture_document(tmp_path)
    real_out = tmp_path / "real.json"
    maxed_out = tmp_path / "maxed.json"

    _run(
        REPO_ROOT,
        ["solve", "--real-stats", str(real_stats), "--owned",
         str(FIXTURE_OWNED_PATH), "--json", str(real_out)],
    )
    _run(
        REPO_ROOT,
        ["solve", "--cards", str(FIXTURE_CARDS_PATH), "--owned",
         str(FIXTURE_OWNED_PATH), "--json", str(maxed_out)],
    )

    real = json.loads(real_out.read_text(encoding="utf-8"))
    maxed = json.loads(maxed_out.read_text(encoding="utf-8"))

    assert real["stats_basis"]["mode"] == "B"
    assert real["stats_basis"]["basis"] == "real"
    assert real["stats_basis"]["stats_source"]
    # Diffability contract: the default payload is unchanged by mode B.
    # Mode B adds a key; mode A gains nothing.
    assert "stats_basis" not in maxed


def test_the_default_run_is_unchanged_and_claims_no_mode(tmp_path):
    result = _run(
        REPO_ROOT,
        ["solve", "--cards", str(FIXTURE_CARDS_PATH), "--owned",
         str(FIXTURE_OWNED_PATH), "--top", "1"],
    )

    assert result.returncode == EXIT_OK, result.stderr
    assert "MODE B" not in result.stdout
    assert "MODE B" not in result.stderr


def test_cards_and_real_stats_are_mutually_exclusive(tmp_path):
    real_stats = _mode_b_fixture_document(tmp_path)

    result = _run(
        REPO_ROOT,
        ["solve", "--cards", str(FIXTURE_CARDS_PATH), "--real-stats", str(real_stats),
         "--owned", str(FIXTURE_OWNED_PATH)],
    )

    # A run has exactly one stat basis; naming two is a usage error, never a
    # silent choice of one.
    assert result.returncode == EXIT_USAGE
    assert "not allowed with" in result.stderr


def test_real_stats_pointed_at_a_maxed_document_fails_loudly(tmp_path):
    result = _run(
        REPO_ROOT,
        ["solve", "--real-stats", str(FIXTURE_CARDS_PATH), "--owned",
         str(FIXTURE_OWNED_PATH)],
    )

    # The failure mode this guards is the expensive one: a run that promised
    # real stats quietly ranking maxed ones.
    assert result.returncode == EXIT_INPUT_DATA
    assert "provenance" in result.stderr
    assert "build_mode_b.py" in result.stderr


def test_the_gates_subcommand_takes_real_stats_too(tmp_path):
    real_stats = _mode_b_fixture_document(tmp_path)
    owned = _read_json(FIXTURE_OWNED_PATH)["owned"]

    result = _run(
        REPO_ROOT,
        ["gates", "--real-stats", str(real_stats), "--owned", str(FIXTURE_OWNED_PATH),
         "--leader", owned[0], "--members", ",".join(owned[1:6])],
    )

    assert result.returncode == EXIT_OK, result.stderr
    assert "MODE B" in result.stdout


# --- 4. the provenance contract ----------------------------------------------


def test_a_document_without_a_provenance_block_is_mode_a():
    # The absence is the default, never a gap: data/cards.json carries no
    # provenance and must keep loading exactly as before.
    assert card_provenance(_read_json(FIXTURE_CARDS_PATH)) is None
    assert card_provenance_of(CARDS_PATH) is None


def _valid_provenance() -> dict:
    return {
        "mode": "B",
        "stats_basis": "real",
        "generated_at": "2026-08-03T00:00:00Z",
        "stats_source": "dumps/card_params.json",
        "skills_source": "cards.json",
        "skills_source_sha256": "0" * 64,
        "card_count": 1,
        "description": "test",
    }


@pytest.mark.parametrize(
    "mutation",
    [
        {"mode": ""},
        {"stats_basis": 3},
        {"generated_at": None},
        {"skills_source_sha256": None},
        {"card_count": "91"},
        {"card_count": 99},
    ],
)
def test_a_half_filled_provenance_block_is_an_error_not_a_shrug(mutation):
    block = _valid_provenance()
    block.update(mutation)
    doc = {"schema_version": "1", "cards": [{"id": "a"}], "provenance": block}

    with pytest.raises(SchemaError):
        card_provenance(doc)


def test_a_mode_b_document_declaring_a_wrong_card_count_is_rejected():
    block = _valid_provenance()
    block["card_count"] = 2
    doc = {"schema_version": "1", "cards": [{"id": "a"}], "provenance": block}

    with pytest.raises(SchemaError) as excinfo:
        card_provenance(doc)

    assert "card_count" in str(excinfo.value)


def test_stale_source_note_is_silent_while_the_skills_source_is_unchanged(tmp_path):
    provenance = card_provenance(
        {
            "schema_version": "1",
            "cards": [],
            "provenance": dict(
                _valid_provenance(),
                card_count=0,
                skills_source="cards.json",
                skills_source_sha256=hashlib.sha256(
                    FIXTURE_CARDS_PATH.read_bytes()
                ).hexdigest(),
            ),
        }
    )
    (tmp_path / "cards.json").write_bytes(FIXTURE_CARDS_PATH.read_bytes())

    assert stale_source_note(provenance, tmp_path) is None


def test_stale_source_note_warns_once_the_skills_source_has_changed(tmp_path):
    # The live example: the 54 untranscribed 3★ costume_skills. The moment
    # any of them is hand-transcribed into data/cards.json, every previously
    # built mode-B document is carrying stale skills.
    provenance = card_provenance(
        {
            "schema_version": "1",
            "cards": [],
            "provenance": dict(
                _valid_provenance(), card_count=0, skills_source="cards.json"
            ),
        }
    )
    (tmp_path / "cards.json").write_text("{}", encoding="utf-8")

    note = stale_source_note(provenance, tmp_path)

    assert note is not None and "STALE" in note


def test_stale_source_note_says_so_when_the_skills_source_cannot_be_read(tmp_path):
    provenance = card_provenance(
        {
            "schema_version": "1",
            "cards": [],
            "provenance": dict(
                _valid_provenance(), card_count=0, skills_source="absent.json"
            ),
        }
    )

    note = stale_source_note(provenance, tmp_path)

    assert note is not None and "UNCHECKED" in note
