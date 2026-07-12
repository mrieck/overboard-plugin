# Overboard

*Shipping too much? You need to go Overboard.*

Overboard is a Claude Code plugin whose dashboard lets your Claude act as the
**CTO's assistant** — you're the CTO, the other Claude Code sessions are your
engineering team, and Overboard presents what matters across all your projects
while that team works. It shows commit activity, architecture and DB-schema
visualizations (Mermaid), detected LLM prompts, and — live — the key "needs
review" tidbits from your working Claudes.

The dashboard is a **two-pane** layout: a condensed left sidebar lists every
project with a GitHub-style 30-day activity grid and any "⚑ N to review" flags;
click a project and the whole right panel becomes its detail — summary, your
assistant's report, recent activity from the team, and per-repo architecture /
prompts / data-shape analysis.

**No Anthropic API key.** All the AI (summaries, digests, architecture) is done
by the `/overboard` assistant itself — on your **Max/Pro subscription**, not the
metered API. You run `/overboard`, Claude launches the dashboard, does the
analysis, and puts *itself* on a loop; you leave the terminal open. The dashboard
is a pure viewer.

**No install, no dependencies.** Overboard runs on the Python 3 **standard
library only** — no venv, no `pip install`, no API key. Anywhere `python3`
exists (Linux, macOS) it just works. Claude Code's `${CLAUDE_PLUGIN_ROOT}`
handles the paths, so it's portable across machines with zero config.

It has two parts in one repo:

1. **The plugin** Claude Code loads — hooks that observe your sessions, an MCP
   server the assistant reads inputs from and writes results to, the `/overboard`
   command, and the `cto-assistant` skill.
2. **The dashboard** — a tiny stdlib `http.server` you view in a browser (or a
   native window if you happen to have `pywebview` installed). Key-free.

## How it works

```
 Working Claude A ─┐  Stop / SubagentStop / PostToolUse hooks  (async, never blocks)
 Working Claude B ─┼─▶ hooks/emit_event.py ─▶ ~/.cache/overboard/events.jsonl
 Working Claude C ─┘                                  │
 Bitbucket commits ──▶ perform_refresh ──▶ state.json ┤  (deterministic, key-free)
                                                      ▼
  /overboard assistant (Max sub) ── reads via MCP ──▶ thinks ──▶ writes ai.json
                                                      ▼
                         dashboard = pure viewer: overlays ai.json on state.json →
                           summaries + activity feed + "needs review" +
                           Mermaid architecture / ER / commit sparklines
```

The assistant is **event-gated**: `get_pending_work` only returns projects that
changed (new finished work, or new commits), so idle projects cost nothing — not
even a Max-sub turn. Bitbucket commits are the cross-machine baseline, so work you
did on other machines and pushed still shows up.

**Three files, no write races:** the dashboard owns `state.json`; the assistant
owns `ai.json`; `events.jsonl` is append-only. The dashboard reads `ai.json` but
never writes it, and the assistant's MCP server never writes `state.json`.

## Setup (once)

Just credentials — there's nothing to install. Open the dashboard and click the
**⚙ Settings** button; it boots even with no credentials. Add either or both
sources (you can run them together — one merged board):

- **Bitbucket** — workspace + Atlassian email + an API token.
- **GitHub** — a personal access token (classic with `repo` scope, or
  fine-grained with repository **contents + metadata, read**). Pulls the repos
  you own.

Saved to `~/.cache/overboard/credentials.json` (mode `0600`, machine-local, so it
survives a plugin reinstall). Tokens are never sent back to the browser.

A legacy `.env` at the repo root still works as a Bitbucket fallback and is
migrated into the credentials file on first save:

```
ATLASSIAN_EMAIL=you@example.com
BITBUCKET_API_TOKEN=xxxxxxxx
```

No `ANTHROPIC_API_KEY` — the `/overboard` agent does all AI on your Max/Pro sub.
Requires `python3` 3.11+ on your PATH (Linux and macOS both have it). That's it.

## Install the plugin in Claude Code

```
/plugin marketplace add /path/to/overboard-plugin
/plugin install overboard@overboard-marketplace
```

Restart Claude Code (a full quit/relaunch, not just `/reload-plugins`) so it
launches the MCP server with the current config. Validate with `/plugin
validate`; check `/mcp` shows **overboard ✔ connected**. Once enabled, its hooks
observe every Claude Code session you run.

**Porting to another machine (e.g. your Mac):** copy the folder, add the
marketplace there, install, drop in a `.env`. No venv, no deps, no path edits —
`${CLAUDE_PLUGIN_ROOT}` and `python3` resolve per-machine.

## Run it

From Claude Code, **`/overboard`** opens the dashboard, does a first update pass,
and loops. To run the dashboard directly:

```sh
python3 -m overboard.app          # serve dashboard, open browser (native window if pywebview present)
python3 -m overboard.app --once   # headless refresh, print, exit
```

It serves a browser dashboard at `http://localhost:8787`. Deterministic content
(commit activity, sparklines, local badges, static prompt/DB analysis, Mermaid ER
+ structure) always shows. AI content (summaries, status digests, architecture
write-ups) fills in once `/overboard` has run. **Refresh** re-pulls commits;
**Rescan** re-discovers local clones; click a repo's **analyze** for prompts /
DB-shape / architecture.

## MCP tools (the assistant's hands)

The `/overboard` assistant drives everything through the bundled MCP server (key-free
— the tools just read deterministic data and persist JSON):

- **Read:** `get_pending_work()` (the to-do list), `list_projects()`,
  `get_commits(slug, limit)`, `get_repo_analysis(slug)`,
  `get_project_events(project, limit)`.
- **Write:** `set_project_summary(project, text)`,
  `record_digest(project, narrative, review)`,
  `set_architecture(slug, text, mermaid)`, `set_prompts(slug, items)`,
  `set_setup(slug, text)`, `set_snippets(slug, items)`,
  `flag_for_review(project, note)`, `record_status(project, note)`.
- **Dashboard:** `launch_dashboard()` — start the dashboard server, return its URL.

The `cto-assistant` skill is the assistant's full playbook (routine + writing
style + the event-gated loop rule). Any working Claude can also call
`flag_for_review` to raise something ad hoc.

## Configuration

Sources & tokens live in the **⚙ Settings** panel (→
`~/.cache/overboard/credentials.json`). Non-secret app config is in
`overboard/projects.json` — `refresh_interval_minutes`, `commit_window_days`, and
optional `local_roots` (default `["~/Sites"]`) for where local clones are
discovered (both `bitbucket.org` and `github.com` clones under those roots are
matched). Files under `~/.cache/overboard/`: `credentials.json` (sources/tokens),
`state.json` (dashboard-owned: commits, analysis, local links, activity),
`ai.json` (agent-owned: summaries, digests, architecture/prompts/setup/snippets),
`events.jsonl` (append-only activity log). Delete any to reset that piece.

## Debug helpers

```sh
python3 -m overboard.localrepo                       # discovered local clones (all sources)
python3 -m overboard.analysis <slug>                 # static analysis of one local repo
python3 -m overboard.bitbucket <slug> <branch>       # raw Bitbucket commit fetch
python3 -m overboard.github <owner> <repo> <branch>  # raw GitHub commit fetch
python3 overboard/mcp_server.py                      # run the MCP server (stdio)
```
