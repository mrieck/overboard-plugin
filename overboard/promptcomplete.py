"""Completion for the command/prompt fields (slot editor, dispatcher): slash
commands and filesystem paths, plus the "that plugin isn't enabled here"
check. A port of the Mac app's Prompt/ folder (PromptToken, SlashCommandCatalog,
PathCompleter, PluginEnablementResolver, PromptCompletionModel).

Slash commands come from Claude Code's own conventions — ~/.claude/commands
(user), <cwd>/.claude/commands (project), and every installed plugin's
commands/**.md + skills/*/SKILL.md — where a subdirectory namespaces the name
("frontend/component.md" → "frontend:component") and a plugin command is
canonically "plugin:name" with the bare name kept as an alias. Only the
prompt's FIRST token can be a slash command (that's how claude parses one);
"~/", "./" and absolute "/" tokens complete as paths anywhere.

Pure functions over injected listers where it matters, so tests never touch
the disk; one cached filesystem scan (per cwd, 30s) for the catalog.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

from . import claudeplugins

CATALOG_TTL = 30
SUGGESTION_LIMIT = 10
_NAME_RE = re.compile(r"^[A-Za-z0-9_:\-]*$")
_catalog_cache: dict = {}   # cwd -> (monotonic, [commands])


# ---- tokens (PromptTokenizer) ---------------------------------------------
def command_name(token: str) -> "str | None":
    """"/frontend:component" → "frontend:component"; None when the token isn't
    command-shaped (a second "/" as in /usr/bin, or any other character, means
    it's a path or prose)."""
    if not token.startswith("/"):
        return None
    name = token[1:]
    return name if _NAME_RE.match(name) else None


def active_token(text: str, cursor: int) -> "dict | None":
    """The completable token containing the caret: {"kind": "slash"|"path",
    "partial": str, "start": int, "end": int}. Tokens are whitespace-
    delimited, so paths with spaces aren't completable — accepted; the run
    launcher has no quoting rules either."""
    text = text or ""
    cursor = max(0, min(len(text), int(cursor or 0)))
    start = cursor
    while start > 0 and not text[start - 1].isspace():
        start -= 1
    if start >= cursor:
        return None
    token = text[start:cursor]
    if start == 0:
        name = command_name(token)
        if name is not None:
            return {"kind": "slash", "partial": name, "start": start, "end": cursor}
    if token.startswith("~/") or token.startswith("./") or token.startswith("/"):
        return {"kind": "path", "partial": token, "start": start, "end": cursor}
    return None


def leading_command(text: str) -> "tuple[str, bool] | None":
    """The prompt's leading slash command as (name, terminated) — `terminated`
    means whitespace follows it (the user has moved on). None when the text
    doesn't start with a command-shaped token."""
    text = text or ""
    if not text.startswith("/"):
        return None
    m = re.match(r"\S+", text)
    token = m.group(0) if m else ""
    name = command_name(token)
    if name is None:
        return None
    return name, len(token) < len(text)


# ---- catalog (SlashCommandDeriver + the filesystem walk) -------------------
def name_from_relative_path(rel: str) -> "str | None":
    """"frontend/component.md" → "frontend:component"; "deploy.md" → "deploy"."""
    if not rel.endswith(".md"):
        return None
    trimmed = rel[:-3]
    if not trimmed:
        return None
    return ":".join(p for p in trimmed.split("/") if p)


def skill_name(frontmatter: "str | None", directory: str) -> str:
    """A skill's invocation name: frontmatter `name:` if present, else the
    skill directory. Frontmatter is the leading "---\\n…\\n---" block."""
    if not frontmatter or not frontmatter.startswith("---"):
        return directory
    for line in frontmatter.split("\n")[1:]:
        if line.startswith("---"):
            break
        key, sep, value = line.partition(":")
        if sep and key.strip() == "name" and value.strip():
            return value.strip()
    return directory


def plugin_commands(plugin_id: str, plugin_name: str, install_path: str,
                    command_files: list, skills: list) -> list:
    """A plugin's commands: each commands/**.md and each skills/<dir>/SKILL.md,
    canonically "pluginName:name" (what gets inserted — the bare name is only
    honoured by claude when nothing else shares it) with the bare name as an
    alias so typing "/revi" still finds "reviewer:review"."""
    out = []
    for rel in command_files:
        name = name_from_relative_path(rel)
        if name is None:
            continue
        out.append({"name": f"{plugin_name}:{name}", "aliases": [name], "source": "plugin",
                    "plugin_id": plugin_id, "plugin_name": plugin_name,
                    "file": f"{install_path}/commands/{rel}"})
    for directory, frontmatter in skills:
        name = skill_name(frontmatter, directory)
        out.append({"name": f"{plugin_name}:{name}", "aliases": [name], "source": "skill",
                    "plugin_id": plugin_id, "plugin_name": plugin_name,
                    "file": f"{install_path}/skills/{directory}/SKILL.md"})
    return out


def _markdown_files(root: Path) -> list:
    """Relative paths of every .md under root (sorted); [] when absent."""
    out = []
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for fn in sorted(filenames):
                if fn.endswith(".md"):
                    out.append(os.path.relpath(os.path.join(dirpath, fn), root))
    except OSError:
        return []
    return out


def _skill_entries(root: Path) -> list:
    out = []
    try:
        dirs = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return []
    for d in dirs:
        skill = d / "SKILL.md"
        if not skill.is_file():
            continue
        try:
            head = skill.read_text(errors="replace")[:2000]
        except OSError:
            head = None
        out.append((d.name, head))
    return out


def scan_catalog(cwd: "str | None", home: "Path | None" = None,
                 inventory: "dict | None" = None) -> list:
    """Every invocable slash command, uncached: user, project (for `cwd`), and
    each installed plugin (first install path that exists)."""
    home = home or Path.home()
    commands = []
    user_dir = home / ".claude" / "commands"
    for rel in _markdown_files(user_dir):
        name = name_from_relative_path(rel)
        if name:
            commands.append({"name": name, "aliases": [], "source": "user",
                             "plugin_id": None, "plugin_name": None,
                             "file": str(user_dir / rel)})
    if cwd:
        proj_dir = Path(cwd).expanduser() / ".claude" / "commands"
        for rel in _markdown_files(proj_dir):
            name = name_from_relative_path(rel)
            if name:
                commands.append({"name": name, "aliases": [], "source": "project",
                                 "plugin_id": None, "plugin_name": None,
                                 "file": str(proj_dir / rel)})
    inv = inventory if inventory is not None else claudeplugins.inventory()
    for plugin_id, p in sorted((inv.get("plugins") or {}).items()):
        root = None
        for inst in p.get("installs") or []:
            ip = inst.get("install_path")
            if ip and Path(ip).expanduser().is_dir():
                root = Path(ip).expanduser()
                break
        if root is None:
            continue
        commands.extend(plugin_commands(
            plugin_id, p.get("name") or plugin_id.partition("@")[0], str(root),
            _markdown_files(root / "commands"), _skill_entries(root / "skills")))
    return commands


def catalog(cwd: "str | None") -> list:
    key = str(Path(cwd).expanduser()) if cwd else ""
    now = time.monotonic()
    hit = _catalog_cache.get(key)
    if hit and now - hit[0] < CATALOG_TTL:
        return hit[1]
    commands = scan_catalog(cwd)
    _catalog_cache[key] = (now, commands)
    return commands


def invalidate_catalog() -> None:
    _catalog_cache.clear()


def command_matches(cmd: dict, prefix: str) -> bool:
    p = prefix.lower()
    if cmd["name"].lower().startswith(p):
        return True
    return any(a.lower().startswith(p) for a in cmd.get("aliases") or [])


def exact_command(commands: list, name: str) -> "dict | None":
    low = name.lower()
    return next((c for c in commands
                 if c["name"].lower() == low
                 or any(a.lower() == low for a in c.get("aliases") or [])), None)


def command_suggestions(commands: list, prefix: str, limit: int = SUGGESTION_LIMIT) -> list:
    out = []
    for cmd in sorted(commands, key=lambda c: c["name"]):
        if not command_matches(cmd, prefix):
            continue
        tag = cmd["plugin_name"] if cmd["source"] in ("plugin", "skill") else cmd["source"]
        out.append({"insertion": "/" + cmd["name"] + " ", "label": "/" + cmd["name"],
                    "detail": tag, "plugin_id": cmd.get("plugin_id")})
        if len(out) >= limit:
            break
    return out


# ---- paths (PathCompleter) -------------------------------------------------
def default_lister(directory: str) -> list:
    """One shallow directory read → [(name, is_dir)]; [] on any trouble."""
    try:
        with os.scandir(directory) as it:
            out = []
            for e in it:
                try:
                    out.append((e.name, e.is_dir()))
                except OSError:
                    out.append((e.name, False))
            return out
    except OSError:
        return []


def path_completions(partial: str, primary_cwd: str, lister=default_lister,
                     limit: int = SUGGESTION_LIMIT) -> list:
    """Completions for a path token in the user's own spelling — "~/pro" →
    "~/projects/", never the expanded home, because the result replaces the
    token verbatim. Directories gain a trailing "/" and sort first. "./"
    resolves against `primary_cwd`."""
    slash = partial.rfind("/")
    if slash < 0:
        return []
    dir_part = partial[:slash + 1]          # keeps the trailing "/"
    prefix = partial[slash + 1:]
    if dir_part.startswith("./"):
        list_dir = (primary_cwd or ".") + dir_part[1:]
    elif dir_part == "/":
        list_dir = "/"
    else:
        list_dir = os.path.expanduser(dir_part)
    if len(list_dir) > 1 and list_dir.endswith("/"):
        list_dir = list_dir[:-1]
    show_hidden = prefix.startswith(".")
    low = prefix.lower()
    entries = [(n, d) for n, d in lister(list_dir)
               if (show_hidden or not n.startswith(".")) and (not low or n.lower().startswith(low))]
    entries.sort(key=lambda e: (not e[1], e[0].lower()))
    return [dir_part + n + ("/" if d else "") for n, d in entries[:limit]]


# ---- enablement (PluginEnablementResolver) ---------------------------------
def enablement_maps(cwd: "str | None") -> dict:
    """{"user": map, "project": map, "local": map} of enabledPlugins."""
    out = {"user": claudeplugins._enabled_plugins(claudeplugins.USER_SETTINGS_PATH),
           "project": {}, "local": {}}
    if cwd:
        for scope, enabled in claudeplugins._scan_dir(cwd):
            out[scope] = enabled
    return out


def is_enabled(plugin_id: str, maps: dict) -> bool:
    """local > project > user — the most specific file that mentions the
    plugin wins; unmentioned means disabled."""
    for scope in ("local", "project", "user"):
        m = maps.get(scope) or {}
        if plugin_id in m:
            return bool(m[plugin_id])
    return False


# ---- the two API-facing entry points ---------------------------------------
def complete(text: str, cursor: int, cwd: "str | None") -> dict:
    tok = active_token(text, cursor)
    if tok is None:
        return {"kind": None, "items": []}
    if tok["kind"] == "slash":
        items = command_suggestions(catalog(cwd), tok["partial"])
    else:
        items = [{"insertion": p, "label": p, "detail": "dir" if p.endswith("/") else None}
                 for p in path_completions(tok["partial"], cwd or os.getcwd())]
    return {"kind": tok["kind"], "start": tok["start"], "end": tok["end"], "items": items}


def enablement(text: str, cwd: "str | None") -> dict:
    """For the notice under a prompt field: the leading command's plugin and
    whether it's enabled in `cwd`. {"ok": True} when nothing to say; `unknown`
    when the name matches no command (claude will say "Unknown command")."""
    lead = leading_command(text)
    if lead is None:
        return {"ok": True}
    name, terminated = lead
    commands = catalog(cwd)
    cmd = exact_command(commands, name)
    if cmd is None:
        return {"ok": True, "unknown": True, "command": name} if terminated else {"ok": True}
    if not cmd.get("plugin_id"):
        return {"ok": True, "command": cmd["name"]}
    enabled = is_enabled(cmd["plugin_id"], enablement_maps(cwd))
    return {"ok": enabled, "command": cmd["name"], "plugin_id": cmd["plugin_id"],
            "plugin_name": cmd["plugin_name"], "folder": cwd or "",
            "folder_name": os.path.basename(os.path.normpath(cwd)) if cwd else ""}
