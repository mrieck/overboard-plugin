"use strict";

// ---- scheduler panel (slots that fire unattended claude runs via herdr) -----
// Opened from the ⏱ header button. Uses app.js globals: call(), el(), ago().
// List mode re-renders on a 5s poll; while the slot form is open the poll
// keeps fetching but never re-renders, so typing is never clobbered.

let SCHED = null;        // latest scheduler_view payload
let SCHED_EDIT = null;   // slot object being edited, {} for a new one, null = list mode
let _schedTimer = null;
let _schedPreviewTimer = null;

async function openScheduler() {
  SCHED_EDIT = null;
  SCHED = await call("scheduler_view");
  renderScheduler();
  _schedTimer = setInterval(async () => {
    try {
      const v = await call("scheduler_view");
      if (v && !v.error) {
        SCHED = v;
        if (!SCHED_EDIT) renderScheduler();
      }
    } catch (_) { /* transient poll errors are fine */ }
  }, 5000);
}

function closeScheduler() {
  const m = document.getElementById("scheduler-modal");
  if (m) m.remove();
  document.removeEventListener("keydown", _schedEsc);
  if (_schedTimer) { clearInterval(_schedTimer); _schedTimer = null; }
  SCHED_EDIT = null;
}
function _schedEsc(e) {
  if (e.key !== "Escape") return;
  if (SCHED_EDIT) { SCHED_EDIT = null; renderScheduler(); }
  else closeScheduler();
}

// A scheduler_view /api call that re-renders; shows err inline on failure.
async function schedCall(method, args) {
  const v = await call(method, args);
  if (v && !v.error) { SCHED = v; renderScheduler(); return true; }
  const status = document.getElementById("sched-status");
  if (status) status.textContent = (v && v.error) || "call failed";
  return false;
}

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
  if (s < 5400) return `${Math.round(s / 60)}m`;
  return `${(s / 3600).toFixed(1)}h`;
}
function tildify(p) {
  return (SCHED && p) ? p.replace(/^\/(?:home|Users)\/[^/]+/, "~") : p || "";
}

function renderScheduler() {
  closeSchedulerDom();
  const ov = el("div", "modal-overlay");
  ov.id = "scheduler-modal";
  ov.addEventListener("click", (e) => { if (e.target === ov) closeScheduler(); });
  const box = el("div", "modal modal-scheduler");

  const head = el("div", "modal-head");
  head.appendChild(el("h3", null, SCHED_EDIT
    ? (SCHED_EDIT.id ? "Edit slot" : "New slot") : "Scheduler"));
  const close = el("button", "btn ghost small", "Close");
  close.addEventListener("click", closeScheduler);
  head.appendChild(close);
  box.appendChild(head);

  const body = el("div", "settings-body");
  if (SCHED && SCHED.error) {
    body.appendChild(note("Scheduler unavailable: " + SCHED.error));
  } else if (SCHED_EDIT) {
    body.appendChild(schedForm());
  } else {
    schedList(body);
  }
  box.appendChild(body);
  ov.appendChild(box);
  document.body.appendChild(ov);
  document.addEventListener("keydown", _schedEsc);
}
function closeSchedulerDom() {
  const m = document.getElementById("scheduler-modal");
  if (m) m.remove();
  document.removeEventListener("keydown", _schedEsc);
}

// ---- list mode --------------------------------------------------------------
function schedList(body) {
  const h = SCHED && SCHED.herdr;
  if (h && (!h.installed || !h.reachable)) {
    const warn = el("p", "sched-banner");
    warn.textContent = !h.installed
      ? "Herdr is required for scheduled runs — install it from herdr.dev."
      : `Herdr is installed but its server isn't reachable (${h.socket}). It will be started on the next run.`;
    body.appendChild(warn);
  }

  const slots = (SCHED && SCHED.slots) || [];
  const fs = el("fieldset", "src");
  const legend = el("legend", null, "Slots");
  fs.appendChild(legend);
  if (!slots.length) {
    fs.appendChild(note("No slots yet — schedule a recurring claude run below."));
  }
  for (const slot of slots) {
    const row = el("div", "sched-slot");
    const top = el("div", "sched-slot-top");
    const enable = document.createElement("input");
    enable.type = "checkbox";
    enable.checked = !!slot.enabled;
    enable.title = "Enable / disable";
    enable.addEventListener("change", () =>
      schedCall("toggle_slot", { slot_id: slot.id, enabled: enable.checked }));
    top.appendChild(enable);
    top.appendChild(el("span", "sched-slot-name", slot.name));
    if (slot.workspace_id) top.appendChild(el("span", "plug-ws-tag", "↳ workspace"));
    top.appendChild(el("span", "subtle", slot.summary || ""));
    const btns = el("span", "sched-slot-btns");
    const run = el("button", "btn ghost small", "Run now");
    run.addEventListener("click", () => schedCall("run_slot_now", { slot_id: slot.id }));
    const edit = el("button", "btn ghost small", "Edit");
    edit.addEventListener("click", () => { SCHED_EDIT = { ...slot }; renderScheduler(); });
    const del = el("button", "btn ghost small", "✕");
    del.title = "Delete slot";
    del.addEventListener("click", () => {
      if (confirm(`Delete slot “${slot.name}”?`)) schedCall("delete_slot", { slot_id: slot.id });
    });
    btns.appendChild(run); btns.appendChild(edit); btns.appendChild(del);
    top.appendChild(btns);
    row.appendChild(top);
    const sub = el("div", "subtle sched-slot-sub",
      tildify(slot.cwd) + (slot.enabled && slot.next_fire
        ? `  ·  next ${schedWhen(slot.next_fire)}` : slot.enabled ? "" : "  ·  disabled"));
    row.appendChild(sub);
    fs.appendChild(row);
  }
  const add = el("button", "btn small", "+ Add slot");
  add.addEventListener("click", () => { SCHED_EDIT = {}; renderScheduler(); });
  const addWrap = el("p", null); addWrap.appendChild(add);
  fs.appendChild(addWrap);
  body.appendChild(fs);

  const running = [...((SCHED && SCHED.active) || []), ...((SCHED && SCHED.queued) || [])];
  if (running.length) {
    const afs = el("fieldset", "src");
    afs.appendChild(el("legend", null, "Running"));
    for (const r of running) {
      const row = el("div", "sched-run");
      const state = r.state === "running" ? (r.herdr_state || "running") : r.state;
      row.appendChild(el("span", `sched-chip sched-${state}`, state));
      row.appendChild(el("span", "sched-slot-name", r.slot_name));
      row.appendChild(el("span", "subtle", "started " + schedDuration(r.started_at, Date.now()) + " ago"));
      if (state === "blocked") {
        row.appendChild(el("span", "subtle", "waiting for input — take it over in herdr"));
      }
      const stop = el("button", "btn ghost small", "Stop");
      stop.addEventListener("click", () => schedCall("stop_run", { run_id: r.id }));
      row.appendChild(stop);
      afs.appendChild(row);
    }
    body.appendChild(afs);
  }

  const history = (SCHED && SCHED.history) || [];
  if (history.length) {
    const hfs = el("fieldset", "src");
    hfs.appendChild(el("legend", null, "History"));
    for (const r of history.slice(0, 25)) {
      const row = el("div", "sched-run");
      row.appendChild(el("span", `sched-chip sched-${r.outcome}`, (r.outcome || "?").replace(/_/g, " ")));
      row.appendChild(el("span", "sched-slot-name", r.slot_name));
      const bits = [schedWhen(r.started_at).replace(/^in .* · /, ""),
                    r.ended_at ? schedDuration(r.started_at, r.ended_at) : ""]
        .filter(Boolean).join(" · ");
      row.appendChild(el("span", "subtle", bits));
      if (r.reason) row.appendChild(el("span", "subtle", r.reason));
      if (r.has_transcript) {
        const t = el("button", "btn ghost small", "Transcript");
        t.addEventListener("click", () => openSchedTranscript(r));
        row.appendChild(t);
      }
      hfs.appendChild(row);
    }
    body.appendChild(hfs);
  }
  body.appendChild(el("p", "subtle hint", "Slots fire only while the dashboard server is running."));
  const status = el("p", "subtle"); status.id = "sched-status";
  body.appendChild(status);
}

async function openSchedTranscript(run) {
  const res = await call("get_run_transcript", { run_id: run.id });
  const ov = el("div", "modal-overlay");
  ov.id = "sched-transcript-modal";
  ov.addEventListener("click", (e) => { if (e.target === ov) ov.remove(); });
  const box = el("div", "modal modal-scheduler");
  const head = el("div", "modal-head");
  head.appendChild(el("h3", null, `Transcript · ${run.slot_name}`));
  const close = el("button", "btn ghost small", "Close");
  close.addEventListener("click", () => ov.remove());
  head.appendChild(close);
  box.appendChild(head);
  const pre = el("pre", "sched-transcript");
  pre.textContent = (res && res.text) || "Transcript unavailable.";
  box.appendChild(pre);
  ov.appendChild(box);
  document.body.appendChild(ov);
}

// ---- slot form --------------------------------------------------------------
function schedForm() {
  const s = SCHED_EDIT;
  const spec = s.schedule || { kind: "daily" };
  const wrap = el("div", null);
  const dl = ((SCHED && SCHED.known_paths) || [])
    .map((p) => `<option value="${p.replace(/"/g, "&quot;")}"></option>`).join("");
  wrap.innerHTML =
    '<fieldset class="src">' +
      '<label>Name <input type="text" id="sl-name" placeholder="Nightly triage"></label>' +
      '<label>Working directory <input type="text" id="sl-cwd" list="sl-paths" placeholder="/home/you/project"></label>' +
      `<datalist id="sl-paths">${dl}</datalist>` +
      '<label>Prompt <textarea id="sl-prompt" rows="4" placeholder="What should claude do each run?"></textarea></label>' +
      '<label>Timeout (minutes) <input type="number" id="sl-timeout" min="5" max="1440"></label>' +
    '</fieldset>' +
    '<fieldset class="src"><legend>Schedule</legend>' +
      '<label>Repeats <select id="sl-kind">' +
        '<option value="daily">Daily</option>' +
        '<option value="weekly">Weekly</option>' +
        '<option value="everyHours">Every N hours</option>' +
      '</select></label>' +
      '<div id="sl-times-row"><label>Times <input type="text" id="sl-times" placeholder="02:00, 14:30"></label></div>' +
      '<div id="sl-days-row"><label>Days</label><div id="sl-days" class="sched-days"></div></div>' +
      '<div id="sl-hours-row">' +
        '<label>Every <input type="number" id="sl-interval" min="1" max="24" value="2"> hours</label>' +
        '<label><input type="checkbox" id="sl-window-on"> Only between ' +
          '<input type="number" id="sl-win-start" min="0" max="23" value="9">:00 and ' +
          '<input type="number" id="sl-win-end" min="1" max="24" value="18">:00</label>' +
      '</div>' +
      '<p id="sl-preview" class="subtle hint"></p>' +
    '</fieldset>' +
    '<div class="settings-actions"><span id="sched-status" class="subtle"></span>' +
      '<button class="btn ghost small" data-cancel>Cancel</button>' +
      '<button class="btn" data-save>Save slot</button></div>';

  wrap.querySelector("#sl-name").value = s.name || "";
  wrap.querySelector("#sl-cwd").value = s.cwd || "";
  wrap.querySelector("#sl-prompt").value = s.prompt || "";
  wrap.querySelector("#sl-timeout").value = s.timeout_minutes || 90;
  wrap.querySelector("#sl-kind").value = spec.kind || "daily";
  wrap.querySelector("#sl-times").value = (spec.times || [{ hour: 2, minute: 0 }])
    .map((t) => `${String(t.hour).padStart(2, "0")}:${String(t.minute).padStart(2, "0")}`)
    .join(", ");
  const dayNames = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  const daysHost = wrap.querySelector("#sl-days");
  const chosen = new Set(spec.days || []);
  dayNames.forEach((nm, i) => {
    const lab = el("label", "sched-day");
    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.dataset.day = String(i + 1); cb.checked = chosen.has(i + 1);
    cb.addEventListener("change", schedPreview);
    lab.appendChild(cb); lab.appendChild(document.createTextNode(nm));
    daysHost.appendChild(lab);
  });
  if (spec.kind === "everyHours") {
    wrap.querySelector("#sl-interval").value = spec.interval || 2;
    if (spec.window) {
      wrap.querySelector("#sl-window-on").checked = true;
      wrap.querySelector("#sl-win-start").value = spec.window.startHour;
      wrap.querySelector("#sl-win-end").value = spec.window.endHour;
    }
  }

  const syncKind = () => {
    const kind = wrap.querySelector("#sl-kind").value;
    wrap.querySelector("#sl-times-row").style.display = kind === "everyHours" ? "none" : "";
    wrap.querySelector("#sl-days-row").style.display = kind === "weekly" ? "" : "none";
    wrap.querySelector("#sl-hours-row").style.display = kind === "everyHours" ? "" : "none";
    schedPreview();
  };
  wrap.querySelector("#sl-kind").addEventListener("change", syncKind);
  for (const id of ["sl-times", "sl-interval", "sl-window-on", "sl-win-start", "sl-win-end"]) {
    wrap.querySelector("#" + id).addEventListener("input", schedPreview);
  }
  wrap.querySelector("[data-cancel]").addEventListener("click", () => {
    SCHED_EDIT = null; renderScheduler();
  });
  wrap.querySelector("[data-save]").addEventListener("click", () => saveSchedSlot(wrap));
  requestAnimationFrame(syncKind);
  return wrap;
}

function schedSpecFromForm(root) {
  const kind = root.querySelector("#sl-kind").value;
  if (kind === "everyHours") {
    const spec = { kind, interval: parseInt(root.querySelector("#sl-interval").value, 10) || 0 };
    if (root.querySelector("#sl-window-on").checked) {
      spec.window = { startHour: parseInt(root.querySelector("#sl-win-start").value, 10) || 0,
                      endHour: parseInt(root.querySelector("#sl-win-end").value, 10) || 0 };
    }
    return spec;
  }
  const times = [];
  for (const part of root.querySelector("#sl-times").value.split(",")) {
    const m = part.trim().match(/^(\d{1,2}):(\d{2})$/);
    if (m) times.push({ hour: parseInt(m[1], 10), minute: parseInt(m[2], 10) });
    else if (part.trim()) return { kind, times: [] };  // invalid entry → server-side error text
  }
  const spec = { kind, times };
  if (kind === "weekly") {
    spec.days = [...root.querySelectorAll("#sl-days input:checked")]
      .map((cb) => parseInt(cb.dataset.day, 10));
  }
  return spec;
}

function schedPreview() {
  clearTimeout(_schedPreviewTimer);
  _schedPreviewTimer = setTimeout(async () => {
    const modal = document.getElementById("scheduler-modal");
    if (!modal || !SCHED_EDIT) return;
    const out = modal.querySelector("#sl-preview");
    const res = await call("preview_schedule", { spec: schedSpecFromForm(modal) });
    if (!out || !document.getElementById("scheduler-modal")) return;
    if (res && res.ok) {
      out.textContent = res.summary + (res.next_fire ? ` — next ${schedWhen(res.next_fire)}` : "");
      out.classList.remove("sched-err");
    } else {
      out.textContent = (res && (res.error || res.detail)) || "invalid schedule";
      out.classList.add("sched-err");
    }
  }, 250);
}

async function saveSchedSlot(root) {
  const btn = root.querySelector("[data-save]");
  const status = root.querySelector("#sched-status");
  btn.disabled = true;
  status.textContent = "Saving…";
  const slot = {
    id: SCHED_EDIT.id,
    name: root.querySelector("#sl-name").value,
    cwd: root.querySelector("#sl-cwd").value,
    prompt: root.querySelector("#sl-prompt").value,
    timeout_minutes: parseInt(root.querySelector("#sl-timeout").value, 10) || 90,
    schedule: schedSpecFromForm(root),
    enabled: !!SCHED_EDIT.enabled,
  };
  const v = await call("save_slot", { slot });
  if (v && !v.error) {
    SCHED = v; SCHED_EDIT = null; renderScheduler();
  } else {
    btn.disabled = false;
    status.textContent = (v && v.error) || "save failed";
  }
}
