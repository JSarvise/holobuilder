// The park screen: today's hidden collectables, drawn on the game's own maps.
//
// What it answers: WHERE ARE TODAY'S 7 RANDOM SYMBOLS, and
// which are still out there. There is no RNG to reverse — the server rolls the
// picks and hands the client the chosen list in plaintext on User/Get, so the
// screen READS today's state, it does not predict it. Reading
// `picked_park_target_mapping_ids` is a field access.
//
// TWO FACTS THIS SCREEN MUST NOT FLATTEN:
//   * `last_refreshed_time` is PER AREA and the areas diverge (a capture caught
//     the casino a full day behind the other two). So every area prints ITS OWN
//     roll time; there is no single "today's roll" and printing one would be a
//     claim the data does not support.
//   * The roll is LAZY — it happens on the first server contact after the 05:00
//     JST reset, not at the boundary. An area you have not entered can still be
//     showing yesterday's pick, so a roll time from before the last reset is
//     surfaced as STALE rather than hidden behind a friendly "today".
//
// ⚠ The positions are ~1 % uncertain: a marker means
// "this spot, within a marker's width", not a pixel. The payload carries that
// caveat in `note` and the screen keeps it visible.
//
// The 42 fixed chests are drawn as a muted background layer, never the headline
// and never something to go find.
//
// The screen invents no visual language of its own — it reuses the page's
// tokens (.lede/.note/.banner/.tag/.ghost).

const ONE_HOUR = 3600 * 1000;
const ONE_DAY = 24 * ONE_HOUR;

import { el } from "/dom.js";

// The most recent 05:00-JST reset boundary at or before `nowMs`. JST is a fixed
// UTC+9 with no DST, so shifting into it and reading the wall clock is exact —
// this does not depend on where the reader's machine is. A roll stamped before
// this boundary is from a game-day the area has not re-rolled into yet.
function lastResetBefore(nowMs) {
  const jst = new Date(nowMs + 9 * ONE_HOUR);
  let boundary =
    Date.UTC(jst.getUTCFullYear(), jst.getUTCMonth(), jst.getUTCDate(), 5, 0, 0)
    - 9 * ONE_HOUR;
  if (boundary > nowMs) boundary -= ONE_DAY;
  return boundary;
}

// Absolute local time, in the reader's own zone, plus a coarse "how long ago".
// The absolute time is the fact; the relative form is only there so a stale
// stamp jumps out without the reader doing date arithmetic.
function describeRoll(rolledAt, nowMs) {
  const when = new Date(rolledAt);
  const abs = when.toLocaleString(undefined, {
    year: "numeric", month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit",
  });
  const ago = nowMs - rolledAt;
  let rel;
  if (ago < ONE_HOUR) rel = Math.max(1, Math.round(ago / 60000)) + " min ago";
  else if (ago < ONE_DAY) rel = Math.round(ago / ONE_HOUR) + " h ago";
  else rel = Math.round(ago / ONE_DAY) + " d ago";
  return { abs, rel };
}

// One marker on the map. Position is a PERCENTAGE of the source image, so it
// holds at whatever size the map is drawn — the maps are 1551–2048 px and are
// shown far smaller. `translate(-50%,-50%)` centres the dot on the point.
function marker(spot, width, height, kind, label) {
  const m = el("span", "pk-mark pk-" + kind);
  m.style.left = (spot.x / width) * 100 + "%";
  m.style.top = (spot.y / height) * 100 + "%";
  if (label) m.title = label;
  return m;
}

function areaLabel(id) {
  // The payload ids are the game's own keys; these are just friendlier names
  // for the reader. An id with no friendly form falls back to the raw key
  // rather than to nothing.
  const names = {
    "area-a01": "Park (a01)",
    "area-a02-game": "Game corner",
    "area-a02-casino": "Casino",
    "area-a02-lobby": "Lobby",
  };
  return names[id] || id;
}

function renderArea(area, nowMs, showCandidates) {
  const card = el("section", "pk-area");

  // The picks and the count. "still out" is what the reader is here for, so it
  // leads; the total says how many the area drew today.
  const outstanding = area.picked.filter((p) => !p.collected);
  const head = el("div", "pk-head");
  head.append(el("h3", "pk-name", areaLabel(area.id)));

  const count = el("span", "pk-count");
  if (!area.picked.length) {
    // The lobby legitimately draws no random symbols — it has a map and spots
    // but no candidate pool — so an empty pick is a fact, not a missing read.
    count.classList.add("pk-count-none");
    count.textContent = "no symbols roll here";
  } else if (outstanding.length) {
    count.classList.add("pk-count-out");
    count.textContent = outstanding.length + " of " + area.picked.length + " still out";
  } else {
    count.classList.add("pk-count-done");
    count.textContent = "all " + area.picked.length + " collected";
  }
  head.append(count);
  card.append(head);

  // This area's OWN roll time. Never a global "today". A roll stamped before the
  // last 05:00-JST reset means the area has not been entered since the reset, so
  // it is showing a pick from an earlier game-day — surfaced, not smoothed over.
  const roll = el("p", "pk-roll");
  if (area.rolled_at) {
    const { abs, rel } = describeRoll(area.rolled_at, nowMs);
    const stale = area.rolled_at < lastResetBefore(nowMs);
    roll.append(el("span", "pk-roll-lab", "rolled"));
    roll.append(el("span", "pk-roll-when", abs + " (" + rel + ")"));
    if (stale) {
      roll.classList.add("pk-stale");
      const tag = el("span", "pk-stale-tag", "before today's reset");
      tag.title =
        "This area's roll predates the most recent 05:00-JST reset. The roll is "
        + "lazy — it happens on first entry after the reset, not at the boundary "
        + "— so this area has not been entered yet today and may still be showing "
        + "an earlier day's pick.";
      roll.append(tag);
    }
  } else {
    roll.append(el("span", "pk-roll-lab", "roll time unknown"));
  }
  card.append(roll);

  // The map, with markers layered over it. The wrapper takes its aspect ratio
  // from the payload's width/height because the four maps differ (1551×2048,
  // 2048×2048, 1851×2048) — assuming one ratio would letterbox or crop three
  // of them.
  const mapWrap = el("div", "pk-map");
  mapWrap.style.aspectRatio = area.width + " / " + area.height;
  const img = el("img", "pk-img");
  img.src = area.image;
  img.alt = "Map of " + areaLabel(area.id);
  img.loading = "lazy";
  mapWrap.append(img);

  // Candidate pool underneath everything (optional), then treasures (muted
  // background), then the picks on top — the picks are the subject and must not
  // be hidden behind a chest or a candidate dot.
  if (showCandidates) {
    for (const c of area.candidates || []) {
      mapWrap.append(marker(c, area.width, area.height, "cand",
        "candidate spot " + c.spot));
    }
  }
  for (const t of area.treasures || []) {
    // A chest is a muted dot, present for orientation, never a task.
    mapWrap.append(marker(t, area.width, area.height,
      t.opened ? "chest-open" : "chest", "fixed chest " + t.spot
        + (t.opened ? " (opened)" : "")));
  }
  for (const p of area.picked) {
    const kind = p.collected ? "got" : "out";
    const label = (p.collected ? "collected — " : "still out — ") + p.spot;
    mapWrap.append(marker(p, area.width, area.height, kind, label));
  }
  card.append(mapWrap);

  return card;
}

function renderLegend(showCandidates) {
  const legend = el("div", "pk-legend");
  const item = (kind, text) => {
    const row = el("span", "pk-leg-item");
    row.append(el("span", "pk-mark pk-" + kind + " pk-leg-dot"));
    row.append(el("span", "pk-leg-text", text));
    return row;
  };
  legend.append(item("out", "still out there"));
  legend.append(item("got", "already collected"));
  legend.append(item("chest-open", "fixed chest (all 42 opened)"));
  if (showCandidates) legend.append(item("cand", "a spot a symbol could spawn on"));
  return legend;
}

function render(body, data, controls) {
  body.textContent = "";

  // The one sentence the screen opens with: what this is and how to read it.
  const lede = el("p", "lede");
  lede.textContent =
    "Today's hidden random symbols, read from your account and drawn on the "
    + "game's own maps. Bright markers are still out there; dimmed ones you have "
    + "already collected.";
  body.append(lede);

  // The re-read control. The route triggers a LIVE LOGIN, so nothing is fetched
  // on page load — the reader asks for it here, explicitly, and can ask again.
  const bar = el("div", "pk-bar");
  const reread = el("button", "ghost", "Re-read from the game");
  reread.title =
    "Logs into your account and reads today's park state fresh. Not free — it "
    + "is a real login — so it runs only when you ask.";
  reread.addEventListener("click", () => controls.reload());
  bar.append(reread);
  const status = el("span", "pk-status");
  bar.append(status);
  body.append(bar);
  controls.status = status;

  if (!data.available) {
    // First-class absence: a reason and, where the payload gives one, the
    // command that fixes it — never a blank screen or a silent empty list.
    const box = el("p", "banner warn");
    box.textContent = data.reason
      || "Today's park state is not available right now.";
    body.append(box);
    if (data.fix) {
      const fix = el("pre", "mono pk-fix");
      fix.textContent = data.fix;
      body.append(fix);
    }
    return;
  }

  // The ~1 % caveat travels with the data, so it is shown, not paraphrased into
  // false precision. A marker is "this spot, within a marker's width".
  if (data.note) {
    const note = el("p", "note pk-note");
    note.textContent = data.note;
    body.append(note);
  }

  // The per-area headline count, stated as three separate figures because the
  // rolls are per-area and there is no single daily total to claim. Areas that
  // roll symbols show "N still out of M"; the lobby, which rolls none, is left
  // out of this line rather than reported as 0/0.
  const rolling = (data.areas || []).filter((a) => a.picked.length);
  if (rolling.length) {
    const parts = rolling.map((a) => {
      const out = a.picked.filter((p) => !p.collected).length;
      return areaLabel(a.id) + " " + out + "/" + a.picked.length;
    });
    const summary = el("p", "pk-summary");
    summary.append(el("b", null, "Still out, by area: "));
    summary.append(document.createTextNode(parts.join("  ·  ")));
    body.append(summary);
  }

  // The candidate-pool toggle. Off by default — the pool is 98/50/50 spots and
  // drowns the two markers that matter; it is an overlay for the curious, not
  // the answer.
  const toggleRow = el("label", "pk-toggle");
  const cb = el("input");
  cb.type = "checkbox";
  cb.checked = controls.showCandidates;
  cb.addEventListener("change", () => {
    controls.showCandidates = cb.checked;
    controls.rerender();
  });
  toggleRow.append(cb);
  toggleRow.append(document.createTextNode(
    " Show every spot a symbol could spawn on"));
  body.append(toggleRow);

  body.append(renderLegend(controls.showCandidates));

  const grid = el("div", "pk-grid");
  const now = Date.now();
  for (const area of data.areas || []) {
    grid.append(renderArea(area, now, controls.showCandidates));
  }
  body.append(grid);

  // The chests, said once, at the bottom, as background. Three of the 42 sit in
  // areas that ship no map asset, so they cannot be placed — said plainly so
  // their absence never reads as "collected" or as a bug.
  const chestNote = el("p", "note pk-chest-note");
  chestNote.textContent =
    "The 42 fixed treasure chests are all opened on this account, so they are "
    + "only background here. Three of them live in areas with no map (house, "
    + "restaurant, theater) and cannot be drawn at all.";
  body.append(chestNote);
}

// One loader. `#pbody` is fetched only when the view is opened or the reader
// presses re-read, because the route triggers a live login. The returned data
// is kept in `controls.lastData` so the candidate toggle can redraw without a
// second login.
async function load(body, controls) {
  if (controls.status) {
    controls.status.textContent = "Reading today's park state…";
  }
  let data;
  try {
    const response = await fetch("/api/park");
    data = await response.json();
  } catch (error) {
    // The transport failed, which is not the same as the route reporting an
    // unavailable state — say which it was.
    data = {
      available: false,
      reason: "Could not reach the local server for /api/park: " + error,
    };
  }
  controls.lastData = data;
  render(body, data, controls);
}

export function mountPark() {
  const wrap = document.getElementById("pwrap");
  const body = document.getElementById("pbody");
  // `#pwrap` may not be in the page at all, in which case app.js has already
  // dropped the view and there is no tab to reach. A quiet no-op, not an error.
  if (!wrap || !body) return;

  // State the view keeps between renders: the candidate toggle, the last data
  // read, and the handles the re-read button and the toggle call.
  const controls = {
    showCandidates: false,
    lastData: null,
    status: null,
    reload: () => load(body, controls),
    rerender: () => {
      // The toggle redraws from the data already in hand — it must NOT re-fetch,
      // because a fetch is a live login and flipping an overlay is not a reason
      // to log in again.
      if (controls.lastData) render(body, controls.lastData, controls);
    },
  };

  // Lazy: fetch on the FIRST time the view is opened, not on page load — the
  // route triggers a live login. Same toggle pattern the collection and board
  // panels use. Re-opening the tab does not re-fetch; the re-read button is the
  // explicit way to pull fresh state.
  let everLoaded = false;
  wrap.addEventListener("toggle", function () {
    if (!this.open || everLoaded) return;
    everLoaded = true;
    load(body, controls);
  });
}
