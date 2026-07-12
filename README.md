# Overboard

*Shipping too much? You need to go Overboard.*

Overboard is a Claude Code plugin whose dashboard lets your Claude act as a
**project manager** — presenting what matters across all your projects while
other Claude Code sessions are working. It shows commit activity, architecture
and DB-schema visualizations (Mermaid), detected LLM prompts, and — live — the
key "needs review" tidbits from your working Claudes.

**No Anthropic API key.** All the AI (summaries, digests, architecture) is done
by the `/overboard` agent itself — on your **Max/Pro subscription**, not the
metered API. You run `/overboard`, Claude launches the dashboard, does the
analysis, and puts *itself* on a loop; you leave the terminal open. The dashboard
is a pure viewer.

**No install, no dependencies.** Overboard runs on the Python 3 **standard
library only** — no venv, no `pip install`, no API key. Anywhere `python3`
exists (Linux, macOS) it just works. Claude Code's `${CLAUDE_PLUGIN_ROOT}`
handles the paths, so it's portable across machines with zero config.

It has two parts in one repo:

1. **The plugin** Claude Code loads — hooks that observe your sessions, an MCP
   server the agent reads inputs from and writes results to, the `/overboard`
   command, and the `project-manager` skill.
2. **The dashboard** — a tiny stdlib `http.server` you view in a browser (or a
   native window if you happen to have `pywebview` installed). Key-free.

## How it works

```
 Working Claude A ─┐  Stop / SubagentStop / PostToolUse hooks  (async, never blocks)
 Working Claude B ─┼─▶ hooks/emit_event.py ─▶ ~/.cache/overboard/events.jsonl
 Working Claude C ─┘                                  │
 Bitbucket commits ──▶ perform_refresh ──▶ state.json ┤  (deterministic, key-free)
                                                      ▼
      /overboard agent (Max sub) ── reads via MCP ──▶ thinks ──▶ writes ai.json
                                                      ▼
                         dashboard = pure viewer: overlays ai.json on state.json →
                           summaries + activity feed + "needs review" +
                           Mermaid architecture / ER / commit sparklines
```

The agent is **event-gated**: `get_pending_work` only returns projects that
changed (new finished work, or new commits), so idle projects cost nothing — not
even a Max-sub turn. Bitbucket commits are the cross-machine baseline, so work you
did on other machines and pushed still shows up.

**Three files, no write races:** the dashboard owns `state.json`; the agent owns
`ai.json`; `events.jsonl` is append-only. The dashboard reads `ai.json` but never
writes it, and the agent's MCP server never writes `state.json`.

## Setup (once)

Just credentials — there's nothing to install. Put a Bitbucket read token in a
`.env` at the repo root:

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
+ structure) always shows. AI content (summaries, PM digests, architecture
write-ups) fills in once `/overboard` has run. **Refresh** re-pulls commits;
**Rescan** re-discovers local clones; click a repo's **analyze** for prompts /
DB-shape / architecture.

## MCP tools (the agent's hands)

The `/overboard` agent drives everything through the bundled MCP server (key-free
— the tools just read deterministic data and persist JSON):

- **Read:** `get_pending_work()` (the to-do list), `list_projects()`,
  `get_commits(slug, limit)`, `get_repo_analysis(slug)`,
  `get_project_events(project, limit)`.
- **Write:** `set_project_summary(project, text)`,
  `record_digest(project, narrative, review)`,
  `set_architecture(slug, text, mermaid)`, `flag_for_review(project, note)`,
  `record_status(project, note)`.
- **Dashboard:** `launch_dashboard()` — start the dashboard server, return its URL.

The `project-manager` skill is the agent's full playbook (routine + writing
style + the event-gated loop rule). Any working Claude can also call
`flag_for_review` to raise something ad hoc.

## Configuration

Edit `overboard/projects.json` — `workspace`, `refresh_interval_minutes`,
`commit_window_days`, and optional `local_roots` (default `["~/Sites"]`) for
where local clones are discovered. Files under `~/.cache/overboard/`:
`state.json` (dashboard-owned: commits, analysis, local links, activity),
`ai.json` (agent-owned: summaries, digests, architecture), `events.jsonl`
(append-only activity log). Delete any to reset that piece.

## Debug helpers

```sh
python3 -m overboard.localrepo                 # discovered local clones
python3 -m overboard.analysis <slug>           # static analysis of one local repo
python3 -m overboard.bitbucket <slug> <branch> # raw commit fetch
python3 overboard/mcp_server.py                # run the MCP server (stdio)
```
