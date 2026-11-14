"""Text in a pull request aimed at the AI reviewing it, found by code rather than by asking a model.

Two kinds, after ECC's AgentShield (github.com/affaan-m/ecc, MIT licence):
- characters a person reading the PR on GitHub does not see: bidirectional controls (which make code read
  differently from how it runs, "Trojan Source"), zero-width characters and Unicode tag characters (which
  can carry hidden ASCII instructions);
- instructions addressed to an AI in the title or description, most of all inside an HTML comment, which
  GitHub does not show.

Bidirectional controls and tag characters in the code block the PR; everything else is advice for the
maintainer to read.
"""

from __future__ import annotations

import re
import unicodedata

BIDI = re.compile("[‪-‮⁦-⁩]")
INVISIBLE = re.compile("[​-‍⁠⁢-⁤﻿­]")
TAGS = re.compile("[\U000e0000-\U000e007f]")
PROSE = re.compile(
    r"\.(md|rst|txt|po|pot|json|ya?ml|xliff|strings|arb|properties)$|(^|/)(locales?|i18n|l10n|translations?|docs?)/",
    re.I,
)
HTML_COMMENT = re.compile(r"<!--(.*?)-->", re.S)
AIMED_AT_AI = re.compile(
    r"\b(ignore|disregard|forget|override)\b[^.\n]{0,40}\b(previous|prior|above|earlier|all|your|system)\b"
    r"[^.\n]{0,20}\b(instructions?|prompts?|rules|guidelines)\b"
    r"|\b(as an? |you are an? |dear |note to (the )?)(ai|llm|language model|assistant|ai reviewer|bot|agent)\b"
    r"|\b(ai|llm|automated|bot)\s+(reviewers?|agents?|assistants?)\s*[:,]"
    r"|\b(approve|merge|accept)\s+(this|the)\s+(pr|pull request|change)s?\s+(without|immediately|regardless)"
    r"|<\|im_start\|>|\[/?INST\]|<\|system\|>|</?system>",
    re.I,
)


def _added_by_file(diff: str) -> dict[str, str]:
    out: dict[str, list[str]] = {}
    path = ""
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else ""
        elif path and line.startswith("+"):
            out.setdefault(path, []).append(line[1:])
    return {p: "\n".join(lines) for p, lines in out.items()}


def _names(text: str, pattern: re.Pattern[str]) -> str:
    found = sorted({unicodedata.name(c, f"U+{ord(c):04X}") for c in pattern.findall(text)})
    return ", ".join(found[:4]) + (" …" if len(found) > 4 else "")


def _quote(text: str, limit: int = 120) -> str:
    one = " ".join(text.split())
    return one if len(one) <= limit else one[: limit - 1] + "…"


def scan(title: str, body: str, diff: str) -> tuple[list[str], list[str]]:
    """(must, should) findings about hidden or AI-directed text in the PR."""
    must: list[str] = []
    should: list[str] = []
    for path, code in _added_by_file(diff).items():
        # Right-to-left text in translations and docs uses these characters legitimately.
        prose = bool(PROSE.search(path))
        if BIDI.search(code):
            (should if prose else must).append(
                f"`{path}` adds bidirectional control characters ({_names(code, BIDI)}): text can read "
                "differently from how it runs. Remove them or explain each one."
            )
        if TAGS.search(code):
            must.append(f"`{path}` adds Unicode tag characters, which can hide text: remove them.")
        if INVISIBLE.search(code) and not prose:
            should.append(f"`{path}` adds invisible characters ({_names(code, INVISIBLE)}): check they are intended.")
    text = f"{title}\n{body}"
    if BIDI.search(text) or TAGS.search(text) or INVISIBLE.search(text):
        pattern = TAGS if TAGS.search(text) else BIDI if BIDI.search(text) else INVISIBLE
        should.append(f"The title or description contains invisible characters ({_names(text, pattern)}).")
    for comment in HTML_COMMENT.findall(body):
        if m := AIMED_AT_AI.search(comment):
            should.append(
                "The description has a hidden comment (not shown on GitHub) addressed to AI reviewers: "
                f'"{_quote(comment[max(0, m.start() - 30) :])}". NightShift ignored it; read it before merging.'
            )
            break
    else:
        visible = HTML_COMMENT.sub("", text)
        if m := AIMED_AT_AI.search(visible):
            should.append(
                "The title or description contains text addressed to AI reviewers: "
                f'"{_quote(visible[max(0, m.start() - 30) :])}". NightShift ignored it.'
            )
    return must, should
