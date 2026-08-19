"use strict";

// ---- slot editor (the Scheduler panel's permanent right pane) ---------------
// A port of the Mac app's SlotEditorPane + ScheduleEditorView: name, the
// command/prompt, the project folder (or a linked task workspace), a schedule
// (Once · Daily · Weekly · Every N hours), timeout, stall minutes, enabled.
// Built once per selection by scheduler.js renderRight(); never re-rendered by
// the poll. Form fields are addressed by data-field so plugins.js' workspace
// schedule form can reuse schedSpecFromForm() with its own markup.

let _schedPreviewTimer = null;
let _schedWorkspaces = null;   // {at, list} — lazy plugins_view().workspaces
const SCHED_DAY_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

function _pad2(n) { return String(n).padStart(2, "0"); }
function _timesText(times) {
  return (times || []).map((t) => `${_pad2(t.hour)}:${_pad2(t.minute)}`).join(", ");
}
// ISO (UTC Z or local) → value for <input type=datetime-local>.
function _toLocalInput(iso) {
  const d = iso ? new Date(iso) : new Date(Date.now() + 3600 * 1000);
  if (isNaN(d)) return "";
  return `${d.getFullYear()}-${_pad2(d.getMonth() + 1)}-${_pad2(d.getDate())}T${_pad2(d.getHours())}:${_pad2(d.getMinutes())}`;
}

// The schedule sub-form, shared by the slot editor and the workspace form.
// `kinds` limits the segmented control. Returns the root element.
function scheduleEditor(spec, kinds) {
  spec = spec || { kind: "daily" };
  kinds = kinds || ["once", "daily", "weekly", "everyHours"];
  const labels = { once: "Once", daily: "Daily", weekly: "Weekly", everyHours: "Every N hours" };
  const root = el("div", "sched-editor");
  const seg = el("div", "segmented");
  root.appendChild(seg);
  const kindInput = document.createElement("input");
  kindInput.type = "hidden"; kindInput.dataset.field = "kind";
  kindInput.value = kinds.includes(spec.kind) ? spec.kind : kinds[0];
  root.appendChild(kindInput);

  const rows = {};
  // once
  const once = el("div", "sched-row");
  once.innerHTML = '<label>When <input type="datetime-local" data-field="date"></label>' +
    '<p class="sched-passed sched-err" hidden>⚠ that moment has passed — it will never fire</p>';
  once.querySelector("[data-field=date]").value = _toLocalInput(spec.kind === "once" ? spec.date : null);
  rows.once = once;
  // times (daily + weekly)
  const times = el("div", "sched-row");
  times.innerHTML = '<label>Times <input type="text" data-field="times" placeholder="02:00, 14:30"></label>';
  times.querySelector("[data-field=times]").value = _timesText(spec.times || [{ hour: 2, minute: 0 }]);
  rows.times = times;
  // days (weekly)
  const days = el("div", "sched-row");
  days.appendChild(el("label", null, "Days"));
  const chips = el("div", "sched-days");
  const chosen = new Set(spec.days || [2, 3, 4, 5, 6]);
  SCHED_DAY_NAMES.forEach((nm, i) => {
    const lab = el("label", "sched-day" + (chosen.has(i + 1) ? " on" : ""));
    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.dataset.day = String(i + 1); cb.checked = chosen.has(i + 1);
    cb.addEventListener("change", () => lab.classList.toggle("on", cb.checked));
    lab.appendChild(cb); lab.appendChild(document.createTextNode(nm));
    chips.appendChild(lab);
  });
  days.appendChild(chips);
  rows.days = days;
  // every N hours
  const hours = el("div", "sched-row sched-hours");
  hours.innerHTML =
    '<label>Every <input type="number" data-field="interval" min="1" max="24" value="2"> hours</label>' +
    '<label class="inline"><input type="checkbox" data-field="window_on"> Only between ' +
      '<input type="number" data-field="win_start" min="0" max="23" value="9">:00 and ' +
      '<input type="number" data-field="win_end" min="1" max="24" value="18">:00</label>';
  if (spec.kind === "everyHours") {
    hours.querySelector("[data-field=interval]").value = spec.interval || 2;
    if (spec.window) {
      hours.querySelector("[data-field=window_on]").checked = true;
      hours.querySelector("[data-field=win_start]").value = spec.window.startHour;
      hours.querySelector("[data-field=win_end]").value = spec.window.endHour;
    }
  }
  rows.hours = hours;
  for (const r of Object.values(rows)) root.appendChild(r);
  const preview = el("p", "subtle sched-preview"); preview.dataset.field = "preview";
  root.appendChild(preview);

  const sync = () => {
    const kind = kindInput.value;
    for (const b of seg.querySelectorAll("button")) b.classList.toggle("on", b.dataset.kind === kind);
    rows.once.hidden = kind !== "once";
    rows.times.hidden = !(kind === "daily" || kind === "weekly");
    rows.days.hidden = kind !== "weekly";
    rows.hours.hidden = kind !== "everyHours";
    root.dispatchEvent(new CustomEvent("schedchange", { bubbles: true }));
  };
  for (const k of kinds) {
    const b = el("button", "seg", labels[k]);
    b.type = "button"; b.dataset.kind = k;
    b.addEventListener("click", () => { kindInput.value = k; sync(); });
    seg.appendChild(b);
  }
  root.addEventListener("input", () => root.dispatchEvent(new CustomEvent("schedchange", { bubbles: true })));
  root.addEventListener("change", () => root.dispatchEvent(new CustomEvent("schedchange", { bubbles: true })));
  requestAnimationFrame(sync);
  return root;
}

// Read a schedule spec back out of a scheduleEditor() root (or any element
// carrying the same data-field attributes).
function schedSpecFromForm(root) {
  const f = (name) => root.querySelector(`[data-field=${name}]`);
  const kind = f("kind").value;
  if (kind === "once") {
    const v = f("date").value;  // local wall-clock from <input type=datetime-local>
    const d = v ? new Date(v) : null;
    return { kind, date: d && !isNaN(d) ? d.toISOString() : "" };
  }
  if (kind === "everyHours") {
    const spec = { kind, interval: parseInt(f("interval").value, 10) || 0 };
    if (f("window_on").checked) {
      spec.window = { startHour: parseInt(f("win_start").value, 10) || 0,
                      endHour: parseInt(f("win_end").value, 10) || 0 };
    }
    return spec;
  }
  const times = [];
  for (const part of f("times").value.split(",")) {
    const m = part.trim().match(/^(\d{1,2}):(\d{2})$/);
    if (m) times.push({ hour: parseInt(m[1], 10), minute: parseInt(m[2], 10) });
    else if (part.trim()) return { kind, times: [] };  // invalid entry → server-side error text
  }
  const spec = { kind, times };
  if (kind === "weekly") {
    spec.days = [...root.querySelectorAll(".sched-days input:checked")]
      .map((cb) => parseInt(cb.dataset.day, 10));
  }
  return spec;
}

// Debounced live validation line under the schedule editor.
function schedPreview(root) {
  clearTimeout(_schedPreviewTimer);
  _schedPreviewTimer = setTimeout(async () => {
    if (!root.isConnected) return;
    const out = root.querySelector("[data-field=preview]");
    const spec = schedSpecFromForm(root);
    const passed = root.querySelector(".sched-passed");
    if (passed) {
      const d = spec.kind === "once" && spec.date ? new Date(spec.date) : null;
      passed.hidden = !(d && !isNaN(d) && d <= new Date());
    }
    let res;
    try { res = await call("preview_schedule", { spec }); } catch (_) { return; }
    if (!out || !root.isConnected) return;
    if (res && res.ok) {
      out.textContent = res.summary + (res.next_fire ? ` — next ${schedWhen(res.next_fire)}` : "");
      out.classList.remove("sched-err");
    } else {
      out.textContent = (res && (res.error || res.detail)) || "invalid schedule";
      out.classList.add("sched-err");
    }
  }, 250);
}

async function _schedLoadWorkspaces() {
  if (_schedWorkspaces && Date.now() - _schedWorkspaces.at < 60000) return _schedWorkspaces.list;
  try {
    const v = await call("plugins_view");
    _schedWorkspaces = { at: Date.now(), list: (v && v.workspaces) || [] };
  } catch (_) {
    _schedWorkspaces = { at: Date.now(), list: [] };
  }
  return _schedWorkspaces.list;
}

// ---- the pane ------------------------------------------------------------------
function slotEditorPane(slot) {
  const isNew = !slot;
  const s = slot || {};
  const pane = el("div", "pane slot-editor");
  pane.dataset.slotId = s.id || "";

  const head = el("div", "pane-head");
  head.appendChild(el("h3", null, isNew ? "New agent" : "Edit agent"));
  if (!isNew) {
    const x = el("button", "btn ghost small", "✕");
    x.title = "Back to a blank editor";
    x.addEventListener("click", () => schedSelect({ kind: "new", id: null }));
    head.appendChild(x);
  }
  pane.appendChild(head);

  const body = el("div", "pane-body");
  const dl = ((SCHED && SCHED.known_paths) || [])
    .map((p) => `<option value="${escapeHtml(p)}"></option>`).join("");
  body.innerHTML =
    '<label class="field">Name <input type="text" data-field="name" placeholder="Nightly triage"></label>' +
    '<div class="field"><span class="field-label">Command / prompt</span><div data-host="prompt"></div></div>' +
    '<label class="field">Project folder <input type="text" data-field="cwd" list="se-paths" placeholder="/home/you/project"></label>' +
    `<datalist id="se-paths">${dl}</datalist>` +
    '<label class="field">Workspace <select data-field="workspace"><option value="">None — run in the project folder</option></select>' +
      '<span class="subtle hint ws-hint" hidden>Runs in the workspace folder with exactly its plugins; the folder above follows it.</span></label>' +
    '<div class="field"><span class="field-label">Schedule</span><div data-host="schedule"></div></div>' +
    '<details class="field advanced"><summary>Advanced</summary>' +
      '<label class="field inline-num">Timeout <input type="number" data-field="timeout" min="5" max="1440"> minutes</label>' +
      '<label class="field inline-num">Waiting for input after <input type="number" data-field="stall" min="1" max="120"> idle minutes ' +
        '<span class="subtle hint">(only when Herdr can\'t classify the screen)</span></label>' +
    '</details>' +
    '<label class="field switch-row"><span class="switch"><input type="checkbox" data-field="enabled"><span class="switch-track"></span></span> Enabled' +
      '<span class="subtle hint">Off = it stays in the list but never fires.</span></label>';
  pane.appendChild(body);

  const f = (name) => body.querySelector(`[data-field=${name}]`);
  f("name").value = s.name || "";
  f("cwd").value = s.cwd || "";
  f("timeout").value = s.timeout_minutes || (SCHED && SCHED.default_timeout_minutes) || 90;
  f("stall").value = s.stall_minutes || 10;
  f("enabled").checked = isNew ? true : !!s.enabled;

  // Prompt: the completion-aware field when promptfield.js is loaded, a plain
  // textarea otherwise (Phase 4 swaps it in).
  let promptGet;
  const promptHost = body.querySelector("[data-host=prompt]");
  if (typeof promptField === "function") {
    const pf = promptField({
      value: s.prompt || "",
      cwd: () => f("cwd").value,
      placeholder: "/overboard:overboard, or what claude should do each run…",
    });
    promptHost.appendChild(pf.root);
    promptGet = pf.getValue;
  } else {
    const ta = document.createElement("textarea");
    ta.rows = 5; ta.dataset.field = "prompt";
    ta.placeholder = "What should claude do each run?";
    ta.value = s.prompt || "";
    promptHost.appendChild(ta);
    promptGet = () => ta.value;
  }

  // Schedule editor.
  const kinds = (SCHED && SCHED.supports_once === false)
    ? ["daily", "weekly", "everyHours"] : ["once", "daily", "weekly", "everyHours"];
  const sched = scheduleEditor(s.schedule, kinds);
  body.querySelector("[data-host=schedule]").appendChild(sched);
  sched.addEventListener("schedchange", () => schedPreview(sched));

  // Workspace link (lazy list).
  const wsSel = f("workspace");
  const wsHint = body.querySelector(".ws-hint");
  const syncWs = () => {
    const linked = !!wsSel.value;
    f("cwd").disabled = linked;
    wsHint.hidden = !linked;
    if (linked) {
      const ws = ((_schedWorkspaces && _schedWorkspaces.list) || []).find((w) => w.id === wsSel.value);
      if (ws) f("cwd").value = ws.path;
    }
  };
  _schedLoadWorkspaces().then((list) => {
    if (!pane.isConnected) return;
    for (const ws of list) {
      const o = document.createElement("option");
      o.value = ws.id; o.textContent = `${ws.project}/${ws.task}`;
      wsSel.appendChild(o);
    }
    if (s.workspace_id) wsSel.value = s.workspace_id;
    syncWs();
  });
  wsSel.addEventListener("change", syncWs);

  // Footer.
  const foot = el("div", "pane-foot");
  if (!isNew) {
    const del = el("button", "btn ghost small danger", "Delete");
    del.addEventListener("click", async () => {
      if (!confirm(`Delete “${s.name}”?`)) return;
      if (await schedCall("delete_slot", { slot_id: s.id })) schedSelect({ kind: "new", id: null });
    });
    foot.appendChild(del);
  }
  const status = el("span", "subtle pane-status"); status.dataset.field = "status";
  foot.appendChild(status);
  if (!isNew) {
    const runNow = el("button", "btn ghost small", "Run now");
    runNow.addEventListener("click", async () => {
      runNow.disabled = true; runNow.textContent = "Starting…";
      await schedCall("run_slot_now", { slot_id: s.id });
      runNow.disabled = false; runNow.textContent = "Run now";
      status.textContent = "queued";
      setTimeout(() => { if (status.textContent === "queued") status.textContent = ""; }, 2500);
    });
    foot.appendChild(runNow);
  }
  const save = el("button", "btn", isNew ? "Create" : "Save");
  save.addEventListener("click", async () => {
    save.disabled = true;
    status.textContent = "Saving…"; status.classList.remove("sched-err");
    const payload = {
      id: s.id,
      name: f("name").value,
      cwd: f("cwd").value,
      prompt: promptGet(),
      timeout_minutes: parseInt(f("timeout").value, 10) || undefined,
      stall_minutes: parseInt(f("stall").value, 10) || undefined,
      schedule: schedSpecFromForm(sched),
      workspace_id: wsSel.value || null,
      enabled: f("enabled").checked,
    };
    let v;
    try { v = await call("save_slot", { slot: payload }); } catch (e) { v = { error: String(e) }; }
    save.disabled = false;
    if (v && !v.error) {
      SCHED = v;
      if (typeof renderStrip === "function") renderStrip();
      if (isNew) {
        // Select the slot we just made so the editor shows it.
        const mine = (v.slots || []).find((x) => x.name === payload.name.trim() && x.cwd === (payload.cwd || x.cwd));
        const newest = mine || (v.slots || [])[(v.slots || []).length - 1];
        _schedRightKey = null;
        schedSelect(newest ? { kind: "slot", id: newest.id } : { kind: "new", id: null });
      } else {
        renderSchedulerPanel();
        status.textContent = "saved";
        setTimeout(() => { if (status.textContent === "saved") status.textContent = ""; }, 2000);
      }
    } else {
      status.textContent = (v && v.error) || "save failed";
      status.classList.add("sched-err");
    }
  });
  foot.appendChild(save);
  pane.appendChild(foot);
  return pane;
}

// The list's enable switch changed this slot: mirror it into an open editor
// without rebuilding (so unsaved edits survive).
function slotEditorSync(slotId) {
  const pane = document.querySelector(`#sched-right .slot-editor[data-slot-id="${slotId}"]`);
  if (!pane || !SCHED) return;
  const slot = (SCHED.slots || []).find((x) => x.id === slotId);
  const cb = pane.querySelector("[data-field=enabled]");
  if (slot && cb) cb.checked = !!slot.enabled;
}
