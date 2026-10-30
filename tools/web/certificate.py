"""Certificate helpers shared by the web server and the recon probes:
`inputs_hash` (the certificate hashing recipe), `git_state`, `cfg_json`,
and the `MODEL_VERSION` re-export.

Read-only: it hashes files, it never writes any. Lives on the recon/web side
so `src/hbuilder` keeps no knowledge of dump paths.
"""
from __future__ import annotations

import dataclasses
import enum
import hashlib
import json
import subprocess
from pathlib import Path

from hbuilder.domain.model_version import MODEL_VERSION  # noqa: F401  (re-export)

REPO = Path(__file__).resolve().parents[2]

# label -> repo-relative path, in the order the recipe hashes them
INPUT_FILES = (
    ("cards", "data/cards.json"),
    ("board", "tools/recon/proxy/dumps/board.json"),
    ("charts", "tools/recon/proxy/dumps/charts.json"),
    ("skilltree", "tools/recon/proxy/dumps/skilltree.json"),
    ("master_stn", "tools/recon/proxy/dumps/master_SkillTreeNode.json"),
)
MAXED_BASIS_LITERAL = b"maxed:level=max,bloom=max"


def _jsonable(o):
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        return {f.name: _jsonable(getattr(o, f.name)) for f in dataclasses.fields(o)}
    if isinstance(o, enum.Enum):
        return o.value if isinstance(o.value, (str, int, float)) else o.name
    if isinstance(o, dict):
        return {str(k): _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    if isinstance(o, (set, frozenset)):
        return sorted(_jsonable(v) for v in o)
    if isinstance(o, (str, int, float, bool)) or o is None:
        return o
    if isinstance(o, Path):
        return str(o)
    # model objects (scoring_models.*): class identity + public state, never a
    # memory address — the hash has to be reproducible across processes
    state = {}
    if hasattr(o, "__dict__"):
        state.update(vars(o))
    for slot in getattr(type(o), "__slots__", ()) or ():
        if hasattr(o, slot):
            state[slot] = getattr(o, slot)
    out = {"__class__": f"{type(o).__module__}.{type(o).__qualname__}"}
    out.update({str(k): _jsonable(v) for k, v in state.items() if not str(k).startswith("_")})
    return out


def cfg_json(cfg) -> str:
    return json.dumps(_jsonable(cfg), sort_keys=True, separators=(",", ":"))


def inputs_hash(cfg, basis: str, model_version: str = MODEL_VERSION, *,
                repo: Path = REPO, collection_digest: str | None = None,
                extra_per: dict | None = None) -> tuple[str, dict]:
    """(hash, per-file digests). `basis` is "maxed" or "account"; anything
    else is treated as a path to the collection file to hash. A caller that
    already holds the collection line's digest (a generated scenario, see
    `scenario_hash`) passes it as `collection_digest` and `basis` is then
    only a label; `extra_per` rows are reported alongside, never hashed."""
    per: dict[str, str] = {}
    for label, rel in INPUT_FILES:
        per[label] = hashlib.sha256((repo / rel).read_bytes()).hexdigest()
    if collection_digest is not None:
        coll = collection_digest
    elif basis == "maxed":
        coll = hashlib.sha256(MAXED_BASIS_LITERAL).hexdigest()
    elif basis == "account":
        coll = hashlib.sha256((repo / "state/collection.json").read_bytes()).hexdigest()
    else:
        coll = hashlib.sha256(Path(basis).read_bytes()).hexdigest()
    per["collection"] = coll
    if extra_per:
        per.update(extra_per)
    cj = cfg_json(cfg)
    per["cfg"] = hashlib.sha256(cj.encode()).hexdigest()
    lines = [f"cards={per['cards']}", f"collection={coll}", f"board={per['board']}",
             f"charts={per['charts']}", f"skilltree={per['skilltree']}",
             f"master_stn={per['master_stn']}", f"cfg={per['cfg']}",
             f"model_version={model_version}"]
    return hashlib.sha256("\n".join(lines).encode()).hexdigest(), per


def git_state(repo: Path = REPO) -> dict:
    def run(*args):
        return subprocess.run(["git", *args], cwd=repo, capture_output=True,
                              text=True, encoding="utf-8", errors="replace").stdout
    try:
        head = run("rev-parse", "HEAD").strip()
        tracked = run("status", "--porcelain", "--untracked-files=no").strip()
        untracked = run("ls-files", "--others", "--exclude-standard").strip()
        diff = run("diff", "HEAD")
        # `dirty` = TRACKED files differ from HEAD (the code / data the run used
        # is not what the commit says). Untracked paths are listed separately:
        # a run's own output directory is untracked until it is committed and
        # must not make the certificate read as taken on a modified tree.
        return {"head": head, "dirty": bool(tracked),
                "status_porcelain": tracked.splitlines(),
                "untracked": untracked.splitlines(),
                "diff_head_sha256": hashlib.sha256(diff.encode("utf-8")).hexdigest()}
    except Exception as exc:  # noqa: BLE001
        return {"head": None, "dirty": None, "error": str(exc)}


def scenario_hash(cfg, bench: dict, scenario_literal: str,
                  model_version: str = MODEL_VERSION, *,
                  repo: Path = REPO) -> tuple[str, dict]:
    """The `inputs_hash` of a GENERATED scenario — the maxed-account basis,
    whose bench (every holomem's board at the full 453 tree with level-2
    Connect cards socketed) is computed, not a file on disk. Same recipe as
    `inputs_hash`; the collection line is
    `sha256(f"{scenario_literal};bench={sha256(json(bench, sort_keys))}")`,
    the same hash the offline certificate runs use, so a certificate and a web
    response on the same scenario carry the same hash. `per["bench"]` reports
    the bench digest on its own."""
    bench_sha = hashlib.sha256(json.dumps(bench, sort_keys=True).encode()).hexdigest()
    coll = hashlib.sha256(f"{scenario_literal};bench={bench_sha}".encode()).hexdigest()
    return inputs_hash(cfg, "scenario", model_version, repo=repo,
                       collection_digest=coll, extra_per={"bench": bench_sha})


def try_scenario_hash(cfg, bench: dict, scenario_literal: str,
                      model_version: str = MODEL_VERSION, *,
                      repo: Path = REPO) -> tuple[str | None, dict | None, str | None]:
    """(hash, per_file, error) — `scenario_hash` with the same tolerance
    `try_inputs_hash` has: a missing dump is reported, never a 500."""
    try:
        h, per = scenario_hash(cfg, bench, scenario_literal, model_version, repo=repo)
        return h, per, None
    except (OSError, TypeError, ValueError) as exc:  # noqa: PERF203
        return None, None, f"{type(exc).__name__}: {exc}"


__all__ = ["MODEL_VERSION", "INPUT_FILES", "inputs_hash", "scenario_hash",
           "try_scenario_hash", "git_state", "cfg_json"]


# ---------------------------------------------------------------------------
# Response-side helpers: the hash a web response carries, computed
# tolerant of an incomplete checkout — a missing dump is REPORTED on the
# response (`inputs_hash: null` + `inputs_hash_error`), never a 500 and never a
# hash over the files that happened to exist.
# ---------------------------------------------------------------------------
def try_inputs_hash(cfg, basis: str, model_version: str = MODEL_VERSION, *,
                    repo: Path = REPO) -> tuple[str | None, dict | None, str | None]:
    """(hash, per_file, error). `error` names the missing/unreadable file."""
    try:
        h, per = inputs_hash(cfg, basis, model_version, repo=repo)
        return h, per, None
    except (OSError, TypeError, ValueError) as exc:  # noqa: PERF203
        return None, None, f"{type(exc).__name__}: {exc}"


def certificate_basis(*, universe: str, basis: str, budget_kind: str,
                      budget: int | None, uptime_model: str | None,
                      platform: str | None, leaf: str | None = None,
                      difficulty: str | None = None) -> dict:
    """The tuple a certificate must match field-for-field before it may be
    attached to a response row (the "same instant, same basis" guard, together
    with `inputs_hash`). `budget_kind`: "flat" (a per-character cap, `budget`
    = the cap), "real" (each character's granted points), "captured" (the
    board as captured/edited, no develop), "none" (board off).

    `leaf` (developed rows only): WHICH board the row's
    number was scored on — "plan_cache" (the FIXED per-(character, role,
    budget, singer-context) plan, the v2 objective the certificates certify)
    or "develop" (the per-team greedy re-develop, a labelled what-if). Absent
    (None) on rows that develop nothing. Not part of the guard's equality
    (the tables carry no such field); `certindex.attach` reads it to refuse
    the ERROR verdict for a develop-leaf row above a certified maximum — a
    different objective, not a bug."""
    out = {"universe": universe, "basis": basis, "budget_kind": budget_kind,
           "budget": budget, "uptime_model": uptime_model, "platform": platform}
    # `difficulty`: the chart difficulty the rows were scored on.
    # Present ONLY when it is not EXPERT, so every EXPERT basis keeps its
    # original shape (and every certificate on file, all EXPERT, still matches);
    # `certindex` compares it with an absent field read as EXPERT.
    if difficulty is not None and str(difficulty).upper() != CHART_DIFFICULTY:
        out["difficulty"] = str(difficulty).upper()
    if leaf is not None:
        out["leaf"] = leaf
    return out


def universe_name(*, basis: str, pool: str) -> str:
    """The candidate universe a web run searched, in the vocabulary the
    certificates use ("5star-maxed-catalogue" is the per-leader tables'). The
    `maxed-account` basis searches the same maxed catalogue as `maxed` (the
    two differ in the account around the cards, which `certificate_basis.basis`
    + `inputs_hash` carry), so it names the same universe."""
    b = "maxed" if basis in ("maxed", "maxed-account") else "account"
    if pool == "5star":
        return f"5star-{b}-catalogue" if b == "maxed" else "5star-account"
    return "maxed-catalogue" if b == "maxed" else "account"


__all__ += ["try_inputs_hash", "certificate_basis", "universe_name"]


# ---------------------------------------------------------------------------
# The MEMBERS-FIRST enumeration's OWN validity key. `inputs_hash`
# digests charts.json AS A WHOLE, so a new song landing in charts.json would
# retire every cached enumeration — but an enumeration's bare scores depend on
# ONE chart (its own song's), never on the others. Two keys, shared by the
# enumeration run (which stamps them into the meta) and the web index
# (`members_first_index.py`, which guards by them):
#
#   reference_key   = sha256 over the NON-chart lines — cards / collection /
#                     board / skilltree / master_stn / cfg / model_version —
#                     what a cross-song TRANSFER reference must share with
#                     the run (the chart it was scored on is irrelevant: its
#                     bare ranking is re-scored on the run's song);
#   enumeration_key = the same lines + `chart=<chart_digest>` — what the
#                     run's OWN-song source must share, `chart_digest` being
#                     the sha256 of the canonical JSON of THAT song's entry
#                     in charts.json (`charts["<song>/EXPERT"]`).
#
# Every per-file digest comes from `inputs_hash`'s `per` dict (the recipe's
# own lines, unchanged), so the keys add no new hashing of the big files.
# ---------------------------------------------------------------------------
CHARTS_PATH = REPO / dict(INPUT_FILES)["charts"]
CHART_DIFFICULTY = "EXPERT"
# Every difficulty the game ships, easiest first (`build_charts.DIFFICULTY`).
# EXPERT is the default everywhere and lives in charts.json; the other three
# live only in charts_all.json (`build_charts.py --all-out`), which also
# carries EXPERT byte-identically.
DIFFICULTIES = ("EASY", "NORMAL", "HARD", "EXPERT")
CHARTS_ALL_PATH = CHARTS_PATH.with_name("charts_all.json")


def normalize_difficulty(value: object) -> str:
    """`value` upper-cased, `CHART_DIFFICULTY` for None / "". Raises
    ValueError on anything that is not one of `DIFFICULTIES` — a typo must not
    silently score EXPERT."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return CHART_DIFFICULTY
    name = str(value).strip().upper()
    if name not in DIFFICULTIES:
        raise ValueError(f"unknown difficulty {value!r}; one of {', '.join(DIFFICULTIES)}")
    return name


def charts_path_for(difficulty: str = CHART_DIFFICULTY, *,
                    charts_path: Path | None = None,
                    charts_all_path: Path | None = None) -> Path:
    """The chart file that holds `difficulty`: charts.json for EXPERT (so every
    EXPERT digest and cache key stays what it was), charts_all.json otherwise."""
    if normalize_difficulty(difficulty) == CHART_DIFFICULTY:
        return Path(charts_path) if charts_path is not None else CHARTS_PATH
    return Path(charts_all_path) if charts_all_path is not None else CHARTS_ALL_PATH


_DIFFICULTIES_SEEN: dict[str, tuple[float, dict[str, list[str]]]] = {}


def available_difficulties(charts_path: Path | None = None,
                           charts_all_path: Path | None = None) -> dict[str, list[str]]:
    """difficulty -> sorted music ids that have a chart at it, from the files on
    disk (EXPERT from charts.json, the rest from charts_all.json). A difficulty
    with no chart on disk is absent. Cached per file mtime."""
    out: dict[str, list[str]] = {}
    for name in DIFFICULTIES:
        path = charts_path_for(name, charts_path=charts_path, charts_all_path=charts_all_path)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        held = _DIFFICULTIES_SEEN.get(str(path))
        if held is None or held[0] != mtime:
            try:
                raw = json.loads(path.read_bytes().decode("utf-8"))
            except (OSError, ValueError):
                continue
            charts = raw.get("charts") if isinstance(raw, dict) else raw
            rows = charts.values() if isinstance(charts, dict) else (charts or [])
            by: dict[str, set[str]] = {}
            for e in rows:
                if isinstance(e, dict) and e.get("music_id"):
                    by.setdefault(str(e.get("difficulty") or "").upper(), set()).add(str(e["music_id"]))
            held = (mtime, {k: sorted(v) for k, v in by.items()})
            _DIFFICULTIES_SEEN[str(path)] = held
        if held[1].get(name):
            out[name] = held[1][name]
    return out


def resolve_difficulty(value: object, song: str | None = None, *,
                       charts_path: Path | None = None,
                       charts_all_path: Path | None = None) -> str:
    """`normalize_difficulty(value)`, then checked against what is ON DISK: a
    difficulty no chart file holds (or, with `song`, that this song has no
    chart at) raises ValueError naming what is available."""
    name = normalize_difficulty(value)
    if name == CHART_DIFFICULTY:
        return name   # the default never depends on charts_all.json; callers say "no chart"
    present = available_difficulties(charts_path, charts_all_path)
    if name not in present:
        raise ValueError(f"difficulty {name}: no chart file on disk holds it "
                         f"(available: {', '.join(present) or 'none'}; build every "
                         f"difficulty with `build_charts.py --all-out dumps/charts_all.json`)")
    if song and song not in set(present[name]):
        have = [d for d, ids in present.items() if song in set(ids)]
        raise ValueError(f"song {song!r} has no {name} chart (it has: {', '.join(have) or 'none'})")
    return name
REFERENCE_KEY_LINES = ("cards", "collection", "board", "skilltree", "master_stn", "cfg")


def chart_entry(song: str, *, charts_path: Path | None = None,
                difficulty: str = CHART_DIFFICULTY):
    """`song`'s chart entry in charts.json (the `charts` mapping keyed
    `<music_id>/<difficulty>`; a list of entries with `music_id` /
    `difficulty` fields is accepted too). None when the song has no chart;
    OSError / ValueError when the file is missing or not JSON."""
    path = Path(charts_path) if charts_path is not None else CHARTS_PATH
    raw = json.loads(path.read_bytes().decode("utf-8"))
    charts = raw.get("charts") if isinstance(raw, dict) else raw
    if isinstance(charts, dict):
        hit = charts.get(f"{song}/{difficulty}")
        if hit is None:
            hit = charts.get(song)
        return hit
    if isinstance(charts, list):
        for e in charts:
            if isinstance(e, dict) and str(e.get("music_id")) == song                     and str(e.get("difficulty") or difficulty).upper() == difficulty:
                return e
    return None


def chart_digest(song: str, *, charts_path: Path | None = None,
                 difficulty: str = CHART_DIFFICULTY) -> str | None:
    """sha256 of `json.dumps(entry, sort_keys=True, separators=(",", ":"))`
    over `song`'s chart entry — the ONE chart an enumeration of `song` was
    scored on. None when charts.json has no such chart. Raises (OSError,
    ValueError) when charts.json itself is missing / unreadable — the
    callers' `try_` wrappers report that, never guess."""
    entry = chart_entry(song, charts_path=charts_path, difficulty=difficulty)
    if entry is None:
        return None
    canon = json.dumps(entry, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _key_lines(per_file, model_version: str) -> list[str]:
    missing = [k for k in REFERENCE_KEY_LINES if not per_file.get(k)]
    if missing:
        raise KeyError(f"per-file digests missing {missing}")
    lines = [f"{k}={per_file[k]}" for k in REFERENCE_KEY_LINES]
    lines.append(f"model_version={model_version}")
    return lines


def reference_key(per_file, model_version: str = MODEL_VERSION) -> str:
    """The NON-chart part of an enumeration's key: sha256 over
    `cards=… collection=… board=… skilltree=… master_stn=… cfg=…
    model_version=…` (one line each, that order). Equal between a cached
    enumeration and a run  <=>  the enumeration is a valid cross-song
    TRANSFER reference for the run, whatever either song's chart is and
    however many songs charts.json has gained since."""
    return hashlib.sha256("\n".join(_key_lines(per_file, model_version)).encode()).hexdigest()


def enumeration_key(per_file, chart_digest: str, model_version: str = MODEL_VERSION) -> str:
    """The enumeration's OWN validity key: the `reference_key` lines plus
    `chart=<chart_digest>` (inserted before `model_version`). Equal between
    a cached enumeration of song S and a run on S  <=>  the enumeration's
    bare scores were computed on exactly the run's cards / collection /
    board / skilltree / master_stn / cfg / model AND the run's chart of S —
    so it may serve as the run's OWN-song source. A new song appended to
    charts.json changes neither key; an edited card does (cards line)."""
    if not chart_digest:
        raise ValueError("enumeration_key needs the song's chart_digest")
    lines = _key_lines(per_file, model_version)
    lines.insert(len(lines) - 1, f"chart={chart_digest}")
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def try_enumeration_keys(per_file, song: str, model_version: str = MODEL_VERSION, *,
                         charts_path: Path | None = None
                         ) -> tuple[str | None, str | None, str | None, str | None]:
    """`(enumeration_key, reference_key, chart_digest, error)` for a run on
    `song` from its `inputs_hash` per-file digests — tolerant: no per-file
    digests / no chart for the song / an unreadable charts.json gives Nones
    and the reason, never a raise (the index then falls back to the
    whole-file `inputs_hash` guard and says so)."""
    if not per_file:
        return None, None, None, "run carries no per-file digests (inputs_hash_files)"
    try:
        ref = reference_key(per_file, model_version)
    except KeyError as exc:
        return None, None, None, str(exc)
    try:
        cd = chart_digest(song, charts_path=charts_path)
    except (OSError, ValueError) as exc:
        return None, ref, None, f"chart digest of {song}: {type(exc).__name__}: {exc}"
    if cd is None:
        return None, ref, None, f"no {CHART_DIFFICULTY} chart for {song} in charts.json"
    return enumeration_key(per_file, cd, model_version), ref, cd, None


__all__ += ["CHARTS_PATH", "CHART_DIFFICULTY", "DIFFICULTIES", "CHARTS_ALL_PATH",
            "normalize_difficulty", "charts_path_for", "available_difficulties",
            "resolve_difficulty", "REFERENCE_KEY_LINES", "chart_entry",
            "chart_digest", "reference_key", "enumeration_key", "try_enumeration_keys"]
