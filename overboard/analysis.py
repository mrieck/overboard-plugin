"""Per-repo intelligence: reads a *local* clone and produces
- prompt detection (LLM prompts / SDK usage found in source) — static, no tokens
- DB schema shape (SQL / ORM models / Prisma)            — static, no tokens
- an architecture overview                               — AI (Sonnet 4.6)

Caching is the caller's job: results carry the repo's git HEAD so a re-run only
happens when HEAD changes (see app.Api.analyze)."""

from __future__ import annotations

import ast
import os
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from overboard import debuglog, diagram

# Directories we never scan.
_SKIP_DIRS = {
    ".git", "node_modules", "venv", ".venv", "__pycache__", "dist", "build",
    "site-packages", "vendor", "target", ".cache", ".next", ".nuxt", "coverage",
}
# Extensions worth reading as text.
_TEXT_EXTS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs", ".rb", ".go", ".java",
    ".php", ".rs", ".kt", ".swift", ".cs", ".cpp", ".c", ".h", ".vue", ".svelte",
    ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".sql", ".prisma", ".sh",
    ".html",
}
_MAX_FILE_BYTES = 512 * 1024
_MAX_PROMPT_FINDINGS = 200
_MAX_DB_TABLES = 200
_ENTRYPOINT_NAMES = {
    "main.py", "app.py", "__main__.py", "manage.py", "wsgi.py", "asgi.py",
    "index.js", "index.ts", "server.js", "server.ts", "main.go", "main.rs",
    "package.json", "pyproject.toml", "Cargo.toml", "go.mod", "Dockerfile",
    "docker-compose.yml", "requirements.txt",
}

# Bump when the analyzers change, so cached results are recomputed.
_ANALYZER_VERSION = 4

# --- prompt extraction ------------------------------------------------------
# Goal: the actual prompt *text* written for an LLM (system prompts, templates,
# instructions), not SDK plumbing. Python is parsed via AST; other languages
# fall back to extracting template-literal / multiline strings. Dynamically
# built prompts (f-strings, concatenation, .format) are shown as the code that
# builds them, flagged `dynamic`.
_PROMPT_NAME = re.compile(
    r"(?i)(prompt|instruction|system|template|directive|persona|guideline|rubric|preamble)"
)
_LLM_CALL_NAMES = {"create", "generate_content", "complete", "completion", "generate", "invoke"}
_PROMPT_KWARGS = {"system", "prompt", "instructions", "input", "system_prompt"}
_MAX_PROMPT_TEXT = 4000
# Word-boundary matched (so "created" does NOT match "create"); \w* on verb
# stems catches inflections (summarize/summarizing, generate/generated).
_PROMPT_KW_RE = re.compile(
    r"\b(you are|you must|your task|your role|act as|step by step|based on|do not|"
    r"respond|repl(?:y|ies)|return|translat\w*|explain|summari\w*|instruction\w*|"
    r"generat\w*|provid\w*|consider|follow|assistant|persona|rubric|guidance|tutor|"
    r"output|format|given|please|write|create|select|extract)\b",
    re.I,
)
_BACKTICK_RE = re.compile(r"`([^`\\]*(?:\\.[^`\\]*)*)`", re.DOTALL)
_TRIPLE_RE = re.compile(r"(?:\"\"\"|''')(.*?)(?:\"\"\"|''')", re.DOTALL)

# Strings that are code/markup/data, not prose written for an LLM. SQL is
# matched case-sensitively (real SQL keywords are uppercase; prose like
# "Create a lesson plan" / "Select the best answer" must survive).
_SQL_RE = re.compile(
    r"^(SELECT\b[\s\S]{0,4000}?\bFROM\b|INSERT\s+INTO\b|UPDATE\b[\s\S]{0,200}?\bSET\b"
    r"|DELETE\s+FROM\b|CREATE\s+(TABLE|INDEX|VIEW|TRIGGER|TEMP)\b|ALTER\s+TABLE\b"
    r"|DROP\s+(TABLE|INDEX)\b|PRAGMA\b|BEGIN\s+TRANSACTION\b)"
)
_MARKUP_RE = re.compile(
    r"^<(!doctype|html|div|table|tr|td|span|head|body|meta|script|style|a\s|button|p[>\s]|ul|section|nav|form|img)",
    re.I,
)
_DATA_RE = re.compile(r"^[\{\[]")  # JSON object/array literal, or [log] lines
_LANGTAG_RE = re.compile(
    r"^(json|bash|sh|shell|sql|html|css|xml|yaml|yml|javascript|typescript|python|diff)\s*\n",
    re.I,
)
_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")
_CSS_RE = re.compile(r"^[\w-]+\s*:\s*[^;\n]+;")  # position:absolute; ...
_CODE_TOKENS = re.compile(
    r"(=>|\bconst\b|\blet\b|\bawait\b|\bfunction\b|console\.|JSON\.(parse|stringify)"
    r"|\.insert\(|\.values\(|catch\s*\(|\);)"
)
# Fragments that begin with punctuation are almost always sliced-up code, not
# prose written for a model.
_CODE_LEAD = set("$,.;{}()[]=>|&`" + "\\")


def _is_codeish(s: str) -> bool:
    st = s.lstrip()
    if not st:
        return True
    if st[0] in _CODE_LEAD:
        return True
    if (_SQL_RE.match(st) or _MARKUP_RE.match(st) or _DATA_RE.match(st)
            or _LANGTAG_RE.match(st) or _CSS_RE.match(st)):
        return True
    # HTML/JSX fragments carry several tags; code fragments carry several
    # source tokens — prose prompts carry neither.
    return (len(_TAG_RE.findall(st[:2000])) >= 2
            or len(_CODE_TOKENS.findall(st[:1500])) >= 2)


def _looks_like_prompt(s: str) -> bool:
    st = s.strip()
    if len(st) < 40 or len(st.split()) < 6:
        return False
    if _is_codeish(st):
        return False
    return bool(_PROMPT_KW_RE.search(st))


def _node_name(target) -> str | None:
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    return None


def _call_name(call: ast.Call) -> str | None:
    f = call.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return None


def _joined_prompty(node: ast.JoinedStr) -> bool:
    parts = [
        v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else " {…} "
        for v in node.values
    ]
    return _looks_like_prompt("".join(parts))


def _scan_python_prompts(src: str, rel: str) -> list[dict]:
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError):
        return _scan_generic_prompts(src, rel, ".py")

    out: list[dict] = []
    seen: set = set()

    def emit(node, name, kind):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text, dynamic = node.value, False
        elif isinstance(node, ast.Name):
            return  # bare reference to a prompt defined elsewhere — skip
        else:
            text = ast.get_source_segment(src, node) or ""
            dynamic = True
        text = text.strip()
        if len(text) < 20 or _is_codeish(text):
            return
        line = getattr(node, "lineno", 1)
        key = (rel, line, text[:80])
        if key in seen:
            return
        seen.add(key)
        out.append({
            "file": rel, "line": line, "name": name or "",
            "kind": kind, "dynamic": dynamic, "text": text[:_MAX_PROMPT_TEXT],
        })

    def prompty(name, value):
        if name and _PROMPT_NAME.search(name):
            return True
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return _looks_like_prompt(value.value)
        if isinstance(value, ast.JoinedStr):
            return _joined_prompty(value)
        return False

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            name = next((_node_name(t) for t in node.targets if _node_name(t)), None)
            if prompty(name, node.value):
                emit(node.value, name, "assign")
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            name = _node_name(node.target)
            if prompty(name, node.value):
                emit(node.value, name, "assign")
        elif isinstance(node, ast.AugAssign):  # prompt += "..."
            name = _node_name(node.target)
            if name and _PROMPT_NAME.search(name):
                emit(node.value, name, "append")
        elif isinstance(node, ast.Return) and node.value is not None:
            v = node.value
            if isinstance(v, ast.Constant) and isinstance(v.value, str) and _looks_like_prompt(v.value):
                emit(v, "return", "return")
            elif isinstance(v, ast.JoinedStr) and _joined_prompty(v):
                emit(v, "return", "return")
        elif isinstance(node, ast.Call) and _call_name(node) in _LLM_CALL_NAMES:
            for kw in node.keywords:
                if kw.arg in _PROMPT_KWARGS:
                    emit(kw.value, (kw.arg or "") + "=", "arg")
                elif kw.arg == "messages":
                    for sub in ast.walk(kw.value):
                        if (isinstance(sub, ast.Constant) and isinstance(sub.value, str)
                                and _looks_like_prompt(sub.value)):
                            emit(sub, "message", "message")
        if len(out) >= _MAX_PROMPT_FINDINGS:
            break
    return out


def _scan_generic_prompts(src: str, rel: str, ext: str) -> list[dict]:
    out: list[dict] = []
    seen: set = set()
    patterns = [_TRIPLE_RE] if ext == ".py" else [_BACKTICK_RE, _TRIPLE_RE]
    for pat in patterns:
        for m in pat.finditer(src):
            text = m.group(1).strip()
            if len(text) < 20 or _is_codeish(text):
                continue
            line_start = src.rfind("\n", 0, m.start()) + 1
            line_no = src.count("\n", 0, m.start()) + 1
            prefix = src[line_start:m.start()]
            nmm = re.search(r"(\w+)\s*[:=]\s*[^=]*$", prefix)
            name = nmm.group(1) if nmm else ""
            # Admit on prose signal only — JS variable-name guessing is too
            # unreliable to gate on. The name is kept just for display.
            if not _looks_like_prompt(text):
                continue
            key = (rel, line_no, text[:80])
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "file": rel, "line": line_no, "name": name, "kind": "template",
                "dynamic": "${" in text,
                "text": text[:_MAX_PROMPT_TEXT],
            })
            if len(out) >= _MAX_PROMPT_FINDINGS:
                return out
    return out


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def git_head(repo_dir: Path) -> str | None:
    """Resolve the current HEAD commit hash by reading .git files only."""
    head = repo_dir / ".git" / "HEAD"
    try:
        content = head.read_text().strip()
    except OSError:
        return None
    if content.startswith("ref:"):
        ref = content.split(":", 1)[1].strip()
        ref_path = repo_dir / ".git" / ref
        try:
            return ref_path.read_text().strip()
        except OSError:
            # Packed refs fallback.
            packed = repo_dir / ".git" / "packed-refs"
            try:
                for line in packed.read_text().splitlines():
                    if line.endswith(ref):
                        return line.split()[0]
            except OSError:
                return None
            return None
    return content or None


def _iter_text_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")
        ]
        for f in filenames:
            if Path(f).suffix.lower() in _TEXT_EXTS:
                full = Path(dirpath) / f
                try:
                    if full.stat().st_size > _MAX_FILE_BYTES:
                        continue
                except OSError:
                    continue
                yield full


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


def scan_prompts(root: Path) -> list[dict]:
    """Extract prompt text written for LLMs — system prompts, templates,
    instructions — plus the code that builds dynamically-assembled prompts."""
    findings: list[dict] = []
    for path in _iter_text_files(root):
        text = _read_text(path)
        if not text:
            continue
        rel = str(path.relative_to(root))
        if path.suffix.lower() == ".py":
            findings.extend(_scan_python_prompts(text, rel))
        else:
            findings.extend(_scan_generic_prompts(text, rel, path.suffix.lower()))
        if len(findings) >= _MAX_PROMPT_FINDINGS:
            break
    return findings[:_MAX_PROMPT_FINDINGS]


# --- DB schema shape --------------------------------------------------------
_SQL_TABLE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"`']?(\w+)[\"`']?\s*\((.*?)\);",
    re.IGNORECASE | re.DOTALL,
)
_PRISMA_MODEL = re.compile(r"\bmodel\s+(\w+)\s*\{(.*?)\}", re.DOTALL)
_DJANGO_CLASS = re.compile(r"class\s+(\w+)\s*\([^)]*models\.Model[^)]*\)\s*:")
_SQLALCHEMY_CLASS = re.compile(r"class\s+(\w+)\s*\([^)]*Base[^)]*\)\s*:")
_SQL_CONSTRAINT_KW = {
    "primary", "foreign", "unique", "constraint", "key", "check", "index",
}


def _sql_columns(body: str) -> list[str]:
    cols = []
    depth = 0
    current = ""
    # Split on top-level commas only (ignore commas inside type parens).
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            cols.append(current)
            current = ""
        else:
            current += ch
    cols.append(current)
    names = []
    for c in cols:
        tokens = c.strip().split()
        if not tokens:
            continue
        first = tokens[0].strip('"`\'').lower()
        if first in _SQL_CONSTRAINT_KW or not re.match(r"^\w+$", first):
            continue
        names.append(tokens[0].strip('"`\''))
    return names[:40]


def scan_db_schema(root: Path) -> list[dict]:
    """Extract table/model shape from SQL, Prisma, and Python ORM files."""
    tables: list[dict] = []
    for path in _iter_text_files(root):
        ext = path.suffix.lower()
        if ext not in {".sql", ".prisma", ".py"}:
            continue
        text = _read_text(path)
        if not text:
            continue
        rel = str(path.relative_to(root))

        if ext == ".sql":
            for m in _SQL_TABLE.finditer(text):
                tables.append({
                    "name": m.group(1), "kind": "sql",
                    "source": rel, "columns": _sql_columns(m.group(2)),
                })
        elif ext == ".prisma":
            for m in _PRISMA_MODEL.finditer(text):
                fields = []
                for line in m.group(2).splitlines():
                    tok = line.strip().split()
                    if tok and re.match(r"^\w+$", tok[0]) and not tok[0].startswith("@"):
                        fields.append(tok[0])
                tables.append({
                    "name": m.group(1), "kind": "prisma",
                    "source": rel, "columns": fields[:40],
                })
        else:  # .py — Django / SQLAlchemy heuristics
            lines = text.splitlines()
            for regex, kind in ((_DJANGO_CLASS, "django"), (_SQLALCHEMY_CLASS, "sqlalchemy")):
                for m in regex.finditer(text):
                    start = text[: m.start()].count("\n")
                    cols = []
                    for line in lines[start + 1:]:
                        if line and not line[0].isspace():
                            break  # dedented out of the class
                        fm = re.match(r"\s+(\w+)\s*=\s*(?:models\.\w+|Column|relationship)", line)
                        if fm:
                            cols.append(fm.group(1))
                    if cols:
                        tables.append({
                            "name": m.group(1), "kind": kind,
                            "source": rel, "columns": cols[:40],
                        })
        if len(tables) >= _MAX_DB_TABLES:
            break
    return tables[:_MAX_DB_TABLES]


# --- architecture manifest + AI overview -----------------------------------
def build_manifest(root: Path) -> dict:
    exts: Counter = Counter()
    per_top: Counter = Counter()
    entrypoints: list[str] = []
    total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")
        ]
        rel_dir = os.path.relpath(dirpath, root)
        top = "." if rel_dir == "." else rel_dir.split(os.sep)[0]
        for f in filenames:
            total += 1
            exts[Path(f).suffix.lower() or "(none)"] += 1
            per_top[top] += 1
            if f in _ENTRYPOINT_NAMES:
                entrypoints.append(os.path.join(rel_dir, f) if rel_dir != "." else f)

    readme_head = ""
    for name in ("README.md", "README.txt", "README"):
        p = root / name
        if p.exists():
            txt = _read_text(p) or ""
            readme_head = "\n".join(txt.splitlines()[:40])[:2000]
            break

    structure = [
        {"dir": d, "files": n}
        for d, n in sorted(per_top.items(), key=lambda kv: -kv[1])
    ]
    return {
        "total_files": total,
        "top_exts": dict(exts.most_common(12)),
        "entrypoints": sorted(entrypoints)[:20],
        "structure": structure,
        "readme_head": readme_head,
    }


def manifest_digest(manifest: dict) -> str:
    lines = [
        f"Total files: {manifest['total_files']}",
        "File types: " + ", ".join(f"{e}={n}" for e, n in manifest["top_exts"].items()),
        "Top-level layout (dir=file count): "
        + ", ".join(f"{s['dir']}={s['files']}" for s in manifest["structure"][:15]),
        "Entrypoints: " + (", ".join(manifest["entrypoints"]) or "(none obvious)"),
    ]
    if manifest["readme_head"]:
        lines += ["", "README (head):", manifest["readme_head"]]
    return "\n".join(lines)


def analyze_repo(path: str, name: str = "") -> dict:
    """Run the static scans against a local clone. No API — the architecture
    *prose* is written later by the /overboard agent into ai.json; `manifest`
    is returned so the agent has the digest it needs to write it."""
    root = Path(path)
    label = name or root.name
    with debuglog.timer(f"  analyze[{label}]: build manifest (walk files)"):
        manifest = build_manifest(root)
    with debuglog.timer(f"  analyze[{label}]: scan prompts"):
        prompts = scan_prompts(root)
    with debuglog.timer(f"  analyze[{label}]: scan db schema"):
        db = scan_db_schema(root)
    with debuglog.timer(f"  analyze[{label}]: build diagrams"):
        diagrams = {
            "er": diagram.er_diagram(db),
            "architecture": diagram.architecture_graph(manifest, label),
        }

    return {
        "head": git_head(root),
        "analyzer_version": _ANALYZER_VERSION,
        "generated_at": _now_iso(),
        "architecture": None,  # filled by the agent (ai.json)
        "prompts": prompts,
        "db": db,
        "diagrams": diagrams,
        "structure": manifest["structure"],
        "stats": {"files": manifest["total_files"], "by_ext": manifest["top_exts"]},
        "manifest_digest": manifest_digest(manifest),
    }


if __name__ == "__main__":
    import json
    import sys

    from overboard import localrepo, store

    if len(sys.argv) < 2:
        print("usage: python -m overboard.analysis <slug>")
        sys.exit(2)
    slug = sys.argv[1]
    config = store.load_config()
    links = localrepo.discover(config.get("local_roots") or localrepo.DEFAULT_ROOTS,
                               config.get("workspace"))
    path = links.get(slug)
    if not path:
        print(f"no local clone found for {slug!r} (known: {', '.join(sorted(links)) or 'none'})")
        sys.exit(1)

    print(json.dumps(analyze_repo(path, slug), indent=2))
