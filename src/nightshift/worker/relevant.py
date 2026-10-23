"""L2 relevant files: the source files that mention the most of the issue's own terms. No model calls.

Terms are what an issue names exactly: `backticked` text, paths, CamelCase and snake_case identifiers.
Each is searched with `git grep` inside the sandbox. A file scores higher for matching more terms, and
for matching rare ones; a term found in many files says little.
"""

from __future__ import annotations

import re
import shlex
from collections import defaultdict

from nightshift.sandbox.docker import Sandbox
from nightshift.worker.verify import is_test_path

MAX_TERMS = 25
TOP_FILES = 10
EXCERPT_LINES = 80
COMMON = {"None", "True", "False", "self", "this", "TODO", "README", "GitHub", "JSON", "HTTP", "API"}
_TERM_RES = [
    re.compile(r"`([^`\n]{3,80})`"),
    re.compile(r"\b([\w./-]+\.(?:py|ts|tsx|js|svelte|rs|go|vue|java|rb))\b"),
    re.compile(r"\b([A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+)\b"),  # CamelCase
    re.compile(r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b"),  # snake_case
]


def issue_terms(text: str) -> list[str]:
    seen: dict[str, None] = {}
    for rx in _TERM_RES:
        for m in rx.finditer(text):
            term = m.group(1).strip().strip("()")
            if len(term) >= 4 and term not in COMMON:
                seen.setdefault(term)
    return list(seen)[:MAX_TERMS]


def _is_code(path: str) -> bool:
    return not is_test_path(path) and not path.endswith((".md", ".lock", ".json", ".txt", ".svg", ".yml", ".yaml"))


async def relevant_files(box: Sandbox, text: str) -> tuple[list[str], str]:
    """Returns (ranked paths, a model-ready section with an excerpt of each)."""
    terms = issue_terms(text)
    hits: dict[str, set[str]] = defaultdict(set)
    weight: dict[str, float] = {}
    for term in terms:
        res = await box.exec(f"git grep -l -I -F -e {shlex.quote(term)} -- . | head -200")
        files = [p for p in res.stdout.splitlines() if _is_code(p)]
        if not files:
            continue
        weight[term] = 1.0 / len(files)
        for p in files:
            hits[p].add(term)
    ranked = sorted(hits, key=lambda p: (-sum(weight[t] for t in hits[p]), -len(hits[p]), p))[:TOP_FILES]

    sections = []
    for path in ranked:
        res = await box.exec(f"awk 'NR<=3000' {shlex.quote(path)}")
        lines = res.stdout.splitlines()
        keep = set(range(min(15, len(lines))))
        for i, line in enumerate(lines):
            if any(t in line for t in hits[path]):
                keep |= set(range(max(0, i - 3), min(len(lines), i + 4)))
        shown, last = [], -2
        for i in sorted(keep)[:EXCERPT_LINES]:
            if i - last > 1:
                shown.append("   ...")
            shown.append(f"{i + 1:5d}  {lines[i][:200]}")
            last = i
        sections.append(f"### {path} (mentions: {', '.join(sorted(hits[path]))})\n" + "\n".join(shown))
    header = f"Terms searched: {', '.join(terms) or 'none'}\n\n"
    return ranked, header + ("\n\n".join(sections) or "No source file mentions the issue's terms.")
