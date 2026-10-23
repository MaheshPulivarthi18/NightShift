"""L3 change context: the code around and connected to a diff, packed for the Coder and the Reviewer.

The analysis runs inside the task's sandbox (`sandbox/inside/change_context.py`), where the language
servers can read the repo without its code touching the host. This module validates what comes back
and ranks it into a context pack under a token budget, with the code the diff did NOT change but
probably should have listed first.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from nightshift.sandbox.docker import REPO_DIR, Sandbox

Kind = Literal["enclosing", "nearby", "caller", "definition", "same_shape"]


class Finding(BaseModel):
    kind: Kind
    path: str
    symbol: str = ""
    line: int = Field(ge=0)  # 0-based
    reason: str
    touched_by_diff: bool
    snippet: str = ""
    score: int = 0


class ChangeContext(BaseModel):
    base: str
    files_changed: list[str]
    findings: list[Finding]
    servers: list[str] = []
    errors: list[str] = []

    @property
    def unchanged_related(self) -> list[Finding]:
        """Callers and same-shape code the diff did not touch: the paths a fix most often misses."""
        return [f for f in self.ranked() if not f.touched_by_diff and f.kind in ("caller", "same_shape")]

    def ranked(self) -> list[Finding]:
        return sorted(self.findings, key=lambda f: (-f.score, f.touched_by_diff, f.path, f.line))


async def change_context(box: Sandbox, base: str) -> ChangeContext:
    """Context for the sandbox's working tree compared with commit `base`."""
    return ChangeContext.model_validate(await box.run_tool("change_context.py", REPO_DIR, base))


def to_markdown(ctx: ChangeContext, budget_tokens: int = 6000) -> tuple[str, int]:
    """The context pack, cut at `budget_tokens` (about 4 characters a token). Returns (text, tokens used)."""
    ranked = ctx.ranked()
    out = [f"# Change context (base {ctx.base[:12]})", "", "Files changed: " + ", ".join(ctx.files_changed), ""]
    if missed := ctx.unchanged_related:
        out += ["## Related code NOT changed by the diff (check these)", ""]
        out += [f"- `{f.path}:{f.line + 1}` {f.symbol}: {f.reason}" for f in missed]
        out.append("")
    out += ["## Context", ""]
    used = sum(len(x) for x in out) // 4
    for i, f in enumerate(ranked):
        block = f"### {f.kind}: `{f.path}:{f.line + 1}` {f.symbol}\n{f.reason}\n```\n{f.snippet}\n```\n"
        if used + len(block) // 4 > budget_tokens:
            out.append(f"_(budget reached; {len(ranked) - i} lower-ranked items left out)_")
            break
        out.append(block)
        used += len(block) // 4
    return "\n".join(out), used
