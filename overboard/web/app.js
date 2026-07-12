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
}

let _refreshingNow = false;
async function refresh() {
  const btn = document.getElementById("refresh");
  btn.disabled = true;
  btn.textContent = "Refreshing…";
  _refreshingNow = true;
  try {
    const v = await callView("refresh");
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
    c.textContent = `${proj.days_idle}d ago`;
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
  ANALYSES = {};
  const detail = document.getElementById("detail");
  detail.innerHTML =
    '<div class="detail-inner"><div id="detail-report"></div><div id="detail-analysis"></div></div>';
  const proj = currentProject();
  if (proj) {
    renderReport(proj);
    loadAnalyses(proj); // auto — no button
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
  h.textContent = proj.name;
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

  // Repositories — analysis for local ones is auto-loaded below.
  host.appendChild(sectionTitle("Repositories"));
  const repos = document.createElement("div");
  repos.className = "repos";
  for (const r of proj.repos) repos.appendChild(repoBadge(r));
  host.appendChild(repos);
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
  h.textContent = `Recent activity · ${proj.name}`;
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

// ---- repo analysis (auto-loaded, always shown below the report) ------------
const AN_TABS = [
  ["overview", "Overview"],
  ["prompts", "Prompts"],
  ["setup", "Setup & run"],
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
  await Promise.all(locals.map(async (r) => {
    try {
      const data = await call("analyze", { slug: r.slug });
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
      ? "No setup/run instructions yet."
      : "Run /overboard — your assistant will write how to install and run this project."));
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
  for (const s of items) {
    const el = document.createElement("div");
    el.className = "finding";
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
    el.appendChild(meta);

    const code = document.createElement("code");
    code.textContent = s.code || "";
    el.appendChild(code);
    if (s.note) {
      const nt = document.createElement("div");
      nt.className = "finding-note subtle";
      nt.textContent = s.note;
      el.appendChild(nt);
    }
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
