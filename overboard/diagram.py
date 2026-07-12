"""Turn structured analysis output into Mermaid diagram text.

Deterministic and token-free: the frontend renders the returned strings with
mermaid.js. Empty string means "nothing to draw" (the UI hides the block)."""

import re


def _san(name: str) -> str:
    """Mermaid-safe identifier."""
    s = re.sub(r"[^A-Za-z0-9_]", "_", name or "")
    if not s:
        return "unnamed"
    return ("t_" + s) if s[0].isdigit() else s


def _label(s: str) -> str:
    """Mermaid-safe node label (no quotes/brackets that break parsing)."""
    return re.sub(r'["\[\]{}|<>]', "", str(s))[:44]


def er_diagram(tables: list[dict]) -> str:
    """Mermaid erDiagram from scan_db_schema output ({name, columns:[...]}).
    Column types aren't recovered by the scanner, so attributes use a generic
    `field` type. Foreign keys are inferred from `*_id` columns."""
    if not tables:
        return ""
    merged: dict[str, list[str]] = {}
    order: list[str] = []
    for t in tables:
        n = _san(t.get("name", ""))
        if n not in merged:
            merged[n] = []
            order.append(n)
        for c in t.get("columns", []):
            cs = _san(c)
            if cs not in merged[n]:
                merged[n].append(cs)

    lines = ["erDiagram"]
    for n in order:
        lines.append(f"    {n} {{")
        for c in merged[n][:30]:
            lines.append(f"        field {c}")
        lines.append("    }")

    known = set(order)
    seen_rel: set[str] = set()
    for n in order:
        for c in merged[n]:
            low = c.lower()
            if not low.endswith("_id") or len(c) <= 3:
                continue
            stem = low[:-3]
            for cand in (stem, stem + "s", stem + "es"):
                target = _san(cand)
                if target in known and target != n:
                    rel = f'    {target} ||--o{{ {n} : "{c}"'
                    if rel not in seen_rel:
                        seen_rel.add(rel)
                        lines.append(rel)
                    break
    return "\n".join(lines)


def architecture_graph(manifest: dict, name: str = "project") -> str:
    """Mermaid flowchart of the top-level directory layout with file counts."""
    struct = [s for s in manifest.get("structure", []) if s.get("dir") != "."]
    if not struct:
        return ""
    root = _san(name) or "root"
    lines = ["flowchart TD", f'    {root}["{_label(name)}"]']
    for i, s in enumerate(struct[:12]):
        node = f"d{i}"
        lines.append(f'    {root} --> {node}["{_label(s["dir"])}<br/>{s["files"]} files"]')
    return "\n".join(lines)


def roadmap_gantt(features: list[dict]) -> str:
    """Mermaid gantt from [{name, start, end, status}] feature entries.
    Returns "" until the manager (M4) starts inferring features."""
    if not features:
        return ""
    lines = ["gantt", "    title Roadmap", "    dateFormat YYYY-MM-DD", "    section Features"]
    for f in features:
        name = _label(f.get("name", "feature")).replace(":", " ")
        start, end = f.get("start"), f.get("end")
        tag = "done, " if f.get("status") == "done" else ("active, " if f.get("status") == "active" else "")
        if start and end:
            lines.append(f"    {name} :{tag}{start}, {end}")
    return "\n".join(lines) if len(lines) > 4 else ""
