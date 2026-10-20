from __future__ import annotations

from conftest import make_item

from nightshift.health.report import build_findings_for_verification


def _verdict(number: int, **kw: object) -> dict[str, object]:
    base = {
        "number": number,
        "signals": {},
        "category": "close_candidate",
        "reason_code": "superseded",
        "confidence": "high",
        "explanation": "Superseded by #12.",
        "evidence": [f"#{number}", "#12"],
        "decision_question": None,
        "closing_message": "Thanks!",
    }
    return {**base, **kw}


def test_close_candidate_carries_resolved_evidence() -> None:
    items = {10: make_item(10), 12: make_item(12, state="MERGED")}
    findings = build_findings_for_verification([], [_verdict(10)], items)
    assert len(findings) == 1
    ids = [e["id"] for e in findings[0]["evidence"]]
    assert ids == ["#10", "#12", "signals"]


def test_unknown_evidence_ids_are_dropped() -> None:
    items = {10: make_item(10)}
    findings = build_findings_for_verification([], [_verdict(10, evidence=["#10", "#404", "comment:1"])], items)
    assert [e["id"] for e in findings[0]["evidence"]] == ["#10", "signals"]


def test_out_of_scope_without_cited_evidence_is_not_proposed() -> None:
    items = {10: make_item(10)}
    v = _verdict(10, reason_code="out_of_scope", confidence="low", evidence=["#10"])
    assert build_findings_for_verification([], [v], items) == []


def test_active_and_reasonless_closes_are_skipped() -> None:
    items = {10: make_item(10), 11: make_item(11)}
    verdicts = [_verdict(10, category="active"), _verdict(11, reason_code="none")]
    assert build_findings_for_verification([], verdicts, items) == []


def test_duplicates_become_findings() -> None:
    items = {1: make_item(1, kind="issue"), 2: make_item(2, kind="issue")}
    clusters = [
        {
            "name": "x",
            "summary": "",
            "numbers": [1, 2],
            "duplicates": [{"duplicate": 2, "original": 1, "reason": "same bug"}],
        }
    ]
    findings = build_findings_for_verification(clusters, [], items)
    assert findings[0]["key"] == "dup:#2-#1"
