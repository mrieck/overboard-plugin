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

Zero third-party deps — runs on bare `python3` 3.11+ on Linux and macOS. `urllib`
(not requests), a hand-rolled `.env` parser (not python-dotenv), a hand-rolled
JSON-RPC server (not the `mcp` package), `http.server` (not pywebview; a native
window is used only if `pywebview` happens to be importable). Keep it this way —
don't reintroduce dependencies. Paths resolve via `${CLAUDE_PLUGIN_ROOT}` and
`python3`, so it's portable across machines with no config.

## No write races (three files under `~/.cache/overboard/`)

- `state.json` — **dashboard-owned** (commits, analysis, local links, activity).
  The MCP server never writes it.
- `ai.json` — **assistant-owned** (summaries, digests, architecture). Written only
  via the `set_*`/`record_*` MCP tools; the dashboard reads but never writes it.
- `events.jsonl` — append-only (hooks + flags/status). Safe by construction.

`Api._build_view` overlays `ai.json` onto `state.json` at read time.

## The dashboard UI (two-pane)

- **Left sidebar** (`overboard/web/`): condensed project list. Each row = project
  name + a GitHub-style **30-day activity grid** (6×5 cells, `lvl-0..lvl-4`
  intensity) + a compact active/idle chip + a `⚑ N` review flag when the
  assistant has flagged items. Clicking a row selects the project.
- **Right panel**: the selected project's full detail. Top row = **summary on the
  left, 30-day commit grid on the right**. Below: the assistant's report
  (narrative + review flags), recent activity from the team, the repositories,
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
returns JSON) and persists it via the MCP tools `set_prompts` / `set_setup` /
`set_snippets` / `set_architecture` into `ai.json`. `Api._overlay_ai` layers those
over the static result at read time: agent prompts *replace* the static ones (and
`prompts_source` flips `static`→`agent`; the UI dims static guesses), and setup /
snippets / architecture come straight from `ai.json`. This is gated to
recently-active projects via `get_pending_work` — never "scan all repos at once."
When adding a new agent-owned panel, follow this exact path: new `ai.json` key →
`fresh_ai()` → a `set_*` MCP tool → `_overlay_ai` → a frontend tab.

Frontend is vanilla HTML/JS (`web/index.html`, `app.js`, `styles.css`), no build
step, `fetch('/api/<method>')` to the Python `Api`. Mermaid is vendored offline.
There are **no commit bar charts** — the day grid replaced them; don't bring bars
back.

## Sources (Bitbucket + GitHub, merged)

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

**To add another provider:** new client module (same interface) → add it to
`providers.py` dispatch + `PROVIDER_HOSTS` → add a source shape to
`store.load_sources` + the Settings panel.

## Credentials & setup

Tokens live in `~/.cache/overboard/credentials.json` (mode 0600, machine-local),
managed by the dashboard **⚙ Settings** panel (`Api.get_settings`/`save_settings`
— tokens are masked on read, blank-on-save keeps the existing one).
`store.load_sources()` returns enabled+complete sources and **never raises**, so
the dashboard **boots with no credentials** (`app.main` no longer exits; the
sidebar shows onboarding when `has_sources` is false). A legacy repo-root `.env`
(`ATLASSIAN_EMAIL`/`BITBUCKET_API_TOKEN`) is auto-migrated into the credentials
file. No `ANTHROPIC_API_KEY`.
