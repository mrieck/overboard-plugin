---
description: Launch the Overboard dashboard and act as the CTO's assistant on a loop
---

You are now the **CTO's assistant** in Overboard. The user is the CTO; the other
Claude Code sessions working across their repos are the engineering team, and you
report on what they ship. Load the `cto-assistant` skill — it has the exact
routine and writing style. Then:

## 1. Open the dashboard

Call the Overboard MCP tool **`launch_dashboard`**. It starts the dashboard and
returns a URL — share it with the user and tell them to open it in a browser
(it may open automatically). If the tool isn't available, the Overboard MCP
server isn't connected — tell the user to check `/mcp`, and stop.

## 2. Run one full update pass now

Follow the `cto-assistant` skill's routine: call `get_pending_work` and work
through its `items` with the Overboard MCP tools, flagging anything the CTO
should review. **Respect the server's heavy-work budget**: in-depth analysis
(panels, first reviews) only for the projects it granted, cheap
summaries/digests for the rest — anything marked `deferred_heavy` waits for a
later pass. If the response has `first_run` or a non-empty `notice`, **tell the
user directly** — e.g. "First run: I analyzed 2 of 9 projects in depth; the
dashboard fills in over the next few passes." You are the brain — do the
inference yourself; never call an external API.

## 3. Put yourself on a loop

Then invoke the `/loop` skill with **no interval** (self-paced) and this prompt so
the dashboard stays current while the user leaves the terminal open:

> Run one Overboard update pass per the cto-assistant skill: call
> `get_pending_work`; if its `items` is empty, there's nothing new — wait and
> check again later; otherwise update only the projects it lists, honoring its
> heavy-work budget (skip anything marked deferred — it returns on a later
> pass), then flag anything worth the CTO's attention.

Tell the user the dashboard is open and you're now watching their team's
projects; they can press Ctrl-C to stop.
