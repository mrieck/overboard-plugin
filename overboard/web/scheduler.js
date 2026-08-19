"use strict";

// ---- scheduler panel (slots that fire unattended claude runs via herdr) -----
// A full-width panel picked from the strip (#/scheduler), laid out like the
// Mac app's SchedulerView:
//
//   ┌ dispatcher ───┐┌ Agents | History ──────┐┌ editor / run detail ─┐
//   │ (dispatch.js) ││ slot rows · queue &     ││ permanent slot editor │
//   ├ agenda ───────┤│ active · history        ││ or the picked run     │
//   │ next 10 days  ││                         ││                       │
//   └───────────────┘└─────────────────────────┘└───────────────────────┘
//
// Each column is its own renderer over its own host node. The 5s poll always
// re-renders the left and middle columns; the right column is rebuilt only
// when the selection changes (or the picked run's record moved), so typing in
// the editor is never clobbered. Uses app.js globals: call(), el(), note(),
// ago(), escapeHtml(); slot_editor.js and run_detail.js build the right pane.

let SCHED = null;                       // latest scheduler_view payload
let SCHED_SEL = { kind: "new", id: null };  // what the right column shows
let SCHED_TAB = "agents";               // agents | history
let SCHED_SHOW_HIDDEN = false;
let _schedTimer = null;
let _schedRightKey = null;              // what the right column currently renders

function _schedVisible() { return currentPanel() === "scheduler"; }

async function startScheduler(arg) {
  _schedApplyArg(arg);
  renderSchedulerPanel();
  await schedRefresh();
  if (_schedTimer) clearInterval(_schedTimer);
  _schedTimer = setInterval(schedRefresh, 5000);
}
function stopScheduler() {
  if (_schedTimer) { clearInterval(_schedTimer); _schedTimer = null; }
}
// #/scheduler/run/<id> picks that run (and the History tab);
// #/scheduler/slot/<id> opens that agent in the editor.
function _schedApplyArg(arg) {
  if (!arg || !arg.id) return;
  if (arg.kind === "slot") { SCHED_SEL = { kind: "slot", id: arg.id }; return; }
  SCHED_SEL = { kind: "run", id: arg.id }; SCHED_TAB = "history";
}
function _schedArg(arg) { _schedApplyArg(arg); renderSchedulerPanel(); }
// Esc backs out of the run detail / an edit to the blank editor.
function _schedEscape() {
  if (SCHED_SEL.kind !== "new") schedSelect({ kind: "new", id: null });
}
registerPanel("scheduler", { start: startScheduler, stop: stopScheduler,
                             arg: _schedArg, escape: _schedEscape });

async function schedRefresh() {
  try {
    const v = await call("scheduler_view");
    if (v && !v.error) {
      SCHED = v;
      if (typeof renderStrip === "function") renderStrip();
      if (typeof dispatchPoll === "function" && _schedVisible()) dispatchPoll();
      renderSchedulerPanel();
    } else if (v && v.error && !SCHED) {
      SCHED = v;
      renderSchedulerPanel();
    }
  } catch (_) { /* transient poll errors are fine */ }
}

// A scheduler_view /api call that re-renders; shows err inline on failure.
async function schedCall(method, args) {
  let v;
  try { v = await call(method, args); } catch (e) { v = { error: String(e) }; }
  if (v && !v.error) {
    SCHED = v;
    if (typeof renderStrip === "function") renderStrip();
    renderSchedulerPanel();
    return true;
  }
  schedStatus((v && v.error) || "call failed", true);
  return false;
}
function schedStatus(text, isErr) {
  const s = document.getElementById("sched-status");
  if (!s) return;
  s.textContent = text || "";
  s.classList.toggle("sched-err", !!isErr);
}

// Select what the right column shows; keeps the URL shareable for runs.
function schedSelect(sel) {
  SCHED_SEL = sel;
  if (sel.kind === "run") {
    SCHED_TAB = "history";
    history.replaceState(null, "", "#/scheduler/run/" + encodeURIComponent(sel.id));
  } else if (sel.kind === "slot") {
    history.replaceState(null, "", "#/scheduler/slot/" + encodeURIComponent(sel.id));
  } else if (location.hash.startsWith("#/scheduler/")) {
    history.replaceState(null, "", "#/scheduler");
  }
  renderSchedulerPanel();
}

// ---- small formatters -------------------------------------------------------
function schedWhen(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  const mins = Math.round((d - Date.now()) / 60000);
  const rel = mins < 1 ? "now" : mins < 60 ? `in ${mins}m`
    : mins < 48 * 60 ? `in ${Math.round(mins / 60)}h` : `in ${Math.round(mins / 1440)}d`;
  const day = d.toLocaleDateString(undefined, { weekday: "short" });
  const time = d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  return `${rel} · ${day} ${time}`;
}
function schedDuration(a, b) {
  const s = Math.max(0, (new Date(b) - new Date(a)) / 1000);
  if (isNaN(s)) return "";
  if (s < 90) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
  return `${Math.floor(s / 3600)}h ${Math.round((s % 3600) / 60)}m`;
}
function schedClock(d) {
  return d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}
function schedDateTime(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return iso || "";
  return d.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" })
    + ", " + schedClock(d);
}
function tildify(p) {
  return p ? p.replace(/^\/(?:home|Users)\/[^/]+/, "~") : "";
}
function baseName(p) {
  return (p || "").replace(/\/+$/, "").split("/").pop() || p || "";
}

// Outcome → glyph + label (RunHistoryRow.swift).
// Plain glyphs (not emoji) so they render with the UI font everywhere.
const SCHED_OUTCOMES = {
  completed: ["✓", "completed", "ok"],
  cancelled: ["■", "cancelled", "muted"],
  skipped_missed_window: ["↷", "missed its window", "muted"],
  timeout: ["◔", "timed out", "bad"],
  launch_failed: ["✕", "failed to launch", "bad"],
  exited: ["✕", "exited without finishing", "bad"],
};
function outcomeInfo(run) {
  return SCHED_OUTCOMES[run.outcome] || ["•", (run.outcome || "unknown").replace(/_/g, " "), "muted"];
}
// Live state → label + tone (blocked = waiting for the CTO).
function runStateInfo(run) {
  if (run.state === "queued") return ["queued", "muted"];
  if (run.state === "launching") return ["starting", "live"];
  if (run.state === "exiting") return ["finishing", "live"];
  if (run.taken_over) return ["you have control", "warn"];
  const h = run.herdr_state;
  if (run.waiting_since || h === "blocked") return ["waiting for input", "warn"];
  if (h === "working") return ["working", "live"];
  if (h === "idle") return ["idle", "muted"];
  if (h === "done") return ["done", "ok"];
  if (run.stalled_at) return ["stalled", "warn"];
  return ["running", "live"];
}
function isRunFailure(run) {
  return ["timeout", "launch_failed", "exited"].includes(run.outcome);
}

// ---- panel shell ------------------------------------------------------------
function _schedShell() {
  const host = document.getElementById("panel-scheduler");
  if (!host) return null;
  if (host.querySelector(".sched-cols")) return host;
  host.textContent = "";
  host.innerHTML =
    '<div class="sched-wrap">' +
      '<div id="sched-notice"></div>' +
      '<div class="sched-cols">' +
        '<div class="sched-left"><div id="sched-dispatch" class="sched-dispatch"></div>' +
          '<div id="sched-agenda" class="sched-agenda"></div></div>' +
        '<div class="sched-mid"><div id="sched-mid-head" class="sched-mid-head"></div>' +
          '<div id="sched-mid-body" class="sched-mid-body"></div></div>' +
        '<div id="sched-right" class="sched-right"></div>' +
      '</div>' +
    '</div>';
  _schedRightKey = null;
  return host;
}

function renderSchedulerPanel() {
  const host = _schedShell();
  if (!host || !_schedVisible()) return;
  renderSchedNotice();
  if (typeof renderDispatchPane === "function") renderDispatchPane();
  else document.getElementById("sched-dispatch").hidden = true;
  renderAgenda();
  renderMidHead();
  renderMidBody();
  renderRight();
}

function renderSchedNotice() {
  const out = document.getElementById("sched-notice");
  out.textContent = "";
  if (!SCHED) return;
  if (SCHED.error) {
    out.appendChild(el("p", "sched-banner", "Scheduler unavailable: " + SCHED.error));
    return;
  }
  const h = SCHED.herdr;
  if (h && (!h.installed || !h.reachable)) {
    const warn = el("p", "sched-banner");
    warn.textContent = !h.installed
      ? "Herdr is required for scheduled runs — install it from herdr.dev. Slots can be set up now; runs start once it's there."
      : `Herdr is installed but its server isn't reachable (${h.socket}). It will be started on the next run.`;
    out.appendChild(warn);
  }
}

// ---- left: agenda (SchedulerCalendarView) ----------------------------------
function renderAgenda() {
  const out = document.getElementById("sched-agenda");
  out.textContent = "";
  const days = (SCHED && SCHED.agenda_days) || 10;
  const head = el("div", "sec-head");
  head.appendChild(el("span", "sec-label", `NEXT ${days} DAYS`));
  out.appendChild(head);
  if (!SCHED || SCHED.error) return;

  // Bucket every enabled slot's upcoming fires by local calendar day.
  const entries = [];
  for (const slot of SCHED.slots || []) {
    if (!slot.enabled) continue;
    for (const iso of slot.upcoming || []) {
      const d = new Date(iso);
      if (!isNaN(d)) entries.push({ at: d, slot });
    }
  }
  entries.sort((a, b) => a.at - b.at || a.slot.name.localeCompare(b.slot.name));
  const byDay = new Map();
  for (const e of entries) {
    const key = e.at.toDateString();
    if (!byDay.has(key)) byDay.set(key, []);
    byDay.get(key).push(e);
  }
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const list = el("div", "agenda-list");
  let anything = false;
  for (let i = 0; i < days; i++) {
    const day = new Date(today); day.setDate(today.getDate() + i);
    const rows = byDay.get(day.toDateString()) || [];
    const dayEl = el("div", "agenda-day");
    const dh = el("div", "agenda-day-head");
    const prefix = i === 0 ? "TODAY" : i === 1 ? "TOMORROW" : "";
    const label = day.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" });
    dh.appendChild(el("span", "agenda-day-name", (prefix ? prefix + " · " : "") + label));
    if (rows.length) dh.appendChild(el("span", "count-chip", String(rows.length)));
    dayEl.appendChild(dh);
    if (!rows.length) {
      dayEl.appendChild(el("div", "agenda-quiet", "nothing scheduled"));
    }
    for (const r of rows) {
      anything = true;
      const row = el("div", "agenda-row");
      if (SCHED_SEL.kind === "slot" && SCHED_SEL.id === r.slot.id) row.classList.add("row-sel");
      row.appendChild(el("span", "agenda-time", schedClock(r.at)));
      row.appendChild(el("span", "agenda-glyph",
        (r.slot.schedule || {}).kind === "once" ? "①" : "⟳"));
      row.appendChild(el("span", "agenda-name", r.slot.name));
      row.title = `${r.slot.name} · ${r.slot.summary || ""}\n${tildify(r.slot.cwd)}`;
      row.addEventListener("click", () => schedSelect({ kind: "slot", id: r.slot.id }));
      dayEl.appendChild(row);
    }
    list.appendChild(dayEl);
  }
  out.appendChild(list);
  if (!anything && !(SCHED.slots || []).some((s) => s.enabled)) {
    out.appendChild(note("No enabled agents. Turn one on and its runs show up here."));
  }
}

// ---- middle: header + tabs ---------------------------------------------------
function renderMidHead() {
  const out = document.getElementById("sched-mid-head");
  out.textContent = "";
  const top = el("div", "sched-mid-top");
  top.appendChild(el("h2", null, "Scheduler"));
  const cap = SCHED && !SCHED.error ? SCHED.concurrency : null;
  if (cap) {
    const c = el("span", "subtle sched-cap", `${cap} concurrent`);
    c.title = "Concurrent runs — change in Settings ▸ Misc";
    top.appendChild(c);
  }
  const add = el("button", "btn small", "New agent");
  add.addEventListener("click", () => schedSelect({ kind: "new", id: null }));
  top.appendChild(add);
  out.appendChild(top);

  const tabs = el("div", "sched-tabs");
  const live = SCHED && !SCHED.error ? (SCHED.active || []).length + (SCHED.queued || []).length : 0;
  for (const [key, label] of [["agents", "Agents"], ["history", "History"]]) {
    const b = el("button", "sched-tab" + (SCHED_TAB === key ? " on" : ""), label);
    if (key === "history" && live) b.appendChild(el("span", "tab-n", String(live)));
    b.addEventListener("click", () => { SCHED_TAB = key; renderMidBody(); renderMidHead(); });
    tabs.appendChild(b);
  }
  out.appendChild(tabs);
}

function renderMidBody() {
  const out = document.getElementById("sched-mid-body");
  out.textContent = "";
  if (!SCHED) { out.appendChild(note("Loading…")); return; }
  if (SCHED.error) return;
  if (SCHED_TAB === "agents") renderAgents(out);
  else { renderQueue(out); renderHistory(out); }
  const status = el("p", "subtle sched-status"); status.id = "sched-status";
  out.appendChild(status);
}

// ---- middle: agents (slot rows) ---------------------------------------------
function renderAgents(out) {
  const slots = SCHED.slots || [];
  if (!slots.length) {
    out.appendChild(note("No agents yet. Set one up on the right — a folder, a prompt, a schedule."));
    return;
  }
  const list = el("div", "slot-list");
  for (const slot of slots) {
    const row = el("div", "slot-row");
    if (SCHED_SEL.kind === "slot" && SCHED_SEL.id === slot.id) row.classList.add("row-sel");
    const main = el("div", "slot-main");
    const top = el("div", "slot-top");
    top.appendChild(el("span", "slot-name", slot.name));
    if (slot.workspace_id) top.appendChild(el("span", "plug-ws-tag", "workspace"));
    if ((slot.schedule || {}).kind === "once" && !slot.enabled && slot.last_fired_at) {
      top.appendChild(el("span", "plug-ws-tag", "ran"));
    }
    main.appendChild(top);
    const cmd = (slot.prompt || "").replace(/\s+/g, " ");
    const sub = el("div", "slot-sub mono",
      (cmd.length > 70 ? cmd.slice(0, 70) + "…" : cmd) + "  ·  " + (slot.summary || ""));
    main.appendChild(sub);
    const meta = el("div", "slot-meta subtle",
      tildify(slot.cwd) + (slot.enabled && slot.next_fire
        ? `  ·  next ${schedWhen(slot.next_fire)}` : slot.enabled ? "" : "  ·  off"));
    main.appendChild(meta);
    main.addEventListener("click", () => schedSelect({ kind: "slot", id: slot.id }));
    row.appendChild(main);

    const side = el("div", "slot-side");
    const sw = el("label", "switch");
    sw.title = slot.enabled ? "Enabled — click to turn off" : "Off — click to enable";
    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.checked = !!slot.enabled;
    cb.addEventListener("change", async () => {
      const ok = await schedCall("toggle_slot", { slot_id: slot.id, enabled: cb.checked });
      if (!ok) cb.checked = !cb.checked;
      else slotEditorSync(slot.id);
    });
    sw.appendChild(cb); sw.appendChild(el("span", "switch-track"));
    side.appendChild(sw);
    const acts = el("div", "slot-acts");
    const run = el("button", "btn ghost small", "Run now");
    run.title = "Queue a run right now";
    run.addEventListener("click", () => schedCall("run_slot_now", { slot_id: slot.id }));
    const del = el("button", "btn ghost small danger", "✕");
    del.title = "Delete agent";
    del.addEventListener("click", async () => {
      if (!confirm(`Delete “${slot.name}”?`)) return;
      if (await schedCall("delete_slot", { slot_id: slot.id })
          && SCHED_SEL.kind === "slot" && SCHED_SEL.id === slot.id) {
        schedSelect({ kind: "new", id: null });
      }
    });
    acts.appendChild(run); acts.appendChild(del);
    side.appendChild(acts);
    row.appendChild(side);
    list.appendChild(row);
  }
  out.appendChild(list);
}

// ---- middle: queue & active ---------------------------------------------------
function renderQueue(out) {
  const live = [...(SCHED.active || []), ...(SCHED.queued || [])];
  const head = el("div", "sec-head");
  head.appendChild(el("span", "sec-label", "QUEUE & ACTIVE"));
  if (live.length) head.appendChild(el("span", "count-chip", String(live.length)));
  out.appendChild(head);
  if (!live.length) {
    out.appendChild(el("p", "subtle sched-empty", "Nothing running."));
    return;
  }
  const list = el("div", "run-list");
  for (const r of live) {
    const row = el("div", "run-row live");
    if (SCHED_SEL.kind === "run" && SCHED_SEL.id === r.id) row.classList.add("row-sel");
    const [label, tone] = runStateInfo(r);
    const main = el("div", "run-main");
    const top = el("div", "run-top");
    top.appendChild(el("span", r.state === "queued" ? "run-glyph" : "run-glyph spin", r.state === "queued" ? "⧗" : ""));
    top.appendChild(el("span", "run-name", r.slot_name));
    top.appendChild(el("span", `run-capsule ${tone}`, label));
    main.appendChild(top);
    let sub;
    if (r.state === "queued") {
      const ahead = (SCHED.active || []);
      sub = ahead.length ? `queued behind ${ahead[0].slot_name}` : "queued — starting shortly";
    } else {
      sub = `started ${schedDuration(r.started_at, Date.now())} ago · ${tildify(r.cwd)}`;
    }
    main.appendChild(el("div", "run-sub subtle", sub));
    main.addEventListener("click", () => schedSelect({ kind: "run", id: r.id }));
    row.appendChild(main);
    const acts = el("div", "run-acts");
    if (r.state !== "queued") {
      const over = el("button", "btn ghost small", "Take over");
      over.title = "Take over in Herdr — Overboard stops managing this run";
      over.addEventListener("click", () => schedCall("take_over_run", { run_id: r.id }));
      acts.appendChild(over);
    }
    const stop = el("button", "btn ghost small", "Stop");
    stop.addEventListener("click", () => schedCall("stop_run", { run_id: r.id }));
    acts.appendChild(stop);
    row.appendChild(acts);
    list.appendChild(row);
  }
  out.appendChild(list);
}

// ---- middle: history -----------------------------------------------------------
function renderHistory(out) {
  const all = SCHED.history || [];
  const hidden = all.filter((r) => r.hidden);
  const shown = SCHED_SHOW_HIDDEN ? all : all.filter((r) => !r.hidden);
  const failed = all.filter((r) => !r.hidden && isRunFailure(r)).length;

  const head = el("div", "sec-head");
  head.appendChild(el("span", "sec-label", "HISTORY"));
  if (all.length) head.appendChild(el("span", "count-chip", String(all.length)));
  if (failed) head.appendChild(el("span", "sec-failed", `${failed} failed`));
  const spacer = el("span", "sec-spacer");
  head.appendChild(spacer);
  if (hidden.length) {
    const tog = el("button", "btn ghost small", SCHED_SHOW_HIDDEN
      ? `hide ${hidden.length}` : `${hidden.length} hidden`);
    tog.addEventListener("click", () => { SCHED_SHOW_HIDDEN = !SCHED_SHOW_HIDDEN; renderMidBody(); });
    head.appendChild(tog);
  }
  if (all.length) {
    const clear = el("button", "btn ghost small danger", "Clear");
    clear.title = "Clear run history";
    clear.addEventListener("click", async () => {
      if (!confirm("Clear run history? Transcripts are deleted too.")) return;
      if (await schedCall("clear_history") && SCHED_SEL.kind === "run") {
        schedSelect({ kind: "new", id: null });
      }
    });
    head.appendChild(clear);
  }
  out.appendChild(head);

  if (!shown.length) {
    out.appendChild(el("p", "subtle sched-empty", all.length ? "All hidden." : "No runs yet."));
    return;
  }
  const list = el("div", "run-list");
  for (const r of shown) {
    const row = el("div", "run-row" + (r.hidden ? " dim" : ""));
    if (SCHED_SEL.kind === "run" && SCHED_SEL.id === r.id) row.classList.add("row-sel");
    const [glyph, label, tone] = outcomeInfo(r);
    const main = el("div", "run-main");
    const top = el("div", "run-top");
    top.appendChild(el("span", "run-glyph " + tone, glyph));
    top.appendChild(el("span", "run-name", r.slot_name));
    main.appendChild(top);
    const bits = [label];
    if (r.ended_at && r.started_at && r.outcome !== "skipped_missed_window") bits.push(schedDuration(r.started_at, r.ended_at));
    bits.push(ago(new Date(r.ended_at || r.started_at).getTime() / 1000));
    main.appendChild(el("div", "run-sub subtle", bits.join(" · ")));
    const msg = (r.completion_message || r.reason || "").replace(/\s+/g, " ");
    if (msg) main.appendChild(el("div", "run-preview", msg.length > 110 ? msg.slice(0, 110) + "…" : msg));
    main.addEventListener("click", () => schedSelect({ kind: "run", id: r.id }));
    row.appendChild(main);
    const acts = el("div", "run-acts hover");
    const hide = el("button", "btn ghost small", r.hidden ? "unhide" : "✕");
    hide.title = r.hidden ? "Show again" : "Hide from history";
    hide.addEventListener("click", () => schedCall("hide_run", { run_id: r.id, hidden: !r.hidden }));
    acts.appendChild(hide);
    row.appendChild(acts);
    list.appendChild(row);
  }
  out.appendChild(list);
}

// ---- right column ------------------------------------------------------------
function _schedFindRun(id) {
  if (!SCHED || SCHED.error) return null;
  for (const bucket of [SCHED.active || [], SCHED.queued || [], SCHED.history || []]) {
    const hit = bucket.find((r) => r.id === id);
    if (hit) return hit;
  }
  return null;
}
function _schedRunSig(r) {
  return [r.state, r.herdr_state, r.outcome, r.ended_at, r.completion_message,
          r.waiting_since, r.stalled_at, r.taken_over, r.hidden].join("|");
}

function renderRight() {
  const out = document.getElementById("sched-right");
  if (!SCHED || SCHED.error) {
    if (_schedRightKey !== "none") { out.textContent = ""; _schedRightKey = "none"; }
    return;
  }
  let key, build;
  if (SCHED_SEL.kind === "run") {
    const run = _schedFindRun(SCHED_SEL.id);
    if (!run) {
      // The run vanished (history cleared / server restarted) — fall back.
      SCHED_SEL = { kind: "new", id: null };
      key = "new"; build = () => slotEditorPane(null);
    } else {
      key = "run:" + run.id + ":" + _schedRunSig(run);
      build = () => runDetailPane(run);
    }
  } else if (SCHED_SEL.kind === "slot") {
    const slot = (SCHED.slots || []).find((s) => s.id === SCHED_SEL.id);
    if (!slot) { SCHED_SEL = { kind: "new", id: null }; key = "new"; build = () => slotEditorPane(null); }
    else { key = "slot:" + slot.id; build = () => slotEditorPane(slot); }
  } else {
    key = "new"; build = () => slotEditorPane(null);
  }
  if (key === _schedRightKey) return;   // never clobber a pane that's in use
  _schedRightKey = key;
  out.textContent = "";
  out.appendChild(build());
}
