"""Compact, model-ready view of a repo: a folder summary and its contribution documents.

The full tree of a big repo is thousands of paths, so code reduces it to file counts per
folder before any model sees it. Documents are trimmed per kind, keeping the parts that
carry rules (CONTRIBUTING, PR template, agent guides) longer than the README.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

# Characters kept per document; contribution rules matter more than README marketing.
DOC_LIMITS = {
    "CONTRIBUTING.md": 8000,
    ".github/CONTRIBUTING.md": 8000,
    "docs/CONTRIBUTING.md": 8000,
    ".github/PULL_REQUEST_TEMPLATE.md": 2500,
    ".github/pull_request_template.md": 2500,
    "CLAUDE.md": 5000,
    "AGENTS.md": 5000,
    "README.md": 2500,
}
TOP_DIRS = 12
SUB_DIRS = 6
ROOT_FILES = 15
_EXT_RE = re.compile(r"\.[a-z]{1,5}$")


def tree_summary(paths: list[str]) -> str:
    """Folders with file counts: the biggest top-level folders and their biggest subfolders."""
    if not paths:
        return "(no files listed)"
    top: Counter[str] = Counter()
    sub: dict[str, Counter[str]] = {}
    root_files = []
    for path in paths:
        parts = path.split("/")
        if len(parts) == 1:
            root_files.append(path)
            continue
        top[parts[0]] += 1
        if len(parts) > 2:
            sub.setdefault(parts[0], Counter())[parts[1]] += 1
    lines = [f"{len(paths)} files"]
    for folder, count in top.most_common(TOP_DIRS):
        lines.append(f"{folder}/ ({count} files)")
        for child, n in sub.get(folder, Counter()).most_common(SUB_DIRS):
            lines.append(f"  {folder}/{child}/ ({n})")
    if len(top) > TOP_DIRS:
        lines.append(f"... {len(top) - TOP_DIRS} more top-level folders")
    shown = sorted(root_files)[:ROOT_FILES]
    if shown:
        more = f" (+{len(root_files) - len(shown)} more)" if len(root_files) > len(shown) else ""
        lines.append("root files: " + ", ".join(shown) + more)
    return "\n".join(lines)


def briefer_input(repo_docs: dict[str, Any]) -> dict[str, Any]:
    docs = {
        path: text[: DOC_LIMITS.get(path, 2500)] + ("\n[truncated]" if len(text) > DOC_LIMITS.get(path, 2500) else "")
        for path, text in repo_docs["docs"].items()
    }
    return {
        "description": repo_docs.get("description"),
        "language": repo_docs.get("language"),
        "folders": tree_summary(repo_docs["paths"]),
        "documents": docs,
    }


def missing_paths(text: str, paths: set[str]) -> list[str]:
    """Repo paths cited in backticks in `text` (with a slash or a file extension) that do not exist.

    A bare file name counts as found if any path ends with it, so `runtime.rs` matches
    `ui/src-tauri/src/runtime.rs`.
    """
    dirs = {p.rsplit("/", 1)[0] for p in paths if "/" in p}
    out = []
    for raw in re.findall(r"`([^`\s]+)`", text):
        cited = raw.split("::")[0].rstrip("/:")
        if cited.startswith(("http", "-")) or not ("/" in cited or _EXT_RE.search(cited)):
            continue
        if cited in paths or cited in dirs or any(p.endswith("/" + cited) for p in paths):
            continue
        out.append(cited)
    return out


# A PR rule about AI use. "AI-generated" counts; "generated files" (protobuf, mocks) or a path like
# `tests/llm/` does not.
AI_RULE = re.compile(r"\bAI\b|(?<![/\w.])LLMs?\b|\bcopilot\b|\bchatgpt\b|assisted-by|machine-generated", re.I)


def ai_rules(pr_rules: list[str]) -> list[str]:
    """The repo's PR rules that are about AI use (sarama: "explain and defend the changes … without AI")."""
    return [r for r in pr_rules if AI_RULE.search(r)]
