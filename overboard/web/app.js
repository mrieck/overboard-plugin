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
  // Cheap periodic poll for live activity from working Claudes (event-gated
  // server-side, so it only spends tokens when something actually finished).
  setInterval(tick, 20000);
}

let _ticking = false;
async function tick() {
  if (_ticking || _refreshingNow) return;
  _ticking = true;
  try {
    VIEW = await call("tick");
    render(VIEW);
  } catch (_) {
    /* ignore transient poll errors */
  } finally {
    _ticking = false;
  }
}

async function loadView() {
  VIEW = await call("get_view");
  render(VIEW);
}

// ---- top-level controls ----------------------------------------------------
function wireControls() {
  document.getElementById("refresh").addEventListener("click", refresh);
  document.getElementById("rescan").addEventListener("click", rescan);
  document.getElementById("detail-close").addEventListener("click", closeDetail);
  document.getElementById("overlay").addEventListener("click", (e) => {
    if (e.target.id === "overlay") closeDetail();
  });
}

let _refreshingNow = false;
async function refresh() {
  const btn = document.getElementById("refresh");
  btn.disabled = true;
  btn.textContent = "Refreshing…";
  _refreshingNow = true;
  try {
    VIEW = await call("refresh");
    render(VIEW);
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
    render(VIEW);
  } finally {
    btn.disabled = false;
  }
}

// ---- rendering -------------------------------------------------------------
function render(view) {
  renderStatus(view);
  const host = document.getElementById("cards");
  host.textContent = "";
  if (!view.projects.length) {
    const p = document.createElement("p");
    p.className = "empty";
    p.textContent = view.last_refresh_ok === false
      ? "Offline — no cached projects yet."
      : "No active projects in the commit window.";
    host.appendChild(p);
    return;
  }
  for (const proj of view.projects) host.appendChild(card(proj, view.window_days));
}

function renderStatus(view) {
  const el = document.getElementById("status");
  el.classList.toggle("warn", view.last_refresh_ok === false);
  const when = view.last_refresh ? fmtTime(view.last_refresh) : "never";
  el.textContent = view.last_refresh_ok === false
    ? `stale · offline (${when})`
    : `updated ${when} · ${view.machine}`;
  el.title = view.last_error || "";
}

function card(proj, windowDays) {
  const el = document.createElement("div");
  el.className = "card";

  const top = document.createElement("div");
  top.className = "card-top";
  const name = document.createElement("span");
  name.className = "card-name";
  name.textContent = proj.name;
  top.appendChild(name);
  top.appendChild(chip(proj));
  el.appendChild(top);

  const summary = document.createElement("p");
  summary.className = "summary";
  summary.textContent = proj.summary
    || (VIEW.agent_has_run ? "No summary yet." : "Run /overboard to have Claude write summaries.");
  el.appendChild(summary);

  const canvas = document.createElement("canvas");
  canvas.className = "spark";
  el.appendChild(canvas);
  requestAnimationFrame(() => sparkline(canvas, proj.daily_counts, windowDays));

  const repos = document.createElement("div");
  repos.className = "repos";
  for (const r of proj.repos) repos.appendChild(repoBadge(r));
  el.appendChild(repos);

  const pm = activityBlock(proj);
  if (pm) el.appendChild(pm);

  return el;
}

// ---- live activity / project-manager block ---------------------------------
function activityBlock(proj) {
  const hasReview = proj.review && proj.review.length;
  const hasFeed = proj.activity && proj.activity.length;
  if (!hasReview && !hasFeed && !proj.pm_narrative) return null;

  const box = document.createElement("div");
  box.className = "pm";

  if (proj.pm_narrative) {
    const n = document.createElement("div");
    n.className = "pm-narr";
    n.textContent = proj.pm_narrative;
    box.appendChild(n);
  }
  if (hasReview) {
    const head = document.createElement("div");
    head.className = "pm-review-head";
    head.textContent = `⚑ ${proj.review.length} to review`;
    box.appendChild(head);
    const ul = document.createElement("ul");
    ul.className = "pm-review";
    for (const r of proj.review) {
      const li = document.createElement("li");
      li.textContent = r;
      ul.appendChild(li);
    }
    box.appendChild(ul);
  }
  if (hasFeed) {
    const foot = document.createElement("div");
    foot.className = "pm-foot subtle";
    const e = proj.activity[0];
    foot.textContent = `latest: ${eventLabel(e)} · ${ago(e.ts)}`;
    box.appendChild(foot);
  }
  return box;
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
    loc.addEventListener("click", () => call("open_local", { path: r.local_path }));
    b.appendChild(loc);

    const analyze = document.createElement("button");
    analyze.textContent = r.has_analysis ? "details" : "analyze";
    analyze.addEventListener("click", () => openDetail(r.slug));
    b.appendChild(analyze);
  } else {
    const loc = document.createElement("span");
    loc.className = "loc";
    loc.textContent = "remote";
    b.appendChild(loc);
  }
  return b;
}

// ---- commit sparkline (hand-drawn, no chart lib) ---------------------------
function dayKey(d) {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const da = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${da}`;
}

function seriesFor(counts, days) {
  const out = [];
  const today = new Date();
  for (let i = days - 1; i >= 0; i--) {
    const d = new Date(today);
    d.setDate(today.getDate() - i);
    out.push(counts[dayKey(d)] || 0);
  }
  return out;
}

function sparkline(canvas, counts, days) {
  const series = seriesFor(counts, days || 30);
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth || 480;
  const h = canvas.clientHeight || 40;
  canvas.width = w * dpr;
  canvas.height = h * dpr;
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, w, h);

  const max = Math.max(1, ...series);
  const n = series.length;
  const gap = 1;
  const bw = (w - gap * (n - 1)) / n;
  for (let i = 0; i < n; i++) {
    const v = series[i];
    const bh = v === 0 ? 1 : Math.max(2, (v / max) * (h - 2));
    const x = i * (bw + gap);
    const y = h - bh;
    // Most recent day highlighted; empty days faint.
    ctx.fillStyle = i === n - 1 ? "#4da3ff" : (v === 0 ? "#2c313b" : "#3ecf7b");
    ctx.fillRect(x, y, bw, bh);
  }
}

// ---- detail overlay --------------------------------------------------------
let DETAIL = { slug: null, tab: "overview", data: null };

async function openDetail(slug) {
  DETAIL = { slug, tab: "overview", data: null };
  document.getElementById("detail-title").textContent = slug;
  document.getElementById("overlay").classList.remove("hidden");
  document.getElementById("tabs").textContent = "";
  document.getElementById("tab-body").innerHTML =
    '<div class="spinner">Analyzing local clone…</div>';
  const data = await call("analyze", { slug });
  DETAIL.data = data;
  if (data && data.error) {
    document.getElementById("tab-body").innerHTML =
      `<div class="spinner">${escapeHtml(data.error)}</div>`;
    return;
  }
  renderTabs();
}

function closeDetail() {
  document.getElementById("overlay").classList.add("hidden");
}

const TABS = [
  ["overview", "Overview"],
  ["prompts", "Prompts"],
  ["db", "Data shape"],
  ["commits", "Commits"],
];

function renderTabs() {
  const nav = document.getElementById("tabs");
  nav.textContent = "";
  for (const [key, label] of TABS) {
    const b = document.createElement("button");
    let n = "";
    if (key === "prompts") n = ` (${DETAIL.data.prompts.length})`;
    if (key === "db") n = ` (${DETAIL.data.db.length})`;
    b.textContent = label + n;
    b.className = DETAIL.tab === key ? "on" : "";
    b.addEventListener("click", () => { DETAIL.tab = key; renderTabs(); });
    nav.appendChild(b);
  }
  renderTabBody();
}

function renderTabBody() {
  const body = document.getElementById("tab-body");
  const d = DETAIL.data;
  body.textContent = "";
  if (DETAIL.tab === "overview") body.appendChild(overviewTab(d));
  else if (DETAIL.tab === "prompts") body.appendChild(promptsTab(d));
  else if (DETAIL.tab === "db") body.appendChild(dbTab(d));
  else if (DETAIL.tab === "commits") body.appendChild(commitsTab());
}

function overviewTab(d) {
  const frag = document.createElement("div");
  const arch = document.createElement("p");
  arch.className = "arch";
  arch.textContent = d.architecture
    || (VIEW.agent_has_run ? "No architecture write-up yet." : "Run /overboard to have Claude describe the architecture.");
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

function commitsTab() {
  const proj = VIEW.projects.find((p) => p.repos.some((r) => r.slug === DETAIL.slug));
  const frag = document.createElement("div");
  if (!proj) { frag.appendChild(note("No commit data.")); return frag; }
  const total = Object.values(proj.daily_counts).reduce((a, b) => a + b, 0);
  frag.appendChild(sectionTitle(`${total} commits in the last ${VIEW.window_days} days`));
  const canvas = document.createElement("canvas");
  canvas.className = "spark";
  canvas.style.height = "80px";
  frag.appendChild(canvas);
  requestAnimationFrame(() => sparkline(canvas, proj.daily_counts, VIEW.window_days));
  return frag;
}

// ---- small helpers ---------------------------------------------------------
function sectionTitle(txt) {
  const h = document.createElement("h3");
  h.style.cssText = "font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:0.05em;margin:14px 0 6px;";
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
