# Overboard — notes for Claude

## What this is (the mental model)

Overboard is a Claude Code plugin that watches every Claude Code session across
all the user's repos and reports what shipped — on the Claude subscription they
already have, no API key. When you work on this codebase or run `/overboard`,
hold this framing — it should shape naming, copy, and behavior:

- **The user runs many projects at once** and doesn't have time to read every
  diff. They want signal: what shipped, what's risky, what needs a decision.
- **The other Claude Code sessions do the work** across the user's repos.
- **The `/overboard` session is the user's assistant.** It watches what the
  other sessions ship (via hooks + Bitbucket commits), keeps the dashboard
  current, and surfaces the few things worth attention. It stays quiet when
  nothing's happening.

Prefer this vocabulary in user-facing copy: "your sessions", "assistant",
"report", "flag for review". Avoid "project manager" / "PM". **Public
positioning (2026-09-01):** lead with the concrete job — scheduled, unattended
Claude Code runs on your subscription, no API key, no cloud — and the morning
report; do not pitch the product as "the CTO's assistant / chief of staff",
"the manager", "the crew", or "a fleet" in marketing copy. Internal identifiers
(`skills/cto-assistant`, prompt text in `commands/` and `agents/`) still use the
older CTO vocabulary; that's fine — it's addressed to Claude, not customers.

## Architecture (two runtimes, one repo)

1. **Plugin** — what Claude Code loads:
   - `hooks/hooks.json` + `hooks/emit_event.py`: observe every session
     (Stop/SubagentStop/PostToolUse/SessionStart/End, all `async`), append to
     `~/.cache/overboard/events.jsonl`. Stdlib-only, never blocks or raises.
   - `.mcp.json` → `overboard/mcp_server.py`: a hand-rolled stdlib JSON-RPC stdio
     MCP server — the assistant's hands (read inputs, write results).
   - `commands/overboard.md`: the `/overboard` command.
   - `commands/dispatch.md`: the `/overboard:dispatch` command — shared by the
     dashboard's Dispatcher (`overboard/dispatch.py`) and the Mac app's
     Telegram dispatcher. Either hands it a request-file path; the session
     resolves the target project via `list_projects`, writes a response JSON
     next to it, and (macOS + app installed only) nudges the app with
     `open -g overboard://dispatch/wake` — the dashboard polls the outbox
     instead. The request also carries `recent_tasks` (the task ledger), so
     the response may be `create_task`, `follow_up` (continue a recent task
     with a revision prompt), `reply` (answer from the ledger) or `reject`. It
     never executes the task itself and never sees chat credentials.
   - `skills/cto-assistant/SKILL.md`: the assistant's full playbook.
2. **Dashboard** — `python3 -m overboard.app`: a stdlib `http.server` you view in
   a browser at `http://localhost:8787`. A panel strip + three panels (see
   below). `OVERBOARD_STATE_DIR=<dir>` points it at another state folder (a
   fixture corpus) without touching `~/.cache/overboard`.

## Key-free by design

**No `ANTHROPIC_API_KEY`.** All AI (summaries, digests, architecture write-ups) is
done by the `/overboard` assistant on the user's Max/Pro subscription, then pushed
in via MCP tools. Python does only deterministic work (Bitbucket commits, static
prompt/DB scanning, Mermaid, local-clone discovery). Never add a runtime call to
an external inference API.

## Portable / stdlib-only

Zero third-party deps — runs on bare `python3` 3.9+ on Linux and macOS (the stock macOS 3.9.6 works; keep new code 3.9-compatible — `from __future__ import annotations` in every module, no `match`, no 3.10+ APIs). `urllib`
(not requests), a hand-rolled `.env` parser (not python-dotenv), a hand-rolled
JSON-RPC server (not the `mcp` package), `http.server` (not pywebview; a native
window is used only if `pywebview` happens to be importable). Keep it this way —
don't reintroduce dependencies. Paths resolve via `${CLAUDE_PLUGIN_ROOT}` and
`python3`, so it's portable across machines with no config.

## No write races (files under `~/.cache/overboard/`)

- `state.json` — **dashboard-owned** (commits, analysis, local links, activity,
  plus the user's dismissals: `dismissed_reviews`, `hidden_work_reviews`, and
  `excluded_repos` — slugs hidden via a repo badge's "hide ✕", filtered out of
  `repo_meta` in `perform_refresh` so every downstream consumer stays clean;
  re-include in Settings). The MCP server never writes it.
  Tracking knobs (`commit_window_days` override, `hide_idle_local` — local
  clones idle past the window are skipped at injection, default on) persist in
  `credentials.json` and are overlaid by `store.load_config`; never write user
  settings into `projects.json` (it lives inside the git checkout).
- `ai.json` — **assistant-owned** (summaries, digests, architecture, and
  `work_reviews` — the per-sprint "recent work" cards). Written only
  via the `set_*`/`record_*` MCP tools; the dashboard reads but never writes it.
- `context.json` — **CTO-owned** (per-project launch/milestone + vision + a
  standing `status` like "Shipped"/"On Hold" that replaces the launch line in the
  sidebar). Written only by the dashboard's `Api` (get_context/set_active_launch/
  update_active_launch/pushback_launch/complete_launch/save_vision/
  set_project_status); the agent **reads** it via the MCP
  `get_project_context` tool (which also returns `status`/`display`) but never
  writes; `get_pending_work` lists every active launch in `launches` so the
  sweep's report can give each one a Direction line. One active launch per project;
  completed ones move to `past_launches`.
- `events.jsonl` — append-only (hooks + flags/status). Safe by construction.
  The dashboard compacts it in the background (`events.maybe_compact`): 30-day
  retention + 5 MB cap, at most once a day. Hooks never trim — they must stay
  append-only and non-blocking; compaction re-appends any tail written during
  the rewrite before the atomic replace.
- `credentials.json` — sources/tokens (see below).
- `scheduler/` (slots.json, runs.json, active.json, queue.json, transcripts/) —
  **dashboard-owned**: written only by the scheduler thread inside the dashboard
  process (`overboard/scheduler.py`, started post-bind in `run_dashboard`); the
  MCP server only *reads* `runs.json` (plus the Mac app's run records) through
  `overboard/runhistory.py` for the `get_recent_runs` tool, and hooks never
  touch it. The scheduler itself only *reads* `events.jsonl`: a run
  claims its SessionStart (by cwd, near launch), then a Stop/SessionEnd for that
  session with no subagent in flight marks it complete (`overboard/runmatch.py`,
  a port of the Mac app's RunMatcher — pure, unit-tested in `tests/`). Distinct
  from the Mac app's scheduler state, which is app-private under
  `~/Library/Application Support`.
- `plugins_popular.json` — **dashboard-owned** 6h cache of the public
  plugmyplugin.com popular-plugins API (`claudeplugins.fetch_popular`).
- `dispatch/` (dispatches.json, inbox/, outbox/, archive/, tasks/<id>/) —
  **dashboard-owned**: the web Dispatcher's ledger + drop-box
  (`overboard/dispatch.py`). The dispatcher *session* reads one request file
  and writes one response file (that's the whole contract); the worker session
  writes `tasks/<id>/result.json` in a folder it's granted via `--add-dir`.
  Wired to the scheduler through two injected seams (`scheduler.on_tick` scans
  the outbox every 5s; `scheduler.on_run_committed` records results) so
  `scheduler.py` never imports `dispatch.py`. Browser-only by design — the
  phone path (Telegram) is the Mac app's paid feature and is pitched, not built.
- Scheduler/dispatcher knobs — `scheduler_concurrency` (1–8),
  `default_timeout_minutes`, `run_directory` (the Overboard run directory: where
  dispatcher sessions run and new task folders may be created) — live in
  `credentials.json` next to the tracking knobs (`store.scheduler_knobs`), saved
  from Settings ▸ Misc.

## Claude plugin management (claudeplugins.py + workspaces.py)

- `overboard/claudeplugins.py` reads Claude Code's own registry files
  (`~/.claude/plugins/installed_plugins.json` v2, `known_marketplaces.json`,
  and the `enabledPlugins` maps in user/project/local settings.json) to build
  the plugin inventory — **read-only**; every mutation goes through the
  `claude plugin ...` CLI as a subprocess on a single-worker job queue (one at
  a time, so concurrent invocations never race Claude's own registry writes).
  Shelling out is fine here — the key-free rule bans external *inference*
  APIs, not subprocesses (git/herdr/terminals already work this way).
- **Task workspaces** live at `~/OverboardWork/<project>/<task>/`
  (`overboard/workspaces.py`). Each has `.claude/settings.json` (project-scope
  `enabledPlugins` — exactly that task's plugins — plus pinned
  `extraKnownMarketplaces`), `CLAUDE.md` (the brief), `work/` (the task's
  persistent archive), and `workspace.json` (the manifest, written last — its
  presence marks the workspace valid; the filesystem is the registry, there is
  no central index). Single writer: the dashboard `Api`. **Scheduled sessions
  write only `work/`** — the generated CLAUDE.md tells them so. Workspace
  creation pre-installs its plugins at project scope via the CLI so the first
  scheduled run never hits a marketplace trust prompt.
- Scheduler slots may carry an optional `workspace_id`: `save_slot` forces the
  slot's cwd to the workspace path and back-links `slot_id` into
  `workspace.json`; `_begin_run` re-resolves the workspace at fire time (a
  deleted workspace fails the run loudly) and refuses to run inside
  `~/.claude/plugins` (mirrors the Mac app's PluginCacheGuard).
- Schedule kinds (`overboard/schedule.py`, byte-compatible with the Mac app's
  ScheduleSpec): `daily`, `weekly`, `everyHours` (+ window) and `once`
  (`{"kind":"once","date":"<ISO UTC Z>"}`; `parse_iso_any` is the one place
  that reads ISO dates — py3.9's `fromisoformat` can't take a `Z`). A once slot
  disables itself when it fires. The engine (`overboard/scheduler.py`) also:
  admits up to `scheduler_concurrency` runs (one launch at a time, one run per
  slot), answers the folder-trust dialog once per run (`runmatch.
  trust_prompt_response` — deliberately the *only* dialog it answers), flags
  `waiting_since`/`stalled_at`, supports **take over** (`taken_over` → no
  auto-/exit, pane left open), history hide/remove/clear, and **ephemeral
  runs** (`run_ephemeral`: `slot_id` = `ephemeral:<uuid>`, never in
  slots.json — the dispatcher's sessions).
- `overboard/promptcomplete.py` (port of the Mac app's Prompt/ folder) backs the
  prompt fields: the slash-command catalog (`~/.claude/commands`,
  `<cwd>/.claude/commands`, every installed plugin's `commands/**.md` +
  `skills/*/SKILL.md`; canonical `plugin:name`, bare name as alias), `~/ ./ /`
  path completion, and the local > project > user enablement check. Pure over
  injected listers; the catalog scan is cached 30s and invalidated by
  `plugin_action`.

`store._atomic_write` uses a **unique** temp file (`tempfile.mkstemp`) per write —
a fixed `.tmp` name raced when the background analyzer and a refresh saved state
concurrently (the `state.json.tmp` error).

`Api._build_view` overlays `ai.json` onto `state.json` at read time.

## The dashboard UI (panel strip + three panels)

`overboard/web/` is vanilla HTML/JS, no build step. `panels.js` owns the
**strip** (a port of the Mac app's PanelStrip: Projects · Scheduler · Plugins,
Settings at the bottom; keys `1`/`2`/`3`, Esc), the hash **router**
(`#/projects/<name>`, `#/scheduler`, `#/scheduler/run/<id>`,
`#/scheduler/slot/<id>`, `#/plugins`, `#/settings/<page>`; legacy `#<name>` still
works) and the top **banners**. Panels are long-lived DOM hosts
(`#panel-projects/-scheduler/-plugins`) toggled with `hidden`; each registers a
poller (`registerPanel`) so only the visible panel polls. `strip_status` (cheap:
scheduler counts + installed-plugin count) keeps the badge/tooltips live.

- **Projects** (`app.js`) is the three-pane board. **Left sidebar**: condensed
  project list — name + a calendar-style **activity grid** (rows = weeks
  Mon→Sun, newest week on top; the current week + 4 full weeks ≈ last 5 weeks,
  `lvl-0..lvl-4` intensity) + active/idle chip + a `⚑ N` review flag. **Center**:
  the selected project's detail — summary on the left, 5-week grid on the right;
  the assistant's report (narrative + review flags), **Recent work** cards (the
  assistant's per-sprint delta layer — newest-first, expandable, hide with ✕;
  `need_review` in `get_pending_work` drives them, the `work-reviewer` subagent
  extracts from real git diffs, `record_work_review` persists,
  `hidden_work_reviews` in state.json remembers hides), recent activity, the
  repositories, and — always shown — each local repo's analysis (Overview /
  Prompts / Data shape). **Right**: the collapsible Direction pane (launches,
  status, vision).
- **Scheduler** (`scheduler.js` + `slot_editor.js` + `run_detail.js` +
  `dispatch.js`) mirrors the Mac app's SchedulerView: left = the **Dispatcher**
  pane over a **10-day agenda** (per-slot `upcoming` from `scheduler_view`);
  middle = **Agents | History** (slot rows with an enable switch; queue &
  active with live state / "waiting for input" / Take over / Stop; history with
  glyphs, hide/unhide/clear); right = the permanent **slot editor** (schedule
  segmented Once · Daily · Weekly · Every N hours — `scheduleEditor()` is also
  what plugins.js' workspace form uses, reading `data-field` attributes, never
  ids) or the picked run's **detail** ("What it reported" = the closing message
  as prose, a facts card, the raw capture behind a disclosure). The poll always
  re-renders the left/middle columns; the right column is rebuilt only when the
  selection (or the picked run's record) changes — that's what keeps typing
  safe, not a "freeze while editing" rule.
- **Plugins** (`plugins.js`): Installed / Task workspaces / Browse popular.
- **Settings** (`app.js` `openSettings(page)`): a modal with the Mac app's four
  pages — General (service + Herdr + claude CLI status), Sources (providers,
  roots, tracking, exclusions), Integrations (the Dispatcher + the Pro/phone
  pitch), Misc (run directory, concurrency, default timeout). All four pages
  are in the DOM; one Save reads everything.
- `promptfield.js` is the completion-aware textarea (slash commands, paths, the
  "isn't enabled in <folder>" notice with an Enable button) used by the slot
  editor and the dispatcher.

**Analysis is automatic.** The *static* part (structure, DB shape, and a noisy
keyword prompt scan) runs buttonless: `Api._ensure_analyses` runs in a background
thread (kicked on init and on every refresh) and analyzes any local clone whose
cache is missing or stale (HEAD moved / `_ANALYZER_VERSION` bumped), caching into
`state.json`. The frontend `loadAnalyses` reads it on project select (and re-pulls
on Refresh).

**The good panels are assistant-owned.** The static prompt scanner
(`analysis.py` keyword regexes) is *intentionally kept only as a dim fallback* —
it false-positives badly (it even flags its own regexes). The real content comes
from the `/overboard` assistant, which delegates per-repo extraction to the
**`repo-analyst` subagent** (`agents/repo-analyst.md`, pinned to Sonnet, read-only,
returns JSON; its sibling **`work-reviewer`** does the same for recent-diff review
cards) and persists it via the MCP tools `set_prompts` / `set_setup` /
`set_snippets` / `set_architecture` into `ai.json`. `Api._overlay_ai` layers those
over the static result at read time: agent prompts *replace* the static ones (and
`prompts_source` flips `static`→`agent`; the UI dims static guesses), and setup /
snippets / architecture come straight from `ai.json`. This is gated by
`get_pending_work`, which computes `need_panels`/`panel_repos` from HEAD-stamped
panel entries (each `set_*` panel tool stamps the repo `head` it was written at)
and **budget-caps heavy work** — panels + first-ever work reviews go to at most
`HEAVY_BUDGET` projects per `HEAVY_COOLDOWN_SECS` window (constants in
`manager.py`; slots are inferred from recent `ai.json` write timestamps, so the
get tool stays read-only). The rest are marked `deferred_heavy` and trickle in
on later passes — a fresh install never "scans all repos at once."
When adding a new agent-owned panel, follow this exact path: new `ai.json` key →
`fresh_ai()` → a `set_*` MCP tool → `_overlay_ai` → a frontend tab.

Frontend calls `fetch('/api/<method>')` to the Python `Api` — every new method
must be added to `ALLOWED` in `app._make_handler`. Mermaid is vendored offline.
There are **no commit bar charts** — the day grid replaced them; don't bring bars
back. Glyphs in the UI are plain Unicode (✓ ✕ ■ ↷ ◔), not emoji — they must
render with the UI font on a box with no emoji font.

## Sources (Bitbucket + GitHub + local git, merged)

Overboard reads repos from **multiple providers at once**. The provider layer is
isolated so adding a provider is mechanical:

- `overboard/errors.py` — shared `ProviderError` / `AuthError` (no imports).
- `overboard/bitbucket.py`, `overboard/github.py` — stdlib `urllib` clients with
  the *same* interface (`make_session`, `list_active_repos(session, cutoff)`,
  `fetch_recent_commits`). GitHub auth is just a PAT; it paginates via the `Link`
  header. Their errors subclass the shared bases.
- `overboard/providers.py` — dispatch facade: `make_session(source)`,
  `active_repos(source, session, cutoff)`, `commits(repo, session)`. Returns
  **normalized** dicts; each repo carries `provider` + `workspace`.
- `perform_refresh(config, sources, old_state)` merges repos from every source
  (`_gather_active_repos`), dedupes by slug (keep most recent), and fetches
  commits per-repo via its provider. **Auth is per-source** — one bad token never
  blanks the other provider.
- Repo state records store `provider`/`workspace`; the view exposes `provider`
  (shown as a `bb`/`gh` tag). `mcp_server.get_commits` dispatches by provider.
- `localrepo` matches clones by **host** (`PROVIDER_HOSTS`) so github.com and
  bitbucket.org clones under `local_roots` are both found.
- `overboard/localgit.py` — a **key-free** provider that reads commits/diffs from
  local clones via `git log`/`git diff` (stdlib subprocess, reusing
  `localrepo._git`). It can't enumerate repos from an API, so instead of
  `active_repos`, `localrepo.discover_localgit(roots)` finds every clone under the
  roots (remote slug when present, else dir name) and `app._inject_localgit_repos`
  merges them into the refresh. A localgit source emits a `(None, None)` wildcard
  matcher so discovery/`local_links` include remote-less repos too. A repo that is
  *also* on GitHub/Bitbucket keeps the API as its commit source and just gains
  `also_providers:["localgit"]` + a local `path`. The repo state record and the
  `mcp_server.get_commits`/`get_recent_diff` repo dicts carry `path` for dispatch.

**To add another provider:** new client module (same interface) → add it to
`providers.py` dispatch + `PROVIDER_HOSTS` → add a source shape to
`store.load_sources` + the Settings panel.

## Onboarding (first-run wizard)

The dashboard boots with no credentials. When `get_view.has_sources` is false the
frontend auto-opens a stepped wizard (`app.js` `openWizard`/`renderWizard`,
reusing the `.modal` shell): pick tracking modes (Local-only / GitHub /
Bitbucket, any combination), guided token steps with links + scopes for the API
providers, then a roots-confirm step. `Api.detect_roots` suggests candidate root
folders by decoding `~/.claude/projects` working-dir names (lossy `/`→`-`
encoding — decode-then-`isdir`-validate + backtrack in `_decode_claude_dir`) plus
`DEFAULT_ROOTS`, each with a shallow repo count. The wizard's Finish reuses
`save_settings` (the single credentials writer) with `localgit.enabled` +
`local_roots`; the Settings modal exposes the same local-git toggle for returning
users.

## Credentials & setup

Tokens live in `~/.cache/overboard/credentials.json` (mode 0600, machine-local),
managed by the dashboard **Settings ▸ Sources** page (`Api.get_settings`/`save_settings`
— tokens are masked on read, blank-on-save keeps the existing one).
`store.load_sources()` returns enabled+complete sources and **never raises**, so
the dashboard **boots with no credentials** (`app.main` no longer exits; the
sidebar shows onboarding when `has_sources` is false). A legacy repo-root `.env`
(`ATLASSIAN_EMAIL`/`BITBUCKET_API_TOKEN`) is auto-migrated into the credentials
file. No `ANTHROPIC_API_KEY`.
