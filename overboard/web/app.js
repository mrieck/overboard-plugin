"use strict";

// ---- backend bridge (stdlib HTTP; no pywebview dependency) ------------------
async function call(method, args) {
  const res = await fetch("/api/" + method, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(args || {}),
  });
  return res.json();
}

// A well-formed view always has a projects array. An error object (e.g. a 403
// from a stale server that predates a new endpoint) must never replace VIEW, or
// it corrupts the whole UI. Returns the view, or null on error/bad shape.
function isView(v) { return v && Array.isArray(v.projects); }
async function callView(method, args) {
  try {
    const v = await call(method, args);
    if (isView(v)) return v;
    console.warn("Overboard: ignoring bad response from", method, v);
  } catch (e) {
    console.warn("Overboard: call failed", method, e);
  }
  return null;
}

let VIEW = null;
let SELECTED = null;   // selected project name
let ANALYSES = {};     // slug -> { tab, data, error, loading } for that project's local repos
let CONTEXT_FOR = null; // project whose right-panel context is currently rendered
// Recent-work cards: renderReport re-runs on every 20s tick, so expansion state
// and rendered Mermaid SVGs must live outside the DOM or cards snap shut and
// diagrams flicker. WORK_OPEN: project -> Set of expanded unit ids (newest card
// opens by default); WORK_MMD: unit id -> rendered SVG string.
const WORK_OPEN = {};
const WORK_MMD = {};

function boot() {
  if (window.mermaid) {
    mermaid.initialize({ startOnLoad: false, theme: "dark", securityLevel: "loose" });
  }
  if (document.readyState !== "loading") init();
  else document.addEventListener("DOMContentLoaded", init);
}

// Render Mermaid text into a host element (async; safe to fire-and-forget).
async function renderMermaid(host, code) {
  if (!code || !window.mermaid) return;
  const wrap = document.createElement("div");
  wrap.className = "mermaid-out";
  host.appendChild(wrap);
  try {
    const id = "mmd-" + Math.random().toString(36).slice(2);
    const { svg } = await mermaid.render(id, code);
    wrap.appendChild(diagramViewer(svg));
  } catch (e) {
    wrap.className = "subtle";
    wrap.textContent = "diagram unavailable: " + (e && e.message ? e.message : e);
  }
}

// ---- interactive diagram viewer (pan/zoom + fullscreen) --------------------
// Mermaid renders an SVG at natural size; force-fitting it to the panel width
// (the old `max-width:100%`) shrank the text to nothing on dense ER diagrams.
// Instead we drop it on a pannable/zoomable canvas that starts fit-to-view.

// Natural pixel size of a mermaid SVG, parsed from its viewBox (no DOM needed).
function svgNatural(svg) {
  const vb = (svg.getAttribute("viewBox") || "").split(/[\s,]+/).map(Number);
  if (vb.length === 4 && vb[2] > 0 && vb[3] > 0) return { w: vb[2], h: vb[3] };
  return { w: parseFloat(svg.getAttribute("width")) || 800,
           h: parseFloat(svg.getAttribute("height")) || 400 };
}

// Build a pan/zoom viewer around a rendered SVG string. `fullscreen` drops the
// Expand button (it's already expanded). Returns the root element.
function diagramViewer(svgMarkup, fullscreen) {
  const root = el("div", "diagram");
  const viewport = el("div", "diagram-viewport");
  const canvas = el("div", "diagram-canvas");
  canvas.innerHTML = svgMarkup;
  root.appendChild(viewport);
  viewport.appendChild(canvas);

  const svg = canvas.querySelector("svg");
  const nat = svg ? svgNatural(svg) : { w: 800, h: 400 };
  if (svg) {
    // Pin the SVG to its natural size so the canvas has real dimensions to scale.
    svg.style.maxWidth = "none";
    svg.style.width = nat.w + "px";
    svg.style.height = nat.h + "px";
    svg.style.display = "block";
  }

  const st = { scale: 1, x: 0, y: 0, min: 0.1, max: 8 };
  const apply = () => { canvas.style.transform =
    `translate(${st.x}px, ${st.y}px) scale(${st.scale})`; };
  function fit() {
    const vw = viewport.clientWidth, vh = viewport.clientHeight;
    if (!vw || !vh || !nat.w || !nat.h) return;
    st.scale = Math.max(st.min, Math.min(st.max, Math.min(vw / nat.w, vh / nat.h)));
    st.x = (vw - nat.w * st.scale) / 2;
    st.y = (vh - nat.h * st.scale) / 2;
    apply();
  }
  function zoomAt(factor, cx, cy) {
    const ns = Math.max(st.min, Math.min(st.max, st.scale * factor));
    const k = ns / st.scale;
    st.x = cx - k * (cx - st.x);
    st.y = cy - k * (cy - st.y);
    st.scale = ns;
    apply();
  }

  viewport.addEventListener("wheel", (e) => {
    e.preventDefault();
    const r = viewport.getBoundingClientRect();
    zoomAt(e.deltaY < 0 ? 1.15 : 1 / 1.15, e.clientX - r.left, e.clientY - r.top);
  }, { passive: false });

  let drag = false, sx = 0, sy = 0, ox = 0, oy = 0;
  viewport.addEventListener("pointerdown", (e) => {
    drag = true; sx = e.clientX; sy = e.clientY; ox = st.x; oy = st.y;
    viewport.setPointerCapture(e.pointerId); viewport.classList.add("grabbing");
  });
  viewport.addEventListener("pointermove", (e) => {
    if (!drag) return;
    st.x = ox + (e.clientX - sx); st.y = oy + (e.clientY - sy); apply();
  });
  const endDrag = () => { drag = false; viewport.classList.remove("grabbing"); };
  viewport.addEventListener("pointerup", endDrag);
  viewport.addEventListener("pointercancel", endDrag);

  const bar = el("div", "diagram-toolbar");
  const mid = () => ({ cx: viewport.clientWidth / 2, cy: viewport.clientHeight / 2 });
  const btn = (label, title, fn) => {
    const b = el("button", "btn ghost small", label);
    b.title = title;
    b.addEventListener("click", (ev) => { ev.stopPropagation(); fn(); });
    bar.appendChild(b);
  };
  btn("−", "Zoom out", () => { const m = mid(); zoomAt(1 / 1.2, m.cx, m.cy); });
  btn("Reset", "Fit to view", fit);
  btn("+", "Zoom in", () => { const m = mid(); zoomAt(1.2, m.cx, m.cy); });
  if (!fullscreen) btn("⤢", "Expand to fullscreen", () => openDiagramModal(svgMarkup));
  root.appendChild(bar);

  // Fit once the viewport actually has a measured size (it's built detached).
  const tryFit = (n) => {
    if (viewport.clientWidth && viewport.clientHeight) fit();
    else if (n < 12) requestAnimationFrame(() => tryFit(n + 1));
  };
  requestAnimationFrame(() => tryFit(0));
  root._fit = fit;
  return root;
}

function openDiagramModal(svgMarkup) {
  closeDiagramModal();
  const ov = el("div", "modal-overlay");
  ov.id = "diagram-modal";
  ov.addEventListener("click", (e) => { if (e.target === ov) closeDiagramModal(); });
  const box = el("div", "modal modal-diagram");
  const head = el("div", "modal-head");
  head.appendChild(el("h3", null, "Diagram"));
  const close = el("button", "btn ghost small", "Close");
  close.addEventListener("click", closeDiagramModal);
  head.appendChild(close);
  box.appendChild(head);
  const viewer = diagramViewer(svgMarkup, true);
  box.appendChild(viewer);
  ov.appendChild(box);
  document.body.appendChild(ov);
  requestAnimationFrame(() => viewer._fit && viewer._fit());
  document.addEventListener("keydown", _escCloseDiagram);
}
function closeDiagramModal() {
  const m = document.getElementById("diagram-modal");
  if (m) m.remove();
  document.removeEventListener("keydown", _escCloseDiagram);
}
function _escCloseDiagram(e) { if (e.key === "Escape") closeDiagramModal(); }

async function init() {
  wireControls();
  await loadView();
  // First paint shows cached state; then pull fresh data in the background.
  refresh();
  // Cheap periodic poll for live activity from the working Claudes (event-gated
  // server-side, so it only spends tokens when something actually finished).
  setInterval(tick, 20000);
}

let _ticking = false;
async function tick() {
  if (_ticking || _refreshingNow) return;
  _ticking = true;
  try {
    const v = await callView("tick");
    if (v) { VIEW = v; render(); }
  } catch (_) {
    /* ignore transient poll errors */
  } finally {
    _ticking = false;
  }
}

async function loadView() {
  const v = await callView("get_view");
  if (v) { VIEW = v; render(); }
}

// ---- top-level controls ----------------------------------------------------
function wireControls() {
  document.getElementById("refresh").addEventListener("click", refresh);
  document.getElementById("rescan").addEventListener("click", rescan);
  document.getElementById("settings").addEventListener("click", openSettings);
  document.getElementById("context-toggle").addEventListener("click", toggleContext);
  applyContextCollapsed();
}

function applyContextCollapsed() {
  const collapsed = localStorage.getItem("ob-context-collapsed") === "1";
  document.body.classList.toggle("context-collapsed", collapsed);
  document.getElementById("context-toggle").textContent = collapsed ? "‹" : "›";
}
function toggleContext() {
  const collapsed = document.body.classList.toggle("context-collapsed");
  localStorage.setItem("ob-context-collapsed", collapsed ? "1" : "0");
  document.getElementById("context-toggle").textContent = collapsed ? "‹" : "›";
}

let _refreshingNow = false;
async function refresh() {
  const btn = document.getElementById("refresh");
  btn.disabled = true;
  btn.textContent = "Refreshing…";
  _refreshingNow = true;
  const t0 = performance.now();
  console.log("[overboard] refresh: calling /api/refresh (provider API calls happen server-side)…");
  try {
    const v = await callView("refresh");
    console.log(`[overboard] refresh: /api/refresh returned in ${(performance.now() - t0).toFixed(0)} ms`);
    if (v) { VIEW = v; render(); }
    // New commits may have moved a clone's HEAD — re-pull the analyses too.
    const proj = currentProject();
    if (proj) loadAnalyses(proj);
  } finally {
    _refreshingNow = false;
    btn.disabled = false;
    btn.textContent = "Refresh";
  }
}

async function rescan() {
  const btn = document.getElementById("rescan");
  btn.disabled = true;
  try {
    const v = await callView("rescan_local");
    if (v) { VIEW = v; render(); }
  } finally {
    btn.disabled = false;
  }
}

// ---- top-level render -------------------------------------------------------
function currentProject() {
  return (VIEW && VIEW.projects.find((p) => p.name === SELECTED)) || null;
}

function render() {
  renderStatus();
  renderSidebar();

  const detail = document.getElementById("detail");
  const proj = currentProject();
  if (!proj) {
    // Selection is gone (or nothing selected yet) — reset the right panel.
    SELECTED = null;
    ANALYSES = {};
    CONTEXT_FOR = null;
    detail.innerHTML = '<div class="detail-empty">Select a project on the left.</div>';
    document.getElementById("context-body").innerHTML =
      '<p class="subtle ctx-hint">Select a project to set its launch & vision.</p>';
    return;
  }
  ensureDetailShell();  // keeps any open analysis intact across ticks
  renderReport(proj);
  if (CONTEXT_FOR !== proj.name) renderContext(proj);  // don't wipe a form on ticks
}

function renderStatus() {
  const el = document.getElementById("status");
  el.classList.toggle("warn", VIEW.last_refresh_ok === false);
  const when = VIEW.last_refresh ? fmtTime(VIEW.last_refresh) : "never";
  el.textContent = VIEW.last_refresh_ok === false
    ? `stale · offline (${when})`
    : `updated ${when} · ${VIEW.machine}`;
  el.title = VIEW.last_error || "";
}

// ---- sidebar (condensed project list) --------------------------------------
function renderSidebar() {
  const host = document.getElementById("project-list");
  host.textContent = "";
  if (!VIEW.has_sources) {
    const wrap = document.createElement("div");
    wrap.className = "empty";
    const p = document.createElement("p");
    p.textContent = "No sources yet — add a Bitbucket or GitHub token to get started.";
    const b = document.createElement("button");
    b.className = "btn small";
    b.textContent = "Open Settings ⚙";
    b.addEventListener("click", openSettings);
    wrap.appendChild(p);
    wrap.appendChild(b);
    host.appendChild(wrap);
    return;
  }
  if (!VIEW.projects.length) {
    const p = document.createElement("p");
    p.className = "empty";
    p.textContent = VIEW.last_refresh_ok === false
      ? "Offline — no cached projects yet."
      : "No active projects in the commit window.";
    host.appendChild(p);
    return;
  }
  for (const proj of VIEW.projects) host.appendChild(projectRow(proj));
}

function projectRow(proj) {
  const row = document.createElement("div");
  row.className = "prow" + (proj.name === SELECTED ? " sel" : "");
  row.addEventListener("click", () => selectProject(proj.name));

  const main = document.createElement("div");
  main.className = "prow-main";

  const name = document.createElement("div");
  name.className = "prow-name";
  name.textContent = proj.display || proj.name;
  if (proj.grouping_source !== "agent") {
    name.classList.add("dim");
    name.title = "Auto-grouped by name — the assistant will confirm the grouping and name";
  }
  main.appendChild(name);

  const meta = document.createElement("div");
  meta.className = "prow-meta";
  meta.appendChild(chipMini(proj));
  if (proj.review && proj.review.length) {
    const rv = document.createElement("span");
    rv.className = "prow-review";
    rv.textContent = `⚑ ${proj.review.length}`;
    rv.title = `${proj.review.length} item${proj.review.length === 1 ? "" : "s"} to review`;
    meta.appendChild(rv);
  }
  main.appendChild(meta);

  if (proj.launch) main.appendChild(launchLine(proj.launch));

  row.appendChild(main);
  row.appendChild(activityGrid(proj.daily_counts)); // 30-day GitHub-style grid
  return row;
}

function chipMini(proj) {
  const c = document.createElement("span");
  c.className = "chip-mini";
  if (proj.commits_today) {
    c.classList.add("active");
    c.textContent = `● ${proj.commits_today} today`;
  } else if (proj.days_idle === 0) {
    c.classList.add("active");
    c.textContent = "● active";
  } else if (proj.days_idle != null) {
    c.classList.add("idle");
    c.textContent = `${proj.days_idle}d ago`;
  } else {
    c.classList.add("idle");
    c.textContent = "no data";
  }
  return c;
}

// A compact scheduled-launch line for the sidebar row: rocket + label + a
// countdown that turns amber when the date is within a week / overdue.
function launchLine(l) {
  const wrap = el("div", "prow-launch");
  wrap.appendChild(el("span", "prow-launch-icon", "🚀"));
  const label = l.title || l.type || "Launch";
  wrap.appendChild(el("span", "prow-launch-label", label));

  const d = l.days_until;
  const c = el("span", "prow-launch-when");
  if (d == null) { c.textContent = l.target_date || "no date"; c.classList.add("muted"); }
  else if (d < 0) { c.textContent = `overdue ${Math.abs(d)}d`; c.classList.add("overdue"); }
  else if (d === 0) { c.textContent = "today"; c.classList.add("soon"); }
  else if (d === 1) { c.textContent = "tomorrow"; c.classList.add("soon"); }
  else { c.textContent = `in ${d}d`; if (d <= 7) c.classList.add("soon"); }
  wrap.appendChild(c);

  wrap.title = [l.type, l.title, l.target_date].filter(Boolean).join(" · ");
  return wrap;
}

// ---- 30-day activity grid (GitHub-style, replaces the commit bars) ---------
function dayKey(d) {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const da = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${da}`;
}

// Most recent `days` logical days, oldest first, with counts. A "day" begins at
// day_start_hour local (default 5am), so we anchor at now-minus-that-many-hours
// before enumerating — matching the backend's bucket keys exactly.
function dailySeries(counts, days) {
  const out = [];
  const startHour = (VIEW && VIEW.day_start_hour != null) ? VIEW.day_start_hour : 5;
  const anchor = new Date(Date.now() - startHour * 3600 * 1000);
  for (let i = days - 1; i >= 0; i--) {
    const d = new Date(anchor);
    d.setDate(anchor.getDate() - i);
    const key = dayKey(d);
    out.push({ key, count: counts[key] || 0 });
  }
  return out;
}

function level(v) {
  if (!v) return 0;
  if (v <= 1) return 1;
  if (v <= 3) return 2;
  if (v <= 6) return 3;
  return 4;
}

// A fixed 30-cell grid (6 columns × 5 rows), oldest → newest. `big` for the
// larger version in the detail panel header.
function activityGrid(counts, big) {
  const wrap = document.createElement("div");
  wrap.className = "grid" + (big ? " grid-lg" : "");
  for (const day of dailySeries(counts || {}, 30)) {
    const cell = document.createElement("span");
    cell.className = "cell lvl-" + level(day.count);
    cell.title = `${day.key}: ${day.count} commit${day.count === 1 ? "" : "s"}`;
    wrap.appendChild(cell);
  }
  return wrap;
}

// ---- right panel: project report -------------------------------------------
function selectProject(name) {
  SELECTED = name;
  ANALYSES = {};
  const detail = document.getElementById("detail");
  detail.innerHTML =
    '<div class="detail-inner"><div id="detail-report"></div><div id="detail-analysis"></div></div>';
  const proj = currentProject();
  if (proj) {
    renderReport(proj);
    loadAnalyses(proj); // auto — no button
    renderContext(proj);
  }
  renderSidebar(); // move the selected highlight
}

// Build the detail scaffold only if it isn't there — so a tick that re-renders
// the report doesn't blow away an open repo analysis.
function ensureDetailShell() {
  if (document.getElementById("detail-report")) return;
  const detail = document.getElementById("detail");
  detail.innerHTML =
    '<div class="detail-inner"><div id="detail-report"></div><div id="detail-analysis"></div></div>';
}

function renderReport(proj) {
  const host = document.getElementById("detail-report");
  host.textContent = "";

  // Top row: summary on the left, 30-day commit grid on the right.
  const top = document.createElement("div");
  top.className = "rep-top";

  const main = document.createElement("div");
  main.className = "rep-top-main";
  const h = document.createElement("h2");
  h.textContent = proj.display || proj.name;
  h.appendChild(chip(proj));
  if (proj.activity && proj.activity.length) {
    const ab = document.createElement("button");
    ab.className = "btn ghost small activity-btn";
    ab.textContent = `Recent activity (${proj.activity.length})`;
    ab.title = "Show recent activity from the working Claudes";
    ab.addEventListener("click", () => openActivityModal(proj));
    h.appendChild(ab);
  }
  main.appendChild(h);
  const sum = document.createElement("p");
  sum.className = "rep-summary";
  sum.textContent = proj.summary
    || (VIEW.agent_has_run ? "No summary yet." : "Run /overboard to have your assistant write summaries.");
  main.appendChild(sum);
  top.appendChild(main);

  const total = Object.values(proj.daily_counts || {}).reduce((a, b) => a + b, 0);
  const gcol = document.createElement("div");
  gcol.className = "rep-top-grid";
  gcol.appendChild(activityGrid(proj.daily_counts, true));
  const gl = document.createElement("span");
  gl.className = "subtle";
  gl.textContent = `${total} commit${total === 1 ? "" : "s"} · 30 days`;
  gcol.appendChild(gl);
  top.appendChild(gcol);

  host.appendChild(top);

  // Assistant report: narrative + review flags.
  if (proj.pm_narrative || (proj.review && proj.review.length)) {
    host.appendChild(sectionTitle("Assistant report"));
    if (proj.pm_narrative) {
      const n = document.createElement("p");
      n.className = "rep-narr";
      n.textContent = proj.pm_narrative;
      host.appendChild(n);
    }
    if (proj.review && proj.review.length) {
      const rh = document.createElement("div");
      rh.className = "rep-review-head";
      rh.textContent = `⚑ ${proj.review.length} to review`;
      host.appendChild(rh);
      const ul = document.createElement("ul");
      ul.className = "rep-review";
      for (const r of proj.review) {
        const li = document.createElement("li");
        const txt = document.createElement("span");
        txt.textContent = r;
        li.appendChild(txt);
        const ok = document.createElement("button");
        ok.className = "review-ok";
        ok.textContent = "OK";
        ok.title = "Dismiss — I've seen this";
        ok.addEventListener("click", async () => {
          ok.disabled = true;
          const v = await callView("dismiss_review", { project: proj.name, text: r });
          if (v) { VIEW = v; render(); }
          else { ok.disabled = false; ok.textContent = "retry"; ok.title = "Couldn't dismiss — restart the dashboard server"; }
        });
        li.appendChild(ok);
        ul.appendChild(li);
      }
      host.appendChild(ul);
    }
  }

  // Recent work — the assistant's per-sprint delta cards, newest first.
  const units = proj.work_reviews || [];
  if (units.length) {
    if (!WORK_OPEN[proj.name]) WORK_OPEN[proj.name] = new Set([units[0].id]);
    host.appendChild(sectionTitle("Recent work"));
    for (const u of units) host.appendChild(workCard(proj, u));
  }

  // Repositories — analysis for local ones is auto-loaded below.
  host.appendChild(sectionTitle("Repositories"));
  const repos = document.createElement("div");
  repos.className = "repos";
  for (const r of proj.repos) repos.appendChild(repoBadge(r));
  host.appendChild(repos);
}

// ---- recent-work cards (assistant-owned delta layer) -----------------------
function workCard(proj, u) {
  const open = WORK_OPEN[proj.name];
  const card = el("div", "work-card" + (u.source === "messages" ? " msg-src" : ""));

  const head = el("div", "work-head");
  head.addEventListener("click", () => {
    if (open.has(u.id)) open.delete(u.id);
    else open.add(u.id);
    render();
  });
  head.appendChild(el("span", "work-caret", open.has(u.id) ? "▾" : "▸"));
  head.appendChild(el("span", "work-title", u.title || "recent work"));

  const bits = [];
  if ((u.repos || []).length) bits.push(u.repos.join(", "));
  const per = u.period || {};
  if (per.from || per.to) bits.push(per.from === per.to ? per.from : `${per.from || "…"} – ${per.to || "…"}`);
  if ((u.commits || []).length) bits.push(`${u.commits.length} commit${u.commits.length === 1 ? "" : "s"}`);
  head.appendChild(el("span", "work-meta subtle", bits.join(" · ")));

  if (u.source === "messages") {
    const b = el("span", "work-src-msg", "messages");
    b.title = "Built from commit messages only — no local clone to diff";
    head.appendChild(b);
  }

  const hide = el("button", "work-hide", "✕");
  hide.title = "Hide this card — I've reviewed it";
  hide.addEventListener("click", async (e) => {
    e.stopPropagation();
    hide.disabled = true;
    const v = await callView("hide_work_review", { project: proj.name, id: u.id });
    if (v) { VIEW = v; render(); }
    else { hide.disabled = false; hide.title = "Couldn't hide — restart the dashboard server"; }
  });
  head.appendChild(hide);
  card.appendChild(head);

  if (!open.has(u.id)) return card;

  const body = el("div", "work-body");
  if (u.summary) body.appendChild(el("p", "work-summary", u.summary));

  if ((u.decisions || []).length) {
    body.appendChild(sectionTitle("Decisions & assumptions"));
    const ul = el("ul", "work-decisions");
    for (const d of u.decisions) {
      const li = document.createElement("li");
      li.appendChild(el("span", "", d.text || ""));
      if (d.file) li.appendChild(el("span", "loc", d.line ? `${d.file}:${d.line}` : d.file));
      ul.appendChild(li);
    }
    body.appendChild(ul);
  }

  if ((u.surface || []).length) {
    body.appendChild(sectionTitle("Now exists"));
    const byKind = {};
    for (const s of u.surface) {
      const k = s.kind || "other";
      (byKind[k] = byKind[k] || []).push(s);
    }
    for (const [kind, items] of Object.entries(byKind)) {
      const row = el("div", "work-surface");
      row.appendChild(el("span", "work-kind", kind));
      const kv = el("div", "kv");
      for (const s of items) {
        const p = pill(s.name + (s.detail ? " — " + s.detail : ""));
        if (s.file) p.title = s.line ? `${s.file}:${s.line}` : s.file;
        kv.appendChild(p);
      }
      row.appendChild(kv);
      body.appendChild(row);
    }
  }

  if ((u.snippets || []).length) {
    body.appendChild(sectionTitle("Key new code"));
    for (const s of u.snippets) body.appendChild(snippetEl(s));
  }

  if (u.mermaid) {
    body.appendChild(sectionTitle("Flow"));
    renderWorkMermaid(body, u.id, u.mermaid);
  }
  card.appendChild(body);
  return card;
}

// Like renderMermaid, but caches the rendered SVG per unit id — renderReport
// re-runs on every tick and a fresh async render each time made diagrams flicker.
async function renderWorkMermaid(host, unitId, code) {
  const wrap = el("div", "mermaid-out");
  host.appendChild(wrap);
  if (WORK_MMD[unitId]) { wrap.appendChild(diagramViewer(WORK_MMD[unitId])); return; }
  if (!code || !window.mermaid) { wrap.remove(); return; }
  try {
    const id = "mmd-" + Math.random().toString(36).slice(2);
    const { svg } = await mermaid.render(id, code);
    WORK_MMD[unitId] = svg;
    wrap.appendChild(diagramViewer(svg));
  } catch (e) {
    wrap.className = "subtle";
    wrap.textContent = "diagram unavailable: " + (e && e.message ? e.message : e);
  }
}

// The activity modal is project-scoped, so labels drop the repo suffix and
// render tool events cleanly (a bare Bash command split on "/" was garbled).
function eventLabel(e) {
  const base = (p) => (p ? String(p).split("/").pop() : "");
  switch (e.type) {
    case "Stop": return "finished a task";
    case "SubagentStop": return `${e.agent_type || "subagent"} finished`;
    case "PostToolUse": {
      const tool = e.tool_name || "edit";
      if (tool === "Bash") {
        const cmd = (e.target || "").split("\n")[0].trim();
        return cmd ? "$ " + (cmd.length > 64 ? cmd.slice(0, 64) + "…" : cmd) : "ran a command";
      }
      const f = base(e.target);
      return f ? `${tool.toLowerCase()} ${f}` : tool.toLowerCase();
    }
    case "SessionStart": return "session started";
    case "SessionEnd": return "session ended";
    case "flag": return e.note ? `⚑ ${e.note.slice(0, 80)}` : "flagged for review";
    case "status": return e.note ? e.note.slice(0, 80) : "status";
    default: return e.type;
  }
}

// ---- recent-activity popup -------------------------------------------------
function openActivityModal(proj) {
  closeActivityModal();
  const ov = document.createElement("div");
  ov.className = "modal-overlay";
  ov.id = "activity-modal";
  ov.addEventListener("click", (e) => { if (e.target === ov) closeActivityModal(); });

  const box = document.createElement("div");
  box.className = "modal";

  const head = document.createElement("div");
  head.className = "modal-head";
  const h = document.createElement("h3");
  h.textContent = `Recent activity · ${proj.display || proj.name}`;
  const close = document.createElement("button");
  close.className = "btn ghost small";
  close.textContent = "Close";
  close.addEventListener("click", closeActivityModal);
  head.appendChild(h);
  head.appendChild(close);
  box.appendChild(head);

  const acts = proj.activity || [];
  if (!acts.length) {
    box.appendChild(note("No recent activity."));
  } else {
    const ul = document.createElement("ul");
    ul.className = "feed modal-feed";
    for (const ev of acts.slice(0, 40)) {
      const li = document.createElement("li");
      const l = document.createElement("span");
      l.textContent = eventLabel(ev);
      l.title = ev.last_message || ev.target || "";
      const t = document.createElement("span");
      t.className = "feed-when subtle";
      t.textContent = ago(ev.ts);
      li.appendChild(l);
      li.appendChild(t);
      ul.appendChild(li);
    }
    box.appendChild(ul);
  }

  ov.appendChild(box);
  document.body.appendChild(ov);
  document.addEventListener("keydown", _escClose);
}

function closeActivityModal() {
  const m = document.getElementById("activity-modal");
  if (m) m.remove();
  document.removeEventListener("keydown", _escClose);
}

function _escClose(e) {
  if (e.key === "Escape") closeActivityModal();
}

// ---- settings (sources & tokens) -------------------------------------------
async function openSettings() {
  const s = await call("get_settings");
  if (!s || !s.bitbucket) return;
  renderSettingsModal(s);
}

function renderSettingsModal(s) {
  closeSettings();
  const ov = document.createElement("div");
  ov.className = "modal-overlay";
  ov.id = "settings-modal";
  ov.addEventListener("click", (e) => { if (e.target === ov) closeSettings(); });

  const box = document.createElement("div");
  box.className = "modal";
  box.innerHTML =
    '<div class="modal-head"><h3>Settings — sources</h3>' +
    '<button class="btn ghost small" data-close>Close</button></div>' +
    '<div class="settings-body">' +
      '<fieldset class="src">' +
        '<legend><label><input type="checkbox" id="bb-enabled"> Bitbucket</label></legend>' +
        '<label>Workspace <input type="text" id="bb-workspace" placeholder="e.g. mrieck81"></label>' +
        '<label>Email <input type="text" id="bb-email" placeholder="you@example.com"></label>' +
        '<label>API token <input type="password" id="bb-token"></label>' +
      '</fieldset>' +
      '<fieldset class="src">' +
        '<legend><label><input type="checkbox" id="gh-enabled"> GitHub</label></legend>' +
        '<label>Personal access token <input type="password" id="gh-token"></label>' +
        '<p class="subtle hint">Classic PAT with <code>repo</code> scope (or fine-grained: repository contents + metadata, read). Pulls the repos you own.</p>' +
      '</fieldset>' +
      '<fieldset class="src">' +
        '<legend>Local folders</legend>' +
        '<label>Where your clones live <input type="text" id="local-roots" placeholder="~/Sites, ~/dev/work"></label>' +
        '<p class="subtle hint">Comma-separated. Common folders (~/Sites, ~/projects, ~/code, …) are searched automatically — add any others here so local analysis finds your repos.</p>' +
      '</fieldset>' +
      '<div class="settings-actions"><span id="settings-status" class="subtle"></span>' +
      '<button class="btn" data-save>Save &amp; refresh</button></div>' +
    '</div>';
  ov.appendChild(box);
  document.body.appendChild(ov);

  box.querySelector("#bb-enabled").checked = s.bitbucket.enabled;
  box.querySelector("#bb-workspace").value = s.bitbucket.workspace || "";
  box.querySelector("#bb-email").value = s.bitbucket.email || "";
  box.querySelector("#bb-token").placeholder = s.bitbucket.token_set ? "•••• saved — blank keeps it" : "paste token";
  box.querySelector("#gh-enabled").checked = s.github.enabled;
  box.querySelector("#gh-token").placeholder = s.github.token_set ? "•••• saved — blank keeps it" : "paste token";
  box.querySelector("#local-roots").value = (s.local_roots || []).join(", ");

  box.querySelector("[data-close]").addEventListener("click", closeSettings);
  box.querySelector("[data-save]").addEventListener("click", () => saveSettings(box));
  document.addEventListener("keydown", _settingsEsc);
}

function _settingsEsc(e) { if (e.key === "Escape") closeSettings(); }
function closeSettings() {
  const m = document.getElementById("settings-modal");
  if (m) m.remove();
  document.removeEventListener("keydown", _settingsEsc);
}

async function saveSettings(box) {
  const status = box.querySelector("#settings-status");
  const saveBtn = box.querySelector("[data-save]");
  saveBtn.disabled = true;
  status.textContent = "Saving & refreshing…";
  const payload = {
    bitbucket: {
      enabled: box.querySelector("#bb-enabled").checked,
      workspace: box.querySelector("#bb-workspace").value.trim(),
      email: box.querySelector("#bb-email").value.trim(),
      token: box.querySelector("#bb-token").value,
    },
    github: {
      enabled: box.querySelector("#gh-enabled").checked,
      token: box.querySelector("#gh-token").value,
    },
    local_roots: box.querySelector("#local-roots").value.split(",").map((r) => r.trim()).filter(Boolean),
  };
  const v = await callView("save_settings", payload);
  if (v) {
    VIEW = v;
    closeSettings();
    render();
    const proj = currentProject();
    if (proj) loadAnalyses(proj);
  } else {
    status.textContent = "Save failed — restart the dashboard server and retry.";
    saveBtn.disabled = false;
  }
}

function ago(ts) {
  if (!ts) return "";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

function chip(proj) {
  const c = document.createElement("span");
  if (proj.commits_today) {
    c.className = "chip active";
    c.textContent = `● ${proj.commits_today} commit${proj.commits_today === 1 ? "" : "s"} today`;
  } else if (proj.days_idle === 0) {
    c.className = "chip active";
    c.textContent = "● active today";
  } else if (proj.days_idle != null) {
    c.className = "chip idle";
    c.textContent = `${proj.days_idle} day${proj.days_idle === 1 ? "" : "s"} ago`;
  } else {
    c.className = "chip idle";
    c.textContent = "no data";
  }
  return c;
}

function repoBadge(r) {
  const b = document.createElement("span");
  b.className = "badge" + (r.local_path ? " local" : "") + (r.fetch_error ? " err" : "");

  if (r.provider) {
    const tag = document.createElement("span");
    tag.className = "prov prov-" + r.provider;
    tag.textContent = r.provider === "github" ? "gh" : "bb";
    tag.title = r.provider;
    b.appendChild(tag);
  }
  for (const ap of (r.also_providers || [])) {
    const tag = document.createElement("span");
    tag.className = "prov prov-" + ap;
    tag.textContent = ap === "github" ? "gh" : "bb";
    tag.title = "also on " + ap;
    b.appendChild(tag);
  }

  const slug = document.createElement("span");
  slug.className = "slug";
  slug.textContent = r.slug;
  b.appendChild(slug);

  if (r.fetch_error) {
    const loc = document.createElement("span");
    loc.className = "loc";
    loc.textContent = "⚠";
    loc.title = r.fetch_error;
    b.appendChild(loc);
  }

  if (r.local_path) {
    const loc = document.createElement("span");
    loc.className = "loc";
    loc.textContent = "local";
    loc.title = r.local_path;
    b.appendChild(loc);

    const s = syncIcon(r.sync);
    if (s) b.appendChild(s);

    const term = document.createElement("span");
    term.className = "loc link";
    term.textContent = "terminal ↗";
    term.title = "Open a terminal in " + r.local_path;
    term.addEventListener("click", (e) => { e.stopPropagation(); call("open_terminal", { path: r.local_path }); });
    b.appendChild(term);
  } else {
    const loc = document.createElement("span");
    loc.className = "loc";
    loc.textContent = "remote";
    b.appendChild(loc);
  }
  return b;
}

// Green ✓ = clone matches origin on its current branch; yellow ⚠ = local work
// not pushed (commits and/or tracked edits — untracked ignored); red ⚠ = origin
// has commits not pulled. Checked in the background on startup and Refresh.
function syncIcon(sync) {
  if (!sync || sync.status === "unknown") return null; // stay quiet when we can't tell
  const s = document.createElement("span");
  s.className = "sync " + sync.status;
  s.textContent = sync.status === "ok" ? "✓" : "⚠";
  const when = sync.checked_at ? " (checked " + fmtTime(sync.checked_at) + ")" : "";
  const where = sync.branch ? "origin/" + sync.branch : "origin";
  if (sync.status === "ok") s.title = "in sync with " + where + when;
  else if (sync.status === "ahead") s.title = "not pushed to " + where + ": " + sync.detail + when;
  else s.title = where + " has work not pulled: " + sync.detail + when;
  return s;
}

// ---- repo analysis (auto-loaded, always shown below the report) ------------
const AN_TABS = [
  ["overview", "Overview"],
  ["prompts", "Prompts"],
  ["setup", "Run & Deploy"],
  ["snippets", "Snippets"],
  ["db", "Data shape"],
];

// Load static analysis for every local repo of the project (no button). The
// backend caches by clone HEAD and pre-warms in the background, so this is
// usually instant and re-runs itself when new commits move HEAD.
async function loadAnalyses(proj) {
  const locals = proj.repos.filter((r) => r.local_path);
  ANALYSES = {};
  for (const r of locals) ANALYSES[r.slug] = { tab: "overview", data: null, error: null, loading: true };
  renderAnalyses();
  console.log(`[overboard] analyses: requesting static analysis for ${locals.length} local repo(s) of ${proj.name}`);
  await Promise.all(locals.map(async (r) => {
    try {
      const t0 = performance.now();
      const data = await call("analyze", { slug: r.slug });
      console.log(`[overboard] analyses: ${r.slug} → ${(performance.now() - t0).toFixed(0)} ms`);
      if (SELECTED !== proj.name || !ANALYSES[r.slug]) return; // switched away
      if (data && data.error) ANALYSES[r.slug] = { ...ANALYSES[r.slug], error: data.error, loading: false };
      else ANALYSES[r.slug] = { ...ANALYSES[r.slug], data, loading: false };
    } catch (e) {
      if (ANALYSES[r.slug]) ANALYSES[r.slug] = { ...ANALYSES[r.slug], error: String(e), loading: false };
    }
  }));
  if (SELECTED === proj.name) renderAnalyses();
}

function renderAnalyses() {
  const host = document.getElementById("detail-analysis");
  if (!host) return;
  host.textContent = "";
  const slugs = Object.keys(ANALYSES);
  if (!slugs.length) return; // no local clones — nothing to analyze
  host.appendChild(sectionTitle("Analysis"));
  for (const slug of slugs) host.appendChild(analysisBlock(slug));
}

function analysisBlock(slug) {
  const a = ANALYSES[slug];
  const box = document.createElement("div");
  box.className = "analysis";

  const head = document.createElement("div");
  head.className = "an-head";
  const h = document.createElement("h3");
  h.textContent = slug;
  head.appendChild(h);
  box.appendChild(head);

  if (a.loading) {
    const s = document.createElement("div");
    s.className = "spinner";
    s.textContent = "Analyzing local clone…";
    box.appendChild(s);
    return box;
  }
  if (a.error || !a.data) {
    const s = document.createElement("div");
    s.className = "spinner";
    s.textContent = a.error || "analysis unavailable";
    box.appendChild(s);
    return box;
  }

  const d = a.data;
  const nav = document.createElement("nav");
  nav.className = "tabs";
  for (const [key, label] of AN_TABS) {
    const b = document.createElement("button");
    let n = "";
    if (key === "prompts") n = ` (${d.prompts.length})`;
    if (key === "snippets") n = ` (${(d.snippets || []).length})`;
    if (key === "db") n = ` (${d.db.length})`;
    b.textContent = label + n;
    b.className = a.tab === key ? "on" : "";
    b.addEventListener("click", () => { a.tab = key; renderAnalyses(); });
    nav.appendChild(b);
  }
  box.appendChild(nav);

  const body = document.createElement("div");
  body.className = "tab-body";
  if (a.tab === "overview") body.appendChild(overviewTab(d));
  else if (a.tab === "prompts") body.appendChild(promptsTab(d));
  else if (a.tab === "setup") body.appendChild(setupTab(d));
  else if (a.tab === "snippets") body.appendChild(snippetsTab(d));
  else if (a.tab === "db") body.appendChild(dbTab(d));
  box.appendChild(body);
  return box;
}

function overviewTab(d) {
  const frag = document.createElement("div");
  const arch = document.createElement("p");
  arch.className = "arch";
  arch.textContent = d.architecture
    || (VIEW.agent_has_run ? "No architecture write-up yet." : "Run /overboard to have your assistant describe the architecture.");
  frag.appendChild(arch);

  const archDiagram = d.diagrams && d.diagrams.architecture;
  if (archDiagram) {
    frag.appendChild(sectionTitle("Architecture"));
    renderMermaid(frag, archDiagram);
  }

  frag.appendChild(sectionTitle(`Structure · ${d.stats.files} files`));
  const struct = document.createElement("div");
  struct.className = "kv";
  for (const s of d.structure) struct.appendChild(pill(`${s.dir}: ${s.files}`));
  frag.appendChild(struct);

  frag.appendChild(sectionTitle("File types"));
  const exts = document.createElement("div");
  exts.className = "kv";
  for (const [ext, n] of Object.entries(d.stats.by_ext)) exts.appendChild(pill(`${ext} ${n}`));
  frag.appendChild(exts);
  return frag;
}

function promptsTab(d) {
  const frag = document.createElement("div");
  const isStatic = d.prompts_source !== "agent";
  if (!d.prompts.length) {
    frag.appendChild(note(VIEW.agent_has_run
      ? "No LLM prompts found in this repo."
      : "Run /overboard — your assistant reads the code and extracts the real prompts. (Noisy keyword guesses show here until then.)"));
    return frag;
  }
  const banner = document.createElement("p");
  banner.className = "subtle prompts-src";
  banner.textContent = isStatic
    ? "⚠ Static keyword guesses — often noisy. Run /overboard for the real prompts."
    : "Extracted by your assistant from the actual code.";
  frag.appendChild(banner);

  for (const f of d.prompts) {
    const el = document.createElement("div");
    el.className = "finding" + (isStatic ? " dim" : "");
    const meta = document.createElement("div");
    meta.className = "meta";

    const kind = document.createElement("span");
    kind.className = "kind";
    kind.textContent = f.name || f.kind || "prompt";
    meta.appendChild(kind);

    if (f.dynamic) {
      const dyn = document.createElement("span");
      dyn.className = "kind";
      dyn.style.color = "var(--amber)";
      dyn.textContent = "built dynamically";
      meta.appendChild(dyn);
    }

    if (f.file) {
      const loc = document.createElement("span");
      loc.className = "loc";
      loc.textContent = f.line ? `${f.file}:${f.line}` : f.file;
      meta.appendChild(loc);
    }

    const code = document.createElement("code");
    code.textContent = f.text;
    el.appendChild(meta);
    el.appendChild(code);
    if (f.note) {
      const nt = document.createElement("div");
      nt.className = "finding-note subtle";
      nt.textContent = f.note;
      el.appendChild(nt);
    }
    frag.appendChild(el);
  }
  return frag;
}

function setupTab(d) {
  const frag = document.createElement("div");
  if (!d.setup) {
    frag.appendChild(note(VIEW.agent_has_run
      ? "No run/deploy steps yet."
      : "Run /overboard — your assistant will write the steps to run, deploy, or try this project (the human actions, not local-dev setup)."));
    return frag;
  }
  const pre = document.createElement("pre");
  pre.className = "setup-text";
  pre.textContent = d.setup;
  frag.appendChild(pre);
  return frag;
}

function snippetsTab(d) {
  const frag = document.createElement("div");
  const items = d.snippets || [];
  if (!items.length) {
    frag.appendChild(note(VIEW.agent_has_run
      ? "No key snippets picked out yet."
      : "Run /overboard — your assistant will surface a few key code snippets."));
    return frag;
  }
  for (const s of items) frag.appendChild(snippetEl(s));
  return frag;
}

// Map a file path's extension to a highlight.js language name (best-effort).
const HL_EXT = {
  py: "python", js: "javascript", mjs: "javascript", cjs: "javascript",
  ts: "typescript", jsx: "javascript", tsx: "typescript",
  go: "go", rs: "rust", java: "java", cs: "csharp", rb: "ruby", php: "php",
  swift: "swift", m: "objectivec", mm: "objectivec", h: "objectivec",
  kt: "kotlin", kts: "kotlin", dart: "dart", scala: "scala",
  ex: "elixir", exs: "elixir", sh: "bash", bash: "bash", zsh: "bash",
  sql: "sql", json: "json", yml: "yaml", yaml: "yaml", toml: "ini", ini: "ini",
  html: "xml", xml: "xml", css: "css", scss: "scss", less: "less",
  c: "c", cpp: "cpp", cc: "cpp", cxx: "cpp", hpp: "cpp",
  lua: "lua", pl: "perl", r: "r", md: "markdown", diff: "diff",
};

function langFromFile(file) {
  if (!file) return "";
  const base = String(file).split(/[?#]/)[0].replace(/:\d+$/, "").split("/").pop().toLowerCase();
  if (base === "makefile") return "makefile";
  const ext = base.includes(".") ? base.split(".").pop() : "";
  return HL_EXT[ext] || "";
}

// Fill a <code> element with syntax-highlighted text — highlight.js is vendored
// offline (index.html). Falls back to plain text if hljs is missing or errors.
function highlightInto(codeEl, text, file) {
  codeEl.textContent = text || "";
  if (!window.hljs) return;
  try {
    const lang = langFromFile(file);
    if (lang && hljs.getLanguage(lang)) codeEl.className = ("language-" + lang + " " + codeEl.className).trim();
    hljs.highlightElement(codeEl);
  } catch (e) { /* leave plain on any hljs error */ }
}

// One code-snippet block ({title, file, line, code, note}) — shared by the
// Snippets analysis tab and the recent-work cards.
function snippetEl(s) {
  const box = document.createElement("div");
  box.className = "finding";
  const meta = document.createElement("div");
  meta.className = "meta";

  const kind = document.createElement("span");
  kind.className = "kind";
  kind.textContent = s.title || "snippet";
  meta.appendChild(kind);

  if (s.file) {
    const loc = document.createElement("span");
    loc.className = "loc";
    loc.textContent = s.line ? `${s.file}:${s.line}` : s.file;
    meta.appendChild(loc);
  }
  box.appendChild(meta);

  const code = document.createElement("code");
  highlightInto(code, s.code || "", s.file);
  box.appendChild(code);
  if (s.note) {
    const nt = document.createElement("div");
    nt.className = "finding-note subtle";
    nt.textContent = s.note;
    box.appendChild(nt);
  }
  return box;
}

function dbTab(d) {
  const frag = document.createElement("div");
  const isStatic = d.data_shape_source !== "agent";
  if (!d.db.length) {
    frag.appendChild(note(VIEW.agent_has_run
      ? "No data model found in this repo."
      : "Run /overboard — your assistant reads the code and extracts the data model for any stack (SQL, Mongo, JPA, EF, ORMs). The static scan only knows SQL/Prisma/Django, so it may miss yours."));
    return frag;
  }
  const banner = document.createElement("p");
  banner.className = "subtle prompts-src";
  banner.textContent = isStatic
    ? "⚠ Static scan — only detects SQL/Prisma/Django. Run /overboard for the real data model (any stack)."
    : "Extracted by your assistant from the actual code.";
  frag.appendChild(banner);
  const er = d.diagrams && d.diagrams.er;
  if (er) {
    frag.appendChild(sectionTitle("Entity relationships"));
    renderMermaid(frag, er);
    frag.appendChild(sectionTitle("Tables"));
  }
  for (const t of d.db) {
    const el = document.createElement("div");
    el.className = "table-card" + (isStatic ? " dim" : "");
    const h = document.createElement("h4");
    h.textContent = t.name;
    const src = document.createElement("span");
    src.className = "subtle";
    src.textContent = `  ${t.kind} · ${t.source}`;
    h.appendChild(src);
    el.appendChild(h);
    const cols = document.createElement("div");
    cols.className = "kv";
    for (const c of t.columns) cols.appendChild(pill(c));
    el.appendChild(cols);
    frag.appendChild(el);
  }
  return frag;
}

// ---- CTO context: launches + vision (right sidebar) ------------------------
const LAUNCH_TYPES = ["MVP", "Public Launch", "Beta", "Soft Launch", "Milestone", "Feature Release", "Other"];
const LAUNCH_ACTIONS = [
  "Submit to App Store", "Submit to Google Play", "Submit to Chrome Web Store",
  "Publish Website", "Deploy to Production", "Publish npm Package",
  "Open-Source Release", "Ship Update", "Send to Beta Testers", "Other",
];

function renderContext(proj) {
  CONTEXT_FOR = proj.name;
  const host = document.getElementById("context-body");
  host.innerHTML = '<p class="subtle">Loading…</p>';
  call("get_context", { project: proj.name }).then((ctx) => {
    if (CONTEXT_FOR !== proj.name) return;
    buildContext(host, proj.name, ctx || {});
  });
}

function buildContext(host, project, ctx) {
  host.textContent = "";
  host.appendChild(el("h3", "ctx-title", "Launch / milestone"));
  if (ctx.active_launch) host.appendChild(launchCard(project, ctx));
  else host.appendChild(launchForm(project, null));
  if ((ctx.past_launches || []).length) host.appendChild(historyBlock(ctx.past_launches));
  host.appendChild(el("h3", "ctx-title", "Vision / direction"));
  host.appendChild(visionBlock(project, ctx.vision || ""));
}

// A <select> of `options` plus a text input that appears only when "Other" is
// chosen; get() returns the effective value.
function selectOther(labelText, options, selected) {
  const wrap = el("label", "ctx-field", labelText);
  const sel = document.createElement("select");
  for (const o of options) {
    const opt = document.createElement("option");
    opt.value = o; opt.textContent = o;
    sel.appendChild(opt);
  }
  const custom = document.createElement("input");
  custom.type = "text"; custom.placeholder = "type it…"; custom.className = "ctx-other";
  if (selected && options.includes(selected)) sel.value = selected;
  else if (selected) { sel.value = "Other"; custom.value = selected; }
  custom.style.display = sel.value === "Other" ? "block" : "none";
  sel.addEventListener("change", () => {
    custom.style.display = sel.value === "Other" ? "block" : "none";
  });
  wrap.appendChild(sel);
  wrap.appendChild(custom);
  return { wrap, get: () => (sel.value === "Other" ? custom.value.trim() : sel.value) };
}

function launchForm(project, existing) {
  const box = el("div", "launch-form");
  const typeF = selectOther("Type", LAUNCH_TYPES, existing ? existing.type : "");

  const titleF = el("label", "ctx-field", "Title");
  const titleIn = document.createElement("input");
  titleIn.type = "text"; titleIn.value = existing ? existing.title || "" : "";
  titleF.appendChild(titleIn);

  const actionF = selectOther("Action", LAUNCH_ACTIONS, existing ? existing.action : "");

  const dateF = el("label", "ctx-field", "Target date");
  const dateIn = document.createElement("input");
  dateIn.type = "date"; dateIn.value = existing ? existing.target_date || "" : "";
  dateF.appendChild(dateIn);

  const goalsF = el("label", "ctx-field", "Goals for launch");
  const goalsIn = document.createElement("textarea");
  goalsIn.rows = 3; goalsIn.value = existing ? existing.goals || "" : "";
  goalsF.appendChild(goalsIn);

  const actions = el("div", "ctx-actions");
  const status = el("span", "subtle ctx-status", "");
  const save = el("button", "btn small", existing ? "Save changes" : "Add launch");
  actions.appendChild(status);
  if (existing) {
    const cancel = el("button", "btn ghost small", "Cancel");
    cancel.addEventListener("click", () => renderContext(currentProject()));
    actions.appendChild(cancel);
  }
  actions.appendChild(save);

  save.addEventListener("click", async () => {
    save.disabled = true; status.textContent = "Saving…";
    const payload = {
      project, type: typeF.get(), title: titleIn.value.trim(),
      action: actionF.get(), target_date: dateIn.value, goals: goalsIn.value.trim(),
    };
    const ctx = await call(existing ? "update_active_launch" : "set_active_launch", payload);
    if (CONTEXT_FOR === project) buildContext(document.getElementById("context-body"), project, ctx || {});
  });

  box.appendChild(typeF.wrap);
  box.appendChild(titleF);
  box.appendChild(actionF.wrap);
  box.appendChild(dateF);
  box.appendChild(goalsF);
  box.appendChild(actions);
  return box;
}

function launchCard(project, ctx) {
  const a = ctx.active_launch;
  const box = el("div", "launch-card");

  const top = el("div", "launch-top");
  top.appendChild(el("span", "launch-type", a.type || "launch"));
  top.appendChild(countdownBadge(a.days_until));
  box.appendChild(top);

  if (a.title) box.appendChild(el("div", "launch-title", a.title));
  if (a.action) box.appendChild(el("div", "launch-action subtle", a.action));
  box.appendChild(el("div", "launch-date subtle", a.target_date || "no date set"));
  if (a.goals) box.appendChild(el("div", "launch-goals", a.goals));

  if ((a.history || []).length) {
    const h = el("div", "launch-hist subtle");
    h.appendChild(el("div", "hist-head", "Push-backs:"));
    for (const x of a.history) {
      h.appendChild(el("div", "", `• ${x.from || "?"} → ${x.to || "?"}${x.reason ? " — " + x.reason : ""}`));
    }
    box.appendChild(h);
  }

  const actions = el("div", "ctx-actions");
  const edit = el("button", "btn ghost small", "Edit");
  edit.addEventListener("click", () => {
    const body = document.getElementById("context-body");
    body.textContent = "";
    body.appendChild(el("h3", "ctx-title", "Launch / milestone"));
    body.appendChild(launchForm(project, a));
    if ((ctx.past_launches || []).length) body.appendChild(historyBlock(ctx.past_launches));
    body.appendChild(el("h3", "ctx-title", "Vision / direction"));
    body.appendChild(visionBlock(project, ctx.vision || ""));
  });
  const push = el("button", "btn ghost small", "Push back");
  push.addEventListener("click", () => openPushback(project, a));
  const ship = el("button", "btn small", "Mark shipped");
  ship.addEventListener("click", async () => {
    const c = await call("complete_launch", { project, status: "shipped" });
    if (CONTEXT_FOR === project) buildContext(document.getElementById("context-body"), project, c || {});
  });
  actions.appendChild(edit);
  actions.appendChild(push);
  actions.appendChild(ship);
  box.appendChild(actions);
  return box;
}

function openPushback(project, a) {
  const body = document.getElementById("context-body");
  const existing = document.getElementById("pushback-form");
  if (existing) { existing.remove(); return; }
  const form = el("div", "pushback-form");
  form.id = "pushback-form";
  form.appendChild(el("div", "subtle", "Push back to a new date, with a reason:"));
  const dateIn = document.createElement("input");
  dateIn.type = "date"; dateIn.value = a.target_date || "";
  const reason = document.createElement("input");
  reason.type = "text"; reason.placeholder = "reason for the slip";
  const go = el("button", "btn small", "Push back");
  go.addEventListener("click", async () => {
    if (!dateIn.value) return;
    const c = await call("pushback_launch", { project, new_date: dateIn.value, reason: reason.value.trim() });
    if (CONTEXT_FOR === project) buildContext(body, project, c || {});
  });
  form.appendChild(dateIn);
  form.appendChild(reason);
  form.appendChild(go);
  const card = body.querySelector(".launch-card");
  if (card) card.after(form);
  else body.appendChild(form);
}

function historyBlock(past) {
  const wrap = el("details", "launch-history");
  wrap.appendChild(el("summary", "", `History (${past.length})`));
  for (const p of past) {
    const row = el("div", "hist-row");
    row.appendChild(el("span", "hist-status " + (p.status === "cancelled" ? "cancelled" : "shipped"), p.status || "done"));
    const label = `${p.type || ""} ${p.title || ""}`.trim() + (p.target_date ? " · " + p.target_date : "");
    row.appendChild(el("span", "", label));
    wrap.appendChild(row);
  }
  return wrap;
}

function visionBlock(project, vision) {
  const box = el("div", "vision-block");
  const ta = document.createElement("textarea");
  ta.rows = 7; ta.className = "vision-text"; ta.value = vision;
  ta.placeholder = "Upcoming plans, direction, priorities…";
  const actions = el("div", "ctx-actions");
  const status = el("span", "subtle ctx-status", "");
  const save = el("button", "btn small", "Save vision");
  save.addEventListener("click", async () => {
    save.disabled = true; status.textContent = "Saving…";
    await call("save_vision", { project, text: ta.value });
    status.textContent = "saved ✓"; save.disabled = false;
    setTimeout(() => { status.textContent = ""; }, 1500);
  });
  actions.appendChild(status);
  actions.appendChild(save);
  box.appendChild(ta);
  box.appendChild(actions);
  return box;
}

function countdownBadge(days) {
  const b = el("span", "countdown");
  if (days == null) { b.textContent = "no date"; b.classList.add("muted"); return b; }
  if (days < 0) { b.textContent = `overdue ${Math.abs(days)}d`; b.classList.add("overdue"); }
  else if (days === 0) { b.textContent = "today"; b.classList.add("soon"); }
  else if (days === 1) { b.textContent = "tomorrow"; b.classList.add("soon"); }
  else { b.textContent = `in ${days} days`; if (days <= 7) b.classList.add("soon"); }
  return b;
}

// ---- small helpers ---------------------------------------------------------
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}
function sectionTitle(txt) {
  const h = document.createElement("h3");
  h.className = "section-title";
  h.textContent = txt;
  return h;
}
function pill(txt) {
  const s = document.createElement("span");
  s.className = "pill";
  s.textContent = txt;
  return s;
}
function note(txt) {
  const p = document.createElement("p");
  p.className = "subtle";
  p.textContent = txt;
  return p;
}
function fmtTime(iso) {
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

boot();
