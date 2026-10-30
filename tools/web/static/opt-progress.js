// The Optimizer's search progress, one look for Score and Event: the stage
// and what it is doing, elapsed (+ the step's time left when the server counts it), Cancel, the running chibi on the bar
// (hb-runner.js), the provisional best, and the stages as chips (blue = now, green = done).
// Built ONCE per search and then only its texts change: re-rendering it every tick would swallow
// Cancel clicks (a press and a release landing on two different buttons) and jolt the bar.
import { esc, fmt } from "/opt-util.js";
import { score } from "/hb-grade.js";
import { runner, pace } from "/hb-runner.js";

const secs = s => s == null ? "—" : s >= 60 ? `${Math.floor(s / 60)}m ${String(Math.round(s % 60)).padStart(2, "0")}s` : `${Math.round(s)} s`;

// kind: what the pace history is kept under (searches of one kind take alike); steps: labels;
// best: false for a search that sends no provisional best (the Event search)
export function progressCard(kind, steps, { best: withBest = true } = {}) {
  const p = pace(kind, steps.length), rn = runner();
  const el = document.createElement("div");
  el.className = "o-progress";
  el.setAttribute("role", "status");
  el.innerHTML = `
    <div class="row"><span><b class="pc-title"></b> <span class="num pc-detail"></span></span>
      <span class="num pc-time"></span>
      <button class="btn small" type="button" data-cancel>Cancel</button></div>
    <div class="pc-run"></div>
    ${withBest ? `<div class="pc-best"></div>` : ""}
    <ol class="o-steps">${steps.map(s => `<li><i></i>${esc(s)}</li>`).join("")}</ol>`;
  el.querySelector(".pc-run").replaceWith(rn.el);
  rn.follow(p);
  const $ = c => el.querySelector(c);
  let active = -1, live = true, stageT = 0, count = null;
  // a time left is shown only when the server sends a real count (done / total) for the
  // stage: the rate since the stage began, extrapolated. The searches' rounds have no total,
  // and an ETA from past searches is unreliable, so then: elapsed only
  const tick = () => {
    if (!live) return;
    let left = "";
    if (count && count.done > 0 && count.total > count.done) {
      const t = p.elapsed() - stageT;
      left = ` · this step ~${secs(t * (count.total - count.done) / count.done)} left`;
    }
    $(".pc-time").textContent = secs(p.elapsed()) + left;
  };
  const timer = setInterval(tick, 500);
  const card = {
    el,
    // active: the stage index; detail: a short live line; best: provisional best score or null
    update({ active: a = active, detail, best, note, done, total } = {}) {
      if (a !== active && a >= 0) {
        active = a;
        p.step(a);
        stageT = p.elapsed(); count = null;
        $(".pc-title").textContent = steps[a] || "Searching";
        el.querySelectorAll(".o-steps li").forEach((li, i) => { li.className = i < a ? "done" : i === a ? "on" : ""; });
      }
      if (total > 0 && done != null) count = { done, total };
      if (detail !== undefined) $(".pc-detail").textContent = detail || "";
      if (withBest && (best !== undefined || note !== undefined)) {
        $(".pc-best").innerHTML = (best != null ? `Provisional best ${score(best)}` : "No provisional best yet")
          + (note ? ` <span class="sub">${esc(note)}</span>` : "");
      }
      tick();
    },
    // the search answered: remember how long each stage took (the next ETA learns from it)
    done() { if (live) p.done(); card.stop(); },
    stop() { live = false; clearInterval(timer); rn.stop(); },
  };
  card.update({ active: 0, best: null });
  return card;
}

// "12,345 teams scored · round 3"
export const countLine = ev => {
  const e = ev || {};
  const n = e.done ?? e.evaluated ?? e.scored;
  return [n ? `${fmt(n)}${e.total ? ` / ${fmt(e.total)}` : ""} teams scored` : "", e.round ? `round ${e.round}` : ""].filter(Boolean).join(" · ");
};
