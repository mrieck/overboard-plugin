"use strict";

// ---- dispatcher pane (top-left of the Scheduler panel) ---------------------
// A port of the Mac app's DispatcherPane, browser-only: the CTO types what
// should get done, a short-lived /overboard:dispatch session works out the
// project, time and plugins, the scheduler runs it, and the result lands in
// the RECENT DISPATCHES feed. The phone path (Telegram, Slack next) is
// Overboard for Mac's — pitched here, not built here. Polled with the
// scheduler panel (dispatchPoll from schedRefresh). Uses promptField().

let DISP = null;              // latest dispatch_view payload
let DISP_OPEN = true;         // feed disclosure
let _dispField = null;        // the promptField instance (kept across renders)
let _dispBusy = false;
let _dispFeedKey = null;

const PRO_URL = "https://getoverboard.app";

async function dispatchPoll() {
  try {
    const v = await call("dispatch_view");
    if (v && !v.error) { DISP = v; renderDispatchPane(); }
  } catch (_) { /* transient */ }
}

// Called by renderSchedulerPanel() on every poll; the composer is built once
// and kept (typing survives), the feed re-renders when its data changed.
function renderDispatchPane() {
  const host = document.getElementById("sched-dispatch");
  if (!host) return;
  host.hidden = false;
  if (!host.querySelector(".disp-compose")) _buildComposer(host);
  _syncHint(host);
  _renderFeed(host);
}

function _buildComposer(host) {
  host.textContent = "";
  const head = el("div", "disp-head");
  head.appendChild(el("span", "sec-label", "DISPATCHER"));
  const tip = el("span", "info-tip", "?");
  tip.title = "Describe a task in plain words. A short-lived Claude session picks the project, "
    + "the time and the plugins — you never fill those in — and the result lands in the feed below.";
  head.appendChild(tip);
  host.appendChild(head);

  const compose = el("div", "disp-compose");
  _dispField = promptField({
    value: "",
    rows: 3,
    cwd: () => (DISP && DISP.run_directory) || null,
    placeholder: "what should get done? (project, time and plugins are worked out for you)…",
    onSubmit: () => _dispatchNow(),
  });
  compose.appendChild(_dispField.root);
  const row = el("div", "disp-row");
  const hint = el("span", "disp-hint subtle"); hint.dataset.role = "hint";
  row.appendChild(hint);
  const btn = el("button", "btn small", "Dispatch"); btn.dataset.role = "go";
  btn.title = "⌘/Ctrl+Enter";
  btn.addEventListener("click", _dispatchNow);
  row.appendChild(btn);
  compose.appendChild(row);
  host.appendChild(compose);

  const feed = el("div", "disp-feed"); feed.dataset.role = "feed";
  host.appendChild(feed);
}

function _syncHint(host) {
  const hint = host.querySelector("[data-role=hint]");
  const btn = host.querySelector("[data-role=go]");
  if (!hint || !btn) return;
  hint.textContent = "";
  hint.className = "disp-hint";
  if (!DISP) { hint.classList.add("subtle"); hint.textContent = "…"; btn.disabled = true; return; }
  if (!DISP.run_directory_set) {
    hint.classList.add("amber");
    hint.appendChild(document.createTextNode("Set the Overboard run directory first — "));
    const a = el("a", "link", "open Settings ▸ Misc");
    a.href = "#";
    a.addEventListener("click", (e) => { e.preventDefault(); openSettings("misc"); });
    hint.appendChild(a);
    btn.disabled = true;
    return;
  }
  btn.disabled = _dispBusy;
  hint.classList.add("subtle");
  hint.appendChild(document.createTextNode("From your phone instead? "));
  const a = el("a", "link", "Overboard for Mac");
  a.href = PRO_URL; a.target = "_blank"; a.rel = "noopener";
  hint.appendChild(a);
  hint.appendChild(document.createTextNode(" dispatches over Telegram (Slack next)."));
}

async function _dispatchNow() {
  if (_dispBusy || !_dispField) return;
  const text = _dispField.getValue().trim();
  if (!text) { _dispField.focus(); return; }
  _dispBusy = true;
  const host = document.getElementById("sched-dispatch");
  const btn = host && host.querySelector("[data-role=go]");
  if (btn) { btn.disabled = true; btn.textContent = "Dispatching…"; }
  let v;
  try { v = await call("begin_dispatch", { text }); } catch (e) { v = { error: String(e) }; }
  _dispBusy = false;
  if (btn) { btn.disabled = false; btn.textContent = "Dispatch"; }
  if (v && !v.error) {
    DISP = v;
    _dispField.setValue("");
    DISP_OPEN = true;
    _dispFeedKey = null;
    renderDispatchPane();
    // The dispatcher session shows up in the queue right away.
    if (typeof schedRefresh === "function") schedRefresh();
  } else if (host) {
    const hint = host.querySelector("[data-role=hint]");
    if (hint) { hint.className = "disp-hint sched-err"; hint.textContent = (v && v.error) || "dispatch failed"; }
  }
}

function _dispStatusTone(r) {
  const s = r.status;
  if (s === "completed" || s === "answered") return "ok";
  if (s === "failed" || s === "rejected") return "bad";
  if (s === "cancelled" || s === "forwarded") return "muted";
  return "live";  // received / dispatching / task_created / continuing
}

function _renderFeed(host) {
  const feed = host.querySelector("[data-role=feed]");
  if (!feed) return;
  const records = (DISP && DISP.records) || [];
  const key = DISP_OPEN + "|" + records.map((r) => r.id + ":" + r.status + ":" + r.state_label + ":" + (r.summary || "").length).join(",");
  if (key === _dispFeedKey) return;
  _dispFeedKey = key;
  feed.textContent = "";
  if (!records.length) return;

  const head = el("div", "sec-head disp-feed-head");
  const tog = el("button", "sec-toggle", (DISP_OPEN ? "▾ " : "▸ ") + "RECENT DISPATCHES");
  tog.addEventListener("click", () => { DISP_OPEN = !DISP_OPEN; _dispFeedKey = null; _renderFeed(host); });
  head.appendChild(tog);
  head.appendChild(el("span", "count-chip", String(DISP.total || records.length)));
  if (!DISP_OPEN) head.appendChild(el("span", "subtle disp-latest", `latest: ${records[0].state_label}`));
  feed.appendChild(head);
  if (!DISP_OPEN) return;

  const list = el("div", "disp-list");
  for (const r of records) {
    const row = el("div", "disp-row-item");
    const top = el("div", "disp-top");
    top.appendChild(el("span", "disp-msg", r.task_name || r.message_text));
    top.appendChild(el("span", `run-capsule ${_dispStatusTone(r)}`, r.state_label));
    row.appendChild(top);
    const bits = [`#${r.id}`];
    if (r.project_name) bits.push(r.project_name);
    if (r.workspace_plugins && r.workspace_plugins.length) {
      bits.push("with " + r.workspace_plugins.map((p) => p.split("@")[0]).join(", "));
    }
    bits.push(ago(new Date(r.received_at).getTime() / 1000));
    row.appendChild(el("div", "disp-meta subtle", bits.join(" · ")));
    const detail = r.error || r.reply_text || r.summary || "";
    if (detail) {
      const d = el("div", "disp-detail" + (r.status === "failed" || r.status === "rejected" ? " sched-err" : ""), detail);
      d.title = detail;
      row.appendChild(d);
    }
    if (r.task_name && r.task_name !== r.message_text) row.title = r.message_text;
    row.addEventListener("click", () => _openDispatchTarget(r));
    list.appendChild(row);
  }
  feed.appendChild(list);
}

// Click → the thing that explains the row: its live/last task run, the once
// slot that will run it, the forwarded task, or the dispatcher's own run.
function _openDispatchTarget(r) {
  if (r.forwarded_task_id) {
    const t = ((DISP && DISP.records) || []).find((x) => x.id === r.forwarded_task_id);
    if (t) { _openDispatchTarget(t); return; }
  }
  const runs = r.task_run_ids || [];
  if (runs.length) { schedSelect({ kind: "run", id: runs[runs.length - 1] }); return; }
  if (r.task_slot_id) { schedSelect({ kind: "slot", id: r.task_slot_id }); return; }
  if (r.dispatcher_run_id) schedSelect({ kind: "run", id: r.dispatcher_run_id });
}
