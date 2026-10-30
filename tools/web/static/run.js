// run.js — the ONE run (REDESIGN-PLAN.md Phase B): POST /api/run, consume its
// SSE stream, and draw the stepper. Nothing here touches `#result`; the ONLY
// thing that reaches the ranking is the final `result` payload, delivered once
// through `onResult`. State: idle → running → done | cancelled | failed.
//
// Events consumed (server.py `run_job`): plan, stage{stage,state}, progress
// {stage,...}, fact, board, collection, developed, notice, result, cancelled
// {stage}, error{stage,message}, end.
import { el } from "/dom.js";

const STAGE_LABEL = {
  search: "Search", leaders: "Every leader", develop: "Develop", refine: "Refine", rank: "Score",
};

const num = (n) => (typeof n === "number" ? n.toLocaleString("en-US") : String(n ?? ""));

// --- the stepper -------------------------------------------------------------
export function renderStepper(host, plan) {
  host.replaceChildren();
  host.className = "stepper";
  const steps = new Map();
  for (const st of plan || []) {
    const step = el("div", "step step-pending");
    step.dataset.stage = st.id;
    const head = el("div", "step-head");
    head.append(el("span", "step-dot"), el("span", "step-label", st.label || STAGE_LABEL[st.id] || st.id));
    step.append(head);
    const bar = el("div", "step-bar");
    bar.append(el("i"));
    step.append(bar);
    step.append(el("div", "step-detail", st.detail || ""));
    host.append(step);
    steps.set(st.id, { node: step, bar: bar.firstChild, detail: step.lastChild, total: st.total || null,
                       started: null, timer: null, elapsedNode: null });
  }
  return steps;
}

function setState(entry, state) {
  if (!entry) return;
  entry.node.className = `step step-${state}`;
  if (state === "active" && !entry.started) {
    entry.started = performance.now();
    entry.elapsedNode = el("span", "step-elapsed", "0 s");
    entry.node.querySelector(".step-head").append(entry.elapsedNode);
    entry.timer = setInterval(() => {
      if (entry.elapsedNode) {
        entry.elapsedNode.textContent = `${((performance.now() - entry.started) / 1000).toFixed(0)} s`;
      }
    }, 500);
  }
  if (state !== "active" && entry.timer) {
    clearInterval(entry.timer);
    entry.timer = null;
    if (entry.elapsedNode && entry.started) {
      entry.elapsedNode.textContent = `${((performance.now() - entry.started) / 1000).toFixed(1)} s`;
    }
    if (state === "done") entry.bar.style.width = "100%";
  }
}

// --- the run --------------------------------------------------------------------
// `startRun(body, hooks)` -> { cancel(), jobId }.
// hooks: onPlan(plan, opts) onStage(ev) onProgress(ev) onFact(ev) onResult(payload)
//        onCancelled(ev) onError(ev) onEnd(state)
export async function startRun(body, hooks, stepperHost) {
  let started;
  try {
    const response = await fetch("/api/run", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const raw = await response.text();
    try { started = JSON.parse(raw); } catch (_) {
      throw new Error(`the server answered HTTP ${response.status} with "${raw.slice(0, 80)}" — ` +
                      `restart tools/web/server.py so it carries the /api/run route.`);
    }
    if (!response.ok) throw new Error(started.error || `HTTP ${response.status}`);
  } catch (error) {
    if (hooks.onError) hooks.onError({ stage: "start", message: String(error.message || error) });
    if (hooks.onEnd) hooks.onEnd("failed");
    return null;
  }
  const jobId = started.job_id;
  let steps = stepperHost ? renderStepper(stepperHost, started.plan) : new Map();
  if (hooks.onPlan) hooks.onPlan(started.plan, started.opts);
  let finalState = "failed";
  let bestSoFar = null;
  const source = new EventSource("/events/" + jobId);
  source.onmessage = (message) => {
    const ev = JSON.parse(message.data);
    switch (ev.type) {
      case "plan":
        if (stepperHost) steps = renderStepper(stepperHost, ev.stages);
        break;
      case "stage": {
        const entry = steps.get(ev.stage);
        setState(entry, ev.state === "done" ? "done" : "active");
        if (entry && ev.state === "active" && ev.detail) entry.detail.textContent = ev.detail;
        if (entry && ev.total) entry.total = ev.total;
        if (entry && ev.state === "done" && typeof ev.teams === "number") {
          entry.detail.textContent = `${num(ev.teams)} teams`;
        }
        if (entry && ev.state === "done" && typeof ev.refined === "number") {
          entry.detail.textContent = `${ev.refined} refined`;
        }
        if (hooks.onStage) hooks.onStage(ev);
        break;
      }
      case "progress": {
        const entry = steps.get(ev.stage);
        if (entry) {
          setState(entry, "active");
          if (typeof ev.done === "number" && (ev.total || entry.total)) {
            const total = ev.total || entry.total;
            entry.bar.style.width = `${Math.min(100, (100 * ev.done) / total).toFixed(1)}%`;
            entry.detail.textContent = `${ev.done} / ${total}` +
              (ev.sub ? ` (${ev.sub})` : "") +
              (typeof ev.optimised_us === "number" ? ` · last ${num(ev.optimised_us)}` : "");
            if (typeof ev.optimised_us === "number" && (bestSoFar === null || ev.optimised_us > bestSoFar)) {
              bestSoFar = ev.optimised_us;
            }
          } else if (typeof ev.evaluated === "number") {
            entry.detail.textContent = `${num(ev.evaluated)} arrangements`;
            if (ev.complete) entry.bar.style.width = "100%";
            else entry.bar.classList.add("sweep");
          }
        }
        if (hooks.onProgress) hooks.onProgress(ev, bestSoFar);
        break;
      }
      case "fact":
      case "board":
      case "collection":
      case "notice":
      case "developed":
        if (hooks.onFact) hooks.onFact(ev);
        break;
      case "result":
        finalState = "done";
        for (const entry of steps.values()) if (entry.node.classList.contains("step-active")) setState(entry, "done");
        if (hooks.onResult) hooks.onResult(ev.payload);
        break;
      case "cancelled": {
        finalState = "cancelled";
        const entry = steps.get(ev.stage);
        if (entry) setState(entry, "cancelled");
        if (hooks.onCancelled) hooks.onCancelled(ev, entry ? entry.detail.textContent : "");
        break;
      }
      case "error": {
        finalState = "failed";
        const entry = steps.get(ev.stage);
        if (entry) setState(entry, "failed");
        if (hooks.onError) hooks.onError(ev);
        break;
      }
      case "end":
        source.close();
        for (const entry of steps.values()) if (entry.timer) { clearInterval(entry.timer); entry.timer = null; }
        if (hooks.onEnd) hooks.onEnd(finalState);
        break;
    }
  };
  source.onerror = () => {
    source.close();
    for (const entry of steps.values()) if (entry.timer) { clearInterval(entry.timer); entry.timer = null; }
    if (hooks.onError) hooks.onError({ stage: "stream", message: "the event stream closed unexpectedly" });
    if (hooks.onEnd) hooks.onEnd("failed");
  };
  return {
    jobId,
    cancel: () => fetch("/cancel/" + jobId, { method: "POST" }).catch(() => {}),
  };
}
