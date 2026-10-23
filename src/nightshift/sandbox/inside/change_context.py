"""L3 change context, run INSIDE a task sandbox: the code around and connected to each change. No model calls.

For the working tree's diff against a base commit:
  1. enclosing: the smallest function or class containing each hunk (4 overlap cases, anonymous
     callbacks skipped); +-10 lines when nothing encloses it
  2. caller: references to every changed symbol, each mapped to the function that contains it
  3. definition: in-repo definitions of names called on added lines
  4. same_shape: places that use several of the string keys found around a change (event names,
     dict keys), found with `git grep`, because language servers do not follow them
Python goes through jedi, TypeScript/JavaScript through typescript-language-server; other files
get text search only. Prints one JSON object after a marker line; the host validates it.

Usage: python change_context.py <repo dir> <base ref>
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from contextlib import ExitStack
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# LSP SymbolKind: 5 class, 6 method, 9 constructor, 11 interface, 12 function, 23 struct
SCOPE_KINDS = {5, 6, 9, 11, 12, 23}
TYPE_KINDS = {5, 11, 23}  # a change inside one of these changes the type its users depend on
CALLBACK_NAMES = ("<lambda>", "callback", "<function>", "<unknown>")
KEY_RE = re.compile(r"""["']([a-z][a-z0-9]*(?:_[a-z0-9]+)+)["']""")  # snake_case string keys
CALL_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")
SKIP_CALLS = {
    "print", "len", "str", "int", "dict", "list", "set", "isinstance", "range", "json", "super", "if", "for",
    "while", "return", "function", "map", "filter", "console", "require", "await", "typeof",
}  # fmt: skip
LANGS = {"python": (".py",), "typescript": (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")}
ROOT_MARKERS = {
    "python": ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt"),
    "typescript": ("tsconfig.json", "package.json"),
}
SERVER_BINARY = {
    "python": "/opt/ns-tools/bin/jedi-language-server",
    "typescript": "/opt/ts-lsp/node_modules/.bin/typescript-language-server",
}
VENDOR = ("node_modules/", ".venv/", "site-packages/", "/venv/", "dist/", "build/")
GENERIC_FILES = 12  # a key used in more files than this is too common to mean anything
WINDOW = 8  # lines around a change whose keys make up its "shape"


@dataclass
class Hunk:
    path: str
    start: int  # 0-based, new file
    end: int
    added: list[int]


@dataclass
class Finding:
    kind: str  # enclosing | nearby | caller | definition | same_shape
    path: str
    symbol: str
    line: int  # 0-based
    reason: str
    touched_by_diff: bool
    snippet: str = ""
    score: int = 0


@dataclass
class Result:
    base: str
    files_changed: list[str]
    findings: list[Finding] = field(default_factory=list)
    servers: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, timeout=120).stdout


def parse_diff(diff: str) -> list[Hunk]:
    hunks: list[Hunk] = []
    path: str | None = None
    cur = 0
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = None if line.endswith("/dev/null") else line[6:]
        elif line.startswith("@@") and path:
            m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
            if not m:
                continue
            cur = int(m.group(1)) - 1
            hunks.append(Hunk(path, cur, cur, []))
        elif hunks and path and line[:1] in (" ", "+"):
            h = hunks[-1]
            if line[:1] == "+":
                h.added.append(cur)
            h.end = cur
            cur += 1
    return hunks


def compress(text_lines: list[str], start: int, focus: set[int], keep: int = 2) -> str:
    """Signature line, plus lines near `focus` (absolute 0-based); the rest elided as `...`."""
    out: list[str] = []
    last: int | None = None
    for i, line in enumerate(text_lines):
        n = start + i
        if i == 0 or any(abs(n - f) <= keep for f in focus):
            if last is not None and n - last > 1:
                out.append("    ...")
            out.append(f"{n + 1:5d}  {line[:200]}")
            last = n
    return "\n".join(out)


def is_source(path: str) -> bool:
    """Code a change can ripple out from: not tests, docs, lock files, vendored or generated output."""
    low = path.lower()
    name = Path(low).name
    return not (
        "/tests/" in low or low.startswith(("tests/", "test/")) or "/__tests__/" in low
        or ".test." in name or ".spec." in name or "_test." in name or name.startswith("test_")
        or low.endswith((".md", ".lock", ".json", ".txt", ".yml", ".yaml", ".toml", ".svg", ".css"))
        or any(v in low for v in VENDOR)
    )  # fmt: skip


def is_import_line(lines: list[str], n: int) -> bool:
    """An import statement, or a name inside a multi-line `import (`/`import {` block."""
    text = lines[n].lstrip()
    if text.startswith(("from ", "import ")) or re.match(r"export\s+.*\bfrom\s+['\"]", text):
        return True
    if re.fullmatch(r"\s*[\w, ]+,?\s*(#.*|//.*)?", lines[n]):
        for i in range(n - 1, max(-1, n - 40), -1):
            prev = lines[i].rstrip()
            if prev.endswith("import (") or re.match(r"\s*(import|export)\s*(type\s*)?\{[^}]*$", prev):
                return True
            if ")" in prev or "}" in prev:
                break
    return False


def name_by_indent(lines: list[str], n: int) -> str:
    """Nearest enclosing def/class/function above line n, by indentation (for text-search hits)."""
    pattern = re.compile(
        r"(?:async\s+)?(?:def|class)\s+(\w+)|(?:export\s+)?(?:async\s+)?function\s+(\w+)"
        r"|(?:export\s+)?(?:const|let)\s+(\w+)\s*=\s*(?:async\s*)?(?:\(|function)"
    )
    indent = len(lines[n]) - len(lines[n].lstrip())
    for i in range(n, -1, -1):
        stripped = lines[i].lstrip()
        if not stripped or len(lines[i]) - len(stripped) >= indent:
            continue
        m = pattern.match(stripped)
        if m:
            return next(g for g in m.groups() if g)
    return ""


class Servers:
    """One language server per (language, project root), started on first use."""

    def __init__(self, repo: Path, stack: ExitStack, result: Result) -> None:
        self.repo, self.stack, self.result = repo, stack, result
        self.started: dict[tuple[str, str], Any] = {}  # multilspy SyncLanguageServer, or None if it failed
        self.symbol_cache: dict[str, list[dict[str, Any]]] = {}

    @staticmethod
    def lang(path: str) -> str | None:
        return next((lang for lang, exts in LANGS.items() if path.endswith(exts)), None)

    def root(self, path: str, lang: str) -> str:
        parent = Path(path).parent
        for d in [parent, *parent.parents]:
            if any((self.repo / d / marker).exists() for marker in ROOT_MARKERS[lang]):
                return "" if str(d) == "." else str(d)
        return ""

    def server(self, path: str) -> tuple[Any, str] | None:
        lang = self.lang(path)
        if lang is None:
            return None
        root = self.root(path, lang)
        key = (lang, root)
        if key not in self.started:
            # Imported here: only the sandbox image has multilspy, and the pure helpers are tested on the host.
            from multilspy import SyncLanguageServer
            from multilspy.multilspy_config import MultilspyConfig
            from multilspy.multilspy_logger import MultilspyLogger

            try:
                cfg: dict[str, Any] = {"code_language": lang}
                if lang in SERVER_BINARY:
                    cfg["server_binary"] = SERVER_BINARY[lang]
                lsp = SyncLanguageServer.create(
                    MultilspyConfig.from_dict(cfg), MultilspyLogger(), str(self.repo / root)
                )
                self.stack.enter_context(lsp.start_server())
                self.started[key] = lsp
                self.result.servers.append(f"{lang} at /{root}")
            except Exception as e:  # a server that cannot start leaves text search
                self.started[key] = None
                self.result.errors.append(f"{lang} server at /{root}: {type(e).__name__}: {e}")
        lsp = self.started[key]
        return (lsp, root) if lsp else None

    def rel(self, path: str, root: str) -> str:
        return str(Path(path).relative_to(root)) if root else path

    def to_repo_path(self, loc: dict[str, Any], root: str) -> str | None:
        if loc.get("absolutePath"):
            try:
                return str(Path(loc["absolutePath"]).resolve().relative_to(self.repo.resolve()))
            except ValueError:
                return None
        rel = loc.get("relativePath")
        return str(Path(root) / rel) if rel else None

    def symbols(self, path: str) -> list[dict[str, Any]]:
        if path not in self.symbol_cache:
            found = self.server(path)
            syms: list[dict[str, Any]] = []
            if found:
                lsp, root = found
                try:
                    syms, _ = lsp.request_document_symbols(self.rel(path, root))
                except Exception as e:
                    self.result.errors.append(f"symbols {path}: {e}")
            self.symbol_cache[path] = [
                s for s in syms or []
                if s.get("kind") in SCOPE_KINDS and "range" in s
                and not any(c in s.get("name", "") for c in CALLBACK_NAMES)
            ]  # fmt: skip
        return self.symbol_cache[path]

    def enclosing(self, path: str, start: int, end: int) -> list[dict[str, Any]]:
        """Smallest symbol containing the range wins; otherwise every symbol that overlaps it."""
        best: dict[str, Any] | None = None
        best_size: int | None = None
        overlaps = []
        for s in self.symbols(path):
            a, b = s["range"]["start"]["line"], s["range"]["end"]["line"]
            if a <= start and b >= end:
                if best_size is None or b - a < best_size:
                    best, best_size = s, b - a
            elif a <= end and b >= start:
                overlaps.append(s)
        return [best] if best else overlaps


def run(repo: Path, base: str) -> Result:
    git(repo, "add", "--intent-to-add", "--all")  # so new files show up in the diff
    all_hunks = parse_diff(git(repo, "diff", "-U3", base))
    touched: dict[str, set[int]] = {}
    for h in all_hunks:
        touched.setdefault(h.path, set()).update(h.added)
    hunks = [h for h in all_hunks if is_source(h.path)]
    result = Result(base, sorted(touched))
    seen: set[tuple[str, int, str]] = set()
    text_cache: dict[str, list[str]] = {}

    def file_lines(p: str) -> list[str]:
        if p not in text_cache:
            try:
                text_cache[p] = (repo / p).read_text(errors="replace").splitlines()
            except OSError:
                text_cache[p] = []
        return text_cache[p]

    def add(f: Finding) -> None:
        if (f.path, f.line, f.kind) not in seen:
            seen.add((f.path, f.line, f.kind))
            result.findings.append(f)

    def was_touched(p: str, a: int, b: int) -> bool:
        return any(a <= t <= b for t in touched.get(p, set()))

    with ExitStack() as stack:
        servers = Servers(repo, stack, result)
        changed: list[tuple[str, dict[str, Any]]] = []

        for h in hunks:
            if not h.added:
                continue
            lines = file_lines(h.path)
            a0, a1 = min(h.added), max(h.added)
            enc = servers.enclosing(h.path, a0, a1)
            for s in enc:
                s0, s1 = s["range"]["start"]["line"], s["range"]["end"]["line"]
                add(Finding("enclosing", h.path, s["name"], s0, "contains the change", True,
                            compress(lines[s0:s1 + 1], s0, set(h.added)), 100))  # fmt: skip
                changed.append((h.path, s))
            if not enc:
                lo = max(0, a0 - 10)
                add(Finding("nearby", h.path, "", a0, "no enclosing function: lines around the change", True,
                            compress(lines[lo:a1 + 11], lo, set(h.added), 10), 90))  # fmt: skip
            # a class or interface whose body changed counts as changed too (e.g. a new model field)
            for s in servers.symbols(h.path):
                if s.get("kind") in TYPE_KINDS and s["range"]["start"]["line"] <= a0 <= s["range"]["end"]["line"]:
                    changed.append((h.path, s))

            found = servers.server(h.path)
            if not found:
                continue
            lsp, root = found
            for n in h.added:
                if n >= len(lines):
                    continue
                for m in CALL_RE.finditer(lines[n]):
                    if m.group(1) in SKIP_CALLS:
                        continue
                    try:
                        defs = lsp.request_definition(servers.rel(h.path, root), n, m.start(1))
                    except Exception:
                        defs = []
                    for d in defs or []:
                        dp = servers.to_repo_path(d, root)
                        if not dp or any(v in dp for v in VENDOR) or not (repo / dp).is_file():
                            continue
                        dl = d["range"]["start"]["line"]
                        if dp == h.path and dl == n:
                            continue  # the name's own declaration
                        add(Finding("definition", dp, m.group(1), dl, f"called on {h.path}:{n + 1}",
                                    dl in touched.get(dp, set()), compress(file_lines(dp)[dl:dl + 15], dl, {dl}),
                                    40))  # fmt: skip

        for path, s in changed:
            found = servers.server(path)
            if not found:
                continue
            lsp, root = found
            sel = s.get("selectionRange", s["range"])["start"]
            try:
                refs = lsp.request_references(servers.rel(path, root), sel["line"], sel["character"])
            except Exception:
                refs = []
            for r in refs or []:
                rp = servers.to_repo_path(r, root)
                if not rp or not is_source(rp) or not (repo / rp).is_file():
                    continue
                rl = r["range"]["start"]["line"]
                if rp == path and s["range"]["start"]["line"] <= rl <= s["range"]["end"]["line"]:
                    continue  # inside the symbol itself
                rlines = file_lines(rp)
                if rl >= len(rlines) or is_import_line(rlines, rl):
                    continue  # an import is not a use
                enc = servers.enclosing(rp, rl, rl)
                e0 = enc[0]["range"]["start"]["line"] if enc else max(0, rl - 5)
                e1 = enc[0]["range"]["end"]["line"] if enc else rl + 5
                add(Finding("caller", rp, enc[0]["name"] if enc else "", rl, f"uses `{s['name']}` (changed)",
                            was_touched(rp, e0, e1), compress(rlines[e0:e1 + 1], e0, {rl}), 80))  # fmt: skip

    # Shape match: code elsewhere that uses several of the string keys around a change (e.g. code that
    # builds the same event dict). Keys come from unchanged lines near the change: keys the change itself
    # adds are used nowhere else yet.
    for h in hunks:
        flines = file_lines(h.path)
        a0, a1 = (min(h.added), max(h.added)) if h.added else (h.start, h.end)
        around = [flines[i] for i in range(max(0, a0 - WINDOW), min(len(flines), a1 + WINDOW + 1)) if i not in h.added]
        hits: dict[str, list[tuple[str, int]]] = {}
        for key in sorted({k for line in around for k in KEY_RE.findall(line)}):
            out = git(repo, "grep", "-n", "-F", "-e", f'"{key}"', "-e", f"'{key}'", "--", ".")
            locs = []
            for row in out.splitlines():
                parts = row.split(":", 2)
                if len(parts) == 3 and parts[1].isdigit() and is_source(parts[0]):
                    locs.append((parts[0], int(parts[1]) - 1))
            if 0 < len({p for p, _ in locs}) <= GENERIC_FILES:
                hits[key] = locs
        groups: dict[tuple[str, int], set[str]] = {}
        for key, locs in hits.items():
            for p, ln0 in locs:
                if p == h.path and h.start - 3 <= ln0 <= h.end + 3:
                    continue
                anchor = next((a for (gp, a) in groups if gp == p and abs(a - ln0) <= WINDOW), ln0)
                groups.setdefault((p, anchor), set()).add(key)
        for (p, ln0), keys in groups.items():
            rare = any(len({q for q, _ in hits[k]}) <= 2 for k in keys)
            if len(keys) < 2 and not rare:
                continue
            plines = file_lines(p)
            if ln0 >= len(plines):
                continue
            listed = ", ".join(f'"{k}"' for k in sorted(keys))
            add(Finding("same_shape", p, name_by_indent(plines, ln0), ln0,
                        f"uses {len(keys)} of the keys around the change in {h.path}: {listed}",
                        was_touched(p, ln0 - WINDOW, ln0 + WINDOW),
                        compress(plines[max(0, ln0 - 3):ln0 + WINDOW + 3], max(0, ln0 - 3), {ln0}),
                        60 + 5 * len(keys)))  # fmt: skip
    return result


if __name__ == "__main__":
    res = run(Path(sys.argv[1]), sys.argv[2])
    # A marker line, so anything else a library prints to stdout cannot be mistaken for the result.
    sys.stdout.write("\n@@NIGHTSHIFT_RESULT@@" + json.dumps(asdict(res)) + "\n")
