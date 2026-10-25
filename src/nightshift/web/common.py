"""Shared by the API routers: Markdown rendering and the guard on every call that changes something.

The server has no login and binds to 127.0.0.1. A call that changes state must come from the app
itself: a JSON body (a cross-site form cannot send one without a CORS preflight this server never
answers) and, when the browser sends them, a same-origin Origin and Sec-Fetch-Site.

Reports, chats and plans contain text written on GitHub or by models, so Markdown is rendered and
sanitized here (nh3), never in the browser.
"""

from __future__ import annotations

import re
from typing import Any

import nh3
from fastapi import Depends, HTTPException, Request
from markdown_it import MarkdownIt

from nightshift.catalog import ModelInfo

_md = MarkdownIt("commonmark", {"html": True}).enable("table")


# Report sections whose issues someone else is already on: they get a chat link, never a plan link.
TAKEN_SECTIONS = ("Already being worked on",)


def render_markdown(text: str, repo: str | None = None) -> str:
    """Markdown to sanitized HTML. Links to the repo's issues and PRs get chat and plan links next to them
    (no plan link for issues in a section listing work already taken)."""
    html = nh3.clean(_md.render(text or ""), link_rel="noopener noreferrer")
    if not repo:
        return html
    pattern = re.compile(rf'(<a href="https://github\.com/{re.escape(repo)}/(issues|pull)/(\d+)"[^>]*>[^<]*</a>)')

    def links(m: re.Match[str], plan: bool) -> str:
        n = m.group(3)
        out = f'{m.group(1)} <a class="chat-link" href="#/chat/{repo}/{n}" title="Chat about #{n}">chat</a>'
        if plan and m.group(2) == "issues":
            out += f' <a class="plan-link" href="#/?repo={repo}&amp;issue={n}" title="Plan a fix for #{n}">plan</a>'
        return out

    out = []
    for section in re.split(r"(?=<h2[ >])", html):  # one piece per report section
        plan = not any(section.startswith(f"<h2>{t}") for t in TAKEN_SECTIONS)
        out.append(pattern.sub(lambda m: links(m, plan), section))  # noqa: B023 - called right here
    return "".join(out)


async def same_origin(request: Request) -> None:
    if request.headers.get("content-type", "").split(";")[0] != "application/json":
        raise HTTPException(415, "JSON only")
    origin = request.headers.get("origin")
    host = request.headers.get("host", "")
    if origin and origin.split("://", 1)[-1] != host:
        raise HTTPException(403, "Cross-site request refused")
    if request.headers.get("sec-fetch-site") not in (None, "same-origin", "none"):
        raise HTTPException(403, "Cross-site request refused")


Guard = Depends(same_origin)


def model_view(m: ModelInfo) -> dict[str, Any]:
    """A catalog model for the UI: prices per million tokens, benchmark scores, what it can do."""
    return {
        "id": m.id,
        "name": m.name,
        "input_per_m": round(m.prompt * 1e6, 3),
        "output_per_m": round(m.completion * 1e6, 3),
        "context": m.context_length,
        "intelligence": m.intelligence,
        "coding": m.coding,
        "agentic": m.agentic,
        "tools": m.tools,
        "structured": m.structured,
    }
