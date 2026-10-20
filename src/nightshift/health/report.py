"""Turning verdicts into verifiable findings, and findings into the Markdown report."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from nightshift.config import get_settings
from nightshift.db import Item, Report, session_scope
from nightshift.health.agents import item_view

REASON_LABEL = {
    "already_fixed": "Already fixed",
    "superseded": "Superseded",
    "linked_issue_not_planned": "Linked issue not planned",
    "abandoned": "Abandoned",
    "out_of_scope": "Out of scope",
    "none": "",
}
# Reasons that code-computed signals can establish on their own.
SIGNAL_BACKED = {"already_fixed", "superseded", "linked_issue_not_planned", "abandoned"}


def _resolve(evidence_id: str, by_number: dict[int, Item]) -> dict[str, str] | None:
    if evidence_id.startswith("#") and evidence_id[1:].isdigit():
        item = by_number.get(int(evidence_id[1:]))
        if item:
            return {"id": evidence_id, "text": json.dumps(item_view(item, body_chars=800), ensure_ascii=False)}
    if evidence_id.startswith("comment:"):
        cid = evidence_id.split(":", 1)[1]
        for item in by_number.values():
            for c in item.data.get("comments", []):
                if str(c["id"]) == cid:
                    return {
                        "id": evidence_id,
                        "text": f"Comment by {c['author']} on {item.ref} ({c['created_at'][:10]}): {c['body']}",
                    }
    return None


def build_findings_for_verification(
    clusters: list[dict[str, Any]], verdicts: list[dict[str, Any]], by_number: dict[int, Item]
) -> list[dict[str, Any]]:
    """Findings the Verifier must confirm. Evidence ids are resolved by code; unknown ids are dropped."""
    findings: list[dict[str, Any]] = []

    for c in clusters:
        for d in c["duplicates"]:
            dup_item = by_number.get(d["duplicate"])
            if dup_item is None or dup_item.state != "OPEN":
                continue  # already closed: nothing for the maintainer to do
            evidence = [
                e for e in (_resolve(f"#{d['duplicate']}", by_number), _resolve(f"#{d['original']}", by_number)) if e
            ]
            findings.append(
                {
                    "key": f"dup:#{d['duplicate']}-#{d['original']}",
                    "type": "duplicate",
                    "claim": f"#{d['duplicate']} duplicates #{d['original']}: {d['reason']}",
                    "evidence": evidence,
                    "confidence": "medium",
                    "data": d,
                }
            )

    for v in verdicts:
        if v["category"] == "active":
            continue
        n = v["number"]
        ids = dict.fromkeys([f"#{n}", *v["evidence"]])
        evidence = [e for e in (_resolve(i, by_number) for i in ids) if e]
        evidence.append({"id": "signals", "text": "Computed by code: " + json.dumps(v["signals"])})
        if v["category"] == "close_candidate":
            if v["reason_code"] == "none":
                continue
            cited_beyond_pr = len(evidence) > 2
            if v["reason_code"] not in SIGNAL_BACKED and not cited_beyond_pr:
                continue  # no evidence, no recommendation
            claim = f"PR #{n} can be closed ({REASON_LABEL[v['reason_code']]}). {v['explanation']}"
        elif v["category"] == "ready_to_merge":
            claim = f"PR #{n} looks ready to merge. {v['explanation']}"
        else:
            claim = f"PR #{n} is waiting on a maintainer decision: {v['decision_question']} {v['explanation']}"
        findings.append(
            {
                "key": f"{v['category']}:#{n}",
                "type": v["category"],
                "claim": claim,
                "evidence": evidence,
                "confidence": v["confidence"],
                "data": v,
            }
        )
    return findings


def _link(n: int, by_number: dict[int, Item]) -> str:
    item = by_number.get(n)
    return f"[#{n}]({item.url})" if item else f"#{n}"


def _title(n: int, by_number: dict[int, Item]) -> str:
    item = by_number.get(n)
    if not item:
        return ""
    title = item.title.replace("|", "\\|")
    title = title if len(title) <= 80 else title[:79].rsplit(" ", 1)[0] + "…"
    return title if item.state == "OPEN" else f"{title} ({item.state.lower()})"


async def _previous_keys(repo_id: int, run_id: str, view: str = "maintainer") -> set[str] | None:
    """Finding keys of the previous report of the same view, or None if there is none."""
    async with session_scope() as s:
        prev = (
            await s.execute(
                select(Report)
                .where(Report.repo_id == repo_id, Report.run_id != run_id)
                .order_by(Report.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    if prev is None or prev.findings.get("view", "maintainer") != view:
        return None
    return {f["key"] for f in prev.findings.get("findings", [])}


async def render_report(
    repo: str,
    repo_id: int,
    findings: list[dict[str, Any]],
    clusters: list[dict[str, Any]],
    verdicts: list[dict[str, Any]],
    by_number: dict[int, Item],
    stats: dict[str, Any],
    run_id: str = "",
    rejected: list[dict[str, Any]] | None = None,
) -> str:

    def L(n: int) -> str:  # noqa: N802
        return _link(n, by_number)

    by_type: dict[str, list[dict[str, Any]]] = {}
    for f in findings:
        by_type.setdefault(f["type"], []).append(f)
    order = {"high": 0, "medium": 1, "low": 2}

    out = [f"# Health report: {repo}", ""]
    out.append(
        f"{datetime.now(UTC):%Y-%m-%d} · {stats.get('open_items', 0)} open items · "
        f"{len(clusters)} clusters · {len(findings)} verified findings · cost ${stats.get('cost_usd', 0):.2f}"
    )
    out += ["", "> Private report. NightShift never closes, labels or comments on its own.", ""]
    if stats.get("truncated"):
        out += [
            f"> **Partial data:** the fetch hit the {get_settings().max_items_per_run}-item cap, "
            "so some issues or PRs were not analysed.",
            "",
        ]
    skipped = stats.get("skipped_groups", 0) + stats.get("skipped_prs", 0)
    if skipped:
        out += [f"> {skipped} item(s) were skipped because the model declined or gave no usable answer.", ""]

    prev = await _previous_keys(repo_id, run_id)
    if prev is not None:
        now_keys = {f["key"] for f in findings}
        out += [
            "## Since last report",
            "",
            f"- New findings: {len(now_keys - prev)}",
            f"- Resolved or gone: {len(prev - now_keys)}",
            "",
        ]

    close = sorted(by_type.get("close_candidate", []), key=lambda f: order[f["confidence"]])
    out += ["## Close candidates", ""]
    if close:
        out += ["| PR | Reason | Confidence | Why |", "| --- | --- | --- | --- |"]
        for f in close:
            v = f["data"]
            out.append(
                f"| {L(v['number'])} {_title(v['number'], by_number)} | {REASON_LABEL[v['reason_code']]} | "
                f"{f['confidence']} | {v['explanation'].replace('|', '/')} |"
            )
        out += ["", "<details><summary>Drafted closing messages</summary>", ""]
        for f in close:
            v = f["data"]
            if v.get("closing_message"):
                out += [f"**#{v['number']}**", "", "> " + v["closing_message"].replace("\n", "\n> "), ""]
        out += ["</details>", ""]
    else:
        out += ["None found.", ""]

    ready = by_type.get("ready_to_merge", [])
    out += ["## Ready to merge", ""]
    out += [
        f"- {L(f['data']['number'])} {_title(f['data']['number'], by_number)}: {f['data']['explanation']}"
        for f in ready
    ] or ["None found."]
    out.append("")

    decide = by_type.get("needs_decision", [])
    out += ["## Needs a decision", ""]
    out += [f"- {L(f['data']['number'])} {f['data']['decision_question']}" for f in decide] or ["None found."]
    out.append("")

    dups = by_type.get("duplicate", [])
    out += ["## Duplicates", ""]
    out += [
        f"- {L(f['data']['duplicate'])} duplicates {L(f['data']['original'])}: {f['data']['reason']}" for f in dups
    ] or ["None found."]
    out.append("")

    out += ["## Related clusters", ""]
    if clusters:
        for c in sorted(clusters, key=lambda c: -len(c["numbers"])):
            out += [
                f"### {c['name']} ({len(c['numbers'])} items)",
                "",
                c["summary"],
                "",
                ", ".join(f"{L(n)} {_title(n, by_number)}" for n in c["numbers"]),
                "",
            ]
    else:
        out += ["None found.", ""]

    if rejected:
        out += ["<details><summary>Rejected by the Verifier (not recommended)</summary>", ""]
        out += [f"- **{r['claim']}**  \n  Why rejected: {r['problem']}" for r in rejected]
        out += ["", "</details>", ""]

    active = sum(1 for v in verdicts if v["category"] == "active")
    out += [
        "---",
        "",
        f"Open PRs reviewed: {len(verdicts)} ({active} active). "
        f"Findings proposed: {stats.get('findings_proposed', 0)}, verified: {stats.get('findings_verified', 0)}.",
    ]
    return "\n".join(out) + "\n"


async def save_report(
    repo: str, repo_id: int, run_id: str, findings: dict[str, Any], markdown: str, cost: float
) -> str:
    folder = Path(get_settings().reports_dir) / repo.replace("/", "__")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{datetime.now(UTC):%Y-%m-%d}-{run_id[:8]}.md"
    path.write_text(markdown)
    async with session_scope() as s:
        existing = (await s.execute(select(Report).where(Report.run_id == run_id))).scalar_one_or_none()
        if existing is None:
            s.add(Report(repo_id=repo_id, run_id=run_id, findings=findings, markdown=markdown, cost_usd=cost))
        else:
            existing.findings, existing.markdown, existing.cost_usd = findings, markdown, cost
    return str(path)


VERDICT_LABEL = {
    "welcoming": "Welcoming: outside PRs get merged and answered quickly",
    "slow": "Possible, but expect to wait",
    "hard": "Hard: outside PRs rarely land right now",
    "inactive": "Inactive: nothing is being merged",
}
SIZE_ORDER = {"small": 0, "medium": 1, "large": 2}
KIND_ORDER = {"bug": 0, "docs": 1, "chore": 2, "feature": 3, "other": 4, "question": 5}


def is_opportunity(o: dict[str, Any]) -> bool:
    """An issue an outsider can usefully take: real, not declined, not maintainer-only, not already claimed."""
    return bool(
        o["actionable"] and not o["maintainer_declined"] and o.get("outsider_can_do", True) and not o.get("claimed")
    )


def _days(value: float | None) -> str:
    return "no responses seen" if value is None else f"{value:g} days"


async def render_contributor_report(
    repo: str,
    repo_id: int,
    pulse: dict[str, Any],
    opportunities: list[dict[str, Any]],
    worked_on: list[dict[str, Any]],
    by_number: dict[int, Item],
    stats: dict[str, Any],
    run_id: str = "",
) -> str:
    def L(n: int) -> str:  # noqa: N802
        return _link(n, by_number)

    def cell(text: str) -> str:
        return text.replace("|", "/").replace("\n", " ")

    out = [f"# Contributor report: {repo}", ""]
    out.append(
        f"{datetime.now(UTC):%Y-%m-%d} · {stats.get('open_issues', 0)} open issues · "
        f"{stats.get('open_prs', 0)} open PRs · cost ${stats.get('cost_usd', 0):.2f}"
    )
    out += [
        "",
        "> For someone who does not maintain this repo. Before starting on an issue, comment on it "
        "so others know, and so a maintainer can say whether they want it.",
        "",
    ]
    if stats.get("truncated"):
        out += ["> **Partial data:** the fetch hit its item cap.", ""]
    if stats.get("issues_not_scouted"):
        out += [
            f"> The {stats['issues_not_scouted']} least recently active free issues were not analysed (cap).",
            "",
        ]

    actionable = [o for o in opportunities if is_opportunity(o)]
    prev = await _previous_keys(repo_id, run_id, view="contributor")
    if prev is not None:
        new = [o for o in actionable if f"opp:#{o['number']}" not in prev]
        out += ["## Since last report", ""]
        out += [f"- New: {L(o['number'])} {_title(o['number'], by_number)}" for o in new] or ["- Nothing new."]
        out.append("")

    p = pulse
    out += ["## Is it worth contributing?", "", f"**{VERDICT_LABEL[p['verdict']]}.** {p['verdict_reason']}", ""]
    rate = "n/a" if p["merge_rate"] is None else f"{p['merge_rate']:.0%}"
    out += [
        f"| Outside contributors, last {p['window_days']} days | |",
        "| --- | --- |",
        f"| PRs merged / closed unmerged / still open | {p['outside_prs_merged']} / "
        f"{p['outside_prs_closed_unmerged']} / {p['outside_prs_open']} |",
        f"| Merge rate of decided PRs | {rate} |",
        f"| Median time to first maintainer response on PRs | {_days(p['median_pr_response_days'])} |",
        f"| Median time to first maintainer response on issues | {_days(p['median_issue_response_days'])} |",
        f"| Open PRs waiting a week or more with no maintainer response | {p['outside_prs_waiting']} |",
        f"| Last merge (anyone) | {p['last_merge'] or 'none seen'} |",
        "",
    ]

    def order(o: dict[str, Any]) -> tuple[int, int, int]:
        return (SIZE_ORDER[o["size"]], KIND_ORDER[o["kind"]], -o["number"])

    def stalled_note(o: dict[str, Any]) -> str:
        if o.get("status") != "stalled":
            return ""
        prs = ", ".join(f"#{n}" for n in o.get("stalled_prs", []))
        return f" Earlier attempt {prs} stalled: you could pick it up."

    start = sorted((o for o in actionable if not o["needs_maintainer_ok"]), key=order)
    out += ["## Good places to start", ""]
    if start:
        out += ["| Issue | Type | Size | What to do | First step |", "| --- | --- | --- | --- | --- |"]
        for o in start:
            out.append(
                f"| {L(o['number'])} {_title(o['number'], by_number)} | {o['kind']} | {o['size']} | "
                f"{cell(o['summary'] + stalled_note(o))} | {cell(o['first_step'])} |"
            )
    else:
        out.append("None found.")
    out.append("")

    ask = sorted((o for o in actionable if o["needs_maintainer_ok"]), key=order)
    out += ["## Ask the maintainers first", ""]
    out += [
        f"- {L(o['number'])} {_title(o['number'], by_number)} ({o['size']}): {o['summary']}{stalled_note(o)}"
        for o in ask
    ] or ["None found."]
    out.append("")

    out += ["## Already being worked on", ""]

    def on_it(w: dict[str, Any]) -> str:
        if w["source"] == "claimed":
            return f"claimed by @{w.get('by') or 'someone'} (fix linked or said they are taking it, no PR yet)"
        return ", ".join(L(n) for n in w["prs"])

    out += [
        f"- {L(w['issue'])} {_title(w['issue'], by_number)} → {on_it(w)}"
        for w in sorted(worked_on, key=lambda w: -w["issue"])
    ] or ["Nothing found."]
    out.append("")

    skip = [o for o in opportunities if not is_opportunity(o)]
    if skip:
        out += [f"<details><summary>Not worth your time ({len(skip)})</summary>", ""]
        for o in sorted(skip, key=lambda o: -o["number"]):
            if o["maintainer_declined"]:
                why = "a maintainer declined it"
            elif o.get("claimed"):
                why = "someone already has a fix or says they are taking it"
            elif not o.get("outsider_can_do", True):
                why = "needs maintainer-only access"
            else:
                why = f"not actionable ({o['kind']})"
            out.append(f"- {L(o['number'])} {_title(o['number'], by_number)}: {why}. {o['summary']}")
        out += ["", "</details>", ""]

    out += [
        "---",
        "",
        "Outside contributors are authors who are not the repo's owner, members or collaborators; bots are "
        "excluded. Response times use the first maintainer comment or review that GitHub returned. "
        f"Issues analysed: {stats.get('issues_scouted', 0)}.",
    ]
    return "\n".join(out) + "\n"
