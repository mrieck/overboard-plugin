# Overboard — notes for Claude

## What this is (the mental model)

Overboard is a Claude Code plugin. Its role is **assistant to the CTO**, *not* a
"project manager." When you work on this codebase or run `/overboard`, hold this
framing — it should shape naming, copy, and behavior:

- **The user is the CTO.** They run many projects at once and don't have time to
  read every diff. They want signal: what shipped, what's risky, what needs a
  decision from them.
- **The other Claude Code sessions are the engineering team.** They do the work
  across the CTO's repos.
- **The `/overboard` session is the CTO's assistant / chief of staff.** It watches
  what the team ships (via hooks + Bitbucket commits), keeps the dashboard
  current, and surfaces the few things worth the CTO's attention. It stays quiet
  when nothing's happening.

Prefer this vocabulary in code and copy: "CTO", "the team", "assistant",
"report", "flag for review". Avoid "project manager" / "PM".

## Architecture (two runtimes, one repo)

1. **Plugin** — what Claude Code loads:
   - `hooks/hooks.json` + `hooks/emit_event.py`: observe every session
     (Stop/SubagentStop/PostToolUse/SessionStart/End, all `async`), append to
     `~/.cache/overboard/events.jsonl`. Stdlib-only, never blocks or raises.
   - `.mcp.json` → `overboard/mcp_server.py`: a hand-rolled stdlib JSON-RPC stdio
     MCP server — the assistant's hands (read inputs, write results).
   - `commands/overboard.md`: the `/overboard` command.
   - `skills/cto-assistant/SKILL.md`: the assistant's full playbook.
2. **Dashboard** — `python3 -m overboard.app`: a stdlib `http.server` you view in
   a browser at `http://localhost:8787`. Two-pane UI (see below). Pure viewer.

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
  `get_project_context` tool but never writes. One active launch per project;
  completed ones move to `past_launches`.
- `events.jsonl` — append-only (hooks + flags/status). Safe by construction.
  The dashboard compacts it in the background (`events.maybe_compact`): 30-day
  retention + 5 MB cap, at most once a day. Hooks never trim — they must stay
  append-only and non-blocking; compaction re-appends any tail written during
  the rewrite before the atomic replace.
- `credentials.json` — sources/tokens (see below).

`store._atomic_write` uses a **unique** temp file (`tempfile.mkstemp`) per write —
a fixed `.tmp` name raced when the background analyzer and a refresh saved state
concurrently (the `state.json.tmp` error).

`Api._build_view` overlays `ai.json` onto `state.json` at read time.

## The dashboard UI (three-pane: sidebar, detail, context)

- **Left sidebar** (`overboard/web/`): condensed project list. Each row = project
  name + a calendar-style **activity grid** (rows = weeks Mon→Sun, newest week
  on top; the current week + 4 full weeks ≈ last 5 weeks, `lvl-0..lvl-4`
  intensity) + a compact active/idle chip + a `⚑ N` review flag when the
  assistant has flagged items. Clicking a row selects the project.
- **Right panel**: the selected project's full detail. Top row = **summary on the
  left, 5-week commit grid on the right**. Below: the assistant's report
  (narrative + review flags), **Recent work** cards (the assistant's per-sprint
  delta layer — newest-first, expandable, hide with ✕; `need_review` in
  `get_pending_work` drives them, the `work-reviewer` subagent extracts from real
  git diffs, `record_work_review` persists, `hidden_work_reviews` in state.json
  remembers hides), recent activity from the team, the repositories,
  and — **always shown, no button** — each local repo's analysis (Overview /
  Prompts / Data shape).

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

Frontend is vanilla HTML/JS (`web/index.html`, `app.js`, `styles.css`), no build
step, `fetch('/api/<method>')` to the Python `Api`. Mermaid is vendored offline.
There are **no commit bar charts** — the day grid replaced them; don't bring bars
back.

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
managed by the dashboard **⚙ Settings** panel (`Api.get_settings`/`save_settings`
— tokens are masked on read, blank-on-save keeps the existing one).
`store.load_sources()` returns enabled+complete sources and **never raises**, so
the dashboard **boots with no credentials** (`app.main` no longer exits; the
sidebar shows onboarding when `has_sources` is false). A legacy repo-root `.env`
(`ATLASSIAN_EMAIL`/`BITBUCKET_API_TOKEN`) is auto-migrated into the credentials
file. No `ANTHROPIC_API_KEY`.
