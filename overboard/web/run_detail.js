"use strict";

// ---- run detail (the Scheduler panel's right pane for a picked run) --------
// A port of the Mac app's RunDetailPane. A finished run shows what it
// *reported* — the closing message from the Stop hook, as prose — plus a
// facts card and the raw terminal capture behind a disclosure (a screen
// scrape full of spinner frames is not a report). A live run shows its state,
// the waiting-for-input callout, and the Herdr actions. Built by
// scheduler.js renderRight(); rebuilt only when the record changes.

function _factsCard(rows) {
  const card = el("div", "facts");
  for (const [k, v, mono] of rows) {
    if (v == null || v === "") continue;
    const row = el("div", "facts-row");
    row.appendChild(el("span", "facts-k", k));
    const val = el("span", "facts-v" + (mono ? " mono" : ""), v);
    val.title = typeof v === "string" ? v : "";
    row.appendChild(val);
    card.appendChild(row);
  }
  return card;
}

// Plain text → paragraphs. No markdown library: blank lines split paragraphs,
// single newlines become line breaks, everything is escaped.
function _prose(text) {
  const box = el("div", "prose");
  const paras = String(text || "").trim().split(/\n\s*\n/);
  for (const p of paras) {
    const pe = document.createElement("p");
    pe.innerHTML = escapeHtml(p).replace(/\n/g, "<br>");
    box.appendChild(pe);
  }
  return box;
}

function _commandLine(run) {
  const cmd = (run.prompt || run.command || "").replace(/\s+/g, " ");
  return cmd.length > 160 ? cmd.slice(0, 160) + "…" : cmd;
}

function runDetailPane(run) {
  const live = run.state != null && run.state !== "";
  const pane = el("div", "pane run-detail");

  // Header: name · status capsule · ago · actions.
  const head = el("div", "pane-head");
  const title = el("div", "run-title");
  title.appendChild(el("h3", null, run.slot_name));
  const meta = el("div", "run-title-meta");
  if (live) {
    const [label, tone] = runStateInfo(run);
    meta.appendChild(el("span", `run-capsule ${tone}`, label));
    if (run.started_at && run.state !== "queued") {
      meta.appendChild(el("span", "subtle", "started " + schedDuration(run.started_at, Date.now()) + " ago"));
    }
  } else {
    const [glyph, label, tone] = outcomeInfo(run);
    meta.appendChild(el("span", `run-capsule ${tone}`, `${glyph} ${label}`));
    if (run.taken_over) meta.appendChild(el("span", "subtle", "you had control"));
    meta.appendChild(el("span", "subtle", ago(new Date(run.ended_at || run.started_at).getTime() / 1000)));
  }
  title.appendChild(meta);
  head.appendChild(title);
  const acts = el("div", "pane-acts");
  if (live) {
    const stop = el("button", "btn ghost small danger", "Stop");
    stop.addEventListener("click", () => schedCall("stop_run", { run_id: run.id }));
    acts.appendChild(stop);
  } else {
    const rm = el("button", "btn ghost small", "Remove");
    rm.title = "Remove this run from history";
    rm.addEventListener("click", async () => {
      if (await schedCall("remove_run", { run_id: run.id })) schedSelect({ kind: "new", id: null });
    });
    acts.appendChild(rm);
  }
  const x = el("button", "btn ghost small", "✕");
  x.title = "Back to the editor";
  x.addEventListener("click", () => schedSelect({ kind: "new", id: null }));
  acts.appendChild(x);
  head.appendChild(acts);
  pane.appendChild(head);

  const body = el("div", "pane-body");
  if (live) liveRunCard(body, run);
  else finishedRunCard(body, run);
  pane.appendChild(body);
  return pane;
}

function liveRunCard(body, run) {
  if (run.state === "queued") {
    body.appendChild(el("p", "subtle", "Queued — it starts as soon as a concurrency slot frees up."));
  }
  if (run.waiting_since || run.herdr_state === "blocked") {
    const c = el("div", "callout amber");
    c.appendChild(el("strong", null, "Waiting for an answer"));
    c.appendChild(el("p", null, "The session is sitting on a prompt Overboard won't answer for you. Take it over in Herdr to reply."
      + (run.waiting_since ? ` Waiting since ${schedDateTime(run.waiting_since)}.` : "")));
    body.appendChild(c);
  } else if (run.stalled_at) {
    const c = el("div", "callout amber");
    c.appendChild(el("strong", null, "No activity for a while"));
    c.appendChild(el("p", null, `Nothing from the hooks since ${schedDateTime(run.stalled_at)} and Herdr can't read the screen. It may be waiting for input.`));
    body.appendChild(c);
  }
  if (run.taken_over) {
    body.appendChild(el("p", "subtle", "You have control: Overboard won't /exit or close this session. The result is still recorded when it finishes."));
  }
  body.appendChild(el("div", "sec-label pad", "DETAILS"));
  body.appendChild(_factsCard([
    ["Folder", tildify(run.cwd), true],
    ["Workspace", run.workspace_id ? "linked task workspace" : null],
    ["Command", _commandLine(run), true],
    ["Session", run.session_id ? run.session_id.slice(0, 8) + "…" : "not yet claimed from the hooks", true],
    ["Herdr", run.agent_name ? `${run.agent_name} · pane ${String(run.pane_id || "").slice(0, 8)}` : null, true],
    ["Timeout", `${run.timeout_minutes} min`],
  ]));
  if (run.state !== "queued") {
    const row = el("div", "pane-actions");
    const open = el("button", "btn small", "Open in Herdr ↗");
    open.addEventListener("click", () => _openInHerdr(run.id, row));
    row.appendChild(open);
    if (!run.taken_over) {
      const over = el("button", "btn ghost small", "Take over");
      over.title = "Focus the session in Herdr and stop managing it";
      over.addEventListener("click", () => schedCall("take_over_run", { run_id: run.id }));
      row.appendChild(over);
    }
    body.appendChild(row);
    body.appendChild(el("p", "subtle hint", "Live output belongs in Herdr — this pane shows state, not a terminal."));
  }
}

async function _openInHerdr(runId, row) {
  let res;
  try { res = await call("open_in_herdr", { run_id: runId }); } catch (_) { res = null; }
  let hint = row.querySelector(".herdr-hint");
  if (!hint) { hint = el("span", "subtle hint herdr-hint"); row.appendChild(hint); }
  hint.textContent = res && res.ok
    ? "opened a Herdr client in a terminal"
    : `couldn't open a terminal here — run \`${(res && res.command) || "herdr"}\` yourself`;
}

function finishedRunCard(body, run) {
  if (isRunFailure(run) || run.outcome === "skipped_missed_window") {
    const c = el("div", "callout " + (isRunFailure(run) ? "red" : "amber"));
    const [, label] = outcomeInfo(run);
    c.appendChild(el("strong", null, label));
    if (run.reason) c.appendChild(el("p", null, run.reason));
    body.appendChild(c);
  }
  body.appendChild(el("div", "sec-label pad", "WHAT IT REPORTED"));
  if (run.completion_message) {
    body.appendChild(_prose(run.completion_message));
  } else {
    body.appendChild(el("p", "subtle", run.outcome === "completed"
      ? "The session left no closing message."
      : "No report — the run didn't reach a clean finish."));
  }
  body.appendChild(el("div", "sec-label pad", "FACTS"));
  const ran = run.started_at
    ? schedDateTime(run.started_at) + (run.ended_at && run.outcome !== "skipped_missed_window"
      ? ` · ${schedDuration(run.started_at, run.ended_at)}` : "")
    : null;
  body.appendChild(_factsCard([
    ["Folder", tildify(run.cwd), true],
    ["Workspace", run.workspace_id ? "linked task workspace" : null],
    ["Command", _commandLine(run), true],
    ["Ran", ran],
    ["Trigger", run.trigger],
    ["Session", run.session_id ? run.session_id.slice(0, 8) + "…" : null, true],
  ]));
  if (run.has_transcript) {
    const det = el("details", "raw-out-wrap");
    det.appendChild(el("summary", null, "Show raw terminal output"));
    const pre = el("pre", "raw-out", "Loading…");
    det.appendChild(pre);
    det.addEventListener("toggle", async () => {
      if (!det.open || pre.dataset.loaded) return;
      let res;
      try { res = await call("get_run_transcript", { run_id: run.id }); } catch (_) { res = null; }
      pre.textContent = (res && res.text) || "Transcript unavailable.";
      pre.dataset.loaded = "1";
    });
    body.appendChild(det);
  }
}
