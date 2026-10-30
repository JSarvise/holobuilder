// certpill.js — the per-row certificate: a status pill on the ranking row and a
// fold-out that says, in plain language, what the status means and what it
// was measured on (OPTIMIZER-V2-STATUS.md phase 8; OBJECTIVE-v2.md §5).
//
//   EXACT      green — every roster for this leader was accounted for, on this
//              exact basis (inputs_hash + budget/uptime/platform/universe).
//   BOUNDED    amber — an incumbent plus a valid upper bound; the gap is shown.
//   HEURISTIC  grey  — best of the N candidates the pipeline evaluated; no bound.
//   ERROR      red   — this row scores ABOVE an EXACT certificate on the SAME
//              hash: refined > exact on the same basis is a bug, not a better
//              team, and it is shown as one.
//
// ⛔ A certificate reaches a row ONLY through the server's same-instant-same-
// basis guard (certindex.attach); this module never decides a status, it only
// draws the one it was given. When a table exists on another basis the row
// stays HEURISTIC and `certificate_note` says which field differed.
import { el } from "/dom.js";

const fmt = (n) => (typeof n === "number" ? n.toLocaleString("en-US") : String(n ?? "—"));
const pct = (g) => (typeof g === "number" ? (g * 100).toFixed(3) + " %" : "—");
const short = (h) => (h ? String(h).slice(0, 12) + "…" : "—");

const LABEL = {
  EXACT: "EXACT",
  BOUNDED: "BOUNDED",
  HEURISTIC: "HEURISTIC",
  ERROR: "ERROR",
};

const TITLE = {
  EXACT: "Certified: every roster for this leader was scored or bounded away on this exact basis — this leader cannot do better.",
  BOUNDED: "Certified with a gap: an incumbent plus a valid upper bound over the rosters not yet explored.",
  HEURISTIC: "Best of the candidates this run evaluated — no bound over the rosters it did not evaluate.",
  ERROR: "This row scores above an EXACT certificate on the same inputs — that is a bug in the pipeline, not a better team.",
};

// The pill for the summary line. `cert` may be undefined (older server / agnostic run).
export function certPill(cert) {
  if (!cert || !cert.status) return null;
  const st = LABEL[cert.status] ? cert.status : "HEURISTIC";
  let text = LABEL[st];
  if (st === "BOUNDED" && typeof cert.gap === "number") text += ` · gap ${pct(cert.gap)}`;
  if (st === "EXACT" && cert.is_certified_argmax === false) text += " · not the argmax";
  const pill = el("span", `cert cert-${st.toLowerCase()}`, text);
  pill.title = TITLE[st] + (cert.certificate_note ? "\n" + cert.certificate_note : "");
  return pill;
}

function row(dl, term, value) {
  if (value === undefined || value === null || value === "") return;
  dl.append(el("dt", null, term), el("dd", null, String(value)));
}

// The fold-out for the row body.
export function certFold(cert) {
  if (!cert || !cert.status) return null;
  const st = LABEL[cert.status] ? cert.status : "HEURISTIC";
  const wrap = el("details", `explain cert-fold cert-fold-${st.toLowerCase()}`);
  wrap.append(el("summary", null, `Certificate — ${LABEL[st]}: what this status means and what it was measured on`));
  const body = el("div", "cert-body");

  // 1. plain-language meaning
  const meaning = el("p", "lede sm");
  if (st === "EXACT") {
    const n = cert.n_rosters;
    meaning.textContent =
      `EXACT means every one of ${fmt(n)} rosters for this leader was accounted for — scored ` +
      `by the exact leaf or bounded away by a monotone relaxation of the same leaf — on this ` +
      `exact basis (same inputs, same budget, same uptime model, same platform, same candidate ` +
      `universe). ${cert.is_certified_argmax === false
        ? "The certified best for this leader is a DIFFERENT roster than this row (see below)."
        : "This row IS the certified best roster for this leader."}`;
  } else if (st === "BOUNDED") {
    meaning.textContent =
      `BOUNDED means the search was time-boxed: this is an incumbent plus a valid upper bound over ` +
      `the rosters not yet explored. gap = (upper bound − score) / score = ${pct(cert.gap)}. ` +
      `No roster for this leader can beat the upper bound on this basis.`;
  } else if (st === "ERROR") {
    meaning.textContent = cert.error || TITLE.ERROR;
  } else {
    meaning.textContent =
      `HEURISTIC means: the best of the ${fmt(cert.candidates_evaluated)} candidates this run ` +
      `evaluated, with no bound over the ones it did not. Candidate generator: ` +
      `${cert.candidate_generator || "—"}.` +
      (cert.certificate_note ? ` A certificate on file was NOT attached: ${cert.certificate_note}.` : "");
  }
  body.append(meaning);
  if (cert.note) body.append(el("p", "note", cert.note));

  // 2. the facts
  const dl = el("dl", "cert-facts");
  row(dl, "status", LABEL[st]);
  row(dl, "model_version", cert.model_version);
  row(dl, "inputs_hash", cert.inputs_hash || "— (a dump is missing on this checkout)");
  const b = cert.basis || {};
  row(dl, "basis", `${b.basis || "—"} · universe ${b.universe || "—"} · budget ${b.budget_kind || "—"}` +
      (b.budget != null ? ` ${b.budget}` : "") + ` · uptime ${b.uptime_model || "—"} · platform ${b.platform || "—"}`);
  if (Array.isArray(cert.stages) && cert.stages.length) row(dl, "pipeline", cert.stages.join(" → "));
  if (cert.seed_team) row(dl, "refine seed", `${cert.seed_team.join(", ")} at ${fmt(cert.seed_score)}`);
  if (typeof cert.row_score === "number") row(dl, "this row's score", fmt(cert.row_score));
  const c = cert.certified;
  if (c) {
    row(dl, "certified score", fmt(c.score));
    if (st === "BOUNDED") row(dl, "upper bound", fmt(c.upper_bound));
    row(dl, "gap", pct(c.gap));
    row(dl, "rosters accounted", `${fmt(c.n_rosters)} (leaves scored ${fmt(c.leaves_evaluated)})`);
    row(dl, "computed in", typeof c.seconds === "number" ? `${c.seconds.toFixed(0)} s` : null);
    row(dl, "computed at", c.written);
    row(dl, "git head", (c.git_head ? String(c.git_head).slice(0, 12) : "—") + (c.git_dirty ? " (dirty tree)" : ""));
    row(dl, "certificate file", c.file);
    if (cert.is_certified_argmax === false && (c.members || c.ids)) {
      row(dl, "certified best roster", (c.members || c.ids).join(", "));
    }
    row(dl, "certificate hash", short(c.inputs_hash) + " (equal to this run's)");
  }
  body.append(dl);
  wrap.append(body);
  return wrap;
}
