"""How useful the PR checker is, in numbers: run it on pull requests maintainers have already decided, and
compare its verdict with what they did.

    nightshift bench pydantic/pydantic --merged 10 --closed 10

Cases are outside contributors' PRs closed in the last `days` that change code (no bots, drafts or reverts):
the most recent `merged` ones and the most recent `closed` (unmerged) ones a maintainer closed (not a bot's
stale or policy rule, not the author withdrawing it), at most PER_AUTHOR from one
author and one per issue, so one contributor's series or five competing PRs for one bug do not make up the
sample. Each is checked as it was decided (its head, on the base it was written on), so a merged PR's final,
reviewed code is expected to be "ready" and a closed one to need work. A closed PR can be closed for reasons
code cannot see (a duplicate, out of scope), so the maintainer's last words are kept beside the verdict.

The verdict compared is the checker's "must fix" list: empty means ready for review, anything in it means
blocked. Its "should" items are suggestions and not counted. The PR's own CI result is recorded too.

Results are written after every case to reports/<repo>/benchmark/<date>.json and .md, so a stopped run
can be resumed by running it again. Nothing is posted anywhere.
"""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import structlog
from pydantic import BaseModel

from nightshift.config import get_settings
from nightshift.github import GitHubClient, GitHubError
from nightshift.worker.prcheck import check_pr
from nightshift.worker.verify import is_doc_path

log = structlog.get_logger()

MAINTAINERS = ("OWNER", "MEMBER", "COLLABORATOR")
BOTS = ("dependabot", "renovate", "pre-commit-ci", "github-actions")
MAX_FILES = 40  # a PR this big is a rewrite, not a review case
MAX_LINES = 1500
FAILED = ("failure", "timed_out", "action_required")
PER_AUTHOR = 2
ISSUE_REF = re.compile(r"#(\d+)")


class Case(BaseModel):
    number: int
    url: str
    title: str
    author: str
    outcome: str  # merged | closed
    changes_requested: bool = False  # a maintainer asked for changes before the decision
    ci: str = "none"  # passed | failed | none: the PR's own checks on its final head
    closing_note: str = ""  # a closed PR: the maintainer's last comment
    closed_by: str = ""  # who closed it; a bot means the repo's policy (pydantic: author not assigned), not code
    verdict: str = ""
    blocked: bool | None = None  # the checker's "must fix" list was not empty
    incomplete: bool = False  # part of the check could not run (a model error, the spend cap): not scored
    must: list[str] = []
    should: int = 0
    seconds: float = 0.0
    cost_usd: float = 0.0
    error: str = ""

    @property
    def by_policy(self) -> bool:
        """Closed by a bot: an automated rule decided, not a maintainer reading the code."""
        return self.outcome == "closed" and self.closed_by.endswith("[bot]")

    @property
    def agrees(self) -> bool | None:
        if self.blocked is None or self.by_policy or (self.incomplete and not self.blocked):
            return None
        return self.blocked == (self.outcome == "closed")


async def pick_cases(full_name: str, merged: int, closed: int, days: int) -> list[Case]:
    """The most recent decided PRs from outside contributors that change code."""
    since = (datetime.now(UTC) - timedelta(days=days)).date().isoformat()
    want = {"merged": merged, "closed": closed}
    picked: dict[str, list[Case]] = {"merged": [], "closed": []}
    authors: dict[str, int] = {}
    issues: set[int] = set()
    gh = GitHubClient()
    try:
        for page in range(1, 6):
            q = f"repo:{full_name} is:pr is:closed closed:>={since}"
            found = await gh.rest_get(f"/search/issues?q={quote(q)}&sort=updated&order=desc&per_page=100&page={page}")
            items = found.get("items", [])
            for item in items:
                if all(len(picked[k]) >= want[k] for k in want):
                    return picked["merged"] + picked["closed"]
                login = (item.get("user") or {}).get("login") or ""
                refs = {int(n) for n in ISSUE_REF.findall(f"{item.get('title', '')} {item.get('body') or ''}")}
                if authors.get(login, 0) >= PER_AUTHOR or refs & issues or item.get("title", "").startswith("Revert"):
                    continue
                case = await _case(gh, full_name, item, want, picked)
                if case:
                    picked[case.outcome].append(case)
                    authors[login] = authors.get(login, 0) + 1
                    issues.update(refs - {case.number})
            if len(items) < 100:
                break
    finally:
        await gh.aclose()
    return picked["merged"] + picked["closed"]


async def _case(
    gh: GitHubClient, full_name: str, item: dict[str, Any], want: dict[str, int], picked: dict[str, list[Case]]
) -> Case | None:
    user = item.get("user") or {}
    login = user.get("login") or ""
    if item.get("author_association") in MAINTAINERS or user.get("type") == "Bot" or login.startswith(BOTS):
        return None
    if item.get("draft"):
        return None
    n = item["number"]
    pr = await gh.rest_get(f"/repos/{full_name}/pulls/{n}")
    outcome = "merged" if pr.get("merged_at") else "closed"
    if len(picked[outcome]) >= want[outcome]:
        return None
    if pr.get("changed_files", 0) > MAX_FILES or pr.get("additions", 0) + pr.get("deletions", 0) > MAX_LINES:
        return None
    files = await gh.rest_get(f"/repos/{full_name}/pulls/{n}/files?per_page=100")
    if not any(not is_doc_path(f["filename"]) for f in files):
        return None  # docs only: nothing to run
    reviews = await gh.rest_get(f"/repos/{full_name}/pulls/{n}/reviews?per_page=100")
    case = Case(
        number=n, url=pr["html_url"], title=pr["title"], author=login, outcome=outcome,
        changes_requested=any(r.get("state") == "CHANGES_REQUESTED" for r in reviews),
        ci=await _ci(gh, full_name, pr["head"]["sha"]),
    )  # fmt: skip
    if outcome == "closed":
        events = await gh.rest_get(f"/repos/{full_name}/issues/{n}/events?per_page=100")
        closed = [e for e in events if e.get("event") == "closed"]
        case.closed_by = ((closed[-1].get("actor") or {}).get("login") or "") if closed else ""
        if case.by_policy or case.closed_by == login:
            return None  # closed by a bot (stale, policy) or withdrawn by its author: no maintainer judged the code
        comments = await gh.rest_get(f"/repos/{full_name}/issues/{n}/comments?per_page=100")
        said = [c for c in comments if c.get("author_association") in MAINTAINERS]
        case.closing_note = " ".join((said[-1].get("body") or "").split())[:300] if said else ""
    return case


async def _ci(gh: GitHubClient, full_name: str, sha: str) -> str:
    try:
        runs = (await gh.rest_get(f"/repos/{full_name}/commits/{sha}/check-runs?per_page=100")).get("check_runs", [])
    except GitHubError:
        return "none"
    done = [r.get("conclusion") for r in runs if r.get("status") == "completed"]
    if not done:
        return "none"
    return "failed" if any(c in FAILED for c in done) else "passed"


def paths(full_name: str, stamp: str) -> tuple[Path, Path]:
    folder = Path(get_settings().reports_dir) / full_name.replace("/", "__") / "benchmark"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{stamp}.json", folder / f"{stamp}.md"


async def run_benchmark(full_name: str, merged: int, closed: int, days: int, stamp: str | None = None) -> Path:
    """Pick the cases (or reload them from the same day's file), check each one not yet checked, and write the
    results after every case. Returns the Markdown report's path."""
    stamp = stamp or datetime.now(UTC).strftime("%Y-%m-%d")
    data_path, md_path = paths(full_name, stamp)
    if data_path.exists():
        cases = [Case.model_validate(c) for c in json.loads(data_path.read_text())["cases"]]
    else:
        cases = await pick_cases(full_name, merged, closed, days)
    for case in cases:
        if case.blocked is not None:
            continue
        log.info("bench_case", repo=full_name, number=case.number, outcome=case.outcome)
        started = time.monotonic()
        try:
            report = await check_pr(case.url)
            case.verdict, case.must, case.should = report.verdict, report.must, len(report.should)
            case.blocked, case.cost_usd = bool(report.must), report.cost_usd
            case.incomplete = bool(report.not_run)
            case.error = ""
        except Exception as e:  # one PR that cannot be checked does not stop the rest
            case.error = f"{type(e).__name__}: {e}"[:500]
        case.seconds = round(time.monotonic() - started, 1)
        _write(full_name, cases, data_path, md_path, days)
    _write(full_name, cases, data_path, md_path, days)
    return md_path


def _write(full_name: str, cases: list[Case], data_path: Path, md_path: Path, days: int) -> None:
    data_path.write_text(json.dumps({"repo": full_name, "days": days, "cases": [c.model_dump() for c in cases]},
                                    indent=1))  # fmt: skip
    md_path.write_text(to_markdown(full_name, cases, days))


def summary(cases: list[Case]) -> dict[str, Any]:
    checked = [c for c in cases if c.blocked is not None]
    policy = [c for c in checked if c.by_policy]
    partial = [c for c in checked if c.incomplete and not c.blocked and not c.by_policy]
    done = [c for c in checked if not c.by_policy and c not in partial]  # scored: complete checks, by a person
    merged = [c for c in done if c.outcome == "merged"]
    closed = [c for c in done if c.outcome == "closed"]
    blocked = [c for c in done if c.blocked]
    return {
        "checked": len(checked),
        "scored": len(done),
        "by_policy": len(policy),
        "not_fully_checked": len(partial),
        "policy_blocked": sum(bool(c.blocked) for c in policy),
        "failed_to_check": sum(bool(c.error) for c in cases),
        "agree": sum(bool(c.agrees) for c in done),
        "merged_ready": sum(not c.blocked for c in merged),
        "merged": len(merged),
        "closed_blocked": sum(bool(c.blocked) for c in closed),
        "closed": len(closed),
        "blocks_right": sum(c.outcome == "closed" for c in blocked),
        "blocked": len(blocked),
        "ci_failed": sum(c.ci == "failed" for c in done),
        "ci_failed_blocked": sum(c.ci == "failed" and bool(c.blocked) for c in done),
        "minutes": round(sum(c.seconds for c in checked) / 60 / max(len(checked), 1), 1),
        "cost_usd": round(sum(c.cost_usd for c in checked) / max(len(checked), 1), 3),
    }


def _pct(a: int, b: int) -> str:
    return f"{a} of {b} ({round(100 * a / b)}%)" if b else "none yet"


def to_markdown(full_name: str, cases: list[Case], days: int) -> str:
    s = summary(cases)
    unchecked = f" ({s['failed_to_check']} could not be checked)" if s["failed_to_check"] else ""
    out = [
        f"# PR checker benchmark: {full_name}",
        "",
        f"Outside contributors' pull requests decided in the last {days} days, checked as they were decided "
        "and compared with what the maintainers did. Nothing was posted.",
        "",
        "| | |",
        "| --- | --- |",
        f"| Checked | {s['checked']} of {len(cases)}{unchecked}"
        + (f"; {s['not_fully_checked']} not fully checked, not scored" if s["not_fully_checked"] else "") + " |",
        f"| Verdict matches the maintainers | {_pct(s['agree'], s['scored'])} |",
        f"| Merged PRs it called ready | {_pct(s['merged_ready'], s['merged'])} |",
        f"| PRs a maintainer closed, that it blocked | {_pct(s['closed_blocked'], s['closed'])} |",
        f"| Closed by a bot (the repo's policy, not scored) | {s['by_policy']}, of which it blocked "
        f"{s['policy_blocked']} |",
        f"| Its blocks that maintainers also rejected | {_pct(s['blocks_right'], s['blocked'])} |",
        f"| PRs whose own CI failed, that it blocked | {_pct(s['ci_failed_blocked'], s['ci_failed'])} |",
        f"| Time per PR | {s['minutes']} min |",
        f"| Cost per PR | ${s['cost_usd']:.2f} |",
        "",
        "A closed PR can be closed for reasons code cannot see (a duplicate, out of scope); the maintainer's last",
        "words are in the table. \"Blocked\" means the checker's must-fix list was not empty.",
        "",
        "| PR | Maintainers | CI | Checker | Why it blocked, or the maintainer's last words | Time | Cost |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]  # fmt: skip
    for c in cases:
        decided = c.outcome + (" after changes" if c.changes_requested and c.outcome == "merged" else "")
        decided += f" by {c.closed_by}" if c.outcome == "closed" and c.closed_by else ""
        if c.error:
            checker, why = "could not check", c.error
        elif c.blocked is None:
            checker, why = "not run yet", ""
        else:
            mark = "·" if c.agrees is None else "✓" if c.agrees else "✗"
            checker = f"{mark} {'blocked' if c.blocked else 'not fully checked' if c.incomplete else 'ready'}"
            why = "; ".join(c.must) if c.blocked else (f"closed: {c.closing_note}" if c.outcome == "closed" else "")
        why = why.replace("|", "\\|").replace("\n", " ")[:400]
        title = c.title.replace("|", "\\|")[:70]
        out.append(f"| [#{c.number}]({c.url}) {title} | {decided} | {c.ci} | {checker} | {why} | "
                   f"{c.seconds / 60:.0f} min | ${c.cost_usd:.2f} |")  # fmt: skip
    return "\n".join(out) + "\n"
