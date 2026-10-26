"""Prompts built stable-first, so repeated calls start with text the provider already has cached.

After the agent's instructions (the system prompt), a prompt is three layers, always in this order:

    repo  shared by every call about the repo: brief, rules, architecture map, environment
    item  the issue or PR the call is about, and what was decided on it: plan, the developer's note
    task  what changes from call to call: relevant files, the diff, verification, a failed setup

The same inputs give the same text, byte for byte, and the repo and item layers each end with a cache
breakpoint. Anthropic and Gemini models read a cached prefix only up to such a breakpoint; other
providers cache a repeated prefix on their own. Either way the prefix must not change between calls,
which is why nothing here adds timestamps or depends on dict or set order.

Every section has a token budget. A section over its budget keeps its start and its end (the end of an
issue thread is often where the request is settled) with a visible note where the middle was cut, and
`report()` lists every cut, so nothing is lost silently. The ledger stores the report with each call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Layer = Literal["repo", "item", "task"]
LAYERS: tuple[Layer, ...] = ("repo", "item", "task")
CACHED: tuple[Layer, ...] = ("repo", "item")  # a cache breakpoint ends each of these

CHARS_PER_TOKEN = 4  # an estimate for budgeting; the ledger records the provider's real counts

# Default section budgets, in tokens.
BRIEF = 3000
MAP = 4000
ENVIRONMENT = 1500
ITEM = 6000
CODE = 8000
DIFF = 15000


def tokens(text: str) -> int:
    return (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


def _cut(text: str, budget: int, tag: str) -> str:
    """The text within `budget` tokens: two thirds from the start, one third from the end, cut at lines."""
    keep = budget * CHARS_PER_TOKEN
    head, tail = text[: keep * 2 // 3], text[len(text) - keep // 3 :]
    head = head[: head.rfind("\n")] if "\n" in head else head
    tail = tail[tail.find("\n") + 1 :] if "\n" in tail else tail
    gone = tokens(text) - tokens(head) - tokens(tail)
    return f"{head}\n[cut here: about {gone} tokens over the {budget}-token budget for <{tag}>]\n{tail}"


@dataclass
class Section:
    layer: Layer
    tag: str
    text: str
    budget: int

    def render(self) -> tuple[str, bool]:
        text = self.text.strip()
        over = tokens(text) > self.budget
        return f"<{self.tag}>\n{_cut(text, self.budget, self.tag) if over else text}\n</{self.tag}>", over


@dataclass
class Prompt:
    sections: list[Section] = field(default_factory=list)

    def add(self, layer: Layer, tag: str, text: str, budget: int) -> Prompt:
        """Add a section; empty text adds nothing. Sections render in layer order, then in the order added."""
        if text.strip():
            self.sections.append(Section(layer, tag, text, budget))
        return self

    def blocks(self) -> list[dict[str, Any]]:
        """The user message's content: one text block per non-empty layer, the cached layers marked."""
        out: list[dict[str, Any]] = []
        for layer in LAYERS:
            parts = [s.render()[0] for s in self.sections if s.layer == layer]
            if parts:
                block: dict[str, Any] = {"type": "text", "text": "\n\n".join(parts)}
                if layer in CACHED:
                    block["cache_control"] = {"type": "ephemeral"}
                out.append(block)
        return out

    def report(self) -> dict[str, Any]:
        """Per layer: estimated tokens, and the sections that went over their budget."""
        out: dict[str, Any] = {}
        for layer in LAYERS:
            rendered = [(s, *s.render()) for s in self.sections if s.layer == layer]
            if rendered:
                out[layer] = {
                    "tokens": sum(tokens(text) for _, text, _ in rendered),
                    "cut": [s.tag for s, _, over in rendered if over],
                }
        return out
