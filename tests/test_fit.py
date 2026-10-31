"""How well a repo fits you, from your GitHub languages and contributions. GitHub is not called."""

from __future__ import annotations

from nightshift.worker.fit import profile_from, score

ANSWER = {"user": {
    "repositories": {"nodes": [
        {"languages": {"edges": [{"size": 600, "node": {"name": "Python"}},
                                 {"size": 300, "node": {"name": "TypeScript"}},
                                 {"size": 900, "node": {"name": "Jupyter Notebook"}}]}},  # notebooks say little
        {"languages": {"edges": [{"size": 100, "node": {"name": "Python"}}]}},
    ]},
    "contributionsCollection": {"pullRequestContributionsByRepository": [
        {"repository": {"nameWithOwner": "aayushch/laya", "primaryLanguage": {"name": "Python"}},
         "contributions": {"totalCount": 3}},
    ]},
}}  # fmt: skip


def test_your_profile() -> None:
    you = profile_from(ANSWER)
    assert you["languages"] == {"Python": 0.7, "TypeScript": 0.3}
    assert you["prs"] == {"aayushch/laya": 3}


def test_a_repo_fits_by_your_languages_and_your_prs_there() -> None:
    you = profile_from(ANSWER)
    python = score({"Python": 900, "CSS": 100}, you, "o/py")
    assert python["score"] == 1.0 and python["label"] == "strong"
    assert python["why"] == "Python is 100% of its code; 70% of yours"  # CSS is left out
    ts = score({"TypeScript": 1000}, you, "o/ts")  # you write it, less than Python
    assert ts["score"] == 0.43 and ts["label"] == "some"
    go = score({"Go": 1000}, you, "o/go")
    assert (go["score"], go["label"], go["why"]) == (0.0, "little", "Go is 100% of its code; you do not write it")
    laya = score({"Go": 1000}, you, "AayushCh/Laya")  # your PRs there count, whatever the language
    assert laya["score"] == 0.25 and laya["prs_here"] == 3 and "you opened 3 PRs here this year" in laya["why"]
    assert score({}, profile_from({"user": {}}), "o/x")["label"] == "little"
