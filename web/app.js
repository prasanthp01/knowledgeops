"use strict";
const API = "/api/projects/knowledgeops";
const $ = (selector) => document.querySelector(selector);
const state = { csrf: null, run: null, events: [], result: null, previous: null, input: null, activeInput: null, source: null, view: "overview", busy: false, generation: 0 };
const labels = {
  RATE_LIMIT: "Please wait a minute before trying again.", CAPACITY_LIMIT: "The demo is busy. Please try again shortly.",
  SESSION_REQUIRED: "Your demo session expired. Reload the page to start a new session.",
  EVIDENCE_CHANGED: "The source permissions or versions changed. Run a fresh search.",
  REPLAY_INPUT_MISMATCH: "Replay requires the original input. Run a new search for edited input.",
  DEPENDENCY_UNAVAILABLE: "The search service is temporarily unavailable. Please try again.",
  RUN_NOT_FOUND: "This run has expired or is unavailable in your session.",
};

function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = text;
  if (className) node.className = className;
  return node;
}

async function api(path, options = {}) {
  const response = await fetch(API + path, {
    credentials: "same-origin", ...options,
    headers: { "Content-Type": "application/json", ...(state.csrf ? { "X-CSRF-Token": state.csrf } : {}), ...options.headers },
  });
  const data = await response.json();
  if (!response.ok) {
    const code = data.error?.code || "SERVICE_ERROR";
    throw new Error(labels[code] || `The request could not be completed (${code}).`);
  }
  return data;
}

function showError(message) {
  $("#form-error").textContent = message;
  $("#form-error").hidden = false;
}

function setBusy(busy) {
  state.busy = busy;
  $("#run").disabled = busy;
  $("#cancel").hidden = !busy;
  $("#replay").disabled = busy;
  $("#run").textContent = busy ? "Searching the sources…" : "Run the search →";
}

function inputValue() {
  return {
    question: $("#question").value.trim(), role: $("#role").value, corpus: "atlas-synthetic",
    collections: Array.from(document.querySelectorAll('[name="collections"]:checked'), (n) => n.value),
    algorithm: $("#algorithm").value, top_k: Number($("#top-k").value), revision: $("#revision").value,
    keyword_filter: $("#keyword").value.trim(), seed: 42,
  };
}

function fillPreset(preset, button) {
  $("#question").value = preset.input.question;
  $("#role").value = preset.input.role;
  $("#revision").value = preset.input.revision;
  $("#keyword").value = "";
  document.querySelectorAll('[name="collections"]').forEach((n) => { n.checked = true; });
  document.querySelectorAll(".presets button").forEach((n) => { n.classList.toggle("active", n === button); n.setAttribute("aria-pressed", String(n === button)); });
  $("#replay").disabled = true;
}

function renderTrace() {
  const timeline = $("#timeline");
  timeline.hidden = state.view === "json";
  $("#json-trace").hidden = state.view !== "json";
  timeline.className = "timeline" + (state.view === "technical" ? " technical" : "");
  $("#json-trace").textContent = JSON.stringify(state.events, null, 2);
  timeline.replaceChildren();
  if (!state.events.length) timeline.append(el("p", "Waiting for the server to report the first step.", "quiet"));
  state.events.forEach((event) => {
    const item = el("article", null, "timeline-item");
    item.append(el("span", String(event.sequence).padStart(2, "0"), "timeline-index"), el("h3", event.title), el("p", event.overview));
    if (state.view === "technical") {
      item.append(el("p", `${event.mode} · ${event.status} · ${event.component}`, "quiet"));
      item.append(el("pre", JSON.stringify(event.technical, null, 2)));
    }
    timeline.append(item);
  });
}

function addEvent(event) {
  if (!state.events.some((e) => e.sequence === event.sequence)) {
    state.events.push(event);
    state.events.sort((a, b) => a.sequence - b.sequence);
    renderTrace();
  }
}

function renderResult(result) {
  const root = $("#result-content");
  root.replaceChildren();
  const out = result.output;
  root.append(el("div", result.mode === "REPLAY_RECORDED" ? "RECORDED REPLAY · ORIGINAL INPUT" : "EXTRACTIVE ANSWER · NO AI GENERATION", "answer-label"));
  if (out.abstention) {
    const box = el("div", null, "abstention");
    const conflict = out.abstention === "CONFLICTING_REVISIONS";
    box.append(el("h3", conflict ? "The sources disagree." : "Not enough evidence."),
      el("p", conflict ? "Permitted policy revisions conflict. Inspect the passages below and request a policy owner review." : "No permitted passage supports this question. Try a more specific question or a different fictional role."));
    root.append(box);
  } else {
    out.answer.forEach((claim) => {
      const index = out.citations.findIndex((c) => c.id === claim.citation_id);
      const block = el("blockquote", claim.text, "answer-quote");
      const link = el("a", `[${index + 1}]`);
      link.href = `#citation-${index + 1}`;
      link.setAttribute("aria-label", `Inspect source ${index + 1}`);
      link.addEventListener("click", () => { const details = $(`#citation-${index + 1}`); if (details) details.open = true; });
      block.append(link); root.append(block);
    });
  }
  out.warnings.forEach((w) => root.append(el("p", w, "answer-note")));
  if (out.citations.length) {
    const list = el("div", null, "source-list"); list.append(el("p", "INSPECT THE SOURCE PASSAGES"));
    out.citations.forEach((citation, i) => {
      const detail = el("details", null, "citation"); detail.id = `citation-${i + 1}`;
      const summary = el("summary", `[${i + 1}] ${citation.title}`);
      summary.append(el("span", `v${citation.version} · ${citation.section}`));
      detail.append(summary, el("p", citation.quote), el("small", `Source: ${citation.source_path} · page ${citation.page} · chunk ${citation.chunk_id}`),
        el("small", `${out.retrieval.algorithm.toUpperCase()} score ${citation.score} · Query term coverage ${Math.round(citation.query_coverage * 100)}%`),
        el("small", `SHA-256: ${citation.source_checksum}`));
      list.append(detail);
    }); root.append(list);
  }
  const metrics = el("div", null, "run-metrics");
  metrics.append(el("span", `${result.duration_ms.toFixed(1)} ms measured execution`), el("span", "€0 external AI spend"), el("span", `${out.permission.role} · ${out.retrieval.revision}`));
  root.append(metrics, el("p", "Access filters applied. Restricted document details are not disclosed.", "answer-note"));
  $("#result-actions").hidden = false;
  $("#trace-caption").textContent = result.mode === "REPLAY_RECORDED" ? "Recorded evidence replayed with unchanged input" : "Actual server execution · synthetic data";
  $("#run-status").textContent = out.abstention ? "Completed · abstained" : "Completed · cited";
  if (state.previous) {
    const previous = state.previous.result.output;
    const comparison = $("#comparison"); comparison.hidden = false; comparison.replaceChildren();
    comparison.append(el("h3", "Compare with the previous run"),
      el("p", `Previous: ${previous.permission.role} / ${previous.retrieval.revision} — ${previous.abstention || previous.citations.length + " cited passages"}`),
      el("p", `Current: ${out.permission.role} / ${out.retrieval.revision} — ${out.abstention || out.citations.length + " cited passages"}`));
    const oldIds = previous.citations.map((c) => c.id).join(", ");
    const newIds = out.citations.map((c) => c.id).join(", ");
    comparison.append(el("p", oldIds === newIds ? "The permitted source references are unchanged." : "The permitted source references changed."));
  }
}

async function finish(status, generation) {
  if (generation !== state.generation || !state.busy) return;
  state.source?.close();
  if (status !== "SUCCEEDED") {
    $("#run-status").textContent = status === "CANCELLED" ? "Run cancelled" : "Run ended";
    if (status !== "CANCELLED") showError("The run did not complete. Please try a fresh search.");
    setBusy(false); return;
  }
  try {
    state.result = await api(`/simulations/${state.run}/result`);
    state.input = structuredClone(state.activeInput);
    renderResult(state.result);
  } catch (error) { showError(error.message); }
  setBusy(false);
  $("#replay").disabled = JSON.stringify(inputValue()) !== JSON.stringify(state.input);
}

async function poll(generation) {
  const started = Date.now();
  while (generation === state.generation && state.busy) {
    try {
      const after = state.events.at(-1)?.sequence || 0;
      const data = await api(`/simulations/${state.run}/events?format=json&after=${after}`);
      data.events.forEach(addEvent);
      if (!["ACCEPTED", "RUNNING"].includes(data.status)) { await finish(data.status, generation); return; }
      if (Date.now() - started > 40000) throw new Error("The run is taking longer than expected. Cancel or try again later.");
      await new Promise((resolve) => setTimeout(resolve, 600));
    } catch (error) { if (generation === state.generation) { showError(error.message); setBusy(false); } return; }
  }
}

function watch(generation) {
  if (!("EventSource" in window)) { void poll(generation); return; }
  const source = new EventSource(`${API}/simulations/${state.run}/events`); state.source = source;
  source.addEventListener("step", (event) => { if (generation === state.generation) addEvent(JSON.parse(event.data)); });
  source.addEventListener("done", (event) => { source.close(); void finish(JSON.parse(event.data).status, generation); });
  source.addEventListener("blocked", (event) => { source.close(); showError(labels[JSON.parse(event.data).code] || "This evidence is no longer available."); setBusy(false); });
  source.onerror = () => { source.close(); if (generation === state.generation && state.busy) void poll(generation); };
}

async function start(replay = false) {
  if (state.busy) return;
  $("#form-error").hidden = true;
  const input = inputValue();
  if (!input.collections.length) { showError("Select at least one collection in Search controls."); return; }
  const sourceRun = state.run;
  if (state.result) state.previous = { result: state.result, input: state.input };
  state.result = null; state.activeInput = input; state.events = []; state.generation += 1;
  const generation = state.generation;
  state.source?.close(); setBusy(true); renderTrace();
  $("#result-actions").hidden = true; $("#run-status").textContent = "Submitting your question…";
  $("#result-content").replaceChildren(el("p", "Waiting for permitted evidence from the server…", "quiet"));
  try {
    if (!state.csrf) state.csrf = (await api("/session", { method: "POST", body: "{}" })).csrf_token;
    const request = { schema_version: "1.0", project_id: "knowledgeops", mode: replay ? "REPLAY_RECORDED" : "SANDBOX_DETERMINISTIC", input,
      ...(replay ? { replay_run_id: sourceRun } : {}) };
    const run = await api("/simulations", { method: "POST", headers: { "Idempotency-Key": crypto.randomUUID() }, body: JSON.stringify(request) });
    state.run = run.run_id; $("#run-status").textContent = "Run accepted · searching"; watch(generation);
  } catch (error) { showError(error.message); $("#run-status").textContent = "Could not start"; setBusy(false); }
}

$("#search-form").addEventListener("submit", (event) => { event.preventDefault(); void start(); });
$("#search-form").addEventListener("input", () => { $("#replay").disabled = true; });
$("#cancel").addEventListener("click", async () => { try { await api(`/simulations/${state.run}/cancel`, { method: "POST", body: "{}" }); } catch (error) { showError(error.message); } });
$("#replay").addEventListener("click", () => void start(true));
$("#download").addEventListener("click", () => {
  if (!state.result) return;
  const blob = new Blob([JSON.stringify({ result: state.result, events: state.events }, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob), link = el("a"); link.href = url; link.download = `knowledgeops-${state.run}.json`; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
});
document.querySelectorAll("[data-view]").forEach((button) => button.addEventListener("click", () => {
  state.view = button.dataset.view;
  document.querySelectorAll("[data-view]").forEach((b) => b.setAttribute("aria-pressed", String(b === button)));
  renderTrace();
}));

const sidebar = $("#sidebar"), menu = $("#menu");
function setMenu(open) {
  sidebar.classList.toggle("open", open); $("#scrim").hidden = !open; document.body.classList.toggle("menu-open", open);
  menu.setAttribute("aria-expanded", String(open)); $("#main").inert = open;
  if (open) { sidebar.setAttribute("role", "dialog"); sidebar.setAttribute("aria-modal", "true"); $("#close-menu").focus(); }
  else { sidebar.removeAttribute("role"); sidebar.removeAttribute("aria-modal"); menu.focus(); }
}
menu.addEventListener("click", () => setMenu(true)); $("#close-menu").addEventListener("click", () => setMenu(false)); $("#scrim").addEventListener("click", () => setMenu(false));
sidebar.addEventListener("click", (event) => { if (event.target.closest("a") && sidebar.classList.contains("open")) setMenu(false); });
document.addEventListener("keydown", (event) => {
  if (!sidebar.classList.contains("open")) return;
  if (event.key === "Escape") setMenu(false);
  if (event.key === "Tab") {
    const items = Array.from(sidebar.querySelectorAll("a,button")).filter((n) => n.offsetParent !== null), first = items[0], last = items.at(-1);
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }
});
window.matchMedia("(min-width: 901px)").addEventListener("change", (event) => { if (event.matches && sidebar.classList.contains("open")) setMenu(false); });
window.addEventListener("pagehide", () => state.source?.close());

async function boot() {
  try {
    const presets = await api("/simulation/presets");
    presets.forEach((preset, i) => { const button = el("button", preset.title); button.type = "button"; button.setAttribute("aria-pressed", String(i === 0)); if (i === 0) button.classList.add("active"); button.addEventListener("click", () => fillPreset(preset, button)); $("#presets").append(button); });
  } catch (error) { showError(error.message); }
  try {
    const evidence = await (await fetch("/projects/knowledgeops/evidence.json")).json();
    const evaluation = evidence.tests?.find((t) => t.name === "retrieval_evaluation");
    if (evaluation?.status === "PASS") { $("#metric-quality").textContent = `${evaluation.recall_at_k_percent}% recall@3`; $("#metric-quality-detail").textContent = `${evaluation.question_count} held-out synthetic questions. Citation validity ${evaluation.citation_validity_percent}%. See manifest for methodology and limits.`; }
    const benchmark = evidence.benchmarks?.find((b) => b.name === "retrieval_local");
    if (benchmark?.p95_ms !== undefined) { $("#metric-latency").textContent = `${benchmark.p95_ms.toFixed(1)} ms P95`; $("#metric-latency-detail").textContent = `${benchmark.sample_count} local search requests, ${benchmark.concurrency} concurrent clients. PostgreSQL and application on the same host; not a VPS capacity claim.`; }
  } catch { /* Explicit unmeasured labels remain visible. */ }
}
void boot();
