"""Lock files in a diff, as the packages they change rather than their lines.

A re-lock rewrites hundreds of lines (laya #50: about 630 in each of two lock files), which a reviewer, human
or model, cannot read as a change. What matters is which packages changed version, were added or removed.
Read from the diff alone: uv, poetry and Cargo locks, package-lock.json, yarn.lock, pnpm-lock.yaml, go.sum and
pinned requirements. A lock file in another format keeps its lines.
"""

from __future__ import annotations

import re

LOCK_RE = re.compile(
    r"(^|/)(package-lock\.json|npm-shrinkwrap\.json|yarn\.lock|pnpm-lock\.yaml|uv\.lock|poetry\.lock|Pipfile\.lock"
    r"|Cargo\.lock|go\.sum|composer\.lock|Gemfile\.lock)$|(^|/)requirements[^/]*\.(lock|txt)$"
)
SHOWN = 40  # packages listed per lock file; the rest are counted

_NAME = [
    re.compile(r'^name = "([^"]+)"'),  # uv.lock, poetry.lock, Cargo.lock: [[package]] blocks
    re.compile(r'^\s*"(?:[^"]*/)?node_modules/((?:@[^/"]+/)?[^/"]+)": \{'),  # package-lock.json v2 and v3
    re.compile(r'^"?((?:@[^@/"\s]+/)?[^@"\s,]+)@[^\n]*:\s*$'),  # yarn.lock: `lodash@^4.17.0:`
]
_VERSION = [
    re.compile(r'^version = "([^"]+)"'),
    re.compile(r'^\s*"version": "([^"]+)"'),
    re.compile(r'^\s+version:? "?([^"\s]+)"?\s*$'),  # yarn.lock
]
# formats that say name and version on one line
_PAIR = [
    re.compile(r"^(\S+) (v[^\s/]+)(?:/go\.mod)? h1:"),  # go.sum
    re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.\-]*)(?:\[[^\]]*\])?==([^\s;\\]+)"),  # requirements: pkg==1.2
    re.compile(r"^\s*'?/?((?:@[^@/'\s]+/)?[^@/'\s]+)@(\d[^:('\s]*)'?:\s*$"),  # pnpm-lock.yaml: /pkg@1.2.3:
]


def changes(lines: list[str]) -> dict[str, tuple[str | None, str | None]]:
    """{package: (old version, new version)} from one lock file's diff lines (` `, `+`, `-` and `@@`)."""
    old: dict[str, str] = {}
    new: dict[str, str] = {}
    current: str | None = None
    for raw in lines:
        if raw.startswith("@@"):
            current = None
            continue
        if not raw or raw[0] not in " +-" or raw.startswith(("+++", "---")):
            continue
        sign, line = raw[0], raw[1:]
        if pair := next((m for p in _PAIR if (m := p.match(line))), None):
            if sign == "-":
                old.setdefault(pair.group(1), pair.group(2))
            elif sign == "+":
                new[pair.group(1)] = pair.group(2)
            continue
        if name := next((m for p in _NAME if (m := p.match(line))), None):
            current = name.group(1)
            continue
        if current and (version := next((m for p in _VERSION if (m := p.match(line))), None)):
            if sign == "-":
                old.setdefault(current, version.group(1))
            elif sign == "+":
                new[current] = version.group(1)
    out = {}
    for package in sorted({*old, *new}):
        before, after = old.get(package), new.get(package)
        if before != after:
            out[package] = (before, after)
    return out


def describe(found: dict[str, tuple[str | None, str | None]]) -> list[str]:
    lines = []
    for name, (before, after) in list(found.items())[:SHOWN]:
        if before and after:
            lines.append(f"{name} {before} → {after}")
        elif after:
            lines.append(f"{name} {after} (added)")
        else:
            lines.append(f"{name} {before} (removed)")
    if len(found) > SHOWN:
        lines.append(f"… and {len(found) - SHOWN} more")
    return lines


def _files(diff: str) -> list[tuple[str, str]]:
    """(path, section) for each file in a unified diff, in order."""
    sections = re.split(r"(?m)^(?=diff --git )", diff)
    out = []
    for section in sections:
        m = re.match(r"diff --git a/(\S+) b/", section)
        out.append((m.group(1) if m else "", section))
    return out


def summaries(diff: str) -> dict[str, list[str]]:
    """{lock file: its package changes, described} for the lock files in the diff whose format is read."""
    out = {}
    for path, section in _files(diff):
        if path and LOCK_RE.search(path) and (found := changes(section.splitlines())):
            out[path] = describe(found)
    return out


def summarize_diff(diff: str) -> str:
    """The diff with each readable lock file's lines replaced by the packages it changes."""
    parts = []
    for path, section in _files(diff):
        lines = section.splitlines()
        found = changes(lines) if path and LOCK_RE.search(path) else {}
        if not found:
            parts.append(section)
            continue
        changed = sum(1 for ln in lines if ln[:1] in "+-" and not ln.startswith(("+++", "---")))
        header = "\n".join(ln for ln in lines[:4] if ln.startswith(("diff --git", "--- ", "+++ ")))
        body = "\n".join(f"  {d}" for d in describe(found))
        parts.append(f"{header}\n(regenerated lock file: {changed} changed lines, shown as the {len(found)} "
                     f"packages it changes)\n{body}\n")  # fmt: skip
    return "".join(parts)
