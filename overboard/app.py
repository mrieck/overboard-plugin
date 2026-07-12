"""Overboard — dashboard + headless entry point.

Run in the panel:      python -m overboard.app        (from the parent directory)
Headless refresh:      python -m overboard.app --once
"""

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from overboard import analysis, bitbucket, localrepo, manager, store
from overboard.bitbucket import AuthError, BitbucketError

APP_TITLE = "Overboard"
ICON_NORMAL = "applications-development"
ICON_ACTIVITY = "starred"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_date(iso: str) -> datetime | None:
    try:
        return datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return None


def _slug_signature(slugs: list[str]) -> str:
    return hashlib.sha1("\n".join(sorted(slugs)).encode()).hexdigest()


def _review_key(project: str, text: str) -> str:
    """Stable content key for a review item, so a dismissed item stays dismissed
    across refreshes (and re-appears only if its text actually changes)."""
    return hashlib.sha1(f"{project}\0{text}".encode()).hexdigest()


def _prefix_group(slugs: list[str]) -> dict[str, list[str]]:
    """Fallback grouping when AI is unavailable: cluster by the name stem
    before the first '-'."""
    groups: dict[str, list[str]] = {}
    for s in slugs:
        groups.setdefault(s.split("-", 1)[0], []).append(s)
    return groups


def resolve_projects(
    config: dict, session, client, old_state: dict
) -> tuple[list[dict], dict]:
    """Discover repos active within the window and group them into projects.

    Returns (projects, discovery_cache). `projects` is
    [{"name", "repos": [{"slug", "branch"}]}], ordered most-recently-active
    first. Grouping is deterministic (name stem before the first '-') — no AI,
    no API. `client` is accepted for signature compatibility but ignored.
    """
    window_days = config["commit_window_days"]
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    repos = bitbucket.list_active_repos(session, config["workspace"], cutoff)

    branch_of = {r["slug"]: r["branch"] for r in repos}
    updated_of = {r["slug"]: r["updated_on"] for r in repos}
    slugs = list(branch_of)
    signature = _slug_signature(slugs)

    cache = old_state.get("discovery") or {}
    persist = True
    if cache.get("signature") == signature and cache.get("groups"):
        groups = cache["groups"]
    else:
        groups = _prefix_group(slugs)

    projects = []
    for name, members in groups.items():
        repos_in = [{"slug": s, "branch": branch_of[s]} for s in members if s in branch_of]
        if repos_in:
            latest = max(updated_of[r["slug"]] for r in repos_in)
            projects.append({"name": name, "repos": repos_in, "_latest": latest})
    projects.sort(key=lambda p: p["_latest"], reverse=True)
    for p in projects:
        del p["_latest"]

    discovery = {"signature": signature, "groups": groups} if persist else cache
    return projects, discovery


def perform_refresh(config: dict, env: dict, old_state: dict) -> dict:
    """Fetch commits, compute activity, regenerate summaries for changed
    projects. Pure function of its inputs — no GTK, safe on a worker thread.
    Per-repo failures are recorded, not raised; only auth failure aborts."""
    new_state = store.fresh_state()
    new_state["unseen_activity"] = old_state.get("unseen_activity", False)
    new_state["local_links"] = old_state.get("local_links", {})
    new_state["analysis"] = old_state.get("analysis", {})
    new_state["dismissed_reviews"] = old_state.get("dismissed_reviews", [])

    window_days = config["commit_window_days"]
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    local_midnight = datetime.now().astimezone().replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    session = bitbucket.make_session(env["ATLASSIAN_EMAIL"], env["BITBUCKET_API_TOKEN"])

    auth_failed = False
    try:
        # Grouping is deterministic (prefix) — no AI/API at runtime.
        projects, discovery = resolve_projects(config, session, None, old_state)
    except AuthError:
        projects, discovery = [], old_state.get("discovery") or {}
        auth_failed = True
    new_state["discovery"] = discovery

    for project in projects:
        name = project["name"]
        old_proj = old_state.get("projects", {}).get(name, {})
        old_repos = old_proj.get("repos", {})

        repos_state: dict = {}
        window_commits: dict[str, list[dict]] = {}
        commits_today = 0
        latest: datetime | None = None

        for repo in project["repos"]:
            slug, branch = repo["slug"], repo["branch"]
            old_repo = old_repos.get(slug, {})
            try:
                commits = bitbucket.fetch_recent_commits(
                    session, config["workspace"], slug, branch
                )
            except AuthError:
                auth_failed = True
                repos_state[slug] = {**old_repo, "fetch_error": "auth failed"}
                continue
            except BitbucketError as e:
                repos_state[slug] = {**old_repo, "fetch_error": e.reason}
                continue

            head = commits[0]["hash"] if commits else None
            head_date = commits[0]["date"] if commits else old_repo.get("last_date")
            repos_state[slug] = {
                "head": head,
                "branch": branch,
                "last_date": head_date,
                "fetch_error": None,
            }
            if head and head != old_repo.get("head"):
                new_state["unseen_activity"] = True

            # Idle time comes from the head commit even when it's outside the
            # summary window, so a long-dormant project shows "idle N days"
            # rather than "no data".
            head_dt = _parse_date(head_date) if head_date else None
            if head_dt and (latest is None or head_dt > latest):
                latest = head_dt

            recent = []
            for c in commits:
                d = _parse_date(c["date"])
                if d is None or d < cutoff:
                    continue
                recent.append(c)
                if d >= local_midnight:
                    commits_today += 1
            window_commits[slug] = recent

        if auth_failed:
            break

        days_idle = (datetime.now(timezone.utc) - latest).days if latest else None

        # Per-day commit counts across the window (local dates), for the
        # frontend sparkline. The commits are already in hand from the fetch
        # loop above, so this costs nothing extra.
        daily_counts: dict[str, int] = {}
        for commits in window_commits.values():
            for c in commits:
                d = _parse_date(c["date"])
                if d is None:
                    continue
                key = d.astimezone().strftime("%Y-%m-%d")
                daily_counts[key] = daily_counts.get(key, 0) + 1

        # Commit-status prose is no longer generated here — the /overboard agent
        # writes it into ai.json. We keep only the deterministic signal, incl. a
        # head signature the manager uses to detect new commits.
        head_sig = "|".join(
            f"{s}:{r.get('head')}" for s, r in sorted(repos_state.items())
        )
        new_state["projects"][name] = {
            "commits_today": commits_today,
            "days_idle": days_idle,
            "daily_counts": daily_counts,
            "head_sig": head_sig,
            "latest_commit_date": latest.isoformat(timespec="seconds") if latest else None,
            "repos": repos_state,
        }

    new_state["last_refresh"] = _now_iso()
    if auth_failed:
        new_state["last_refresh_ok"] = False
        new_state["last_error"] = "Bitbucket auth failed — token invalid or expired"
        # Keep previously known project data rather than the partial fetch.
        for name, proj in old_state.get("projects", {}).items():
            new_state["projects"].setdefault(name, proj)
        new_state["project_order"] = old_state.get("project_order") or list(
            new_state["projects"]
        )
    else:
        new_state["project_order"] = [p["name"] for p in projects]
        fetch_errors = [
            f"{slug}: {r['fetch_error']}"
            for proj in new_state["projects"].values()
            for slug, r in proj["repos"].items()
            if r.get("fetch_error")
        ]
        all_failed = fetch_errors and all(
            r.get("fetch_error")
            for proj in new_state["projects"].values()
            for r in proj["repos"].values()
        )
        new_state["last_refresh_ok"] = not all_failed
        if fetch_errors and not new_state["last_error"]:
            new_state["last_error"] = "; ".join(fetch_errors[:3])

    return new_state


def tooltip_text(state: dict) -> str:
    if not state.get("last_refresh_ok"):
        when = _parse_date(state.get("last_refresh") or "")
        stamp = when.astimezone().strftime("%H:%M") if when else "never"
        return f"{APP_TITLE} — offline, showing cached data ({stamp})"
    active = [
        (name, p["commits_today"])
        for name, p in state.get("projects", {}).items()
        if p.get("commits_today")
    ]
    if not active:
        return f"{APP_TITLE} — no commits today"
    total = sum(n for _, n in active)
    return (
        f"{APP_TITLE} — {total} new commit{'s' if total != 1 else ''} today "
        f"across {len(active)} project{'s' if len(active) != 1 else ''}"
    )


def run_once() -> int:
    env = store.load_env()
    config = store.load_config()
    state = perform_refresh(config, env, store.load_state())
    store.save_state(state)
    ai_sum = store.load_ai().get("summaries", {})

    for name in state.get("project_order", []):
        p = state["projects"].get(name)
        if p is None:
            continue
        if p["commits_today"]:
            chip = f"{p['commits_today']} commit(s) today"
        elif p["days_idle"] is not None:
            chip = f"{p['days_idle']} day(s) ago"
        else:
            chip = "no data"
        window_total = sum((p.get("daily_counts") or {}).values())
        print(f"● {name}  [{chip}]  ({window_total} commits in window)")
        summary = (ai_sum.get(name) or {}).get("text") or "(no summary yet — run /overboard)"
        print(f"  {summary}")
        errors = {s: r["fetch_error"] for s, r in p["repos"].items() if r.get("fetch_error")}
        if errors:
            print(f"  ⚠ fetch errors: {errors}")
    if state.get("last_error"):
        print(f"\nlast_error: {state['last_error']}")
    print(f"\nrefresh ok: {state['last_refresh_ok']}  state: {store.STATE_PATH}")
    return 0 if state["last_refresh_ok"] else 1


class Api:
    """JS bridge for the pywebview UI. Every method returns JSON-serializable
    data and runs on pywebview's worker thread, so blocking calls (network, AI)
    are fine — the JS side awaits them."""

    def __init__(self, config: dict, env: dict):
        self.config = config
        self.env = env
        self.state = store.load_state()
        self._refreshing = False
        self._lock = threading.Lock()
        self._analysis_lock = threading.Lock()
        self._window = None
        # First run on this machine: discover local clones so badges work
        # immediately, before any manual rescan.
        if not localrepo.links_for_machine(self.state):
            try:
                localrepo.update_state_links(self.state, self.config)
                store.save_state(self.state)
            except Exception:  # discovery must never block startup
                pass
        # Static analysis is free (no API), so pre-warm it in the background so
        # every project's details are ready without the user asking.
        self._kick_analyses()

    # ---- read -----------------------------------------------------------
    def get_view(self) -> dict:
        return self._build_view()

    def _build_view(self) -> dict:
        state = self.state
        ai = store.load_ai()
        ai_sum, ai_dig = ai.get("summaries", {}), ai.get("digests", {})
        links = localrepo.links_for_machine(state)
        analyzed = state.get("analysis", {})
        activity = state.get("activity", {})
        dismissed = set(state.get("dismissed_reviews", []))
        projects = []
        for name in state.get("project_order", []):
            p = state.get("projects", {}).get(name)
            if p is None:
                continue
            slugs = list(p.get("repos", {}))
            repos = [
                {
                    "slug": slug,
                    "branch": r.get("branch"),
                    "local_path": links.get(slug),
                    "fetch_error": r.get("fetch_error"),
                    "has_analysis": slug in analyzed,
                }
                for slug, r in p.get("repos", {}).items()
            ]
            # Live activity feed + self-reported flags (from events).
            feed, flags = [], []
            for slug in slugs:
                a = activity.get(slug)
                if not a:
                    continue
                for e in a.get("recent", []):
                    feed.append({**e, "repo": slug})
                    if e.get("type") == "flag" and e.get("note"):
                        flags.append(e["note"])
            feed.sort(key=lambda e: e.get("ts", 0), reverse=True)
            # AI content is overlaid from the agent-owned ai.json. Human-flagged
            # items come first, then the agent's digest review items. Anything
            # the user has dismissed ("OK") is filtered out.
            dig = ai_dig.get(name) or {}
            review = [
                r for r in (flags + list(dig.get("review", [])))
                if _review_key(name, r) not in dismissed
            ]
            projects.append({
                "name": name,
                "summary": (ai_sum.get(name) or {}).get("text"),
                "commits_today": p.get("commits_today") or 0,
                "days_idle": p.get("days_idle"),
                "daily_counts": p.get("daily_counts") or {},
                "repos": repos,
                "activity": feed[:25],
                "review": review[:6],
                "pm_narrative": dig.get("narrative", ""),
            })
        return {
            "projects": projects,
            "last_refresh": state.get("last_refresh"),
            "last_refresh_ok": state.get("last_refresh_ok"),
            "last_error": state.get("last_error"),
            "agent_has_run": bool(ai_sum or ai_dig or ai.get("architecture")),
            "refreshing": self._refreshing,
            "machine": localrepo.machine_key(),
            "window_days": self.config.get("commit_window_days", 30),
        }

    def _overlay_ai(self, slug: str, result: dict) -> dict:
        """Overlay the agent-owned panels (ai.json) onto a static analysis
        result. Architecture prose + Mermaid, real prompts (which replace the
        noisy static guesses — those stay only as a dim fallback), setup/run
        instructions, and key code snippets."""
        ai = store.load_ai()
        result = dict(result)

        arch = ai.get("architecture", {}).get(slug) or {}
        if arch.get("text"):
            result["architecture"] = arch["text"]
        if arch.get("mermaid"):
            result["diagrams"] = {**result.get("diagrams", {}), "architecture": arch["mermaid"]}

        pr = ai.get("prompts", {}).get(slug) or {}
        if pr.get("items"):
            result["prompts"] = pr["items"]
            result["prompts_source"] = "agent"
        else:
            result["prompts_source"] = "static"  # keyword guesses; show dimmed

        result["setup"] = (ai.get("setup", {}).get(slug) or {}).get("text", "")
        result["snippets"] = (ai.get("snippets", {}).get(slug) or {}).get("items", [])
        return result

    def get_analysis(self, slug: str):
        cached = self.state.get("analysis", {}).get(slug)
        return self._overlay_ai(slug, cached) if cached else None

    # ---- actions --------------------------------------------------------
    def refresh(self) -> dict:
        with self._lock:
            if self._refreshing:
                return self._build_view()
            self._refreshing = True
        try:
            new_state = perform_refresh(self.config, self.env, self.state)
            self.state = new_state
            manager.update_activity(self.state, self.env)  # fold in live activity
            store.save_state(self.state)
        except Exception as e:  # never let the UI hang on a failed refresh
            self.state["last_refresh_ok"] = False
            self.state["last_error"] = f"refresh failed: {e}"
        finally:
            self._refreshing = False
        # New commits may have moved a clone's HEAD — re-run analysis in the
        # background so details stay current without a button.
        self._kick_analyses()
        return self._build_view()

    def tick(self) -> dict:
        """Cheap periodic poll: fold in new activity events (event-gated digest,
        no Bitbucket network). Safe to call on a short interval."""
        try:
            if manager.update_activity(self.state, self.env):
                store.save_state(self.state)
        except Exception as e:
            self.state["last_error"] = f"activity update failed: {e}"
        return self._build_view()

    def rescan_local(self) -> dict:
        localrepo.update_state_links(self.state, self.config)
        store.save_state(self.state)
        return self._build_view()

    def dismiss_review(self, project: str, text: str) -> dict:
        """Mark a review item as handled ("OK") so it stops showing."""
        key = _review_key(project, text)
        lst = self.state.setdefault("dismissed_reviews", [])
        if key not in lst:
            lst.append(key)
            store.save_state(self.state)
        return self._build_view()

    def analyze(self, slug: str) -> dict:
        """Run analysis for one repo's local clone, reusing the cached result
        when the clone's HEAD is unchanged."""
        path = localrepo.links_for_machine(self.state).get(slug)
        if not path:
            return {"error": f"no local clone for {slug} on this machine"}
        head = analysis.git_head(Path(path))
        cached = self.state.get("analysis", {}).get(slug)
        if (cached and head and cached.get("head") == head
                and cached.get("analyzer_version") == analysis._ANALYZER_VERSION):
            return self._overlay_ai(slug, cached)
        try:
            result = analysis.analyze_repo(path, slug)  # static only — no API
        except Exception as e:
            return {"error": f"analysis failed: {e}"}
        self.state.setdefault("analysis", {})[slug] = result
        store.save_state(self.state)
        return self._overlay_ai(slug, result)

    def _kick_analyses(self) -> None:
        """Spawn a background pass that analyzes every local clone whose cache is
        missing or stale (HEAD moved / analyzer bumped). Static-only, no API, so
        it's safe to run unattended; the frontend just reads the cached result."""
        threading.Thread(target=self._ensure_analyses, daemon=True).start()

    def _ensure_analyses(self) -> None:
        if not self._analysis_lock.acquire(blocking=False):
            return  # one analyzer at a time is enough
        try:
            links = localrepo.links_for_machine(self.state)
            changed = False
            for slug, path in links.items():
                try:
                    head = analysis.git_head(Path(path))
                except Exception:
                    continue
                cached = self.state.get("analysis", {}).get(slug)
                if (cached and head and cached.get("head") == head
                        and cached.get("analyzer_version") == analysis._ANALYZER_VERSION):
                    continue  # up to date
                try:
                    result = analysis.analyze_repo(path, slug)  # static only
                except Exception:
                    continue
                self.state.setdefault("analysis", {})[slug] = result
                changed = True
            if changed:
                store.save_state(self.state)
        finally:
            self._analysis_lock.release()

    def open_terminal(self, path: str) -> bool:
        if not path or not Path(path).is_dir():
            return False
        return _open_terminal(path)


def _make_handler(api: "Api"):
    from http.server import BaseHTTPRequestHandler

    web_dir = Path(__file__).resolve().parent / "web"
    ALLOWED = {"get_view", "refresh", "tick", "rescan_local", "analyze",
               "get_analysis", "open_terminal", "dismiss_review"}
    CONTENT_TYPES = {
        ".html": "text/html", ".js": "text/javascript", ".css": "text/css",
        ".json": "application/json", ".svg": "image/svg+xml",
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # keep stdout/stderr quiet
            pass

        def _json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _file(self, rel):
            f = (web_dir / rel).resolve()
            if not (f == web_dir or web_dir in f.parents) or not f.is_file():
                self.send_error(404)
                return
            data = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPES.get(f.suffix, "application/octet-stream"))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/":
                path = "/index.html"
            if path == "/api/view":
                self._json(api.get_view())
                return
            self._file(path.lstrip("/"))

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            if not path.startswith("/api/"):
                self._json({"error": "not found"}, 404)
                return
            method = path[len("/api/"):]
            if method not in ALLOWED:
                self._json({"error": f"forbidden: {method}"}, 403)
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                args = json.loads(raw) if raw else {}
            except ValueError:
                args = {}
            try:
                result = getattr(api, method)(**args)
            except Exception as e:
                self._json({"error": str(e)}, 500)
                return
            self._json(result)

    return Handler


def _open_terminal(path: str) -> bool:
    """Open a terminal emulator with its working directory at `path`. Returns
    True if something was launched. macOS is the primary target (prefers iTerm,
    falls back to Terminal); on Linux the first available emulator wins."""
    if sys.platform == "darwin":
        app = "iTerm" if os.path.isdir("/Applications/iTerm.app") else "Terminal"
        cmd = ["open", "-a", app, path]
    else:
        # (binary, extra args) tried in order; the first on PATH is used.
        candidates = [
            ("gnome-terminal", ["--working-directory=" + path]),
            ("konsole", ["--workdir", path]),
            ("xfce4-terminal", ["--working-directory=" + path]),
            ("tilix", ["--working-directory=" + path]),
            ("terminator", ["--working-directory=" + path]),
            ("kitty", ["--directory", path]),
            ("alacritty", ["--working-directory", path]),
            ("x-terminal-emulator", ["--working-directory=" + path]),  # last resort
            ("xterm", ["-e", "bash", "-c", f"cd {shlex.quote(path)}; exec bash"]),
        ]
        cmd = None
        for binary, args in candidates:
            if shutil.which(binary):
                cmd = [binary, *args]
                break
        if cmd is None:
            return False
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError:
        return False


def _open_url(url: str) -> None:
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif sys.platform.startswith("linux"):
            subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            import webbrowser
            webbrowser.open(url)
    except OSError:
        pass


def run_dashboard(config: dict, env: dict, port: int, prefer_window: bool) -> int:
    """Serve the dashboard over stdlib HTTP (zero deps). Opens a native
    pywebview window if that package happens to be installed and wanted, else
    the default browser."""
    from http.server import ThreadingHTTPServer

    api = Api(config, env)
    url = f"http://localhost:{port}"
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), _make_handler(api))
    except OSError:
        _open_url(url)  # already running — just point a browser at it
        print(f"Overboard dashboard already running at {url}")
        return 0

    if prefer_window:
        try:
            import webview  # optional native window
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            webview.create_window(APP_TITLE, url=url, width=560, height=820, min_size=(400, 480))
            webview.start()
            return 0
        except ImportError:
            pass

    _open_url(url)
    print(f"Overboard dashboard: {url}  (Ctrl-C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def _port_from_argv(default: int = 8787) -> int:
    if "--port" in sys.argv:
        i = sys.argv.index("--port")
        if i + 1 < len(sys.argv):
            try:
                return int(sys.argv[i + 1])
            except ValueError:
                pass
    return default


def main() -> int:
    if "--once" in sys.argv:
        return run_once()
    try:
        env = store.load_env()
        config = store.load_config()
    except store.ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    # --serve: headless server (spawned by the MCP launch_dashboard tool) —
    # opens a browser tab. Default (direct run): native window if pywebview is
    # installed, otherwise browser.
    prefer_window = "--serve" not in sys.argv
    return run_dashboard(config, env, _port_from_argv(), prefer_window)


if __name__ == "__main__":
    sys.exit(main())
