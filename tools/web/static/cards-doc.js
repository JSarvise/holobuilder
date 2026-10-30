// Collection page: the collection DOCUMENT (state/collection.json) as a working copy.
//
// The rules carried over from the old collection.js, because they are why it was thin:
// 1. No bound is invented here. Caps by limit break, max bloom, the bloom that lifts each
//    skill to Lv 2 and the account bounds all come from GET /api/collection, which reads
//    them out of hbuilder.domain.collection.
// 2. The disk file is the record; browser storage is only an unsaved draft. Nothing reaches
//    disk except through Save, which is the existing POST /api/collection.
// New guard: a draft remembers which disk document it was started from. An account refresh
// (refresh_all --merge-collection) rewrites that file, and a draft started before it would
// silently put the old values back on Save; the page says so and asks first.

const DRAFT_KEY = "hb.cards.draft";

export const doc = {
  data: null,      // the /api/collection response (cards, bounds, document as on disk)
  disk: null,      // the document as on disk
  base: "",        // fingerprint of `disk`
  work: null,      // the working copy
  dirty: false,
  staleDraft: false,
};

const clone = o => JSON.parse(JSON.stringify(o));
// Key-order-independent JSON, so the fingerprint only moves when a value does.
function canon(v) {
  if (Array.isArray(v)) return `[${v.map(canon).join(",")}]`;
  if (v && typeof v === "object") return `{${Object.keys(v).sort().map(k => JSON.stringify(k) + ":" + canon(v[k])).join(",")}}`;
  return JSON.stringify(v);
}
export const fingerprint = d => canon(d || {});

function readDraft() {
  try { const raw = localStorage.getItem(DRAFT_KEY); return raw ? JSON.parse(raw) : null; } catch (_) { return null; }
}
function writeDraft() {
  try { localStorage.setItem(DRAFT_KEY, JSON.stringify({ base: doc.base, doc: doc.work, at: Date.now() })); } catch (_) { /* the disk file is the record */ }
}
function dropDraft() {
  try { localStorage.removeItem(DRAFT_KEY); } catch (_) { /* nothing to drop */ }
}

export async function fetchCollection() {
  const r = await fetch("/api/collection");
  if (!r.ok) throw new Error(`GET /api/collection answered ${r.status}`);
  return r.json();
}

// Load the server's view; a draft from this browser wins (it is newer by definition) but is
// flagged stale when the disk document moved underneath it.
export function adopt(data, { keepDraft = true } = {}) {
  doc.data = data;
  doc.disk = data.document || { schema_version: "collection-1", card_default: "max", cards: {} };
  doc.base = fingerprint(doc.disk);
  const draft = keepDraft ? readDraft() : null;
  if (draft && draft.doc && typeof draft.doc === "object") {
    doc.work = draft.doc;
    doc.dirty = fingerprint(draft.doc) !== doc.base;
    doc.staleDraft = doc.dirty && draft.base !== doc.base;
    doc.draftAt = draft.at || null;
    if (!doc.dirty) dropDraft();
  } else {
    doc.work = clone(doc.disk);
    doc.dirty = false;
    doc.staleDraft = false;
  }
  if (!doc.work.cards || typeof doc.work.cards !== "object") doc.work.cards = {};
}

export function touch() {
  doc.dirty = fingerprint(doc.work) !== doc.base;
  if (doc.dirty) writeDraft(); else { dropDraft(); doc.staleDraft = false; }
}

export function discard() {
  dropDraft();
  doc.work = clone(doc.disk);
  if (!doc.work.cards) doc.work.cards = {};
  doc.dirty = false;
  doc.staleDraft = false;
}

// ---- per card ----
const FIELDS = ["level", "bloom", "limit_break"];
export const entryOf = id => (doc.work.cards || {})[id] || null;
const diskEntry = id => ((doc.disk && doc.disk.cards) || {})[id] || null;

// What the card IS in the working copy. A value typed in the draft wins; a value the draft
// removed (back to the default) is unknown until the server resolves it on save → `pending`.
export function valuesOf(card) {
  const e = entryOf(card.id) || {};
  const d = diskEntry(card.id) || {};
  const out = { pending: false, typed: false };
  for (const f of FIELDS) {
    if (e[f] !== undefined) { out[f] = e[f]; out.typed = true; }
    else if (d[f] !== undefined) { out[f] = null; out.pending = true; }
    else out[f] = card[f];
  }
  out.changed = canon(e) !== canon(d);
  return out;
}

export function setCard(id, vals) {
  const cards = doc.work.cards || (doc.work.cards = {});
  const e = Object.assign({}, cards[id] || {});
  for (const f of FIELDS) {
    if (!(f in vals)) continue;
    if (vals[f] === null || vals[f] === "" || vals[f] === undefined) delete e[f];
    else e[f] = Number(vals[f]);
  }
  // An entry with nothing in it is no claim at all: leaving {} would pin the card off its default.
  if (Object.keys(e).length) cards[id] = e; else delete cards[id];
  touch();
}

export function clearCard(id) {
  if (doc.work.cards) delete doc.work.cards[id];
  touch();
}

// ---- cards tried as if owned ----
// `trial` in the document: the search pool takes them beside the owned cards, the next account
// refresh clears the list (the account does not have them). A tried card always carries typed
// values: with none, the Member Power-Up cannot be derived exactly and drops for EVERY team.
export const isTrial = id => (doc.work.trial || []).includes(id);
export function setTrial(card, on, maxed) {
  const list = new Set(doc.work.trial || []);
  if (on) {
    list.add(card.id);
    if (!entryOf(card.id)) setCard(card.id, maxed);
  } else {
    list.delete(card.id);
    if (doc.work.cards) delete doc.work.cards[card.id];      // an unowned card's values are only for the try
  }
  if (list.size) doc.work.trial = [...list].sort(); else delete doc.work.trial;
  touch();
}

// ---- account scalars ----
export const player = () => doc.work.player || (doc.work.player = {});

// ---- save: the one existing write route ----
export async function save() {
  const r = await fetch("/api/collection", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(doc.work),
  });
  let body = {};
  try { body = await r.json(); } catch (_) { body = { error: `HTTP ${r.status}, not JSON` }; }
  if (!r.ok) throw new Error(body.error || `HTTP ${r.status}`);
  dropDraft();
  return body;
}
