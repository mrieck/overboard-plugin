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

let VIEW = null;
let SELECTED = null;                                  // selected project name
let ANALYSIS = { slug: null, tab: "overview", data: null }; // open repo analysis

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
    wrap.innerHTML = svg;
  } catch (e) {
    wrap.className = "subtle";
    wrap.textContent = "diagram unavailable: " + (e && e.message ? e.message : e);
  }
}

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
    VIEW = await call("tick");
    render();
  } catch (_) {
    /* ignore transient poll errors */
  } finally {
    _ticking = false;
  }
}

async function loadView() {
  VIEW = await call("get_view");
  render();
}

// ---- top-level controls ----------------------------------------------------
function wireControls() {
  document.getElementById("refresh").addEventListener("click", refresh);
  document.getElementById("rescan").addEventListener("click", rescan);
}

let _refreshingNow = false;
async function refresh() {
  const btn = document.getElementById("refresh");
  btn.disabled = true;
  btn.textContent = "Refreshing…";
  _refreshingNow = true;
  try {
    VIEW = await call("refresh");
    render();
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
    VIEW = await call("rescan_local");
    render();
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
    ANALYSIS = { slug: null, tab: "overview", data: null };
    detail.innerHTML = '<div class="detail-empty">Select a project on the left.</div>';
    return;
  }
  ensureDetailShell();  // keeps any open analysis intact across ticks
  renderReport(proj);
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
  name.textContent = proj.name;
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
    c.textContent = `idle ${proj.days_idle}d`;
  } else {
    c.classList.add("idle");
    c.textContent = "no data";
  }
  return c;
}

// ---- 30-day activity grid (GitHub-style, replaces the commit bars) ---------
function dayKey(d) {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const da = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${da}`;
}

// Most recent `days` days, oldest first, with counts.
function dailySeries(counts, days) {
  const out = [];
  const today = new Date();
  for (let i = days - 1; i >= 0; i--) {
    const d = new Date(today);
    d.setDate(today.getDate() - i);
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
  ANALYSIS = { slug: null, tab: "overview", data: null };
  const detail = document.getElementById("detail");
  detail.innerHTML =
    '<div class="detail-inner"><div id="detail-report"></div><div id="detail-analysis"></div></div>';
  const proj = currentProject();
  if (proj) renderReport(proj);
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

  const head = document.createElement("div");
  head.className = "rep-head";
  const h = document.createElement("h2");
  h.textContent = proj.name;
  head.appendChild(h);
  head.appendChild(chip(proj));
  host.appendChild(head);

  // Large 30-day grid + commit total.
  const total = Object.values(proj.daily_counts || {}).reduce((a, b) => a + b, 0);
  const gwrap = document.createElement("div");
  gwrap.className = "rep-grid";
  gwrap.appendChild(activityGrid(proj.daily_counts, true));
  const gl = document.createElement("span");
  gl.className = "subtle";
  gl.textContent = `${total} commit${total === 1 ? "" : "s"} in the last 30 days`;
  gwrap.appendChild(gl);
  host.appendChild(gwrap);

  // Summary.
  host.appendChild(sectionTitle("Summary"));
  const sum = document.createElement("p");
  sum.className = "rep-summary";
  sum.textContent = proj.summary
    || (VIEW.agent_has_run ? "No summary yet." : "Run /overboard to have your assistant write summaries.");
  host.appendChild(sum);

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
        li.textContent = r;
        ul.appendChild(li);
      }
      host.appendChild(ul);
    }
  }

  // Recent activity from the working Claudes.
  if (proj.activity && proj.activity.length) {
    host.appendChild(sectionTitle("Recent activity"));
    const ul = document.createElement("ul");
    ul.className = "feed";
    for (const e of proj.activity.slice(0, 14)) {
      const li = document.createElement("li");
      const l = document.createElement("span");
      l.textContent = eventLabel(e);
      const t = document.createElement("span");
      t.className = "feed-when subtle";
      t.textContent = ago(e.ts);
      li.appendChild(l);
      li.appendChild(t);
      ul.appendChild(li);
    }
    host.appendChild(ul);
  }

  // Repositories — click a local one to analyze it in-panel.
  host.appendChild(sectionTitle("Repositories"));
  const repos = document.createElement("div");
  repos.className = "repos";
  for (const r of proj.repos) repos.appendChild(repoBadge(r));
  host.appendChild(repos);
}

function eventLabel(e) {
  const base = (p) => (p ? String(p).split("/").pop() : "");
  switch (e.type) {
    case "Stop": return `finished in ${e.repo}`;
    case "SubagentStop": return `${e.agent_type || "subagent"} finished in ${e.repo}`;
    case "PostToolUse": return `${e.tool_name || "edit"} ${base(e.target)} in ${e.repo}`;
    case "SessionStart": return `session started in ${e.repo}`;
    case "SessionEnd": return `session ended in ${e.repo}`;
    case "flag": return `flagged for review in ${e.repo}`;
    case "status": return `${e.note ? e.note.slice(0, 60) : "status"} · ${e.repo}`;
    default: return `${e.type} in ${e.repo}`;
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
    c.textContent = `idle ${proj.days_idle} day${proj.days_idle === 1 ? "" : "s"}`;
  } else {
    c.className = "chip idle";
    c.textContent = "no data";
  }
  return c;
}

function repoBadge(r) {
  const b = document.createElement("span");
  b.className = "badge" + (r.local_path ? " local" : "") + (r.fetch_error ? " err" : "");

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
    loc.className = "loc link";
    loc.textContent = "local";
    loc.title = "Open " + r.local_path;
    loc.addEventListener("click", (e) => { e.stopPropagation(); call("open_local", { path: r.local_path }); });
    b.appendChild(loc);

    const analyze = document.createElement("button");
    analyze.textContent = r.has_analysis ? "details" : "analyze";
    analyze.addEventListener("click", () => openRepoAnalysis(r.slug));
    b.appendChild(analyze);
  } else {
    const loc = document.createElement("span");
    loc.className = "loc";
    loc.textContent = "remote";
    b.appendChild(loc);
  }
  return b;
}

// ---- repo analysis (rendered in-panel, below the report) -------------------
const AN_TABS = [
  ["overview", "Overview"],
  ["prompts", "Prompts"],
  ["db", "Data shape"],
];

async function openRepoAnalysis(slug) {
  ANALYSIS = { slug, tab: "overview", data: null };
  const host = document.getElementById("detail-analysis");
  host.innerHTML =
    `<div class="analysis"><div class="an-head"><h3>${escapeHtml(slug)}</h3></div>` +
    `<div class="spinner">Analyzing local clone…</div></div>`;
  const data = await call("analyze", { slug });
  if (ANALYSIS.slug !== slug) return; // user switched away while we waited
  ANALYSIS.data = data;
  if (data && data.error) {
    host.querySelector(".analysis").innerHTML =
      `<div class="an-head"><h3>${escapeHtml(slug)}</h3></div>` +
      `<div class="spinner">${escapeHtml(data.error)}</div>`;
    return;
  }
  renderAnalysis();
  host.scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderAnalysis() {
  const host = document.getElementById("detail-analysis");
  const d = ANALYSIS.data;
  if (!d) return;

  const box = document.createElement("div");
  box.className = "analysis";

  const head = document.createElement("div");
  head.className = "an-head";
  const h = document.createElement("h3");
  h.textContent = ANALYSIS.slug;
  head.appendChild(h);
  const close = document.createElement("button");
  close.className = "btn ghost small";
  close.textContent = "Close";
  close.addEventListener("click", () => {
    ANALYSIS = { slug: null, tab: "overview", data: null };
    host.textContent = "";
  });
  head.appendChild(close);
  box.appendChild(head);

  const nav = document.createElement("nav");
  nav.className = "tabs";
  for (const [key, label] of AN_TABS) {
    const b = document.createElement("button");
    let n = "";
    if (key === "prompts") n = ` (${d.prompts.length})`;
    if (key === "db") n = ` (${d.db.length})`;
    b.textContent = label + n;
    b.className = ANALYSIS.tab === key ? "on" : "";
    b.addEventListener("click", () => { ANALYSIS.tab = key; renderAnalysis(); });
    nav.appendChild(b);
  }
  box.appendChild(nav);

  const body = document.createElement("div");
  body.className = "tab-body";
  if (ANALYSIS.tab === "overview") body.appendChild(overviewTab(d));
  else if (ANALYSIS.tab === "prompts") body.appendChild(promptsTab(d));
  else if (ANALYSIS.tab === "db") body.appendChild(dbTab(d));
  box.appendChild(body);

  host.textContent = "";
  host.appendChild(box);
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
  if (!d.prompts.length) {
    frag.appendChild(note("No prompts written for LLMs were detected in this repo."));
    return frag;
  }
  for (const f of d.prompts) {
    const el = document.createElement("div");
    el.className = "finding";
    const meta = document.createElement("div");
    meta.className = "meta";

    const kind = document.createElement("span");
    kind.className = "kind";
    kind.textContent = f.name || f.kind;
    meta.appendChild(kind);

    if (f.dynamic) {
      const dyn = document.createElement("span");
      dyn.className = "kind";
      dyn.style.color = "var(--amber)";
      dyn.textContent = "built dynamically";
      meta.appendChild(dyn);
    }

    const loc = document.createElement("span");
    loc.className = "loc";
    loc.textContent = `${f.file}:${f.line}`;
    meta.appendChild(loc);

    const code = document.createElement("code");
    code.textContent = f.text;
    el.appendChild(meta);
    el.appendChild(code);
    frag.appendChild(el);
  }
  return frag;
}

function dbTab(d) {
  const frag = document.createElement("div");
  if (!d.db.length) {
    frag.appendChild(note("No SQL tables or ORM models detected."));
    return frag;
  }
  const er = d.diagrams && d.diagrams.er;
  if (er) {
    frag.appendChild(sectionTitle("Entity relationships"));
    renderMermaid(frag, er);
    frag.appendChild(sectionTitle("Tables"));
  }
  for (const t of d.db) {
    const el = document.createElement("div");
    el.className = "table-card";
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

// ---- small helpers ---------------------------------------------------------
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
