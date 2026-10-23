"""Source index for the L1 architecture map, run INSIDE a task sandbox. Standard library only, no model calls.

Computes what a model should not guess: which files are source, who imports whom (Python, TypeScript/
JavaScript, Svelte, Rust), which files are entry points, and which are central (most imported, largest,
or wired up directly by an entry point). Returns excerpts of the central files for the map's one model
call. Prints one JSON object after a marker line; the host validates it and builds the prompt.

Usage: python source_index.py <repo dir>
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

SOURCE_EXT = (".py", ".ts", ".tsx", ".js", ".jsx", ".svelte", ".vue", ".rs", ".go")
SKIP = re.compile(
    r"(^|/)(tests?|__tests__|docs?|examples?|benchmarks?|migrations|fixtures|scripts|\.github|landing|static|assets"
    r"|icons|node_modules|vendor|dist|build)(/|$)|\.(test|spec)\.|\.d\.ts$|(^|/)test_[^/]*\.py$"
)
TREE_SKIP_EXT = (".png", ".jpg", ".gif", ".svg", ".ico", ".json", ".lock", ".woff", ".woff2", ".ttf")
ENTRY_NAMES = {"main.py", "__main__.py", "app.py", "server.py", "cli.py", "manage.py", "main.rs", "lib.rs",
               "main.go", "main.ts", "index.ts", "server.ts", "app.ts", "+layout.svelte", "App.svelte"}  # fmt: skip
CENTRAL = 24
MAX_CENTRAL = 40
EXCERPTS = 22


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, timeout=120).stdout


def read(repo: Path, path: str) -> str:
    try:
        return (repo / path).read_text(errors="replace")
    except OSError:
        return ""


def py_imports(text: str, path: str, modules: dict[str, str]) -> set[str]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()
    names: set[str] = set()
    package = ".".join(Path(path).parent.parts)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level:  # relative import: resolve against this file's package
                parts = package.split(".") if package else []
                parts = parts[: len(parts) - (node.level - 1)] if node.level > 1 else parts
                mod = ".".join([*parts, mod] if mod else parts)
            names.add(mod)
            names |= {f"{mod}.{a.name}" for a in node.names}
    return {modules[n] for n in names if n in modules and modules[n] != path}


def js_imports(text: str, path: str, files: set[str], aliases: dict[str, str]) -> set[str]:
    out: set[str] = set()
    for a, b in re.findall(r"""from\s+['"]([^'"]+)['"]|import\(\s*['"]([^'"]+)['"]""", text):
        spec = a or b
        base = None
        for alias, target in aliases.items():
            if spec == alias or spec.startswith(alias + "/"):
                base = target + spec[len(alias) :]
                break
        if base is None and spec.startswith("."):
            base = os.path.normpath(os.path.join(os.path.dirname(path), spec))
        if base is None:
            continue
        for cand in (base, *(base + e for e in (".ts", ".tsx", ".js", ".svelte", "/index.ts", "/index.js"))):
            if cand in files and cand != path:
                out.add(cand)
                break
    return out


def rust_imports(text: str, path: str, files: set[str], crate_src: str | None) -> set[str]:
    """`mod x;` -> the submodule file; `use crate::a::b` -> the longest matching module file."""
    out: set[str] = set()
    here = Path(path)
    mod_dir = here.parent if here.name in ("mod.rs", "lib.rs", "main.rs") else here.with_suffix("")
    for name in re.findall(r"^\s*(?:pub(?:\([^)]*\))?\s+)?mod\s+(\w+)\s*;", text, re.M):
        for cand in (mod_dir / f"{name}.rs", mod_dir / name / "mod.rs"):
            if str(cand) in files:
                out.add(str(cand))
    if crate_src:
        for chain in re.findall(r"\buse\s+crate::([\w:]+)", text):
            parts = chain.split("::")
            for i in range(len(parts), 0, -1):
                stem = Path(crate_src, *parts[:i])
                hit = next((str(c) for c in (stem.with_suffix(".rs"), stem / "mod.rs") if str(c) in files), None)
                if hit:
                    if hit != path:
                        out.add(hit)
                    break
    return out


def js_aliases(repo: Path, tracked: list[str]) -> dict[str, str]:
    """Import aliases: SvelteKit's $lib, plus `paths` from tsconfig/jsconfig files."""
    aliases: dict[str, str] = {}
    for p in tracked:
        if p.endswith("src/lib/index.ts") or "/src/lib/" in p or p.startswith("src/lib/"):
            root = p.split("src/lib/")[0]
            aliases.setdefault("$lib", f"{root}src/lib")
        if Path(p).name in ("tsconfig.json", "jsconfig.json"):
            text = re.sub(r"//[^\n]*", "", read(repo, p))
            try:
                paths = json.loads(text).get("compilerOptions", {}).get("paths", {})
            except (json.JSONDecodeError, AttributeError):
                continue
            base = str(Path(p).parent)
            for alias, targets in paths.items():
                if targets and alias.endswith("/*") and targets[0].endswith("/*"):
                    aliases.setdefault(alias[:-2], os.path.normpath(os.path.join(base, targets[0][:-2])))
    return aliases


def excerpt(text: str, max_lines: int = 70) -> str:
    """Top of the file plus every def/class/function/export/route signature."""
    lines = text.splitlines()
    keep = set(range(min(len(lines), 25)))
    sig = re.compile(
        r"^\s*(async\s+def|def|class|export\s+(default\s+)?(async\s+)?function|export\s+(const|class)|fn|pub\s+(async\s+)?fn"
        r"|@\w*(router|app)\.|impl\b|func\s)"
    )
    keep |= {i for i, line in enumerate(lines) if sig.match(line)}
    out: list[str] = []
    last = -2
    for i in sorted(keep)[:max_lines]:
        if i - last > 1:
            out.append("  ...")
        out.append(lines[i][:160])
        last = i
    return "\n".join(out)


def build(repo: Path) -> dict[str, object]:
    tracked = git(repo, "ls-files").splitlines()
    sources = [p for p in tracked if p.endswith(SOURCE_EXT) and not SKIP.search(p)]
    files = set(sources)
    texts = {p: read(repo, p) for p in sources}

    modules: dict[str, str] = {}
    for p in sources:
        if p.endswith(".py"):
            parts = Path(p).with_suffix("").parts
            for i in range(len(parts)):  # every suffix as a dotted module name
                modules.setdefault(".".join(parts[i:]), p)
                if parts[-1] == "__init__":
                    modules.setdefault(".".join(parts[i:-1]), p)
    aliases = js_aliases(repo, tracked)
    crates = {str(Path(p).parent / "src") for p in tracked if Path(p).name == "Cargo.toml"}

    imports: dict[str, set[str]] = {}
    for p in sources:
        t = texts[p]
        if p.endswith(".py"):
            imports[p] = py_imports(t, p, modules)
        elif p.endswith(".rs"):
            crate = next((c for c in sorted(crates, key=len, reverse=True) if p.startswith(c + "/")), None)
            imports[p] = rust_imports(t, p, files, crate)
        elif p.endswith(".go"):
            imports[p] = set()
        else:
            imports[p] = js_imports(t, p, files, aliases)
    sizes = {p: texts[p].count("\n") + 1 for p in sources}
    indeg = Counter(t for deps in imports.values() for t in deps)

    entries = sorted(
        p for p in sources
        if Path(p).name in ENTRY_NAMES or (p.endswith(".py") and re.search(r"__name__\s*==\s*['\"]__main__", texts[p]))
    )  # fmt: skip
    top = sorted(sources, key=lambda p: -(indeg[p] * 40 + min(sizes[p], 2000)))[:CENTRAL]
    wired = sorted({d for e in entries for d in imports[e]}, key=lambda p: -indeg[p])
    central = list(dict.fromkeys([*top, *entries, *wired]))[:MAX_CENTRAL]

    lines = ["CORE MODULES (most imported or largest): " + ", ".join(top[:12])]
    lines.append("ENTRY POINTS: " + (", ".join(entries) or "none found"))
    lines.append("")
    for p in central:
        deps = ", ".join(sorted(imports[p])[:12]) or "-"
        lines.append(f"{p} [{sizes[p]} lines, imported by {indeg[p]}] imports: {deps}")

    shown = list(dict.fromkeys([*entries, *top]))[:EXCERPTS]
    tree = [p for p in tracked if not SKIP.search(p) and "/" in p and not p.endswith(TREE_SKIP_EXT)]
    readme = next((p for p in tracked if p.lower() == "readme.md"), None)
    return {
        "commit": git(repo, "rev-parse", "HEAD").strip(),
        "tracked": tracked,
        "tree": tree,
        "readme": read(repo, readme)[:6000] if readme else "",
        "index": "\n".join(lines),
        "entry_points": entries,
        "central": central,
        "excerpts": {p: excerpt(texts[p]) for p in shown},
        "source_files": len(sources),
    }


if __name__ == "__main__":
    sys.stdout.write("\n@@NIGHTSHIFT_RESULT@@" + json.dumps(build(Path(sys.argv[1]))) + "\n")
