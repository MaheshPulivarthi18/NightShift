"""End-to-end run of the LangGraph pipeline with GitHub, embeddings and Claude faked.

Needs the local Postgres (docker compose up); skipped otherwise.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy import delete, select

from nightshift import db, memory
from nightshift.config import get_settings
from nightshift.db import Repo, Report, Run, RunEvent, session_scope
from nightshift.github import GitHubClient, normalize_issue, normalize_pr
from nightshift.health import agents, graph
from nightshift.llm import LLMGateway


def _issue(n: int, title: str, **kw: Any) -> dict[str, Any]:
    node = {
        "number": n,
        "title": title,
        "body": "",
        "url": f"https://github.com/t/r/issues/{n}",
        "state": "OPEN",
        "stateReason": None,
        "createdAt": "2026-06-01T00:00:00Z",
        "updatedAt": "2026-06-02T00:00:00Z",
        "closedAt": None,
        "author": {"login": "u"},
        "labels": {"nodes": []},
        "comments": {"totalCount": 0, "nodes": []},
        "timelineItems": {"nodes": []},
    }
    return normalize_issue({**node, **kw})


def _pr(n: int, title: str, closes: list[dict[str, Any]], **kw: Any) -> dict[str, Any]:
    node = {
        "number": n,
        "title": title,
        "body": "",
        "url": f"https://github.com/t/r/pull/{n}",
        "state": "OPEN",
        "createdAt": "2026-05-01T00:00:00Z",
        "updatedAt": "2026-05-02T00:00:00Z",
        "closedAt": None,
        "mergedAt": None,
        "isDraft": False,
        "mergeable": "CONFLICTING",
        "reviewDecision": None,
        "additions": 5,
        "deletions": 1,
        "changedFiles": 1,
        "author": {"login": "c"},
        "labels": {"nodes": []},
        "files": {"nodes": [{"path": "a.py"}]},
        "closingIssuesReferences": {"nodes": closes},
        "commits": {"nodes": [{"commit": {"committedDate": "2026-05-01T00:00:00Z", "statusCheckRollup": None}}]},
        "reviews": {"nodes": []},
        "comments": {"totalCount": 0, "nodes": []},
        "timelineItems": {"nodes": []},
    }
    return normalize_pr({**node, **kw})


FIXTURE = [
    _issue(1, "Crash on Windows paths"),
    _issue(
        2,
        "Windows path crash again",
        timelineItems={
            "nodes": [{"__typename": "MarkedAsDuplicateEvent", "canonical": {"__typename": "Issue", "number": 1}}]
        },
    ),
    _issue(
        3,
        "Old bug",
        state="CLOSED",
        stateReason="COMPLETED",
        timelineItems={"nodes": [{"__typename": "ClosedEvent", "closer": {"__typename": "PullRequest", "number": 5}}]},
    ),
    _pr(4, "Fix old bug", [{"number": 3, "state": "CLOSED", "stateReason": "COMPLETED"}]),
    _pr(
        5,
        "Fix old bug properly",
        [{"number": 3, "state": "CLOSED", "stateReason": "COMPLETED"}],
        state="MERGED",
        mergedAt="2026-06-01T00:00:00Z",
    ),
]


async def fake_fetch(
    self: GitHubClient, full_name: str, since: Any, lookback: int, limit: int
) -> tuple[list[dict[str, Any]], bool, list[str]]:
    return FIXTURE, False, []


async def same_name(self: GitHubClient, full_name: str) -> str:
    return full_name  # the repo has not moved


async def fake_docs(self: GitHubClient, full_name: str) -> dict[str, Any]:
    return {
        "head_oid": "abc",
        "default_branch": "main",
        "description": "A test repo",
        "language": "Python",
        "docs": {"CONTRIBUTING.md": "Open an issue before any feature PR."},
        "paths": ["src/paths.py", "tests/test_paths.py", "README.md"],
        "tree_truncated": False,
    }


def fake_embed(texts: list[str]) -> list[list[float]]:
    dim = get_settings().embedding_dim
    out = []
    for t in texts:
        v = [0.0] * dim
        v[0 if "Windows" in t else 1 if "old bug" in t.lower() else 2] = 1.0
        out.append(v)
    return out


async def fake_parse(self: LLMGateway, *, agent: str, schema: type[BaseModel], user: str, **_: Any) -> BaseModel:
    if schema is agents.RelevanceResult:
        return agents.RelevanceResult(
            clusters=[
                agents.Cluster(
                    name="Windows paths",
                    summary="Path handling on Windows.",
                    numbers=[1, 2],
                    duplicates=[agents.DuplicatePair(duplicate=2, original=1, reason="Same crash")],
                )
            ]
            if "Windows" in user
            else [],
            unrelated=[],
        )
    if schema is agents.PRVerdict:
        return agents.PRVerdict(
            category="close_candidate",
            reason_code="already_fixed",
            confidence="high",
            explanation="Issue #3 was fixed by #5.",
            evidence=["#4", "#3", "#5"],
            decision_question=None,
            closing_message="Thanks! #3 was fixed in #5.",
        )
    if schema is agents.RepoBrief:
        assert "Open an issue before any feature PR." in user
        return agents.RepoBrief(
            summary="A test repo.",
            pr_rules=["Open an issue before any feature PR."],
            setup_and_test=["pytest"],
            code_map=[agents.CodeArea(path="src/", purpose="Code")],
            source_documents=["CONTRIBUTING.md"],
        )
    if schema is agents.ScoutResult:
        assert "Open an issue before any feature PR." in user  # the Scout sees the brief
        return agents.ScoutResult(
            issues=[
                agents.IssueOpportunity(
                    number=1,
                    actionable=True,
                    kind="bug",
                    size="small",
                    needs_maintainer_ok=False,
                    maintainer_declined=False,
                    outsider_can_do=True,
                    claimed=False,
                    already_addressed_by=[],
                    summary="Fix the Windows path crash.",
                    first_step="Reproduce in `src/paths.py`, then check `src/windows.py`.",
                    evidence=["#1"],
                ),
                agents.IssueOpportunity(  # not in the batch: must be dropped by code
                    number=999,
                    actionable=True,
                    kind="bug",
                    size="small",
                    needs_maintainer_ok=False,
                    maintainer_declined=False,
                    outsider_can_do=True,
                    claimed=False,
                    already_addressed_by=[],
                    summary="x",
                    first_step="x",
                    evidence=[],
                ),
            ]
        )
    if schema is agents.VerifierResult:
        import re

        keys = re.findall(r'"key": "([^"]+)"', user)
        return agents.VerifierResult(
            checks=[agents.ClaimCheck(key=k, supported=True, confidence="high", problem=None) for k in keys]
        )
    raise AssertionError(schema)


@pytest.fixture
async def repo_name(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[str]:
    name = f"test/e2e-{uuid.uuid4().hex[:8]}"
    try:
        await graph.setup_checkpointer()
    except Exception as e:  # pragma: no cover
        pytest.skip(f"Postgres not available: {e}")
    monkeypatch.setattr(GitHubClient, "__init__", lambda self, *a, **k: None)
    monkeypatch.setattr(GitHubClient, "aclose", lambda self: _noop())
    monkeypatch.setattr(GitHubClient, "fetch_items", fake_fetch)
    monkeypatch.setattr(GitHubClient, "fetch_repo_docs", fake_docs)
    monkeypatch.setattr(GitHubClient, "canonical_name", same_name)
    monkeypatch.setattr(memory, "embed", fake_embed)
    monkeypatch.setattr(LLMGateway, "__init__", lambda self, run_id, repo_id, client=None: None)
    monkeypatch.setattr(LLMGateway, "parse", fake_parse)
    monkeypatch.setattr(get_settings(), "reports_dir", str(tmp_path))
    yield name
    async with session_scope() as s:
        await s.execute(delete(Repo).where(Repo.full_name == name))
    await db.get_engine().dispose()
    db._engine = None


async def _noop() -> None:
    return None


async def _create(name: str, role: str) -> None:
    async with session_scope() as s:
        s.add(Repo(full_name=name, role=role))


async def test_full_pipeline(repo_name: str) -> None:
    await _create(repo_name, "maintainer")
    state = await graph.run_health_report(repo_name)

    keys = {f["key"] for f in state["findings"]}
    assert "dup:#2-#1" in keys
    assert "close_candidate:#4" in keys
    assert [c["name"] for c in state["clusters"]] == ["Windows paths"]

    md = Path(state["report_path"]).read_text()
    assert "## Close candidates" in md and "Already fixed" in md
    assert "## Open issues" in md  # maintainers get issue triage from the Scout too

    async with session_scope() as s:
        run = await s.get(Run, state["run_id"])
        steps = (
            (
                await s.execute(
                    select(RunEvent.node)
                    .where(RunEvent.run_id == state["run_id"], RunEvent.kind == "node_end")
                    .order_by(RunEvent.id)  # without it Postgres may return rows in any order
                )
            )
            .scalars()
            .all()
        )
    assert run is not None and run.status == "done" and run.report_path == state["report_path"]
    assert steps == ["fetch", "link", "analyse_groups", "assess_prs", "verify", "scout", "report"]
    assert "Thanks! #3 was fixed in #5." in md

    async with session_scope() as s:
        repo = (await s.execute(select(Repo).where(Repo.full_name == repo_name))).scalar_one()
        reports = (await s.execute(select(Report).where(Report.repo_id == repo.id))).scalars().all()
    assert len(reports) == 1

    # A second run on unchanged data reports no new findings.
    state2 = await graph.run_health_report(repo_name)
    assert "New findings: 0" in Path(state2["report_path"]).read_text()


async def test_contributor_pipeline(repo_name: str) -> None:
    await _create(repo_name, "contributor")
    state = await graph.run_health_report(repo_name)

    assert [o["number"] for o in state["opportunities"]] == [1]  # #2 is a marked duplicate; #999 not asked
    assert state["pulse"]["verdict"] == "inactive"  # last merge is older than the window
    md = Path(state["report_path"]).read_text()
    assert "# Contributor report" in md
    assert "## Good places to start" in md and "Fix the Windows path crash." in md
    assert "## Close candidates" not in md  # maintainer sections are not part of this view
    assert "## Before you open a PR here" in md and "- Open an issue before any feature PR." in md
    assert state["opportunities"][0]["unverified_paths"] == ["src/windows.py"]
    assert "not found in the repo, check: src/windows.py" in md
