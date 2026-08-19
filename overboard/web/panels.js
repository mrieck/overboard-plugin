"use strict";

// ---- panel strip + router --------------------------------------------------
// The thin far-left rail (a port of the Mac app's PanelStrip): one item per
// panel — Projects · Scheduler · Plugins — and Settings pinned at the bottom.
// Picking Scheduler or Plugins hides the projects nav and hands that panel the
// whole width right of the strip. Panels are long-lived DOM hosts toggled with
// `hidden`; only the visible panel polls (see registerPanel).
//
// Routes (hash): #/projects/<name> · #/scheduler · #/scheduler/run/<id> ·
// #/scheduler/slot/<id> · #/plugins · #/settings/<page> (modal). A legacy "#<project name>" still selects that project.
// Loaded before scheduler.js / plugins.js / app.js; those call registerPanel.

const PANELS = ["projects", "scheduler", "plugins"];
let CUR_PANEL = null;      // which panel is showing
let CUR_ARG = null;        // the route's trailing argument (project name, run id)
let STRIP = null;          // latest strip_status payload
const _panelPollers = {};  // name -> {start(arg), stop(), arg(arg)}
let _stripTimer = null;

// Scheduler/Plugins register a poller; Projects is app.js's own 20s tick.
function registerPanel(name, poller) { _panelPollers[name] = poller; }

function currentPanel() { return CUR_PANEL; }

function parseRoute() {
  const raw = decodeURIComponent(location.hash.slice(1));
  if (!raw) return { panel: "projects", arg: null };
  if (raw[0] !== "/") return { panel: "projects", arg: raw.trim() };  // legacy #Name
  const parts = raw.slice(1).split("/");
  const panel = parts[0] || "projects";
  if (panel === "projects") return { panel, arg: parts.slice(1).join("/") || null };
  if (panel === "scheduler") {
    // #/scheduler/run/<id> or #/scheduler/slot/<id>
    const arg = (parts[1] === "run" || parts[1] === "slot") && parts[2]
      ? { kind: parts[1], id: parts[2] } : null;
    return { panel, arg };
  }
  if (panel === "plugins") return { panel, arg: null };
  if (panel === "settings") return { panel, arg: parts[1] || null };  // modal over the current panel
  return { panel: "projects", arg: null };
}

// Apply the current hash: show the panel, hand the argument to its poller.
function route() {
  let { panel, arg } = parseRoute();
  if (panel === "settings") {
    // #/settings/<page>: the modal over whatever panel is showing.
    if (typeof openSettings === "function") openSettings(arg || "general");
    panel = CUR_PANEL || "projects";
    arg = null;
  }
  const changed = panel !== CUR_PANEL;
  CUR_ARG = arg;
  if (changed) {
    if (CUR_PANEL && _panelPollers[CUR_PANEL]) _panelPollers[CUR_PANEL].stop();
    CUR_PANEL = panel;
    for (const name of PANELS) {
      const host = document.getElementById("panel-" + name);
      if (host) host.hidden = name !== panel;
    }
    if (_panelPollers[panel]) _panelPollers[panel].start(arg);
    renderStrip();
  } else if (_panelPollers[panel] && _panelPollers[panel].arg) {
    _panelPollers[panel].arg(arg);
  }
  if (panel === "projects" && typeof projectFromHash === "function") {
    // Project deep-links resolve once VIEW is loaded; render() re-checks.
    const name = projectFromHash();
    if (name && name !== SELECTED) selectProject(name);
    else if (typeof render === "function" && VIEW) render();
    // Keep the URL shareable once a project is showing.
    if (SELECTED && !name && !location.hash.startsWith("#/settings")) {
      history.replaceState(null, "", "#/projects/" + encodeURIComponent(SELECTED));
    }
  }
}

// Navigate: writes the hash (one history entry per panel switch) and routes.
function showPanel(name, arg) {
  let hash = "#/" + name;
  if (name === "projects" && arg) hash += "/" + encodeURIComponent(arg);
  if (name === "scheduler" && arg) {
    hash += "/" + (arg.kind === "slot" ? "slot" : "run") + "/" + encodeURIComponent(arg.id || arg);
  }
  if (location.hash === hash) { route(); return; }
  location.hash = hash;   // fires hashchange → route()
}

// ---- strip -----------------------------------------------------------------
const STRIP_ICONS = {
  // The ship outline — the app's branding (overboard-mac ShipOutline asset).
  projects: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M11.5 1.5v14.5"/><path d="M10.6 4L2.6 16h8"/><path d="M12.4 3.2l8.6 12.8h-8.6"/><path d="M1.5 18h21l-3.2 4.5H4.7z"/></svg>',
  // clock with circling arrows (SF clock.arrow.2.circlepath, simplified)
  scheduler: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="7.2"/><path d="M12 8v4.3l2.8 1.7"/><path d="M20.5 6.5v3.6h-3.6"/><path d="M3.5 17.5v-3.6h3.6"/><path d="M20.2 10A8.5 8.5 0 0 0 5.2 6.6"/><path d="M3.8 14a8.5 8.5 0 0 0 15 3.4"/></svg>',
  // puzzle piece (SF puzzlepiece.extension)
  plugins: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M9.5 4.5a2 2 0 1 1 4 0V6h3.5a1 1 0 0 1 1 1v3.5h1.5a2 2 0 1 1 0 4H18V18a1 1 0 0 1-1 1h-3.5v-1.5a2 2 0 1 0-4 0V19H6a1 1 0 0 1-1-1v-3.5h1.5a2 2 0 1 0 0-4H5V7a1 1 0 0 1 1-1h3.5V4.5z"/></svg>',
  // gear (SF gearshape)
  settings: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/></svg>',
};

function _stripItem(name, caption, onClick) {
  const item = document.createElement("button");
  item.type = "button";
  item.className = "strip-item";
  item.dataset.panel = name;
  item.innerHTML =
    `<span class="strip-icon-wrap"><span class="strip-icon">${STRIP_ICONS[name]}</span>` +
    `<span class="strip-badge" hidden></span><span class="strip-spin" hidden></span></span>` +
    `<span class="strip-caption">${caption}</span>`;
  item.addEventListener("click", onClick);
  return item;
}

function buildStrip() {
  const strip = document.getElementById("strip");
  if (!strip || strip.childElementCount) return;
  strip.appendChild(_stripItem("projects", "Projects",
    () => showPanel("projects", typeof SELECTED !== "undefined" ? SELECTED : null)));
  strip.appendChild(_stripItem("scheduler", "Scheduler", () => showPanel("scheduler")));
  strip.appendChild(_stripItem("plugins", "Plugins", () => showPanel("plugins")));
  const bottom = document.createElement("div");
  bottom.className = "strip-bottom";
  bottom.appendChild(_stripItem("settings", "Settings", () => {
    if (typeof openSettings === "function") openSettings();
  }));
  strip.appendChild(bottom);
}

// Badge/tooltip inputs: the cheap strip_status poll, overridden by the
// scheduler panel's own fresher payload when it's loaded.
function _stripCounts() {
  const s = STRIP || {};
  const out = {
    running: s.running || 0, queued: s.queued || 0, next_fire: s.next_fire || null,
    installed: s.installed || 0,
    projects: (typeof VIEW !== "undefined" && VIEW && VIEW.projects) ? VIEW.projects.length : null,
  };
  if (typeof SCHED !== "undefined" && SCHED && !SCHED.error) {
    out.running = (SCHED.active || []).length;
    out.queued = (SCHED.queued || []).length;
    const fires = (SCHED.slots || []).map((x) => x.next_fire).filter(Boolean).sort();
    out.next_fire = fires[0] || null;
  }
  if (typeof PLUG !== "undefined" && PLUG && PLUG.inventory && PLUG.inventory.plugins) {
    out.installed = Object.keys(PLUG.inventory.plugins).length;
  }
  return out;
}

function _relativeFire(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return "";
  const mins = Math.round((d - Date.now()) / 60000);
  if (mins < 1) return "now";
  if (mins < 60) return `in ${mins}m`;
  if (mins < 48 * 60) return `in ${Math.round(mins / 60)}h`;
  return `in ${Math.round(mins / 1440)}d`;
}

function renderStrip() {
  buildStrip();
  const c = _stripCounts();
  for (const item of document.querySelectorAll("#strip .strip-item")) {
    const name = item.dataset.panel;
    item.classList.toggle("on", name === CUR_PANEL);
    const badge = item.querySelector(".strip-badge");
    const spin = item.querySelector(".strip-spin");
    if (name === "scheduler") {
      badge.hidden = !(c.running > 0);
      badge.textContent = c.running > 0 ? String(c.running) : "";
      spin.hidden = c.running > 0 || !(c.queued > 0);
    }
    let tip;
    if (name === "projects") {
      tip = c.projects == null ? "Projects (1)" : `Projects · ${c.projects} tracked (1)`;
    } else if (name === "scheduler") {
      const parts = [];
      if (c.running > 0) parts.push(`${c.running} running`);
      if (c.queued > 0) parts.push(`${c.queued} queued`);
      if (!parts.length) {
        parts.push(c.next_fire ? `next run ${_relativeFire(c.next_fire)}` : "no runs scheduled");
      }
      tip = `Scheduler · ${parts.join(" · ")} (2)`;
    } else if (name === "plugins") {
      tip = `Plugins · ${c.installed} installed (3)`;
    } else {
      tip = "Settings";
    }
    item.title = tip;
    item.setAttribute("aria-label", tip);
  }
}

async function pollStrip() {
  try {
    const s = await call("strip_status");
    if (s && !s.error) { STRIP = s; renderStrip(); }
  } catch (_) { /* transient */ }
}

// ---- banners (top of window; the Mac app's RootView banners) --------------
function setBanner(text, kind) {
  const host = document.getElementById("banners");
  if (!host) return;
  host.textContent = "";
  const b = document.createElement("div");
  b.className = "banner " + (kind || "red");
  b.textContent = text;
  const x = document.createElement("button");
  x.className = "banner-x";
  x.title = "Dismiss";
  x.textContent = "✕";
  x.addEventListener("click", clearBanner);
  b.appendChild(x);
  host.appendChild(b);
}
function clearBanner() {
  const host = document.getElementById("banners");
  if (host) host.textContent = "";
}

// ---- keyboard --------------------------------------------------------------
function _editableTarget(e) {
  const t = e.target;
  if (!t) return false;
  if (t.isContentEditable) return true;
  return /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName);
}
function _panelKeys(e) {
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.key === "Escape") {
    if (document.querySelector(".modal-overlay")) return;  // modals own Esc
    const p = _panelPollers[CUR_PANEL];
    if (p && p.escape) p.escape();
    return;
  }
  if (_editableTarget(e)) return;
  if (e.key === "1") showPanel("projects");
  else if (e.key === "2") showPanel("scheduler");
  else if (e.key === "3") showPanel("plugins");
}

function panelsInit() {
  buildStrip();
  document.addEventListener("keydown", _panelKeys);
  window.addEventListener("hashchange", route);
  route();
  pollStrip();
  _stripTimer = setInterval(pollStrip, 20000);
}
