# Overboard for Mac — Feasibility Report & Roadmap

**Product:** paid ($29.99, one-time) native SwiftUI macOS app for users who want a
native, always-on, automated Overboard experience. Direct sale (no App Store, no
sandbox), Sparkle updates, license-key activation. The free plugin (hooks + MCP
server + `/overboard` + web dashboard) stays free and fully functional.

**Verdict up front:** feasible, no architectural blockers. The plugin's file
contract (`~/.cache/overboard/`, five files, strict single-writer ownership,
`schema_version` 3) is clean enough to build a native client on. The total Python
surface is ~4,000 lines, of which only ~2,300 need Swift equivalents (the MCP
server and hooks stay Python and stay free). The dominant cost is not the backend
port — it's rebuilding the 74 KB vanilla-JS frontend as SwiftUI. Realistic solo
effort to a sellable 1.0: **~13–18 weeks**.

The premium story that justifies $29.99 is **automation and presence** (scheduled
autonomous `/overboard` runs + keep-awake + menu bar + notifications +
transcript/usage browsing), not a prettier viewer.

---

## 1. Architecture of the Mac app

### 1.1 The core insight: the app becomes *the dashboard*, not a second client

The system has three actors today: hooks (append `events.jsonl`), the MCP server
(writes `ai.json` only), and the dashboard (`overboard/app.py`) — the sole writer
of `state.json`, `context.json`, `credentials.json`, and the compactor of
`events.jsonl`. Critically, the dashboard is not just a viewer: it runs
`perform_refresh` (provider fetching), `_ensure_analyses` (static analysis),
`_ensure_sync` (git status), and event compaction. The MCP server *depends* on
that output — `get_pending_work`, `get_commits`, `get_recent_diff` all read
`state.json` for repo metadata, local paths, and heads.

So the Mac app cannot stay a pure reader forever: for the assistant loop to work,
something must keep writing `state.json`. **The Mac app takes over the dashboard
role entirely.** It assumes ownership of `state.json`, `context.json`,
`credentials.json`, and `events.jsonl` compaction, with the exact same
discipline: unique temp file in the same directory + atomic rename (mirror of
`store._atomic_write`), same schemas, same ownership boundaries. `ai.json`
remains MCP-owned — the app never writes it. `events.jsonl` remains
hook-appended — the app only compacts (replicating `events.compact`'s
tail-preserving rewrite).

**Coexistence protocol (both dashboards may run):**

- On launch and periodically, probe `http://localhost:8787` (cheap `get_view`
  with a short timeout). If the Python dashboard responds, the app enters
  **mirror mode**: read-only on shared files, all mutations (dismiss review,
  context edits, settings) proxied through the existing allowlisted HTTP POST
  API (`ALLOWED`, `overboard/app.py:1031`). This is nearly free to build — the
  web API is already exactly the mutation surface the app needs — and it
  preserves single-writer by definition.
- If no server responds, the app is the **owner**: direct file writes,
  background refresh loop, analysis, compaction.
- Show a subtle banner in mirror mode ("Web dashboard is running — mirroring
  it") so the state is never mysterious. Most paying users will simply stop
  launching `python3 -m overboard.app`.

App-private state (window layout, selected project, license, scheduler config,
notification prefs, run logs) goes in `UserDefaults` +
`~/Library/Application Support/Overboard/` — never into the shared contract
files.

### 1.2 File watching

`_atomic_write` uses `os.replace`, which swaps the **inode** — a per-file
`DispatchSource` fires `.rename` once and then tracks a dead inode. Correct
approach:

- **One `DispatchSource.makeFileSystemObjectSource` on the directory fd** for
  `~/.cache/overboard/` (vnode `.write`), debounced ~200 ms, then re-`stat` the
  five files and reload whichever mtime changed. One watcher, immune to inode
  swaps, near-zero latency. (FSEvents is the fallback if odd filesystems surface
  edge cases; overkill for one flat directory.)
- Keep a 20–30 s fallback poll as belt-and-braces (the web app polls at 20 s
  today; the native app will feel instant by comparison).
- `events.jsonl`: track file size; on growth read only the appended tail (seek
  to last offset); on *shrink* (compaction happened) re-read from the 30-day
  cutoff. This mirrors `events.read_events` cheaply and gives real-time hook
  events — the raw material for notifications and the menu bar.

### 1.3 Codable models and schema tolerance

Mirror `schema_version` 3 (`store.fresh_state`/`fresh_ai`, `context.json`, and
the event record shape in `hooks/emit_event.py`). Two hard rules:

1. **Tolerant decode, lossless re-encode.** Python's `load_state` wipes to
   `fresh_state()` on version mismatch (`store.py:198`) — the app must NOT copy
   that for files it writes. Decode typed models for known keys but retain the
   raw JSON object; when writing, merge model changes back into the raw
   dictionary so unknown keys added by a newer plugin are never dropped. This
   single decision is what prevents the free plugin and paid app from
   corrupting each other as they evolve.
2. **On an unseen `schema_version`: degrade to read-only** with an "update
   Overboard for Mac" prompt (Sparkle makes this painless). Never reset the file.

`local_links` and `local_sync` are already machine-keyed
(`localrepo.machine_key()` — node name); the Swift side must reuse the identical
key derivation so a Mac that ran the Python dashboard yesterday sees its own
links today.

### 1.4 What must be reimplemented in Swift (effort per module)

| Python module | Lines | Swift approach | Effort |
|---|---|---|---|
| `store.py` | 268 | Codable + atomic write + credentials (chmod 0600) | **S** (2–3 d) |
| `events.py` | 160 | JSONL tail-reader, cwd→slug mapping, compaction | **S** (2–3 d) |
| `github.py` / `bitbucket.py` / `providers.py` | 439 | `URLSession` async clients, same normalized dicts, Link-header pagination, per-source auth isolation | **S–M** (4–6 d) |
| `localrepo.py` / `localgit.py` | 442 | `Process` running `/usr/bin/git` (log/diff/ls-remote), clone discovery under roots, `~/.claude/projects` dir-name decoding (`_decode_claude_dir` backtracking) | **M** (1 wk) |
| `analysis.py` | 513 | **Partial port only.** Structure scan, manifest digest, DB-shape detection; *drop the keyword prompt scanner* — CLAUDE.md itself calls it a noisy dim fallback the AI overlay replaces. Show "awaiting assistant analysis" instead. | **S–M** (3–5 d) |
| `diagram.py` | 91 | Trivial Mermaid string builder | **S** (0.5 d) |
| `app.py` refresh/view core (`perform_refresh`, `_build_view`, `_overlay_ai`, grouping, discovery, `_ensure_sync`) | ~700 | The app's engine + view model. The subtle part: grouping resolution, ai-overlay semantics, logical-day bucketing (5 am `day_start_hour`), review-flag keys. Port faithfully with golden-file tests (Python vs Swift view over the same fixture files). | **M–L** (1.5–2 wk) |
| `web/app.js` + `styles.css` (74 + 22 KB) | — | Full SwiftUI rebuild: sidebar with 30-day 6×5 activity grids, detail panel, recent-work cards, analysis tabs, launch/vision editors, settings + onboarding wizard, modals | **L** (3–5 wk) — biggest line item |
| `mcp_server.py`, `hooks/` | 669 | **No port.** Stay Python, stay in the free plugin. | 0 |

**Mermaid:** WKWebView island with the already-vendored `mermaid.js` — bundle in
`Resources`, minimal local HTML shell, `postMessage` the diagram source in,
measure rendered SVG height out, wrap in `NSViewRepresentable`. Optionally one
hidden web view rendering to SVG if per-card views feel heavy. Do not attempt a
native Mermaid parser. Effort S (2–3 d).

**Git:** shell out via `Process`. No sandbox means no entitlement pain; libgit2
adds a dependency for zero benefit — subprocess git is already proven sufficient.

**Menu bar:** `MenuBarExtra` (target macOS 14+, `.window` style). Not a nicety —
half the product: aggregate status (N active sessions, ⚑ flags), "open main
window," "run assistant now," "hold Mac awake." Support menu-bar-only mode
(LSUIElement toggle) for ambient use.

---

## 2. Premium feature assessment

### 2.1 Scheduler for headless Claude runs — **the flagship. Build it. (M)**

The `/overboard` assistant loop is the product's magic, but today a human must
start it. Automating it turns Overboard from "a dashboard I check" into "a chief
of staff that worked while I slept" — that sentence sells a $29.99 app.

- **Mechanism:** run `claude -p "/overboard"` headless via `Process` (capture
  stdout/stderr; JSON output mode for run metadata). Results flow back **for
  free**: the session writes summaries/digests/work-reviews through the existing
  MCP `set_*`/`record_*` tools into `ai.json`; the file watcher sees the change,
  the UI updates, a notification fires. No new data path — the elegant payoff of
  building on the plugin's contract.
- **Scheduling:** the app UI owns the schedule ("every 2 h during work hours,"
  "nightly at 2 am") and registers a launchd LaunchAgent
  (`StartCalendarInterval`) via `SMAppService.agent` (macOS 13+) pointing at a
  bundled CLI helper — true run-even-if-the-app-was-quit behavior, no launchctl
  for the user.
- **Skill/plugin selection:** user picks the command/prompt per schedule slot
  (default `/overboard`; power users add their own slash commands or
  `--append-system-prompt` variants). Keep the invocation string user-editable —
  also the hedge against `claude` CLI flag churn.
- **Run history:** log each run (start/end, exit code, truncated transcript) to
  `~/Library/Application Support/Overboard/runs/` and surface a Runs pane with
  failures highlighted.
- **Honest caveats to document:** headless runs consume Max/Pro plan limits;
  runs need `claude` on PATH and a valid login; a run can take minutes —
  serialize runs, never overlap.
- **Effort:** M (1.5–2 wk incl. UI, launchd, run log). **Value: highest of any
  candidate.**

### 2.2 Keep-awake / scheduled wake — **keep-awake in v1 (S); real wake in v1.1 (M)**

- **Keep-awake:** `IOPMAssertionCreateWithName` with
  `PreventUserIdleSystemSleep` (display-sleep prevention optional, off by
  default). No privileges, ~50 lines, identical to `caffeinate`. Integrate with
  the scheduler ("hold awake while a run is pending/active") plus a manual
  menu-bar toggle with a countdown ("awake until 6 am"). **Effort S (1–2 d).**
- **Scheduled wake (`pmset schedule`) requires root.** It needs a privileged
  helper installed via `SMAppService.daemon` with an admin prompt, and it still
  can't wake a closed-lid MacBook on battery. **Defer the helper to v1.1.** In
  v1, detect the situation ("your Mac may sleep before the 2 am run") and offer
  the assertion path plus a copy-pasteable
  `sudo pmset repeat wakeorpoweron ...` snippet.

### 2.3 Candidate features — accept / reject

- **Menu bar live status + native notifications — ACCEPT (core, S–M).** Hooks
  already stream everything needed into `events.jsonl` in real time
  (Stop/SubagentStop with `last_message`, session ids, cwd). Tail the file, map
  cwd→project (logic exists in `events.py`), post `UNUserNotificationCenter`
  notifications: "Session finished in *api-server* — 'Deployed the migration…'",
  "Assistant flagged 2 items in *checkout*." Per-project mute, quiet hours,
  click-through deep links. This is the feature the web dashboard *structurally
  cannot have* (browser tab, 20 s poll) — the clearest daily-felt value.
- **Session transcript browsing — ACCEPT (M, ~1.5–2 wk).** Read
  `~/.claude/projects/<encoded-cwd>/*.jsonl` transcripts natively (the plugin
  already solves the lossy dir-name decoding). A searchable "what exactly did
  the team do in that session" viewer, linked from activity rows and recent-work
  cards via `session_id`. Closes the trust loop the whole product is about.
- **Usage tracking — ACCEPT (M, ~1 wk).** Parse the same transcripts for
  per-session token/model usage; per-project and daily rollups, "heaviest
  sessions." ccusage-style tools prove demand in exactly this audience. Frame as
  *usage against your plan*, not dollars (Max users don't pay per token).
- **URL scheme deep links (`overboard://project/x`) — ACCEPT (S, ~1 d)** —
  needed for notification click-through anyway.
- **Spotlight indexing of reports — REJECT:** niche, real maintenance cost.
- **Shortcuts / App Intents — DEFER to v1.1 (S):** "Run assistant now," "Hold
  awake 2 h," "Get flags" — cheap once the features exist, great marketing copy,
  sells to nobody on its own.
- **Widgets — DEFER to v1.2:** widget extensions are sandboxed even for
  Developer ID distribution, so a widget can't read `~/.cache/overboard`
  directly — the app would have to mirror data into a group container. Plumbing
  for a glanceable the menu bar already covers.
- **AppleScript dictionary — REJECT:** App Intents supersedes it.

### 2.4 The coherent premium tier

Don't sell a grab bag; sell one sentence: **"The web dashboard shows you what
happened. The Mac app is the chief of staff that's always on: it watches in the
menu bar, notifies you the moment something needs you, runs your assistant on a
schedule — even overnight, keeping the Mac awake — and lets you audit any
session's transcript and usage."**

v1 paid set: native app + menu bar + notifications + scheduler + keep-awake +
transcript browser + usage view + deep links. Comfortably north of $29.99 for
someone running many projects at once.

---

## 3. Free / paid split

- **Free (unchanged, open):** the plugin — hooks, MCP server, `/overboard`,
  skills, agents, and the web dashboard. Do not remove or degrade anything. The
  plugin is the funnel: every plugin user already has data in
  `~/.cache/overboard/`, so the Mac app's first launch shows *their* projects
  instantly — the best demo imaginable.
- **Paid:** everything native and everything autonomous (§2.4). The split is
  **manual vs. automated** and **browser vs. ambient**, not data
  hostage-taking. The web dashboard keeps full parity for its existing features;
  the app's exclusives are things the web architecturally can't do (real-time
  file watching, notifications, launchd, IOPM, native transcript access).
- Avoid: paywalling schema features (drives forks and resentment in an
  open-source-adjacent audience) or plugin nag screens. One tasteful dashboard
  footer line ("Overboard for Mac — native app with scheduling & notifications")
  is enough.

---

## 4. Sync-readiness for v2

Low-cost v1 decisions that keep sync cheap later:

1. **Don't move the shared files.** `~/.cache/overboard/` is the plugin
   contract; relocating forks the ecosystem. App-private data goes to
   Application Support; shared data stays put. (If the path ever migrates, do it
   in the plugin, for everyone.)
2. **Keep machine-keying; extend nothing unilaterally.** Reuse `machine_key()`
   exactly. Tag app-ingested records (in app-private storage) with a stable
   machine UUID; don't change the hook's event shape.
3. **Preserve the merge affordances that already exist:** almost every
   AI/context record carries an `at` ISO timestamp and is keyed by project/slug.
   That makes **per-key last-writer-wins** a natural, conflict-light merge for
   `ai.json` and `context.json` — the two files actually worth syncing. Never
   strip `at`, never renumber ids.
4. **Lossless round-trip writing (§1.3)** doubles as sync safety: a merge engine
   can operate on raw JSON without the app's models being exhaustive.
5. Maintain the schema contract doc (§6) so a second machine on an older plugin
   is detected, not corrupted.

**v2 sketch (not a commitment):** CloudKit private database, one record per
(file, key) — e.g. `ai.summaries["checkout"]` is one record with the JSON blob +
`at` — LWW by `at`; `events` synced as per-machine append-only streams or not at
all initially (commits + AI content carry most cross-machine signal, and
`get_recent_diff` exists precisely to review repos with no local clone).
CloudKit fits: single-user multi-Mac, zero server cost, no accounts to build.
**Verify iCloud/CloudKit entitlements for Developer ID (non-App Store) apps
during v2 planning.** If v2 grows into *teams* (multiple humans), CloudKit is
wrong and a small hosted sync (per-key JSON + LWW, same merge model) replaces
it — which is why v1's only real obligation is the merge-friendly data shape,
not the transport.

---

## 5. Licensing & distribution

- **Store: Lemon Squeezy.** Merchant of record (handles EU VAT — non-negotiable
  for a solo dev selling globally), first-class license-key API
  (generate/activate/validate, seat limits), simple checkout overlay. Paddle is
  the heavier MoR alternative if volume grows; Gumroad's flat 10% and weaker
  license API make it strictly worse at $29.99. Reversible later — the keys are
  yours.
- **License model:** $29.99 one-time, 2–3 seat activations (this audience owns
  multiple Macs — be generous), 1 year of updates via Sparkle; optionally paid
  major-version upgrades later. Validate online at activation, cache a signed
  (Ed25519) receipt locally, re-check lazily — never brick offline.
- **Trial:** 14-day full-featured, no email wall (this audience hates it). The
  trial's one job: get a scheduled run configured on day one so the user wakes
  up to fresh reports.
- **Updates:** Sparkle 2, EdDSA-signed appcast.
- **Apple requirements:** Developer Program ($99/yr), Developer ID Application
  cert, Hardened Runtime, notarization via `notarytool` in CI, stapled ticket.
  No sandbox needed or wanted (git subprocesses, `~/.cache` + `~/.claude` reads,
  launchd, IOPM all stay trivial). Budget a few days once for the
  signing/notarization/Sparkle pipeline; afterwards it's a script.

---

## 6. Risks & open questions

1. **Schema drift between free plugin and paid app (top risk).** The plugin
   moves fast (`schema_version` already at 3; `_ANALYZER_VERSION` bumps
   invalidate caches). Mitigations in order: (a) extract a **versioned
   `SCHEMA.md`** in this repo covering the five files + the HTTP API + the event
   record, with the rule that any shape change bumps `schema_version` and
   updates the doc in the same commit; (b) lossless round-trip writes and
   read-only degrade on unknown versions; (c) golden-file cross-tests (same
   fixtures → Python view vs Swift view) in the app's CI against plugin `main`.
   Same author controls both repos today, so this is process, not politics —
   write it down before the first external contributor touches `store.py`.
2. **Dual-writer window.** Mirror mode covers the normal case; atomic writes
   prevent corruption; lost updates remain possible in a narrow window if the
   web dashboard starts mid-write-cycle. Acceptable: document "one dashboard at
   a time," and have the app back off (re-probe) when it sees a `state.json`
   change it didn't author.
3. **Claude Code surface churn:** hook payload fields, transcript JSONL format
   under `~/.claude/projects`, and `claude -p` flags are informally versioned
   upstream. Hooks absorb the first; the app owns transcript + headless-CLI
   risk — keep both behind small adapters, keep the scheduler command
   user-editable, and lean on Sparkle for fast fixes.
4. **Free-rides-free tension:** the web dashboard is genuinely good and free. A
   "native viewer only" v1 would not sell — the automation tier *is* the
   product. Conversely, resist gating plugin improvements behind the app.
5. **Scheduled runs vs. plan limits:** conservative defaults, visible failures,
   and the usage view (§2.3) as the pressure gauge.
6. **Open questions:** minimum macOS target (recommend 14 — modern
   `MenuBarExtra` + `SMAppService`); CloudKit-for-Developer-ID confirmation
   before v2; Keychain vs `credentials.json` — keep the 0600 file authoritative
   (the MCP server reads it), optionally *mirror* to Keychain later, never fork.

---

## 7. Phased roadmap

| Phase | Deliverable | Effort |
|---|---|---|
| **0 — Contract** | `SCHEMA.md` from `store.py`/`events.py`/hooks/HTTP API; fixture corpus + golden views from the Python implementation | 1 wk |
| **1 — Native viewer + presence** (private alpha) | Codable models w/ lossless round-trip; directory DispatchSource + JSONL tailing; sidebar w/ activity grids, project detail, recent-work cards, AI overlay, Mermaid island; menu bar; notifications; deep links. Read-only + mirror-mode mutations via localhost API | 4–6 wk |
| **2 — Dashboard takeover** (beta) | Swift refresh engine (GitHub/Bitbucket/localgit), discovery + grouping, settings + onboarding wizard, context/launch editing, partial static-analysis port, compaction, sync-status; port-probe ownership protocol; golden-view parity tests pass | 3–4 wk |
| **3 — Automation tier** | Scheduler (in-app + launchd via SMAppService), run history, keep-awake tied to runs + manual toggle, failure notifications | 2–3 wk |
| **4 — Audit tier** | Transcript browser, usage view, links from activity/cards into transcripts | 2–3 wk |
| **5 — Ship** | Lemon Squeezy checkout + activation, trial, Sparkle appcast, Developer ID + notarization CI, marketing site, 1.0 | 2 wk |
| **v1.x** | Privileged-helper scheduled wake (`pmset`), App Intents/Shortcuts, widget via group container | as demanded |
| **v2** | CloudKit private-DB sync of `ai.json`/`context.json` (per-key LWW on `at`), multi-Mac merge UI | 4–6 wk |

Total to 1.0: **~13–18 weeks solo.** Phases 1–2 are sequenced so a working demo
on real user data exists at ~week 6, and the app never blocks the free plugin's
evolution.

---

## Appendix — ground-truth files for implementation

- `overboard/store.py` — file contract, schemas, atomic-write discipline
  (`SCHEMA_VERSION = 3` at :25; wipe-on-mismatch at :198 — do NOT replicate)
- `overboard/app.py` — refresh engine, `_build_view`/`_overlay_ai`, `ALLOWED`
  POST API at :1031 (the mirror-mode mutation surface)
- `overboard/mcp_server.py` — MCP tool surface whose `state.json`/`ai.json`
  expectations the app must keep satisfying
- `hooks/emit_event.py` — event record shape powering notifications/menu bar
- `overboard/events.py` — JSONL read/compaction semantics (tail-preserving
  rewrite) and cwd→repo mapping
- `CLAUDE.md` — ownership rules ("No write races") and product framing the port
  must preserve
