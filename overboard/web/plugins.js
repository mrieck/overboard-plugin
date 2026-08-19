"use strict";

// ---- plugins panel (Claude plugin management + task workspaces) -------------
// A full-width panel picked from the strip (#/plugins). Uses app.js globals:
// call(), el(), note() and scheduler.js helpers: schedWhen(),
// schedSpecFromForm(), tildify(). List modes re-render on a 5s poll; while
// any form is open the poll keeps fetching but never re-renders, so typing is
// never clobbered.

let PLUG = null;          // latest plugins_view payload
let PLUG_TAB = "installed";
let PLUG_EDIT = null;     // workspace being edited ({} for new) — form mode
let PLUG_SCHED = null;    // workspace whose schedule form is open
let PLUG_INSTALL = null;  // popular plugin awaiting a scope choice
let PLUG_POPULAR = null;  // popular_plugins payload
let _plugTimer = null;

function _plugFormOpen() { return !!(PLUG_EDIT || PLUG_SCHED || PLUG_INSTALL); }
function _plugVisible() { return currentPanel() === "plugins"; }

async function startPlugins() {
  PLUG_EDIT = PLUG_SCHED = PLUG_INSTALL = null;
  renderPlugins();
  PLUG = await call("plugins_view");
  renderPlugins();
  if (_plugTimer) clearInterval(_plugTimer);
  _plugTimer = setInterval(async () => {
    try {
      const v = await call("plugins_view");
      if (v && !v.error) {
        PLUG = v;
        if (typeof renderStrip === "function") renderStrip();
        if (!_plugFormOpen()) renderPlugins();
      }
    } catch (_) { /* transient poll errors are fine */ }
  }, 5000);
}

function stopPlugins() {
  if (_plugTimer) { clearInterval(_plugTimer); _plugTimer = null; }
}
// Esc (from panels.js) backs out of an open form.
function _plugEscape() {
  if (_plugFormOpen()) { PLUG_EDIT = PLUG_SCHED = PLUG_INSTALL = null; renderPlugins(); }
}
registerPanel("plugins", { start: startPlugins, stop: stopPlugins, escape: _plugEscape });

// An /api call that refreshes the panel; shows err inline on failure.
async function plugCall(method, args) {
  const v = await call(method, args);
  if (v && !v.error) {
    if (v.inventory) PLUG = v;   // plugins_view-shaped responses replace state
    renderPlugins();
    return true;
  }
  const status = document.getElementById("plug-status");
  if (status) status.textContent = (v && v.error) || "call failed";
  return false;
}

function renderPlugins() {
  const host = document.getElementById("panel-plugins");
  if (!host) return;
  host.textContent = "";
  const inner = el("div", "panel-inner");

  const head = el("div", "panel-head col");
  const top = el("div", "panel-head-top");
  top.appendChild(el("h2", null,
    PLUG_EDIT ? (PLUG_EDIT.id ? "Edit workspace" : "New workspace")
      : PLUG_SCHED ? "Workspace schedule"
      : PLUG_INSTALL ? "Install plugin" : "Plugins"));
  if (PLUG && PLUG.inventory && PLUG.inventory.plugins) {
    top.appendChild(el("span", "subtle", `${Object.keys(PLUG.inventory.plugins).length} installed`));
  }
  if (_plugFormOpen()) {
    const back = el("button", "btn ghost small", "‹ Back");
    back.addEventListener("click", _plugEscape);
    top.appendChild(back);
  }
  head.appendChild(top);
  if (PLUG && !PLUG.error && !_plugFormOpen()) plugTabs(head);
  inner.appendChild(head);

  const body = el("div", "panel-body");
  if (!PLUG) {
    body.appendChild(note("Loading…"));
  } else if (PLUG.error) {
    body.appendChild(note("Plugins unavailable: " + PLUG.error));
  } else if (PLUG_EDIT) {
    body.appendChild(wsForm());
  } else if (PLUG_SCHED) {
    body.appendChild(wsScheduleForm());
  } else if (PLUG_INSTALL) {
    body.appendChild(installForm());
  } else {
    if (PLUG.claude_cli && !PLUG.claude_cli.found) {
      const warn = el("p", "sched-banner",
        "The claude CLI wasn't found on this machine — install/enable actions are unavailable.");
      body.appendChild(warn);
    }
    if (PLUG_TAB === "installed") plugInstalled(body);
    else if (PLUG_TAB === "workspaces") plugWorkspaces(body);
    else plugBrowse(body);
    plugJobs(body);
  }
  const status = el("p", "subtle"); status.id = "plug-status";
  body.appendChild(status);
  inner.appendChild(body);
  host.appendChild(inner);
}

function plugTabs(host) {
  const bar = el("div", "sched-tabs");
  const wsCount = (PLUG && PLUG.workspaces || []).length;
  for (const [key, label, n] of [["installed", "Installed", null],
                                 ["workspaces", "Task workspaces", wsCount || null],
                                 ["browse", "Browse popular", null]]) {
    const b = el("button", "sched-tab" + (PLUG_TAB === key ? " on" : ""), label);
    if (n) b.appendChild(el("span", "tab-n", String(n)));
    b.addEventListener("click", () => { PLUG_TAB = key; renderPlugins(); });
    bar.appendChild(b);
  }
  host.appendChild(bar);
}

// ---- installed tab ----------------------------------------------------------
function plugInstalled(body) {
  const inv = (PLUG && PLUG.inventory) || {};
  const plugins = inv.plugins || {};
  const fs = el("fieldset", "src");
  fs.appendChild(el("legend", null, "Installed plugins"));
  for (const n of inv.notices || []) fs.appendChild(note(n));
  const ids = Object.keys(plugins).sort();
  if (!ids.length) fs.appendChild(note("No Claude plugins found on this machine."));
  for (const id of ids) {
    const p = plugins[id];
    const row = el("div", "sched-slot");
    const top = el("div", "sched-slot-top");
    top.appendChild(el("span", "sched-slot-name", p.name));
    top.appendChild(el("span", "subtle", "@" + (p.marketplace || "?")));
    const version = p.installs.length && p.installs[0].version;
    if (version) top.appendChild(el("span", "subtle", "v" + version));
    const btns = el("span", "sched-slot-btns");
    if (p.enabled_user || p.installs.some((i) => i.scope === "user")) {
      const toggle = el("button", "btn ghost small", p.enabled_user ? "Disable" : "Enable");
      toggle.title = "Enable / disable at user scope (all projects)";
      toggle.addEventListener("click", () => plugCall("plugin_action",
        { action: p.enabled_user ? "disable" : "enable", plugin_id: id, scope: "user" }));
      btns.appendChild(toggle);
    }
    top.appendChild(btns);
    row.appendChild(top);

    for (const inst of p.installs) {
      const bits = [inst.scope + " scope"];
      if (inst.project_path) bits.push(tildify(inst.project_path) + (inst.missing ? " (missing)" : ""));
      const line = el("div", "subtle sched-slot-sub");
      line.appendChild(el("span", inst.missing ? "plug-missing" : null, bits.join("  ·  ")));
      const un = el("button", "btn ghost small plug-inline", "Uninstall");
      un.addEventListener("click", () => {
        if (!confirm(`Uninstall ${id} (${inst.scope} scope)?`)) return;
        plugCall("plugin_action", { action: "uninstall", plugin_id: id,
                                    scope: inst.scope, cwd: inst.project_path });
      });
      line.appendChild(un);
      row.appendChild(line);
    }
    const used = (p.enabled_in || []).map((w) =>
      `${w.kind === "workspace" ? "⚒ " : ""}${w.name}${w.scope === "local" ? " (local)" : ""}`);
    if (p.enabled_user) used.unshift("everywhere (user)");
    row.appendChild(el("div", "subtle sched-slot-sub",
      used.length ? "enabled in: " + used.join(", ") : "not enabled anywhere"));
    if ((inv.orphans || []).includes(id)) {
      row.appendChild(el("div", "subtle sched-slot-sub plug-missing",
        "enabled somewhere but not installed — install it or clean up settings"));
    }
    fs.appendChild(row);
  }
  const add = el("button", "btn small", "+ Add marketplace");
  add.addEventListener("click", async () => {
    const src = prompt("Marketplace source (owner/repo, git URL, or local path):");
    if (src) await plugCall("add_marketplace", { source: src.trim() });
  });
  const wrap = el("p", null); wrap.appendChild(add);
  fs.appendChild(wrap);
  body.appendChild(fs);
}

// ---- workspaces tab ---------------------------------------------------------
function plugWorkspaces(body) {
  const list = (PLUG && PLUG.workspaces) || [];
  const fs = el("fieldset", "src");
  fs.appendChild(el("legend", null, "Task workspaces (~/OverboardWork)"));
  if (!list.length) {
    fs.appendChild(note("No task workspaces yet — a workspace is a folder where " +
      "a scheduled claude run gets exactly the plugins you pick, a brief, and a " +
      "persistent work/ archive."));
  }
  for (const ws of list) {
    const row = el("div", "sched-slot");
    const top = el("div", "sched-slot-top");
    top.appendChild(el("span", "sched-slot-name", `${ws.project}/${ws.task}`));
    top.appendChild(el("span", "subtle", (ws.plugins || []).join(", ") || "no plugins"));
    const btns = el("span", "sched-slot-btns");
    const openBtn = el("button", "btn ghost small", "Open");
    openBtn.title = "Open the workspace folder in a terminal";
    openBtn.addEventListener("click", () => call("open_terminal", { path: ws.path }));
    const schedBtn = el("button", "btn ghost small", ws.slot ? "Schedule…" : "+ Schedule");
    schedBtn.addEventListener("click", () => { PLUG_SCHED = ws; renderPlugins(); });
    const editBtn = el("button", "btn ghost small", "Edit");
    editBtn.addEventListener("click", () => { PLUG_EDIT = { ...ws }; renderPlugins(); });
    btns.appendChild(openBtn); btns.appendChild(schedBtn); btns.appendChild(editBtn);
    top.appendChild(btns);
    row.appendChild(top);
    const slotBits = ws.slot
      ? `⏱ ${ws.slot.summary || ""}${ws.slot.enabled ? "" : " (disabled)"}` +
        (ws.slot.enabled && ws.slot.next_fire ? ` · next ${schedWhen(ws.slot.next_fire)}` : "")
      : "no schedule";
    row.appendChild(el("div", "subtle sched-slot-sub", tildify(ws.path) + "  ·  " + slotBits));
    fs.appendChild(row);
  }
  const add = el("button", "btn small", "+ New workspace");
  add.addEventListener("click", () => { PLUG_EDIT = {}; renderPlugins(); });
  const wrap = el("p", null); wrap.appendChild(add);
  fs.appendChild(wrap);
  body.appendChild(fs);
}

function wsForm() {
  const ws = PLUG_EDIT;
  const inv = (PLUG && PLUG.inventory) || {};
  const wrap = el("div", null);
  const projects = ((PLUG && PLUG.projects) || [])
    .map((p) => `<option value="${p.replace(/"/g, "&quot;")}"></option>`).join("");
  wrap.innerHTML =
    '<fieldset class="src">' +
      '<label>Project <input type="text" id="ws-project" list="ws-projects" placeholder="socialcue"></label>' +
      `<datalist id="ws-projects">${projects}</datalist>` +
      '<label>Task <input type="text" id="ws-task" placeholder="daily-posts"></label>' +
      '<label>Plugins this task may use</label><div id="ws-plugins" class="plug-picks"></div>' +
      '<label>Brief (becomes the workspace CLAUDE.md) ' +
        '<textarea id="ws-brief" rows="6" placeholder="What should this task do each run?"></textarea></label>' +
    '</fieldset>' +
    '<div class="settings-actions"><span id="plug-status" class="subtle"></span>' +
      '<span id="ws-del-btns"></span>' +
      '<button class="btn ghost small" data-cancel>Cancel</button>' +
      '<button class="btn" data-save>Save workspace</button></div>';

  wrap.querySelector("#ws-project").value = ws.project || "";
  wrap.querySelector("#ws-task").value = ws.task || "";
  wrap.querySelector("#ws-brief").value = ws.brief || "";
  if (ws.id) {
    wrap.querySelector("#ws-project").disabled = true;
    wrap.querySelector("#ws-task").disabled = true;
  }
  const picks = wrap.querySelector("#ws-plugins");
  const chosen = new Set(ws.plugins || []);
  const ids = new Set([...Object.keys(inv.plugins || {}), ...chosen]);
  for (const id of [...ids].sort()) {
    const lab = el("label", "sched-day");
    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.dataset.plugin = id; cb.checked = chosen.has(id);
    lab.appendChild(cb); lab.appendChild(document.createTextNode(id));
    picks.appendChild(lab);
  }
  if (!ids.size) picks.appendChild(note("No plugins installed yet — browse popular ones first."));

  if (ws.id) {
    const host = wrap.querySelector("#ws-del-btns");
    const mkDelete = (label, keep) => {
      const b = el("button", "btn ghost small", label);
      b.addEventListener("click", async () => {
        if (!confirm(keep
          ? `Delete workspace ${ws.project}/${ws.task} but keep its work/ archive?`
          : `Delete workspace ${ws.project}/${ws.task} INCLUDING its work/ archive?`)) return;
        PLUG_EDIT = null;
        await plugCall("delete_workspace", { workspace_id: ws.id, keep_work: keep });
      });
      host.appendChild(b);
    };
    mkDelete("Delete (keep work/)", true);
    mkDelete("Delete all", false);
  }

  wrap.querySelector("[data-cancel]").addEventListener("click", () => {
    PLUG_EDIT = null; renderPlugins();
  });
  wrap.querySelector("[data-save]").addEventListener("click", async () => {
    const btn = wrap.querySelector("[data-save]");
    btn.disabled = true;
    const payload = {
      id: ws.id,
      project: wrap.querySelector("#ws-project").value,
      task: wrap.querySelector("#ws-task").value,
      plugins: [...wrap.querySelectorAll("#ws-plugins input:checked")]
        .map((cb) => cb.dataset.plugin),
      brief: wrap.querySelector("#ws-brief").value,
    };
    PLUG_EDIT = null;
    const ok = await plugCall("save_workspace", { workspace: payload });
    if (!ok) { PLUG_EDIT = ws; btn.disabled = false; }
  });
  return wrap;
}

// ---- workspace schedule form ------------------------------------------------
// Reuses slot_editor.js' scheduleEditor() / schedSpecFromForm().
function wsScheduleForm() {
  const ws = PLUG_SCHED;
  const slot = ws.slot || {};
  const spec = slot.schedule || { kind: "daily", times: [{ hour: 9, minute: 0 }] };
  const wrap = el("div", null);
  wrap.innerHTML =
    '<fieldset class="src">' +
      `<p class="subtle">Runs in ${escapeHtml(tildify(ws.path))} with this workspace's plugins.</p>` +
      '<label>Prompt <textarea id="wss-prompt" rows="4" ' +
        'placeholder="What should claude do each run?"></textarea></label>' +
      '<label>Schedule</label><div id="wss-schedule"></div>' +
      '<label><input type="checkbox" id="wss-enabled"> Enabled</label>' +
    '</fieldset>' +
    '<div class="settings-actions"><span id="plug-status" class="subtle"></span>' +
      '<span id="wss-unlink"></span>' +
      '<button class="btn ghost small" data-cancel>Cancel</button>' +
      '<button class="btn" data-save>Save schedule</button></div>';

  wrap.querySelector("#wss-prompt").value = slot.prompt || "";
  const sched = scheduleEditor(spec, ["once", "daily", "weekly", "everyHours"]);
  wrap.querySelector("#wss-schedule").appendChild(sched);
  sched.addEventListener("schedchange", () => schedPreview(sched));
  wrap.querySelector("#wss-enabled").checked = slot.id ? !!slot.enabled : true;

  if (slot.id) {
    const un = el("button", "btn ghost small", "Remove schedule");
    un.addEventListener("click", async () => {
      if (!confirm("Remove this workspace's schedule?")) return;
      await call("delete_slot", { slot_id: slot.id });
      PLUG_SCHED = null;
      await plugCall("plugins_view");
    });
    wrap.querySelector("#wss-unlink").appendChild(un);
  }
  wrap.querySelector("[data-cancel]").addEventListener("click", () => {
    PLUG_SCHED = null; renderPlugins();
  });
  wrap.querySelector("[data-save]").addEventListener("click", async () => {
    const payload = {
      id: slot.id,
      name: slot.name || `${ws.project}/${ws.task}`,
      cwd: ws.path,
      prompt: wrap.querySelector("#wss-prompt").value,
      timeout_minutes: slot.timeout_minutes || 90,
      schedule: schedSpecFromForm(sched),
      enabled: wrap.querySelector("#wss-enabled").checked,
    };
    PLUG_SCHED = null;
    const ok = await plugCall("link_workspace_slot", { workspace_id: ws.id, slot: payload });
    if (!ok) PLUG_SCHED = ws;
  });
  return wrap;
}

// ---- browse tab -------------------------------------------------------------
function plugBrowse(body) {
  const fs = el("fieldset", "src");
  const legend = el("legend", null, "Popular plugins — plugmyplugin.com");
  fs.appendChild(legend);
  const refreshBtn = el("button", "btn ghost small plug-inline", "Refresh");
  refreshBtn.addEventListener("click", async () => {
    PLUG_POPULAR = await call("popular_plugins", { force: true });
    renderPlugins();
  });
  legend.appendChild(refreshBtn);

  if (!PLUG_POPULAR) {
    fs.appendChild(note("Loading…"));
    body.appendChild(fs);
    call("popular_plugins").then((res) => {
      PLUG_POPULAR = res || { payload: null };
      if (_plugVisible() && PLUG_TAB === "browse" && !_plugFormOpen()) renderPlugins();
    });
    return;
  }
  const payload = PLUG_POPULAR.payload;
  if (!payload || !payload.plugins) {
    fs.appendChild(note("plugmyplugin.com is unreachable right now" +
      (PLUG_POPULAR.error ? ` (${PLUG_POPULAR.error})` : "") + "."));
    body.appendChild(fs);
    return;
  }
  if (PLUG_POPULAR.stale) fs.appendChild(note("Showing cached results — the site is unreachable."));
  for (const p of payload.plugins) {
    const row = el("div", "sched-slot");
    const top = el("div", "sched-slot-top");
    top.appendChild(el("span", "sched-slot-name", p.name));
    top.appendChild(el("span", "subtle", p.owner || ""));
    if (p.stars != null) top.appendChild(el("span", "plug-stars", `★ ${p.stars}`));
    if (p.card && p.card.category) top.appendChild(el("span", "subtle", p.card.category));
    const btns = el("span", "sched-slot-btns");
    const inst = el("button", "btn ghost small", "Install…");
    inst.addEventListener("click", () => { PLUG_INSTALL = p; renderPlugins(); });
    const link = el("button", "btn ghost small", "↗");
    link.title = "Open on plugmyplugin.com";
    link.addEventListener("click", () => window.open(p.url, "_blank"));
    btns.appendChild(inst); btns.appendChild(link);
    top.appendChild(btns);
    row.appendChild(top);
    if (p.card && p.card.summary) {
      row.appendChild(el("div", "subtle sched-slot-sub", p.card.summary));
    }
    fs.appendChild(row);
  }
  body.appendChild(fs);
}

function installForm() {
  const p = PLUG_INSTALL;
  const wrap = el("div", null);
  const workspaces = (PLUG && PLUG.workspaces) || [];
  const paths = (PLUG && PLUG.known_paths) || [];
  const wsOpts = workspaces.map((w) =>
    `<option value="ws:${w.id}">workspace ${w.project}/${w.task}</option>`).join("");
  const projOpts = paths.map((path) =>
    `<option value="proj:${path.replace(/"/g, "&quot;")}">project ${path}</option>`).join("");
  wrap.innerHTML =
    '<fieldset class="src">' +
      `<p><strong>${p.name}</strong> <span class="subtle">${p.owner || ""}</span></p>` +
      (p.card && p.card.summary ? `<p class="subtle">${p.card.summary}</p>` : "") +
      '<label>Install into <select id="pi-scope">' +
        '<option value="user">everywhere (user scope)</option>' +
        wsOpts + projOpts +
      '</select></label>' +
      `<p class="subtle hint">Marketplace ${p.marketplaceRepo || "?"} will be added first if needed.</p>` +
    '</fieldset>' +
    '<div class="settings-actions"><span id="plug-status" class="subtle"></span>' +
      '<button class="btn ghost small" data-cancel>Cancel</button>' +
      '<button class="btn" data-save>Install</button></div>';
  wrap.querySelector("[data-cancel]").addEventListener("click", () => {
    PLUG_INSTALL = null; renderPlugins();
  });
  wrap.querySelector("[data-save]").addEventListener("click", async () => {
    const pick = wrap.querySelector("#pi-scope").value;
    let scope = "user", cwd = null;
    if (pick.startsWith("ws:")) {
      const ws = workspaces.find((w) => w.id === pick.slice(3));
      scope = "project"; cwd = ws && ws.path;
    } else if (pick.startsWith("proj:")) {
      scope = "project"; cwd = pick.slice(5);
    }
    if (p.marketplaceRepo) await call("add_marketplace", { source: p.marketplaceRepo });
    PLUG_INSTALL = null;
    // Bare name resolves across known marketplaces once the add lands (jobs
    // run one at a time, in order).
    await plugCall("plugin_action", { action: "install", plugin_id: p.name, scope, cwd });
    PLUG_TAB = "installed";
    renderPlugins();
  });
  return wrap;
}

// ---- job strip --------------------------------------------------------------
function plugJobs(body) {
  const jobs = (PLUG && PLUG.jobs) || [];
  const live = jobs.filter((j) => j.state === "queued" || j.state === "running");
  const recent = jobs.filter((j) => j.state === "done" || j.state === "failed").slice(0, 3);
  if (!live.length && !recent.length) return;
  const fs = el("fieldset", "src");
  fs.appendChild(el("legend", null, "Plugin CLI activity"));
  for (const j of [...live, ...recent]) {
    const row = el("div", "sched-run");
    row.appendChild(el("span", `sched-chip sched-${j.state === "done" ? "completed"
      : j.state === "failed" ? "launch_failed" : "running"}`, j.state));
    row.appendChild(el("span", "sched-slot-name",
      `${j.action.replace(/_/g, " ")} ${j.plugin || ""}`));
    if (j.state === "failed" && j.output) {
      row.appendChild(el("span", "subtle", j.output.slice(-160)));
    }
    fs.appendChild(row);
  }
  body.appendChild(fs);
}
